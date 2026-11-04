"""A second submission merges into the pending one (#655).

The link works for 7 days so an alum can come back. Two pending rows meant the
last one APPROVED won on shared fields, even when it was the older answer. Now a
repeat submission folds into the pending row, newest answer on top.

No DB and no network. The fake session filters on what the statements actually
ask for, and models `FOR UPDATE`: a locked read waits for the holder's commit, then
re-checks the row, which is what lets the race test below mean something. The real
Postgres behaviour it stands in for is described in `_live_reply`.
"""

import asyncio
import datetime
import types

import pytest
from sqlalchemy.dialects import postgresql

from app.core.errors import ServiceError
from app.models.note import Note
from app.models.survey_response import COMMENTS_KEY, SurveyResponse
from app.schemas.survey import (
    SURVEY_COMMENTS_MAX,
    SURVEY_COMMENTS_SEPARATOR,
    SURVEY_SUBMIT_MAX_TOTAL_BYTES,
)
from app.services import supabase_storage, survey_email
from app.services import survey_responses as sr

UTC = datetime.UTC
SEP = SURVEY_COMMENTS_SEPARATOR


class _Result:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj

    def first(self):
        return None


class _Db:
    """The state two sessions share."""

    def __init__(self, alum, rows):
        self.alum = alum
        self.rows = list(rows)
        self.locks: dict[int, asyncio.Lock] = {}
        self.stmts = []

    def lock(self, row_id):
        return self.locks.setdefault(row_id, asyncio.Lock())


class _Session:
    def __init__(self, db):
        self.db = db
        self.held = []
        self.added = []
        self.committed = 0

    def _matches(self, stmt):
        params = dict(stmt.compile().params)
        statuses = set()
        for key, value in params.items():
            if key.startswith("status"):
                statuses.update(value if isinstance(value, list | tuple) else [value])
        rows = [
            r
            for r in self.db.rows
            if r.status in statuses and not getattr(r, "superseded", False)
        ]
        return sorted(rows, key=lambda r: r.submitted_at, reverse=True)

    async def execute(self, stmt):
        await asyncio.sleep(0)
        froms = {getattr(f, "name", None) for f in stmt.get_final_froms()}
        if "survey_responses" not in froms:
            return _Result(self.db.alum)
        self.db.stmts.append(stmt)
        rows = self._matches(stmt)
        if not rows:
            return _Result(None)
        if stmt._for_update_arg is not None:
            lock = self.db.lock(rows[0].survey_response_id)
            await lock.acquire()
            self.held.append(lock)
            # Postgres re-checks the WHERE on the row it waited for.
            rows = self._matches(stmt)
        return _Result(rows[0] if rows else None)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if isinstance(obj, SurveyResponse) and obj.survey_response_id is None:
                obj.survey_response_id = 777

    async def commit(self):
        await asyncio.sleep(0)
        self.committed += 1
        now = datetime.datetime.now(UTC)
        for row in self.db.rows:
            if not isinstance(row.submitted_at, datetime.datetime):
                row.submitted_at = now
        for obj in self.added:
            if isinstance(obj, SurveyResponse) and obj not in self.db.rows:
                obj.submitted_at = now
                self.db.rows.append(obj)
        for lock in self.held:
            lock.release()
        self.held = []


