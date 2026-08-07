"""Survey response review queue.

Alumni submit "confirm your info" updates from the public survey link; we STAGE
them (`submit_response`) as pending rows instead of touching the record. Staff
review each in the console (`list_pending`, with a before/after diff) and apply
(`apply_response` — writes the whitelisted fields to the real record) or reject
(`reject_response`).

Every field an alum can submit is in `_FIELDS` (key -> table/column/kind), which
is the ONLY thing that gets written — nothing else in the payload is applied.

These rows ARE the survey history the profile's Surveys tab shows: it derives
from them in `profile._derive_survey_history`. Do NOT also insert into the
legacy `surveys` table (see `models.crm.Survey`) — one fact, one home.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dropdowns import MARITAL_STATUSES, holds_designation
from app.core.errors import InvalidRequestError, NotFoundError
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.models.employment import CurrentEmployment
from app.models.engagement import AlumniProgramEngagement
from app.models.survey_response import SurveyResponse
from app.schemas.survey import (
    SurveyChange,
    SurveyResponseItem,
    SurveySubmitResult,
)
from app.services import hygiene, supabase_storage
from app.services.survey_email import LINK_DEAD_MESSAGE, verify_survey_token

log = logging.getLogger(__name__)

# A staged survey photo lives in the SAME private bucket as real headshots, but
# under a `survey-pending/` prefix so it can NEVER overwrite an alum's actual
# headshot before an admin approves it. On apply it's copied to the headshot key
# (the alum's net_id, or their alumni_id as a fallback) and the staged copy is
# removed; on reject the staged copy is just removed.
_HEADSHOT_BUCKET = "headshots"
_STAGED_PHOTO_PREFIX = "survey-pending"


def _staged_photo_path(survey_response_id: int) -> str:
    return f"{_STAGED_PHOTO_PREFIX}/{survey_response_id}"


def _headshot_key(alum: Alumni) -> str:
    """The object key an alum's headshot is stored under: their net_id, or their
    alumni_id as a string when they have no net_id (so a missing net_id NEVER
    hard-fails an otherwise-valid approval)."""
    net_id = (alum.net_id or "").strip()
    return net_id or str(alum.alumni_id)


def _sniff_image_content_type(data: bytes) -> str:
    """Best-effort image MIME from magic bytes for re-uploading a staged photo as
    a headshot. Mirrors the headshot route's sniff; defaults to JPEG (the staged
    bytes were already validated as JPEG/PNG/WebP at upload time)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


@dataclass(frozen=True)
class _Field:
    key: str
    label: str
    group: str  # alumni | contact | employment | engagement
    column: str
    kind: str  # text | int | bool | date | designation | choice
    # `designation` only: the canonical string written to the column when the alum
    # ticks the box. Lives HERE, server-side, never in the payload — see `_coerce`.
    marker: str | None = None
    # `choice` only: the exact values that may be written. Anything else the alum
    # (or anyone holding a survey link) submits is IGNORED — see `_coerce`.
    options: tuple[str, ...] | None = None
    # False when a blank submission is NOT an instruction to clear the column.
    # Defaults True, which is the long-standing `text` behaviour (blank -> NULL).
    # Turned off for the identity columns the confirm page pre-fills: an empty box
    # there means the field was cleared or never rendered, not that the alumnus has
    # no surname. See the name block in `_FIELDS`.
    blankable: bool = True


# What `_coerce` returns for a value the server refuses to write. DISTINCT from
# `None`, which is a real instruction ("store NULL"). The apply path skips the
# column entirely on this, so an off-list or disallowed-blank answer leaves what
# is on file exactly as it was rather than overwriting it.
_IGNORE = object()


