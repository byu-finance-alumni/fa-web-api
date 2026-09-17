"""Fail loudly if the PROD Supabase org is configured to cost more than $25/month.

WHY THIS EXISTS. The prod org moved to Pro on 2026-09-15 with an agreed bill of
$25/month: the $25 plan fee plus ~$10 of Micro compute, minus the $10 of Compute
Credits every paid org gets. Supabase's Spend Cap (Cost Control, org billing
page) stops usage overages — egress, storage, MAU and so on — from being billed.
It does NOT cover the opt-in add-ons: compute size, read replicas, branching
compute, custom domains, PITR, IPv4, log drains, MFA phone, extra disk
IOPS/throughput. One click on any of those quietly raises the invoice and the
first anyone hears of it is the bill. This script reads the org's configuration
through the Management API once a day and fails if anything is selected that the
Spend Cap would not have stopped.

WHAT IT CANNOT DO. It cannot see or set the Spend Cap — the Management API does
not expose it (checked against https://api.supabase.com/api/v1-json on
2026-09-16) — and it cannot undo a purchase. It DETECTS within a day; a human
reverts. See docs/SUPABASE-SPEND-GUARD.md.

⚠️ STDLIB ONLY. This runs on a bare GitHub runner without the project's
dependencies installed. Do not import from ``app`` or add third-party imports.

⚠️ NEVER PRINT THE TOKEN. Every error message passes through ``redact`` and the
HTTP layer never includes request headers in an exception. Keep it that way.

Field names below are taken from the Management API OpenAPI spec:
  GET /v1/projects
      -> [{ref, organization_slug, organization_id, name, status, ...}]
  GET /v1/organizations/{slug}
      -> {id, name, plan: free|pro|team|enterprise|platform, ...}
  GET /v1/organizations/{slug}/projects?limit=&offset=
      -> {projects: [{ref, name, is_branch, status,
                      databases: [{type: PRIMARY|READ_REPLICA,
                                   infra_compute_size: nano|micro|small|...,
                                   disk_type, disk_throughput_mbps, ...}]}],
          pagination: {count, limit, offset}}
  GET /v1/projects/{ref}/billing/addons
      -> {selected_addons: [{type, variant: {id, name,
                             price: {amount, interval: monthly|hourly, type, description}}}],
          available_addons: [{type, name, variants: [{id, name, price}]}]}
      addon types: compute_instance, custom_domain, pitr, ipv4, auth_mfa_phone,
                   auth_mfa_web_authn, log_drain, etl_pipeline
      compute variant ids: ci_micro, ci_small, ci_medium, ... (there is NO ci_nano:
                   a Nano project has no compute_instance entry at all)
  GET /v1/projects/{ref}/config/disk
      -> {attributes: {type: gp3|io2, iops, size_gb, throughput_mibps?}}

Usage:
  SUPABASE_ACCESS_TOKEN=... python -m scripts.supabase_spend_guard --ref <ref> [--org <slug>]
  python -m scripts.supabase_spend_guard --ref <ref> --dry-run   # no network, no token
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

API_BASE = "https://api.supabase.com"

#: What was agreed with Tanya and Amy: the plan fee, and nothing above it.
MONTHLY_CEILING_USD = 25.00
#: Plan fees Supabase charges up front, per organization. Only Pro is expected;
#: any other plan is a FAIL on its own (check e) and is priced here only so the
#: projection can still print something sensible.
PLAN_FEE_USD = {"free": 0.00, "pro": 25.00, "team": 599.00}
#: Every paid org gets $10/month of Compute Credits, applied to compute only.
COMPUTE_CREDIT_USD = 10.00
#: Supabase prices Micro at $0.01344/hour and quotes it as "$10/month", which
#: is 744 hours (a 31-day month). Use the same conversion so our projection
#: matches their invoice rather than undercutting it with a 30-day month.
HOURS_PER_MONTH = 744
#: Fallback Micro price if available_addons does not list one (it should).
MICRO_HOURLY_USD_FALLBACK = 0.01344

#: The only compute add-on the $10 credit covers in full.
ALLOWED_COMPUTE_VARIANT = "ci_micro"
#: Addon ``type`` values from the spec. Anything in ``selected_addons`` that is
#: not the compute instance is an opt-in the Spend Cap does not cover.
COMPUTE_ADDON_TYPE = "compute_instance"
#: Human names for the FAIL line. Unknown types are still reported (by their raw
#: type) — a new add-on Supabase invents must not slip through as "unknown".
ADDON_LABELS = {
    "custom_domain": "custom domain",
    "pitr": "point-in-time recovery",
    "ipv4": "dedicated IPv4",
    "auth_mfa_phone": "MFA phone",
    "auth_mfa_web_authn": "MFA WebAuthn",
    "log_drain": "log drains",
    "etl_pipeline": "ETL pipeline",
}
#: gp3 defaults that come with the plan. Anything above these, or io2, is a
#: paid disk upgrade the Spend Cap does not cover.
DISK_DEFAULT_TYPE = "gp3"
DISK_DEFAULT_IOPS = 3000
DISK_DEFAULT_THROUGHPUT_MIBPS = 125

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

_TOKEN_RE = re.compile(r"(?i)(bearer\s+|sbp_)[A-Za-z0-9._\-]+")
_QUERY_RE = re.compile(r"\?[^\s\"'<>]*")
_HEADER_RE = re.compile(r"(?im)^(authorization|cookie|set-cookie|x-api-key)\s*:.*$")


class GuardError(Exception):
    """A request or a response the guard could not use. Message is redacted."""


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str
    lines: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.status == FAIL


Fetch = Callable[[str], object]


# ----------------------------------------------------------------------------
# Redaction
# ----------------------------------------------------------------------------
def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Strip anything that could carry the token from a message.

    Removes known secret values, bearer / ``sbp_`` tokens, whole URL query
    strings (a token could be passed as one) and any header line that carries
    credentials. Applied to every error before it is raised or printed.
    """
    out = text
    for s in secrets:
        if s:
            out = out.replace(s, "***")
    out = _TOKEN_RE.sub(r"\1***", out)
    out = _HEADER_RE.sub(lambda m: f"{m.group(1)}: ***", out)
    out = _QUERY_RE.sub("?<redacted>", out)
    return out


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
class HttpFetcher:
    """GET a Management API path and return the parsed JSON body.

    Only the PATH is ever included in an error. Headers are never included, the
    token is scrubbed from any body text Supabase echoes back, and query strings
    are removed from URLs.
    """

    def __init__(self, token: str, api_base: str = API_BASE, timeout: float = 30.0):
        if not token:
            raise GuardError("SUPABASE_ACCESS_TOKEN is empty.")
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout

    def __call__(self, path: str) -> object:
        url = self._api_base + path
        shown = redact(path, [self._token])
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "User-Agent": "fa-web-api supabase-spend-guard",
            },
            method="GET",
        )
        try:
            # url is API_BASE (a literal https://api.supabase.com) plus a
            # path this module composes; nothing external picks the scheme.
            # nosemgrep -- dynamic-urllib-use-detected, see above
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            snippet = ""
            try:
                snippet = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise GuardError(
                redact(f"GET {shown} -> HTTP {e.code}: {snippet}".strip(), [self._token])
            ) from None
        except urllib.error.URLError as e:
            raise GuardError(redact(f"GET {shown} -> {e.reason}", [self._token])) from None
        except OSError as e:
            raise GuardError(redact(f"GET {shown} -> {e}", [self._token])) from None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise GuardError(f"GET {shown} -> response was not JSON") from None


