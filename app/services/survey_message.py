"""The staff-editable copy of the alumni survey email (#524).

WHAT THIS FIXES. The "Edit email message" box on the Needs Surveying page saved
its intro / closing / on-file field selection to the browser's ``localStorage``
and nowhere else. The real send built its subject and body from module constants
in ``app/services/survey_email.py``, so an edit was per-browser, per-machine,
invisible to the other Career Director, and reached NO alum — the box was a
preview of something nobody was sending. This module makes that copy data: it
lives in one row of ``survey_email_message``, and the send path reads it.

THE DEFAULTS LIVE HERE, IN CODE. :data:`DEFAULT_SUBJECT` / :data:`DEFAULT_INTRO`
/ :data:`DEFAULT_CLOSING` are the Career Directors' authored copy, moved verbatim
out of ``survey_email`` (where they were ``_SUBJECT`` / ``_INTRO`` / ``_CLOSING``).
A stored row is an OVERRIDE of them, exactly like ``alert_message_templates``:

  * no row, an unreadable table, or a database that never had the migration ->
    the defaults;
  * a blank column -> the default for that field alone;
  * ``is_customized`` compares the resolved copy against the defaults, so it
    answers "does this differ from what we wrote" rather than "does a row exist".

That direction is the whole safety property. Making the wording editable must
never be able to make the email empty, and it must never be able to stop a send.
:func:`get_for_send` therefore cannot raise: every failure resolves to
:data:`DEFAULT_MESSAGE`, which is byte for byte what the email said before this
feature existed.

⚠️ ``reminder_note`` IS THE ONE FIELD WHERE BLANK IS NOT "USE THE DEFAULT" (#560).
It is the line the 1-week and 2-week reminders open with, above the intro, and
the initial email never shows it. A NULL column means "never set" and resolves to
:data:`DEFAULT_REMINDER_NOTE`; a stored ``''`` means someone cleared it in the
console and the reminders carry no extra line. Every other field here folds blank
into the default, and folding this one would make the off switch un-saveable.

⚠️ ``on_file_fields`` CAN ONLY HIDE, NEVER ADD. It is validated against
:data:`ON_FILE_FIELDS` — the canonical label list that ``survey_email``'s
``_build_on_file`` builds from — and stored in that canonical order. A stored
list is intersected with the canonical one again at render time. So this control
cannot introduce a field the email does not already know how to fill, cannot
reorder the box, and cannot put the email's field list out of step with the
survey form or the sample survey. That is deliberate: the form / email picker /
sample-survey lists must agree, and a parity test enforces it.
"""

import logging
import unicodedata
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidRequestError
from app.models.survey_email_message import SurveyEmailMessage
from app.models.user import User

# The invisible characters no copy may carry. The same set the alumni email/URL
# gates use, imported rather than re-listed so the app keeps ONE definition of
# "invisible": a zero-width character or a bidi override in an outbound email is
# either a slip or an attempt to make the text read differently from what was
# typed into the editor.
from app.schemas.alumni import _INVISIBLE_CHARS
from app.schemas.survey import SurveyMessageRead

log = logging.getLogger(__name__)


# ------------------------------------------------------------- the defaults --
#
# The Career Directors' authored copy. Moved here verbatim from
# `survey_email._SUBJECT` / `_INTRO` / `_CLOSING` — do not reword these to "fix"
# anything a staff edit could fix instead; this is the text a reset restores.

DEFAULT_SUBJECT = "Your BYU Finance alumni information — a quick update"

DEFAULT_INTRO = (
    "Our BYU Finance alumni are one of the greatest strengths of our program. "
    "We are working to strengthen our alumni community by staying connected with "
    "you throughout your career. To do that, we're reaching out to ensure we have "
    "your most current information.\n\n"
    "Please take a moment to review the information below and update or replace "
    "any information in our alumni survey that is wrong or missing."
)

DEFAULT_CLOSING = (
    "If everything above is correct, please confirm that your information is up "
    "to date at the bottom of the survey. If anything has changed, please update "
    "the applicable questions in the survey. We have also included a few optional "
    "questions that will help us better connect with and support our alumni "
    "community.\n\n"
    "Thank you for being an important part of the BYU Finance family. We look "
    "forward to staying connected with you in the years ahead!\n\n"
    "Warmest regards,\nTanya Harmon & Amy Densley\nBYU Finance Career Directors"
)

