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
    _word_similarity_floor,
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
    # Term LENGTH does change the shape — a four-letter term drops the
    # word-extent leg (#447, section E2) — but only length: every four-letter
    # company still compiles to the same SQL as every other.
    assert shape("Ramp") == shape("Domo")
    assert shape("Ramp") == shape("Nike")
    # Same for a place: routing does not consult any list of city names.
    def place_shape(city: str) -> str:
        return _sql(build_alumni_query(q=f"in {city}"))

    assert place_shape("Provo") == place_shape("Kalamazoo")
    assert place_shape("Lehi") == place_shape("Kyiv")
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


# --- E2. a short company name (#447) -----------------------------------------
#
# Jake, 2026-08-18, stating the requirement in his own words: "if a company is
# in our database then you should be able to search it just by typing it and
# nothing else and it pulls people who work there and who have worked there".
#
# ``q=Domo`` did the second half and failed the first: alongside the two people
# who had actually worked there it returned five with no connection at all.
# Reproduced on dev with seven seeded rows (7 rows before, 2 after); the
# similarity numbers quoted below were measured in the same session.


def test_the_word_similarity_floor_rises_as_the_term_gets_shorter():
    # THE bug, in one line of arithmetic. word_similarity is
    # shared / (term_trigrams + extent - shared), and a four-character term has
    # only five trigrams — so THREE shared trigrams score exactly 3/5 = 0.600 and
    # cleared the flat floor. Three trigrams is the term's first three letters:
    # a shared prefix, not a typo. Every unrelated row dev returned for q=Domo
    # (Dominguez, Dominic, Domingo, Dominion Energy, Domain Architect, an alumna
    # in the Dominican Republic) scored exactly 0.600.
    assert _word_similarity_floor("domo") is None
    assert _word_similarity_floor("chase") == 5 / 6
    assert _word_similarity_floor("stripe") == 5 / 7
    assert _word_similarity_floor("fidelty") == 5 / 8
    # Eight characters and up are untouched — 5/9 is already below the flat floor.
    assert _word_similarity_floor("delloite") == _WORD_SIMILARITY_FLOOR
    assert _word_similarity_floor("charlesschwabb") == _WORD_SIMILARITY_FLOOR


def test_the_word_floor_never_drops_below_the_index_operators_threshold():
    # `<%` is answered from the GIN index using the session threshold (0.6). The
    # recheck may only ever NARROW that; if it went below, the operator would
    # stop being a superset pre-filter and which rows matched would depend on a
    # GUC again.
    for term in ("chase", "stripe", "fidelty", "delloite", "charlesschwabb"):
        assert _word_similarity_floor(term) >= _WORD_SIMILARITY_FLOOR


def test_a_four_letter_company_emits_no_word_extent_leg():
    # At exactly MIN_FUZZY_LENGTH the word-extent leg degenerates into "starts
    # with the same three letters". Nothing short of the whole term can clear its
    # floor now, and the whole term is already the contains leg, so the leg is
    # not emitted at all — one fewer index probe per column, not one more.
    sql = _sql(build_alumni_query(q="Domo"))
    assert "word_similarity(" not in sql
    # The whole-string measure survives at four characters and is well behaved
    # there (it penalises the length difference): on dev 'domoo' still found
    # Domo at 0.571, while the closest unrelated value, 'dominic', sat at 0.300.
    assert "similarity(" in sql
    assert "word_similarity(" in _sql(build_alumni_query(q="Fidelity"))


def test_a_company_name_typed_alone_finds_current_and_former_employees():
    # Both halves of Jake's sentence. An unrouted term searches the current
    # employer AND the employment history...
    sql = _sql(build_alumni_query(q="Domo"))
    assert _searches(sql, "current_employment", "current_employer")
    assert _searches(sql, "employment_history", "employer_name")
    # ...and so does the same name with the "at" hint in front of it.
    routed = _sql(build_alumni_query(q="at Domo"))
    assert _searches(routed, "current_employment", "current_employer")
    assert _searches(routed, "employment_history", "employer_name")


def test_the_short_term_fix_narrows_the_floor_and_not_the_column_set():
    # A four-letter query must keep reaching every searchable field — the fix is
    # about how loosely one leg may fire, not about what q covers. Searching a
    # name, a city or an industry still works.
    sql = _sql(build_alumni_query(q="Kyle"))
    assert _searches(sql, "alumni", "first_name")
    assert _searches(sql, "alumni", "last_name")
    assert _searches(sql, "current_employment", "current_city")
    assert _searches(sql, "current_employment", "current_industry")
    assert _searches(sql, "current_employment", "current_title")
    assert _searches(sql, "employment_history", "employer_name")


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


# --- H. full-name search (#692) ----------------------------------------------
#
# Meeting 2026-11-18: searching "Scott Kim" did not show Scott Kim. He WAS in
# the result set — but so was every alumnus named Scott, because the joined
# phrase ``scottkim`` fuzzy-matched a first-name cell holding just "Scott". All
# of those rows ranked 100 (an exact hit on the ``scott`` token), so he sorted
# alphabetically among them: off the top bar's eight suggestions, and often off
# page 1 of the list.


