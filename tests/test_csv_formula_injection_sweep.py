"""Sweep of every CSV/spreadsheet export path for formula-injection coverage
(#169-adjacent audit). Complements the path-specific tests already living in
``test_alumni_export.py`` (customizable export), ``test_alumni_cohort_export.py``
(cohort round-trip), and ``test_events_routes.py`` (event attendee export) — all
three reuse the single canonical guard, ``alumni_export._fmt``.

This file exists to pin down two things that don't belong to any one export
path:

1. ``_fmt`` still does the right thing on values that are LEGITIMATELY allowed
   to start with ``+``/``-`` (a phone number, a negative number) — the guard
   must not corrupt real data while neutralizing an attack.
2. ``_fmt`` only inspects ``text[0]``, so a value with LEADING WHITESPACE
   before the formula-lead character (``" =HYPERLINK(...)"``) slips past it.
   That's a real gap in ``_fmt`` in isolation. What makes it unreachable today
   is that every producer trims before the value ever reaches ``_fmt``:
   ``survey_responses._text()``, the schema-level ``_empty_to_none`` /
   ``_Section._trim_strings`` validators used by named/notes/URL fields — AND,
   for the handful of free-text alumni core fields with no dedicated
   pydantic validator at all (e.g. ``languages``, which has none), the generic
   whitespace-collapse fallback in ``hygiene._clean_field`` that both
   ``create_alumni`` and ``update_alumni`` run before anything is persisted.
   That hygiene fallback is the load-bearing piece for those fields — if it
   is ever narrowed to a fixed field allow-list, this test starts failing and
   that's the signal to add real ``_fmt``-level protection instead of relying
   on upstream trimming.
"""

from app.schemas.alumni import AlumniUpdate
from app.services import alumni_export, hygiene

# --- 1. _fmt(): real attack payloads across every formula-lead char ----------


def test_fmt_neutralizes_every_formula_lead_character():
    payloads = {
        "=": '=HYPERLINK("http://evil.com","Click")',
        "+": "+1+1+cmd|' /C calc'!A1",
        "-": "-2+3+cmd|' /C calc'!A1",
        "@": "@SUM(1+1)",
        "\t": "\t=1+1",
        "\r": "\r=1+1",
    }
    for lead, payload in payloads.items():
        out = alumni_export._fmt(payload, "str")
        assert out.startswith("\t"), f"lead {lead!r} was not neutralized: {out!r}"
        assert out == "\t" + payload, "the original value must be preserved verbatim after the tab"


# --- 2. _fmt(): legitimate data that happens to start with +/- is preserved --


def test_fmt_preserves_legitimate_phone_and_negative_number_content():
    # A phone number legitimately starts with '+' (international format) and a
    # negative number legitimately starts with '-'. Both get the same
    # tab-prefix treatment as an attack payload would (the guard can't tell the
    # difference from the character alone) but the guard must not truncate,
    # reorder, or otherwise corrupt the value -- stripping the leading tab
    # must recover the exact original text.
    phone = "+1 801-555-0100"
    negative = "-42"

    fmt_phone = alumni_export._fmt(phone, "str")
    fmt_negative = alumni_export._fmt(negative, "str")

    assert fmt_phone == "\t" + phone
    assert fmt_phone[1:] == phone  # nothing lost, nothing reordered
    assert fmt_negative == "\t" + negative
    assert fmt_negative[1:] == negative


def test_fmt_leaves_ordinary_text_untouched():
    assert alumni_export._fmt("Acme Corp", "str") == "Acme Corp"
    assert alumni_export._fmt(None, "str") == ""


# --- 3. _fmt() in isolation misses a LEADING-WHITESPACE formula payload ------


def test_fmt_alone_does_not_catch_leading_whitespace_before_formula_lead():
    # Documents the real, narrow gap in _fmt: it only inspects text[0]. This is
    # NOT a live vulnerability by itself -- see the module docstring and the
    # hygiene test below for why no current write path can hand _fmt a value
    # like this. If a future export path calls _fmt directly on unvalidated
    # user input, THIS is the case that must be closed first.
    hostile = ' =HYPERLINK("http://evil.com","Click")'
    out = alumni_export._fmt(hostile, "str")
    assert out == hostile  # NOT tab-prefixed -- _fmt alone is insufficient here


# --- 4. What actually closes the gap: hygiene strips before storage ----------


def test_hygiene_strips_leading_whitespace_before_it_ever_reaches_storage():
    # `languages` was singled out because, unlike first_name/notes/linkedin_url/
    # citizenship/etc., AlumniBase has NO field_validator for it at all -- so a
    # value with leading whitespace sails through pydantic untouched. It is the
    # one alumni core column that depends entirely on hygiene's generic
    # collapse-any-string fallback (not a field-specific validator) for
    # whitespace normalization.
    hostile = ' =HYPERLINK("http://evil.com","Click")'
    payload = AlumniUpdate(languages=hostile)
    # Confirms the schema really does let it through raw (i.e. this isn't
    # already handled at the pydantic layer, which would make this test moot).
    assert payload.languages == hostile

    # Both create_alumni and update_alumni run every write through
    # hygiene.clean_alumni_payload before touching the DB (app/services/
    # alumni.py). That's what actually strips the leading space here.
    cleaned, changes = hygiene.clean_alumni_payload(payload, jsonable=False)
    assert cleaned["languages"] == hostile.strip()
    assert changes[0]["field"] == "languages"

    # And once cleaned, _fmt correctly neutralizes what's left.
    formatted = alumni_export._fmt(cleaned["languages"], "str")
    assert formatted.startswith("\t=HYPERLINK")
