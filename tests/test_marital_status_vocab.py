"""``alumni.marital_status`` is a fixed four-option choice (#647).

The list lives in code (``app/core/dropdowns.py``), is mirrored in the frontend
and is written down in ``database/dropdowns.md``. That is three copies of one
fact, so this file pins the two the backend owns to each other — the same guard
``tests/test_employment_status_vocab.py`` puts on ``EMPLOYMENT_STATUSES``, and
for the same reason: the doc is what a human edits when the list changes, and a
doc that silently disagrees with the code is worse than no doc.

Why a constant and not a ``vocabulary_terms`` category is argued in
``dropdowns.md``; the short version is that a runtime-editable list would make an
admin edit able to start silently rejecting alumni survey answers. The
consequences of the choice — off-list values stay readable, blanks are ignored —
are exercised in ``tests/test_survey_names_and_marital.py``.

Offline: a tuple and a markdown file, no database.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.core.dropdowns import MARITAL_STATUSES

DROPDOWNS_MD = Path(__file__).resolve().parents[1] / "database" / "dropdowns.md"


# --- the canonical list ------------------------------------------------------


def test_the_four_options_are_exactly_what_jake_decided() -> None:
    """Frozen verbatim (#647). Widening this is a product decision, not a
    judgement call: every value added here becomes writable by anyone holding a
    survey link."""
    assert MARITAL_STATUSES == ("Single", "Married", "Divorced", "Widowed")


def test_options_are_unique() -> None:
    assert len(set(MARITAL_STATUSES)) == len(MARITAL_STATUSES)


def test_there_is_no_marital_status_validator() -> None:
    """Deliberate, and the mirror of ``validate_employment_status``' absence: the
    column is a plain varchar holding off-list legacy values, and an allow-list on
    it would 422 those records on an unrelated edit. The constraint belongs on the
    untrusted WRITE path (the survey), not on the column."""
    from app.core import dropdowns

    assert not hasattr(dropdowns, "validate_marital_status")


def test_it_is_not_an_editable_vocabulary_category() -> None:
    """No ``/vocabulary/marital_status`` endpoint and no seed migration — the list
    is code on both sides. A DB-backed category would make the survey's allow-list
    runtime-mutable."""
    from app.core.vocabularies import VocabularyCategory

    assert "marital_status" not in {c.value for c in VocabularyCategory}


# --- the doc is the source of truth and must agree ---------------------------

_BULLET_RE = re.compile(r"^-\s+(.+?)(\s*\*\([^)]+\)\*)?\s*$")


def _documented_statuses() -> list[str]:
    """Parse the ``## Marital Status`` option bullets from ``dropdowns.md``.

    Reads only the single unbroken run of bullets after that section's "Options:"
    line, mirroring ``tests/test_employment_status_vocab.py``.
    """
    lines = DROPDOWNS_MD.read_text(encoding="utf-8").splitlines()
    start = lines.index("## Marital Status")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "---")
    options_at = next(i for i in range(start, end) if lines[i].strip() == "Options:")

    found: list[str] = []
    for line in lines[options_at + 1 : end]:
        if not line.startswith("-"):
            break
        match = _BULLET_RE.match(line.strip())
        assert match is not None, f"unparseable option bullet: {line!r}"
        found.append(match.group(1).strip())
    return found


def test_doc_actually_lists_statuses() -> None:
    """Guard the parser: a silent miss would make the assertion below pass
    vacuously."""
    assert len(_documented_statuses()) == len(MARITAL_STATUSES)


def test_doc_matches_the_tuple_in_order() -> None:
    assert _documented_statuses() == list(MARITAL_STATUSES)
