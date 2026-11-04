"""Phone display formatting for server-built files (fa-web-app#924)."""

import re

# ASCII only: Python's ``\D`` keeps other scripts' digits, JavaScript's does not.
_NON_DIGIT = re.compile(r"[^0-9]")


def format_phone(raw: str | None) -> str:
    """Format a US phone number for display, as the app's ``formatPhone`` does
    (fa-web-app ``src/lib/phone.ts``; keep the two together).

    Ten digits, or eleven with a leading country-code ``1``, become
    ``(555) 123-4567``. Anything else comes back unchanged, so a number we do
    not recognize is never mangled. Display only: the stored value is not
    touched."""
    if not raw:
        return ""
    digits = _NON_DIGIT.sub("", raw)
    ten = digits[1:] if len(digits) == 11 and digits.startswith("1") else digits
    if len(ten) == 10:
        return f"({ten[:3]}) {ten[3:6]}-{ten[6:]}"
    return raw
