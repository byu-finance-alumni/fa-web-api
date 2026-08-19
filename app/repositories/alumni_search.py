"""SQL for the typo- and spacing-tolerant free-text alumni search (#620).

Companion to :mod:`app.core.search_terms` (which parses the typed sentence) and
to ``database/migrations/2026-08-05_fuzzy_alumni_search.sql`` (which supplies the
``alumni_search_norm`` function and the pg_trgm GIN indexes).

Everything here is a SQLAlchemy expression — nothing fetches rows and nothing
scores in Python. At 8,000+ alumni and growing, pulling candidates into the
application to rank them is not an option.

WHAT ``q`` MATCHES NOW
----------------------
The old ``q`` was ``ILIKE '%token%'`` over the NAME columns only, which is why
``?q=Goldman Sachs`` returned 0 while ``?employer=Goldman Sachs`` returned 15.
``q`` now also reaches the current employer, title, city, state, country and
industry, plus past employers.

HOW A TERM IS MATCHED (in rank order)
-------------------------------------
Both sides are reduced to ``alumni_search_norm`` — lower-cased, accent-folded,
every non-alphanumeric character deleted — and then, cheapest and most exact
first:

  1. ``= term``            exact
  2. ``LIKE term || '%'``  prefix
  3. ``LIKE '%term%'``     contains        <- also covers the missing-space case:
                                             'newyork' is literally contained in
                                             normalize('New York') = 'newyork'
  4. trigram similarity    approximate     <- 'goldmanschs' -> 'goldmansachs'

Legs 1-3 are the *precise* legs; leg 4 is the guess. The ranking below is built
so leg 4 can NEVER outrank legs 1-3 (see ``_FUZZY_CEILING`` vs
``_CONTAINS_SCORE``): someone searching a real name never finds it buried under
a near-miss.

WHY TWO FUZZY MEASURES
----------------------
``similarity()`` compares whole strings, so it degrades as the stored value gets
longer than the query: similarity('fidelty', 'fidelity') = 0.55 but
similarity('fidelty', 'fidelityinvestments') = 0.22. ``word_similarity()``
compares the query against the best *extent* of the stored value and stays at
0.63 for both. Whole-string similarity is the accurate measure for a full-name
typo; word similarity is the one that finds a short query inside a long company
name. A term passes if EITHER clears its own floor.

That second measure is the one that has to be length-aware. Ignoring the rest of
the stored value is exactly what makes it useful, and exactly what makes it
reckless for a SHORT term: see ``_MIN_SHARED_TRIGRAMS`` below for why a flat
floor let ``q=Domo`` return the Dominguez family (#447).

INDEX USE
---------
Each fuzzy leg is written as ``<trigram operator> AND <explicit floor>``:

* the OPERATOR (``%`` / ``<%``) is what the GIN index can answer, and it uses
  PostgreSQL's session thresholds (0.3 and 0.6 by default);
* the explicit ``similarity(...) >= FLOOR`` recheck is what actually decides,
  so the result does not depend on a session GUC.

Because ``_SIMILARITY_FLOOR`` (0.45) is ABOVE the operator's 0.3 default, the
operator is a pure superset pre-filter and the recheck narrows it to exactly the
intended set. The same holds for the word leg: its floor is never allowed BELOW
``_WORD_SIMILARITY_FLOOR`` (0.6), which is the ``<%`` session threshold, so
tightening it for a short term narrows the recheck and never the index probe.
Conceptually the plan is: GIN bitmap index scan on the normalized
expression -> recheck the floor -> (for the list) evaluate the ranking CASE over
just the surviving rows. No sequential scan, no per-row scoring of the whole
table.
"""

from sqlalchemy import ColumnElement, and_, case, func, literal, or_, select
from sqlalchemy.orm import aliased

from app.core.search_terms import (
    MIN_FUZZY_LENGTH,
    ROLE_ANY,
    ROLE_EMPLOYER,
    ROLE_PLACE,
    ParsedQuery,
    QuerySegment,
    expand,
)
from app.models.alumni import Alumni
from app.models.employment import CurrentEmployment, EmploymentHistory

# --- tuning -------------------------------------------------------------------

