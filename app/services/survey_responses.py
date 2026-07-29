"""Survey response review queue.

Alumni submit "confirm your info" updates from the public survey link; we STAGE
them (`submit_response`) as pending rows instead of touching the record. Staff
review each in the console (`list_pending`, with a before/after diff) and apply
(`apply_response` — writes the whitelisted fields to the real record) or reject
(`reject_response`).

Every field an alum can submit is in `_FIELDS` (key -> table/column/kind), which
is the ONLY thing that gets written — nothing else in the payload is applied.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
from app.services import supabase_storage
from app.services.survey_email import verify_survey_token

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
    kind: str  # text | int | bool


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
    _Field("contact.personal_email", "Permanent email", "contact", "personal_email", "text"),
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
    _Field(
        "profile.other_designations", "Finance designations", "alumni", "other_designations", "text"
    ),
    # Personal & family (alumni table). Columns already exist — no migration.
    _Field("profile.gender", "Gender", "alumni", "gender", "text"),
    _Field("profile.marital_status", "Marital status", "alumni", "marital_status", "text"),
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

_TRUE = frozenset({"yes", "true", "1"})


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _current(field: _Field, obj: object | None) -> str:
    """The on-file value as a display string ('Yes'/'No' for booleans)."""
    if obj is None:
        return ""
    raw = getattr(obj, field.column, None)
    if field.kind == "bool":
        return "Yes" if raw else "No"
    return _text(raw)


def _after(field: _Field, raw: object) -> str:
    """The submitted value as a display string, normalized to match `_current`."""
    value = _text(raw)
    if field.kind == "bool":
        return "Yes" if value.lower() in _TRUE else "No"
    return value


def _coerce(field: _Field, raw: object):
    """The submitted value coerced to the column's Python type for writing."""
    value = _text(raw)
    if field.kind == "bool":
        return value.lower() in _TRUE
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
        raise NotFoundError("This survey link is invalid or has expired.")
    alum = (
        await session.execute(select(Alumni).where(Alumni.alumni_id == alumni_id))
    ).scalar_one_or_none()
    if alum is None or alum.archived:
        raise NotFoundError("This survey link is invalid or has expired.")

    payload = {k: _text(v) for k, v in (fields or {}).items() if k in _FIELD_BY_KEY}
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
        raise NotFoundError("This survey link is invalid or has expired.")
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
) -> None:
    """Write the staged changes to the alum's record and mark applied."""
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

    for key, raw in (resp.payload or {}).items():
        field = _FIELD_BY_KEY.get(key)
        if field is None:
            continue
        value = _coerce(field, raw)
        if field.group == "alumni":
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

    resp.status = "applied"
    resp.reviewed_by_user_id = actor_user_id
    resp.reviewed_at = datetime.datetime.now(datetime.UTC)
    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type="apply_survey_response",
            entity_type="alumni",
            entity_id=alum.alumni_id,
            new_value=f"survey_response={response_id} fields={len(resp.payload or {})}",
        )
    )
    await session.commit()


async def reject_response(
    session: AsyncSession, response_id: int, actor_user_id: int | None
) -> None:
    """Mark a staged response rejected — nothing is written to the record."""
    resp = await _get_pending(session, response_id)
    # Discard any staged photo so rejected uploads don't linger in storage.
    if resp.staged_photo_path:
        await supabase_storage.delete_object(_HEADSHOT_BUCKET, resp.staged_photo_path)
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
