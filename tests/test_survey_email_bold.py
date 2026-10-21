"""Bold text in the survey email copy (#877).

Staff wrap words in ``**double asterisks**`` and the email shows them bold. The
questions pinned here, most dangerous first:

* can the markers be used to get HTML into an email? (escape FIRST, then add
  ``<strong>`` — so no: anything tag-shaped stays visible text);
* is the scan linear-time, whatever is typed? (CodeQL ``py/polynomial-redos``
  flags patterns, so this is a ``str.find`` scanner, and a large adversarial
  input is timed here);
* does the plain-text part lose the markers, and the subject stay untouched;
* does an unmatched / empty / multi-line ``**`` stay exactly as typed.

⚠️ ``VECTORS`` is mirrored byte for byte in the frontend's
``src/lib/surveyBold.test.ts``. The editor preview and the sample survey must
bold exactly what the real email bolds; if you change the rule, change both
tables and both implementations together.
"""

import time

import pytest

from app.services.survey_email import (
    STAGE_INITIAL,
    STAGE_REMINDER_1,
    Recipient,
    render_survey_email,
)
from app.services.survey_message import (
    SurveyMessage,
    _bold_segments,
    copy_to_html,
    copy_to_text,
)

LINK = "https://finance.alumni.byu.edu/survey/tok"

# (typed copy, expected segments as (is_bold, text)). Mirrored in the frontend.
VECTORS: list[tuple[str, list[tuple[bool, str]]]] = [
    ("plain text", [(False, "plain text")]),
    ("a **b** c", [(False, "a "), (True, "b"), (False, " c")]),
    ("**all**", [(True, "all")]),
    ("**a** and **b**", [(True, "a"), (False, " and "), (True, "b")]),
    # Unmatched: stays literal.
    ("a ** b", [(False, "a ** b")]),
    ("**a** b **c", [(True, "a"), (False, " b **c")]),
    # Empty / blank pairs are not bold and are kept as typed.
    ("****", [(False, "****")]),
    ("** **", [(False, "** **")]),
    ("x **** y **z**", [(False, "x ****"), (False, " y "), (True, "z")]),
    # Never across a line (so never across a paragraph either).
    ("**a\nb**", [(False, "**a"), (False, "\n"), (False, "b**")]),
    ("**a**\n\n**b**", [(True, "a"), (False, "\n"), (False, "\n"), (True, "b")]),
    # No nesting; single asterisks are just text.
    ("**a *b* c**", [(True, "a *b* c")]),
    ("***x***", [(True, "*x"), (False, "*")]),
    ("", []),
]


@pytest.mark.parametrize(("typed", "expected"), VECTORS)
def test_segments_follow_the_rule(typed, expected):
    assert _bold_segments(typed) == expected


@pytest.mark.parametrize(("typed", "_"), VECTORS)
def test_non_bold_text_survives_round_trip(typed, _):
    """Nothing typed is lost except the markers of a real pair."""
    rebuilt = "".join(
        f"**{chunk}**" if bold else chunk for bold, chunk in _bold_segments(typed)
    )
    assert rebuilt == typed


def test_html_wraps_pairs_in_strong():
    assert copy_to_html("Please **reply soon**.") == (
        "Please <strong>reply soon</strong>."
    )


def test_text_drops_matched_markers_only():
    assert copy_to_text("Please **reply soon**. 5 ** 2") == (
        "Please reply soon. 5 ** 2"
    )


# ---------------------------------------------------------------- injection --

INJECTIONS = [
    ("**<script>alert(1)</script>**",
     "<strong>&lt;script&gt;alert(1)&lt;/script&gt;</strong>"),
    ("**<a href=x>**click", "<strong>&lt;a href=x&gt;</strong>click"),
    ('**" onmouseover="alert(1)**',
     "<strong>&quot; onmouseover=&quot;alert(1)</strong>"),
    ("**'><img src=x onerror=alert(1)>**",
     "<strong>&#x27;&gt;&lt;img src=x onerror=alert(1)&gt;</strong>"),
    ("<strong>raw</strong>", "&lt;strong&gt;raw&lt;/strong&gt;"),
    ("**</strong><script>x</script><strong>**",
     "<strong>&lt;/strong&gt;&lt;script&gt;x&lt;/script&gt;&lt;strong&gt;</strong>"),
    ("&lt;b&gt; **&amp;**", "&amp;lt;b&amp;gt; <strong>&amp;amp;</strong>"),
]


