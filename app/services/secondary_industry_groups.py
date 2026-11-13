"""Secondary-industry GROUPS — the one place a stored secondary industry is folded
into the row it is counted under (fa-web-app#936).

The dashboard's "Other" bar opens a table of the secondary industries the people
in it hold, and every row of that table links to the alumni list (and its CSV
export) of exactly the people it counted. Both halves — the counts
(``GET /dashboard/other-industries``) and the list/export filter
(``secondary_industry_group`` / ``no_secondary_industry``) — go through
:class:`SecondaryIndustryGrouper` built from the SAME set of stored values, so a
row's count and the list it opens can never describe different people.

``current_industry_secondary`` is free text, so one industry is often written
several ways. Jake's rule (2026-11-11) — group by NAME, nothing fuzzier:

1. **Case and spacing** are ignored: "corporate  banking " == "Corporate Banking".
2. **"Healthcare" prefix**: any value that STARTS with "Healthcare" (case-
   insensitive, leading spaces ignored) is the Healthcare row — "Healthcare -
   Biotech", "Healthcare Services", "HealthcareTech". See
   :data:`PREFIX_GROUPS`. "Health Care" (with a space) is NOT folded in — that
   merge was left for Jake to confirm.
3. **Name + extra detail**: detail after a separator is dropped — a spaced dash
   (" - ", " – ", " — "), a colon, a comma or an opening parenthesis — so "Law
   (IP)" and "Law: Litigation" are Law. A value that is an existing name followed
   by more WORDS ("Real Estate Development" when "Real Estate" is a known name)
   folds into that name. Known names are the industry vocabulary plus every
   value actually stored (after rule 3's separator strip). A vocabulary term
   never folds into another name — "Sales and Trading" stays its own row even
   though "Sales" is also a term.

The literal "Other" is just another name (its own row), a blank / whitespace-only
value is "no secondary industry", and a value nobody has used before simply
becomes a new row — nothing here lists the rows themselves.

The stored values are never rewritten; this is grouping only. The list filter
matches the EXACT stored strings that fold into the requested group (an ``IN``
over raw values), so it is byte-for-byte the set the counts grouped.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable

from sqlalchemy import false, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dropdowns import SECONDARY_INDUSTRIES
from app.models.alumni import Alumni
from app.models.employment import CurrentEmployment

#: Names that swallow EVERY value starting with them, word boundary or not
#: (Jake, 2026-11-11: "anything that starts with Healthcare is Healthcare").
PREFIX_GROUPS: tuple[str, ...] = ("Healthcare",)

#: Display label for the row of people with no secondary industry.
NO_SECONDARY_LABEL = "No secondary industry"

_WS = re.compile(r"\s+")
# Where "extra detail" starts: a dash with whitespace on at least one side (so a
# hyphenated word like "Non-profit" is left whole), a colon, a comma or "(".
_DETAIL = re.compile(r"\s[-–—]|[-–—]\s|[:,(]")


def clean(raw: str | None) -> str:
    """Trim and collapse internal whitespace. ``""`` for None / blank."""
    return _WS.sub(" ", raw or "").strip()


def _key(text: str) -> str:
    return clean(text).casefold()


_VOCAB_BY_KEY: dict[str, str] = {_key(v): v for v in SECONDARY_INDUSTRIES}
_PREFIX_BY_KEY: dict[str, str] = {_key(p): p for p in PREFIX_GROUPS}


def _base_name(raw: str | None) -> str | None:
    """The value's name before any data-dependent folding (rules 1-3a)."""
    text = clean(raw)
    if not text:
        return None
    folded = text.casefold()
    for key, name in _PREFIX_BY_KEY.items():
        if folded.startswith(key):
            return name
    head = _DETAIL.split(text, maxsplit=1)[0].strip()
    # A value that is ALL detail ("(none)", "- Biotech") keeps its full text
    # rather than collapsing to an empty name.
    return head or text