# The line the REMINDER emails open with, above the intro (#560). Amy's own
# wording, from her 2026-09-21 mail asking for it — the directors' authored copy,
# exactly like the three fields above, so "Reset to default" restores this.
#
# ⚠️ NOT BLANK BY DEFAULT, and that is deliberate. Everywhere else on this table
# "nothing stored" means "send what we always sent"; here it means "send the
# reminder line", because the POINT of #560 is that reminders stop reading like a
# first contact. Shipping it blank would have left the change inert until someone
# happened to type it into the console, which is the failure mode this feature
# exists to remove. Clearing the box in the console stores '' and turns it off.
#
# Stage 0 NEVER shows it, whatever is stored — see `survey_email.render_survey_email`.
DEFAULT_REMINDER_NOTE = (
    "In case you missed this survey, we really value your update and would "
    "appreciate you filling it out."
)

#: The "here's what we have on file" rows, by label, IN THE ORDER THE EMAIL SHOWS
#: THEM. This is the canonical list: ``survey_email._ON_FILE_BUILDERS`` is keyed
#: by exactly these strings and iterates this tuple, so a label that appears in
#: one and not the other is a hard failure in tests rather than a row that
#: silently disappears from the email.
#:
#: ⚠️ Adding a label here without adding its builder — or the matching survey
#: question and sample value — is the drift this project keeps re-growing. Three
#: lists must agree (form / email picker / sample values) and a parity test
#: enforces it.
ON_FILE_FIELDS: tuple[str, ...] = (
    "Current employment status",
    "Company",
    "Title",
    "Industry",
    "Secondary industry",
    "Employment city",
    "Employment state",
    "Employment country",
    "Residence city",
    "Residence state",
    "Residence country",
    "Spouse name",
    # "Personal email" everywhere (#392) — see survey_responses._FIELDS.
    "Personal email",
    "Work email",
    "LinkedIn profile",
    "Graduate school program",
    "Graduate school name",
    "Projected graduation year",
    "Finance designations",
)

# Length caps, mirrored by CHECK constraints in the migration. Generous — this is
# a whole email body — but still caps: Resend answers an oversized payload with a
# 400, and an email lost to a 400 is worse than a wordy one.
SUBJECT_MAX_CHARS = 200
BODY_MAX_CHARS = 5000
# The reminder line is a sentence or two, not a second body (#560).
REMINDER_NOTE_MAX_CHARS = 2000


@dataclass(frozen=True)
class SurveyMessage:
    """The resolved copy one send renders from: never blank, never unknown.

    Frozen and self-contained on purpose — it is read ONCE per send (see
    ``survey_email.send_survey_stage``) and handed to every recipient's render,
    so a staff edit landing mid-send cannot make one batch of a cohort read
    differently from the next. Equality is by value, which is what lets
    "customised?" be a comparison against :data:`DEFAULT_MESSAGE`.
    """

    subject: str
    intro: str
    closing: str
    on_file_fields: tuple[str, ...]
    #: The line the 1-week and 2-week reminders open with (#560). '' means the
    #: reminders carry no extra line and read exactly as the initial does. Never
    #: None — the None/'' distinction lives in the COLUMN, and is resolved away
    #: by :func:`_resolve` before it gets this far.
    reminder_note: str = DEFAULT_REMINDER_NOTE


#: What the email says with nothing stored — and what a reset restores.
DEFAULT_MESSAGE = SurveyMessage(
    subject=DEFAULT_SUBJECT,
    intro=DEFAULT_INTRO,
    closing=DEFAULT_CLOSING,
    on_file_fields=ON_FILE_FIELDS,
    reminder_note=DEFAULT_REMINDER_NOTE,
)


# --------------------------------------------------------------- validation --

def _has_forbidden_chars(value: str, *, allow_newlines: bool) -> bool:
    """Control (category ``Cc``) or invisible characters.

    ``allow_newlines`` exempts ``\\n`` alone: an intro and a closing are
    multi-paragraph prose and blank lines are how paragraphs are expressed, but a
    SUBJECT is a header field — a newline in one is header injection, not
    formatting, so the subject is checked without the exemption.
    """
    for ch in value:
        if allow_newlines and ch == "\n":
            continue
        if unicodedata.category(ch) == "Cc" or ch in _INVISIBLE_CHARS:
            return True
    return False


def _clean_line(value: str | None, *, label: str, max_chars: int) -> str:
    """Trim and check a single-line field (the subject)."""
    trimmed = (value or "").strip()
    if not trimmed:
        raise InvalidRequestError(f"The {label} cannot be empty.")
    if len(trimmed) > max_chars:
        raise InvalidRequestError(
            f"The {label} is too long ({len(trimmed)} characters); "
            f"the limit is {max_chars}."
        )
    if _has_forbidden_chars(trimmed, allow_newlines=False):
        raise InvalidRequestError(
            f"The {label} contains a line break or an invisible character. "
            "It must be a single line of ordinary text."
        )
    return trimmed


