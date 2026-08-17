"""Campaign-cycle stamping on survey responses (#497). No real DB / network.

`submit_response` records WHICH campaign a reply answers, because nothing else
can afterwards: every console count joins the year's CURRENT `cycle_seq`, so the
first cycle's numbers vanish the moment a graduation year runs a second campaign,
and the response row itself held no trace of which one asked.

Two invariants these tests exist to hold:

* The stamp is READ OFF a `survey_send_log` row, never computed. A cycle derived
  from a date is the #357 bug -- a campaign starting in December sends its
  reminders in January -- so `test_cycle_is_not_derived_from_the_calendar_year`
  submits in a later year than the send and expects the SEND's cycle.
* The stamp is never worth a failed submission. Anything unresolvable stores
  NULL, and NULL is a first-class "we do not know", never a defaulted 1.
"""

import asyncio
import datetime
import types

import pytest

from app.models.survey_response import SurveyResponse
from app.services import survey_responses as sr

# --------------------------------------------------------------- fakes -------


class _Result:
    """Just the two accessors the submit path uses."""

    def __init__(self, rows=()):
        self._rows = list(rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _Session:
    """AsyncSession stand-in that can actually answer the send-log lookup.

    The suite's older survey fake returns the SAME canned row for every
    ``execute``, which cannot express "the alum row, and separately the send log"
    -- so it could not tell a correct stamp from an absent one. This one
    dispatches on the statement's FROM and serves `survey_send_log` from a real
    in-memory list, honouring the same ordering the query asks for.
    """

    def __init__(self, alum=None, send_log=()):
        self.alum = alum
        # dicts: graduation_year, alumni_id, stage, cycle_seq, sent_at, id
        self.send_log = list(send_log)
        self.added = []
        self.committed = 0
        self.send_log_reads = 0

    async def execute(self, stmt):
        froms = {getattr(f, "name", None) for f in stmt.get_final_froms()}
        if "survey_send_log" in froms:
            self.send_log_reads += 1
            params = dict(stmt.compile().params)
            rows = [
                r
                for r in self.send_log
                if r["graduation_year"] == params.get("graduation_year_1")
                and r["alumni_id"] == params.get("alumni_id_1")
            ]
            # Newest row wins, id as the tie-break -- what the ORDER BY says.
            rows.sort(key=lambda r: (r["sent_at"], r["id"]), reverse=True)
            return _Result([(r["cycle_seq"], r["stage"]) for r in rows[:1]])
        return _Result([self.alum] if self.alum is not None else [])

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if isinstance(obj, SurveyResponse) and obj.survey_response_id is None:
                obj.survey_response_id = 777

    async def commit(self):
        self.committed += 1


def _sent(year, alumni_id, stage, cycle_seq, sent_at, id=1):
    return {
        "graduation_year": year,
        "alumni_id": alumni_id,
        "stage": stage,
        "cycle_seq": cycle_seq,
        "sent_at": sent_at,
        "id": id,
    }


def _alum(alumni_id=5, graduation_year=2020):
    return types.SimpleNamespace(
        alumni_id=alumni_id, archived=False, graduation_year=graduation_year
    )


def _submit(session, monkeypatch, alumni_id=5, fields=None):
    monkeypatch.setattr(sr, "verify_survey_token", lambda _t: alumni_id)
    return asyncio.run(
        sr.submit_response(session, "tok", fields or {"contact.city": "Provo"})
    )


def _staged(session):
    rows = [o for o in session.added if isinstance(o, SurveyResponse)]
    assert len(rows) == 1
    return rows[0]


_JAN = datetime.datetime(2027, 1, 5, tzinfo=datetime.UTC)
_DEC = datetime.datetime(2026, 12, 20, tzinfo=datetime.UTC)


# ---------------------------------------------------- stamped when known ------


def test_stamps_cycle_and_stage_from_the_send_log(monkeypatch):
    # The live case: the alum was emailed in this year's SECOND campaign, at the
    # 1-week reminder, and replies. Both facts land on the row.
    session = _Session(
        _alum(),
        send_log=[_sent(2020, 5, stage=1, cycle_seq=2, sent_at=_DEC)],
    )
    result = _submit(session, monkeypatch)

    assert result.staged is True
    row = _staged(session)
    assert row.cycle_seq == 2
    assert row.stage == 1
    # Untouched by the stamp: the response still stages exactly as before.
    assert row.status == "pending"
    assert row.graduation_year == 2020
    assert session.committed == 1


def test_stamps_the_most_recent_send_not_the_first(monkeypatch):
    # An alum who got the initial AND both reminders is attributed to the LAST
    # email they were sent -- the one that actually prompted the reply.
    session = _Session(
        _alum(),
        send_log=[
            _sent(2020, 5, stage=0, cycle_seq=3, sent_at=_DEC, id=1),
            _sent(2020, 5, stage=2, cycle_seq=3, sent_at=_JAN, id=3),
            _sent(2020, 5, stage=1, cycle_seq=3, sent_at=_DEC, id=2),
        ],
    )
    _submit(session, monkeypatch)

    row = _staged(session)
    assert (row.cycle_seq, row.stage) == (3, 2)


def test_cycle_is_not_derived_from_the_calendar_year(monkeypatch):
    # #357's trap, at this table. The campaign started in late December (cycle 4)
    # and the reply arrives in January. A cycle inferred from any date would flip
    # here and split one campaign's responses across two. The stored value is the
    # SEND ROW's, so it does not move.
    session = _Session(
        _alum(),
        send_log=[_sent(2020, 5, stage=2, cycle_seq=4, sent_at=_DEC)],
    )
    _submit(session, monkeypatch)

    row = _staged(session)
    assert row.cycle_seq == 4


def test_stamp_is_scoped_to_the_alum_and_year(monkeypatch):
    # Another alum's send, and the same alum's send under a different graduation
    # year, are both ignored: a cycle number only means anything against the year
    # it counts for, so a mismatched pair would be a WRONG stamp, not a near one.
    session = _Session(
        _alum(alumni_id=5, graduation_year=2020),
        send_log=[
            _sent(2020, 99, stage=1, cycle_seq=7, sent_at=_JAN),
            _sent(2019, 5, stage=1, cycle_seq=6, sent_at=_JAN),
        ],
    )
    _submit(session, monkeypatch)

    row = _staged(session)
    assert (row.cycle_seq, row.stage) == (None, None)


# ------------------------------------------------ null when unresolvable ------


def test_stores_null_when_no_send_was_logged(monkeypatch):
    # A hand-issued or dev link: the alum was never emailed by a campaign, so
    # there is no cycle. The submission still stages -- NULL is the answer, not
    # an error, and NOT cycle 1.
    session = _Session(_alum(), send_log=[])
    result = _submit(session, monkeypatch)

    assert result.staged is True
    assert result.change_count == 1
    row = _staged(session)
    assert row.cycle_seq is None
    assert row.stage is None
    assert session.committed == 1


def test_no_graduation_year_stores_null_without_querying(monkeypatch):
    # No year means no (year, cycle) pair worth storing. The lookup short-circuits
    # rather than issuing a query that could only match by accident.
    session = _Session(_alum(graduation_year=None), send_log=[])
    result = _submit(session, monkeypatch)

    assert result.staged is True
    assert _staged(session).cycle_seq is None
    assert session.send_log_reads == 0


def test_submit_survives_a_failing_cycle_lookup(monkeypatch):
    # The hard rule: the stamp is bookkeeping, the alum's answers are not. If the
    # lookup blows up the response is still staged and committed, with NULLs.
    async def boom(*_a, **_k):
        raise RuntimeError("send log unreadable")

    monkeypatch.setattr(sr, "sent_cycle_and_stage", boom)
    session = _Session(_alum())
    result = _submit(session, monkeypatch)

    assert result.staged is True
    assert result.survey_response_id == 777
    row = _staged(session)
    assert (row.cycle_seq, row.stage) == (None, None)
    assert session.committed == 1


def test_failing_lookup_is_logged_without_the_payload(monkeypatch, caplog):
    # It is a warning, not a silence -- but it must not carry what the alum typed.
    async def boom(*_a, **_k):
        raise RuntimeError("send log unreadable")

    monkeypatch.setattr(sr, "sent_cycle_and_stage", boom)
    session = _Session(_alum())
    with caplog.at_level("WARNING"):
        _submit(session, monkeypatch, fields={"contact.city": "Secretville"})

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("campaign cycle" in r.getMessage() for r in warnings)
    assert not any("Secretville" in r.getMessage() for r in warnings)


def test_true_noop_still_stages_nothing(monkeypatch):
    # Unchanged by #497: no recognized fields and no photo is still a no-op, and
    # the cycle lookup never runs for a row that is never created.
    session = _Session(_alum(), send_log=[_sent(2020, 5, 1, 2, _JAN)])
    result = _submit(session, monkeypatch, fields={"bogus": "x"})

    assert result.staged is False
    assert result.survey_response_id is None
    assert [o for o in session.added if isinstance(o, SurveyResponse)] == []
    assert session.send_log_reads == 0
    assert session.committed == 0


# ------------------------------------------------- review path untouched ------


@pytest.mark.parametrize("review", ["apply", "reject"])
def test_review_leaves_the_stamp_alone(monkeypatch, review):
    # Capture only: apply and reject behave exactly as before and neither reads,
    # rewrites nor clears the stamp the submission put there.
    from app.services import supabase_storage

    resp = types.SimpleNamespace(
        survey_response_id=1,
        alumni_id=5,
        graduation_year=2020,
        payload={"profile.linkedin_url": "https://www.linkedin.com/in/jdoe"},
        status="pending",
        staged_photo_path=None,
        reviewed_by_user_id=None,
        reviewed_at=None,
        cycle_seq=2,
        stage=1,
    )
    alum = types.SimpleNamespace(alumni_id=5, net_id="jdoe5", linkedin_url=None)

    async def fake_get_pending(_s, _rid):
        return resp

    async def fake_side(_s, _ids):
        return ({}, {}, {})

    async def boom(*_a, **_k):
        raise AssertionError("storage must not be touched")

    monkeypatch.setattr(sr, "_get_pending", fake_get_pending)
    monkeypatch.setattr(sr, "_load_side_rows", fake_side)
    monkeypatch.setattr(supabase_storage, "download_object", boom)
    monkeypatch.setattr(supabase_storage, "upload_object", boom)
    monkeypatch.setattr(supabase_storage, "delete_object", boom)

    session = _Session(alum)
    if review == "apply":
        asyncio.run(sr.apply_response(session, 1, actor_user_id=9))
        assert resp.status == "applied"
        # The existing write still happens.
        assert alum.linkedin_url == "https://www.linkedin.com/in/jdoe"
    else:
        asyncio.run(sr.reject_response(session, 1, actor_user_id=9))
        assert resp.status == "rejected"

    assert (resp.cycle_seq, resp.stage) == (2, 1)
    assert resp.reviewed_by_user_id == 9


# ------------------------------------------------------ the model itself ------


def test_columns_are_nullable_and_not_defaulted():
    # The migration's central promise. A NOT NULL or a DEFAULT 1 here would
    # assert a cycle for every historical row that nobody ever observed, and a
    # wrong stamp is worse than a missing one -- NULL is excludable in a report,
    # a plausible wrong number is not.
    for name in ("cycle_seq", "stage"):
        column = SurveyResponse.__table__.columns[name]
        assert column.nullable is True, name
        assert column.default is None, name
        assert column.server_default is None, name
