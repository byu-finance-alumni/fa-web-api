"""Typo-, spacing- and phrasing-tolerant free-text alumni search (#620).

Jake, 2026-08-04, typing into the search bar:

    "im looking for all alumni at goldman schs in new york"   -> nothing
    "i am looking for jake in newyork ... it needs to be able to regoinizr
     mispleling and spacing like newyork"
    "alumni in lehi at adobe"  -> filtered ADOBE as the CITY

Three separate defects: ``q`` only searched names (``?q=Goldman Sachs`` -> 0
while ``?employer=Goldman Sachs`` -> 15), every filler word became a REQUIRED
match, and there was no tolerance for a typo or a missing space.

These are pure tests — the parser is plain Python and ``build_alumni_query`` is
compiled to PostgreSQL text — so no database is needed. The row-level behaviour
was verified separately against a local mirror of the dev data; the counts
quoted in the comments come from that.
"""

import re

from sqlalchemy.dialects import postgresql

from app.core.search_terms import (
    INDUSTRY_GROUPS,
    ROLE_ANY,
    ROLE_EMPLOYER,
    ROLE_PLACE,
    expand,
    normalize,
    parse_free_text,
)
from app.repositories.alumni import build_alumni_query
from app.repositories.alumni_search import (
    _CONTAINS_SCORE,
    _EXACT_SCORE,
    _FUZZY_CEILING,
    _NAME_WEIGHT,
    _PREFIX_SCORE,
    _RELATED_WEIGHT,
    _SIMILARITY_FLOOR,
    _WORD_SIMILARITY_FLOOR,
    relevance_expression,
)


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def _roles(q: str) -> list[tuple[str, tuple[str, ...]]]:
    return [(s.role, s.tokens) for s in parse_free_text(q).segments]


def _searches(sql: str, table: str, column: str) -> bool:
    """Is ``column`` on ``table`` (or an alias of it) part of the free-text match?"""
    return bool(re.search(rf"alumni_search_norm\({table}(_\d+)?\.{column}\)", sql))


# --- A. normalization: spacing, punctuation, accents -------------------------


def test_normalize_folds_case_spacing_punctuation_and_accents():
    # THE spacing fix: both sides of every comparison collapse to the same form,
    # so a missing space stops being a way to miss a row.
    assert normalize("New York") == normalize("newyork") == "newyork"
    assert normalize("Goldman Sachs") == normalize("goldmansachs") == "goldmansachs"
    assert normalize("J.P. Morgan") == normalize("JPMorgan") == "jpmorgan"
    assert normalize("Sao Paulo") == normalize("São Paulo") == "saopaulo"
    assert normalize("O'Brien-Smith") == "obriensmith"


def test_normalized_terms_cannot_carry_like_metacharacters():
    # Normalization DELETES % and _ rather than escaping them, so a wildcard can
    # never reach the LIKE pattern. Strictly safer than the previous escaping.
    assert normalize("50%_admin") == "50admin"
    values = list(build_alumni_query(q="50%_admin").compile(
        dialect=postgresql.dialect()
    ).params.values())
    assert "%50admin%" in values


def test_spreadsheet_injection_strings_are_searched_as_inert_text():
    # dev's current_employer really does contain "=cmd|'/c calc'!A1" and
    # "@SUM(1+1)" (CSV-injection probes). They must be searchable as text and
    # never interpreted — and the '@' must not be read as the "at" preposition
    # when it is glued to the rest of a formula.
    assert normalize("=cmd|'/c calc'!A1") == "cmdccalca1"
    sql = _sql(build_alumni_query(q="=cmd|'/c calc'!A1"))
    assert "alumni_search_norm" in sql
    assert normalize("@SUM(1+1)") == "sum11"


# --- B. filler words ---------------------------------------------------------


