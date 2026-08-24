"""Project-specific security tripwires for fa-web-api.

Generic scanners (gitleaks, pip-audit, Semgrep, CodeQL) find generic problems.
This finds OUR problems: the handful of invariants that, if they ever quietly
stop being true, hand out alumni PII or let someone trigger a mass email. Each
check is written as a TRIPWIRE, not a heuristic — it holds an explicit list of
what is allowed today and reports anything that has DIVERGED from it. That is
the difference between a scan that is worth reading on a Sunday night and one
that cries wolf until it is muted.

Run it locally the same way CI does::

    python -m scripts.security_scan            # human-readable
    python -m scripts.security_scan --json out.json

Exit code is 0 unless ``--fail-on`` is given (CI passes ``--fail-on high``), so
a report can be delivered without a red X becoming the weekly normal.

⚠️ WHEN A TRIPWIRE FIRES, THE FIX IS USUALLY THE CODE, NOT THE ALLOWLIST.
Widening a list here is a security decision. Do it deliberately, in a commit
that says why, and never as a reflex to get a build green.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
ROUTES = ROOT / "app" / "api" / "routes"

CRITICAL, HIGH, MEDIUM, LOW, INFO = "critical", "high", "medium", "low", "info"
_ORDER = {CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3, INFO: 4}


# --- The allowlists (the tripwire settings) ----------------------------------

#: Routes that are deliberately reachable WITHOUT an authenticated user.
#: Every one of these carries its own credential instead: the login pair is
#: pre-authentication by definition and is per-IP rate limited (#423); the
#: survey routes authenticate a stateless HMAC token in the path; the cron
#: routes check CRON_SECRET (see :func:`check_cron_auth`, which enforces that
#: separately rather than trusting this list).
#:
#: ⚠️ A NEW ENTRY HERE IS A NEW UNAUTHENTICATED SURFACE ON A DATABASE OF REAL
#: PEOPLE. Adding one is a decision to review, not a formality.
EXPECTED_PUBLIC_ROUTES = {
    # Pre-authentication by definition — they exist to decide whether a sign-in
    # may be attempted and to record how it went. Per-IP rate limited (#423) and
    # deliberately answerless about whether an account exists.
    ("POST", "/auth/login/precheck"),
    ("POST", "/auth/login/record"),
    # Cron. NOT trusted to this list: check_cron_auth independently proves each
    # one verifies CRON_SECRET with hmac.compare_digest and fails CLOSED when the
    # secret is unset. These send real email and spend real function budget.
    ("POST", "/survey/cron/run"),
    ("GET", "/survey/cron/run"),
    ("POST", "/storage/cron/headshot-sweep"),
    ("GET", "/storage/cron/headshot-sweep"),
    # The survey. Authenticated by a stateless HMAC token in the path, with the
    # 7-day expiry signed INTO it. This is the one public surface that reads and
    # writes real alumni PII, so it is also the one that has produced the most
    # findings historically — treat any change here as security-relevant.
    ("GET", "/survey/respond/{token}"),
    ("POST", "/survey/respond/{token}"),
    ("POST", "/survey/respond/{token}/links"),
    ("POST", "/survey/respond/{token}/photo"),
    # Public on purpose: the maintenance page has to render for signed-out
    # visitors, and the readiness probe has to answer before anyone is signed in.
    # /health/db opens a database connection per call and returns no detail on
    # failure (checked 2026-08-24) — a mild availability lever, nothing more.
    ("GET", "/maintenance/status"),
    ("GET", "/health/db"),
}

#: Framework and infrastructure paths that were never going to carry a session.
#: Serving these is not a decision anyone needs to re-review weekly.
UNAUTHENTICATED_BY_DESIGN = {
    "/",
    "/health",
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
    "/favicon.ico",
    "/favicon.svg",
}

#: The resolvers that actually authenticate a request. A route is protected iff
#: one of these appears ANYWHERE in its FastAPI dependency tree.
#:
#: ⚠️ THIS IS CHECKED BY IMPORTING THE APP AND WALKING THE REAL GRAPH, not by
#: reading the source. The first version of this check pattern-matched parameter
#: text and reported 17 false criticals, because protection here is spelled as
#: typed aliases (``EmploymentWriteRateLimit``, ``CurrentDBUserAllowMustChange``,
#: the per-route rate limiters) that expand to ``Annotated[..., Depends(...)]``.
#: Any text-based check has to keep pace with every new alias; the dependency
#: graph cannot be fooled by naming.
AUTH_RESOLVERS = {
    "get_current_user",
    "get_current_db_user",
    "get_current_db_user_allow_must_change",
    "actor_guard",
}

ROUTE_RX = re.compile(
    r'@(?:\w+)\.(get|post|put|patch|delete)\(\s*["\']([^"\']+)["\'](.*?)\)\s*\n'
    r"async def (\w+)\(\s*(.*?)\n\)\s*(?:->|:)",
    re.S,
)


def _findings_sort(findings: list[dict]) -> list[dict]:
    return sorted(findings, key=lambda f: (_ORDER[f["severity"]], f["check"], f["where"]))


def _py_files(*roots: pathlib.Path):
    for root in roots:
        for p in sorted(root.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            yield p


def _rel(p: pathlib.Path) -> str:
    return str(p.relative_to(ROOT)).replace("\\", "/")


# --- Checks ------------------------------------------------------------------


def parse_routes() -> list[tuple[str, str, str, str, str]]:
    """(file, VERB, path, function, decorator+signature text) for every route.

    Source-level, and used only by the checks that genuinely need source (cron
    bodies). Route AUTHENTICATION is decided from the live app instead — see
    :func:`live_routes`.
    """
    out = []
    for p in _py_files(ROUTES):
        for m in ROUTE_RX.finditer(p.read_text(encoding="utf-8")):
            verb, path, decor, fn, sig = m.groups()
            out.append((_rel(p), verb.upper(), path, fn, decor + sig))
    return out


def _dependency_names(dependant) -> set[str]:
    """Every callable name in a route's dependency tree, depth-first."""
    names: set[str] = set()
    stack = list(dependant.dependencies)
    while stack:
        dep = stack.pop()
        call = getattr(dep, "call", None)
        if call is not None:
            names.add(getattr(call, "__name__", type(call).__name__))
            # Class-based dependencies (the rate limiters) carry their guard on
            # the instance rather than in the graph, so look one level in.
            for attr in ("actor_guard", "guard", "dependency"):
                inner = getattr(call, attr, None)
                if inner is not None:
                    names.add(getattr(inner, "__name__", type(inner).__name__))
        stack.extend(dep.dependencies)
    return names


