"""The reminder emails say they are reminders (#560).

Amy, 2026-09-21, for Tanya: the 2nd and 3rd survey emails should open with
something like "In case you missed this survey, we really value your update and
would appreciate you filling it out."

Before this, all three stages were BYTE FOR BYTE IDENTICAL. The cadence is stage
0 on day 0, stage 1 on day 7 and stage 2 on day 14, but the staff-editable copy
(#524) was one row and ``render_survey_email`` took no stage at all — so a
reminder arrived looking exactly like the original, with nothing to say it was a
second ask.

Jake, 2026-09-21: a line ON TOP of the current message, not a per-stage rewrite.
Hence one field, ``reminder_note``, and everything else identical across stages.

The questions pinned here, in the order of what would hurt most if it broke:

* does the note reach the REAL send, on the reminder stages and ONLY those — the
  #524 lesson is that proving it on a preview is worth nothing;
* does a FORGOTTEN stage argument understate rather than overstate (a first
  contact that reads like a chase-up is the worse failure);
* can staff turn it OFF and have that survive a round trip? This is the one
  place on this table where blank is a value rather than "use the default", and
  the off switch is un-saveable if NULL and '' are ever folded together;
* is the note escaped into the HTML like every other piece of staff copy.
"""

import asyncio

import pytest

from app.core.errors import InvalidRequestError
from app.models.survey_email_message import SurveyEmailMessage
from app.services import survey_email, survey_message
from app.services.survey_email import (
    STAGE_INITIAL,
    STAGE_REMINDER_1,
    STAGE_REMINDER_2,
    Recipient,
    render_survey_email,
)
from app.services.survey_message import (
    DEFAULT_MESSAGE,
    DEFAULT_REMINDER_NOTE,
    ON_FILE_FIELDS,
    SurveyMessage,
)
from tests.test_survey_message import (  # noqa: F401 - fake_settings is a fixture
    _capture_send,
    _MessageSession,
    _SendSession,
    fake_settings,
)

LINK = "https://finance.alumni.byu.edu/survey/tok"


def _msg(note: str, **kw) -> SurveyMessage:
    return SurveyMessage(
        subject=kw.get("subject", "S"),
        intro=kw.get("intro", "The intro."),
        closing=kw.get("closing", "The closing."),
        on_file_fields=kw.get("on_file_fields", ("Company",)),
        reminder_note=note,
    )


def _rcpt() -> Recipient:
    return Recipient(1, "Dana", "dana@example.com", (("Company", "Acme"),))


# --------------------------------------------------------------- rendering ----


@pytest.mark.parametrize("stage", [STAGE_REMINDER_1, STAGE_REMINDER_2])
def test_both_reminders_open_with_the_note_in_html_and_plaintext(stage):
    """BOTH parts, because a mail client may render either — the same reason
    #524 checks both."""
    _, html, text = render_survey_email(
        _rcpt(), LINK, _msg("Just a nudge."), stage=stage
    )
    assert "Just a nudge." in text
    assert "Just a nudge." in html
    # It OPENS the message: after the greeting, before the intro.
    assert text.index("Just a nudge.") < text.index("The intro.")
    assert html.index("Just a nudge.") < html.index("The intro.")
    assert text.index("Hello Dana,") < text.index("Just a nudge.")


def test_the_initial_never_shows_the_note_however_it_is_worded():
    """Stage 0 is not a chase-up and must never read like one, whatever staff
    have typed into the box."""
    _, html, text = render_survey_email(
        _rcpt(), LINK, _msg("Just a nudge."), stage=STAGE_INITIAL
    )
    assert "Just a nudge." not in text
    assert "Just a nudge." not in html


def test_a_forgotten_stage_argument_sends_the_undecorated_email():
    """The default is STAGE_INITIAL on purpose: a missing argument must
    UNDERSTATE. Overstating turns someone's first contact into a chase-up."""
    message = _msg("Just a nudge.")
    assert render_survey_email(_rcpt(), LINK, message) == render_survey_email(
        _rcpt(), LINK, message, stage=STAGE_INITIAL
    )


