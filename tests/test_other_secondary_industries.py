"""Dashboard "Other" table, grouped by secondary industry (fa-web-app#936).

Three things are pinned here:

* the NAME grouping Jake decided (case / spacing / "name + extra detail", and
  anything starting with "Healthcare" is Healthcare) — and nothing fuzzier;
* ``GET /dashboard/other-industries`` returns rows that add up, ordered by
  count, with the population taken from the list's own
  ``industry_group=other`` statement;
* COUNT-vs-LIST parity: the filter each row links to resolves to EXACTLY the
  stored values the row counted, so the list and its CSV are the people in it.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.main import app
from app.models.alumni import Alumni
from app.repositories.alumni import build_alumni_query
from app.schemas.auth import UserContext
from app.services import secondary_industry_groups as sig
from app.services.secondary_industry_groups import SecondaryIndustryGrouper, group_filter


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles) or ["view_only"],
    )


# --- grouping rule ------------------------------------------------------------

_STORED = [
    "Healthcare",
    "Healthcare - Biotech",
    "  healthcare services",
    "HEALTHCARE",
    "HealthcareTech",
    "Health Care",
    "Law",
    "law ",
    "Law (IP)",
    "Law: Litigation",
    "Corporate  Banking",
    "corporate banking",
    "Real Estate Development",
    "Sales and Trading",
    "Sales Operations",
    "Investment Banking Associate",
    "Other",
    "other",
    "Not in the Labor Force",
    "Non-profit",
    "Education Technology",
    "",
    "   ",
]


@pytest.fixture
def grouper():
    return SecondaryIndustryGrouper(_STORED)


@pytest.mark.parametrize(
    "raw",
    [
        "Healthcare",
        "Healthcare - Biotech",
        "  healthcare services",
        "HEALTHCARE",
        "HealthcareTech",
        "healthcare-biotech",
    ],
)
def test_anything_starting_with_healthcare_is_one_group(grouper, raw):
    assert grouper.group_key(raw) == "healthcare"


def test_health_care_with_a_space_is_not_folded_into_healthcare(grouper):
    # Not decided by Jake — left as its own row until he confirms the merge.
    assert grouper.group_key("Health Care") == "health care"


def test_healthcare_folds_even_when_no_one_typed_bare_healthcare():
    only_variants = SecondaryIndustryGrouper(["Healthcare - Biotech", "Healthcare Services"])
    assert only_variants.group_key("Healthcare Services") == "healthcare"
    assert only_variants.group_key("Healthcare - Biotech") == "healthcare"


@pytest.mark.parametrize("raw", ["Law", "law ", "Law (IP)", "Law: Litigation", " LAW,  Corporate"])
def test_case_spacing_and_detail_fold_to_the_name(grouper, raw):
    assert grouper.group_key(raw) == "law"


def test_internal_spacing_is_collapsed(grouper):
    assert grouper.group_key("Corporate  Banking") == grouper.group_key("corporate banking")


def test_known_name_plus_more_words_folds_into_it(grouper):
    assert grouper.group_key("Real Estate Development") == "real estate"
    assert grouper.group_key("Investment Banking Associate") == "investment banking"
    assert grouper.group_key("Sales Operations") == "sales"


def test_a_vocabulary_term_never_folds_into_a_shorter_one(grouper):
    # "Sales" is a term too, but "Sales and Trading" is its own industry.
    assert grouper.group_key("Sales and Trading") == "sales and trading"


def test_no_merge_without_a_known_name_prefix(grouper):
    # "Education" is neither stored nor vocabulary, so nothing to fold into.
    assert grouper.group_key("Education Technology") == "education technology"


def test_hyphenated_word_is_not_detail(grouper):
    assert grouper.group_key("Non-profit") == "non-profit"


def test_literal_other_is_its_own_group(grouper):
    assert grouper.group_key("Other") == grouper.group_key("other") == "other"


def test_new_values_are_handled_like_any_other(grouper):
    assert grouper.group_key("Not in the Labor Force") == "not in the labor force"
    assert grouper.group_key("Brand New Industry") == "brand new industry"


@pytest.mark.parametrize("raw", [None, "", "   ", "\t"])
def test_blank_is_no_secondary(grouper, raw):
    assert grouper.group_key(raw) is None


def test_display_names_prefer_vocab_then_prefix_then_most_used(grouper):
    names = grouper.display_names(
        {
            "law ": 5,
            "Law (IP)": 1,
            "Healthcare - Biotech": 2,
            "healthcare services": 1,
            "education technology": 1,
            "Education Technology": 1,
            "Corporate  Banking": 3,
        }
    )
    assert names["law"] == "Law"
    assert names["healthcare"] == "Healthcare"
    assert names["education technology"] == "Education Technology"
    assert names["corporate banking"] == "Corporate Banking"


# --- the endpoint ---------------------------------------------------------------

# raw secondary value -> people in the Other bar holding it.
_OTHER_COUNTS = {
    "Healthcare": 2,
    "Healthcare - Biotech": 3,
    "  healthcare services": 1,
    "Health Care": 1,
    "Law": 4,
    "law ": 1,
    "Law (IP)": 2,
    "Other": 2,
    "other": 1,
    "Not in the Labor Force": 1,
    None: 5,
    "": 1,
    "   ": 1,
}


class _Session:
    """First execute: the grouped Other-population rows; then the distinct
    stored values (a superset — some alumni outside Other hold others)."""

    def __init__(self):
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        if len(self.statements) == 1:
            return _Rows(list(_OTHER_COUNTS.items()))
        return _Rows([(v,) for v in _STORED if v is not None])


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


@pytest.fixture
def session():
    return _Session()


@pytest.fixture
def client(session):
    async def _override():
        yield session

    app.dependency_overrides[get_session] = _override
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_rows_group_add_up_and_sort_by_count(client):
    response = client.get("/dashboard/other-industries")
    assert response.status_code == 200, response.text
    body = response.json()
    rows = {r["name"]: r for r in body["rows"]}
    assert rows["Healthcare"]["count"] == 6
    assert rows["Health Care"]["count"] == 1
    assert rows["Law"]["count"] == 7
    assert rows["Other"]["count"] == 3
    assert rows["Not in the Labor Force"]["count"] == 1
    none_row = rows[sig.NO_SECONDARY_LABEL]
    assert none_row == {
        "name": "No secondary industry",
        "group": None,
        "no_secondary": True,
        "count": 7,
    }
    assert rows["Law"]["group"] == "Law" and rows["Law"]["no_secondary"] is False
    # Every counted person lands in exactly one row.
    assert body["total"] == sum(_OTHER_COUNTS.values()) == sum(r["count"] for r in body["rows"])
    counts = [r["count"] for r in body["rows"]]
    assert counts == sorted(counts, reverse=True)


def test_population_is_the_lists_industry_group_other_statement(client, session):
    client.get("/dashboard/other-industries")
    counted = str(session.statements[0].compile(dialect=postgresql.dialect()))
    other_list = build_alumni_query(industry_group="other").with_only_columns(
        Alumni.alumni_id
    )
    where = str(other_list.compile(dialect=postgresql.dialect())).split("WHERE", 1)[1]
    assert where.strip() in counted


def test_requires_login():
    app.dependency_overrides.clear()
    with TestClient(app) as c:
        assert c.get("/dashboard/other-industries").status_code in (401, 403)


# --- count-vs-list parity ---------------------------------------------------------


def _in_values(predicate) -> set[str]:
    compiled = predicate.compile(
        dialect=postgresql.dialect(), compile_kwargs={"render_postcompile": True}
    )
    return {v for v in compiled.params.values() if isinstance(v, str)}


def test_each_row_links_to_exactly_the_values_it_counted(client):
    body = client.get("/dashboard/other-industries").json()
    grouper = SecondaryIndustryGrouper(_STORED)
    for row in body["rows"]:
        if row["no_secondary"]:
            continue
        predicate = group_filter(_STORED, [row["group"]], False)
        matched = _in_values(predicate)
        # The stored values the filter matches are exactly that group's values …
        expected = {v for v in _STORED if grouper.group_key(v) == grouper.group_key(row["group"])}
        assert matched == expected
        # … and the people the row counted are exactly the ones holding them.
        counted = sum(n for raw, n in _OTHER_COUNTS.items() if raw in matched)
        assert counted == row["count"], row


def test_no_secondary_row_matches_the_blank_values_it_counted(client):
    body = client.get("/dashboard/other-industries").json()
    none_row = next(r for r in body["rows"] if r["no_secondary"])
    predicate = group_filter(_STORED, None, True)
    sql = str(predicate.compile(dialect=postgresql.dialect()))
    assert sql.startswith("NOT (EXISTS")
    named = _in_values(predicate)
    # NOT EXISTS a named secondary: everyone the row counted holds none of these.
    counted = sum(n for raw, n in _OTHER_COUNTS.items() if raw not in named)
    assert counted == none_row["count"]


def test_unknown_group_matches_nobody_never_everybody():
    predicate = group_filter(_STORED, ["No Such Industry"], False)
    assert str(predicate.compile(dialect=postgresql.dialect())) == "false"


def test_group_filter_is_off_when_not_requested():
    assert group_filter(_STORED, None, False) is None
    assert group_filter(_STORED, [], False) is None