# The whitelist of fields an alum may submit + where each writes. Order matches
# the confirm page. Anything not here is ignored on submit AND on apply.
_FIELDS: tuple[_Field, ...] = (
    _Field("employment.current_industry", "Industry", "employment", "current_industry", "text"),
    _Field("profile.employment_status", "Employment status", "alumni", "employment_status", "text"),
    _Field("employment.current_employer", "Company", "employment", "current_employer", "text"),
    _Field("employment.current_title", "Title", "employment", "current_title", "text"),
    _Field(
        "employment.current_industry_secondary",
        "Secondary industry",
        "employment",
        "current_industry_secondary",
        "text",
    ),
    _Field("employment.current_city", "Employment city", "employment", "current_city", "text"),
    _Field("employment.current_state", "Employment state", "employment", "current_state", "text"),
    _Field(
        "employment.current_country", "Employment country", "employment", "current_country", "text"
    ),
    _Field("employment.current_zip", "Company ZIP", "employment", "current_zip", "text"),
    _Field("contact.city", "Residence city", "contact", "city", "text"),
    _Field("contact.state", "Residence state", "contact", "state", "text"),
    _Field("contact.country", "Residence country", "contact", "country", "text"),
    _Field("profile.spouse_first_name", "Spouse first name", "alumni", "spouse_first_name", "text"),
    _Field("profile.spouse_last_name", "Spouse last name", "alumni", "spouse_last_name", "text"),
    # "Personal email", not "Permanent email" (#392): the profile UI, the intake
    # sheet and staff all call this column the personal email, and the survey
    # calling it something else made "it says I have no personal email" hard to
    # reconcile with a form that plainly showed one. One name everywhere.
    _Field("contact.personal_email", "Personal email", "contact", "personal_email", "text"),
    _Field("contact.work_email", "Work email", "contact", "work_email", "text"),
    _Field("contact.phone", "Phone", "contact", "phone", "text"),
    _Field("profile.linkedin_url", "LinkedIn", "alumni", "linkedin_url", "text"),
    _Field("profile.graduate_degree", "Graduate program", "alumni", "graduate_degree", "text"),
    _Field("profile.graduate_school", "Graduate school", "alumni", "graduate_school", "text"),
    _Field(
        "profile.graduate_graduation_year",
        "Projected graduation year",
        "alumni",
        "graduate_graduation_year",
        "int",
    ),
    # Finance designations (#529). CFA/CFP are their OWN varchar columns on
    # alumni_program_engagement holding the literal marker ('CFA'/'CFP'), not
    # booleans and not free text: the designation filter maps "CFA" ->
    # cfa_designation and matches `holds_designation` — non-NULL and not one of
    # the negatives (`core/dropdowns.py`, `repositories/alumni.py`) — and the
    # exports/import read the same columns. Writing a ticked CFA into
    # `other_designations` instead would drop that alum out of the CFA filter and
    # the designation counts, so the two live apart on purpose.
    #
    # Anything with a survey link can POST to this whitelist, so `designation`
    # deliberately ignores the submitted text and writes the marker from the field
    # definition — a `text` kind here would let a stranger put 100 arbitrary
    # characters into a column the rest of the app reads as "holds the CFA".
    _Field(
        "program.cfa_designation",
        "CFA designation",
        "engagement",
        "cfa_designation",
        "designation",
        "CFA",
    ),
    _Field(
        "program.cfp_designation",
        "CFP designation",
        "engagement",
        "cfp_designation",
        "designation",
        "CFP",
    ),
    # CPA joined the list after CFA/CFP (Jake, 2026-08-03). It has always had a
    # column and a filter but NO intake-sheet mapping, so nothing ever populated
    # it — a CPA typed into an "Other" blank landed in free text and stayed
    # invisible to the CPA filter.
    _Field(
        "program.cpa_designation",
        "CPA designation",
        "engagement",
        "cpa_designation",
        "designation",
        "CPA",
    ),
    # Everything else the alum holds stays free text (the survey collects it in
    # three "Other" blanks and joins them with ", ").
    _Field(
        "profile.other_designations", "Other designations", "alumni", "other_designations", "text"
    ),
    # Personal & family (alumni table). Columns already exist — no migration.
    #
    # NAME BLOCK (#646). The four name columns on `alumni`, at the head of the
    # personal group so the confirm page reads as one "Name" question rather than
    # four fields scattered through the form. Until #646 an alum could not correct
    # their own name at all — a marriage rename had to go to staff.
    #
    # "Middle or Maiden name" is the LABEL on purpose, and it is a product call,
    # not a guess: staff have been entering maiden names in `middle_name` for
    # years, so the label is being changed to match the data rather than the data
    # migrated to match a label.
    #
    # `alumni.birth_name` STAYS IN THE SCHEMA AND STAYS UNUSED. It is the column
    # you would expect a maiden name to live in, which is exactly why this note is
    # here — the next person to read this will go looking for it. It is not
    # surveyed, not written, not repurposed and not dropped: the real maiden names
    # are in `middle_name`, and pointing the survey at `birth_name` instead would
    # split one fact across two columns and leave every existing record's maiden
    # name invisible to it.
    #
    # `blankable=False` on all four: `survey_email.get_respondent` pre-fills every
    # name box, so a blank one means the box was cleared or never rendered — never
    # "this alumnus has no first name". Anything holding a survey link can POST to
    # this whitelist, and NULLing an identity column that search, the duplicate
    # check and every export key off is not an edit a public form should be able to
    # stage. Staff can still clear a name from the profile editor.
    _Field("profile.first_name", "First name", "alumni", "first_name", "text", blankable=False),
    _Field(
        "profile.middle_name",
        "Middle or Maiden name",
        "alumni",
        "middle_name",
        "text",
        blankable=False,
    ),
    _Field("profile.last_name", "Last name", "alumni", "last_name", "text", blankable=False),
    _Field(
        "profile.preferred_first_name",
        "Preferred first name",
        "alumni",
        "preferred_first_name",
        "text",
        blankable=False,
    ),
    _Field("profile.gender", "Gender", "alumni", "gender", "text"),
    # Marital status became a fixed four-option choice in #647. It was free text
    # here (and the column still is a plain varchar(50) — see
    # `core.dropdowns.MARITAL_STATUSES` for why the constraint is not on the
    # column), so the `choice` kind is what stops a public submit from putting
    # arbitrary text into it while leaving every off-list value already on file
    # readable, displayable and untouched.
    _Field(
        "profile.marital_status",
        "Marital status",
        "alumni",
        "marital_status",
        "choice",
        options=MARITAL_STATUSES,
        # A blank is not an answer here either: an alumnus whose stored status is
        # off-list ("Separated") sees a dropdown with no matching option, and if
        # leaving it alone wiped the column the survey would destroy exactly the
        # legacy value #647 requires be preserved.
        blankable=False,
    ),
    _Field("profile.birth_date", "Birthday", "alumni", "birth_date", "date"),
    _Field("profile.citizenship", "Citizenship", "alumni", "citizenship", "text"),
    _Field("profile.home_country", "Home country", "alumni", "home_country", "text"),
    _Field(
        "program.mentor_willing",
        "Willing to mentor students",
        "engagement",
        "mentor_willing",
        "bool",
    ),
    _Field(
        "program.women_in_finance_mentor_willing",
        "Willing to mentor for Women in Finance",
        "engagement",
        "women_in_finance_mentor_willing",
        "bool",
    ),
    _Field(
        "program.guest_speaker_willing",
        "Willing to be a guest speaker",
        "engagement",
        "guest_speaker_willing",
        "bool",
    ),
    _Field(
        "program.help_at_event_willing",
        "Willing to help at an event",
        "engagement",
        "help_at_event_willing",
        "bool",
    ),
    _Field(
        "program.nettrek_host_willing",
        "Willing to host a NetTrek visit",
        "engagement",
        "nettrek_host_willing",
        "bool",
    ),
    _Field(
        "program.finance_conference_willing",
        "Willing to take part in the finance conference",
        "engagement",
        "finance_conference_willing",
        "bool",
    ),
    _Field(
        "program.company_event_sponsor_willing",
        "Willing to sponsor a company event",
        "engagement",
        "company_event_sponsor_willing",
        "bool",
    ),
    _Field(
        "program.case_competition_host_willing",
        "Willing to host a case competition",
        "engagement",
        "case_competition_host_willing",
        "bool",
    ),
    _Field("program.piff_donor", "Pay It Forward donor", "engagement", "piff_donor", "bool"),
)
_FIELD_BY_KEY = {f.key: f for f in _FIELDS}