#: Whole-string trigram floor. Chosen against the real dev employer/city values:
#: every intended typo correction measured 0.50-1.00 ('delloite'->Deloitte 0.50,
#: 'fidelty'->Fidelity 0.55, 'microsft'->Microsoft 0.58, 'goldmanschs'->Goldman
#: Sachs 0.67, 'morganstanly'->Morgan Stanley 0.69, 'aresmanagment'->Ares
#: Management 0.71, 'charlesschwabb'->Charles Schwab 0.81) while the near-miss
#: junk sat at 0.33-0.40 ('blackrok'->Blackstone 0.33, 'delloite'->Dell 0.40).
#: 0.45 is the gap between those two populations, not a round number picked from
#: the air. Raising it to 0.5 would start dropping real corrections; lowering it
#: to 0.35 lets Blackstone answer a BlackRock search.
_SIMILARITY_FLOOR = 0.45

#: Word-extent floor, used for the "short query inside a long stored value" case.
#: Left at PostgreSQL's own default (``pg_trgm.word_similarity_threshold`` =
#: 0.6) so the ``<%`` index operator and this recheck agree exactly, and it
#: always ranks below the exact hit.
#:
#: It is the FLOOR ON THE FLOOR now, not the floor itself — see
#: ``_MIN_SHARED_TRIGRAMS``, which raises it for a short term. The noise this
#: constant was originally called tiny and sensible ('cite'->Citi,
#: 'dell'->Deloitte) turned out to be the #447 bug wearing a friendly face:
#: 'dell' pulled all ten dev Deloitte alumni into a search for Dell.
_WORD_SIMILARITY_FLOOR = 0.6

#: Fewest trigrams a word-similarity hit has to actually SHARE with the term
#: (#447). ``word_similarity`` is ``shared / (term_trigrams + extent - shared)``
#: and ``shared <= extent``, so a score of W over a term of N trigrams proves at
#: least ``W * N`` shared trigrams. Read backwards: demanding K shared trigrams
#: is the floor ``K / N`` — a floor that RISES as the term gets shorter, which is
#: precisely what a flat floor cannot do.
#:
#: WHY IT WAS NEEDED. A four-character term has five trigrams, so a flat 0.6 is
#: cleared by THREE shared trigrams — the term's first three letters and nothing
#: else. Three characters is the smallest overlap trigram matching can express at
#: all: that is a shared prefix, not a typo. Reproduced on dev with seven seeded
#: rows, ``q=Domo`` returned Dominguez, Dominic, Dominion Energy, Domain
#: Architect and an alumna in the Dominican Republic, every one of them scoring
#: exactly 0.600, none of them ever employed by Domo.
#:
#: WHY FIVE. It is the smallest K that rejects all of those (and 'chase'->
#: Chastain, 0.667) while keeping every correction the flat floor was tuned for,
#: measured on dev: 'fidelty'->fidelityinvestments 0.625 = 5/8, exactly its own
#: floor; 'vanguad'->vanguardgroup 0.750; 'microsft'->microsoftcorporation
#: 0.667; 'qualtrcs'->qualtricsinternational 0.667; 'blackstne'->blackstonegroup
#: 0.700; 'goldmanschs'->goldmansachs 0.667; 'morganstanly'->morganstanleyandco
#: 0.846; 'charlesschwabb'->charlesschwabcorporation 0.867. Terms of eight
#: characters or more are untouched — 5/9 is already below 0.6.
_MIN_SHARED_TRIGRAMS = 5

# Ranking tiers. The only invariant that matters: _FUZZY_CEILING (the most an
# approximate match can score) is strictly BELOW _CONTAINS_SCORE (the least a
# precise match can score), even after the field weights below are applied —
# 50 * 1.0 < 60 * 0.9. Exact and prefix matches therefore always outrank
# approximate ones.
_EXACT_SCORE = 100.0
_PREFIX_SCORE = 80.0
_CONTAINS_SCORE = 60.0
_FUZZY_CEILING = 50.0

# Field weights. A hit on the person's own name outranks the same quality of hit
# on their employer's, and a CURRENT employer outranks a former one.
_NAME_WEIGHT = 1.0
_RELATED_WEIGHT = 0.9
_HISTORY_WEIGHT = 0.7
#: A synonym/abbreviation hit ("GS" -> Goldman Sachs) scores below a hit on the
#: literal term the user typed.
_ALIAS_PENALTY = 0.85

#: Terms scored per segment (the phrase plus its first few tokens). A cap keeps
#: the ORDER BY expression bounded for a pasted-paragraph query.
_MAX_SCORED_TERMS = 4