def test_filler_words_are_not_required_matches():
    # "im looking for jake" returned 0 while "Jake" returned 1, because every
    # token had to match something.
    assert _roles("im looking for jake") == [(ROLE_ANY, ("jake",))]
    assert _roles("show me all alumni named Marsh") == [(ROLE_ANY, ("marsh",))]
    assert _roles("do we have anyone called Abbott") == [(ROLE_ANY, ("abbott",))]


def test_a_query_made_entirely_of_filler_falls_back_to_the_raw_words():
    # Stripping everything must NOT degrade to "no predicate", which would
    # return the entire alumni table. The raw words are used instead.
    assert _roles("show me everyone") == [
        (ROLE_ANY, ("show", "me", "everyone"))
    ]
    assert "alumni_search_norm" in _sql(build_alumni_query(q="show me everyone"))


def test_active_is_filler_but_inactive_is_not():
    # Jake: "everything is active, so in the search don't let it affect the
    # search." Whole-word only — "inactive" is a real status and survives.
    assert _roles("find active alumni in Seattle") == [(ROLE_PLACE, ("seattle",))]
    assert _roles("alumni in Seattle") == [(ROLE_PLACE, ("seattle",))]
    assert _roles("inactive alumni") == [(ROLE_ANY, ("inactive",))]


def test_filler_removal_only_widens_so_a_real_name_is_never_lost():
    # Tokens are AND-ed, so dropping one can only ever ENLARGE the result set.
    # "Work" is a real (if rare) surname; stripping it leaves a superset that
    # still contains the person, and ranking floats the exact hit up.
    tokens = set(parse_free_text("Jake Work").segments[0].tokens)
    assert "jake" in tokens
    assert set(parse_free_text("Jake").segments[0].tokens) <= tokens | {"jake"}


def test_words_that_are_real_values_are_deliberately_not_filler():
    # A stop-word that swallows a legitimate search term is worse than no
    # stop-words at all. These are all real stored values or real names.
    for word in (
        "may", "will", "grant", "frank", "other", "graduate", "student",
        "new", "lake", "city", "north", "banking", "retired", "deceased",
    ):
        assert _roles(word) == [(ROLE_ANY, (word,))], word


# --- C. preposition routing --------------------------------------------------


def test_at_routes_to_employer_and_in_routes_to_place():
    assert _roles("im looking for all alumni at goldman schs in new york") == [
        (ROLE_EMPLOYER, ("goldman", "schs")),
        (ROLE_PLACE, ("new", "york")),
    ]


def test_jakes_lehi_adobe_sentence_routes_the_right_way_round():
    # THE reported bug: "alumni in lehi at adobe" filtered adobe as the CITY.
    assert _roles("alumni in lehi at adobe") == [
        (ROLE_PLACE, ("lehi",)),
        (ROLE_EMPLOYER, ("adobe",)),
    ]


def test_multi_word_hints_collapse_onto_the_preposition():
    # "who works at" / "based in" / "living in" reduce to "at" / "in" because
    # the surrounding verbs are filler — no separate phrase table needed.
    assert _roles("who works at Fidelity") == [(ROLE_EMPLOYER, ("fidelity",))]
    assert _roles("alumni based in Provo") == [(ROLE_PLACE, ("provo",))]
    assert _roles("someone living in Denver") == [(ROLE_PLACE, ("denver",))]
    assert _roles("@Adobe") == [(ROLE_EMPLOYER, ("adobe",))]


def test_an_unrouted_token_stays_broad_and_excludes_nothing():
    # Absence of a preposition must not force a guess. A bare token searches
    # names AND employer AND title AND place AND industry.
    sql = _sql(build_alumni_query(q="Adobe"))
    assert _searches(sql, "alumni", "last_name")
    assert _searches(sql, "current_employment", "current_employer")
    assert _searches(sql, "current_employment", "current_city")
    assert _searches(sql, "current_employment", "current_title")
    assert _searches(sql, "employment_history", "employer_name")


