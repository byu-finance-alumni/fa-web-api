"""The survey email's copy is editable, stored, and actually SENT (#524).

Before this, the "Edit email message" box on the Needs Surveying page wrote to
the browser's ``localStorage`` and the send built its subject and body from
constants in ``app/services/survey_email.py``. An edit was therefore per-browser,
per-machine, invisible to the other Career Director, and reached no alum at all.

The questions pinned here are, in order of what would hurt most if it broke:

* does a stored edit reach the REAL send — both the HTML and the plaintext part
  of the message that goes to Resend (``test_a_stored_edit_reaches_...``)?
* can this feature ever produce an EMPTY or unsent email — a missing row, a blank
  column, an unreadable table? Every one of those must resolve to the Career
  Directors' original copy, because a feature that changes what an email says
  must not be able to stop it being sent;
* is staff-authored copy still escaped on its way into the HTML part;
* can the on-file field picker put the email's field list out of step with the
  survey form? It must be structurally incapable of it — it can only hide rows,
  never add or reorder one;
* and is the whole thing gated on ``surveys.manage``, so a view-only user cannot
  rewrite what alumni are about to be emailed.
"""

import asyncio
import datetime
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.sql.dml import Delete

from app.api.dependencies import auth as auth_deps
from app.core.capabilities import DEFAULT_GRANTS
from app.core.database import get_session
from app.core.errors import InvalidRequestError
from app.core.roles import RoleName
from app.core.security import AuthorizationError
from app.main import app
from app.models.audit import AuditLog
from app.models.survey_email_message import SurveyEmailMessage
from app.schemas.auth import UserContext
from app.services import survey_email, survey_message
from app.services.survey_email import Recipient, render_survey_email
from app.services.survey_message import (
    DEFAULT_CLOSING,
    DEFAULT_INTRO,
    DEFAULT_MESSAGE,
    DEFAULT_SUBJECT,
    ON_FILE_FIELDS,
    SurveyMessage,
)
from tests.survey_fakes import SendLogSession


class _FakeSettings:
    survey_token_secret = "unit-test-secret"
    survey_from_email = "test@jakegunnell.com"
    survey_from_name = "BYU Finance Alumni"
    survey_app_base_url = "https://finance.alumni.byu.edu"
    resend_api_key = "re_test_key"
    survey_usage_baseline_at = None
    survey_usage_baseline_today = 0
    survey_usage_baseline_month = 0


@pytest.fixture
def fake_settings(monkeypatch):
    settings = _FakeSettings()
    monkeypatch.setattr(survey_email, "get_settings", lambda: settings)
    return settings


def _recipient(alumni_id: int = 1) -> Recipient:
    """A recipient carrying the FULL on-file list, which is what
    ``_load_recipients`` always builds — the selection is applied at render."""
    return Recipient(
        alumni_id,
        f"Alum{alumni_id}",
        f"alum{alumni_id}@example.com",
        tuple((label, f"value-{i}") for i, label in enumerate(ON_FILE_FIELDS)),
    )


# ------------------------------------------------- the canonical field list ---


def test_every_canonical_field_has_a_builder_and_vice_versa():
    """The list the picker validates against and the list the email can fill are
    ONE list. Drift between them is the bug class this project keeps re-growing:
    a label in the picker with no builder is a row that silently vanishes from
    the email, and a builder with no label is a field nobody can ever select."""
    assert set(survey_email._ON_FILE_BUILDERS) == set(ON_FILE_FIELDS)
    assert len(ON_FILE_FIELDS) == len(set(ON_FILE_FIELDS))  # no duplicates