def _clean_body(value: str | None, *, label: str) -> str:
    """Trim and check a multi-paragraph field (the intro / the closing).

    ``\\r\\n`` is normalised to ``\\n`` first: a browser textarea submits CRLF,
    and leaving the CR in would both trip the control-character check and put a
    stray carriage return into the plaintext part of the email.
    """
    trimmed = (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not trimmed:
        raise InvalidRequestError(f"The {label} cannot be empty.")
    if len(trimmed) > BODY_MAX_CHARS:
        raise InvalidRequestError(
            f"The {label} is too long ({len(trimmed)} characters); "
            f"the limit is {BODY_MAX_CHARS}."
        )
    if _has_forbidden_chars(trimmed, allow_newlines=True):
        raise InvalidRequestError(
            f"The {label} contains a control or invisible character. "
            "Use ordinary text and blank lines between paragraphs."
        )
    return trimmed


def _clean_reminder_note(value: str | None) -> str:
    """Trim and check the reminder line (#560). MAY be empty — that is the off switch.

    The one validated field on this message that is allowed to come back "".
    Every other one raises on blank, because a blank subject or intro is a broken
    email; a blank reminder note is a deliberate choice to leave the reminders
    reading exactly as they did before #560, and the console has to be able to
    save it.

    Otherwise identical to :func:`_clean_body`: CRLF normalised, length capped,
    control and invisible characters refused — this text is rendered into an
    email to alumni like everything else here.
    """
    trimmed = (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not trimmed:
        return ""
    if len(trimmed) > REMINDER_NOTE_MAX_CHARS:
        raise InvalidRequestError(
            f"The reminder line is too long ({len(trimmed)} characters); "
            f"the limit is {REMINDER_NOTE_MAX_CHARS}."
        )
    if _has_forbidden_chars(trimmed, allow_newlines=True):
        raise InvalidRequestError(
            "The reminder line contains a control or invisible character. "
            "Use ordinary text and blank lines between paragraphs."
        )
    return trimmed


def canonical_fields(fields: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    """The selection, de-duplicated and forced back into canonical order.

    Order comes from :data:`ON_FILE_FIELDS`, never from the request: the on-file
    box reads in the order the survey asks its questions, and letting a caller
    reorder it would be a second, silent way for the email and the form to
    disagree.

    Raises 422 on a label this email cannot build, which is the guard that keeps
    the picker honest — a typo'd or invented field is refused at the moment
    someone saves it, not discovered as a missing row in a sent email.
    """
    if fields is None:
        return ON_FILE_FIELDS
    unknown = sorted({f for f in fields if f not in ON_FILE_FIELDS})
    if unknown:
        raise InvalidRequestError(
            "Unknown information field(s): " + ", ".join(unknown) + "."
        )
    chosen = set(fields)
    return tuple(label for label in ON_FILE_FIELDS if label in chosen)


# ------------------------------------------------------------------ reading --


def _resolve(row: SurveyEmailMessage | None) -> SurveyMessage:
    """Stored copy over the defaults, field by field.

    FIELD BY FIELD, not row by row: a column that is blank (or a list that is
    somehow NULL) falls back on its own, so a half-written row cannot produce an
    email with an empty subject or no body. Nothing here can return "".
    """
    if row is None:
        return DEFAULT_MESSAGE
    subject = (row.subject or "").strip() or DEFAULT_SUBJECT
    intro = (row.intro or "").strip() or DEFAULT_INTRO
    closing = (row.closing or "").strip() or DEFAULT_CLOSING
    # ⚠️ NOT the `or DEFAULT` pattern above, and it must not become one. NULL
    # means "never set" and takes the default; '' means someone cleared the box
    # and the reminders get no extra line. Collapsing the two would make the off
    # switch un-saveable — clearing it would put the default sentence back on the
    # next read.
    reminder_note = (
        DEFAULT_REMINDER_NOTE
        if row.reminder_note is None
        else row.reminder_note.strip()
    )
    stored = row.on_file_fields
    if stored is None:
        fields = ON_FILE_FIELDS
    else:
        # Intersected with the canonical list AGAIN, so a label removed from the
        # code (or inserted by hand in psql) is dropped rather than rendering a
        # row the builders cannot fill.
        chosen = set(stored)
        fields = tuple(label for label in ON_FILE_FIELDS if label in chosen)
    return SurveyMessage(
        subject=subject,
        intro=intro,
        closing=closing,
        on_file_fields=fields,
        reminder_note=reminder_note,
    )


async def _load_row(session: AsyncSession) -> SurveyEmailMessage | None:
    return (
        await session.execute(
            select(SurveyEmailMessage).where(SurveyEmailMessage.id == 1)
        )
    ).scalar_one_or_none()


async def get_for_send(session: AsyncSession) -> SurveyMessage:
    """The copy one send renders from. TOTAL — this cannot raise.

    Read once per send and threaded through every recipient, so an edit saved
    mid-send cannot split a cohort across two versions of the email.

    Any failure at all — the table missing on a database that has not been
    migrated, a permissions problem, a transport error — resolves to
    :data:`DEFAULT_MESSAGE` and is logged. Same contract as
    ``alert_templates.load``: the editable-wording feature must never be able to
    stop the thing it words.
    """
    try:
        return _resolve(await _load_row(session))
    except Exception:  # noqa: BLE001 - a send must never depend on this read
        log.warning(
            "survey_message: could not read the stored copy; "
            "sending with the built-in default wording",
            exc_info=True,
        )
        return DEFAULT_MESSAGE


async def get_message(session: AsyncSession) -> SurveyMessageRead:
    """What the editor shows: the resolved copy plus who last changed it.

    UNCACHED and NOT wrapped in the fail-safe above, unlike :func:`get_for_send`:
    the console must show what is stored right now (or someone saves an edit and
    appears to see it not take), and a read that failed should say so rather than
    quietly presenting the defaults as if they were what is saved.
    """
    row = (
        await session.execute(
            select(SurveyEmailMessage, User.email)
            .outerjoin(User, User.user_id == SurveyEmailMessage.updated_by_user_id)
            .where(SurveyEmailMessage.id == 1)
        )
    ).first()
    stored, updated_by_email = (row[0], row[1]) if row is not None else (None, None)
    message = _resolve(stored)
    return SurveyMessageRead(
        subject=message.subject,
        intro=message.intro,
        closing=message.closing,
        on_file_fields=list(message.on_file_fields),
        reminder_note=message.reminder_note,
        # Compared against the DEFAULTS, not "is there a row" — a row that
        # happens to hold the default copy is not a customisation, and the
        # editor's "Reset to default" affordance keys off this.
        is_customized=message != DEFAULT_MESSAGE,
        updated_at=stored.updated_at if stored is not None else None,
        updated_by_email=updated_by_email,
    )


# ------------------------------------------------------------------ writing --


async def set_message(
    session: AsyncSession,
    *,
    subject: str,
    intro: str,
    closing: str,
    on_file_fields: list[str],
    reminder_note: str,
    actor_user_id: int | None,
) -> None:
    """Store the copy. Validates first; raises 422 if it will not do.

    Does NOT commit — the route commits alongside its audit row, so the copy and
    the record of who wrote it land together or not at all.

    Upserts the single row through the ORM rather than raw SQL: there is exactly
    one row, the read that precedes the write is the same one the console just
    did, and ``updated_at`` bumps from ``TimestampMixin``'s ``onupdate``.
    """
    clean = SurveyMessage(
        subject=_clean_line(subject, label="subject", max_chars=SUBJECT_MAX_CHARS),
        intro=_clean_body(intro, label="introduction"),
        closing=_clean_body(closing, label="closing"),
        on_file_fields=canonical_fields(on_file_fields),
        reminder_note=_clean_reminder_note(reminder_note),
    )
    row = await _load_row(session)
    if row is None:
        row = SurveyEmailMessage(id=1)
        session.add(row)
    row.subject = clean.subject
    row.intro = clean.intro
    row.closing = clean.closing
    row.on_file_fields = list(clean.on_file_fields)
    # Written as '' rather than NULL when cleared — see `_resolve`. A save always
    # makes the column non-NULL, so "never set" can only ever mean a row that
    # predates #560.
    row.reminder_note = clean.reminder_note
    row.updated_by_user_id = actor_user_id


async def reset_message(session: AsyncSession) -> bool:
    """Delete the override so the built-in copy applies again.

    Returns False when there was nothing stored. The route reports that as a
    clean success rather than a 404: unlike an alert template (one of four named
    rows), this is a single site-wide message, and "put it back how it was" is
    the recovery path from copy that reads badly — answering a double-click with
    an error there is the same lockout-shaped mistake as rate-limiting the
    maintenance-mode *disable* route.

    Does not commit; the route does.
    """
    result = await session.execute(
        delete(SurveyEmailMessage).where(SurveyEmailMessage.id == 1)
    )
    return bool(getattr(result, "rowcount", 0))