def _walk_routes(routes, seen=None):
    """Yield every real APIRoute, descending through included routers.

    ⚠️ FastAPI does not keep included routers' routes flat on ``app.routes`` in
    current versions — it inserts a ``_IncludedRouter`` wrapper that exposes the
    real one as ``original_router``. Iterating ``app.routes`` alone finds 5 routes
    out of 161 and would report the whole API as unreachable-and-therefore-fine.
    """
    seen = seen if seen is not None else set()
    for route in routes:
        if id(route) in seen:
            continue
        seen.add(id(route))
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _walk_routes(getattr(inner, "routes", []), seen)
            continue
        if getattr(route, "dependant", None) is not None:
            yield route
        nested = getattr(route, "routes", None)
        if nested:
            yield from _walk_routes(nested, seen)


def live_routes() -> list[tuple[str, str, str, set[str]]]:
    """(VERB, full path, endpoint name, dependency names) from the imported app."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from app.main import app  # noqa: PLC0415 — deliberately late, needs sys.path

    out = []
    for route in _walk_routes(app.routes):
        names = _dependency_names(route.dependant)
        # A rate-limited route's guard resolves the actor itself; its own
        # signature type is UserContext, which only the graph reveals.
        for verb in sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}):
            out.append((verb, route.path, route.name, names))
    return out


def check_unauthenticated_routes(_routes) -> list[dict]:
    """Every route resolves a user, or is on the reviewed public list.

    This is the highest-value check in the file. The realistic way this codebase
    starts leaking is not an exotic exploit — it is one new endpoint that forgets
    its dependency and is never noticed because it works fine in the browser,
    where a session always happens to exist.
    """
    findings = []
    seen_public = set()
    try:
        routes = live_routes()
    except Exception as exc:  # pragma: no cover - import failure is itself news
        return [
            {
                "check": "scanner-could-not-import-app",
                "severity": HIGH,
                "where": "app/main.py",
                "detail": (
                    f"Could not import the app to inspect its dependency graph, so "
                    f"route authentication was NOT checked this run: {exc!r}"
                ),
            }
        ]

    for verb, path, name, deps in routes:
        if deps & AUTH_RESOLVERS:
            continue
        if path in UNAUTHENTICATED_BY_DESIGN:
            continue
        key = (verb, path)
        seen_public.add(key)
        f, fn = "app/api/routes", name
        if key not in EXPECTED_PUBLIC_ROUTES:
            findings.append(
                {
                    "check": "unauthenticated-route",
                    "severity": CRITICAL,
                    "where": f"{f}:{fn}",
                    "detail": (
                        f"{verb} {path} takes no authenticated user and is not on the "
                        f"reviewed public list. If that is intended, add it to "
                        f"EXPECTED_PUBLIC_ROUTES in this file with a comment saying "
                        f"what credential it carries instead."
                    ),
                }
            )
    # The other direction: an allowlisted route that has since GAINED auth (or
    # been deleted) should be pruned, or the list slowly stops meaning anything.
    for verb, path in sorted(EXPECTED_PUBLIC_ROUTES - seen_public):
        findings.append(
            {
                "check": "stale-public-allowlist",
                "severity": INFO,
                "where": "scripts/security_scan.py",
                "detail": (
                    f"{verb} {path} is allowlisted as public but is no longer an "
                    f"unauthenticated route. Remove it from EXPECTED_PUBLIC_ROUTES."
                ),
            }
        )
    return findings


def check_cron_auth(_routes) -> list[dict]:
    """Anything under /cron/ verifies CRON_SECRET in constant time.

    These routes send real email and burn real function budget. An open one is
    not an information leak, it is a button anyone on the internet can press.

    ⚠️ Driven off the LIVE route graph, not the source parser. The regex parser
    matches 2 of the 4 cron routes — it cannot see a decorator spread over
    several lines with ``include_in_schema=False`` — and a cron check that
    silently skips half the cron routes is worse than no check, because it
    reports green.
    """
    findings = []
    try:
        live = live_routes()
    except Exception:  # already reported by check_unauthenticated_routes
        return []
    sources = {p: p.read_text(encoding="utf-8") for p in _py_files(ROUTES)}
    for verb, path, fn, _deps in live:
        if "/cron/" not in path:
            continue
        src = next((s for s in sources.values() if f"async def {fn}(" in s), None)
        f = next(
            (_rel(p) for p, s in sources.items() if f"async def {fn}(" in s),
            "app/api/routes",
        )
        if src is None:
            findings.append(
                {
                    "check": "unguarded-cron",
                    "severity": HIGH,
                    "where": f"{f}:{fn}",
                    "detail": (
                        f"Could not locate the source of {verb} {path} to verify "
                        f"its cron guard."
                    ),
                }
            )
            continue
        body = src[src.find(f"async def {fn}(") :][:4000]
        # The handler may delegate to a shared helper (_run_cron); accept either
        # the check inline or a call to a helper that contains it.
        guarded = "cron_secret" in body and "compare_digest" in body
        if not guarded:
            helper = re.search(r"return await (_\w+)\(", body)
            if helper:
                h = src[src.find(f"async def {helper.group(1)}(") :][:4000]
                guarded = "cron_secret" in h and "compare_digest" in h
        if not guarded:
            findings.append(
                {
                    "check": "unguarded-cron",
                    "severity": CRITICAL,
                    "where": f"{f}:{fn}",
                    "detail": (
                        f"{verb} {path} does not verify CRON_SECRET with "
                        f"hmac.compare_digest. Cron routes must fail CLOSED when the "
                        f"secret is unset."
                    ),
                }
            )
    return findings


def check_sql_injection() -> list[dict]:
    """No SQL text() built by interpolation, and no `:param::type` casts.

    Two different bugs, same line of code:

    * f-string / % / .format / + concatenation into text() is injection.
    * `:name::type` is not injection but is WORSE THAN IT LOOKS — SQLAlchemy
      parses the `::` cast as part of the parameter name and silently DROPS the
      bind, so the statement runs with the parameter missing. It shipped green
      once already because the tests fake the database. Use CAST(:name AS type).
    """
    findings = []
    text_call = re.compile(r"\btext\(\s*(?P<q>f?['\"]{1,3})", re.S)
    for p in _py_files(ROOT / "app", ROOT / "scripts"):
        src = p.read_text(encoding="utf-8")
        for m in text_call.finditer(src):
            line = src.count("\n", 0, m.start()) + 1
            # Grab the literal that follows, to the closing paren of text(...)
            chunk = src[m.start() : m.start() + 2000]
            end = chunk.find("\n)")
            literal = chunk[: end if end > 0 else 2000]
            if m.group("q").startswith("f"):
                findings.append(
                    {
                        "check": "sql-interpolation",
                        "severity": HIGH,
                        "where": f"{_rel(p)}:{line}",
                        "detail": "text() built from an f-string — use bind parameters.",
                    }
                )
            if re.search(r'"\s*\+|\'\s*\+|%\s*\(|\.format\(', literal):
                findings.append(
                    {
                        "check": "sql-interpolation",
                        "severity": HIGH,
                        "where": f"{_rel(p)}:{line}",
                        "detail": "text() assembled by concatenation/format — use bind parameters.",
                    }
                )
            if re.search(r":[A-Za-z_]\w*::", literal):
                findings.append(
                    {
                        "check": "sql-cast-swallows-bind",
                        "severity": MEDIUM,
                        "where": f"{_rel(p)}:{line}",
                        "detail": (
                            "`:name::type` in text() — the Postgres cast swallows the "
                            "bind parameter. Use CAST(:name AS type)."
                        ),
                    }
                )
    return findings


def check_dangerous_calls() -> list[dict]:
    """No eval/exec/pickle/shell — none of which this app has any reason to use."""
    patterns = {
        r"\beval\(": (HIGH, "eval()"),
        r"\bexec\(": (HIGH, "exec()"),
        r"\bpickle\.loads?\(": (HIGH, "pickle (deserialisation)"),
        r"\bos\.system\(": (HIGH, "os.system()"),
        r"shell\s*=\s*True": (HIGH, "subprocess(shell=True)"),
        r"\byaml\.load\((?!.*Loader)": (MEDIUM, "yaml.load without a safe Loader"),
    }
    findings = []
    for p in _py_files(ROOT / "app"):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for rx, (sev, what) in patterns.items():
                if re.search(rx, line):
                    findings.append(
                        {
                            "check": "dangerous-call",
                            "severity": sev,
                            "where": f"{_rel(p)}:{i}",
                            "detail": f"{what} — not used anywhere this app needs.",
                        }
                    )
    return findings


def check_cors() -> list[dict]:
    """A wildcard origin together with credentials is never correct."""
    findings = []
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    block = main[main.find("CORSMiddleware") :][:600]
    if '"*"' in block and "allow_credentials=True" in block:
        findings.append(
            {
                "check": "cors-wildcard-credentials",
                "severity": HIGH,
                "where": "app/main.py",
                "detail": "allow_origins includes '*' with allow_credentials=True.",
            }
        )
    cfg = (ROOT / "app" / "core" / "config.py").read_text(encoding="utf-8")
    if "cors_origins" in cfg and "*" in re.sub(r"#.*", "", cfg).split("cors_origins")[1][:400]:
        findings.append(
            {
                "check": "cors-wildcard-default",
                "severity": MEDIUM,
                "where": "app/core/config.py",
                "detail": "The CORS origins default appears to contain a wildcard.",
            }
        )
    return findings


def check_database_defense_in_depth() -> list[dict]:
    """Report that RLS is not the control here, so the grants stay reviewed.

    Verified empirically 2026-08-24 against the dev project: both `anon` and
    `authenticated` are refused by PostgREST on every table (no grants), so the
    data is NOT exposed. But that means privilege grants are the ONLY layer —
    there is no row-level security underneath them. One `GRANT SELECT … TO anon`,
    typed by hand or added by a future Supabase default, would expose everything
    at once with nothing behind it.
    """
    schema = (ROOT / "database" / "schema.sql").read_text(encoding="utf-8")
    tables = len(re.findall(r"^CREATE TABLE", schema, re.M))
    if re.search(r"ENABLE ROW LEVEL SECURITY", schema, re.I):
        return []
    return [
        {
            "check": "no-rls-defense-in-depth",
            "severity": INFO,
            "where": "database/schema.sql",
            "detail": (
                f"{tables} tables, none with row-level security. Access is closed "
                f"today because the anon/authenticated roles hold no grants — that is "
                f"the only layer. Re-verify after any Supabase project change: "
                f"GET /rest/v1/alumni with the publishable key must return 401/403."
            ),
        }
    ]


CHECKS = (
    ("unauthenticated routes", lambda r: check_unauthenticated_routes(r)),
    ("cron authentication", lambda r: check_cron_auth(r)),
    ("sql construction", lambda r: check_sql_injection()),
    ("dangerous calls", lambda r: check_dangerous_calls()),
    ("cors", lambda r: check_cors()),
    ("database defense in depth", lambda r: check_database_defense_in_depth()),
)


def run() -> list[dict]:
    routes = parse_routes()
    findings: list[dict] = []
    for _name, fn in CHECKS:
        findings.extend(fn(routes))
    return _findings_sort(findings)


def to_markdown(findings: list[dict], route_count: int) -> str:
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in _ORDER}
    lines = [
        "# fa-web-api — project security tripwires",
        "",
        f"{route_count} routes inspected. "
        + ", ".join(f"{counts[s]} {s}" for s in _ORDER if counts[s])
        + ("no findings." if not findings else ""),
        "",
    ]
    for f in findings:
        lines.append(f"- **{f['severity'].upper()}** `{f['check']}` — {f['where']}")
        lines.append(f"  - {f['detail']}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", help="write findings as JSON to this path")
    ap.add_argument(
        "--fail-on",
        choices=[CRITICAL, HIGH, MEDIUM, LOW, INFO],
        help="exit non-zero when a finding at or above this severity exists",
    )
    args = ap.parse_args()

    routes = parse_routes()
    findings = run()

    print(to_markdown(findings, len(routes)))
    if args.json:
        pathlib.Path(args.json).write_text(
            json.dumps(
                {"tool": "fa-web-api tripwires", "routes": len(routes), "findings": findings},
                indent=2,
            ),
            encoding="utf-8",
        )
    if args.fail_on:
        limit = _ORDER[args.fail_on]
        if any(_ORDER[f["severity"]] <= limit for f in findings):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
