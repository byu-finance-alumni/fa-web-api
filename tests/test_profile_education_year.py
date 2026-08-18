"""Offline regression tests for the education degree-year write path (FA-4).

A fake session captures the ``EducationHistory`` row the service builds and
stamps a primary key on ``refresh`` (so the post-commit ``model_validate`` of
the read schema succeeds without Postgres). These lock in that the entered
``degree_year`` actually reaches the persisted row on both add and update —
the field a QA pass reported as silently dropped.
"""

import asyncio

from app.models.alumni import Alumni
from app.models.employment import EducationHistory
from app.schemas.profile import EducationCreate, EducationUpdate
from app.services import profile as service


class _FakeSession:
    """Minimal async session: serves ``get`` lookups, records ``add``, and
    assigns a PK on ``refresh`` so read-schema validation can run."""

    def __init__(self, *, alumni: Alumni | None = None, education: EducationHistory | None = None):
        self._alumni = alumni
        self._education = education
        self.added: list[object] = []
        self.committed = 0

    async def get(self, model, pk):
        if model is Alumni:
            return self._alumni
        if model is EducationHistory:
            return self._education
        return None

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        # ``add_education`` flushes before auditing so the audit row can name the
        # id the INSERT generated (#45); stamp it here as Postgres would.
        self._assign_pk()

    async def commit(self) -> None:
        self.committed += 1

    async def refresh(self, obj) -> None:
        # Stamp the generated PK the DB would assign so EducationRead validates.
        self._assign_pk()

    def _assign_pk(self) -> None:
        for obj in self.added:
            if isinstance(obj, EducationHistory) and obj.education_id is None:
                obj.education_id = 1


def test_add_education_persists_degree_year():
    alumnus = Alumni(alumni_id=1, first_name="Jane", last_name="Doe")
    session = _FakeSession(alumni=alumnus)
    payload = EducationCreate(university="BYU", degree="BS", degree_year=2019)

    read = asyncio.run(
        service.add_education(session, 1, payload, actor_user_id=None)
    )

    row = next(o for o in session.added if isinstance(o, EducationHistory))
    assert row.degree_year == 2019
    assert read.degree_year == 2019


def test_add_education_persists_year_only_entry():
    # Even when degree_year is the only meaningful value, it must persist.
    alumnus = Alumni(alumni_id=1, first_name="Jane", last_name="Doe")
    session = _FakeSession(alumni=alumnus)
    payload = EducationCreate(degree_year=2005)

    read = asyncio.run(
        service.add_education(session, 1, payload, actor_user_id=None)
    )

    assert read.degree_year == 2005


def test_update_education_persists_degree_year():
    existing = EducationHistory(education_id=7, alumni_id=1, degree_year=None)
    session = _FakeSession(education=existing)
    payload = EducationUpdate(degree_year=2021)

    read = asyncio.run(
        service.update_education(session, 1, 7, payload, actor_user_id=None)
    )

    assert existing.degree_year == 2021
    assert read.degree_year == 2021
