"""The visible identifier of a friend-of-the-program record (#538).

Friend records (``alumni.is_alumni = false``) carry no Net ID and no BYU ID, so
staff had no stable handle to quote, search or put in a spreadsheet the way a
Net ID works for alumni. Jake's decision (2026-09-16): every friend gets a
visible id **derived from the primary key** -- ``FRIEND-00042`` for
``alumni_id = 42`` -- so nothing is minted, nothing is stored, and no migration
is needed. It is shown on the profile, the list and the CSV export, and the
alumni search accepts it.

This module is the ONE place the format lives. Everything that shows or parses
a friend id goes through :func:`friend_id_for` / :func:`parse_friend_id`; do not
re-derive the string anywhere else.

Why derive rather than store: the primary key is already unique, immutable and
never reused, which is exactly what an identifier needs. A stored column would
buy nothing except a migration (and migrations change the promotion order --
the migrate job trails Vercel by minutes). The zero-padding is cosmetic: ids
past 99999 simply grow a digit (``FRIEND-123456``) and still parse.
"""

from __future__ import annotations

import re

PREFIX = "FRIEND-"
_WIDTH = 5
_PATTERN = re.compile(r"^\s*friend[\s\-_]*0*(\d{1,12})\s*$", re.IGNORECASE)


def friend_id_for(alumni_id: int | None, is_alumni: bool | None) -> str | None:
    """The visible id for a record, or ``None`` when it is not a friend.

    ``None`` for every alumnus (``is_alumni`` true or unknown) so the field can
    ride on the shared alumni read schemas without misleading anyone: only a
    friend row ever has a friend id.
    """
    if alumni_id is None or is_alumni is None or is_alumni:
        return None
    return f"{PREFIX}{int(alumni_id):0{_WIDTH}d}"


def parse_friend_id(value: object) -> int | None:
    """The ``alumni_id`` a typed friend id names, or ``None`` if it is not one.

    Lenient on purpose -- it is what the search box feeds: case-insensitive,
    surrounding whitespace ignored, the hyphen optional (``friend42``,
    ``FRIEND-00042`` and ``Friend 42`` all name record 42). Anything that is
    not clearly a friend id is ``None`` so the caller falls through to the
    ordinary search.
    """
    if not isinstance(value, str):
        return None
    match = _PATTERN.match(value)
    if match is None:
        return None
    number = int(match.group(1))
    return number if number > 0 else None
