"""Industry vocabulary: the three sources of truth must agree (#282).

The industry list lives in THREE places that had all silently drifted apart
("Financial Services" was index 14 in the Python tuple but sort_order 19 in the
DB seed; the doc was missing six industries entirely):

* ``app/core/dropdowns.py``  — the ``INDUSTRIES`` tuple, which drives write
  validation, the CSV importer and the intake template.
* ``vocabulary_terms``       — the DB rows seeded by ``database/migrations/*``,
  which drive what the dropdowns actually render.
* ``database/dropdowns.md``  — the human-facing doc that calls ITSELF the single
  source of truth, and which nothing machine-checked until now.

These tests reconcile all three by replaying the migrations' industry INSERTs and
parsing the doc's bullet list, then asserting both match ``INDUSTRIES`` exactly.
They are offline — the SQL and markdown are parsed as text, no database is
touched.

Also covered: the primary/secondary split, and that the split did NOT change the
dashboard wheel (membership or bar order).
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.core.dropdowns import (
    INDUSTRIES,
    PRIMARY_INDUSTRIES,
    SECONDARY_INDUSTRIES,
    WHEEL_INDUSTRIES,
    filter_primary_industries,
    validate_industry,
)
from app.main import app
from app.schemas.auth import UserContext

DATABASE_DIR = Path(__file__).resolve().parents[1] / "database"
MIGRATIONS_DIR = DATABASE_DIR / "migrations"
DROPDOWNS_MD = DATABASE_DIR / "dropdowns.md"

# The four industries Tanya asked to drop from the PRIMARY dropdown (2026-07-16).
# NOTE the stored value is "Sales and Trading", not "Sales & Trading".
SECONDARY_ONLY = ("Law", "Corporate Banking", "Sales and Trading", "Credit Risk")

# A ('industry', 'Some Value', 12) tuple inside a vocabulary_terms INSERT.
_TERM_RE = re.compile(r"\(\s*'industry'\s*,\s*'([^']+)'\s*,\s*(\d+)\s*\)")


def _seeded_industry_terms() -> dict[str, int]:
    """Replay every migration's industry seed, in lexical (= apply) order.

    Returns {value: sort_order} as the DB would hold it. Later migrations win,
    mirroring ``ON CONFLICT ... DO UPDATE SET sort_order``.
    """
    terms: dict[str, int] = {}
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        # Only migrations that actually touch the vocabulary table; this keeps
        # unrelated ('industry', 'CA', ...) rows in the city-geo crosswalk out.
        if "vocabulary_terms" not in sql:
            continue
        for value, sort_order in _TERM_RE.findall(sql):
            terms[value] = int(sort_order)
    return terms


# A bullet in the doc's Industries list, with the optional secondary-only marker:
#   "- Law *(secondary only)*"  ->  ("Law", " *(secondary only)*")
_DOC_BULLET_RE = re.compile(r"^-\s+(.+?)(\s*\*\(secondary only\)\*)?\s*$")


def _documented_industries() -> list[tuple[str, bool]]:
    """Parse the ``## Industries`` option bullets from ``database/dropdowns.md``.

    Returns [(value, secondary_only), ...] in document order. Reads ONLY the
    single unbroken run of bullets immediately following this section's
    "Options:" line, and stops at the first non-bullet line. Anchoring to the
    contiguous run (rather than scanning to the section's ``---`` rule) keeps
    the prose bullets in the "Primary vs secondary" subsection below the list
    from leaking in.
    """
    lines = DROPDOWNS_MD.read_text(encoding="utf-8").splitlines()
    start = lines.index("## Industries")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "---")
    options_at = next(
        i for i in range(start, end) if lines[i].strip() == "Options:"
    )

    found: list[tuple[str, bool]] = []
    for line in lines[options_at + 1 : end]:
        if not line.startswith("-"):
            break
        match = _DOC_BULLET_RE.match(line.strip())
        assert match is not None, f"unparseable option bullet: {line!r}"
        found.append((match.group(1).strip(), match.group(2) is not None))
    return found


def test_doc_actually_lists_industries() -> None:
    """Guard the markdown parser: if the doc gets restructured and the parser
    silently finds nothing, every doc assertion below would pass vacuously."""
    assert len(_documented_industries()) == len(INDUSTRIES)


def test_doc_matches_industries_tuple_in_order() -> None:
    """``database/dropdowns.md`` calls itself the single source of truth, so it
    must agree with the tuple on BOTH membership and dropdown order."""
    assert [value for value, _ in _documented_industries()] == list(INDUSTRIES)


def test_doc_marks_exactly_the_secondary_only_industries() -> None:
    """The doc's *(secondary only)* markers are the human-readable copy of
    PRIMARY_INDUSTRIES — drift here is how someone ends up "restoring" one of the
    four to the primary dropdown."""
    marked = {value for value, secondary_only in _documented_industries() if secondary_only}
    assert marked == set(SECONDARY_ONLY)
    assert marked == set(INDUSTRIES) - set(PRIMARY_INDUSTRIES)


def test_migrations_actually_seed_industries() -> None:
    """Guard the parser itself: a silent regex miss would make every DB-vs-tuple
    assertion below trivially pass."""
    assert len(_seeded_industry_terms()) == len(INDUSTRIES)


def test_db_vocab_values_match_industries_tuple() -> None:
    """Same VALUES in both sources — no term seeded that the validator rejects,
    and no canonical industry missing from the dropdown."""
    assert set(_seeded_industry_terms()) == set(INDUSTRIES)


def test_db_vocab_order_matches_industries_tuple() -> None:
    """Same ORDER in both sources. The DB's sort_order drives the rendered
    dropdown, so this is what actually makes 'Financial Services' land in
    alphabetical order for Tanya."""
    terms = _seeded_industry_terms()
    by_sort_order = sorted(terms, key=lambda v: (terms[v], v))
    assert by_sort_order == list(INDUSTRIES)


def test_db_vocab_sort_order_is_tuple_index_with_other_pinned_last() -> None:
    """The exact contract the migration encodes: sort_order == tuple index for
    the alphabetical body, and the three pinned tail options are held out of the
    body order — "Unknown" at 97, "Graduate Student" at 98 and the "Other"
    catch-all at 99 — so new terms can be appended into the body without
    disturbing them (#295/#294/#282).
    """
    terms = _seeded_industry_terms()
    assert terms["Other"] == 99
    assert terms["Graduate Student"] == 98
    assert terms["Unknown"] == 97
    for index, value in enumerate(INDUSTRIES[:-3]):
        assert terms[value] == index, f"{value} sort_order {terms[value]} != {index}"


# --- ordering ----------------------------------------------------------------


def test_other_is_pinned_last() -> None:
    assert INDUSTRIES[-1] == "Other"


def test_graduate_student_is_pinned_second_to_last() -> None:
    """"Graduate Student" (#294) is held just above "Other", out of the
    otherwise alphabetical order, so it renders as its own bottom-of-list
    indicator on the dashboard."""
    assert INDUSTRIES[-2] == "Graduate Student"


def test_unknown_is_pinned_third_to_last() -> None:
    """"Unknown" (#295) is held just above "Graduate Student" (and thus above
    "Other"), out of the otherwise alphabetical order. It is a distinct
    selectable value meaning "we checked and it is genuinely unknown", not a
    blank/unset ("not yet collected") industry."""
    assert INDUSTRIES[-3] == "Unknown"


def test_industries_are_alphabetical_ignoring_case_other_aside() -> None:
    # Exclude the three pinned tail options ("Unknown", "Graduate Student",
    # "Other"); the rest of the list is alphabetical case-insensitively.
    body = list(INDUSTRIES[:-3])
    assert body == sorted(body, key=str.casefold)


# --- Military (#608) ---------------------------------------------------------


def test_military_is_an_industry() -> None:
    """#608 — a service member had no industry that fit and had to be recorded
    as Other/Unknown, which is what dropped them out of the dashboard breakdown."""
    assert "Military" in INDUSTRIES


def test_military_sits_in_the_alphabetical_body_not_the_pinned_tail() -> None:
    """Unlike "Graduate Student"/"Unknown"/"Other", Military is an ordinary
    answer to "what do you do" — people scan for it under M, so it goes between
    "Law" and "Private Banking" rather than being pinned to the bottom."""
    i = INDUSTRIES.index("Military")
    assert INDUSTRIES[i - 1] == "Law"
    assert INDUSTRIES[i + 1] == "Private Banking"
    assert i < len(INDUSTRIES) - 3


def test_military_is_selectable_as_either_primary_or_secondary() -> None:
    """Jake's reservist case (#608): someone can serve AND hold a civilian job,
    e.g. primary Investment Banking + secondary Military. employment_status is a
    single value and can't express that; industry has two slots, which is why
    Military lives here too. So it must be offered in BOTH dropdowns."""
    assert "Military" in PRIMARY_INDUSTRIES
    assert "Military" in SECONDARY_INDUSTRIES
    assert validate_industry("military") == "Military"


def test_military_is_not_a_wheel_industry() -> None:
    """Jake kept the dashboard industry chart about FINANCE SECTORS, so Military
    gets no bar of its own and simply folds into the "Other" catch-all — the
    default behaviour of _NON_WHEEL_INDUSTRIES, not a special case."""
    assert "Military" not in WHEEL_INDUSTRIES


def test_military_has_no_dashboard_bucket_of_its_own() -> None:
    """Guard the "folds into Other" decision at the contract level: if someone
    later adds a `military` bucket to the breakdown, this fails and they have to
    re-check with Jake first. Contrast `graduate_student`, which IS a bucket."""
    from app.schemas.dashboard import DashboardIndustryBreakdown

    fields = set(DashboardIndustryBreakdown.model_fields)
    assert "graduate_student" in fields
    assert "military" not in fields


def test_military_body_insert_renumbered_the_rest_of_the_body() -> None:
    """Inserting into the alphabetical body shifts every later sort_order, so
    the migration had to re-upsert the whole body. If someone "fixes" it into a
    single-row append later, the seeded order silently stops matching the tuple
    and this catches it."""
    terms = _seeded_industry_terms()
    assert terms["Military"] == INDUSTRIES.index("Military") == 11
    assert terms["Private Banking"] == 12
    assert terms["Wealth Management"] == 20


def test_financial_services_sits_between_equity_research_and_fpa() -> None:
    """Tanya's literal ask (#282): "move Financial Services into alphabetical
    order". Case-insensitively "financial services" < "fp&a"; a case-SENSITIVE
    sort would wrongly put FP&A first."""
    i = INDUSTRIES.index("Financial Services")
    assert INDUSTRIES[i - 1] == "Equity Research"
    assert INDUSTRIES[i + 1] == "FP&A"


# --- primary / secondary split -----------------------------------------------


@pytest.mark.parametrize("value", SECONDARY_ONLY)
def test_secondary_only_industries_are_hidden_from_primary(value: str) -> None:
    assert value not in PRIMARY_INDUSTRIES
    assert value in SECONDARY_INDUSTRIES


def test_secondary_list_is_the_full_vocabulary() -> None:
    assert SECONDARY_INDUSTRIES == INDUSTRIES


def test_primary_is_the_full_list_minus_exactly_the_four() -> None:
    assert set(INDUSTRIES) - set(PRIMARY_INDUSTRIES) == set(SECONDARY_ONLY)


def test_primary_preserves_vocabulary_order() -> None:
    assert list(PRIMARY_INDUSTRIES) == [
        i for i in INDUSTRIES if i in set(PRIMARY_INDUSTRIES)
    ]


def test_fpa_stays_in_primary() -> None:
    """FP&A is a non-wheel industry like the other four, but Tanya did NOT ask
    to remove it — do not "helpfully" drop it (#282)."""
    assert "FP&A" in PRIMARY_INDUSTRIES


def test_other_stays_in_primary() -> None:
    """"Other" is the catch-all the data migration folds records INTO, so it
    must remain selectable as a primary industry."""
    assert "Other" in PRIMARY_INDUSTRIES


@pytest.mark.parametrize("value", SECONDARY_ONLY)
def test_secondary_only_industries_still_validate(value: str) -> None:
    """The split is dropdown VISIBILITY only. These stay writable: the data
    migration deliberately skips conflict rows that keep one as their primary,
    and those profiles must still save without a 422."""
    assert validate_industry(value) == value
    assert validate_industry(value.lower()) == value


def test_filter_primary_industries_is_case_insensitive_and_order_preserving() -> None:
    values = ["Asset Management", "law", " Credit Risk ", "Other"]
    assert filter_primary_industries(values) == ["Asset Management", "Other"]


def test_filter_primary_industries_keeps_unknown_free_text() -> None:
    """Only the four are hidden; an admin-added term the tuple doesn't know
    about must still reach the dropdown."""
    assert filter_primary_industries(["Underwater Basket Weaving"]) == [
        "Underwater Basket Weaving"
    ]


# --- dashboard must not change (#282) ----------------------------------------

# The dashboard industry breakdown as it rendered BEFORE #282, frozen verbatim.
# Both the bar order and the membership are asserted against it, because
# dashboard.py emits one bar per entry IN THIS ORDER and repositories/alumni.py
# keys the alumni-list `industry_group=other` drill-down off the same set.
_WHEEL_BEFORE_282 = (
    "Asset Management",
    "Commercial Banking",
    "Consulting",
    "Corporate Finance",
    "Equity Research",
    "Investment Banking",
    "Private Banking",
    "Private Credit",
    "Private Equity",
    "Real Estate",
    "Sales",
    "Valuation & Advisory",
    "Venture Capital",
    "Wealth Management",
    "Financial Services",
)


def test_wheel_industries_unchanged_by_the_primary_secondary_split() -> None:
    """#282 alphabetized the dropdown; the dashboard must be byte-identical."""
    assert WHEEL_INDUSTRIES == _WHEEL_BEFORE_282


def test_wheel_membership_still_derives_from_industries() -> None:
    """WHEEL_INDUSTRIES' order is pinned by hand, so this is the guard that
    stops it rotting: it must still be exactly INDUSTRIES minus the non-wheel
    values. Adding a wheel industry to INDUSTRIES alone fails here."""
    from app.core.dropdowns import _NON_WHEEL_INDUSTRIES

    assert set(WHEEL_INDUSTRIES) == set(INDUSTRIES) - _NON_WHEEL_INDUSTRIES


@pytest.mark.parametrize("value", SECONDARY_ONLY)
def test_secondary_only_industries_were_already_non_wheel(value: str) -> None:
    """Why the dashboard can't move: all four already folded into the "Other"
    slice before this change, so rewriting them to the literal "Other" doesn't
    reassign anyone."""
    assert value not in WHEEL_INDUSTRIES


# --- GET /vocabulary/industry?scope=primary ----------------------------------


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


@pytest.fixture
def client(monkeypatch):
    async def _fake_list_active_values(session, category):
        return list(INDUSTRIES)

    monkeypatch.setattr(
        "app.services.vocabulary.list_active_values", _fake_list_active_values
    )

    async def _no_db_session():
        yield None

    app.dependency_overrides[get_session] = _no_db_session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_vocabulary_industry_defaults_to_the_full_list(client) -> None:
    """Unchanged default = the secondary dropdown keeps working untouched."""
    resp = client.get("/vocabulary/industry")
    assert resp.status_code == 200
    assert resp.json()["values"] == list(SECONDARY_INDUSTRIES)


def test_vocabulary_industry_scope_primary_hides_the_four(client) -> None:
    resp = client.get("/vocabulary/industry", params={"scope": "primary"})
    assert resp.status_code == 200
    values = resp.json()["values"]
    assert values == list(PRIMARY_INDUSTRIES)
    for value in SECONDARY_ONLY:
        assert value not in values


def test_vocabulary_scope_all_is_the_default(client) -> None:
    resp = client.get("/vocabulary/industry", params={"scope": "all"})
    assert resp.status_code == 200
    assert resp.json()["values"] == list(INDUSTRIES)


def test_vocabulary_scope_primary_does_not_affect_other_categories(
    client, monkeypatch
) -> None:
    async def _fake(session, category):
        return ["Law", "Networking"]  # 'Law' is only special for industry

    monkeypatch.setattr("app.services.vocabulary.list_active_values", _fake)
    resp = client.get("/vocabulary/interaction_type", params={"scope": "primary"})
    assert resp.status_code == 200
    assert resp.json()["values"] == ["Law", "Networking"]


def test_vocabulary_rejects_an_unknown_scope(client) -> None:
    assert client.get("/vocabulary/industry", params={"scope": "bogus"}).status_code == 422


# ================== THE DASHBOARD'S ROW BUDGET (added 2026-08-21) ============


def test_the_wheel_still_fits_the_dashboard_breakdown_panel():
    """⚠️ THE INDUSTRY COUNT IS A LAYOUT CONSTRAINT, NOT JUST A VOCABULARY.

    The dashboard's Industry breakdown panel does NOT scroll (Jake, 2026-08-20).
    Its rows compress to fill whatever height is left after the hero and the KPI
    strip, down to a 14px floor — and that only works because the row COUNT is
    bounded. The panel draws one row per wheel industry plus the "Other" and
    "Unknown" buckets; a stored value outside the vocabulary is folded into one
    of those rather than adding a row, so the list cannot grow as alumni are
    added.

    It CAN grow if someone edits this tuple, which is what this test is for. At
    the 14px floor, 17 rows need roughly 240px, which is about what the panel has
    on a laptop. Another industry or two is probably absorbed; several more and
    the rows hit the floor and the tail is clipped with NOTHING ON SCREEN SAYING
    SO — the trade that was accepted when the scrollbar was removed.

    If you have just added an industry and landed here: bumping the number below
    is not the fix on its own. Open the dashboard at a laptop height and check
    the last row is still visible. If it is not, the height has to come from
    somewhere else, the way the hero band and the KPI tiles gave up theirs (see
    fa-web-app/src/app/(app)/dashboard/page.tsx).
    """
    # Fold-in buckets the panel draws alongside the wheel: the "Other" catch-all
    # and the "Unknown" data-gap row.
    EXTRA_BUCKETS = 2
    TUNED_FOR_ROWS = 17

    assert len(WHEEL_INDUSTRIES) + EXTRA_BUCKETS == TUNED_FOR_ROWS, (
        f"The dashboard breakdown was tuned for {TUNED_FOR_ROWS} rows and this "
        f"vocabulary now produces {len(WHEEL_INDUSTRIES) + EXTRA_BUCKETS}. "
        "Check the panel still fits at a laptop height before changing this."
    )