def _org_projects_path(slug: str, offset: int = 0, limit: int = 100) -> str:
    q = urllib.parse.urlencode({"limit": limit, "offset": offset})
    return f"/v1/organizations/{urllib.parse.quote(slug, safe='')}/projects?{q}"


def planned_requests(ref: str, org: str | None) -> list[str]:
    """The GETs a real run makes, in order, for --dry-run and the docs.

    Without --org the slug is read from the project's own record, so the
    placeholder ``{slug}`` stands in for it here.
    """
    slug = org or "{slug}"
    return [
        "/v1/projects",
        f"/v1/organizations/{slug}",
        f"/v1/organizations/{slug}/projects?limit=100&offset=0",
        f"/v1/projects/{ref}/billing/addons",
        f"/v1/projects/{ref}/config/disk",
    ]


# ----------------------------------------------------------------------------
# Data collection
# ----------------------------------------------------------------------------
@dataclass
class Snapshot:
    ref: str
    org_slug: str
    org_input: str | None
    projects: list[dict]  # /v1/projects, every project the token can see
    organization: dict  # /v1/organizations/{slug}
    org_projects: list[dict]  # /v1/organizations/{slug}/projects (paged)
    addons: dict  # /v1/projects/{ref}/billing/addons
    disk: dict  # /v1/projects/{ref}/config/disk