def test_build_on_file_returns_every_field_in_canonical_order():
    alum = SimpleNamespace(
        employment_status="Employed full-time",
        spouse_first_name="Dana",
        spouse_last_name="Reyes",
        linkedin_url="https://linkedin.test/in/x",
        graduate_degree="MBA",
        graduate_school="BYU",
        graduate_graduation_year=2027,
        other_designations="CFA",
    )
    contact = SimpleNamespace(
        city="Provo",
        state="Utah",
        country="United States",
        personal_email="a@example.com",
        work_email=None,
    )
    job = SimpleNamespace(
        current_employer="Goldman Sachs",
        current_title="Analyst",
        current_industry="Investment Banking",
        current_industry_secondary=None,
        current_city="New York",
        current_state="New York",
        current_country="United States",
    )
    rows = survey_email._build_on_file(alum, contact, job)
    assert tuple(label for label, _ in rows) == ON_FILE_FIELDS
    values = dict(rows)
    assert values["Company"] == "Goldman Sachs"
    assert values["Spouse name"] == "Dana Reyes"
    # Nothing on file still renders a row, as an em dash — unchanged behaviour.
    assert values["Work email"] == "—"
    assert values["Secondary industry"] == "—"


# --------------------------------------------------------- field selection ----


def test_canonical_fields_reorders_dedupes_and_defaults():
    # Order comes from ON_FILE_FIELDS, never from the caller: the on-file box
    # reads in the order the survey asks its questions.
    assert survey_message.canonical_fields(["Title", "Company", "Title"]) == (
        "Company",
        "Title",
    )
    assert survey_message.canonical_fields(None) == ON_FILE_FIELDS
    assert survey_message.canonical_fields([]) == ()


def test_canonical_fields_refuses_a_field_the_email_cannot_build():
    """The picker can only ever HIDE rows. An invented label is refused at the
    moment someone saves it, not discovered as a missing row in a sent email."""
    with pytest.raises(InvalidRequestError):
        survey_message.canonical_fields(["Company", "Favourite colour"])


def test_a_selection_filters_the_on_file_box_in_both_parts(fake_settings):
    message = SurveyMessage(
        subject="S", intro="I", closing="C", on_file_fields=("Company", "Title")
    )
    _, html, text = render_survey_email(
        _recipient(), "https://x.test/survey/tok", message
    )
    for part in (html, text):
        assert "Company" in part and "Title" in part
        assert "LinkedIn profile" not in part
        assert "Spouse name" not in part


def test_an_empty_selection_omits_the_on_file_box_entirely(fake_settings):
    message = SurveyMessage(subject="S", intro="I", closing="C", on_file_fields=())
    _, html, text = render_survey_email(
        _recipient(), "https://x.test/survey/tok", message
    )
    assert "what we have on file" not in html
    assert "what we have on file" not in text
    # The link and the copy still go out — hiding the box is not hiding the email.
    assert "https://x.test/survey/tok" in html
    assert "https://x.test/survey/tok" in text


# ---------------------------------------------------------------- defaults ----


def test_no_message_renders_the_career_directors_copy(fake_settings):
    """An untouched deployment sends byte for byte what it sent before #524."""
    subject, html, text = render_survey_email(_recipient(), "https://x.test/s/t")
    assert subject == DEFAULT_SUBJECT
    assert DEFAULT_INTRO.split("\n\n")[0] in text
    assert "Tanya Harmon & Amy Densley" in text
    assert "Tanya Harmon &amp; Amy Densley" in html
    # Every canonical field, because the default selection is all of them.
    assert all(label in text for label in ON_FILE_FIELDS)


def test_a_blank_column_falls_back_field_by_field():
    """A half-written row must not produce an email with an empty subject. Each
    field falls back on its own."""
    row = SurveyEmailMessage(
        id=1, subject="  ", intro="Custom intro", closing="", on_file_fields=None
    )
    resolved = survey_message._resolve(row)
    assert resolved.subject == DEFAULT_SUBJECT
    assert resolved.intro == "Custom intro"
    assert resolved.closing == DEFAULT_CLOSING
    assert resolved.on_file_fields == ON_FILE_FIELDS


def test_a_stored_label_the_code_no_longer_knows_is_dropped():
    """Re-filtered at render time, so a label removed from the code (or inserted
    by hand in psql) cannot ask for a row the builders cannot fill."""
    row = SurveyEmailMessage(
        id=1,
        subject="S",
        intro="I",
        closing="C",
        on_file_fields=["Title", "Retired field", "Company"],
    )
    assert survey_message._resolve(row).on_file_fields == ("Company", "Title")