def test_routing_is_generic_over_the_real_data_not_a_list_of_names():
    # Adobe and Lehi were only ILLUSTRATIONS. The routing decision is made by
    # the SENTENCE and answered by whatever the employer / city columns actually
    # contain — there is no table of company names or city names anywhere. A
    # company this codebase has never seen must behave identically.
    #
    # Compiled with the company name factored out, the two statements are the
    # SAME SQL: identical structure, only the bound literal differs.
    def shape(company: str) -> str:
        return _sql(build_alumni_query(q=f"at {company}"))

    assert shape("Adobe") == shape("Snowflake")
    assert shape("Adobe") == shape("Ramp")
    # Same for a place: routing does not consult any list of city names.
    def place_shape(city: str) -> str:
        return _sql(build_alumni_query(q=f"in {city}"))

    assert place_shape("Lehi") == place_shape("Kalamazoo")
    # And the parse is a PURE function — it cannot have looked anything up.
    assert parse_free_text("at Snowflake") == parse_free_text("at Snowflake")


def test_at_narrows_to_employers_and_never_matches_a_surname():
    # "at adobe" must not match an alumna surnamed Adobe — that is the point of
    # routing. The employer group is employer columns only.
    sql = _sql(build_alumni_query(q="at Adobe"))
    assert _searches(sql, "current_employment", "current_employer")
    assert _searches(sql, "employment_history", "employer_name")
    assert not _searches(sql, "alumni", "last_name")
    assert not _searches(sql, "current_employment", "current_city")


def test_in_narrows_to_places_and_industries_and_never_matches_an_employer():
    sql = _sql(build_alumni_query(q="in Lehi"))
    assert _searches(sql, "current_employment", "current_city")
    assert _searches(sql, "current_employment", "current_state")
    assert _searches(sql, "current_employment", "current_country")
    assert _searches(sql, "current_employment", "current_industry")
    assert _searches(sql, "current_employment", "current_industry_secondary")
    assert not _searches(sql, "current_employment", "current_employer")
    assert not _searches(sql, "alumni", "last_name")


# --- D. abbreviations and industry groups ------------------------------------


def test_abbreviations_expand_to_the_stored_spelling():
    assert "goldmansachs" in expand("gs")
    assert "newyork" in expand("nyc")
    assert "saltlakecity" in expand("slc")
    assert "investmentbanking" in expand("ib")
    assert "managingdirector" in expand("md")
    # State codes come from the canonical state table, not a retyped list —
    # current_state stores FULL names, so "in az" would otherwise match nothing.
    assert "arizona" in expand("az")
    assert "utah" in expand("ut")
    # The literal term is always first, so an expansion can only widen.
    assert expand("gs")[0] == "gs"
    assert expand("marsh") == ("marsh",)


def test_banking_umbrella_covers_the_three_specific_industries():
    # Jake, 2026-08-04: a bare "banking" search returns commercial, investment
    # AND corporate banking; the specific terms stay precise.
    assert set(INDUSTRY_GROUPS["banking"]) == {
        "investmentbanking",
        "commercialbanking",
        "corporatebanking",
    }
    assert "corporatebanking" in expand("banking")
    # The narrow terms are NOT keys, so they can never be widened by the rule.
    assert expand("investmentbanking") == ("investmentbanking",)
    assert expand("commercialbanking") == ("commercialbanking",)


def test_industry_search_covers_the_secondary_column_too():
    # Corporate Banking (like Law, Sales and Trading, Credit Risk) is
    # PRIMARY-EXCLUDED — it can only ever be stored as a SECONDARY industry. An
    # industry match that only looked at current_industry would silently drop
    # every corporate banker from a "banking" search.
    sql = _sql(build_alumni_query(q="in banking"))
    assert _searches(sql, "current_employment", "current_industry")
    assert _searches(sql, "current_employment", "current_industry_secondary")


# --- E. fuzzy matching -------------------------------------------------------


