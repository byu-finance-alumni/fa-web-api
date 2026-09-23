"""The names behind the Progress tab's `replied` and `confirmed` counts (#836).

The whole contract is that a hover list is exactly as long as the number it
hangs off, so these run the real SQL against the in-memory SQLite world from the
follow-up suite and compare the list with the count computed by
`list_schedules` over the SAME rows — rather than asserting either one against a
hand-picked expectation that could agree with one and not the other.
"""

import asyncio
import datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.services import survey_schedule as ss
from tests.test_survey_followup import _NOW, _YEAR, _ctx, _ddl, _Fixture, _get


@pytest.fixture
def db():
    # The follow-up suite's SQLite world (see its `db` fixture for why
    # StaticPool and check_same_thread), rebuilt here rather than imported so
    # the fixture name is not an unused import.
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as conn:
        _ddl(conn)
    with Session(engine) as session:
        yield _Fixture(session)
    engine.dispose()


def _responders(db, year=_YEAR):
    return asyncio.run(ss.list_responders(db.session, year))


def _item(db, year=_YEAR):
    items = asyncio.run(ss.list_schedules(db.session))
    return next(i for i in items if i.graduation_year == year)


def _reset(db, alumni_id, *, seq=1, at=None):
    db.conn.execute(
        text(
            "INSERT INTO survey_reset_log (alumni_id, reset_seq, reset_at)"
            " VALUES (:a, :s, :t)"
        ),
        {"a": alumni_id, "s": seq, "t": at or _NOW},
    )


def _world(db):
    """A cohort with every kind of answer in it, plus the cases that must NOT
    count: a rejected reply, a reply outside the annual window, a previous
    cycle's replier, and an alum who submitted twice."""
    db.schedule(cycle=2)
    for i, last in enumerate(
        ["Young", "Adams", "Moss", "Baker", "Cole", "Diaz", "Evans"], start=1
    ):
        db.alum(i, last_name=last)
        db.sent(i, (0,), cycle=2)
    db.replied(1, "applied")
    db.replied(2, "confirmed")
    db.replied(3, "pending")
    db.replied(3, "confirmed")  # twice — still one person in each list
    db.replied(4, "rejected")  # staff threw it away: not a reply
    db.replied(
        5, "confirmed", when=_NOW - datetime.timedelta(days=400)
    )  # outside the re-survey window
    # Replied, but only ever emailed by the PREVIOUS campaign.
    db.alum(8, last_name="Ford")
    db.sent(8, (0,), cycle=1)
    db.replied(8, "confirmed")


def test_the_lists_match_the_counts(db):
    _world(db)
    item = _item(db)
    got = _responders(db)
    assert len(got.replied) == item.replied == 3
    assert len(got.confirmed) == item.confirmed == 2


def test_the_lists_hold_the_right_people_sorted_by_name(db):
    _world(db)
    got = _responders(db)
    assert [r.name for r in got.replied] == ["A2 Adams", "A3 Moss", "A1 Young"]
    assert [r.alumni_id for r in got.confirmed] == [2, 3]
    # Everyone who said "looks good" also replied.
    assert {r.alumni_id for r in got.confirmed} <= {r.alumni_id for r in got.replied}


def test_only_the_minimal_fields_are_returned(db):
    _world(db)
    assert set(_responders(db).replied[0].model_dump()) == {"alumni_id", "name"}


def test_a_reply_superseded_by_a_reset_is_excluded_from_list_and_count(db):
    _world(db)
    # The reset comes AFTER Adams's confirmation, so it no longer counts as a
    # reply anywhere (#395) — the hover must drop her exactly when the number does.
    # Her email is stamped with the same reset seq, so it is NOT superseded:
    # this isolates the RESPONSE half of the reset rule from the send half.
    db.conn.execute(
        text("UPDATE survey_send_log SET reset_seq = 1 WHERE alumni_id = 2")
    )
    _reset(db, 2, at=_NOW + datetime.timedelta(minutes=5))
    item = _item(db)
    got = _responders(db)
    assert 2 not in {r.alumni_id for r in got.replied}
    assert 2 not in {r.alumni_id for r in got.confirmed}
    assert len(got.replied) == item.replied == 2
    assert len(got.confirmed) == item.confirmed == 1
    # Still a recipient — only the reply was superseded.
    assert item.recipients == 7