def _as_list(value: object, what: str) -> list:
    if not isinstance(value, list):
        raise GuardError(f"{what}: expected a JSON array, got {type(value).__name__}")
    return value


def _as_dict(value: object, what: str) -> dict:
    if not isinstance(value, dict):
        raise GuardError(f"{what}: expected a JSON object, got {type(value).__name__}")
    return value


def collect(fetch: Fetch, ref: str, org: str | None) -> Snapshot:
    projects = _as_list(fetch("/v1/projects"), "/v1/projects")
    prod = next((p for p in projects if isinstance(p, dict) and p.get("ref") == ref), None)
    if prod is None:
        raise GuardError(
            f"project {ref} is not visible to this token (it saw {len(projects)} project(s)). "
            "Wrong ref, or the token belongs to a different Supabase account."
        )
    slug = str(prod.get("organization_slug") or "")
    org_id = str(prod.get("organization_id") or "")
    if not slug:
        raise GuardError(f"/v1/projects: project {ref} has no organization_slug")
    if org and org not in (slug, org_id):
        raise GuardError(
            f"project {ref} belongs to organization '{slug}' (id {org_id or '?'}), "
            f"not the configured '{org}'. Fix SUPABASE_PROD_ORG or the ref."
        )

    organization = _as_dict(
        fetch(f"/v1/organizations/{urllib.parse.quote(slug, safe='')}"),
        f"/v1/organizations/{slug}",
    )

    org_projects: list[dict] = []
    offset, limit = 0, 100
    while True:
        page = _as_dict(fetch(_org_projects_path(slug, offset, limit)), "org projects")
        items = _as_list(page.get("projects", []), "org projects.projects")
        org_projects.extend(i for i in items if isinstance(i, dict))
        pagination = page.get("pagination") or {}
        count = int(pagination.get("count", 0) or 0)
        offset += limit
        # Keep paging while EITHER signal says there may be more: a full page
        # (the pattern walk_bucket / walk_listing use elsewhere) OR a
        # `pagination.count` we have not yet reached. Stopping on `count` alone
        # let a stale count end the walk before a page holding an extra
        # billable project — a false PASS from the one check whose job is
        # "find every project". Stop on an empty page or after a sane cap.
        if not items or offset > 10_000:
            break
        if len(items) < limit and len(org_projects) >= count:
            break

    addons = _as_dict(fetch(f"/v1/projects/{ref}/billing/addons"), "billing/addons")
    disk = _as_dict(fetch(f"/v1/projects/{ref}/config/disk"), "config/disk")
    return Snapshot(
        ref=ref,
        org_slug=slug,
        org_input=org,
        projects=[p for p in projects if isinstance(p, dict)],
        organization=organization,
        org_projects=org_projects,
        addons=addons,
        disk=disk,
    )


# ----------------------------------------------------------------------------
# Pricing helpers
# ----------------------------------------------------------------------------
def _amount(price: dict) -> float:
    """``price.amount`` as a float, or a GuardError — never a raw ValueError.

    An unparseable amount must land on the "FAIL guard / NOTHING was verified"
    path, not a traceback the workflow's one-line summary would garble."""
    raw = price.get("amount")
    if raw is None or raw == "":
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise GuardError(f"billing price amount is not a number: {raw!r}") from None


def monthly_usd(price: dict | None) -> float:
    """Normalise a spec ``price`` object to dollars per month."""
    if not price:
        return 0.0
    amount = _amount(price)
    interval = str(price.get("interval") or "monthly").lower()
    if interval == "hourly":
        return amount * HOURS_PER_MONTH
    return amount


def _selected(snapshot: Snapshot) -> list[dict]:
    return [a for a in snapshot.addons.get("selected_addons", []) if isinstance(a, dict)]


def _selected_compute(snapshot: Snapshot) -> dict | None:
    return next((a for a in _selected(snapshot) if a.get("type") == COMPUTE_ADDON_TYPE), None)


def _available_variant(snapshot: Snapshot, addon_type: str, variant_id: str) -> dict | None:
    for addon in snapshot.addons.get("available_addons", []):
        if not isinstance(addon, dict) or addon.get("type") != addon_type:
            continue
        for v in addon.get("variants", []):
            if isinstance(v, dict) and v.get("id") == variant_id:
                return v
    return None


def _prod_org_entry(snapshot: Snapshot) -> dict | None:
    return next((p for p in snapshot.org_projects if p.get("ref") == snapshot.ref), None)