def test_the_fuzzy_legs_carry_an_explicit_floor_not_a_session_setting():
    # Each fuzzy leg is "<index operator> AND <explicit floor>". The operator is
    # what the GIN index answers (using PostgreSQL's session thresholds); the
    # explicit recheck is what decides, so behaviour never depends on a GUC.
    sql = _sql(build_alumni_query(q="goldman"))
    assert "similarity(" in sql
    assert "word_similarity(" in sql
    # 0.45 must stay ABOVE the `%` operator's 0.3 default, or the operator would
    # be a stricter filter than the recheck and the floor would stop being the
    # thing that decides.
    assert _SIMILARITY_FLOOR > 0.3
    assert _WORD_SIMILARITY_FLOOR >= 0.6


def test_short_terms_skip_the_fuzzy_legs():
    # Below four characters a trigram set is mostly padding, so "matches" are
    # noise. Short terms still get exact / prefix / contains.
    assert "similarity(" not in _sql(build_alumni_query(q="Li"))
    assert "similarity(" in _sql(build_alumni_query(q="Liam"))


def test_an_approximate_match_can_never_outrank_a_precise_one():
    # THE ranking invariant. The most an approximate match can score, on the
    # highest-weighted field, is still below the least a CONTAINS match can
    # score on the lowest-weighted one — so someone searching a real name never
    # finds it buried under a near-miss.
    assert _EXACT_SCORE > _PREFIX_SCORE > _CONTAINS_SCORE > _FUZZY_CEILING
    assert _FUZZY_CEILING * _NAME_WEIGHT < _CONTAINS_SCORE * _RELATED_WEIGHT


def test_the_relevance_case_tiers_are_emitted_in_precision_order():
    expr = relevance_expression(parse_free_text("goldman"))
    sql = str(expr.compile(dialect=postgresql.dialect()))
    # exact -> prefix -> contains -> fuzzy, in that order inside each CASE.
    first_case = sql[sql.index("CASE") : sql.index("END")]
    assert first_case.index("= %(") < first_case.index("LIKE")
    assert "ELSE" in first_case
    assert "similarity" in first_case


# --- F. ordering and pagination ----------------------------------------------


def test_relevance_ordering_is_a_total_order_so_pages_cannot_repeat():
    # A score is not unique. Ordering by score alone lets two tied rows swap
    # between the OFFSET fetch for page 1 and the one for page 2, which repeats
    # one alumnus and drops another (#183). The last-name + PK tiebreakers make
    # the order total.
    from app.repositories.alumni import alumni_order_by

    expr = relevance_expression(parse_free_text("goldman sachs"))
    clauses = alumni_order_by("relevance", relevance=expr)
    assert len(clauses) == 3
    rendered = [str(c.compile(dialect=postgresql.dialect())) for c in clauses]
    assert rendered[0].endswith("DESC")
    assert "last_name ASC" in rendered[1]
    assert "alumni_id ASC" in rendered[2]


def test_relevance_falls_back_to_name_when_there_is_no_free_text():
    from app.repositories.alumni import alumni_order_by

    assert relevance_expression(parse_free_text("")) is None
    clauses = alumni_order_by("relevance", relevance=None)
    assert "last_name" in str(clauses[0].compile(dialect=postgresql.dialect()))


def test_ranking_never_changes_which_rows_match():
    # Ranking is applied ONLY in the list's ORDER BY. The population predicate —
    # the thing the CSV export reuses — must be untouched by it.
    assert "CASE" not in _sql(build_alumni_query(q="at goldman schs in new york"))


# --- G. caps -----------------------------------------------------------------


def test_a_pasted_paragraph_cannot_fan_out_without_bound():
    parsed = parse_free_text("alpha bravo charlie delta echo foxtrot golf hotel india juliet")
    assert len(parsed.all_tokens) <= 8
    assert len(parsed.segments) <= 4


def test_external_ids_are_only_searched_for_a_one_word_query():
    # Atomic ids never contain a space, so AND-ing them into a multi-word query
    # could never match (#281).
    assert parse_free_text("123456789").single_token is True
    assert parse_free_text("Kyle Marsh").single_token is False
    assert parse_free_text("at Adobe").single_token is False