def test_a_superseded_send_drops_the_alum_from_the_cycle(db):
    _world(db)
    # A reset with a higher seq than the send supersedes the EMAIL, even if a
    # reply came in later — they are owed the campaign again and are not one of
    # this cycle's recipients until it reaches them.
    _reset(db, 1, at=_NOW - datetime.timedelta(days=1))
    item = _item(db)
    got = _responders(db)
    assert 1 not in {r.alumni_id for r in got.replied}
    assert len(got.replied) == item.replied


def test_archived_alumni_stay_in_the_list_because_the_count_includes_them(db):
    _world(db)
    db.conn.execute(text("UPDATE alumni SET archived = 1 WHERE alumni_id = 1"))
    assert len(_responders(db).replied) == _item(db).replied == 3


def test_a_campaign_nobody_has_answered_is_two_empty_lists(db):
    db.schedule()
    db.alum(1)
    db.sent(1, (0,))
    got = _responders(db)
    assert got.replied == [] and got.confirmed == []


def test_a_year_with_no_campaign_is_none(db):
    db.schedule(year=2001)
    assert _responders(db, 1999) is None


def test_name_falls_back_to_the_id(db):
    db.schedule()
    db.conn.execute(
        text("INSERT INTO alumni (alumni_id, archived) VALUES (9, 0)")
    )
    db.sent(9, (0,))
    db.replied(9, "confirmed")
    assert [r.name for r in _responders(db).confirmed] == ["Alum #9"]


# ------------------------------------------------------------------ route -----


def _path(year=_YEAR):
    return f"/survey/schedules/{year}/responders"


def test_route_requires_auth(db):
    assert _get(_path(), db.session).status_code == 401


def test_route_forbidden_for_view_only(db):
    # Gated like the counts it expands (`GET /survey/schedules`).
    assert _get(_path(), db.session, _ctx("view_only")).status_code == 403


def test_route_returns_both_lists_for_full_access(db):
    _world(db)
    resp = _get(_path(), db.session, _ctx("full_access"))
    assert resp.status_code == 200
    assert resp.json() == {
        "replied": [
            {"alumni_id": 2, "name": "A2 Adams"},
            {"alumni_id": 3, "name": "A3 Moss"},
            {"alumni_id": 1, "name": "A1 Young"},
        ],
        "confirmed": [
            {"alumni_id": 2, "name": "A2 Adams"},
            {"alumni_id": 3, "name": "A3 Moss"},
        ],
    }


def test_route_404s_for_a_year_with_no_campaign(db):
    assert _get(_path(1999), db.session, _ctx("full_access")).status_code == 404


# ------------------------------------------------- "No reply yet" export ------
#
# The export's contract is the same as the hover's: its row count is the number
# on screen. "No reply yet" is `recipients - replied` (web `toProgressRow`), so
# every population test below compares the CSV against those two counts from
# `list_schedules` over the same rows.


class _AuditingSession:
    """The follow-up suite's session plus the two writes an export makes: the
    audit row (kept for inspection) and the commit."""

    def __init__(self, db):
        self._inner = db.session
        self.added = []

    async def execute(self, stmt):
        return await self._inner.execute(stmt)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass


def _export(db, year=_YEAR, actor=1):
    session = _AuditingSession(db)
    out = asyncio.run(ss.export_no_reply_csv(session, year, actor_user_id=actor))
    return out, session


def _rows(csv_text):
    import csv
    import io

    return list(csv.reader(io.StringIO(csv_text)))


def _silent(item):
    return item.recipients - item.replied


def test_export_row_count_matches_no_reply_yet(db):
    _world(db)
    csv_text, _ = _export(db)
    header, *body = _rows(csv_text)
    assert header == list(ss.NO_REPLY_EXPORT_COLUMNS)
    assert len(body) == _silent(_item(db)) == 4


def test_export_excludes_every_kind_of_reply_but_keeps_rejected_only(db):
    _world(db)
    names = [r[0] for r in _rows(_export(db)[0])[1:]]
    # Young applied, Adams confirmed, Moss pending + confirmed: all replies.
    assert not {"A1 Young", "A2 Adams", "A3 Moss"} & set(names)
    # Baker's only submission was rejected — not a reply, so still owed one.
    assert "A4 Baker" in names
    # Cole replied over a year ago (outside the window); Diaz and Evans never did.
    # Ford was emailed only by the previous campaign, so is not in this one.
    assert names == ["A4 Baker", "A5 Cole", "A6 Diaz", "A7 Evans"]