# The `alumni` columns the fuzzy duplicate check keys off (with graduation_year,
# which the survey cannot write). Named here so the apply path and
# `hygiene.detect_duplicates` cannot drift about which columns make a rename a
# rename — middle/preferred names are not part of the dedup identity.
_DEDUP_NAME_COLUMNS = frozenset({"first_name", "last_name"})

_TRUE = frozenset({"yes", "true", "1"})


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _current(field: _Field, obj: object | None) -> str:
    """The on-file value as a display string ('Yes'/'No' for booleans). A
    designation column is a held/not-held fact to the alum, so its marker string
    reads as 'Yes' too — the reviewer's diff should say what changed, not which
    literal we store."""
    raw = getattr(obj, field.column, None) if obj is not None else None
    # A MISSING side row is not "unknown" for a yes/no question: these columns are
    # NOT NULL with a false/NULL default, so an alum with no engagement row simply
    # holds none of them. Reading that as "" made every such alum's honest "No"
    # show up in the review queue as a change ("" -> "No") the reviewer then
    # "applied" for nothing. Text fields keep returning "" — a blank column and a
    # blank answer really are the same thing there.
    if obj is None and field.kind not in ("bool", "designation"):
        return ""
    if field.kind == "designation":
        # Presence is NOT the question: a column imported as the literal "No" is
        # a stored (truthy) value that must still read as "No" here, or the
        # reviewer's before/after diff would claim they already held it.
        return "Yes" if holds_designation(raw) else "No"
    if field.kind == "bool":
        return "Yes" if raw else "No"
    # `choice` deliberately falls through to the verbatim stored text: a value
    # that is no longer (or never was) one of the options — "Separated" in
    # marital_status — must still READ back exactly as stored, here and on the
    # confirm page. The option list constrains what can be WRITTEN, never what can
    # be displayed (#647).
    return _text(raw)