def _primary_db(snapshot: Snapshot) -> dict | None:
    entry = _prod_org_entry(snapshot)
    if not entry:
        return None
    dbs = [d for d in entry.get("databases", []) if isinstance(d, dict)]
    return next((d for d in dbs if d.get("type") == "PRIMARY"), dbs[0] if dbs else None)


def _fmt(usd: float) -> str:
    sign = "-" if usd < 0 else ""
    return f"{sign}${abs(usd):,.2f}"


# ----------------------------------------------------------------------------
# Checks — each returns one named PASS / WARN / FAIL line
# ----------------------------------------------------------------------------
def check_plan(snapshot: Snapshot) -> CheckResult:
    """(e) The org is on Pro — not Free (no backups, pausing) and not Team ($599)."""
    plan = str(snapshot.organization.get("plan") or "").lower()
    name = snapshot.organization.get("name") or snapshot.org_slug
    if plan == "pro":
        return CheckResult("plan", PASS, f"organization '{name}' is on plan 'pro'")
    if not plan:
        return CheckResult("plan", FAIL, f"organization '{name}': the API returned no 'plan' field")
    return CheckResult("plan", FAIL, f"organization '{name}' is on plan '{plan}', expected 'pro'")


def check_org_membership(snapshot: Snapshot) -> CheckResult:
    """(d) The prod org holds exactly the prod project. Every extra project or
    branch is another ~$10/month of compute the credit does not cover."""
    slug = snapshot.org_slug
    from_list = {
        p.get("ref")
        for p in snapshot.projects
        if p.get("organization_slug") == slug or p.get("organization_id") == slug
    }
    from_org = {p.get("ref") for p in snapshot.org_projects}
    seen = {r for r in from_list | from_org if r}
    extras = sorted(seen - {snapshot.ref})
    if snapshot.ref not in seen:
        return CheckResult(
            "org-membership", FAIL, f"prod project {snapshot.ref} is not listed under org '{slug}'"
        )
    if extras:
        names = []
        for ref in extras:
            entry = next((p for p in snapshot.org_projects if p.get("ref") == ref), None) or next(
                (p for p in snapshot.projects if p.get("ref") == ref), {}
            )
            label = entry.get("name") or "?"
            kind = "branch" if entry.get("is_branch") else "project"
            names.append(f"{ref} ({label}, {kind}, {entry.get('status', '?')})")
        return CheckResult(
            "org-membership",
            FAIL,
            f"org '{slug}' contains {len(extras)} other project(s) besides {snapshot.ref}: "
            + "; ".join(names)
            + ". Each one is ~$10/month of compute on top of the plan.",
        )
    return CheckResult(
        "org-membership", PASS, f"org '{slug}' contains only the prod project {snapshot.ref}"
    )


def check_compute(snapshot: Snapshot) -> CheckResult:
    """(a) Compute is Micro. Nano is tolerated with a warning: on a paid plan a
    Nano instance is billed at the Micro price and is never auto-upgraded."""
    selected = _selected_compute(snapshot)
    variant = (selected or {}).get("variant") or {}
    variant_id = str(variant.get("id") or "")
    db = _primary_db(snapshot) or {}
    infra = str(db.get("infra_compute_size") or "").lower()

    if variant_id == ALLOWED_COMPUTE_VARIANT or (not variant_id and infra == "micro"):
        return CheckResult(
            "compute-variant",
            PASS,
            f"compute is Micro (addon variant '{variant_id or '-'}', "
            f"instance size '{infra or '-'}', {_fmt(monthly_usd(variant.get('price')))}/month)",
        )
    if variant_id:
        return CheckResult(
            "compute-variant",
            FAIL,
            f"compute add-on is '{variant.get('name') or variant_id}' ({variant_id}) at "
            f"{_fmt(monthly_usd(variant.get('price')))}/month; only {ALLOWED_COMPUTE_VARIANT} "
            "is covered by the $10 credit. Downgrade it in Project Settings -> Compute and Disk.",
        )
    if infra in ("", "nano", "pico"):
        return CheckResult(
            "compute-variant",
            WARN,
            f"no compute add-on is selected and the instance size is '{infra or 'unknown'}'. "
            "On a paid plan Nano is billed AS MICRO (~$10/month) and is never auto-upgraded; "
            "bump it to Micro when convenient (Project Settings -> Compute and Disk; it "
            "restarts the database). This WARN becomes PASS once that is done.",
        )
    return CheckResult(
        "compute-variant",
        FAIL,
        f"instance size is '{infra}' but no compute add-on is selected; expected micro. "
        "Read Project Settings -> Compute and Disk before trusting the bill.",
    )