def test_get_for_send_is_total_when_the_store_is_unreadable():
    """The safety property. An unreadable table means the built-in wording, never
    a failed send — a feature that changes what an email says must not be able to
    stop one going out."""

    class _Broken:
        async def execute(self, stmt):
            raise RuntimeError("relation survey_email_message does not exist")

    assert asyncio.run(survey_message.get_for_send(_Broken())) == DEFAULT_MESSAGE


def test_get_for_send_with_no_row_is_the_default():
    assert asyncio.run(survey_message.get_for_send(_MessageSession())) == DEFAULT_MESSAGE


# -------------------------------------------------------------- validation ----


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("subject", "   "),
        ("intro", ""),
        ("closing", "\n\n"),
        ("subject", "x" * (survey_message.SUBJECT_MAX_CHARS + 1)),
        ("intro", "x" * (survey_message.BODY_MAX_CHARS + 1)),
        ("closing", "x" * (survey_message.BODY_MAX_CHARS + 1)),
        # A newline in a SUBJECT is header injection, not formatting.
        ("subject", "Two\nlines"),
        # A zero-width space makes the stored text read differently from what
        # was typed. Same rule the alumni email/URL gates use.
        ("intro", "Hello​there"),
        ("closing", "Regards\x07"),
    ],
)
def test_a_field_that_would_break_the_email_is_refused(field, value):
    payload = {"subject": "S", "intro": "I", "closing": "C", field: value}
    with pytest.raises(InvalidRequestError):
        asyncio.run(
            survey_message.set_message(
                _MessageSession(), on_file_fields=[], actor_user_id=1, **payload
            )
        )


def test_a_textarea_crlf_is_normalised_not_rejected():
    """A browser textarea submits CRLF. Leaving the CR in would both trip the
    control-character check and put a stray carriage return into the plaintext
    part of the email."""
    session = _MessageSession()
    asyncio.run(
        survey_message.set_message(
            session,
            subject="S",
            intro="Para one\r\n\r\nPara two",
            closing="Bye\r\nJake",
            on_file_fields=["Company"],
            actor_user_id=4,
        )
    )
    assert session.row.intro == "Para one\n\nPara two"
    assert session.row.closing == "Bye\nJake"
    assert "\r" not in session.row.intro + session.row.closing


# ----------------------------------------------------------------- storage ----


class _MessageSession:
    """A session with a real single ``survey_email_message`` row.

    Enough to exercise the read/write/reset triangle without a database: the two
    reads the service issues are told apart by how many things the SELECT names
    (the console read joins ``users`` for the editor's "last changed by")."""

    def __init__(self, row: SurveyEmailMessage | None = None, email: str | None = None):
        self.row = row
        self.email = email
        self.added: list = []
        self.commits = 0

    async def execute(self, stmt):
        if isinstance(stmt, Delete):
            existed = self.row is not None
            self.row = None
            return SimpleNamespace(rowcount=1 if existed else 0)
        joined = len(getattr(stmt, "column_descriptions", [])) > 1
        if joined:
            row = None if self.row is None else (self.row, self.email)
        else:
            row = self.row
        return SimpleNamespace(
            first=lambda: row, scalar_one_or_none=lambda: self.row
        )

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, SurveyEmailMessage):
            self.row = obj

    async def commit(self):
        self.commits += 1

    @property
    def audits(self) -> list:
        return [a for a in self.added if isinstance(a, AuditLog)]


def test_nothing_stored_reads_as_the_defaults_and_not_customized():
    read = asyncio.run(survey_message.get_message(_MessageSession()))
    assert read.subject == DEFAULT_SUBJECT
    assert read.intro == DEFAULT_INTRO
    assert read.closing == DEFAULT_CLOSING
    assert read.on_file_fields == list(ON_FILE_FIELDS)
    assert read.is_customized is False
    assert read.updated_at is None and read.updated_by_email is None