def _alum(**kw):
    base = dict(
        alumni_id=5,
        archived=False,
        graduation_year=2020,
        first_name="Jane",
        last_name="Doe",
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def _row(payload=None, *, status="pending", response_id=9, days_ago=1, **kw):
    base = dict(
        survey_response_id=response_id,
        alumni_id=5,
        graduation_year=2020,
        payload=payload if payload is not None else {},
        status=status,
        staged_photo_path=None,
        cycle_seq=4,
        stage=1,
        fill_seconds=None,
        submitted_at=datetime.datetime.now(UTC) - datetime.timedelta(days=days_ago),
        reviewed_by_user_id=None,
        reviewed_at=None,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


@pytest.fixture
def record(monkeypatch):
    """What is on file: Provo, Acme, Analyst. Tests may add to it."""
    contact = types.SimpleNamespace(city="Provo")
    job = types.SimpleNamespace(current_employer="Acme", current_title="Analyst")

    async def side_rows(_session, _ids):
        return ({5: contact}, {5: job}, {})

    async def no_cycle(*_a, **_k):
        return (8, 2)

    monkeypatch.setattr(sr, "_load_side_rows", side_rows)
    monkeypatch.setattr(sr, "_sent_cycle_and_stage", no_cycle)
    monkeypatch.setattr(sr, "verify_survey_token", lambda _t: 5)
    return types.SimpleNamespace(contact=contact, job=job)


def _submit(db, fields=None, **kw):
    return asyncio.run(sr.submit_response(_Session(db), "tok", fields or {}, **kw))


def _db(*rows, **alum):
    return _Db(_alum(**alum), rows)


# ----------------------------------------------------------- the answers ----


def test_newest_answer_wins_on_a_field_in_both(record):
    row = _row({"contact.city": "Denver"})
    db = _db(row)
    result = _submit(db, {"contact.city": "Boise"})

    assert row.payload["contact.city"] == "Boise"
    assert result.survey_response_id == 9
    assert len(db.rows) == 1


def test_a_field_only_in_the_older_submission_is_kept(record):
    row = _row({"contact.city": "Denver"})
    _submit(_db(row), {"employment.current_title": "VP"})

    assert row.payload == {"contact.city": "Denver", "employment.current_title": "VP"}


def test_the_row_keeps_its_stamp_and_moves_to_now(record):
    row = _row({"contact.city": "Denver"}, days_ago=3)
    before = row.submitted_at
    _submit(_db(row), {"contact.city": "Boise"})

    assert row.status == "pending"
    assert (row.survey_response_id, row.cycle_seq, row.stage) == (9, 4, 1)
    assert row.submitted_at > before


def test_a_missing_stamp_is_filled_but_never_overwritten(record):
    row = _row({"contact.city": "Denver"}, cycle_seq=None, stage=None)
    _submit(_db(row), {"contact.city": "Boise"})

    assert (row.cycle_seq, row.stage) == (8, 2)


def test_the_first_fill_time_stands(record):
    row = _row({"contact.city": "Denver"}, fill_seconds=90)
    _submit(_db(row), {"contact.city": "Boise"}, fill_seconds=15)

    assert row.fill_seconds == 90


def test_a_fill_time_is_taken_when_the_row_has_none(record):
    row = _row({"contact.city": "Denver"})
    _submit(_db(row), {"contact.city": "Boise"}, fill_seconds=15)

    assert row.fill_seconds == 15


def test_change_count_is_the_fields_on_the_row_and_not_the_comment(record):
    row = _row({"contact.city": "Denver", COMMENTS_KEY: "hello"})
    result = _submit(_db(row), {"employment.current_title": "VP"})

    assert result.change_count == 2
    assert result.staged is True


# ------------------------------------------- touched-only vs the whole form --


def test_a_deliberate_revert_to_the_record_wins(record):
    # The page posts only touched fields, so this key is an answer: the alum
    # changed their city, came back and put it back. It must not be ignored.
    row = _row({"contact.city": "Denver"})
    _submit(_db(row), {"contact.city": "Provo"})

    assert row.payload["contact.city"] == "Provo"


def _whole_form(**overrides):
    return {
        "profile.first_name": "Jane",
        "profile.last_name": "Doe",
        "contact.city": "Provo",
        "employment.current_employer": "Acme",
        "employment.current_title": "Analyst",
        **overrides,
    }


def test_a_whole_form_echo_does_not_undo_the_first_edit(record):
    row = _row({"contact.city": "Denver"})
    _submit(_db(row), _whole_form())

    assert row.payload["contact.city"] == "Denver"


def test_a_whole_form_still_carries_its_real_changes(record):
    row = _row({"contact.city": "Denver", "employment.current_title": "VP"})
    _submit(_db(row), _whole_form(**{"employment.current_title": "Director"}))

    assert row.payload["contact.city"] == "Denver"
    assert row.payload["employment.current_title"] == "Director"


def test_a_whole_form_is_recognised_when_a_prefilled_value_cannot_be_staged(record):
    # "Separated" is off-list so it never reaches the payload, but the form did
    # send it, and that must not make this look like a list of edits.
    row = _row({"contact.city": "Denver"})
    _submit(
        _db(row, marital_status="Separated"),
        _whole_form(**{"profile.marital_status": "Separated"}),
    )

    assert row.payload["contact.city"] == "Denver"


def test_a_partial_form_is_not_mistaken_for_a_whole_one(record):
    row = _row({"contact.city": "Denver"})
    fields = _whole_form()
    del fields["employment.current_title"]
    _submit(_db(row), fields)

    assert row.payload["contact.city"] == "Provo"


def test_the_record_is_only_read_when_two_answers_disagree(record, monkeypatch):
    async def boom(*_a, **_k):
        raise AssertionError("a plain merge must not read the side tables")

    monkeypatch.setattr(sr, "_load_side_rows", boom)
    row = _row({"contact.city": "Denver"})
    _submit(_db(row), {"contact.city": "Denver", "employment.current_title": "VP"})

    assert row.payload["employment.current_title"] == "VP"


# -------------------------------------------------------------- comments ----


def test_both_comments_are_kept_oldest_first(record):
    row = _row({COMMENTS_KEY: "first"})
    _submit(_db(row), {}, comments="second")

    assert row.payload[COMMENTS_KEY] == f"first{SEP}second"


def test_a_comment_is_added_to_a_row_that_had_none(record):
    row = _row({"contact.city": "Denver"})
    _submit(_db(row), {}, comments="only")

    assert row.payload[COMMENTS_KEY] == "only"


def test_a_submission_without_a_comment_keeps_the_older_one(record):
    row = _row({COMMENTS_KEY: "first"})
    _submit(_db(row), {"contact.city": "Boise"})

    assert row.payload[COMMENTS_KEY] == "first"


def test_a_retry_does_not_repeat_a_comment(record):
    row = _row({COMMENTS_KEY: f"first{SEP}second"})
    db = _db(row)
    _submit(db, {}, comments="second")
    _submit(db, {}, comments="first")

    assert row.payload[COMMENTS_KEY] == f"first{SEP}second"


def test_comments_that_exactly_fit_the_cap_merge(record):
    older = "a" * 1000
    newer = "b" * (SURVEY_COMMENTS_MAX - len(older) - len(SEP))
    row = _row({COMMENTS_KEY: older})
    db = _db(row)
    _submit(db, {}, comments=newer)

    assert len(row.payload[COMMENTS_KEY]) == SURVEY_COMMENTS_MAX
    assert len(db.rows) == 1
    assert sr._payload_comment(row.payload) == f"{older}{SEP}{newer}"


def test_comments_one_over_the_cap_stage_a_separate_row_and_lose_nothing(record):
    older = "a" * 1000
    newer = "b" * (SURVEY_COMMENTS_MAX - len(older) - len(SEP) + 1)
    row = _row({"contact.city": "Denver", COMMENTS_KEY: older})
    db = _db(row)
    result = _submit(db, {"contact.city": "Boise"}, comments=newer)

    # Merging would have made both comments unreadable; nothing was trimmed.
    assert row.payload == {"contact.city": "Denver", COMMENTS_KEY: older}
    assert result.survey_response_id == 777
    assert db.rows[1].payload == {"contact.city": "Boise", COMMENTS_KEY: newer}


def test_applying_a_merged_row_puts_both_comments_in_one_note(record, monkeypatch):
    row = _row({COMMENTS_KEY: f"first{SEP}second"})
    session = _Session(_db(row))

    async def get_pending(_s, _rid):
        return row

    monkeypatch.setattr(sr, "_get_pending", get_pending)
    asyncio.run(sr.apply_response(session, 9, actor_user_id=1))

    notes = [o for o in session.added if isinstance(o, Note)]
    assert len(notes) == 1
    assert "first" in notes[0].body
    assert "second" in notes[0].body
    assert notes[0].body.index("first") < notes[0].body.index("second")


# ----------------------------------------------------------------- photos ---


@pytest.fixture
def storage(monkeypatch):
    calls = types.SimpleNamespace(uploads=[], deletes=[], delete_error=None)

    async def upload(bucket, path, data, _content_type):
        calls.uploads.append((bucket, path, data))

    async def delete(bucket, path):
        if calls.delete_error:
            raise calls.delete_error
        calls.deletes.append((bucket, path))

    monkeypatch.setattr(supabase_storage, "upload_object", upload)
    monkeypatch.setattr(supabase_storage, "delete_object", delete)
    return calls


def test_a_photo_only_repeat_returns_the_pending_row_to_attach_to(record):
    row = _row({"contact.city": "Denver"}, staged_photo_path="survey-pending/9")
    db = _db(row)
    result = _submit(db, {}, has_photo=True)

    assert result.survey_response_id == 9
    assert len(db.rows) == 1


def test_a_repeat_without_a_photo_keeps_the_staged_one(record):
    row = _row({"contact.city": "Denver"}, staged_photo_path="survey-pending/9")
    _submit(_db(row), {"contact.city": "Boise"})

    assert row.staged_photo_path == "survey-pending/9"


def test_the_newest_photo_replaces_the_older_on_the_same_key(record, storage):
    # `upload_object` upserts, so writing the row's own key IS the cleanup.
    row = _row(staged_photo_path="survey-pending/9")
    session = _Session(_db(row))

    async def execute(_stmt):
        return _Result(row)

    session.execute = execute
    asyncio.run(sr.stage_photo(session, "tok", 9, b"new", "image/jpeg"))

    assert storage.uploads == [("headshots", "survey-pending/9", b"new")]
    assert storage.deletes == []
    assert row.staged_photo_path == "survey-pending/9"


def test_an_older_photo_on_another_key_is_cleaned_up(record, storage):
    row = _row(staged_photo_path="survey-pending/legacy")
    session = _Session(_db(row))

    async def execute(_stmt):
        return _Result(row)

    session.execute = execute
    asyncio.run(sr.stage_photo(session, "tok", 9, b"new", "image/jpeg"))

    assert storage.deletes == [("headshots", "survey-pending/legacy")]
    assert row.staged_photo_path == "survey-pending/9"


def test_a_failed_cleanup_does_not_fail_the_saved_photo(record, storage):
    storage.delete_error = ServiceError("storage down")
    row = _row(staged_photo_path="survey-pending/legacy")
    session = _Session(_db(row))

    async def execute(_stmt):
        return _Result(row)

    session.execute = execute
    asyncio.run(sr.stage_photo(session, "tok", 9, b"new", "image/jpeg"))

    assert row.staged_photo_path == "survey-pending/9"
    assert session.committed == 1


# ------------------------------------------------- what must NOT be merged --


@pytest.mark.parametrize("status", ["applied", "rejected"])
def test_a_reviewed_submission_is_left_alone(record, status):
    row = _row({"contact.city": "Denver"}, status=status)
    db = _db(row)
    result = _submit(db, {"contact.city": "Boise"})

    assert row.payload == {"contact.city": "Denver"}
    assert result.survey_response_id == 777
    assert db.rows[1].payload == {"contact.city": "Boise"}
    assert db.rows[1].status == "pending"


def test_a_reset_superseded_row_is_not_merged(record):
    row = _row({"contact.city": "Denver"}, superseded=True)
    db = _db(row)
    result = _submit(db, {"contact.city": "Boise"})

    assert row.payload == {"contact.city": "Denver"}
    assert result.survey_response_id == 777


def test_the_lookup_is_the_shared_definition_of_a_live_reply(record):
    # The fake does not evaluate these predicates, so pin them on the SQL.
    db = _db(_row({"contact.city": "Denver"}))
    _submit(db, {"contact.city": "Boise"})
    sql = str(db.stmts[-1].compile(dialect=postgresql.dialect()))

    assert "FOR UPDATE" in sql
    assert "survey_reset_log" in sql
    assert "survey_responses.submitted_at >=" in sql
    assert "survey_responses.alumni_id =" in sql


def test_a_confirmation_upgrade_is_unchanged(record):
    row = _row({}, status=survey_email.STATUS_CONFIRMED)
    db = _db(row)
    result = _submit(db, {"contact.city": "Boise"})

    assert row.status == "pending"
    assert row.payload == {"contact.city": "Boise"}
    assert result.survey_response_id == 9
    assert len(db.rows) == 1


def test_a_confirmation_does_not_touch_a_pending_row(record):
    row = _row({"contact.city": "Denver"})
    db = _db(row)
    result = _submit(db, {}, confirmed_only=True)

    assert row.payload == {"contact.city": "Denver"}
    assert result.survey_response_id == 9
    assert len(db.rows) == 1


def test_the_newest_of_several_pending_rows_takes_the_merge(record):
    older = _row({"contact.city": "Older"}, response_id=1, days_ago=5)
    newer = _row({"contact.city": "Newer"}, response_id=2, days_ago=1)
    db = _db(older, newer)
    result = _submit(db, {"employment.current_title": "VP"})

    assert result.survey_response_id == 2
    assert older.payload == {"contact.city": "Older"}
    assert newer.payload == {"contact.city": "Newer", "employment.current_title": "VP"}


# ------------------------------------------------------------------- size ---


def test_repeat_submissions_cannot_grow_a_row_past_the_cap(record):
    # `bool` answers have no length rule, so each key can hold a request's worth
    # of junk. One request is legal; a row stacking them must not be.
    keys = [f.key for f in sr._FIELDS if f.kind == "bool"]
    junk = "x" * 30_000
    db = _db(_row({keys[0]: junk}))
    for key in keys[1:]:
        _submit(db, {key: junk})

    assert len(db.rows) > 1
    for row in db.rows:
        assert sr._fields_bytes(row.payload) <= SURVEY_SUBMIT_MAX_TOTAL_BYTES
    # Every answer is still on some row.
    assert {k for row in db.rows for k in row.payload} == set(keys)


def test_resubmitting_the_same_answers_does_not_grow_the_row(record):
    row = _row({"contact.city": "Denver", COMMENTS_KEY: "note"})
    db = _db(row)
    before = dict(row.payload)
    for _ in range(25):
        _submit(db, {"contact.city": "Denver"}, comments="note")

    assert row.payload == before
    assert len(db.rows) == 1


def test_a_full_comment_budget_does_not_grow_the_row(record):
    row = _row({COMMENTS_KEY: "a" * SURVEY_COMMENTS_MAX})
    db = _db(row)
    for n in range(3):
        _submit(db, {}, comments=f"more {n}")

    assert len(row.payload[COMMENTS_KEY]) == SURVEY_COMMENTS_MAX
    assert db.rows[1].payload[COMMENTS_KEY] == f"more 0{SEP}more 1{SEP}more 2"


# -------------------------------------------------------------------- jobs ---


def test_the_role_on_file_is_archived_when_a_merged_row_is_applied(record, monkeypatch):
    # The merge leaves the archiving to `apply_response`, which compares the record
    # with what the merged payload wrote. Newest answer on top, old job preserved.
    row = _row(
        {
            "employment.current_employer": "Beta",
            "employment.current_title": "Associate",
        }
    )
    db = _db(row)
    _submit(db, {"employment.current_employer": "Gamma"})
    assert row.payload["employment.current_employer"] == "Gamma"

    archived = []

    async def get_pending(_s, _rid):
        return row

    async def not_in_history(*_a):
        return False

    async def archive(_session, _alumni_id, outgoing):
        archived.append(outgoing)
        return types.SimpleNamespace()

    monkeypatch.setattr(sr, "_get_pending", get_pending)
    monkeypatch.setattr(
        sr,
        "career_snapshot",
        lambda job: {
            "current_employer": job.current_employer,
            "current_title": job.current_title,
        },
    )
    monkeypatch.setattr(sr, "history_has_role", not_in_history)
    monkeypatch.setattr(sr, "archive_current_role", archive)
    monkeypatch.setattr(sr, "audit_role_archive", lambda *_a, **_k: None)
    asyncio.run(sr.apply_response(_Session(db), 9, actor_user_id=1))

    assert [r["current_employer"] for r in archived] == ["Acme"]
    assert record.job.current_employer == "Gamma"
    assert record.job.current_title == "Associate"


# ------------------------------------------------------------- concurrency ---


async def _race(db, *submissions):
    return await asyncio.gather(
        *(
            sr.submit_response(_Session(db), "tok", fields, comments=comment)
            for fields, comment in submissions
        )
    )


def test_two_submissions_at_once_lose_nothing(record):
    row = _row({"contact.city": "Denver"})
    db = _db(row)
    results = asyncio.run(
        _race(
            db,
            ({"employment.current_title": "VP"}, "one"),
            ({"employment.current_employer": "Beta"}, "two"),
        )
    )

    assert len(db.rows) == 1
    assert {r.survey_response_id for r in results} == {9}
    assert row.payload["contact.city"] == "Denver"
    assert row.payload["employment.current_title"] == "VP"
    assert row.payload["employment.current_employer"] == "Beta"
    assert set(row.payload[COMMENTS_KEY].split(SEP)) == {"one", "two"}


def test_two_submissions_racing_a_confirmation_lose_nothing(record):
    # The first upgrades it. The second blocked on `confirmed`, which drops out on
    # the re-check, and now finds the same row pending and merges into it.
    row = _row({}, status=survey_email.STATUS_CONFIRMED)
    db = _db(row)
    asyncio.run(
        _race(
            db,
            ({"employment.current_title": "VP"}, None),
            ({"employment.current_employer": "Beta"}, None),
        )
    )

    assert len(db.rows) == 1
    assert row.status == "pending"
    assert row.payload == {
        "employment.current_title": "VP",
        "employment.current_employer": "Beta",
    }


def test_a_row_reviewed_while_waiting_for_the_lock_is_not_merged_into(record):
    row = _row({"contact.city": "Denver"})
    db = _db(row)
    lock = db.lock(9)

    async def scenario():
        await lock.acquire()  # staff hold the row, as `_get_pending` does
        submit = asyncio.create_task(
            sr.submit_response(_Session(db), "tok", {"contact.city": "Boise"})
        )
        await asyncio.sleep(0.01)
        row.status = "applied"
        lock.release()
        return await submit

    result = asyncio.run(scenario())

    assert row.payload == {"contact.city": "Denver"}
    assert result.survey_response_id == 777
