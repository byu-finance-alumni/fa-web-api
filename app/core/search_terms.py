"""Free-text alumni search: turning a typed sentence into search segments (#620).

Jake types sentences, not query syntax::

    "im looking for all alumni at goldman schs in new york"
    "i am looking for jake in newyork"
    "alumni in lehi at adobe"

Three things have to happen before any SQL can be built, and all three are pure
text work with no database access — which is why they live here rather than in
the repository. ``build_alumni_query`` must stay a pure function (the export
path compiles it without a session), so the parse can never depend on a lookup.

1. **Filler removal.** Every token used to become a REQUIRED match, so
   "im looking for jake" matched nobody while "jake" matched one person. The
   contentless openers are dropped.

2. **Preposition routing.** The prepositions are NOT filler — they are the
   strongest signal in the sentence about what the NEXT words mean:

       "... **at** goldman schs ..."  -> goldman schs is an EMPLOYER
       "... **in** new york ..."      -> new york is a PLACE

   Jake, 2026-08-04: typing "alumni in lehi at adobe" filtered *adobe* as the
   city. Routing is what makes "at adobe" an employer and "in lehi" a place.
   Routing is decided by the SENTENCE, then verified against the REAL DATA in
   SQL — there is deliberately no list of company names or city names anywhere
   in this module. Whatever the employer column actually contains is what an
   "at ..." segment matches; a company nobody has heard of behaves exactly like
   Adobe.

3. **Normalization.** Everything is reduced to a case-folded, accent-folded,
   punctuation-free form so "newyork" == "New York" and "jpmorgan" == "J.P.
   Morgan". :func:`normalize` is the Python twin of the ``alumni_search_norm``
   SQL function added in ``database/migrations/2026-08-05_fuzzy_alumni_search.sql``
   — the two MUST agree or a search silently stops matching.

Nothing here decides *whether* a token is really a place or really a company.
That question is answered by the data, in SQL: a routed segment simply restricts
which columns it is allowed to match, and an UNROUTED segment (no preposition in
front of it) stays broad and searches every field. Absence of a hint never
excludes anything.
"""

import re
import unicodedata
from dataclasses import dataclass

from app.core.dropdowns import INDUSTRIES
from app.core.us_states import STATE_NAME_BY_CODE

