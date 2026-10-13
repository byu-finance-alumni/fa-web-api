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
    # id, display name, and the median-hover fill time (#543 follow-on) — and
    # nothing that would need contacting the alum. On SQLite the fill time has
    # no column, so it comes back None, but the field is always present.
    assert set(_responders(db).replied[0].model_dump()) == {
        "alumni_id",
        "name",
        "fill_seconds",
    }
    assert _responders(db).replied[0].fill_seconds is None


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
    # `fill_seconds` is None throughout: the SQLite test world has no timer
    # column, so the median-hover field falls back exactly as it does for a
    # cycle with no usable times yet (see the Postgres-shape tests below).
    assert resp.json() == {
        "replied": [
            {"alumni_id": 2, "name": "A2 Adams", "fill_seconds": None},
            {"alumni_id": 3, "name": "A3 Moss", "fill_seconds": None},
            {"alumni_id": 1, "name": "A1 Young", "fill_seconds": None},
        ],
        "confirmed": [
            {"alumni_id": 2, "name": "A2 Adams", "fill_seconds": None},
            {"alumni_id": 3, "name": "A3 Moss", "fill_seconds": None},
        ],
    }


def test_route_404s_for_a_year_with_no_campaign(db):
    assert _get(_path(1999), db.session, _ctx("full_access")).status_code == 404


# ----------------------------------------- median-hover fill time (#543) ------
#
# Each `replied` responder also carries the representative fill time behind the
# "Median time to complete" hover. The value reads `survey_responses.fill_seconds`
# / `cycle_seq`, columns the SQLite world above does not have, so the population
# is proven by compiling the Postgres subquery (as the median's own tests do) and
# the mapping is proven through the fake session — never both against a
# hand-picked SQLite row.

from sqlalchemy.dialects import postgresql  # noqa: E402

from tests.test_survey_scheduler import QueueSession, _Res, _sched  # noqa: E402


