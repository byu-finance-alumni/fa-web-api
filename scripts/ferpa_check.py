#!/usr/bin/env python3
"""Deterministic FERPA-compliance static check for fa-web-api.

No LLM, no network, no API key — pure stdlib static analysis of the repo so it
can run as a required CI status check. Exits non-zero (1) only when a HARD
requirement is missing; heuristic findings are printed as warnings and never
fail the build.

Run locally from the repo root:

    python scripts/ferpa_check.py

What it enforces is documented in scripts/FERPA_CHECKS.md.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Repo root = parent of the scripts/ directory holding this file.
REPO_ROOT = Path(__file__).resolve().parent.parent

SCHEMA_SQL = REPO_ROOT / "database" / "schema.sql"
RLS_SQL = REPO_ROOT / "database" / "rls_lockdown.sql"
DATABASE_PY = REPO_ROOT / "app" / "core" / "database.py"
APP_DIR = REPO_ROOT / "app"

# Alumni-data GET route files that should carry record-of-disclosure logging.
DISCLOSURE_ROUTE_FILES = [
    REPO_ROOT / "app" / "api" / "routes" / "alumni.py",
    REPO_ROOT / "app" / "api" / "routes" / "dashboard.py",
    REPO_ROOT / "app" / "api" / "routes" / "geography.py",
]


hard_failures: list[str] = []
warnings: list[str] = []


def fail(msg: str) -> None:
    hard_failures.append(msg)


def warn(msg: str) -> None:
    warnings.append(msg)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


# -----------------------------------------------------------------------------
# Check 1 (HARD): RLS coverage.
# Every CREATE TABLE <name> in schema.sql must have a matching
# ENABLE ROW LEVEL SECURITY target in rls_lockdown.sql. This is the most
# important check — an un-locked table is auto-exposed through Supabase's REST
# Data API with the publishable key that ships in the frontend bundle.
# -----------------------------------------------------------------------------
def check_rls_coverage() -> None:
    if not SCHEMA_SQL.exists():
        fail(f"RLS: schema file not found: {SCHEMA_SQL}")
        return
    if not RLS_SQL.exists():
        fail(f"RLS: lockdown file not found: {RLS_SQL}")
        return

    schema = read(SCHEMA_SQL)
    rls = read(RLS_SQL)

    # CREATE TABLE [IF NOT EXISTS] [schema.]name  — capture the bare table name.
    create_re = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"(?:[\"`]?[A-Za-z_][\w$]*[\"`]?\.)?"  # optional schema qualifier
        r"[\"`]?([A-Za-z_][\w$]*)[\"`]?",
        re.IGNORECASE,
    )
    schema_tables: list[str] = []
    seen: set[str] = set()
    for m in create_re.finditer(schema):
        name = m.group(1).lower()
        if name not in seen:
            seen.add(name)
            schema_tables.append(name)

    if not schema_tables:
        fail("RLS: no CREATE TABLE statements found in schema.sql (parse error?)")
        return

    # ENABLE ROW LEVEL SECURITY target table names.
    enable_re = re.compile(
        r"ALTER\s+TABLE\s+(?:ONLY\s+)?"
        r"(?:[\"`]?[A-Za-z_][\w$]*[\"`]?\.)?"  # optional schema qualifier
        r"[\"`]?([A-Za-z_][\w$]*)[\"`]?"
        r"[^;]*?ENABLE\s+ROW\s+LEVEL\s+SECURITY",
        re.IGNORECASE | re.DOTALL,
    )
    locked: set[str] = {m.group(1).lower() for m in enable_re.finditer(rls)}

    missing = [t for t in schema_tables if t not in locked]
    if missing:
        fail(
            "RLS: "
            f"{len(missing)} table(s) in schema.sql are NOT locked down in "
            "rls_lockdown.sql (each is auto-exposed via the Supabase Data API): "
            + ", ".join(missing)
        )
    else:
        print(
            f"  [ok] RLS coverage: all {len(schema_tables)} schema tables have "
            "ENABLE ROW LEVEL SECURITY."
        )


# -----------------------------------------------------------------------------
# Check 2 (HARD): SQL echo production guard.
# SQLAlchemy echo logs every statement *with bound parameters* — that can carry
# alumni PII into logs. If the engine's echo is driven by the sql_echo setting,
# require that it is gated by environment != "production" nearby.
# -----------------------------------------------------------------------------
def check_sql_echo_guard() -> None:
    if not DATABASE_PY.exists():
        fail(f"SQL echo: {DATABASE_PY} not found.")
        return

    src = read(DATABASE_PY)
    lines = src.splitlines()

    # Find where sql_echo feeds the echo flag — either a direct
    # `echo=settings.sql_echo` kwarg, or an intermediate `_echo = ... sql_echo`.
    echo_line_idxs = [
        i for i, ln in enumerate(lines) if re.search(r"sql_echo", ln)
    ]
    if not echo_line_idxs:
        # sql_echo is not used to drive engine echo at all — nothing to guard.
        print("  [ok] SQL echo: settings.sql_echo not wired to engine echo.")
        return

    # A production guard must appear within a small window around an sql_echo
    # use (same statement/assignment block), referencing "production".
    window = 4
    guarded = False
    for idx in echo_line_idxs:
        lo = max(0, idx - window)
        hi = min(len(lines), idx + window + 1)
        block = "\n".join(lines[lo:hi])
        if re.search(r"production", block, re.IGNORECASE):
            guarded = True
            break

    if not guarded:
        fail(
            "SQL echo: settings.sql_echo drives engine echo in "
            "app/core/database.py with no production guard nearby "
            '(expected `environment != "production"` gating echo, so PII in '
            "bound parameters is never logged in prod)."
        )
    else:
        print(
            "  [ok] SQL echo: sql_echo is gated by an environment "
            '!= "production" guard.'
        )


# -----------------------------------------------------------------------------
# Check 3 (HARD): No tracked secrets / committed .env.
# Conservative, low-false-positive: flag a real root .env that is NOT covered by
# .gitignore (i.e. would be committable), and obvious hardcoded service-role
# keys / long JWT literals under app/.
# -----------------------------------------------------------------------------
def _gitignore_covers_env() -> bool:
    gi = REPO_ROOT / ".gitignore"
    if not gi.exists():
        return False
    for raw in read(gi).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Treat `.env`, `.env.*`, `*.env`, or a bare `.env` glob as coverage.
        if line in (".env", "/.env", ".env*", ".env.*", "*.env"):
            return True
    return False


def check_no_secrets() -> None:
    # (a) Committable root .env file.
    root_env = REPO_ROOT / ".env"
    if root_env.is_file():
        if _gitignore_covers_env():
            print(
                "  [ok] root .env exists locally but is covered by .gitignore "
                "(untracked, not committed)."
            )
        else:
            fail(
                "Secrets: a real .env exists in the repo root and is NOT "
                "covered by .gitignore — it can be committed. Add `.env` to "
                ".gitignore and use .env.example for templates."
            )

    # (b) Hardcoded secret literals under app/.
    # Service-role / secret env assignments with an actual value, and long JWT
    # literals (three base64url segments). Kept conservative.
    service_role_re = re.compile(
        r"(SUPABASE_SERVICE_ROLE_KEY|SERVICE_ROLE_KEY|SUPABASE_SECRET|"
        r"SUPABASE_JWT_SECRET)\s*[=:]\s*['\"]?[A-Za-z0-9._\-]{12,}",
    )
    jwt_re = re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")

    offenders: list[str] = []
    if APP_DIR.exists():
        for py in APP_DIR.rglob("*.py"):
            text = read(py)
            for line_no, line in enumerate(text.splitlines(), 1):
                # Skip os.environ / getenv reads — those are not hardcoded values.
                if "os.environ" in line or "getenv" in line:
                    continue
                if service_role_re.search(line) or jwt_re.search(line):
                    rel = py.relative_to(REPO_ROOT).as_posix()
                    offenders.append(f"{rel}:{line_no}")

    if offenders:
        fail(
            "Secrets: hardcoded secret/JWT literal(s) found under app/: "
            + ", ".join(offenders[:10])
        )
    else:
        print("  [ok] Secrets: no committable .env and no hardcoded secrets in app/.")


# -----------------------------------------------------------------------------
# Check 4 (WARN): record-of-disclosure logging on alumni-data GET routes.
# Heuristic — if a disclosure route file has GET routes but never references an
# audit log (AuditLog / _audit / audit), warn about possible missing
# record-of-disclosure logging. Does not fail the build.
# -----------------------------------------------------------------------------
def check_disclosure_logging() -> None:
    audit_re = re.compile(r"AuditLog|_audit|\baudit\b", re.IGNORECASE)
    get_re = re.compile(r"@router\.get\b")
    for path in DISCLOSURE_ROUTE_FILES:
        if not path.exists():
            warn(f"Disclosure: expected route file missing: {path.name}")
            continue
        text = read(path)
        if not get_re.search(text):
            continue  # no GET routes — nothing to disclose
        if not audit_re.search(text):
            rel = path.relative_to(REPO_ROOT).as_posix()
            warn(
                f"Disclosure: {rel} has alumni-data GET route(s) but no "
                "AuditLog/audit reference — possible missing record-of-"
                "disclosure logging."
            )


def main() -> int:
    print("FERPA check (fa-web-api) - deterministic static analysis")
    print("-" * 60)
    check_rls_coverage()
    check_sql_echo_guard()
    check_no_secrets()
    check_disclosure_logging()

    print("-" * 60)
    for w in warnings:
        print(f"  [warn] {w}")
    for f in hard_failures:
        print(f"  [FAIL] {f}")

    print("-" * 60)
    print(
        f"FERPA check: {len(hard_failures)} hard failures, "
        f"{len(warnings)} warnings"
    )
    return 1 if hard_failures else 0


if __name__ == "__main__":
    sys.exit(main())
