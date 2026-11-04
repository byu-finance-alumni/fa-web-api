"""``format_phone`` must agree with the app's ``formatPhone`` (fa-web-app#924):
the CSVs and the Outreach tables show the same number.

⚠️ The same table is pinned in fa-web-app ``src/lib/phone.test.ts``. Change both.
"""

from __future__ import annotations

import pytest

from app.utils.phone import format_phone

SHARED_VECTORS = [
    (None, ""),
    ("", ""),
    ("8015551234", "(801) 555-1234"),
    ("801-555-1234", "(801) 555-1234"),
    ("801.555.1234", "(801) 555-1234"),
    ("(801) 555-1234", "(801) 555-1234"),
    ("+1 801 555 1234", "(801) 555-1234"),
    ("+1 (801) 555-1234", "(801) 555-1234"),
    ("1-801-555-1234", "(801) 555-1234"),
    ("18015551234", "(801) 555-1234"),
    ("555-0100", "555-0100"),
    ("+44 20 7946 0958", "+44 20 7946 0958"),
    ("28015551234", "28015551234"),
    ("801555123", "801555123"),
    ("80155512345", "80155512345"),
    ("n/a", "n/a"),
    ("801-555-1234 x12", "801-555-1234 x12"),
    ("٨٠١٥٥٥١٢٣٤", "٨٠١٥٥٥١٢٣٤"),
]


@pytest.mark.parametrize(("raw", "shown"), SHARED_VECTORS)
def test_matches_the_app_formatter(raw, shown):
    assert format_phone(raw) == shown