def test_a_row_holding_the_default_copy_is_still_not_customized():
    """"Customised" compares against the DEFAULTS, never "does a row exist" —
    otherwise saving the message unchanged would light up the editor's
    "Reset to default" affordance for a change nobody made."""
    row = SurveyEmailMessage(
        id=1,
        subject=DEFAULT_SUBJECT,
        intro=DEFAULT_INTRO,
        closing=DEFAULT_CLOSING,
        on_file_fields=list(ON_FILE_FIELDS),
    )
    read = asyncio.run(survey_message.get_message(_MessageSession(row)))
    assert read.is_customized is False


def test_a_stored_edit_reads_back_with_its_author():
    row = SurveyEmailMessage(
        id=1,
        subject="Quick favour",
        intro="Hi there.",
        closing="Thanks!",
        on_file_fields=["Company", "Title"],
        updated_by_user_id=9,
    )
    row.updated_at = datetime.datetime(2026, 9, 9, 12, tzinfo=datetime.UTC)
    read = asyncio.run(
        survey_message.get_message(_MessageSession(row, email="tanya@byu.edu"))
    )
    assert read.subject == "Quick favour"
    assert read.on_file_fields == ["Company", "Title"]
    assert read.is_customized is True
    assert read.updated_by_email == "tanya@byu.edu"
    assert read.updated_at == datetime.datetime(2026, 9, 9, 12, tzinfo=datetime.UTC)


def test_set_message_stores_canonical_order_and_does_not_commit():
    """The service does not commit — the route commits alongside its audit row,
    so the copy and the record of who wrote it land together or not at all."""
    session = _MessageSession()
    asyncio.run(
        survey_message.set_message(
            session,
            subject="  Trimmed  ",
            intro="Intro",
            closing="Closing",
            on_file_fields=["Title", "Company"],
            actor_user_id=7,
        )
    )
    assert session.row.subject == "Trimmed"
    assert session.row.on_file_fields == ["Company", "Title"]
    assert session.row.updated_by_user_id == 7
    assert session.commits == 0


def test_reset_deletes_the_override_and_reports_whether_there_was_one():
    row = SurveyEmailMessage(id=1, subject="S", intro="I", closing="C")
    session = _MessageSession(row)
    assert asyncio.run(survey_message.reset_message(session)) is True
    assert session.row is None
    # A second reset is not an error — see the route docstring.
    assert asyncio.run(survey_message.reset_message(session)) is False


# ------------------------------------------------------------------ escape ----


def test_staff_copy_is_escaped_into_the_html_part(fake_settings):
    """Staff-authored copy is now untrusted-ish input rendered into an email. The
    escape runs BEFORE the paragraph breaks are added, so the only markup those
    two replaces can introduce is </p><p> and <br>."""
    message = SurveyMessage(
        subject="S",
        intro='<script>alert(1)</script>\n\n<b>bold</b>',
        closing='<img src=x onerror="alert(2)">',
        on_file_fields=(),
    )
    _, html, text = render_survey_email(_recipient(), "https://x.test/s/t", message)
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "onerror" not in html or "&lt;img" in html
    assert "<b>bold</b>" not in html
    # The paragraph break the intro asked for IS still applied, to escaped text.
    assert '</p><p style="margin:0 0 12px;">' in html
    # The plaintext part is not escaped (it is not markup) but is unchanged.
    assert "<script>alert(1)</script>" in text


# ------------------------------------- the send path actually reads the copy ---


class _SendSession(SendLogSession):
    @property
    def committed(self):
        return self.commits


def _capture_send(monkeypatch, *, message):
    """Wire a real send: 2 recipients, Resend faked, the stored copy faked."""
    sent: list[list[dict]] = []

    async def fake_load(session, year):
        return [_recipient(1), _recipient(2)]

    async def fake_batch(emails):
        sent.append(emails)
        return (None, None)

    async def fake_message(session):
        return message

    monkeypatch.setattr(survey_email, "_load_recipients", fake_load)
    monkeypatch.setattr(survey_email, "_send_batch", fake_batch)
    monkeypatch.setattr(survey_message, "get_for_send", fake_message)
    return sent