# --- column groups ------------------------------------------------------------
#
# Which columns each routed role is allowed to match. This is the whole of the
# "is it a company or a place?" decision: the SENTENCE picks the group, and the
# REAL DATA in that group decides whether anything matches. There is no list of
# company names or city names — an employer the database has never seen behaves
# exactly like one it has.

_NAME_COLUMNS = (
    Alumni.first_name,
    Alumni.middle_name,
    Alumni.last_name,
    Alumni.preferred_first_name,
    Alumni.birth_name,
    # Free text that legitimately contains spaces ("Series 7"), kept in the
    # per-token set so both tokens can match the same cell (#404).
    Alumni.other_designations,
)

#: Only searched for a single-word query — atomic ids never contain a space, so
#: AND-ing them into a multi-word query could never match (#281).
_ID_COLUMNS = (Alumni.byu_id, Alumni.net_id)

_EMPLOYER_COLUMNS = (CurrentEmployment.current_employer,)
_TITLE_COLUMNS = (CurrentEmployment.current_title,)
_PLACE_COLUMNS = (
    CurrentEmployment.current_city,
    CurrentEmployment.current_state,
    CurrentEmployment.current_country,
    # "works in investment banking" uses the same preposition as "in new york",
    # so the place group covers the industry columns too. A city is never an
    # industry and vice versa, so this costs nothing in precision.
    CurrentEmployment.current_industry,
    CurrentEmployment.current_industry_secondary,
)

_HISTORY_COLUMNS = (EmploymentHistory.employer_name,)


def _employment_columns(role: str) -> tuple:
    if role == ROLE_EMPLOYER:
        return _EMPLOYER_COLUMNS
    if role == ROLE_PLACE:
        return _PLACE_COLUMNS
    return _EMPLOYER_COLUMNS + _TITLE_COLUMNS + _PLACE_COLUMNS


def _history_columns(role: str) -> tuple:
    # "at <company>" and an unrouted term also consider where someone USED to
    # work; a place-routed term does not (employment history has no country /
    # industry columns worth widening into here).
    return _HISTORY_COLUMNS if role in (ROLE_EMPLOYER, ROLE_ANY) else ()


def _name_columns(role: str) -> tuple:
    # Only an unrouted term searches the person's name. "at adobe" must not
    # match an alumna surnamed Adobe — that is the entire point of routing.
    return _NAME_COLUMNS if role == ROLE_ANY else ()


# --- primitives ---------------------------------------------------------------


def norm(column) -> ColumnElement[str]:
    """The normalized search form of ``column`` (SQL side).

    Must stay identical to the expression the GIN indexes were built on, or the
    planner silently drops to a sequential scan.
    """
    return func.alumni_search_norm(column)


def _matches(normalized, term: str):
    """Predicate: ``normalized`` matches ``term`` (or a known synonym of it).

    ``term`` is already normalized, so it cannot contain a LIKE metacharacter
    and needs no escaping — normalization deletes ``%`` and ``_`` outright.
    """
    spellings = expand(term)
    if len(spellings) > 1:
        return or_(*[_matches_one(normalized, s) for s in spellings])
    return _matches_one(normalized, term)


def _word_similarity_floor(term: str) -> float | None:
    """Word-extent floor for ``term``, or ``None`` when the leg cannot add a row.

    Never below ``_WORD_SIMILARITY_FLOOR`` (the ``<%`` session threshold), so the
    index probe stays a superset of whatever this returns.

    ``None`` once ``_MIN_SHARED_TRIGRAMS`` no longer fits inside the term: the
    only extent that could clear the floor is then the whole term, which the
    ``contains`` leg already matches, so emitting the leg would cost an index
    probe that can never contribute a row.
    """
    trigrams = len(term) + 1
    if trigrams <= _MIN_SHARED_TRIGRAMS:
        return None
    return max(_WORD_SIMILARITY_FLOOR, _MIN_SHARED_TRIGRAMS / trigrams)


