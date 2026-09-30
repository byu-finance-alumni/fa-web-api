"""A not-in-labor-force alum can still save their RESIDENCE city.

The "no employer to record" exemption for Not in the Labor Force / Unemployed /
Graduate Student / Military (EMPLOYER_NOT_APPLICABLE_STATUSES / employer_applies
in app.core.dropdowns) lives ONLY on the staff read side (missing-employer flag,
completeness score, employer display fallback). It is NOT wired into the survey
submit/stage path, and these tests exist so it never quietly becomes so: the
residence city (contact.city) must stage regardless of employment status, and a
not-in-labor-force submission carrying one must stage status='pending' -- the
status that excludes the alum from the follow-up reminders.
"""
import asyncio
import types

import pytest

from app.core.dropdowns import EMPLOYER_NOT_APPLICABLE_STATUSES, SURVEY_EMPLOYMENT_STATUSES
from app.models.survey_response import SurveyResponse
from app.services import survey_email
from app.services import survey_responses as sr
from app.services.survey_responses import _IGNORE, _coerce


class _Scalar:
    def __init__(self, o):
        self._o = o

    def scalar_one_or_none(self):
        return self._o

    def first(self):  # send-log stamp lookup calls .first()
        return self._o


class _Session:
    def __init__(self, alum):
        self.alum = alum
        self.added = []
        self.committed = 0

    async def execute(self, stmt):
        try:
            froms = {getattr(f, "name", None) for f in stmt.get_final_froms()}
        except Exception:
            froms = set()
        if "alumni" in froms:
            return _Scalar(self.alum)
        return _Scalar(None)  # no send-log stamp, no existing reply to upgrade

    def add(self, o):
        self.added.append(o)

    async def flush(self):
        for o in self.added:
            if isinstance(o, SurveyResponse) and o.survey_response_id is None:
                o.survey_response_id = 777

    async def commit(self):
        self.committed += 1


def _alum():
    return types.SimpleNamespace(alumni_id=5, archived=False, graduation_year=2020)


def _submit(session, monkeypatch, fields):
    monkeypatch.setattr(sr, "verify_survey_token", lambda _t: 5)
    return asyncio.run(sr.submit_response(session, "tok", fields))


def _staged(session):
    return [o for o in session.added if isinstance(o, SurveyResponse)]


def test_residence_city_and_employment_city_are_distinct_columns():
    res = sr._FIELD_BY_KEY["contact.city"]
    emp = sr._FIELD_BY_KEY["employment.current_city"]
    assert (res.group, res.column, res.label) == ("contact", "city", "Residence city")
    assert (emp.group, emp.column, emp.label) == ("employment", "current_city", "Employment city")


@pytest.mark.parametrize("status", EMPLOYER_NOT_APPLICABLE_STATUSES)
def test_not_employed_statuses_are_all_survey_selectable(status):
    assert status in SURVEY_EMPLOYMENT_STATUSES


@pytest.mark.parametrize("status", EMPLOYER_NOT_APPLICABLE_STATUSES)
def test_residence_city_coerces_regardless_of_status(status):
    assert _coerce(sr._FIELD_BY_KEY["profile.employment_status"], status) == status
    assert _coerce(sr._FIELD_BY_KEY["contact.city"], "Provo") == "Provo"
    assert _coerce(sr._FIELD_BY_KEY["contact.city"], "Provo") is not _IGNORE


def test_not_in_labor_force_with_residence_city_stages_pending(monkeypatch):
    session = _Session(_alum())
    result = _submit(
        session,
        monkeypatch,
        {"profile.employment_status": "Not in the Labor Force", "contact.city": "Provo"},
    )
    assert result.staged is True and result.change_count == 2
    row = _staged(session)[0]
    assert row.status == survey_email.STATUS_PENDING
    assert row.payload["contact.city"] == "Provo"
    assert row.payload["profile.employment_status"] == "Not in the Labor Force"
    assert session.committed == 1


def test_pending_is_a_reply_that_stops_reminders():
    assert survey_email.STATUS_PENDING in survey_email.RESPONDED_STATUSES


def test_residence_city_only_submission_still_stages(monkeypatch):
    session = _Session(_alum())
    result = _submit(session, monkeypatch, {"contact.city": "Provo"})
    assert result.staged is True and result.change_count == 1
    assert _staged(session)[0].payload == {"contact.city": "Provo"}
