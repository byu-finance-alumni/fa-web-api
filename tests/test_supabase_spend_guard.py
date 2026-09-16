"""Tests for scripts/supabase_spend_guard.py — the daily $25 ceiling check.

The HTTP layer is a plain callable ``fetch(path) -> json``, so every check runs
against canned Management API bodies shaped like the OpenAPI spec (field names
in the script's module docstring). Nothing here touches the network: the one
test that exercises ``HttpFetcher`` monkeypatches ``urllib.request.urlopen``.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from copy import deepcopy

import pytest

from scripts import supabase_spend_guard as guard

REF = "njobhhdopwdodvzosrns"
ORG = "finance-alumni-prod"
ORG_ID = "org-id-12345"
# Not shaped like a real PAT on purpose: GitHub push protection rejects anything
# matching the sbp_ + 40 hex pattern, fake or not. The redaction tests below use
# a short sbp_ fragment instead.
TOKEN = "sbp_fake-token-for-tests"

MICRO_VARIANT = {
    "id": "ci_micro",
    "name": "Micro",
    "price": {"description": "Micro", "type": "fixed", "interval": "hourly", "amount": 0.01344},
}
SMALL_VARIANT = {
    "id": "ci_small",
    "name": "Small",
    "price": {"description": "Small", "type": "fixed", "interval": "hourly", "amount": 0.0206},
}
PITR_VARIANT = {
    "id": "pitr_7",
    "name": "7 days",
    "price": {"description": "PITR 7", "type": "fixed", "interval": "monthly", "amount": 100},
}
CUSTOM_DOMAIN_VARIANT = {
    "id": "cd_default",
    "name": "Custom domain",
    "price": {"description": "cd", "type": "fixed", "interval": "monthly", "amount": 10},
}
IPV4_VARIANT = {
    "id": "ipv4_default",
    "name": "IPv4",
    "price": {"description": "ipv4", "type": "fixed", "interval": "hourly", "amount": 0.0055},
}

AVAILABLE_ADDONS = [
    {"type": "compute_instance", "name": "Compute", "variants": [MICRO_VARIANT, SMALL_VARIANT]},
    {"type": "pitr", "name": "PITR", "variants": [PITR_VARIANT]},
    {"type": "custom_domain", "name": "Custom domain", "variants": [CUSTOM_DOMAIN_VARIANT]},
    {"type": "ipv4", "name": "IPv4", "variants": [IPV4_VARIANT]},
]


def primary_db(size: str = "micro") -> dict:
    return {
        "type": "PRIMARY",
        "identifier": REF,
        "region": "us-west-1",
        "status": "ACTIVE_HEALTHY",
        "cloud_provider": "AWS",
        "infra_compute_size": size,
        "disk_volume_size_gb": 8,
        "disk_type": "gp3",
        "disk_throughput_mbps": 125,
    }


def org_project(ref: str = REF, name: str = "fa-prod", size: str = "micro", **extra) -> dict:
    entry = {
        "ref": ref,
        "name": name,
        "cloud_provider": "AWS",
        "region": "us-west-1",
        "is_branch": False,
        "status": "ACTIVE_HEALTHY",
        "inserted_at": "2026-07-09T00:00:00Z",
        "databases": [primary_db(size)],
    }
    entry.update(extra)
    return entry


def healthy_bodies(compute_size: str = "micro", selected_compute: bool = True) -> dict[str, object]:
    """Exactly what the prod org should look like: Pro, one project, Micro."""
    selected = [{"type": "compute_instance", "variant": MICRO_VARIANT}] if selected_compute else []
    return {
        "/v1/projects": [
            {
                "id": REF,
                "ref": REF,
                "organization_id": ORG_ID,
                "organization_slug": ORG,
                "name": "fa-prod",
                "region": "us-west-1",
                "created_at": "2026-07-09T00:00:00Z",
                "status": "ACTIVE_HEALTHY",
                "database": {
                    "host": "db.example",
                    "version": "17",
                    "postgres_engine": "17",
                    "release_channel": "ga",
                },
            },
            # The dev project lives in a DIFFERENT (Free) org and must be ignored.
            {
                "id": "tnnhhnzglyfqolxdojyb",
                "ref": "tnnhhnzglyfqolxdojyb",
                "organization_id": "org-id-dev",
                "organization_slug": "finance-alumni-dev",
                "name": "fa-dev",
                "region": "us-west-1",
                "created_at": "2026-01-01T00:00:00Z",
                "status": "ACTIVE_HEALTHY",
                "database": {
                    "host": "db.example",
                    "version": "17",
                    "postgres_engine": "17",
                    "release_channel": "ga",
                },
            },
        ],
        f"/v1/organizations/{ORG}": {
            "id": ORG_ID,
            "name": "Finance Alumni (prod)",
            "plan": "pro",
            "opt_in_tags": [],
            "allowed_release_channels": ["ga"],
        },
        f"/v1/organizations/{ORG}/projects?limit=100&offset=0": {
            "projects": [org_project(size=compute_size)],
            "pagination": {"count": 1, "limit": 100, "offset": 0},
        },
        f"/v1/projects/{REF}/billing/addons": {
            "selected_addons": selected,
            "available_addons": AVAILABLE_ADDONS,
        },
        f"/v1/projects/{REF}/config/disk": {
            "attributes": {"type": "gp3", "iops": 3000, "throughput_mibps": 125, "size_gb": 8},
            "last_modified_at": "2026-09-15T00:00:00Z",
        },
    }


class FakeFetch:
    """Dict-backed stand-in for HttpFetcher that records every path asked for."""

    def __init__(self, bodies: dict[str, object]):
        self.bodies = bodies
        self.calls: list[str] = []

    def __call__(self, path: str) -> object:
        self.calls.append(path)
        if path not in self.bodies:
            raise guard.GuardError(f"GET {path} -> HTTP 404: unexpected path in test")
        return deepcopy(self.bodies[path])


def results_by_name(snapshot: guard.Snapshot) -> dict[str, guard.CheckResult]:
    return {r.name: r for r in guard.run_checks(snapshot)}


def snapshot_for(bodies: dict[str, object], org: str | None = ORG) -> guard.Snapshot:
    return guard.collect(FakeFetch(bodies), REF, org)


# ----------------------------------------------------------------------------
# The happy path, and the request plan
# ----------------------------------------------------------------------------
def test_healthy_org_passes_every_check():
    fetch = FakeFetch(healthy_bodies())
    snapshot = guard.collect(fetch, REF, ORG)
    results = guard.run_checks(snapshot)
    assert [r.name for r in results] == [
        "plan",
        "org-membership",
        "compute-variant",
        "no-other-addons",
        "projected-monthly",
    ]
    assert all(r.status == guard.PASS for r in results), results
    # Exactly the requests the docs promise, in order, one page of org projects.
    assert fetch.calls == guard.planned_requests(REF, ORG)


def test_org_is_derived_from_the_project_when_not_given():
    snapshot = snapshot_for(healthy_bodies(), org=None)
    assert snapshot.org_slug == ORG
    assert results_by_name(snapshot)["org-membership"].status == guard.PASS


def test_org_id_is_accepted_in_place_of_slug():
    snapshot = snapshot_for(healthy_bodies(), org=ORG_ID)
    assert snapshot.org_slug == ORG


def test_mismatched_org_is_an_error_not_a_silent_pass():
    with pytest.raises(guard.GuardError, match="belongs to organization"):
        snapshot_for(healthy_bodies(), org="some-other-org")


def test_unknown_ref_is_an_error():
    with pytest.raises(guard.GuardError, match="not visible to this token"):
        guard.collect(FakeFetch(healthy_bodies()), "aaaaaaaaaaaaaaaaaaaa", ORG)


def test_org_projects_pagination_is_followed():
    bodies = healthy_bodies()
    first = f"/v1/organizations/{ORG}/projects?limit=100&offset=0"
    bodies[first] = {
        "projects": [org_project()],
        "pagination": {"count": 2, "limit": 100, "offset": 0},
    }
    bodies[f"/v1/organizations/{ORG}/projects?limit=100&offset=100"] = {
        "projects": [org_project(ref="bbbbbbbbbbbbbbbbbbbb", name="staging")],
        "pagination": {"count": 2, "limit": 100, "offset": 100},
    }
    snapshot = snapshot_for(bodies)
    assert len(snapshot.org_projects) == 2
    membership = results_by_name(snapshot)["org-membership"]
    assert membership.status == guard.FAIL
    assert "bbbbbbbbbbbbbbbbbbbb" in membership.detail


def test_a_full_page_is_followed_even_when_count_says_we_are_done():
    """A stale `pagination.count` must not end the walk while pages are full."""
    bodies = healthy_bodies()
    page1 = [org_project()] + [
        org_project(ref=f"{i:020d}"[-20:], name=f"filler-{i}") for i in range(99)
    ]
    bodies[f"/v1/organizations/{ORG}/projects?limit=100&offset=0"] = {
        "projects": page1,
        "pagination": {"count": 1, "limit": 100, "offset": 0},
    }
    bodies[f"/v1/organizations/{ORG}/projects?limit=100&offset=100"] = {
        "projects": [org_project(ref="bbbbbbbbbbbbbbbbbbbb", name="surprise")],
        "pagination": {"count": 1, "limit": 100, "offset": 100},
    }
    snapshot = snapshot_for(bodies)
    assert len(snapshot.org_projects) == 101
    assert results_by_name(snapshot)["org-membership"].status == guard.FAIL


def test_non_numeric_price_amount_is_a_guard_failure_not_a_traceback():
    with pytest.raises(guard.GuardError) as exc:
        guard.monthly_usd({"amount": "not-a-number", "interval": "hourly"})
    assert "not a number" in str(exc.value)
    assert guard.monthly_usd({"amount": "", "interval": "hourly"}) == 0.0
    assert guard.monthly_usd({"amount": "0.01344", "interval": "hourly"}) == pytest.approx(
        0.01344 * guard.HOURS_PER_MONTH
    )


# ----------------------------------------------------------------------------
# (e) plan
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("plan", ["free", "team", "enterprise", "platform"])
def test_plan_other_than_pro_fails(plan):
    bodies = healthy_bodies()
    bodies[f"/v1/organizations/{ORG}"]["plan"] = plan
    r = results_by_name(snapshot_for(bodies))["plan"]
    assert r.status == guard.FAIL
    assert plan in r.detail and "expected 'pro'" in r.detail


def test_missing_plan_field_fails_rather_than_passing_vacuously():
    bodies = healthy_bodies()
    del bodies[f"/v1/organizations/{ORG}"]["plan"]
    r = results_by_name(snapshot_for(bodies))["plan"]
    assert r.status == guard.FAIL


# ----------------------------------------------------------------------------
# (d) org contains only the prod project
# ----------------------------------------------------------------------------
def test_extra_project_in_prod_org_fails_and_is_named():
    bodies = healthy_bodies()
    extra = deepcopy(bodies["/v1/projects"][0])
    extra.update(ref="cccccccccccccccccccc", id="cccccccccccccccccccc", name="oops-staging")
    bodies["/v1/projects"].append(extra)
    r = results_by_name(snapshot_for(bodies))["org-membership"]
    assert r.status == guard.FAIL
    assert "cccccccccccccccccccc" in r.detail and "oops-staging" in r.detail
    assert "~$10/month" in r.detail


def test_dev_project_in_another_org_does_not_count():
    # healthy_bodies already lists dev under a different org.
    r = results_by_name(snapshot_for(healthy_bodies()))["org-membership"]
    assert r.status == guard.PASS
    assert "tnnhhnzglyfqolxdojyb" not in r.detail


def test_branch_listed_under_the_org_fails_membership_and_addons():
    bodies = healthy_bodies()
    page = bodies[f"/v1/organizations/{ORG}/projects?limit=100&offset=0"]
    page["projects"].append(
        org_project(ref="dddddddddddddddddddd", name="feature-x", is_branch=True)
    )
    page["pagination"]["count"] = 2
    results = results_by_name(snapshot_for(bodies))
    assert results["org-membership"].status == guard.FAIL
    assert "branch" in results["org-membership"].detail
    assert results["no-other-addons"].status == guard.FAIL
    assert "preview branch" in results["no-other-addons"].detail


# ----------------------------------------------------------------------------
# (a) compute variant
# ----------------------------------------------------------------------------
def test_micro_passes():
    r = results_by_name(snapshot_for(healthy_bodies()))["compute-variant"]
    assert r.status == guard.PASS
    assert "Micro" in r.detail


def test_nano_warns_with_billed_as_micro_note_but_does_not_fail():
    snapshot = snapshot_for(healthy_bodies(compute_size="nano", selected_compute=False))
    results = results_by_name(snapshot)
    r = results["compute-variant"]
    assert r.status == guard.WARN
    assert "billed AS MICRO" in r.detail
    assert "when convenient" in r.detail
    assert not any(x.failed for x in results.values())


def test_nano_to_micro_bump_flips_warn_to_pass():
    before = results_by_name(snapshot_for(healthy_bodies("nano", selected_compute=False)))
    after = results_by_name(snapshot_for(healthy_bodies("micro", selected_compute=True)))
    assert before["compute-variant"].status == guard.WARN
    assert after["compute-variant"].status == guard.PASS


def test_small_compute_fails_with_price():
    bodies = healthy_bodies(compute_size="small")
    bodies[f"/v1/projects/{REF}/billing/addons"]["selected_addons"] = [
        {"type": "compute_instance", "variant": SMALL_VARIANT}
    ]
    results = results_by_name(snapshot_for(bodies))
    r = results["compute-variant"]
    assert r.status == guard.FAIL
    assert "ci_small" in r.detail and "$15.33/month" in r.detail  # 0.0206 * 744
    # and the projection agrees it is over the ceiling
    assert results["projected-monthly"].status == guard.FAIL


def test_large_instance_size_without_addon_entry_still_fails():
    """Belt and braces: if the addons list is empty but the org endpoint says
    the primary database is big, do not let that read as Nano."""
    snapshot = snapshot_for(healthy_bodies(compute_size="large", selected_compute=False))
    r = results_by_name(snapshot)["compute-variant"]
    assert r.status == guard.FAIL
    assert "large" in r.detail


# ----------------------------------------------------------------------------
# (b) nothing the Spend Cap does not cover
# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("addon_type", "variant", "label"),
    [
        ("pitr", PITR_VARIANT, "point-in-time recovery"),
        ("custom_domain", CUSTOM_DOMAIN_VARIANT, "custom domain"),
        ("ipv4", IPV4_VARIANT, "dedicated IPv4"),
        (
            "log_drain",
            {"id": "log_drain_default", "name": "Log drain", "price": PITR_VARIANT["price"]},
            "log drains",
        ),
        (
            "auth_mfa_phone",
            {"id": "auth_mfa_phone_default", "name": "MFA", "price": PITR_VARIANT["price"]},
            "MFA phone",
        ),
        (
            "brand_new_thing",
            {"id": "x_default", "name": "X", "price": PITR_VARIANT["price"]},
            "brand_new_thing",
        ),
    ],
)
def test_any_selected_non_compute_addon_fails(addon_type, variant, label):
    bodies = healthy_bodies()
    bodies[f"/v1/projects/{REF}/billing/addons"]["selected_addons"].append(
        {"type": addon_type, "variant": variant}
    )
    r = results_by_name(snapshot_for(bodies))["no-other-addons"]
    assert r.status == guard.FAIL
    assert label in r.detail and variant["id"] in r.detail
    assert "Spend Cap does NOT cover" in r.detail


def test_read_replica_fails():
    bodies = healthy_bodies()
    page = bodies[f"/v1/organizations/{ORG}/projects?limit=100&offset=0"]
    replica = primary_db()
    replica.update(type="READ_REPLICA", identifier=f"{REF}-eu", region="eu-west-1")
    page["projects"][0]["databases"].append(replica)
    r = results_by_name(snapshot_for(bodies))["no-other-addons"]
    assert r.status == guard.FAIL
    assert "read replica" in r.detail and "eu-west-1" in r.detail


@pytest.mark.parametrize(
    ("attrs", "needle"),
    [
        ({"type": "io2", "iops": 3000, "size_gb": 8}, "io2"),
        ({"type": "gp3", "iops": 6000, "size_gb": 8, "throughput_mibps": 125}, "IOPS 6000"),
        ({"type": "gp3", "iops": 3000, "size_gb": 8, "throughput_mibps": 500}, "throughput 500"),
    ],
)
def test_paid_disk_tier_fails(attrs, needle):
    bodies = healthy_bodies()
    bodies[f"/v1/projects/{REF}/config/disk"]["attributes"] = attrs
    r = results_by_name(snapshot_for(bodies))["no-other-addons"]
    assert r.status == guard.FAIL
    assert needle in r.detail


def test_bigger_disk_size_alone_is_not_flagged():
    """Disk SIZE is a usage item the Spend Cap covers; only tier/IOPS/throughput
    are opt-in add-ons. Growing to 20 GB must not trip the add-on check."""
    bodies = healthy_bodies()
    bodies[f"/v1/projects/{REF}/config/disk"]["attributes"]["size_gb"] = 20
    r = results_by_name(snapshot_for(bodies))["no-other-addons"]
    assert r.status == guard.PASS


# ----------------------------------------------------------------------------
# (c) the arithmetic
# ----------------------------------------------------------------------------
def test_monthly_conversion_matches_supabase_quote():
    assert guard.monthly_usd({"amount": 0.01344, "interval": "hourly"}) == pytest.approx(
        10.0, 0.001
    )
    assert guard.monthly_usd({"amount": 100, "interval": "monthly"}) == 100.0
    assert guard.monthly_usd(None) == 0.0


def test_projection_micro_is_exactly_the_plan_fee():
    total, lines = guard.projected_monthly(snapshot_for(healthy_bodies()))
    assert total == 25.0
    text = "\n".join(lines)
    assert "plan fee (pro)" in text and "$25.00" in text
    assert "ci_micro" in text and "0.01344/h x 744h" in text
    assert "compute credits" in text and "-$10.00" in text
    assert "= projected monthly" in text
    assert "ceiling" in text


def test_projection_prices_nano_as_micro_from_available_addons():
    snapshot = snapshot_for(healthy_bodies("nano", selected_compute=False))
    total, lines = guard.projected_monthly(snapshot)
    assert total == 25.0
    assert any("Nano billed as Micro" in line for line in lines)


def test_projection_pitr_blows_the_ceiling_by_100():
    bodies = healthy_bodies()
    bodies[f"/v1/projects/{REF}/billing/addons"]["selected_addons"].append(
        {"type": "pitr", "variant": PITR_VARIANT}
    )
    total, _ = guard.projected_monthly(snapshot_for(bodies))
    assert total == 125.0
    r = results_by_name(snapshot_for(bodies))["projected-monthly"]
    assert r.status == guard.FAIL
    assert "$125.00" in r.detail and "$100.00/month" in r.detail
    assert any("pitr/pitr_7" in line and "$100.00" in line for line in r.lines)


def test_credit_never_exceeds_compute_cost():
    """A Free org with no compute still gets no negative credit."""
    bodies = healthy_bodies(selected_compute=False)
    bodies[f"/v1/organizations/{ORG}"]["plan"] = "free"
    bodies[f"/v1/projects/{REF}/billing/addons"]["available_addons"] = []
    total, lines = guard.projected_monthly(snapshot_for(bodies))
    # fee 0 + fallback micro 10.00 - credit 10.00
    assert total == pytest.approx(0.0, abs=0.01)
    assert not any(line.startswith("  = projected monthly") and "-$" in line for line in lines)


def test_projection_prints_arithmetic_lines_on_pass_and_fail():
    r = results_by_name(snapshot_for(healthy_bodies()))["projected-monthly"]
    assert r.status == guard.PASS
    assert len(r.lines) >= 5


# ----------------------------------------------------------------------------
# Redaction
# ----------------------------------------------------------------------------
def test_redact_strips_token_query_and_headers():
    msg = (
        f"GET https://api.supabase.com/v1/projects?access_token={TOKEN}&x=1 failed\n"
        f"Authorization: Bearer {TOKEN}\n"
        f"body: {{'error': 'bad token {TOKEN}'}}"
    )
    out = guard.redact(msg, [TOKEN])
    assert TOKEN not in out
    assert "access_token=" not in out
    assert "?<redacted>" in out
    assert "Authorization: ***" in out


def test_redact_catches_bearer_and_sbp_prefixes_without_knowing_the_value():
    out = guard.redact("Bearer abc.def-ghi and sbp_deadbeef01 leaked")
    assert "abc.def-ghi" not in out and "deadbeef" not in out
    assert out == "Bearer *** and sbp_*** leaked"


def _fake_http_error(code: int, body: str):
    return urllib.error.HTTPError(
        url="https://api.supabase.com/v1/x",
        code=code,
        msg="boom",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(body.encode()),
    )


def test_http_error_message_never_contains_token_or_headers(monkeypatch):
    seen: dict[str, object] = {}

    def fake_urlopen(req, timeout=0):
        seen["auth"] = req.get_header("Authorization")
        raise _fake_http_error(401, f'{{"message":"invalid token {TOKEN}"}}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    fetch = guard.HttpFetcher(TOKEN)
    with pytest.raises(guard.GuardError) as exc:
        fetch(f"/v1/organizations/{ORG}/projects?limit=100&offset=0")
    text = str(exc.value)
    assert seen["auth"] == f"Bearer {TOKEN}"  # it WAS sent...
    assert TOKEN not in text  # ...and never echoed
    assert "HTTP 401" in text
    assert "limit=100" not in text and "?<redacted>" in text
    assert "Authorization" not in text


def test_url_error_is_wrapped_and_redacted(monkeypatch):
    def fake_urlopen(req, timeout=0):
        raise urllib.error.URLError(f"dns failed for token {TOKEN}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(guard.GuardError) as exc:
        guard.HttpFetcher(TOKEN)("/v1/projects")
    assert TOKEN not in str(exc.value)
    assert "GET /v1/projects" in str(exc.value)


def test_http_fetcher_parses_json_and_sends_bearer(monkeypatch):
    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        return Resp(json.dumps({"plan": "pro"}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    body = guard.HttpFetcher(TOKEN)(f"/v1/organizations/{ORG}")
    assert body == {"plan": "pro"}
    assert captured["url"] == f"https://api.supabase.com/v1/organizations/{ORG}"
    assert captured["auth"] == f"Bearer {TOKEN}"


def test_non_json_body_is_a_guard_error(monkeypatch):
    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: Resp(b"<html>"))
    with pytest.raises(guard.GuardError, match="not JSON"):
        guard.HttpFetcher(TOKEN)("/v1/projects")


# ----------------------------------------------------------------------------
# CLI: dry-run, exit codes, output shape
# ----------------------------------------------------------------------------
def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network call attempted")

    monkeypatch.setattr(urllib.request, "urlopen", boom)


def test_dry_run_prints_requests_and_touches_nothing(monkeypatch, capsys):
    _no_network(monkeypatch)
    monkeypatch.delenv("SUPABASE_ACCESS_TOKEN", raising=False)
    code = guard.main(["--ref", REF, "--org", ORG, "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "DRY RUN" in out
    for path in guard.planned_requests(REF, ORG):
        assert f"GET {path}" in out
    assert "Checks:" in out


def test_dry_run_from_env_without_org(monkeypatch, capsys):
    _no_network(monkeypatch)
    monkeypatch.setenv("SUPABASE_PROD_REF", REF)
    monkeypatch.delenv("SUPABASE_PROD_ORG", raising=False)
    assert guard.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "/v1/organizations/{slug}" in out


def test_missing_ref_is_usage_error(monkeypatch, capsys):
    monkeypatch.delenv("SUPABASE_PROD_REF", raising=False)
    assert guard.main(["--dry-run"]) == 2
    assert "--ref" in capsys.readouterr().out


def test_malformed_ref_is_usage_error(monkeypatch, capsys):
    assert guard.main(["--ref", "not-a-ref", "--dry-run"]) == 2
    assert "does not look like a project ref" in capsys.readouterr().out


def test_missing_token_is_usage_error_without_network(monkeypatch, capsys):
    _no_network(monkeypatch)
    monkeypatch.delenv("SUPABASE_ACCESS_TOKEN", raising=False)
    assert guard.main(["--ref", REF]) == 2
    assert "SUPABASE_ACCESS_TOKEN" in capsys.readouterr().out


def test_main_exit_zero_and_named_lines_on_pass(monkeypatch, capsys):
    monkeypatch.setenv("SUPABASE_ACCESS_TOKEN", TOKEN)
    monkeypatch.setattr(guard, "HttpFetcher", lambda token, api_base: FakeFetch(healthy_bodies()))
    code = guard.main(["--ref", REF, "--org", ORG])
    out = capsys.readouterr().out
    assert code == 0
    for name in (
        "plan",
        "org-membership",
        "compute-variant",
        "no-other-addons",
        "projected-monthly",
    ):
        assert f"PASS {name}:" in out
    assert out.rstrip().endswith("nothing more")
    assert TOKEN not in out


def test_main_exit_one_on_any_fail(monkeypatch, capsys):
    bodies = healthy_bodies()
    bodies[f"/v1/projects/{REF}/billing/addons"]["selected_addons"].append(
        {"type": "pitr", "variant": PITR_VARIANT}
    )
    monkeypatch.setenv("SUPABASE_ACCESS_TOKEN", TOKEN)
    monkeypatch.setattr(guard, "HttpFetcher", lambda token, api_base: FakeFetch(bodies))
    code = guard.main(["--ref", REF, "--org", ORG])
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL no-other-addons:" in out
    assert "FAIL projected-monthly:" in out
    assert "FAIL: no-other-addons, projected-monthly" in out


def test_main_warn_only_exits_zero_but_says_so(monkeypatch, capsys):
    monkeypatch.setenv("SUPABASE_ACCESS_TOKEN", TOKEN)
    monkeypatch.setattr(
        guard,
        "HttpFetcher",
        lambda token, api_base: FakeFetch(healthy_bodies("nano", selected_compute=False)),
    )
    code = guard.main(["--ref", REF, "--org", ORG])
    out = capsys.readouterr().out
    assert code == 0
    assert "WARN compute-variant:" in out
    assert "PASS with warnings: compute-variant" in out


def test_main_api_failure_exits_one_and_redacts(monkeypatch, capsys):
    monkeypatch.setenv("SUPABASE_ACCESS_TOKEN", TOKEN)

    def broken(token, api_base):
        def fetch(path):
            raise guard.GuardError(f"GET {path}?t={token} -> HTTP 403: forbidden {token}")

        return fetch

    monkeypatch.setattr(guard, "HttpFetcher", broken)
    code = guard.main(["--ref", REF, "--org", ORG])
    out = capsys.readouterr().out
    assert code == 1
    assert TOKEN not in out
    assert "NOTHING was verified" in out