# --- normalization ------------------------------------------------------------

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize(text: str) -> str:
    """Case-, accent- and punctuation-insensitive search form of ``text``.

    Python twin of the SQL ``alumni_search_norm(text)``. Decompose to NFKD, drop
    the combining marks ("São" -> "Sao"), lower-case, then delete every
    character that is not ``[a-z0-9]``.

    Dropping the separators entirely — rather than collapsing them to a single
    space — is what buys the spacing tolerance Jake asked for: "New York",
    "new-york" and "newyork" all normalize to ``newyork``, so a missing space
    can no longer be a missed match. It also means a normalized term can never
    contain a LIKE metacharacter, so patterns built from it need no escaping.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    folded = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _NON_ALNUM.sub("", folded.lower())


# --- routing prepositions -----------------------------------------------------
#
# Deliberately tiny. A preposition only ever NARROWS the following words to a
# field group; it is never treated as a search term itself.

#: "at goldman sachs", "@ adobe" -> the words that follow name an EMPLOYER.
EMPLOYER_PREPOSITIONS = frozenset({"at"})

#: "in new york", "from provo", "near seattle" -> the words that follow name a
#: PLACE. ``in`` is also how people say "works in investment banking", so the
#: place group additionally covers the industry columns (see
#: ``repositories.alumni_search``): "lehi" is not an industry and "investment
#: banking" is not a city, so covering both costs nothing and reads naturally.
PLACE_PREPOSITIONS = frozenset({"in", "from", "near", "around", "outside"})

_PREPOSITIONS = EMPLOYER_PREPOSITIONS | PLACE_PREPOSITIONS

# --- filler ------------------------------------------------------------------
#
# CONSERVATIVE BY CONSTRUCTION. Only closed-class function words, the handful of
# verbs people open a search with, and the domain nouns that describe the
# PERSON rather than anything stored on their record. Nothing here is a
# plausible surname, employer, city or industry on its own.
#
# Words deliberately LEFT OUT because they are real names or real values:
#   may / will / can / grant / frank / best  -> given names & surnames
#   still / just / see / list-as-a-name      -> surnames
#   other                                    -> a stored industry value ("Other")
#   graduate / student / students            -> a stored industry value
#                                               ("Graduate Student", #294)
#   new / lake / city / saint / fort / north -> city-name words
#   in / at / from / near                    -> ROUTING, not noise (see above)
#
# Why over-stripping is survivable but under-stripping is not: tokens are
# AND-ed, so removing a token can only ever WIDEN the result set. The record you
# were looking for is still in it, and relevance ranking floats it to the top.
# A filler word left IN, by contrast, is a required match that nothing satisfies
# — which is exactly how "im looking for jake" returned zero rows. The one case
# where stripping could genuinely hurt is a query made ENTIRELY of filler, and
# that falls back to the raw tokens (see :func:`parse_free_text`).
_FILLER = frozenset(
    {
        # First person / articles / demonstratives.
        "i", "im", "id", "ive", "am", "me", "my", "mine", "we", "us", "our",
        "you", "your", "they", "them", "their",
        "a", "an", "the", "this", "that", "these", "those", "there", "here",
        # Copulas / auxiliaries.
        "is", "are", "was", "were", "be", "been", "being", "do", "does", "did",
        # Conjunctions / particles.
        "and", "or", "of", "to", "for", "with", "please",
        # Quantifiers.
        "all", "any", "every", "some",
        # The verbs a search sentence opens with.
        "find", "show", "search", "searching", "look", "looking", "get",
        "pull", "display",
        # Nouns for the person, never for anything stored on their record.
        "alumni", "alum", "alums", "alumnus", "alumna", "alumnae",
        "people", "person", "someone", "anyone", "everyone", "folks",
        # Relatives.
        "who", "whom", "whose", "which",
        # Employment / residence verbs ("who works at", "living in").
        "works", "work", "working", "worked", "employed",
        "lives", "live", "living", "based", "located", "currently",
        # Possession ("holds a CPA").
        "holds", "hold", "has", "have",
        # Leftovers from the phrasings Jake listed ("pull up", "give me",
        # "do we have").
        "up", "give",
        # "anyone called X" / "alumni named X" -- the verb, not the name.
        "named", "called",
        # "active" (Jake, 2026-08-04): "people will search active but everything
        # is active, so in the search don't let it affect the search." Dropped as
        # a WHOLE WORD only -- "inactive", "retired", "deceased", "lost contact"
        # and "do not contact" are real statuses and are untouched by this.
        "active",
    }
)


# --- synonyms and abbreviations ----------------------------------------------
#
# THE tuning knob. Jake's examples use abbreviations as if they were the real
# thing ("GS", "JPM", "IB", "NYC", "Big Four", "MD"), so a typed abbreviation is
# expanded into the value the database actually stores and the two are OR-ed.
#
# Expansion only ever WIDENS: the typed term is always searched as well, and the
# ranking still puts an exact/prefix hit on the literal term above an expansion
# hit — so "MS" finds Morgan Stanley without burying an alumna whose initials
# are MS.
#
# Keys and values are already NORMALIZED (lower case, no punctuation), because
# that is the form both sides of every comparison are reduced to.
#
# Edit this table freely — it is meant to be adjusted once real searches are
# seen. Nothing else in the search depends on its contents.
_ABBREVIATIONS: dict[str, tuple[str, ...]] = {
    # Employers.
    "gs": ("goldmansachs",),
    "jpm": ("jpmorgan", "jpmorganchase"),
    "bofa": ("bankofamerica",),
    "ms": ("morganstanley",),
    "bb": ("bulgebracket",),
    # "Big Four" is a category people type, not a stored value.
    "bigfour": ("deloitte", "ey", "ernstyoung", "kpmg", "pwc", "pricewaterhousecoopers"),
    "big4": ("deloitte", "ey", "ernstyoung", "kpmg", "pwc", "pricewaterhousecoopers"),
    "pwc": ("pricewaterhousecoopers",),
    "ey": ("ernstyoung",),
    # Industries.
    "ib": ("investmentbanking",),
    "pe": ("privateequity",),
    "vc": ("venturecapital",),
    "wm": ("wealthmanagement",),
    "am": ("assetmanagement",),
    "cre": ("commercialrealestate", "realestate"),
    "fintech": ("financialtechnology", "technology"),
    "fpa": ("financialplanninganalysis", "corporatefinance"),
    # Titles.
    "ceo": ("chiefexecutiveofficer",),
    "cfo": ("chieffinancialofficer",),
    "coo": ("chiefoperatingofficer",),
    "cto": ("chieftechnologyofficer",),
    "vp": ("vicepresident",),
    "svp": ("seniorvicepresident",),
    "evp": ("executivevicepresident",),
    "md": ("managingdirector",),
    # Metros. City names are stored plainly, so only the ones that are NOT a
    # prefix of the stored value need spelling out.
    "nyc": ("newyork",),
    "slc": ("saltlakecity",),
    "sf": ("sanfrancisco",),
    "sfo": ("sanfrancisco",),
    "la": ("losangeles",),
    "dc": ("washington", "districtofcolumbia"),
    "dfw": ("dallas", "fortworth"),
}

# NOTE: ``am`` is also the copula in "i am looking for ...", and filler removal
# runs FIRST, so the asset-management expansion can never fire from that
# phrasing. That is the intended precedence — Jake's own sentence was "i am
# looking for jake in newyork".

# Industries the "finance" umbrella deliberately leaves OUT (assumption A7).
# Everything else in the canonical vocabulary is finance. Kept as the EXCLUSION
# list, derived from ``dropdowns.INDUSTRIES``, so a new industry added to the
# vocabulary joins the umbrella automatically instead of being silently missed.
#
# "Sales" is excluded but "Sales and Trading" is NOT — the first is a function,
# the second is a finance desk. "Other" / "Unknown" / "Graduate Student" are not
# industries at all.
_NOT_FINANCE = frozenset(
    {
        "Law",
        "Real Estate",
        "Consulting",
        "Sales",
        "Other",
        "Unknown",
        "Graduate Student",
    }
)

#: Industry umbrella terms -> the specific stored industries they cover.
#:
#: Jake, 2026-08-04: a bare "banking" search must return commercial banking,
#: investment banking AND corporate banking, while "investment banking" stays
#: precise and returns only that one. Because matching is substring-based on the
#: normalized form, "investmentbanking" can never be widened by this rule — only
#: the bare umbrella term is a key here. That asymmetry is the whole design:
#: broad word broad, specific word specific.
#:
#: Corporate Banking, Credit Risk, Law and Sales and Trading are all
#: PRIMARY-EXCLUDED in the vocabulary — they can ONLY be stored as a SECONDARY
#: industry. These groupings are therefore only meaningful because the
#: place/industry column group covers ``current_industry_secondary`` as well as
#: ``current_industry``; matching only the primary column would silently drop
#: every corporate banker from a "banking" search.
#:
#: A7 (finance) and A10 (credit / trading) live here alongside banking. Same as
#: the abbreviation table above: this exists to be edited when Jake sees real
#: results — a one-line change, no query-logic surgery.
INDUSTRY_GROUPS: dict[str, tuple[str, ...]] = {
    # Jake's ruling, 2026-08-04.
    "banking": ("investmentbanking", "commercialbanking", "corporatebanking"),
    # A7: "finance" = every canonical industry except Law, Real Estate,
    # Consulting and Sales.
    "finance": tuple(
        sorted(normalize(i) for i in INDUSTRIES if i not in _NOT_FINANCE)
    ),
    # A10. Both of these ALSO fall out of plain substring matching on the
    # normalized form ("credit" is contained in "privatecredit"); they are
    # written down so the rule is visible and adjustable rather than emergent.
    "credit": ("privatecredit", "creditrisk"),
    "trading": ("salesandtrading",),
}

_EXPANSIONS: dict[str, tuple[str, ...]] = {**_ABBREVIATIONS, **INDUSTRY_GROUPS}

# Two-letter USPS codes -> the stored spelling. ``current_state`` stores FULL
# names ("Arizona"), so "in az" would otherwise match nothing. Derived from the
# canonical state table rather than retyped, so it cannot drift.
for _code, _name in STATE_NAME_BY_CODE.items():
    _EXPANSIONS.setdefault(normalize(_code), ())
    _EXPANSIONS[normalize(_code)] = _EXPANSIONS[normalize(_code)] + (
        normalize(_name),
    )


def expand(term: str) -> tuple[str, ...]:
    """The term itself plus any synonym/abbreviation spellings, OR-ed by callers.

    Always includes ``term`` first, so an expansion can only widen a search and
    the literal term is what the ranking prefers.
    """
    return (term, *_EXPANSIONS.get(term, ()))

# --- limits -------------------------------------------------------------------

#: Most content tokens honoured across the whole query. A pasted paragraph must
#: not fan out into an unbounded AND-chain of trigram lookups.
MAX_TOKENS = 8
#: Most segments honoured. Four covers "<name> at <employer> in <city>" with room
#: to spare.
MAX_SEGMENTS = 4
#: Shortest normalized term the FUZZY legs are allowed to run on. Below four
#: characters a trigram set is mostly padding and "matches" become noise; short
#: terms still get the exact / prefix / contains legs, i.e. the old behaviour.
MIN_FUZZY_LENGTH = 4

_SPLIT = re.compile(r"[\s,;/|]+")

#: Roles a segment can carry.
ROLE_ANY = "any"
ROLE_EMPLOYER = "employer"
ROLE_PLACE = "place"


@dataclass(frozen=True)
class QuerySegment:
    """One routed run of words from the typed query.

    ``role`` says which field group the segment may match (``any`` = every
    searchable field). ``tokens`` are the individually normalized words and
    ``phrase`` is their normalized concatenation — "new york" -> tokens
    ``("new", "york")`` and phrase ``"newyork"``. Both are needed: the phrase is
    what makes a missing space harmless and what a whole-name typo is measured
    against; the tokens are what lets "Kyle Marsh" match a first name in one
    column and a surname in another.
    """

    role: str
    tokens: tuple[str, ...]
    phrase: str


@dataclass(frozen=True)
class ParsedQuery:
    """The parse of a free-text ``q``."""

    segments: tuple[QuerySegment, ...]
    #: True when the whole query was a single unrouted word. Only then do the
    #: external-id columns (byu_id / net_id) join the search: they are atomic
    #: values that never contain a space, so AND-ing them into a multi-word
    #: query could never match (#281).
    single_token: bool

    def __bool__(self) -> bool:
        return bool(self.segments)

    @property
    def all_tokens(self) -> tuple[str, ...]:
        return tuple(t for s in self.segments for t in s.tokens)


def _words(q: str) -> list[str]:
    """Split on whitespace and separators, normalizing each word.

    ``@`` is spelled out as the ``at`` preposition first, so "@goldman" and
    "at goldman" route the same way. Words that normalize to nothing (bare
    punctuation) are dropped.
    """
    text = q.replace("@", " at ")
    return [w for w in (normalize(part) for part in _SPLIT.split(text)) if w]


def _segment(words: list[str]) -> list[QuerySegment]:
    """Group ``words`` into segments, starting a new one at each preposition."""
    segments: list[QuerySegment] = []
    role = ROLE_ANY
    current: list[str] = []

    def flush() -> None:
        if current:
            segments.append(
                QuerySegment(role=role, tokens=tuple(current), phrase="".join(current))
            )
        current.clear()

    for word in words:
        if word in _PREPOSITIONS:
            flush()
            role = (
                ROLE_EMPLOYER if word in EMPLOYER_PREPOSITIONS else ROLE_PLACE
            )
            continue
        current.append(word)
    flush()
    return segments


def _truncate(segments: list[QuerySegment]) -> tuple[QuerySegment, ...]:
    """Apply the segment and total-token caps, in order."""
    kept: list[QuerySegment] = []
    budget = MAX_TOKENS
    for segment in segments[:MAX_SEGMENTS]:
        if budget <= 0:
            break
        tokens = segment.tokens[:budget]
        budget -= len(tokens)
        kept.append(
            QuerySegment(role=segment.role, tokens=tokens, phrase="".join(tokens))
        )
    return tuple(kept)


def parse_free_text(q: str | None) -> ParsedQuery:
    """Parse a typed query into routed, normalized segments.

    Returns an empty parse (falsy) when there is nothing searchable at all.

    The filler pass runs first and the routing pass second, so multi-word hints
    fall out for free: "who works at" collapses to "at" once ``who`` and
    ``works`` are dropped, and "based in" / "living in" collapse to "in".

    **Fallback:** if stripping filler would leave no searchable word — the query
    was nothing BUT filler, e.g. "show me everyone" — the raw words are used
    instead. Returning every alumnus because the sentence happened to be made of
    common words would be far worse than returning the (empty) result the raw
    words produce.
    """
    if not q or not q.strip():
        return ParsedQuery(segments=(), single_token=False)

    raw_words = _words(q)
    if not raw_words:
        return ParsedQuery(segments=(), single_token=False)

    content = [w for w in raw_words if w not in _FILLER]
    # Prepositions are not content; a query of only filler + prepositions has
    # nothing to search for, so fall back to the words as typed.
    if not any(w not in _PREPOSITIONS for w in content):
        content = raw_words

    segments = _truncate(_segment(content))
    if not segments:
        # Everything was a preposition (e.g. "in at"). Treat the raw words as
        # one broad segment rather than dropping the filter entirely.
        segments = _truncate(
            [
                QuerySegment(
                    role=ROLE_ANY,
                    tokens=tuple(raw_words),
                    phrase="".join(raw_words),
                )
            ]
        )

    single_token = (
        len(segments) == 1
        and segments[0].role == ROLE_ANY
        and len(segments[0].tokens) == 1
    )
    return ParsedQuery(segments=segments, single_token=single_token)