def _after(field: _Field, raw: object) -> str:
    """The submitted value as a display string, normalized to match `_current`."""
    value = _text(raw)
    if field.kind in ("bool", "designation"):
        return "Yes" if value.lower() in _TRUE else "No"
    if field.kind == "choice":
        # Show the CANONICAL option, not what was typed, so the reviewer's "after"
        # is the string that will actually be stored. Nothing off-list ever reaches
        # here — `_coerce` returns `_IGNORE` for those and both the submit and the
        # diff drop the field before this is called — but resolving it again here
        # keeps the displayed value and the written value derived from one rule.
        return _choice(field, raw) or ""
    return value


def _choice(field: _Field, raw: object) -> str | None:
    """The canonical option matching *raw*, or ``None`` when it isn't one.

    Case-insensitive and whitespace-trimmed: the alum's browser sends whatever the
    form put in the option's value attribute, and a stored value being re-submitted
    unchanged can carry the casing drift already on the record.
    """
    value = _text(raw)
    if not value:
        return None
    folded = value.lower()
    for option in field.options or ():
        if option.lower() == folded:
            return option
    return None


def _coerce(field: _Field, raw: object):
    """The submitted value coerced to the column's Python type for writing.

    Returns the module-level `_IGNORE` sentinel when the server refuses to write
    the value at all (an off-list `choice`, or a blank on a non-`blankable`
    field). Callers must check for it BEFORE writing — `None` means "store NULL"
    and is a different instruction.
    """
    value = _text(raw)
    if field.kind == "bool":
        return value.lower() in _TRUE
    if field.kind == "designation":
        # The payload only says whether the box was ticked; WHAT gets stored comes
        # from the field definition, so a public submit can never write anything
        # other than the canonical marker (or NULL) into a filtered column.
        return field.marker if value.lower() in _TRUE else None
    if not value and not field.blankable:
        # A blank on a field that cannot be blanked is not an instruction, it's a
        # missing answer — leave what's on file alone.
        return _IGNORE
    if field.kind == "choice":
        # Same principle as `designation`: the server decides what may be written,
        # not the payload. Anything holding a survey link can POST to this
        # whitelist, so an unrecognized answer is ignored outright rather than
        # stored as free text (which is what this field used to be) or written as
        # NULL (which would destroy a legitimate off-list value already on file).
        return _choice(field, raw) or _IGNORE
    if field.kind == "int":
        try:
            return int(value) if value else None
        except ValueError:
            return None
    if field.kind == "date":
        # Expect an ISO "YYYY-MM-DD" string (the survey's <input type="date">).
        try:
            return datetime.date.fromisoformat(value) if value else None
        except ValueError:
            return None
    return value or None


# --------------------------------------------------------------- submit ------


