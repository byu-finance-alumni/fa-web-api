"""SQL helpers shared across repositories and routes.

``escape_like`` neutralizes LIKE/ILIKE metacharacters in user-supplied search
terms so a ``%`` or ``_`` in the input matches literally instead of acting as a
wildcard (LIKE wildcard injection). Pair it with ``ESCAPE '\\'`` on the
``.ilike(...)`` call so the backslash escapes are honored by PostgreSQL.
"""

import re

_LIKE_METACHARS = re.compile(r"([%_\\])")


def escape_like(value: str) -> str:
    """Escape LIKE/ILIKE metacharacters in a user-supplied term.

    Escapes ``%``, ``_`` and the escape character ``\\`` itself by prefixing each
    with a backslash. The pattern must then be used with ``ESCAPE '\\'`` so the
    database treats these as literals rather than wildcards.
    """
    return _LIKE_METACHARS.sub(r"\\\1", value)