@pytest.mark.parametrize(("typed", "expected"), INJECTIONS)
def test_injection_attempts_stay_escaped(typed, expected):
    out = copy_to_html(typed)
    assert out == expected
    # The ONLY tags that can appear are the ones this function adds.
    stripped = out.replace("<strong>", "").replace("</strong>", "")
    assert "<" not in stripped and ">" not in stripped
    assert '"' not in stripped and "'" not in stripped


@pytest.mark.parametrize(("typed", "_"), INJECTIONS)
def test_injection_through_the_real_render(typed, _):
    msg = SurveyMessage(
        subject="S",
        intro=typed,
        closing=typed,
        on_file_fields=(),
        reminder_note=typed,
    )
    r = Recipient(1, "Dana", "dana@example.com", ())
    _, html, _ = render_survey_email(r, LINK, msg, stage=STAGE_REMINDER_1)
    assert "<script" not in html
    assert "<a href=x" not in html
    assert "<img" not in html
    assert 'onmouseover="' not in html


# ------------------------------------------------------------ the real email --


def _msg(**kw) -> SurveyMessage:
    return SurveyMessage(
        subject=kw.get("subject", "S"),
        intro=kw.get("intro", "The intro."),
        closing=kw.get("closing", "The closing."),
        on_file_fields=(),
        reminder_note=kw.get("note", ""),
    )


def _rcpt() -> Recipient:
    return Recipient(1, "Dana", "dana@example.com", ())


def test_intro_closing_and_note_are_bold_in_html_and_plain_in_text():
    msg = _msg(
        intro="Intro **one**.\n\nSecond **two**.",
        closing="Thanks **three**!\nTanya",
        note="Reminder **four**.",
    )
    _, html, text = render_survey_email(_rcpt(), LINK, msg, stage=STAGE_REMINDER_1)
    for word in ("one", "two", "three", "four"):
        assert f"<strong>{word}</strong>" in html
    assert "**" not in html
    assert "**" not in text
    assert "Intro one." in text and "Second two." in text
    assert "Thanks three!" in text and "Reminder four." in text
    # Paragraph / line structure is unchanged by the bold pass.
    assert '<strong>one</strong>.</p><p style="margin:0 0 12px;">Second' in html
    assert "<strong>three</strong>!<br>Tanya" in html


def test_subject_stays_plain():
    subject, html, _ = render_survey_email(
        _rcpt(), LINK, _msg(subject="Your **update**"), stage=STAGE_INITIAL
    )
    assert subject == "Your **update**"


def test_unmatched_markers_reach_both_parts_as_typed():
    msg = _msg(intro="Rate us 5 ** stars", closing="a **b")
    _, html, text = render_survey_email(_rcpt(), LINK, msg)
    assert "Rate us 5 ** stars" in html and "Rate us 5 ** stars" in text
    assert "a **b" in html and "a **b" in text
    assert "<strong>" not in html


def test_bold_never_spans_a_paragraph_break():
    msg = _msg(intro="**first paragraph\n\nsecond**")
    _, html, _ = render_survey_email(_rcpt(), LINK, msg)
    assert "<strong>" not in html


def test_default_copy_renders_unchanged():
    """The shipped wording has no markers, so an untouched deployment's email is
    exactly what it was before #877."""
    _, html, _ = render_survey_email(_rcpt(), LINK)
    assert "<strong>" not in html


# --------------------------------------------------------------- linear time --


@pytest.mark.parametrize(
    "evil",
    [
        "*" * 200_000,
        "**" + "a" * 200_000,
        "** " * 70_000,
        "**a" * 70_000,
        ("**" + "\n") * 70_000,
        "***" * 70_000,
    ],
)
def test_scan_is_linear_on_adversarial_input(evil):
    """40x the 5000-char cap, in shapes that blow up a backtracking pattern.
    A linear scan does each in a few milliseconds; the bound is generous so CI
    noise cannot flake it, and still far below what quadratic would need."""
    t0 = time.perf_counter()
    copy_to_html(evil)
    copy_to_text(evil)
    assert time.perf_counter() - t0 < 1.0
