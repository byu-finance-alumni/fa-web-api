"""A fake session that can actually express the survey send log.

The suite's older fakes only record ``add()`` and count ``commit()``. That was
enough while the scheduler wrote log rows via the ORM, but it could not express
the two things that now matter most:

* an ``INSERT ... ON CONFLICT DO NOTHING RETURNING`` claim — the reservation the
  sender makes BEFORE it calls Resend — and its ``DELETE`` release;
* a commit that FAILS.

Both gaps are why several real bugs survived 1330 passing tests. This session
keeps a real ``(graduation_year, alumni_id, stage)`` set, honours the unique
constraint on it, and can be told to raise on the Nth commit.

Not a test module (no ``test_`` prefix), so pytest imports it rather than
collecting it.
"""

from types import SimpleNamespace

from sqlalchemy.sql.dml import Delete, Insert
from sqlalchemy.sql.selectable import Select


class _Result:
    def __init__(self, rows=None, one="__unset__"):
        self._rows = list(rows or [])
        self._one = one

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._rows))

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def one(self):
        return self._rows[0]

    def scalar_one_or_none(self):
        if self._one != "__unset__":
            return self._one
        return self._rows[0] if self._rows else None


class CommitFailed(Exception):
    """What a real session raises when a commit cannot be flushed."""


class SendLogSession:
    """Fake session with a working ``survey_send_log``.

    ``results`` queues canned results for ordinary SELECTs (in order); anything
    unqueued returns an empty result. INSERT/DELETE against ``survey_send_log``
    are executed for real against :attr:`send_log`.

    ``fail_commit_after`` makes the (N+1)-th ``commit()`` raise, so failure
    handling can be tested against a session that genuinely breaks.
    """

    def __init__(self, results=None, *, fail_commit_after=None):
        self._queue = list(results or [])
        self.added = []
        self.commits = 0
        self.executed = 0
        self.send_log: set[tuple[int, int, int]] = set()
        self._fail_commit_after = fail_commit_after

    # -- helpers -------------------------------------------------------------

    def seed_sent(self, graduation_year, stage, alumni_ids):
        for alumni_id in alumni_ids:
            self.send_log.add((graduation_year, alumni_id, stage))

    def logged(self, graduation_year, stage):
        return {
            alumni_id
            for year, alumni_id, s in self.send_log
            if year == graduation_year and s == stage
        }

    # -- session surface -----------------------------------------------------

    async def execute(self, stmt):
        self.executed += 1
        table = getattr(getattr(stmt, "table", None), "name", None)
        if isinstance(stmt, Insert) and table == "survey_send_log":
            return _Result(self._claim(stmt))
        if isinstance(stmt, Delete) and table == "survey_send_log":
            self._release(stmt)
            return _Result()
        if isinstance(stmt, Select):
            read = self._read_send_log(stmt)
            if read is not None:
                return _Result(read)
        if self._queue:
            return self._queue.pop(0)
        return _Result()

    def _read_send_log(self, stmt):
        """Serve `survey_email.logged_alumni_ids` from the real store, so a run
        SEES its own claims — without that, "did the first send stop the second?"
        could not be tested at all."""
        froms = stmt.get_final_froms()
        if not any(getattr(f, "name", None) == "survey_send_log" for f in froms):
            return None
        params = dict(stmt.compile().params)
        year, stage = params.get("graduation_year_1"), params.get("stage_1")
        if year is None or stage is None:
            return None
        return sorted(self.logged(year, stage))

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        if (
            self._fail_commit_after is not None
            and self.commits >= self._fail_commit_after
        ):
            raise CommitFailed("commit failed")
        self.commits += 1

    # -- survey_send_log emulation -------------------------------------------

    def _claim(self, stmt):
        """Apply the multi-row insert, honouring the unique constraint, and
        return the alumni_ids actually inserted (what RETURNING gives)."""
        claimed = []
        for row in stmt._multi_values[0]:
            values = {col.name: value for col, value in row.items()}
            key = (values["graduation_year"], values["alumni_id"], values["stage"])
            if key in self.send_log:
                continue  # ON CONFLICT DO NOTHING -> not returned
            self.send_log.add(key)
            claimed.append(values["alumni_id"])
        return claimed

    def _release(self, stmt):
        params = dict(stmt.compile().params)
        year = params.get("graduation_year_1")
        stage = params.get("stage_1")
        ids = set(params.get("alumni_id_1") or [])
        self.send_log -= {
            (year, alumni_id, stage) for alumni_id in ids
        }


def audits(session):
    return [a for a in session.added if type(a).__name__ == "AuditLog"]