def test_an_empty_note_makes_a_reminder_identical_to_the_initial():
    """The off switch, end to end: cleared copy means the reminders render
    exactly as they did before #560 — not merely "without the sentence"."""
    off = _msg("")
    assert render_survey_email(
        _rcpt(), LINK, off, stage=STAGE_REMINDER_1
    ) == render_survey_email(_rcpt(), LINK, off, stage=STAGE_INITIAL)


def test_nothing_but_the_note_changes_between_stages():
    """Jake's scope: a line on top, not a per-stage rewrite. Subject, intro,
    on-file box, link and closing are the same email."""
    message = _msg("Just a nudge.")
    subj0, html0, text0 = render_survey_email(
        _rcpt(), LINK, message, stage=STAGE_INITIAL
    )
    subj1, html1, text1 = render_survey_email(
        _rcpt(), LINK, message, stage=STAGE_REMINDER_1
    )
    assert subj0 == subj1
    # Taking the note's own paragraph back out of the reminder gets the initial.
    stripped = html1.replace(
        '<p style="margin:0 0 12px;font-size:15px;">Just a nudge.</p>', ""
    )
    assert stripped.split() == html0.split()
    assert text1.replace("Just a nudge.\n\n", "") == text0


def test_the_note_is_escaped_into_the_html_like_every_other_piece_of_copy():
    """Staff-authored input rendered into an email. Escape first, then add
    markup — the only markup a note can introduce is a paragraph break."""
    _, html, _text = render_survey_email(
        _rcpt(),
        LINK,
        _msg("<script>alert(1)</script>\n\nSecond <b>para</b>."),
        stage=STAGE_REMINDER_1,
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<b>para</b>" not in html
    # ...but a blank line between paragraphs still becomes real markup.
    assert '</p><p style="margin:0 0 12px;">Second' in html


# ------------------------------------------------------------- the real send --


def test_the_note_reaches_a_real_reminder_send_and_not_the_initial(
    fake_settings, monkeypatch  # noqa: F811
):
    """THE POINT OF #560, and the #524 lesson applied: prove it on the dict
    handed to Resend, not on a preview.

    The path is ``send_survey_stage`` -> ``_send_and_log`` ->
    ``_build_survey_email`` -> ``render_survey_email``. ``_send_and_log`` is
    already claiming the send log with the stage, so the stage was in scope all
    along — it simply was never passed down.
    """
    sent = _capture_send(monkeypatch, message=_msg("In case you missed this."))

    # The initial first: nobody has been emailed, so stage 0 is what goes out.
    session = _SendSession()
    asyncio.run(
        survey_email.send_survey_stage(
            session,
            graduation_year=1900,
            max_stage=STAGE_REMINDER_2,
            actor_user_id=1,
        )
    )
    (initial_batch,) = sent
    assert initial_batch
    for email in initial_batch:
        assert "In case you missed this." not in email["text"]
        assert "In case you missed this." not in email["html"]

    # Now the 1-week reminder to the same cohort, through the same session, so
    # the send log really is what moves the send on to the next stage.
    sent.clear()
    asyncio.run(
        survey_email.send_survey_stage(
            session,
            graduation_year=1900,
            max_stage=STAGE_REMINDER_2,
            actor_user_id=1,
        )
    )
    (reminder_batch,) = sent
    assert reminder_batch, "the reminder stage sent nothing"
    for email in reminder_batch:
        assert "In case you missed this." in email["text"]
        assert "In case you missed this." in email["html"]
    # Same subject line — the note is the only difference (Jake's scope).
    assert reminder_batch[0]["subject"] == initial_batch[0]["subject"]


def test_an_unedited_reminder_carries_the_default_sentence(
    fake_settings, monkeypatch  # noqa: F811
):
    """Shipping #560 is enough on its own. The default is NOT blank, so the
    reminders start saying it without anyone having to go and type it into the
    console — which is the failure mode the feature exists to remove."""
    sent = _capture_send(monkeypatch, message=DEFAULT_MESSAGE)
    session = _SendSession()
    session.seed_sent(1900, STAGE_INITIAL, [1, 2])
    asyncio.run(
        survey_email.send_survey_stage(
            session,
            graduation_year=1900,
            max_stage=STAGE_REMINDER_1,
            actor_user_id=1,
        )
    )
    (batch,) = sent
    assert batch
    for email in batch:
        assert DEFAULT_REMINDER_NOTE in email["text"]


# ------------------------------------------------------- storage resolution ---


def test_a_column_that_was_never_set_resolves_to_the_default_sentence():
    """NULL = "never set". Every row written before this migration is NULL, so a
    cohort whose copy was already customised still gets the reminder line."""
    row = SurveyEmailMessage(id=1, subject="S", intro="I", closing="C")
    assert row.reminder_note is None
    assert survey_message._resolve(row).reminder_note == DEFAULT_REMINDER_NOTE


def test_a_cleared_column_stays_cleared():
    """'' = "staff turned it off", and it must NOT fall back to the default.

    ⚠️ This is the assertion that stops someone "tidying" ``_resolve`` into the
    ``(row.x or "").strip() or DEFAULT`` pattern the three fields beside it use.
    Under that pattern, clearing the box would put the default sentence straight
    back on the next read and the off switch would be un-saveable.
    """
    row = SurveyEmailMessage(
        id=1, subject="S", intro="I", closing="C", reminder_note=""
    )
    assert survey_message._resolve(row).reminder_note == ""


def test_no_row_at_all_still_resolves_to_the_default():
    assert survey_message._resolve(None).reminder_note == DEFAULT_REMINDER_NOTE


def test_saving_writes_a_cleared_note_as_empty_string_not_null():
    """So "never set" can only ever mean a row that predates #560."""
    session = _MessageSession(
        SurveyEmailMessage(id=1, subject="S", intro="I", closing="C")
    )
    asyncio.run(
        survey_message.set_message(
            session,
            subject="S",
            intro="I",
            closing="C",
            on_file_fields=[],
            reminder_note="   ",
            actor_user_id=3,
        )
    )
    assert session.row.reminder_note == ""


def test_the_editor_sees_the_note_and_a_changed_note_counts_as_customised():
    """``is_customized`` drives the "Reset to default" affordance, and compares
    against the DEFAULTS rather than asking whether a row exists."""
    row = SurveyEmailMessage(
        id=1,
        subject=survey_message.DEFAULT_SUBJECT,
        intro=survey_message.DEFAULT_INTRO,
        closing=survey_message.DEFAULT_CLOSING,
        on_file_fields=list(ON_FILE_FIELDS),
        reminder_note="Something else entirely.",
    )
    read = asyncio.run(survey_message.get_message(_MessageSession(row)))
    assert read.reminder_note == "Something else entirely."
    assert read.is_customized is True

    row.reminder_note = DEFAULT_REMINDER_NOTE
    unchanged = asyncio.run(survey_message.get_message(_MessageSession(row)))
    assert unchanged.is_customized is False


# ---------------------------------------------------------------- validation --


def test_an_empty_note_is_accepted_where_an_empty_intro_would_be_refused():
    """The asymmetry is the feature: a blank intro is a broken email, a blank
    note is a decision."""
    assert survey_message._clean_reminder_note("") == ""
    assert survey_message._clean_reminder_note(None) == ""
    with pytest.raises(InvalidRequestError):
        survey_message._clean_body("", label="introduction")


@pytest.mark.parametrize(
    "value",
    [
        "x" * (survey_message.REMINDER_NOTE_MAX_CHARS + 1),
        "Hello​there",  # zero-width space
        "Nudge\x07",  # control character
    ],
)
def test_a_note_that_would_break_the_email_is_refused(value):
    with pytest.raises(InvalidRequestError):
        survey_message._clean_reminder_note(value)


def test_a_textarea_crlf_in_the_note_is_normalised_not_rejected():
    assert survey_message._clean_reminder_note("One\r\n\r\nTwo") == "One\n\nTwo"
