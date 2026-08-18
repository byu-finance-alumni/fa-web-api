"""Field-level audit capture on the survey APPLY path (#45).

The staff edit path and the CSV import path both record what a save changed —
one ``audit_logs`` row per field, with the real old and new value, grouped by a
``change_set_id`` and labelled with a ``source``. ``apply_response`` did not. It
writes the alum's answers with a raw ``setattr`` (deliberately — the whitelist
above it is the load-bearing part of a PUBLIC write surface, so it must not be
routed through ``update_alumni``), and it recorded ONE summary row: how many
fields moved, never which, never from what.

That gap is permanent in a way most gaps are not. An old value exists only until
the ``setattr`` overwrites it; nothing else in the system remembers it. So every
survey approval made before this capture landed is unreconstructable, and every
week without it loses more.

What is pinned here:

  * **N changed fields -> N rows**, each with the real old and new value.
  * **A resubmitted, identical value writes nothing.** This is the case that
    dominates: the survey re-confirms the whole form, so most applies carry a
    dozen unchanged answers and the two the alum actually corrected. Recording
    all fourteen would bury the two. The comparison is
    ``alumni_service._unchanged``, imported rather than restated, so the survey
    path and the staff path agree on what "changed" means.
  * **One apply = one change set**, summary row included.
  * **``source='survey'``** — a third provenance, not a flavour of ``manual`` or
    ``import``, because restore has to tell an alum's own correction from a staff
    edit from a spreadsheet overwrite.
  * **The staff path's field-name namespace**, so ``career.current_employer``
    means the same column no matter which path wrote it.
  * **The summary row survives unchanged**, counts and all — it holds the
    dropped/ignored signal no per-field row can express.
  * **Reject is untouched**, and the engineer reroute carries the new rows.

Offline throughout — fake sessions in the style of
``tests/test_survey_names_and_marital.py``; no Postgres, no network. The one
exception is the engineer-reroute test, which needs a REAL flush (the guard is a
``before_flush`` hook) and uses in-memory SQLite.
"""

import asyncio
import io
import types

import pytest
from PIL import Image
from sqlalchemy import BigInteger, Integer, create_engine, func, select
from sqlalchemy.orm import Session

from app.core.audit_context import (
    AUDIT_SOURCE_MANUAL,
    AUDIT_SOURCE_SURVEY,
    audit_source,
    reset_audit_actor,
    reset_audit_source,
    set_audit_actor,
)
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.models.employment import CurrentEmployment, EmploymentHistory
from app.models.engagement import AlumniProgramEngagement
from app.models.engineer_action import EngineerActionLog
from app.services import survey_responses as sr
from app.services.alumni import SECTION_KEYS


@pytest.fixture(autouse=True)
def _clean_audit_context():
    """Both audit contextvars are module-global; a leaked ``survey`` scope or a
    leaked engineer actor would silently mislabel (or delete) every later test's
    rows."""
    reset_audit_source()
    reset_audit_actor()
    yield
    reset_audit_source()
    reset_audit_actor()


# ------------------------------------------------------------- fakes ---------


class _Result:
    """One canned ``execute`` result answering both access shapes — the alumnus
    read (``scalar_one_or_none``) and the fuzzy duplicate read
    (``scalars().all()``)."""

    def __init__(self, obj, rows=()):
        self._obj = obj
        self._rows = list(rows)

    def scalar_one_or_none(self):
        return self._obj

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _Session:
    def __init__(self, obj=None):
        self._obj = obj
        self.added = []
        self.committed = 0

    async def execute(self, _stmt):
        return _Result(self._obj)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        # Assign the surrogate id Postgres would, so a row added mid-apply and
        # then named in an audit row (#446's demotion names `employment[<id>]`)
        # is named by its real id rather than by None.
        for obj in self.added:
            if isinstance(obj, EmploymentHistory) and obj.employment_history_id is None:
                obj.employment_history_id = 77

    async def commit(self):
        self.committed += 1