def test_a_stored_edit_reaches_the_html_and_the_plaintext_of_a_real_send(
    fake_settings, monkeypatch
):
    """THE POINT OF #524. Everything else here is a guard rail; this is the bug.

    A staff edit has to survive the whole path — ``send_campaign`` ->
    ``send_survey_stage`` -> ``_send_and_log`` -> ``_build_survey_email`` ->
    ``render_survey_email`` -> the dict handed to Resend — and land in BOTH parts
    of the message, because a mail client may render either."""
    message = SurveyMessage(
        subject="Two minutes for the BYU Finance alumni team?",
        intro="We are updating our records.\n\nIt takes about two minutes.",
        closing="Thank you!\nTanya & Amy",
        on_file_fields=("Company", "Title"),
    )
    sent = _capture_send(monkeypatch, message=message)

    result = asyncio.run(
        survey_email.send_campaign(
            _SendSession(), graduation_year=1900, actor_user_id=1, dry_run=False
        )
    )

    assert result.sent == 2
    (batch,) = sent
    assert len(batch) == 2
    for email in batch:
        assert email["subject"] == "Two minutes for the BYU Finance alumni team?"
        for part in (email["html"], email["text"]):
            assert "We are updating our records." in part
            assert "Tanya &amp; Amy" in part or "Tanya & Amy" in part
            assert "Company" in part and "Title" in part
            # The deselected fields are gone from the sent message, not merely
            # from the preview.
            assert "LinkedIn profile" not in part
        # None of the built-in copy survives an edit that replaced it.
        assert DEFAULT_SUBJECT not in email["subject"]
        assert "one of the greatest strengths" not in email["text"]


def test_an_unedited_send_is_unchanged(fake_settings, monkeypatch):
    """No row stored -> the send is exactly what it was before this feature."""
    sent = _capture_send(monkeypatch, message=DEFAULT_MESSAGE)
    asyncio.run(
        survey_email.send_campaign(
            _SendSession(), graduation_year=1900, actor_user_id=1, dry_run=False
        )
    )
    (batch,) = sent
    assert batch[0]["subject"] == DEFAULT_SUBJECT
    assert "Tanya Harmon & Amy Densley" in batch[0]["text"]
    assert all(label in batch[0]["text"] for label in ON_FILE_FIELDS)


def test_the_copy_is_read_once_for_the_whole_send(fake_settings, monkeypatch):
    """Read once, not per recipient or per batch: an edit saved while a cohort is
    going out must not make the first batch read differently from the last."""
    reads = {"n": 0}

    async def counting(session):
        reads["n"] += 1
        return DEFAULT_MESSAGE

    async def fake_load(session, year):
        return [_recipient(i) for i in range(1, 151)]  # two Resend batches

    async def fake_batch(emails):
        return (None, None)

    async def no_cap(session):
        return None

    from app.services import survey_schedule

    monkeypatch.setattr(survey_schedule, "_run_allowance", no_cap)
    monkeypatch.setattr(survey_email, "_load_recipients", fake_load)
    monkeypatch.setattr(survey_email, "_send_batch", fake_batch)
    monkeypatch.setattr(survey_message, "get_for_send", counting)

    asyncio.run(
        survey_email.send_campaign(
            _SendSession(), graduation_year=1900, actor_user_id=1, dry_run=False
        )
    )
    assert reads["n"] == 1


def test_a_dry_run_does_not_read_the_copy(fake_settings, monkeypatch):
    """A preview sends nothing, so it must not pay for the read — and, more
    importantly, adding the read must not have moved a query into a path that had
    none."""

    async def boom(session):  # pragma: no cover - must not be called
        raise AssertionError("a dry run must not read the stored copy")

    async def fake_load(session, year):
        return [_recipient(1)]

    monkeypatch.setattr(survey_email, "_load_recipients", fake_load)
    monkeypatch.setattr(survey_message, "get_for_send", boom)
    result = asyncio.run(
        survey_email.send_campaign(
            _SendSession(), graduation_year=1900, actor_user_id=1, dry_run=True
        )
    )
    assert result.sent == 0


# ------------------------------------------------------------------ routes ----

_STAFF = UserContext(
    user_id=7,
    auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
    email="tanya@byu.edu",
    roles=[RoleName.FULL_ACCESS.value],
)


