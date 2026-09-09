"""Text neutralisation for untrusted email content.

Three separate jobs live here, and it matters that they stay separate:

* **Character neutralisation** — strip the characters that let text render as
  something other than what it is. Same family of characters, and the same
  reasoning, as ``tests/test_invisible_char_rules.py``: bidi overrides and
  zero-width characters are Unicode category ``Cf``, not ``Cc``, so the naive
  "is it a control character" check misses every one of them.
* **Structural containment** — computing a fence that the body provably cannot
  break out of, and escaping the two byte sequences that could otherwise forge
  or terminate the sentinel comments wrapping it.
* **Slugs and truncation** — turning an attacker-controlled subject line into a
  filename, and capping a body that is long enough to flood a context window.

Nothing here decides anything. It only makes untrusted text safe to look at.
"""

from __future__ import annotations

import re
import unicodedata

#: Characters that are invisible, or that reorder what follows them, and are
#: therefore a way to make a reader and a parser disagree about the same bytes.
#: Ranges are exactly the ones the design calls out.
INVISIBLE_RANGES: tuple[tuple[int, int], ...] = (
    (0x200B, 0x200F),  # zero-width space/non-joiner/joiner, LRM, RLM
    (0x202A, 0x202E),  # LRE, RLE, PDF, LRO, RLO — bidi overrides
    (0x2066, 0x2069),  # LRI, RLI, FSI, PDI — bidi isolates
    (0xFEFF, 0xFEFF),  # zero-width no-break space / BOM
)

#: Maximum characters of email body we are willing to carry into a Markdown
#: file that an assistant will later read in full.
BODY_CHAR_LIMIT = 20_000

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_BACKTICK_RUN = re.compile(r"`+")


def is_invisible(ch: str) -> bool:
    """True for the zero-width / bidi characters we refuse to carry."""
    point = ord(ch)
    return any(low <= point <= high for low, high in INVISIBLE_RANGES)


def find_invisible(text: str) -> list[str]:
    """Every invisible character present, as ``U+XXXX`` labels, de-duplicated.

    Used by the validator, which must be able to say WHICH character it found
    rather than just refusing.
    """
    seen: dict[str, None] = {}
    for ch in text:
        if is_invisible(ch):
            seen.setdefault(f"U+{ord(ch):04X}", None)
    return list(seen)


def strip_invisible(text: str) -> tuple[str, int]:
    """Remove invisible/bidi characters. Returns the text and how many went."""
    kept = [ch for ch in text if not is_invisible(ch)]
    return "".join(kept), len(text) - len(kept)


def normalise_newlines(text: str) -> str:
    """CRLF and lone CR both become LF, so line-anchored checks are reliable."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def strip_other_controls(text: str) -> str:
    """Drop remaining C0/C1 control characters except tab and newline.

    A stray ``\\x00`` or ``\\x1b`` in a Markdown file is not an attack on its
    own, but it is a way to make a diff and a terminal disagree.
    """
    return "".join(
        ch for ch in text if ch in "\n\t" or unicodedata.category(ch) not in {"Cc", "Cf"}
    )


def escape_html_comments(text: str) -> str:
    """Neutralise ``<!--`` and ``-->`` so a body cannot forge a sentinel.

    The sentinels that wrap the untrusted region are HTML comments. If a body
    could contain a literal ``-->`` it could close the region early and have
    everything after it read as trusted text; if it could contain ``<!--`` it
    could open a fake one. A backslash makes the escape visible to a human
    reader (it renders literally inside the code fence) and unambiguous to the
    validator, which refuses on any UNESCAPED comment marker outside the two
    real sentinel lines.
    """
    return text.replace("<!--", "<\\!--").replace("-->", "--\\>")


def clean_body(text: str) -> tuple[str, int]:
    """Full neutralisation pass for an email body.

    Returns the cleaned body and the number of invisible characters removed
    (which is itself worth recording — a body that had twelve of them is a
    different kind of message from one that had none).
    """
    text = normalise_newlines(text)
    text, removed = strip_invisible(text)
    text = strip_other_controls(text)
    text = escape_html_comments(text)
    # Trailing whitespace on every line, and a single trailing newline overall.
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return text.strip("\n"), removed


def clean_field(text: str, *, max_len: int = 200) -> str:
    """Neutralise a short header value (subject, sender name, filename).

    Collapses to a single line: these land next to machine-read keys, so a
    newline in one of them would let a subject line inject a header field.
    """
    text = normalise_newlines(text or "")
    text, _ = strip_invisible(text)
    text = strip_other_controls(text)
    text = " ".join(text.split())
    text = escape_html_comments(text)
    return text[:max_len].strip()


def longest_backtick_run(text: str) -> int:
    return max((len(m.group(0)) for m in _BACKTICK_RUN.finditer(text)), default=0)


def fence_for(text: str) -> str:
    """A code fence the body provably cannot terminate.

    ``max(3, longest run + 1)``: a Markdown fence is closed only by a run of at
    least the same length, so a fence one backtick longer than anything in the
    body cannot be closed from inside it.
    """
    return "`" * max(3, longest_backtick_run(text) + 1)


def truncate(text: str, limit: int = BODY_CHAR_LIMIT) -> tuple[str, bool]:
    """Cap the body length. Returns the text and whether it was cut."""
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def slugify(text: str, *, max_len: int = 48, default: str = "untitled") -> str:
    """Filename-safe slug from arbitrary (attacker-controlled) text."""
    text = normalise_newlines(text or "")
    text, _ = strip_invisible(text)
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_STRIP.sub("-", text).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rsplit("-", 1)[0] or slug[:max_len]
    return slug.strip("-") or default