async def submit_response(
    session: AsyncSession, token: str, fields: dict[str, str], has_photo: bool = False
) -> SurveySubmitResult:
    """Stage an alum's submission (token-gated, public). Keeps only recognized
    fields; nothing is applied to the record here.

    A photo-only submission (empty `fields` but `has_photo=True`) still creates a
    pending response so the page has an id to attach the photo to. Only a true
    no-op (no recognized fields AND no photo) returns early with a null id."""
    alumni_id = verify_survey_token(token)
    if alumni_id is None:
        raise NotFoundError(LINK_DEAD_MESSAGE)
    alum = (
        await session.execute(select(Alumni).where(Alumni.alumni_id == alumni_id))
    ).scalar_one_or_none()
    if alum is None or alum.archived:
        raise NotFoundError(LINK_DEAD_MESSAGE)

    # Recognized keys only — and, for the kinds where the server decides what may
    # be written, only values it would actually write. Staging an answer the apply
    # path is going to ignore would put a change in the review queue that silently
    # does nothing when a reviewer approves it, which is the same class of bug as
    # the dropped-key warning in `apply_response`. Rejecting it here is also what
    # makes the four marital-status options a real constraint rather than a UI
    # convention: the endpoint is public (token-gated), so anyone with a link can
    # POST arbitrary text at this whitelist.
    payload = {
        k: _text(v)
        for k, v in (fields or {}).items()
        if k in _FIELD_BY_KEY and _coerce(_FIELD_BY_KEY[k], v) is not _IGNORE
    }
    if not payload and not has_photo:
        return SurveySubmitResult(staged=False, change_count=0)

    response = SurveyResponse(
        alumni_id=alumni_id,
        graduation_year=alum.graduation_year,
        payload=payload,
        status="pending",
    )
    session.add(response)
    # Flush so the identity is assigned; capture it BEFORE commit expires the row,
    # so the public page can attach an optional photo to this exact response.
    await session.flush()
    new_id = response.survey_response_id
    await session.commit()
    return SurveySubmitResult(
        staged=True, change_count=len(payload), survey_response_id=new_id
    )


async def stage_photo(
    session: AsyncSession,
    token: str,
    survey_response_id: int,
    data: bytes,
    content_type: str,
) -> None:
    """Attach an already-validated NEW profile photo to a pending response
    (token-gated, public). The token proves which alum is calling; the response
    must belong to that alum AND still be pending, else it's a 404. The image is
    uploaded to the private headshots bucket under a `survey-pending/<id>` key
    (never an alum's live headshot) and recorded on the row for admin review."""
    alumni_id = verify_survey_token(token)
    if alumni_id is None:
        raise NotFoundError(LINK_DEAD_MESSAGE)
    resp = (
        await session.execute(
            select(SurveyResponse).where(
                SurveyResponse.survey_response_id == survey_response_id
            )
        )
    ).scalar_one_or_none()
    # A foreign response (belongs to another alum) or one already reviewed is
    # indistinguishable from "not found" to the caller — never leak which.
    if resp is None or resp.alumni_id != alumni_id or resp.status != "pending":
        raise NotFoundError("Survey response not found.")
    path = _staged_photo_path(survey_response_id)
    await supabase_storage.upload_object(_HEADSHOT_BUCKET, path, data, content_type)
    resp.staged_photo_path = path
    await session.commit()


# ------------------------------------------------------------- review --------


async def _load_side_rows(session: AsyncSession, ids: list[int]):
    async def by_alum(model):
        rows = (
            (await session.execute(select(model).where(model.alumni_id.in_(ids)))).scalars().all()
        )
        return {r.alumni_id: r for r in rows}

    return (
        await by_alum(AlumniContactInfo),
        await by_alum(CurrentEmployment),
        await by_alum(AlumniProgramEngagement),
    )