def _matches_one(normalized, term: str):
    """Exact / prefix / contains / trigram-similar, for a single spelling."""
    legs = [normalized.like(f"%{term}%")]
    if len(term) >= MIN_FUZZY_LENGTH:
        legs.append(
            and_(
                # GIN-answerable pre-filter (session threshold, default 0.3)...
                normalized.op("%", is_comparison=True)(term),
                # ...narrowed to OUR floor, so behaviour never depends on a GUC.
                func.similarity(normalized, term) >= _SIMILARITY_FLOOR,
            )
        )
        word_floor = _word_similarity_floor(term)
        if word_floor is not None:
            legs.append(
                and_(
                    # `<%` wants the query on the LEFT and the indexed expression
                    # on the RIGHT for the index to be usable.
                    literal(term).op("<%", is_comparison=True)(normalized),
                    func.word_similarity(term, normalized) >= word_floor,
                )
            )
    return or_(*legs)


def _exists_over(model, columns: tuple, alumni_id_column, term: str):
    """Correlated EXISTS: some row of ``model`` for this alumnus matches ``term``."""
    return (
        select(literal(1))
        .where(
            alumni_id_column == Alumni.alumni_id,
            or_(*[_matches(norm(c), term) for c in columns]),
        )
        .exists()
    )


def _alumni_row_in(columns: tuple, term: str):
    """``alumni_id IN (SELECT ... WHERE <term matches one of these columns>)``.

    Written as an id-set subquery rather than as inline column predicates for a
    PLANNER reason, and it matters a lot. The free-text condition is one big OR
    across the alumni row AND two correlated EXISTS. PostgreSQL cannot combine an
    indexable column predicate with a subplan membership test in a single bitmap
    scan, so inlining the name predicates made the whole OR unindexable and
    forced a sequential scan of ``alumni`` that re-normalized eight columns per
    row — measured at ~2.3s over 50k rows.

    As a self-contained subquery the name legs become their own BitmapOr over the
    name trigram indexes, PostgreSQL hashes the resulting id set once, and the
    outer scan is a hash probe. Same rows, same semantics.
    """
    other = aliased(Alumni)
    return Alumni.alumni_id.in_(
        select(other.alumni_id).where(
            or_(*[_matches(norm(getattr(other, c.key)), term) for c in columns])
        )
    )


def _term_predicate(term: str, role: str, *, include_ids: bool, extra=None):
    """OR of every place ``term`` is allowed to match, for one routed role."""
    row_columns = _name_columns(role) + (_ID_COLUMNS if include_ids else ())
    legs = [_alumni_row_in(row_columns, term)] if row_columns else []
    # Caller-supplied extra meaning for a bare token (the CFP/CFA/CPA
    # holder-EXISTS, #404). OR-ed INTO this token's group so the token is
    # satisfied by holding the certification alone and never has to also match a
    # name column. Unrouted terms only — "at cfa" is not a certification claim.
    if extra is not None and role == ROLE_ANY:
        extra_leg = extra(term)
        if extra_leg is not None:
            legs.append(extra_leg)
    employment = _employment_columns(role)
    if employment:
        legs.append(
            _exists_over(
                CurrentEmployment, employment, CurrentEmployment.alumni_id, term
            )
        )
    history = _history_columns(role)
    if history:
        legs.append(
            _exists_over(
                EmploymentHistory, history, EmploymentHistory.alumni_id, term
            )
        )
    return or_(*legs)


def segment_predicate(segment: QuerySegment, *, include_ids: bool = False, extra=None):
    """Predicate for one segment: the whole phrase matched, OR every token matched.

    Two legs, OR-ed:

    * **phrase** — the segment's words with the spaces removed, matched against a
      single cell. This is what makes "goldman schs" reach "Goldman Sachs" (one
      cell, one typo) and "newyork" reach "New York" (one cell, one missing
      space).
    * **tokens** — every token must match SOMETHING, but not necessarily the same
      column: "Kyle Marsh" is satisfied by 'kyle' on ``first_name`` and 'marsh'
      on ``last_name`` (#281). This is the pre-existing semantic, preserved.

    For a one-word segment the two legs are identical, so only one is emitted.
    """
    token_legs = [
        _term_predicate(token, segment.role, include_ids=include_ids, extra=extra)
        for token in segment.tokens
    ]
    tokens_leg = and_(*token_legs) if len(token_legs) > 1 else token_legs[0]
    if len(segment.tokens) == 1:
        return tokens_leg
    phrase_leg = _term_predicate(
        segment.phrase, segment.role, include_ids=include_ids, extra=extra
    )
    return or_(phrase_leg, tokens_leg)