def _fill_sql(year=_YEAR):
    return str(
        ss._representative_fill_seconds(year).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_representative_fill_time_uses_the_same_population_as_the_median():
    sql = _fill_sql()
    # The response's own `cycle_seq` joined to the schedule's current cycle —
    # identical to `_cycle_fill_times`, so the hover never shows a time the
    # median did not count.
    assert (
        "survey_schedule.graduation_year = survey_responses.graduation_year AND "
        "survey_schedule.cycle_seq = survey_responses.cycle_seq" in sql
    )
    # A reply, by the shared definition: RESPONDED_STATUSES, in-window, not reset.
    assert "IN ('pending', 'applied', 'confirmed')" in sql
    assert "survey_responses.submitted_at >=" in sql
    assert "survey_reset_log" in sql
    # Only a usable value can be shown — a confirmation (no timer) and pre-column
    # history are NULL and drop out, exactly as in the median.
    assert "survey_responses.fill_seconds IS NOT NULL" in sql
    assert "survey_responses.fill_seconds > 0" in sql
    assert str(ss._FILL_SECONDS_MAX) in sql


def test_representative_fill_time_is_the_most_recent_qualifying_response():
    # One row per person, picked deterministically: the latest qualifying
    # response's time. The median aggregates every qualifying row; the hover
    # shows this one representative of them.
    sql = _fill_sql()
    assert "survey_responses.alumni_id = alumni.alumni_id" in sql
    assert "ORDER BY survey_responses.submitted_at DESC" in sql
    assert "LIMIT 1" in sql


def test_fill_times_flow_through_to_the_repliers():
    # Postgres path (QueueSession reports the pg dialect): `list_responders`
    # runs the schedule lookup, then the replied names, then the confirmed
    # names. A non-null time maps straight through; a null (no usable timer)
    # becomes None so the hover omits that person.
    session = QueueSession(
        [
            _Res(one=_sched(_YEAR, datetime.date(2026, 7, 1), status="active")),
            _Res(
                rows=[
                    (2, None, "A2", "Adams", None),
                    (3, None, "A3", "Moss", 200),
                    (1, None, "A1", "Young", 45),
                ]
            ),  # replied
            _Res(rows=[(2, None, "A2", "Adams", None)]),  # confirmed
        ]
    )
    got = asyncio.run(ss.list_responders(session, _YEAR))
    assert [(r.name, r.fill_seconds) for r in got.replied] == [
        ("A2 Adams", None),
        ("A3 Moss", 200),
        ("A1 Young", 45),
    ]
    assert got.confirmed[0].fill_seconds is None


# ------------------------------------------------- "No reply yet" export ------
#
# The export's contract: its row count is the number on screen MINUS archived
# alumni, which the file leaves out as the follow-up call sheet does (Jake,
# #836) while the count keeps them. "No reply yet" is `recipients - replied`
# (web `toProgressRow`), so every population test below compares the CSV
# against those two counts from `list_schedules` over the same rows.


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


@pytest.fixture
def no_app_url(monkeypatch):
    """``SURVEY_APP_BASE_URL`` unset, whatever the environment says."""
    monkeypatch.setattr(ss, "_profile_link_base", lambda: None)


@pytest.fixture
def app_url(monkeypatch):
    """A configured frontend origin, with the trailing slash the setting is
    allowed to carry — the link must not come out with ``//alumni``."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        ss.survey_email,
        "get_settings",
        lambda: SimpleNamespace(survey_app_base_url=" https://app.example/ "),
    )


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


def test_export_columns_carry_contact_and_cycle_details(db, no_app_url):
    db.schedule()
    db.alum(1, last_name="Diaz", email="diaz@example.com")
    db.conn.execute(
        text("UPDATE alumni_contact_info SET phone = '801-555-0100' WHERE alumni_id = 1")
    )
    db.sent(1, (0, 1))
    # Only the cycle columns are filled here; the profile columns (#605) are
    # empty for an alum with nothing else on file — including the profile link
    # when the frontend URL is not configured.
    assert _rows(_export(db)[0])[1] == [
        "A1 Diaz",
        str(_YEAR),
        "diaz@example.com",
        "801-555-0100",
        "2",
        _NOW.date().isoformat(),
        *[""] * len(ss._PROFILE_EXPORT_COLUMNS),
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


def _archive(db, alumni_id):
    db.conn.execute(
        text("UPDATE alumni SET archived = 1 WHERE alumni_id = :a"), {"a": alumni_id}
    )


def test_an_archived_non_replier_is_left_out_of_the_file_but_still_counted(db):
    _world(db)
    _archive(db, 6)  # Diaz: emailed, never replied
    item = _item(db)
    body = _rows(_export(db)[0])[1:]
    assert "A6 Diaz" not in [r[0] for r in body]
    # The column is unchanged; the file is it minus the one archived alum.
    assert _silent(item) == 4
    assert len(body) == _silent(item) - 1 == 3


def test_archiving_a_replier_changes_neither_the_file_nor_the_column(db):
    # Only archived NON-repliers are the difference: an archived replier was
    # never in the file to begin with.
    _world(db)
    _archive(db, 1)  # Young replied
    assert len(_rows(_export(db)[0])[1:]) == _silent(_item(db)) == 4


def test_export_all_years_leaves_out_archived_alumni_too(db):
    _world(db)
    db.schedule(year=2001)
    db.alum(20, last_name="Hale")
    db.alum(21, last_name="Iver")
    db.sent(20, (0,), year=2001)
    db.sent(21, (0,), year=2001)
    _archive(db, 21)
    _archive(db, 7)  # Evans, in the 2000 cohort
    body = _rows(_export(db, year=None)[0])[1:]
    items = asyncio.run(ss.list_schedules(db.session))
    assert sum(_silent(i) for i in items) == 6
    assert len(body) == 6 - 2
    assert not {"A21 Iver", "A7 Evans"} & {r[0] for r in body}


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


# ------------------------------------- export profile columns (#605) ----------


def _profile(db, alumni_id, **employment):
    """Fill in an alum's profile columns and (optionally) a current role."""
    db.conn.execute(
        text(
            "UPDATE alumni SET net_id = :n, linkedin_url = :l, updated_at = :u"
            " WHERE alumni_id = :a"
        ),
        {
            "a": alumni_id,
            "n": f"net{alumni_id}",
            "l": f"https://linkedin.example/in/a{alumni_id}",
            "u": datetime.datetime(2026, 9, 15, 23, 30),
        },
    )
    if employment:
        db.conn.execute(
            text(
                "INSERT INTO current_employment (current_employment_id, alumni_id,"
                " current_employer, current_title, current_industry, current_city,"
                " current_state, current_country) VALUES (:a, :a, :e, :t, :i, :c,"
                " :s, :k)"
            ),
            {
                "a": alumni_id,
                "e": employment.get("employer"),
                "t": employment.get("title"),
                "i": employment.get("industry"),
                "c": employment.get("city"),
                "s": employment.get("state"),
                "k": employment.get("country"),
            },
        )


def test_no_reply_header_keeps_the_old_columns_first_then_the_profile_ones():
    assert ss.NO_REPLY_EXPORT_COLUMNS == (
        "Name",
        "Graduation year",
        "Email",
        "Phone",
        "Emails sent this cycle",
        "Last email sent",
        "NetID",
        "Employer",
        "Title",
        "Industry",
        "Work City",
        "Work State",
        "Work Country",
        "LinkedIn",
        "Last Updated",
        "Profile Link",
    )


def test_no_reply_row_carries_the_profile_columns_and_link(db, app_url):
    db.schedule()
    db.alum(1, last_name="Diaz", email="diaz@example.com")
    _profile(
        db,
        1,
        employer="Acme Capital",
        title="Analyst",
        industry="Investment Banking",
        city="New York",
        state="New York",
        country="United States",
    )
    db.sent(1, (0,))
    header, row = _rows(_export(db)[0])
    assert dict(zip(header, row, strict=True)) == {
        "Name": "A1 Diaz",
        "Graduation year": str(_YEAR),
        "Email": "diaz@example.com",
        "Phone": "",
        "Emails sent this cycle": "1",
        "Last email sent": _NOW.date().isoformat(),
        "NetID": "net1",
        "Employer": "Acme Capital",
        "Title": "Analyst",
        "Industry": "Investment Banking",
        "Work City": "New York",
        "Work State": "New York",
        "Work Country": "United States",
        "LinkedIn": "https://linkedin.example/in/a1",
        "Last Updated": "2026-09-15",
        # Trimmed origin + the integer id, no double slash.
        "Profile Link": "https://app.example/alumni/1",
    }


def test_an_alum_with_no_job_on_file_is_still_one_row(db, app_url):
    # The employment join is OUTER: no current role must not drop the person.
    db.schedule()
    db.alum(1, last_name="Diaz")
    db.alum(2, last_name="Evans")
    _profile(db, 2, employer="Acme")
    db.sent(1, (0,))
    db.sent(2, (0,))
    body = _rows(_export(db)[0])[1:]
    assert [r[0] for r in body] == ["A1 Diaz", "A2 Evans"]
    assert body[0][7] == "" and body[1][7] == "Acme"


def test_the_profile_columns_go_through_the_formula_guard(db, app_url):
    db.schedule()
    db.alum(1, last_name="Evil")
    db.conn.execute(
        text(
            "UPDATE alumni SET net_id = '=1+1', linkedin_url = '@SUM(A1)'"
            " WHERE alumni_id = 1"
        )
    )
    db.conn.execute(
        text(
            "INSERT INTO current_employment (current_employment_id, alumni_id,"
            " current_employer, current_title, current_industry, current_city)"
            " VALUES (1, 1, '=HYPERLINK(\"http://x\")', '+cmd', '-2', '=c')"
        )
    )
    db.sent(1, (0,))
    header, row = _rows(_export(db)[0])
    cells = dict(zip(header, row, strict=True))
    for column in ("NetID", "Employer", "Title", "Industry", "Work City", "LinkedIn"):
        assert cells[column].startswith("\t"), column


# ------------------------------------------------- "Replied" export (#605) ----
#
# The file's population is the `replied` count's — the very people the hover
# (`list_responders`) names — minus archived alumni. Compared against BOTH the
# count and the hover list over the same rows, never a hand-picked expectation.


def _export_replied(db, year=_YEAR, actor=1):
    session = _AuditingSession(db)
    out = asyncio.run(ss.export_replied_csv(session, year, actor_user_id=actor))
    return out, session


def test_replied_header_is_the_no_reply_columns_plus_the_mailing_address():
    assert ss.REPLIED_EXPORT_COLUMNS == (
        *ss.NO_REPLY_EXPORT_COLUMNS,
        "Mailing Address 1",
        "Mailing Address 2",
        "Mailing City",
        "Mailing State",
        "Mailing Zip",
        "Mailing Country",
    )


def test_replied_export_is_exactly_the_replied_count_and_hover(db):
    _world(db)
    header, *body = _rows(_export_replied(db)[0])
    assert header == list(ss.REPLIED_EXPORT_COLUMNS)
    hover = [r.name for r in _responders(db).replied]
    # Same people, same (name) order as the hover, as long as the count.
    assert [r[0] for r in body] == hover == ["A2 Adams", "A3 Moss", "A1 Young"]
    assert len(body) == _item(db).replied == 3


def test_replied_and_no_reply_partition_the_recipients(db):
    # The two files are the two sides of ONE predicate over the same rows:
    # nobody in both, nobody in neither (Baker's rejected-only reply puts her in
    # no-reply; Cole's out-of-window reply likewise).
    _world(db)
    replied = {r[0] for r in _rows(_export_replied(db)[0])[1:]}
    silent = {r[0] for r in _rows(_export(db)[0])[1:]}
    assert not replied & silent
    assert len(replied | silent) == _item(db).recipients == 7
    assert "A4 Baker" in silent and "A4 Baker" not in replied


def test_replied_export_drops_a_reply_a_reset_superseded(db):
    _world(db)
    db.conn.execute(
        text("UPDATE survey_send_log SET reset_seq = 1 WHERE alumni_id = 2")
    )
    _reset(db, 2, at=_NOW + datetime.timedelta(minutes=5))
    body = _rows(_export_replied(db)[0])[1:]
    assert "A2 Adams" not in [r[0] for r in body]
    assert len(body) == _item(db).replied == len(_responders(db).replied)


def test_replied_export_leaves_out_archived_alumni_the_count_keeps(db):
    _world(db)
    _archive(db, 1)  # Young replied
    item = _item(db)
    body = _rows(_export_replied(db)[0])[1:]
    assert "A1 Young" not in [r[0] for r in body]
    # The column (and the hover) still count her; the file is that minus one.
    assert item.replied == len(_responders(db).replied) == 3
    assert len(body) == item.replied - 1


def test_replied_export_all_years_matches_the_summed_column(db):
    _world(db)
    db.schedule(year=2001)
    db.alum(20, last_name="Hale")
    db.alum(21, last_name="Iver")
    db.sent(20, (0,), year=2001)
    db.sent(21, (0,), year=2001)
    db.replied(20, "applied")
    db.replied(21, "confirmed")
    _archive(db, 21)
    items = asyncio.run(ss.list_schedules(db.session))
    total = sum(i.replied for i in items)
    assert total == 5
    body = _rows(_export_replied(db, year=None)[0])[1:]
    assert len(body) == total - 1  # Iver is archived
    # Newest cohort first, like the table.
    assert body[0][:2] == ["A20 Hale", "2001"]


def test_replied_export_carries_the_mailing_address(db, no_app_url):
    db.schedule()
    db.alum(1, last_name="Diaz", email="diaz@example.com")
    db.conn.execute(
        text(
            "UPDATE alumni_contact_info SET address_line_1 = '1 Main St',"
            " address_line_2 = '=Apt 2', city = 'Provo', state = 'Utah',"
            " zip = '84604', country = 'United States' WHERE alumni_id = 1"
        )
    )
    db.sent(1, (0,))
    db.replied(1, "confirmed")
    header, row = _rows(_export_replied(db)[0])
    cells = dict(zip(header, row, strict=True))
    assert cells["Mailing Address 1"] == "1 Main St"
    # The mailing cells take the same formula guard as every other cell.
    assert cells["Mailing Address 2"] == "\t=Apt 2"
    assert cells["Mailing City"] == "Provo"
    assert cells["Mailing State"] == "Utah"
    assert cells["Mailing Zip"] == "84604"
    assert cells["Mailing Country"] == "United States"
    assert cells["Profile Link"] == ""


def test_replied_export_is_audited_without_the_rows(db):
    _world(db)
    _, session = _export_replied(db, actor=42)
    (entry,) = session.added
    assert entry.action_type == "export_survey_replied"
    assert entry.user_id == 42
    assert entry.entity_type == "survey_campaign"
    assert entry.entity_id == _YEAR
    assert entry.new_value == f"rows=3; graduation_year={_YEAR}"


def test_replied_export_all_years_audits_as_all(db):
    _world(db)
    _, session = _export_replied(db, year=None, actor=42)
    (entry,) = session.added
    assert entry.entity_id is None
    assert entry.new_value == "rows=3; graduation_year=all"


def test_replied_export_is_none_for_a_year_with_no_campaign(db):
    assert _export_replied(db, year=1999)[0] is None


# ------------------------------------------ export + progress gates (#605) ----


@pytest.fixture
def fresh_limits():
    from app.core import rate_limit

    rate_limit.reset()
    yield
    rate_limit.reset()


_EXPORT_PATHS = [
    f"/survey/schedules/{_YEAR}/no-reply/export",
    "/survey/schedules/no-reply/export",
    f"/survey/schedules/{_YEAR}/replied/export",
    "/survey/schedules/replied/export",
]


@pytest.mark.parametrize("path", _EXPORT_PATHS)
def test_survey_exports_require_auth(db, path):
    assert _get(path, db.session).status_code == 401


@pytest.mark.parametrize("path", _EXPORT_PATHS)
def test_survey_exports_forbidden_for_view_only(db, path, fresh_limits):
    assert _get(path, _AuditingSession(db), _ctx("view_only")).status_code == 403


@pytest.mark.parametrize("path", _EXPORT_PATHS)
def test_students_may_download_the_survey_exports(db, path, fresh_limits):
    # Jake, 2026-10-13: every student may export.
    _world(db)
    resp = _get(path, _AuditingSession(db), _ctx("student"))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")


def test_replied_export_route_downloads_a_csv(db, fresh_limits):
    _world(db)
    resp = _get(
        f"/survey/schedules/{_YEAR}/replied/export",
        _AuditingSession(db),
        _ctx("full_access"),
    )
    assert resp.status_code == 200
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith(f'attachment; filename="survey_replied_{_YEAR}_')
    assert len(_rows(resp.text)) == 1 + 3


def test_replied_export_all_years_route_is_not_taken_for_a_year(db, fresh_limits):
    # Declared before the parametrised route, so `replied` is never parsed as
    # a graduation year (which would 422).
    _world(db)
    resp = _get(
        "/survey/schedules/replied/export", _AuditingSession(db), _ctx("full_access")
    )
    assert resp.status_code == 200
    assert 'filename="survey_replied_all_' in resp.headers["content-disposition"]


def test_replied_export_route_404s_for_a_year_with_no_campaign(db, fresh_limits):
    resp = _get(
        "/survey/schedules/1999/replied/export",
        _AuditingSession(db),
        _ctx("full_access"),
    )
    assert resp.status_code == 404


def test_students_can_see_who_replied(db, fresh_limits):
    _world(db)
    resp = _get(_path(), db.session, _ctx("student"))
    assert resp.status_code == 200
    assert len(resp.json()["replied"]) == 3
