"""Batch Net ID -> alumni resolution, shared by the bulk CSV importers.

Both the events importer (#156) and the donations importer (#161) key their
uploaded rows on **Net ID** — the human-readable name column is for confirmation
only. Matching is:

  * **case-insensitive** — a stored ``net_id`` of ``"jdoe"`` matches an incoming
    ``"JDOE"``;
  * **active-only** — archived alumni are never matched (they are not valid
    attendees/donors), mirroring the duplicate-detection index in
    ``import_csv._load_existing_index``;
  * **batched** — every distinct Net ID across the file is resolved in ONE query
    so a large import doesn't fan out into per-row round-trips.

An ambiguous Net ID (more than one active alumnus sharing it) cannot happen for
non-archived rows — the partial-unique index ``uq_alumni_net_id_active`` forbids
it — so the map keeps the first id deterministically and callers treat a hit as
unambiguous.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alumni import Alumni


def normalize_net_id(raw: str | None) -> str:
    """Canonical match key for a Net ID: trimmed + lowercased ("" if blank)."""
    return (raw or "").strip().lower()


async def match_net_ids(
    session: AsyncSession, net_ids: list[str]
) -> dict[str, int]:
    """Resolve a batch of Net IDs to active ``alumni_id``s.

    Returns a map keyed by the NORMALIZED Net ID (trimmed + lowercased) for every
    *net_id* that matches a non-archived alumnus. Blank inputs are ignored; a Net
    ID with no active match is simply absent from the result, so callers can
    detect "unmatched" with ``normalize_net_id(x) not in result``.
    """
    keys = {normalize_net_id(n) for n in net_ids}
    keys.discard("")
    if not keys:
        return {}

    rows = (
        await session.execute(
            select(Alumni.alumni_id, Alumni.net_id).where(
                Alumni.archived.is_(False),
                Alumni.net_id.isnot(None),
                func.lower(func.trim(Alumni.net_id)).in_(keys),
            )
        )
    ).all()

    matched: dict[str, int] = {}
    for alumni_id, net_id in rows:
        matched.setdefault(normalize_net_id(net_id), alumni_id)
    return matched