def q_conditions(parsed: ParsedQuery, *, extra=None) -> list:
    """The WHERE conditions for a parsed free-text query (segments AND-ed).

    Shared verbatim by ``GET /alumni`` and ``POST /alumni/export`` because both
    go through ``build_alumni_query`` — the list and the export can never
    describe different populations, which is the parity bug class this codebase
    keeps hitting. Ranking lives in :func:`relevance_expression` and is applied
    only by the list's ORDER BY, so it cannot affect the exported population.
    """
    return [
        segment_predicate(segment, include_ids=parsed.single_token, extra=extra)
        for segment in parsed.segments
    ]


# --- ranking ------------------------------------------------------------------


def _score(normalized, term: str, weight: float):
    """Tiered relevance for one column against one term and its synonyms.

    A synonym hit is scored at ``_ALIAS_PENALTY`` of a literal hit, so the thing
    the user actually typed always sorts above an expansion of it.
    """
    spellings = expand(term)
    if len(spellings) == 1:
        return _score_one(normalized, term, weight)
    return func.greatest(
        _score_one(normalized, term, weight),
        *[
            _score_one(normalized, s, weight * _ALIAS_PENALTY)
            for s in spellings[1:]
        ],
    )


def _score_one(normalized, term: str, weight: float):
    """Tiered relevance for one column against one exact spelling."""
    fuzzy: ColumnElement
    if len(term) >= MIN_FUZZY_LENGTH:
        fuzzy = _FUZZY_CEILING * func.greatest(
            func.similarity(normalized, term),
            func.word_similarity(term, normalized),
        )
    else:
        fuzzy = literal(0.0)
    return (
        case(
            (normalized == term, literal(_EXACT_SCORE)),
            (normalized.like(f"{term}%"), literal(_PREFIX_SCORE)),
            (normalized.like(f"%{term}%"), literal(_CONTAINS_SCORE)),
            else_=fuzzy,
        )
        * weight
    )


def _best(scores: list):
    if not scores:
        return literal(0.0)
    if len(scores) == 1:
        return scores[0]
    return func.greatest(*scores)


def _scored_terms(segment: QuerySegment) -> tuple[str, ...]:
    """Phrase plus tokens, deduplicated and capped."""
    terms: list[str] = [segment.phrase]
    for token in segment.tokens:
        if token not in terms:
            terms.append(token)
    return tuple(terms[:_MAX_SCORED_TERMS])


def _max_over(model, columns: tuple, alumni_id_column, terms, weight: float):
    """Correlated scalar subquery: this alumnus's best score on a related table."""
    return func.coalesce(
        select(
            func.max(
                _best([_score(norm(c), t, weight) for c in columns for t in terms])
            )
        )
        .where(alumni_id_column == Alumni.alumni_id)
        .correlate(Alumni)
        .scalar_subquery(),
        0.0,
    )


def _segment_score(segment: QuerySegment):
    terms = _scored_terms(segment)
    parts = [
        _best(
            [
                _score(norm(c), t, _NAME_WEIGHT)
                for c in _name_columns(segment.role)
                for t in terms
            ]
        )
    ]
    employment = _employment_columns(segment.role)
    if employment:
        parts.append(
            _max_over(
                CurrentEmployment,
                employment,
                CurrentEmployment.alumni_id,
                terms,
                _RELATED_WEIGHT,
            )
        )
    history = _history_columns(segment.role)
    if history:
        parts.append(
            _max_over(
                EmploymentHistory,
                history,
                EmploymentHistory.alumni_id,
                terms,
                _HISTORY_WEIGHT,
            )
        )
    return _best(parts)


def relevance_expression(parsed: ParsedQuery):
    """A per-row relevance score for a parsed query, or ``None`` when there is none.

    Segment scores are SUMMED, so a row that satisfies "at goldman sachs" *and*
    "in new york" outranks one that only just satisfies both.

    Used ONLY in the list's ORDER BY (never in a WHERE), and always followed by
    ``last_name, alumni_id`` tiebreakers so the order is a total order. Without
    that, two rows tied on score could swap between the OFFSET fetches of page 1
    and page 2 and the same alumnus would appear twice — or vanish (#183).
    """
    if not parsed.segments:
        return None
    total = _segment_score(parsed.segments[0])
    for segment in parsed.segments[1:]:
        total = total + _segment_score(segment)
    return total