def _client(session):
    async def _override():
        yield session

    app.dependency_overrides[get_session] = _override
    app.dependency_overrides[auth_deps.require_surveys_manage] = lambda: _STAFF
    return TestClient(app, raise_server_exceptions=False)


def test_get_returns_the_defaults_when_nothing_is_stored():
    session = _MessageSession()
    try:
        with _client(session) as c:
            resp = c.get("/survey/message")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    body = resp.json()
    assert body["subject"] == DEFAULT_SUBJECT
    assert body["is_customized"] is False
    assert body["on_file_fields"] == list(ON_FILE_FIELDS)
    assert body["updated_by_email"] is None


def test_put_stores_the_copy_audits_it_and_commits_once():
    session = _MessageSession()
    try:
        with _client(session) as c:
            resp = c.put(
                "/survey/message",
                json={
                    "subject": "Quick favour",
                    "intro": "Hi there.",
                    "closing": "Thanks!",
                    "on_file_fields": ["Title", "Company"],
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["subject"] == "Quick favour"
    # Canonical order, not the order the request happened to use.
    assert body["on_file_fields"] == ["Company", "Title"]
    assert body["is_customized"] is True

    (audit,) = session.audits
    assert audit.action_type == "update_survey_message"
    assert audit.entity_type == "survey_message"
    assert audit.user_id == 7
    # The trail records THAT the outbound copy changed, not the prose.
    assert "Quick favour" not in (audit.new_value or "")
    assert session.commits == 1


def test_put_refuses_a_field_the_email_cannot_build():
    session = _MessageSession()
    try:
        with _client(session) as c:
            resp = c.put(
                "/survey/message",
                json={
                    "subject": "S",
                    "intro": "I",
                    "closing": "C",
                    "on_file_fields": ["Favourite colour"],
                },
            )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 422
    assert session.added == []  # nothing stored, nothing audited


def test_reset_restores_the_defaults_and_audits():
    row = SurveyEmailMessage(
        id=1, subject="Edited", intro="i", closing="c", on_file_fields=["Company"]
    )
    session = _MessageSession(row)
    try:
        with _client(session) as c:
            resp = c.post("/survey/message/reset")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["subject"] == DEFAULT_SUBJECT
    assert body["is_customized"] is False
    (audit,) = session.audits
    assert audit.action_type == "reset_survey_message"
    assert audit.new_value == "default"
    assert session.commits == 1


def test_resetting_already_default_copy_is_a_clean_success():
    """The recovery path from wording that reads badly. A second click must not
    land as an error — same reasoning as not rate-limiting maintenance-mode
    disable. The audit row still distinguishes the two."""
    session = _MessageSession()
    try:
        with _client(session) as c:
            resp = c.post("/survey/message/reset")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    (audit,) = session.audits
    assert audit.new_value == "already_default"


# ------------------------------------------------------------------- gating ---


@pytest.mark.parametrize(
    "role", [RoleName.VIEW_ONLY.value, RoleName.STUDENT.value]
)
def test_a_view_only_user_may_not_touch_the_copy(role):
    """This is console copy for an outbound campaign to alumni. `surveys.manage`,
    the same capability as the year picker, the send config and "Send now"."""
    ctx = UserContext(
        user_id=3,
        auth_user_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        email="viewer@byu.edu",
        roles=[role],
    )
    with pytest.raises(AuthorizationError):
        asyncio.run(auth_deps.require_surveys_manage(ctx, dict(DEFAULT_GRANTS)))


def _all_routes(router):
    for route in getattr(router, "routes", []):
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _all_routes(inner)
        elif hasattr(route, "routes"):
            yield from _all_routes(route)
        else:
            yield route


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/survey/message", "GET"),
        ("/survey/message", "PUT"),
        ("/survey/message/reset", "POST"),
    ],
)
def test_every_copy_route_is_wired_to_that_guard(path, method):
    """A guard that isn't attached protects nothing, so pin the wiring too."""
    route = next(
        r
        for r in _all_routes(app)
        if getattr(r, "path", None) == path and method in getattr(r, "methods", set())
    )
    guards = set()
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        guards.add(dep.call)
        stack.extend(dep.dependencies)
    assert auth_deps.require_surveys_manage in guards
