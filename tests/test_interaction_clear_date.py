"""Offline regression test for clearing an interaction's date/time (FA-6).

The edit dialog sends ``interaction_date_time: null`` when the user clears the
field. ``InteractionUpdate`` treats an explicit null as a present value, so the
update service must overwrite the stored timestamp with ``None`` rather than
silently keeping the old one. This locks that in without Postgres.
"""

import asyncio
import datetime

from app.models.crm import Interaction
from app.schemas.profile import InteractionUpdate
from app.services import profile as service


class _FakeSession:
    def __init__(self, interaction: Interaction):
        self._interaction = interaction
        self.committed = 0

    async def get(self, model, pk):
        if model is Interaction:
            return self._interaction
        return None  # User lookup for logged_by -> unknown actor.

    async def commit(self) -> None:
        self.committed += 1

    async def refresh(self, obj) -> None:
        pass


def test_update_interaction_clears_date_when_null_sent():
    row = Interaction(
        interaction_id=3,
        alumni_id=1,
        interaction_type="Meeting",
        interaction_date_time=datetime.datetime(2026, 1, 2, 15, 30),
        interaction_notes="Coffee chat",
        user_id=None,
    )
    session = _FakeSession(row)
    # interaction_date_time explicitly null = a real "clear" request.
    payload = InteractionUpdate(interaction_type="Meeting", interaction_date_time=None)

    read = asyncio.run(
        service.update_interaction(session, 1, 3, payload, actor_user_id=None)
    )

    assert row.interaction_date_time is None
    assert read.interaction_date_time is None