def _fake_resp(**kw):
    base = dict(
        survey_response_id=1,
        alumni_id=5,
        payload={},
        status="pending",
        staged_photo_path=None,
        reviewed_by_user_id=None,
        reviewed_at=None,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def _alum(**kw):
    base = dict(
        alumni_id=5,
        net_id="jdoe5",
        archived=False,
        first_name="Jane",
        middle_name=None,
        last_name="Doe",
        preferred_first_name=None,
        graduation_year=2018,
        linkedin_url="https://www.linkedin.com/in/jane-old",
        employment_status=None,
        gender=None,
        citizenship=None,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def _apply(session, resp, monkeypatch, side_rows=({}, {}, {})):
    """Run ``apply_response`` with ``_get_pending`` / ``_load_side_rows`` stubbed."""

    async def fake_get_pending(_s, _rid):
        return resp

    async def fake_side(_s, _ids):
        return side_rows

    monkeypatch.setattr(sr, "_get_pending", fake_get_pending)
    monkeypatch.setattr(sr, "_load_side_rows", fake_side)
    return asyncio.run(sr.apply_response(session, 1, actor_user_id=9))


def _audits(session) -> list[AuditLog]:
    return [o for o in session.added if isinstance(o, AuditLog)]


def _summary(session) -> AuditLog:
    rows = [a for a in _audits(session) if a.action_type == "apply_survey_response"]
    assert len(rows) == 1, "exactly one summary row per apply"
    return rows[0]


def _fields(session) -> dict[str, tuple[str | None, str | None]]:
    """``{field_name: (old_value, new_value)}`` for the field-level rows only.

    Keyed on ``action_type == "update"`` rather than on "has a field_name",
    because an apply can now also emit a ``archive_current_role`` row (#446):
    that row carries a field_name too (the demoted history row's id) but it is
    not a field change, and letting it in here would make every assertion in this
    module about WHICH FIELDS MOVED depend on whether the employer moved as well.
    Its own behaviour is pinned in ``tests/test_employment_archiving.py``.
    """
    return {
        a.field_name: (a.old_value, a.new_value)
        for a in _audits(session)
        if a.field_name is not None and a.action_type == "update"
    }


# ------------------------------------------------- one row per real change ---


def test_each_changed_field_gets_its_own_row_with_the_real_old_value(monkeypatch):
    """The whole point: three answers moved, so three rows say exactly what moved
    and what it moved FROM. Before this, the trail said only "written=4"."""
    alum = _alum()
    contact = types.SimpleNamespace(alumni_id=5, phone="801-555-0199", city="Provo")
    job = types.SimpleNamespace(
        alumni_id=5, current_employer="Old Bank", current_title="Analyst"
    )
    session = _Session(alum)
    _apply(
        session,
        _fake_resp(
            payload={
                "profile.linkedin_url": "https://www.linkedin.com/in/jane-doe",
                "employment.current_employer": "Goldman Sachs",
                "contact.phone": "801-555-0100",
                # Resubmitted unchanged — the survey re-confirms the whole form.
                "employment.current_title": "Analyst",
            }
        ),
        monkeypatch,
        side_rows=({5: contact}, {5: job}, {}),
    )

    assert _fields(session) == {
        "linkedin_url": (
            "https://www.linkedin.com/in/jane-old",
            "https://www.linkedin.com/in/jane-doe",
        ),
        "career.current_employer": ("Old Bank", "Goldman Sachs"),
        "contact.phone": ("801-555-0199", "801-555-0100"),
    }
    # And the record itself is written exactly as before — capture must not have
    # changed what an apply does.
    assert alum.linkedin_url == "https://www.linkedin.com/in/jane-doe"
    assert job.current_employer == "Goldman Sachs"
    assert job.current_title == "Analyst"
    assert contact.phone == "801-555-0100"


def test_a_resubmitted_identical_value_writes_no_field_row(monkeypatch):
    """A survey confirms the whole form. If every confirmed answer wrote a row,
    the two fields an alum actually corrected would be buried under a dozen
    no-ops, and "what changed on this record" would stop meaning anything."""
    alum = _alum(gender="Female", citizenship="USA")
    session = _Session(alum)
    _apply(
        session,
        _fake_resp(payload={"profile.gender": "Female", "profile.citizenship": "USA"}),
        monkeypatch,
    )
    assert _fields(session) == {}
    # The summary row still reports them as written — that count is "fields this
    # apply wrote", which is a different question and stays as it was.
    assert "written=2" in _summary(session).new_value


def test_a_blank_over_an_empty_column_is_not_a_change(monkeypatch):
    """Legacy rows hold ``""`` where a cleaned write now sends ``None``; both
    render as an empty field. ``alumni_service._unchanged`` is what knows that,
    which is exactly why it is imported rather than restated here."""
    alum = _alum(gender="")
    session = _Session(alum)
    _apply(session, _fake_resp(payload={"profile.gender": "   "}), monkeypatch)
    assert _fields(session) == {}


def test_only_the_fields_that_moved_are_recorded_not_the_whole_payload(monkeypatch):
    alum = _alum(citizenship="USA", gender="Female")
    session = _Session(alum)
    _apply(
        session,
        _fake_resp(
            payload={
                "profile.citizenship": "USA",  # unchanged
                "profile.gender": "Nonbinary",  # changed
                "profile.unknown_key": "x",  # dropped, not on the whitelist
            }
        ),
        monkeypatch,
    )
    assert list(_fields(session)) == ["gender"]


# ------------------------------------------------------ new side rows --------


def test_a_brand_new_side_row_records_what_it_carries_and_skips_the_blanks(monkeypatch):
    """An alum with no engagement row gets one created. Every column on it reads
    back None, so this mirrors ``_upsert_section``'s brand-new-row rule: record
    the answers that carry something, skip blank/False. "Nothing became No" is
    noise, and there are nine of those checkboxes."""
    session = _Session(_alum())
    _apply(
        session,
        _fake_resp(
            payload={
                "program.mentor_willing": "Yes",
                "program.piff_donor": "No",
                "program.cfa_designation": "Yes",
                "program.cfp_designation": "No",
            }
        ),
        monkeypatch,
    )
    assert _fields(session) == {
        "engagement.mentor_willing": (None, "True"),
        "engagement.cfa_designation": (None, "CFA"),
    }


def test_an_existing_side_row_records_a_real_false(monkeypatch):
    """The skip above is about a row that did not exist. On a row that DID, an
    alum answering "No" to something they were marked willing for is a real
    change and the most important one on the form to keep."""
    eng = types.SimpleNamespace(alumni_id=5, mentor_willing=True, piff_donor=False)
    session = _Session(_alum())
    _apply(
        session,
        _fake_resp(
            payload={"program.mentor_willing": "No", "program.piff_donor": "No"}
        ),
        monkeypatch,
        side_rows=({}, {}, {5: eng}),
    )
    assert _fields(session) == {"engagement.mentor_willing": ("True", "False")}


# ----------------------------------------------------------- change set ------


def test_one_apply_is_one_change_set(monkeypatch):
    """Including the summary row: the header and its detail rows have to be one
    version, or the counts can't be tied to the changes they describe."""
    contact = types.SimpleNamespace(alumni_id=5, phone="801-555-0199")
    session = _Session(_alum())
    _apply(
        session,
        _fake_resp(
            payload={
                "profile.linkedin_url": "https://www.linkedin.com/in/jane-doe",
                "contact.phone": "801-555-0100",
            }
        ),
        monkeypatch,
        side_rows=({5: contact}, {}, {}),
    )
    rows = _audits(session)
    assert len(rows) == 3  # summary + two fields
    ids = {a.change_set_id for a in rows}
    assert len(ids) == 1
    assert ids.pop() is not None


def test_two_applies_get_two_change_sets(monkeypatch):
    sessions = []
    for _ in range(2):
        session = _Session(_alum())
        _apply(
            session,
            _fake_resp(
                payload={"profile.linkedin_url": "https://www.linkedin.com/in/jane-doe"}
            ),
            monkeypatch,
        )
        sessions.append(session)
    first, second = (_summary(s).change_set_id for s in sessions)
    assert first != second


def test_an_apply_that_changes_nothing_still_groups_its_summary_row(monkeypatch):
    """No field moved, so no field row — but the summary row is still written and
    still needs an id, or a "this approval did nothing" record could never be
    joined to the approval it describes."""
    session = _Session(_alum(gender="Female"))
    _apply(session, _fake_resp(payload={"profile.gender": "Female"}), monkeypatch)
    assert _fields(session) == {}
    assert _summary(session).change_set_id is not None


# -------------------------------------------------------------- source -------


def test_every_row_of_an_apply_is_sourced_survey(monkeypatch):
    contact = types.SimpleNamespace(alumni_id=5, phone="801-555-0199")
    session = _Session(_alum())
    _apply(
        session,
        _fake_resp(
            payload={
                "profile.linkedin_url": "https://www.linkedin.com/in/jane-doe",
                "contact.phone": "801-555-0100",
            }
        ),
        monkeypatch,
        side_rows=({5: contact}, {}, {}),
    )
    assert {a.source for a in _audits(session)} == {AUDIT_SOURCE_SURVEY}
    assert AUDIT_SOURCE_SURVEY == "survey"


def test_survey_is_its_own_provenance_not_a_flavour_of_the_other_two():
    from app.core.audit_context import AUDIT_SOURCE_IMPORT

    assert len({AUDIT_SOURCE_MANUAL, AUDIT_SOURCE_IMPORT, AUDIT_SOURCE_SURVEY}) == 3
    # Registered in one place and short enough for the varchar(20) column.
    assert len(AUDIT_SOURCE_SURVEY) <= 20


def test_the_survey_source_does_not_leak_past_the_apply(monkeypatch):
    """The scope is a contextvar. A leak would relabel the NEXT write in the same
    request — a staff edit — as a survey answer."""
    session = _Session(_alum())
    _apply(
        session,
        _fake_resp(payload={"profile.linkedin_url": "https://www.linkedin.com/in/jd"}),
        monkeypatch,
    )
    assert audit_source() == AUDIT_SOURCE_MANUAL


# ------------------------------------------------------- field namespace -----


def test_field_names_use_the_staff_paths_namespace_not_the_survey_keys(monkeypatch):
    """Two namespaces meet on this path. The survey payload key
    ``employment.current_employer`` is a FORM grouping; the audit trail's name for
    that column is ``career.current_employer``, because that is what
    ``update_alumni`` writes for the same row. Using the form's namespace would
    put the same column in the trail under two names, and every comparison,
    grouping and restore would treat them as different fields."""
    contact = types.SimpleNamespace(alumni_id=5, city="Provo")
    job = types.SimpleNamespace(alumni_id=5, current_employer="Old Bank")
    session = _Session(_alum())
    _apply(
        session,
        _fake_resp(
            payload={
                "profile.linkedin_url": "https://www.linkedin.com/in/jane-doe",
                "employment.current_employer": "Goldman Sachs",
                "contact.city": "Salt Lake City",
                "program.mentor_willing": "Yes",
            }
        ),
        monkeypatch,
        side_rows=({5: contact}, {5: job}, {}),
    )
    assert set(_fields(session)) == {
        "linkedin_url",  # the alumni core row: bare, no prefix
        "career.current_employer",  # NOT employment.*
        "contact.city",
        "engagement.mentor_willing",
    }


def test_every_survey_group_maps_to_a_real_staff_section_and_column():
    """The drift guard. If a survey field's group stopped resolving to a real
    section, or a column stopped existing on that section's table, survey history
    would quietly stop lining up with staff history — and nothing else would
    notice, because both paths would keep writing perfectly valid-looking rows."""
    models = {
        None: Alumni,
        "contact": AlumniContactInfo,
        "career": CurrentEmployment,
        "engagement": AlumniProgramEngagement,
    }
    for field in sr._FIELDS:
        assert field.group in sr._AUDIT_SECTION_BY_GROUP, field.key
        section = sr._AUDIT_SECTION_BY_GROUP[field.group]
        # Every prefix is a section the staff edit path already uses.
        assert section is None or section in SECTION_KEYS, field.key
        # ...and names a column that really is on that section's table, so
        # `career.current_employer` from here and from `update_alumni` are the
        # same column and not merely the same string.
        assert field.column in models[section].__table__.columns, field.key
        name = sr._audit_field_name(field)
        assert name == (field.column if section is None else f"{section}.{field.column}")


# ------------------------------------------------------- the summary row -----


def test_the_summary_row_is_kept_with_its_counts(monkeypatch):
    """KEPT, not replaced. `dropped` (unknown key) and `ignored` (known field,
    refused value) are not field changes, so no per-field row can carry them —
    and they are the only signal that a rename on either side of the wire is
    eating alumni answers silently."""
    session = _Session(_alum())
    _apply(
        session,
        _fake_resp(
            payload={
                "profile.linkedin_url": "https://www.linkedin.com/in/jane-doe",
                "profile.marital_status": "Definitely Not An Option",  # ignored
                "profile.nope": "x",  # dropped
            }
        ),
        monkeypatch,
    )
    summary = _summary(session)
    assert summary.field_name is None
    assert "survey_response=1" in summary.new_value
    assert "fields=3" in summary.new_value
    assert "written=1" in summary.new_value
    assert "dropped=1" in summary.new_value
    assert "ignored=1" in summary.new_value


def test_the_summary_row_comes_first(monkeypatch):
    """Ordering is load-bearing for readers that take the first audit row of an
    apply as its header (several existing tests do exactly that)."""
    session = _Session(_alum())
    _apply(
        session,
        _fake_resp(
            payload={"profile.linkedin_url": "https://www.linkedin.com/in/jane-doe"}
        ),
        monkeypatch,
    )
    assert _audits(session)[0].action_type == "apply_survey_response"


def test_field_rows_are_actioned_update_like_every_other_change(monkeypatch):
    """Version history filters on the action. A survey-applied change to a column
    is an update to that column — `source` is what says where it came from — so
    it must not hide behind a bespoke action the history query doesn't know."""
    session = _Session(_alum())
    _apply(
        session,
        _fake_resp(
            payload={"profile.linkedin_url": "https://www.linkedin.com/in/jane-doe"}
        ),
        monkeypatch,
    )
    rows = [a for a in _audits(session) if a.field_name]
    assert [a.action_type for a in rows] == ["update"]
    assert all(a.entity_type == "alumni" and a.entity_id == 5 for a in rows)


# ------------------------------------------------------------- photo only ----


def test_a_photo_only_apply_writes_the_summary_row_and_nothing_else(monkeypatch):
    """A headshot is not a column on the record, so there is no field, no old
    value and nothing to diff. The approval is still recorded — as one summary
    row saying no fields moved — rather than inventing a field-level row for a
    change that has no field."""
    calls = []

    async def fake_download(_bucket, path):
        calls.append(("download", path))
        # A REAL JPEG, not a magic number with filler behind it. The promotion
        # path re-encodes the bytes now (it no longer trusts a 16-byte sniff), so
        # undecodable filler would be discarded and this test would exercise the
        # drop path instead of the promotion it means to check.
        buffer = io.BytesIO()
        Image.new("RGB", (64, 64), (120, 140, 160)).save(buffer, format="JPEG")
        return buffer.getvalue()

    async def fake_upload(_bucket, key, _data, _ct):
        calls.append(("upload", key))

    async def fake_delete(_bucket, path):
        calls.append(("delete", path))

    monkeypatch.setattr(sr.supabase_storage, "download_object", fake_download)
    monkeypatch.setattr(sr.supabase_storage, "upload_object", fake_upload)
    monkeypatch.setattr(sr.supabase_storage, "delete_object", fake_delete)

    resp = _fake_resp(payload={}, staged_photo_path="survey-pending/1")
    session = _Session(_alum())
    _apply(session, resp, monkeypatch)

    assert [c[0] for c in calls] == ["download", "upload", "delete"]
    assert resp.staged_photo_path is None
    rows = _audits(session)
    assert len(rows) == 1
    assert rows[0].action_type == "apply_survey_response"
    assert rows[0].field_name is None
    assert "written=0" in rows[0].new_value
    # Still grouped and still sourced, so a photo approval is findable alongside
    # the field approvals rather than being an unlabelled orphan.
    assert rows[0].change_set_id is not None
    assert rows[0].source == AUDIT_SOURCE_SURVEY


# ------------------------------------------------------------- reject --------


def test_reject_writes_exactly_what_it_wrote_before(monkeypatch):
    """Nothing is written to the record on a reject, so there is nothing to
    capture. This test exists to pin that the capture work did NOT quietly start
    labelling or grouping a row that records no change."""
    resp = _fake_resp()
    session = _Session(_alum())

    async def fake_get_pending(_s, _rid):
        return resp

    monkeypatch.setattr(sr, "_get_pending", fake_get_pending)
    asyncio.run(sr.reject_response(session, 1, actor_user_id=9))

    rows = _audits(session)
    assert len(rows) == 1
    row = rows[0]
    assert row.action_type == "reject_survey_response"
    assert row.new_value == "survey_response=1"
    assert row.field_name is None and row.old_value is None
    assert row.change_set_id is None and row.source is None
    assert resp.status == "rejected"


def test_reject_of_a_populated_payload_still_writes_no_field_rows(monkeypatch):
    """The staged answers are discarded, not applied — a field row here would
    claim the record changed when it did not."""
    resp = _fake_resp(
        payload={
            "profile.linkedin_url": "https://www.linkedin.com/in/jane-doe",
            "contact.phone": "801-555-0100",
        }
    )
    session = _Session(_alum())

    async def fake_get_pending(_s, _rid):
        return resp

    monkeypatch.setattr(sr, "_get_pending", fake_get_pending)
    asyncio.run(sr.reject_response(session, 1, actor_user_id=9))
    assert _fields(session) == {}


# ------------------------------------------------------------------ PII ------


def test_no_old_or_new_value_ever_reaches_the_log(monkeypatch, caplog):
    """Old values are PII — a phone number, a home city, a birthday, an employer.
    They belong in ``audit_logs`` and nowhere else. The apply path logs counts and
    field KEYS; capture must not have widened that to values."""
    contact = types.SimpleNamespace(alumni_id=5, phone="801-555-0199", city="Provo")
    session = _Session(_alum())
    with caplog.at_level("DEBUG"):
        _apply(
            session,
            _fake_resp(
                payload={
                    "contact.phone": "801-555-0100",
                    "contact.city": "Salt Lake City",
                    "profile.nope": "x",  # forces the dropped-key warning to fire
                }
            ),
            monkeypatch,
            side_rows=({5: contact}, {}, {}),
        )
    for secret in ("801-555-0199", "801-555-0100", "Provo", "Salt Lake City"):
        assert secret not in caplog.text, secret
    # ...while the values really were captured to the audit trail.
    assert _fields(session)["contact.phone"] == ("801-555-0199", "801-555-0100")


# ------------------------------------------------- the engineer reroute ------


@pytest.fixture
def real_session():
    """A REAL SQLAlchemy session. The engineer guard is a ``before_flush`` hook,
    so a fake session that never flushes cannot observe it at all.

    ``audit_logs.audit_log_id`` is a Postgres-generated BigInteger, which SQLite
    renders as BIGINT and therefore does NOT autoincrement — the existing
    suppression tests work around that by setting the PK by hand, which is not an
    option here because ``apply_response`` constructs the rows itself. So the
    column is rendered as SQLite's INTEGER PRIMARY KEY (its rowid alias) for the
    life of this fixture and restored afterwards. DDL-time only, and Postgres is
    never involved in the suite.
    """
    column = AuditLog.__table__.c.audit_log_id
    original = column.type
    column.type = BigInteger().with_variant(Integer, "sqlite")
    engine = create_engine("sqlite://")
    try:
        AuditLog.__table__.create(engine)
        EngineerActionLog.__table__.create(engine)
        with Session(engine) as s:
            yield s
    finally:
        engine.dispose()
        column.type = original


class _RerouteSession:
    """Async surface over a real Session, so ``apply_response``'s ``add`` + commit
    drive a genuine flush. Only AuditLog rows are added by this apply (the payload
    is alumni-core only), so no other table is needed."""

    def __init__(self, inner, alum):
        self._inner = inner
        self._alum = alum

    async def execute(self, _stmt):
        return _Result(self._alum)

    def add(self, obj):
        self._inner.add(obj)

    async def flush(self):
        self._inner.flush()

    async def commit(self):
        self._inner.commit()


def test_an_engineers_apply_reroutes_the_new_field_rows_intact(
    real_session, monkeypatch
):
    """The engineer's audit rows are expunged and mirrored into the append-only
    oversight log. The field-level rows are new, so this pins that they go the
    same way — an engineer approving survey responses must not be able to make
    changes that leave no trace anywhere."""
    set_audit_actor(["engineer"])
    alum = _alum(gender="Female")
    session = _RerouteSession(real_session, alum)
    _apply(
        session,
        _fake_resp(
            payload={
                "profile.linkedin_url": "https://www.linkedin.com/in/jane-doe",
                "profile.gender": "Nonbinary",
            }
        ),
        monkeypatch,
    )

    # Nothing in the FERPA record-change trail...
    assert real_session.scalar(select(func.count()).select_from(AuditLog)) == 0
    # ...and everything in the oversight trail: the summary row plus both fields.
    rows = list(real_session.scalars(select(EngineerActionLog)))
    assert len(rows) == 3
    by_field = {r.field_name: (r.old_value, r.new_value) for r in rows if r.field_name}
    assert by_field == {
        "linkedin_url": (
            "https://www.linkedin.com/in/jane-old",
            "https://www.linkedin.com/in/jane-doe",
        ),
        "gender": ("Female", "Nonbinary"),
    }
    # The grouping and the provenance survive the crossing, or a suppressed
    # approval would read as three unrelated rows of unknown origin.
    assert len({r.change_set_id for r in rows}) == 1
    assert next(iter({r.change_set_id for r in rows})) is not None
    assert {r.source for r in rows} == {AUDIT_SOURCE_SURVEY}


def test_a_non_engineers_apply_still_lands_in_the_audit_trail(
    real_session, monkeypatch
):
    """The other half of the guard: the reroute must not swallow ordinary staff
    approvals."""
    set_audit_actor(["full_access"])
    session = _RerouteSession(real_session, _alum())
    _apply(
        session,
        _fake_resp(
            payload={"profile.linkedin_url": "https://www.linkedin.com/in/jane-doe"}
        ),
        monkeypatch,
    )
    assert real_session.scalar(select(func.count()).select_from(AuditLog)) == 2
    assert (
        real_session.scalar(select(func.count()).select_from(EngineerActionLog)) == 0
    )