class SecondaryIndustryGrouper:
    """Maps stored secondary-industry values to group keys.

    Built from the set of distinct stored values; :meth:`group_key` is the only
    thing either the counts or the filter ask of it.
    """

    def __init__(self, stored_values: Iterable[str | None]) -> None:
        names = {_base_name(v) for v in stored_values}
        self._known: set[str] = (
            {_key(n) for n in names if n}
            | set(_VOCAB_BY_KEY)
            | set(_PREFIX_BY_KEY)
        )

    def _resolve(self, key: str) -> str:
        if key in _VOCAB_BY_KEY or key in _PREFIX_BY_KEY:
            return key
        words = key.split(" ")
        # Longest known leading run of whole words first, so "Private Equity
        # Real Estate" lands on "Private Equity", not on a shorter name.
        for n in range(len(words) - 1, 0, -1):
            candidate = " ".join(words[:n])
            if candidate in self._known:
                return self._resolve(candidate)
        return key

    def group_key(self, raw: str | None) -> str | None:
        """The group *raw* counts under; ``None`` means no secondary industry."""
        base = _base_name(raw)
        if base is None:
            return None
        return self._resolve(_key(base))

    def display_names(
        self, counts: dict[str | None, int], stored_values: Iterable[str | None] = ()
    ) -> dict[str, str]:
        """A clean display name per group key, given raw value -> count.

        The vocabulary spelling (or the :data:`PREFIX_GROUPS` spelling) wins;
        otherwise the most common way people wrote the bare name. *stored_values*
        (weight 0) supply a bare spelling when every counted member carried
        extra detail.
        """
        spellings: dict[str, Counter[str]] = {}
        weighted = [(raw, 0) for raw in stored_values] + list(counts.items())
        for raw, n in weighted:
            group = self.group_key(raw)
            if group is None:
                continue
            base = _base_name(raw) or ""
            bucket = spellings.setdefault(group, Counter())
            if _key(base) == group:
                bucket[base] += n
        out: dict[str, str] = {}
        for group, bucket in spellings.items():
            if group in _VOCAB_BY_KEY:
                out[group] = _VOCAB_BY_KEY[group]
            elif group in _PREFIX_BY_KEY:
                out[group] = _PREFIX_BY_KEY[group]
            elif bucket:
                # Most used; then a capitalised spelling over an all-lower one;
                # then alphabetical, so the label is stable across requests.
                out[group] = min(bucket, key=lambda s: (-bucket[s], s.islower(), s))
            else:
                # Every member is a longer variant of a name only some OTHER
                # alumnus's value supplied — fall back to the key, title-cased.
                out[group] = group.title()
        return out


async def stored_secondary_values(session: AsyncSession) -> list[str]:
    """Every distinct non-NULL ``current_industry_secondary`` on file."""
    rows = await session.execute(
        select(CurrentEmployment.current_industry_secondary)
        .where(CurrentEmployment.current_industry_secondary.is_not(None))
        .distinct()
    )
    return [r[0] for r in rows.all()]


def group_filter(
    stored_values: Iterable[str | None],
    groups: Iterable[str] | None,
    no_secondary: bool,
):
    """Alumni-level predicate for the requested groups (OR-ed), or ``None``.

    * ``groups`` — names as shown in the table (or any spelling of one: each is
      folded with the same :meth:`SecondaryIndustryGrouper.group_key`). Matches
      alumni whose current secondary is one of the stored values in the group.
    * ``no_secondary`` — alumni with NO current secondary that falls in any
      group (NULL, blank or whitespace-only), i.e. NOT EXISTS a non-blank one.

    A requested group nobody is in matches NOBODY — never "no predicate".
    """
    wanted_names = [g for g in (groups or []) if g is not None]
    if not wanted_names and not no_secondary:
        return None
    values = [v for v in stored_values if v is not None]
    grouper = SecondaryIndustryGrouper(values)
    parts = []
    if wanted_names:
        wanted = {grouper.group_key(g) for g in wanted_names} - {None}
        raws = sorted(v for v in values if grouper.group_key(v) in wanted)
        parts.append(
            select(CurrentEmployment.current_employment_id)
            .where(
                CurrentEmployment.alumni_id == Alumni.alumni_id,
                CurrentEmployment.current_industry_secondary.in_(raws),
            )
            .exists()
            if raws
            else false()
        )
    if no_secondary:
        named = sorted(v for v in values if grouper.group_key(v) is not None)
        if named:
            parts.append(
                ~select(CurrentEmployment.current_employment_id)
                .where(
                    CurrentEmployment.alumni_id == Alumni.alumni_id,
                    CurrentEmployment.current_industry_secondary.in_(named),
                )
                .exists()
            )
        else:
            # Nobody holds a secondary at all: everyone qualifies.
            parts.append(true())
    return parts[0] if len(parts) == 1 else or_(*parts)


async def resolve_group_filter(
    session: AsyncSession, groups: Iterable[str] | None, no_secondary: bool
):
    """:func:`group_filter` over the values stored right now. The list route and
    the export both call this, so they resolve a group to the same raw values.
    No query is made when neither filter is set."""
    groups = [g for g in (groups or []) if g is not None]
    if not groups and not no_secondary:
        return None
    return group_filter(await stored_secondary_values(session), groups, no_secondary)
