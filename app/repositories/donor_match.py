"""Batch donor -> alumni resolution for the donations importer (#148).

Donation records are keyed on **MSTID** (the institutional master ID), not Net
ID. This resolves a batch of donor rows to active ``alumni_id``s by:

  * **MSTID** (primary) — trimmed + case-insensitive, active-only;
  * **last + first name** (fallback) — used when a row has no MSTID, or its MSTID
    doesn't resolve.

Both matchers return a **list** of matching ids per key (not a single id) so the
importer can tell apart the three outcomes that matter for strict integrity:
zero matches (unmatched), exactly one (safe to attribute), and more than one
(**ambiguous** — surfaced for a human to disambiguate rather than silently
mis-attributing a donation). Archived alumni are never matched.
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alumni import Alumni


def normalize_mstid(raw: str | None) -> str:
    """Canonical match key for an MSTID: trimmed + lowercased ("" if blank)."""
    return (raw or "").strip().lower()


def normalize_name_key(last: str | None, first: str | None) -> tuple[str, str]:
    """Canonical (last, first) match key: each trimmed + lowercased."""
    return ((last or "").strip().lower(), (first or "").strip().lower())


async def match_mstids(
    session: AsyncSession, mstids: list[str]
) -> dict[str, list[int]]:
    """Resolve a batch of MSTIDs to active ``alumni_id``s.

    Returns a map keyed by the NORMALIZED MSTID; the value is the list of active
    alumni sharing it (normally one — ``mst_id`` has no unique index, so a list
    lets the caller flag the rare ambiguous case). Blank inputs are ignored.
    """
    keys = {normalize_mstid(m) for m in mstids}
    keys.discard("")
    if not keys:
        return {}

    rows = (
        await session.execute(
            select(Alumni.alumni_id, Alumni.mst_id).where(
                Alumni.archived.is_(False),
                Alumni.mst_id.isnot(None),
                func.lower(func.trim(Alumni.mst_id)).in_(keys),
            )
        )
    ).all()

    matched: dict[str, list[int]] = defaultdict(list)
    for alumni_id, mst_id in rows:
        matched[normalize_mstid(mst_id)].append(alumni_id)
    return dict(matched)


async def match_names(
    session: AsyncSession, name_keys: list[tuple[str | None, str | None]]
) -> dict[tuple[str, str], list[int]]:
    """Resolve a batch of (last, first) name pairs to active ``alumni_id``s.

    Returns a map keyed by the NORMALIZED (last, first) tuple; the value is the
    list of active alumni with that exact legal first + last name. A name with
    more than one match is ambiguous and must not be auto-attributed. Only pairs
    with BOTH names non-blank are resolvable (a fallback needs both halves).
    """
    keys = {normalize_name_key(last, first) for last, first in name_keys}
    keys = {k for k in keys if k[0] and k[1]}
    if not keys:
        return {}

    lasts = {k[0] for k in keys}
    firsts = {k[1] for k in keys}
    rows = (
        await session.execute(
            select(Alumni.alumni_id, Alumni.last_name, Alumni.first_name).where(
                Alumni.archived.is_(False),
                Alumni.last_name.isnot(None),
                Alumni.first_name.isnot(None),
                func.lower(func.trim(Alumni.last_name)).in_(lasts),
                func.lower(func.trim(Alumni.first_name)).in_(firsts),
            )
        )
    ).all()

    matched: dict[tuple[str, str], list[int]] = defaultdict(list)
    for alumni_id, last_name, first_name in rows:
        key = normalize_name_key(last_name, first_name)
        # The cross-product query can return a (last, first) combo that wasn't
        # asked for (last of one request, first of another); keep only requested.
        if key in keys:
            matched[key].append(alumni_id)
    return dict(matched)