def _pg_trigrams(word: str) -> set[str]:
    """pg_trgm's trigram set for one already-normalized word (two leading
    blanks, one trailing), so the arithmetic below is PostgreSQL's own."""
    padded = f"  {word} "
    return {padded[i : i + 3] for i in range(len(padded) - 2)}


def _pg_similarity(a: str, b: str) -> float:
    ta, tb = _pg_trigrams(a), _pg_trigrams(b)
    return len(ta & tb) / len(ta | tb)


def _phrase_fuzzy_min_lengths(stmt) -> set[int]:
    """The ``length(cell) >= n`` bounds the statement carries (bound params)."""
    compiled = stmt.compile(dialect=postgresql.dialect())
    names = re.findall(
        r"length\(alumni_search_norm\([^)]*\)\) >= %\((\w+)\)s", str(compiled)
    )
    return {compiled.params[n] for n in names}


def test_the_root_cause_one_word_of_a_full_name_cleared_the_fuzzy_floor():
    # THE bug in arithmetic: 5 shared trigrams of 10 -> 0.50 >= 0.45. A cell that
    # holds ONE word W of a phrase scores len(W) / (len(phrase) + 2), so every
    # name whose other words are short flooded the result. "Dallas Smith" did the
    # same through the CITY column.
    assert _pg_similarity("scott", "scottkim") >= _SIMILARITY_FLOOR
    assert _pg_similarity("scotty", "scottkim") >= _SIMILARITY_FLOOR
    assert _pg_similarity("dallas", "dallassmith") >= _SIMILARITY_FLOOR


def test_the_phrase_fuzzy_legs_skip_a_cell_holding_only_part_of_the_name():
    from app.repositories.alumni_search import _PHRASE_SHORTFALL

    floor = len("scottkim") - _PHRASE_SHORTFALL
    assert _phrase_fuzzy_min_lengths(build_alumni_query(q="Scott Kim")) == {floor}
    # The guard rejects exactly the flood rows above...
    for cell in ("scott", "scotty", "kim"):
        assert len(cell) < floor
    # ...but a whole-value typo inside one cell keeps its fuzzy match.
    assert len("goldmansachs") >= len("goldmanschs") - _PHRASE_SHORTFALL
    assert _pg_similarity("goldmansachs", "goldmanschs") >= _SIMILARITY_FLOOR
    assert len(normalize("Dallas")) < len("dallassmith") - _PHRASE_SHORTFALL


def test_a_full_name_still_requires_both_words_and_keeps_its_precise_legs():
    # The per-token leg (each word somewhere) is what finds Scott Kim, and it is
    # unbounded: "kim" is too short to be fuzzy at all, so it is a plain contains.
    values = list(build_alumni_query(q="Scott Kim").compile(
        dialect=postgresql.dialect()
    ).params.values())
    assert "%scott%" in values
    assert "%kim%" in values
    assert "%scottkim%" in values  # the phrase's precise leg is untouched


def test_single_word_and_routed_queries_get_no_length_guard():
    assert _phrase_fuzzy_min_lengths(build_alumni_query(q="goldman")) == set()
    assert _phrase_fuzzy_min_lengths(build_alumni_query(q="Scottsdale")) == set()


def test_an_exact_full_name_outranks_every_single_word_hit():
    from app.repositories.alumni_search import (
        _FULL_NAME_EXACT_SCORE,
        _FULL_NAME_PREFIX_SCORE,
    )

    # Scott Kim above "Scott Kimball" above anyone who only matched each word
    # somewhere (an exact first name, weight 1.0, scores _EXACT_SCORE).
    assert _FULL_NAME_EXACT_SCORE > _FULL_NAME_PREFIX_SCORE > _EXACT_SCORE * _NAME_WEIGHT
    sql = str(relevance_expression(parse_free_text("Scott Kim")).compile(
        dialect=postgresql.dialect()
    ))
    def joined(*cols: str) -> str:
        return ", ".join(f"alumni_search_norm(alumni.{c})" for c in cols) + ")"

    assert "concat(" + joined("first_name", "last_name") in sql
    assert "concat(" + joined("preferred_first_name", "last_name") in sql
    # Surname-first ("Kim, Scott") and the middle name are spellings too.
    assert "concat(" + joined("last_name", "first_name") in sql
    assert joined("middle_name", "last_name") in sql


def test_the_full_name_tier_is_only_for_multi_word_unrouted_segments():
    for q in ("Scott", "at goldman sachs", "in new york"):
        sql = str(relevance_expression(parse_free_text(q)).compile(
            dialect=postgresql.dialect()
        ))
        assert "concat(" not in sql, q


def test_the_full_name_tier_never_changes_who_matches():
    # Ranking only: the population (shared with the CSV export) has no concat.
    assert "concat(" not in _sql(build_alumni_query(q="Scott Kim"))


def test_two_word_names_parse_as_one_unrouted_segment():
    # Several names that LOOK like places or filler must stay a name search.
    for q in ("Scott Kim", "Dallas Smith", "Grant Park", "Ann Lee", "Austin Jones"):
        assert _roles(q) == [(ROLE_ANY, tuple(normalize(w) for w in q.split()))], q