def check_no_other_addons(snapshot: Snapshot) -> CheckResult:
    """(b) Nothing the Spend Cap does not cover is switched on: no add-ons other
    than compute, no read replicas, no branches, no paid disk tier."""
    problems: list[str] = []

    for addon in _selected(snapshot):
        kind = str(addon.get("type") or "?")
        if kind == COMPUTE_ADDON_TYPE:
            continue
        variant = addon.get("variant") or {}
        label = ADDON_LABELS.get(kind, kind)
        problems.append(
            f"add-on '{label}' ({kind}/{variant.get('id', '?')}) "
            f"at {_fmt(monthly_usd(variant.get('price')))}/month"
        )

    entry = _prod_org_entry(snapshot) or {}
    replicas = [
        d
        for d in entry.get("databases", [])
        if isinstance(d, dict) and d.get("type") == "READ_REPLICA"
    ]
    if replicas:
        where = ", ".join(str(d.get("region") or d.get("identifier") or "?") for d in replicas)
        problems.append(f"{len(replicas)} read replica(s) ({where}) - each is its own compute bill")

    branches = [p for p in snapshot.org_projects if p.get("is_branch")]
    if branches:
        names = ", ".join(str(b.get("name") or b.get("ref")) for b in branches)
        problems.append(
            f"{len(branches)} preview branch(es) ({names}) - branching compute is billed"
        )

    attrs = snapshot.disk.get("attributes") or {}
    disk_type = str(attrs.get("type") or DISK_DEFAULT_TYPE).lower()
    iops = int(attrs.get("iops") or 0)
    throughput = int(attrs.get("throughput_mibps") or 0)
    if disk_type != DISK_DEFAULT_TYPE:
        problems.append(f"disk type is '{disk_type}' (provisioned-IOPS tier), expected gp3")
    if iops > DISK_DEFAULT_IOPS:
        problems.append(f"disk IOPS {iops} exceeds the included {DISK_DEFAULT_IOPS}")
    if throughput > DISK_DEFAULT_THROUGHPUT_MIBPS:
        problems.append(
            f"disk throughput {throughput} MiB/s exceeds the included "
            f"{DISK_DEFAULT_THROUGHPUT_MIBPS}"
        )

    if problems:
        return CheckResult(
            "no-other-addons",
            FAIL,
            "the Spend Cap does NOT cover these and they are switched on: " + "; ".join(problems),
        )
    return CheckResult(
        "no-other-addons",
        PASS,
        "no add-ons besides compute, no read replicas, no branches, "
        f"disk is {disk_type} at default IOPS/throughput",
    )


def projected_monthly(snapshot: Snapshot) -> tuple[float, list[str]]:
    """Plan fee + every selected add-on, minus the compute credit, with the
    arithmetic as printable lines. Nano is priced as Micro, as Supabase bills it."""
    plan = str(snapshot.organization.get("plan") or "").lower()
    fee = PLAN_FEE_USD.get(plan)
    lines: list[str] = []
    if fee is None:
        fee = PLAN_FEE_USD["pro"]
        lines.append(_row(f"plan fee ('{plan or '?'}' unknown, priced as pro)", fee))
    else:
        lines.append(_row(f"plan fee ({plan})", fee))

    compute_total = 0.0
    other_total = 0.0
    selected = _selected(snapshot)
    if not any(a.get("type") == COMPUTE_ADDON_TYPE for a in selected):
        micro = _available_variant(snapshot, COMPUTE_ADDON_TYPE, ALLOWED_COMPUTE_VARIANT)
        price = (micro or {}).get("price") or {
            "amount": MICRO_HOURLY_USD_FALLBACK,
            "interval": "hourly",
        }
        implied = monthly_usd(price)
        compute_total += implied
        lines.append(_row(f"compute: none selected -> Nano billed as Micro{_rate(price)}", implied))
    for addon in selected:
        variant = addon.get("variant") or {}
        price = variant.get("price") or {}
        cost = monthly_usd(price)
        label = f"{addon.get('type', '?')}/{variant.get('id', '?')}"
        lines.append(_row(f"add-on {label}{_rate(price)}", cost))
        if addon.get("type") == COMPUTE_ADDON_TYPE:
            compute_total += cost
        else:
            other_total += cost

    credit = min(compute_total, COMPUTE_CREDIT_USD)
    lines.append(_row(f"compute credits (min(compute, {_fmt(COMPUTE_CREDIT_USD)}))", -credit))
    total = fee + compute_total + other_total - credit
    lines.append(_row("= projected monthly", total))
    lines.append(_row("ceiling", MONTHLY_CEILING_USD))
    return round(total, 2), lines