def test_export_includes_someone_whose_reply_a_reset_superseded(db):
    _world(db)
    db.conn.execute(
        text("UPDATE survey_send_log SET reset_seq = 1 WHERE alumni_id = 2")
    )
    _reset(db, 2, at=_NOW + datetime.timedelta(minutes=5))
    body = _rows(_export(db)[0])[1:]
    assert "A2 Adams" in [r[0] for r in body]
    assert len(body) == _silent(_item(db))


def test_export_columns_carry_contact_and_cycle_details(db):
    db.schedule()
    db.alum(1, last_name="Diaz", email="diaz@example.com")
    db.conn.execute(
        text("UPDATE alumni_contact_info SET phone = '801-555-0100' WHERE alumni_id = 1")
    )
    db.sent(1, (0, 1))
    assert _rows(_export(db)[0])[1] == [
        "A1 Diaz",
        str(_YEAR),
        "diaz@example.com",
        "801-555-0100",
        "2",
        _NOW.date().isoformat(),
    ]


def test_export_neutralises_spreadsheet_formulas(db):
    db.schedule()
    db.conn.execute(
        text(
            "INSERT INTO alumni (alumni_id, first_name, last_name, archived)"
            " VALUES (1, '=HYPERLINK(\"http://x\")', 'Evil', 0)"
        )
    )
    db.conn.execute(
        text(
            "INSERT INTO alumni_contact_info (contact_info_id, alumni_id,"
            " personal_email, phone) VALUES (1, 1, '@SUM(A1)', '+1 801 555 0100')"
        )
    )
    db.sent(1, (0,))
    name, _, email, phone, *_ = _rows(_export(db)[0])[1]
    assert name.startswith("\t=")
    assert email == "\t@SUM(A1)"
    assert phone == "\t+1 801 555 0100"


def test_export_all_years_matches_the_summed_column(db):
    _world(db)
    db.schedule(year=2001)
    db.alum(20, last_name="Hale")
    db.sent(20, (0,), year=2001)
    csv_text, _ = _export(db, year=None)
    body = _rows(csv_text)[1:]
    items = asyncio.run(ss.list_schedules(db.session))
    assert len(body) == sum(_silent(i) for i in items) == 5
    # Newest cohort first, like the table.
    assert body[0][:2] == ["A20 Hale", "2001"]


def test_export_is_audited_without_the_rows(db):
    _world(db)
    _, session = _export(db, actor=42)
    (entry,) = session.added
    assert entry.action_type == "export_survey_no_reply"
    assert entry.user_id == 42
    assert entry.entity_id == _YEAR
    assert entry.new_value == f"rows=4; graduation_year={_YEAR}"


def test_export_is_none_for_a_year_with_no_campaign(db):
    assert _export(db, year=1999)[0] is None


def _export_path(year=_YEAR):
    return f"/survey/schedules/{year}/no-reply/export"


def test_export_route_requires_auth(db):
    assert _get(_export_path(), db.session).status_code == 401
    assert _get("/survey/schedules/no-reply/export", db.session).status_code == 401


def test_export_route_forbidden_for_view_only(db):
    ctx = _ctx("view_only")
    assert _get(_export_path(), db.session, ctx).status_code == 403
    assert (
        _get("/survey/schedules/no-reply/export", db.session, ctx).status_code == 403
    )


def test_export_route_downloads_a_csv(db):
    _world(db)
    resp = _get(_export_path(), _AuditingSession(db), _ctx("full_access"))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith(f'attachment; filename="survey_no_reply_{_YEAR}_')
    assert len(_rows(resp.text)) == 1 + 4


def test_export_all_years_route_downloads_a_csv(db):
    _world(db)
    resp = _get(
        "/survey/schedules/no-reply/export", _AuditingSession(db), _ctx("full_access")
    )
    assert resp.status_code == 200
    assert 'filename="survey_no_reply_all_' in resp.headers["content-disposition"]


def test_export_route_404s_for_a_year_with_no_campaign(db):
    resp = _get(_export_path(1999), _AuditingSession(db), _ctx("full_access"))
    assert resp.status_code == 404