async def list_pending(session: AsyncSession, graduation_year: int) -> list[SurveyResponseItem]:
    """Pending responses for a grad year, each with its before/after diff
    (unchanged fields are dropped)."""
    responses = (
        (
            await session.execute(
                select(SurveyResponse)
                .where(
                    SurveyResponse.status == "pending",
                    SurveyResponse.graduation_year == graduation_year,
                )
                .order_by(SurveyResponse.submitted_at.desc())
            )
        )
        .scalars()
        .all()
    )
    if not responses:
        return []

    ids = [r.alumni_id for r in responses]
    alumni = {
        a.alumni_id: a
        for a in (await session.execute(select(Alumni).where(Alumni.alumni_id.in_(ids))))
        .scalars()
        .all()
    }
    contacts, jobs, engs = await _load_side_rows(session, ids)

    items: list[SurveyResponseItem] = []
    for r in responses:
        alum = alumni.get(r.alumni_id)
        if alum is None:
            continue
        by_group = {
            "alumni": alum,
            "contact": contacts.get(r.alumni_id),
            "employment": jobs.get(r.alumni_id),
            "engagement": engs.get(r.alumni_id),
        }
        changes: list[SurveyChange] = []
        for key, raw in (r.payload or {}).items():
            field = _FIELD_BY_KEY.get(key)
            if field is None:
                continue
            # A value the apply path will not write is not a change. `submit_response`
            # already drops these, so in practice this only catches rows staged
            # BEFORE the field gained its constraint (#647) — those must not sit in
            # the queue advertising an edit that approving them wouldn't make.
            if _coerce(field, raw) is _IGNORE:
                continue
            before = _current(field, by_group.get(field.group))
            after = _after(field, raw)
            if before == after:
                continue
            changes.append(
                SurveyChange(field_key=key, label=field.label, before=before, after=after)
            )
        name = (
            " ".join(p for p in (alum.first_name, alum.last_name) if p).strip()
            or alum.preferred_first_name
            or "Alum"
        )
        # Mint a short-lived signed URL so the reviewer can preview a submitted
        # photo (the bucket is private). None when no photo was staged.
        photo_preview_url = None
        if r.staged_photo_path:
            photo_preview_url = await supabase_storage.create_signed_url(
                _HEADSHOT_BUCKET, r.staged_photo_path
            )
        items.append(
            SurveyResponseItem(
                survey_response_id=r.survey_response_id,
                alumni_id=r.alumni_id,
                name=name,
                submitted_at=r.submitted_at.isoformat(),
                changes=changes,
                photo_preview_url=photo_preview_url,
            )
        )
    return items


async def _get_pending(session: AsyncSession, response_id: int) -> SurveyResponse:
    resp = (
        await session.execute(
            select(SurveyResponse).where(SurveyResponse.survey_response_id == response_id)
        )
    ).scalar_one_or_none()
    if resp is None:
        raise NotFoundError("Survey response not found.")
    if resp.status != "pending":
        raise InvalidRequestError("This response has already been reviewed.")
    return resp