def _row(label: str, usd: float) -> str:
    return f"  {label:<58}{_fmt(usd):>11}"


def _rate(price: dict) -> str:
    """' (0.01344/h x 744h)' for hourly prices, '' for monthly ones."""
    if str(price.get("interval", "")).lower() != "hourly":
        return ""
    return f" ({_amount(price):.5f}/h x {HOURS_PER_MONTH}h)"


def check_projection(snapshot: Snapshot) -> CheckResult:
    """(c) plan fee + selected add-ons - credits <= $25.00, arithmetic shown."""
    total, lines = projected_monthly(snapshot)
    if total <= MONTHLY_CEILING_USD + 0.005:
        return CheckResult(
            "projected-monthly",
            PASS,
            f"{_fmt(total)} <= {_fmt(MONTHLY_CEILING_USD)} (usage overages excluded - "
            "those are the Spend Cap's job)",
            lines,
        )
    return CheckResult(
        "projected-monthly",
        FAIL,
        f"{_fmt(total)} exceeds the {_fmt(MONTHLY_CEILING_USD)} ceiling by "
        f"{_fmt(total - MONTHLY_CEILING_USD)}/month",
        lines,
    )


CHECKS: tuple[Callable[[Snapshot], CheckResult], ...] = (
    check_plan,
    check_org_membership,
    check_compute,
    check_no_other_addons,
    check_projection,
)


def run_checks(snapshot: Snapshot) -> list[CheckResult]:
    return [check(snapshot) for check in CHECKS]


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def _print_results(results: list[CheckResult], out) -> None:
    for r in results:
        print(f"{r.status:<4} {r.name}: {r.detail}", file=out)
        for line in r.lines:
            print(line, file=out)


def _summary(results: list[CheckResult]) -> str:
    fails = [r.name for r in results if r.status == FAIL]
    warns = [r.name for r in results if r.status == WARN]
    if fails:
        return "FAIL: " + ", ".join(fails) + (f" (warnings: {', '.join(warns)})" if warns else "")
    if warns:
        return "PASS with warnings: " + ", ".join(warns)
    return "PASS: prod Supabase org is configured for the $25/month plan fee and nothing more"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="supabase_spend_guard",
        description="Fail if the prod Supabase org is configured to bill more than the plan fee.",
    )
    p.add_argument(
        "--ref",
        default=os.environ.get("SUPABASE_PROD_REF"),
        help="prod project ref (env SUPABASE_PROD_REF)",
    )
    p.add_argument(
        "--org",
        default=os.environ.get("SUPABASE_PROD_ORG") or None,
        help="prod organization slug or id (env SUPABASE_PROD_ORG). Optional: derived "
        "from the project when omitted; asserted against it when given.",
    )
    p.add_argument("--api-base", default=API_BASE, help=argparse.SUPPRESS)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the requests that would be made and exit 0 without touching the network",
    )
    return p


def main(argv: list[str] | None = None, out=None) -> int:
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    ref = (args.ref or "").strip()
    org = (args.org or "").strip() or None
    if not ref:
        print("error: --ref or SUPABASE_PROD_REF is required", file=out)
        return 2
    if not re.fullmatch(r"[a-z]{20}", ref):
        print(f"error: '{ref}' does not look like a project ref (20 lowercase letters)", file=out)
        return 2

    if args.dry_run:
        print(f"DRY RUN - no requests sent. Would GET from {args.api_base}:", file=out)
        for path in planned_requests(ref, org):
            print(f"  GET {path}", file=out)
        print(
            "Checks: " + ", ".join(c.__name__.removeprefix("check_") for c in CHECKS),
            file=out,
        )
        return 0

    token = os.environ.get("SUPABASE_ACCESS_TOKEN", "")
    if not token:
        print("error: SUPABASE_ACCESS_TOKEN is not set", file=out)
        return 2

    try:
        snapshot = collect(HttpFetcher(token, args.api_base), ref, org)
        results = run_checks(snapshot)
    except GuardError as e:
        print(f"FAIL guard: {redact(str(e), [token])}", file=out)
        print("FAIL: the guard could not complete, so NOTHING was verified.", file=out)
        return 1

    _print_results(results, out)
    print(_summary(results), file=out)
    return 1 if any(r.failed for r in results) else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