async def apply_response(
    session: AsyncSession, response_id: int, actor_user_id: int | None
) -> list[dict]:
    """Write the staged changes to the alum's record and mark applied.

    Returns the soft duplicate warnings a NAME change raised (#646/#627) — an
    empty list for every other apply. They never block: the response is already
    applied and committed by the time they are returned, exactly as on the staff
    rename path, because two alumni genuinely can share a name and a graduation
    year and a marriage rename into a real collision is sometimes correct. The
    point is that the person who approved it is told.
    """
    resp = await _get_pending(session, response_id)
    alum = (
        await session.execute(select(Alumni).where(Alumni.alumni_id == resp.alumni_id))
    ).scalar_one_or_none()
    if alum is None:
        raise NotFoundError("Alum not found.")
    contacts, jobs, engs = await _load_side_rows(session, [resp.alumni_id])
    contact = contacts.get(resp.alumni_id)
    job = jobs.get(resp.alumni_id)
    eng = engs.get(resp.alumni_id)

    # Promote a staged photo (if any) into the alum's real headshot: download the
    # staged copy, re-upload it under the headshot key (net_id, or alumni_id when
    # no net_id), then remove the staged copy so the pending prefix stays clean.
    if resp.staged_photo_path:
        data = await supabase_storage.download_object(
            _HEADSHOT_BUCKET, resp.staged_photo_path
        )
        content_type = _sniff_image_content_type(data)
        await supabase_storage.upload_object(
            _HEADSHOT_BUCKET, _headshot_key(alum), data, content_type
        )
        await supabase_storage.delete_object(_HEADSHOT_BUCKET, resp.staged_photo_path)
        # Clear the pointer with the object. Leaving it set left the row naming a
        # key that no longer exists, so every later reader — the review queue's
        # signed-URL preview, the engineer survey-state screen, the profile's
        # "+ photo" note — was working from a path that resolves to nothing.
        resp.staged_photo_path = None

    # An apply that writes NOTHING used to report success: a payload key missing
    # from `_FIELDS` was skipped silently, no log, no error, and the response
    # still flipped to "applied". Any future rename on either side of the wire
    # would therefore lose alumni answers invisibly, so count what was written
    # and what was dropped, warn on the drops, and put both in the audit row.
    # Field KEYS only ever appear in the log — never a submitted value.
    written = 0
    dropped: list[str] = []
    # Values the server refused to write (an off-list `choice`, a blank on a
    # non-blankable name). Counted apart from `dropped`, which means "we don't know
    # this key at all": an ignored value is a KNOWN field whose answer we declined,
    # and the column keeps whatever it already held.
    ignored: list[str] = []
    # True when this apply moves first_name or last_name — the only two columns the
    # fuzzy duplicate check keys off (with graduation_year). Nothing else the survey
    # can write affects it, so the extra query below runs on renames only.
    name_changed = False
    for key, raw in (resp.payload or {}).items():
        field = _FIELD_BY_KEY.get(key)
        if field is None:
            dropped.append(key)
            continue
        value = _coerce(field, raw)
        if value is _IGNORE:
            ignored.append(key)
            continue
        written += 1
        if field.group == "alumni":
            if field.column in _DEDUP_NAME_COLUMNS and getattr(alum, field.column) != value:
                name_changed = True
            setattr(alum, field.column, value)
        elif field.group == "contact":
            if contact is None:
                contact = AlumniContactInfo(alumni_id=alum.alumni_id)
                session.add(contact)
            setattr(contact, field.column, value)
        elif field.group == "employment":
            if job is None:
                job = CurrentEmployment(alumni_id=alum.alumni_id)
                session.add(job)
            setattr(job, field.column, value)
        elif field.group == "engagement":
            if eng is None:
                eng = AlumniProgramEngagement(alumni_id=alum.alumni_id)
                session.add(eng)
            setattr(eng, field.column, value)

    if dropped:
        log.warning(
            "Survey response %s: %d submitted field key(s) are not in the apply "
            "whitelist and wrote NOTHING to alumni %s: %s",
            response_id,
            len(dropped),
            alum.alumni_id,
            ", ".join(sorted(dropped)),
        )
    if ignored:
        log.info(
            "Survey response %s: %d submitted value(s) were not writable and left "
            "alumni %s unchanged: %s",
            response_id,
            len(ignored),
            alum.alumni_id,
            ", ".join(sorted(ignored)),
        )

    # A rename can collide with an existing alumnus, and until now the survey was
    # the one write path where that happened in total silence — #627 fixed the
    # staff create/edit path and the survey did not exist on it. Same check, same
    # rule, reached by handing `detect_duplicates` the identity this apply
    # produces (the values are already on `alum` in memory).
    #
    # ONLY first/last/graduation_year are passed, and the `blockers` half is
    # deliberately discarded rather than raised. The survey cannot write byu_id or
    # net_id — they aren't in `_FIELDS` — so the exact-collision branches have
    # nothing to fire on that this apply caused; passing the stored ids anyway
    # would let a pre-existing data problem surface here as if this approval had
    # created it, and could turn an unrelated archived-ghost warning into noise on
    # every name change.
    duplicate_warnings: list[dict] = []
    if name_changed:
        _blockers, duplicate_warnings = await hygiene.detect_duplicates(
            session,
            {
                "first_name": alum.first_name,
                "last_name": alum.last_name,
                "graduation_year": alum.graduation_year,
            },
            exclude_alumni_id=alum.alumni_id,
        )

    resp.status = "applied"
    resp.reviewed_by_user_id = actor_user_id
    resp.reviewed_at = datetime.datetime.now(datetime.UTC)
    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type="apply_survey_response",
            entity_type="alumni",
            entity_id=alum.alumni_id,
            new_value=(
                f"survey_response={response_id} fields={len(resp.payload or {})} "
                f"written={written} dropped={len(dropped)} ignored={len(ignored)}"
                + (f" duplicate_warnings={len(duplicate_warnings)}" if name_changed else "")
            ),
        )
    )
    await session.commit()
    return duplicate_warnings


async def reject_response(
    session: AsyncSession, response_id: int, actor_user_id: int | None
) -> None:
    """Mark a staged response rejected — nothing is written to the record."""
    resp = await _get_pending(session, response_id)
    # Discard any staged photo so rejected uploads don't linger in storage, and
    # clear the pointer with it — a row naming a deleted key is a path every
    # later reader (preview URLs, the engineer state screen) tries to resolve.
    if resp.staged_photo_path:
        await supabase_storage.delete_object(_HEADSHOT_BUCKET, resp.staged_photo_path)
        resp.staged_photo_path = None
    resp.status = "rejected"
    resp.reviewed_by_user_id = actor_user_id
    resp.reviewed_at = datetime.datetime.now(datetime.UTC)
    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type="reject_survey_response",
            entity_type="alumni",
            entity_id=resp.alumni_id,
            new_value=f"survey_response={response_id}",
        )
    )
    await session.commit()
