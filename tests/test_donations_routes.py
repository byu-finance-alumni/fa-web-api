"""Tests for the Pay It Forward Fund donation routes (#161, #278).

Read access (#278): the ledger reads (``/donors``, ``/summary``,
``/alumni/{id}``) require the ``alumni.full`` capability — students and
view_only are DENIED (403) outright. FIELD-LEVEL amount gating still nulls
dollar amounts server-side for any authorized reader lacking ``alumni.full``
(belt-and-suspenders that holds if an engineer later widens who may read);
authorized readers (full_access+) see real numbers. Writes are super_admin-only.

No real DATABASE_URL is required (CI has none); sessions are stubbed.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.main import app
from app.schemas.auth import UserContext


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


@pytest.fixture
def client():
    async def _no_db():
        yield None

    app.dependency_overrides[get_session] = _no_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        return _Result(self._rows)


class _SeqSession:
    """Returns queued ``execute().all()`` results and ``scalar()`` values in
    call order; ``get`` returns a fixed object. Records added rows."""

    def __init__(self, *, results=None, scalars=None, get_obj=None):
        self._results = list(results or [])
        self._scalars = list(scalars or [])
        self._get_obj = get_obj
        self.added: list = []
        self.committed = False
        self.deleted: list = []

    async def execute(self, _stmt):
        return _Result(self._results.pop(0) if self._results else [])

    async def scalar(self, _stmt):
        return self._scalars.pop(0) if self._scalars else 0

    async def get(self, _model, _pk):
        return self._get_obj

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if hasattr(obj, "donation_id") and getattr(obj, "donation_id", None) is None:
                obj.donation_id = 555

    async def commit(self):
        self.committed = True

    async def refresh(self, _obj):
        pass

    async def delete(self, obj):
        self.deleted.append(obj)


def _with_session(session):
    async def _override():
        yield session

    return _override


# --- auth gating --------------------------------------------------------------


@pytest.mark.parametrize("path", ["/donations/donors", "/donations/summary"])
def test_donor_reads_require_auth(client, path):
    assert client.get(path).status_code == 401


# --- amount gating (the FERPA-critical property) ------------------------------


def _donor_session():
    # base aggregate row + per-year rows for one donor.
    return _SeqSession(
        results=[
            [(42, "Jane", None, "Doe", 2018, 3, 1500)],  # base aggregate
            [(42, 2026, 1000), (42, 2025, 500)],  # per-year
        ]
    )


@pytest.mark.parametrize("role", ["view_only", "student"])
@pytest.mark.parametrize(
    "path", ["/donations/donors", "/donations/summary", "/donations/alumni/42"]
)
def test_ledger_reads_forbidden_for_students_and_view_only(client, role, path):
    # #278: Pay It Forward is a data-management surface, not part of the
    # read-only directory. Students and view_only are DENIED the ledger reads
    # outright (403) — not merely amount-gated.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
    response = client.get(path)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_donors_shows_amounts_for_full_access(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(_donor_session())
    donor = client.get("/donations/donors").json()["items"][0]
    assert donor["lifetime_total"] == 1500.0
    assert {py["year"]: py["total"] for py in donor["per_year"]} == {
        2026: 1000.0,
        2025: 500.0,
    }


def test_summary_shows_amounts_for_full_access(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    session = _SeqSession(
        scalars=[5, 12, 9999],  # donor_count, donation_count, total_raised
        results=[[(2026, 4, 9999)]],  # per-year (year, donor_count, total)
    )
    app.dependency_overrides[get_session] = _with_session(session)
    body = client.get("/donations/summary").json()
    assert body["donor_count"] == 5
    assert body["donation_count"] == 12
    assert body["total_raised"] == 9999.0
    assert body["per_year"][0]["donor_count"] == 4
    assert body["per_year"][0]["total"] == 9999.0


def test_summary_shows_amounts_for_super_admin(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")
    session = _SeqSession(
        scalars=[5, 12, 9999],
        results=[[(2026, 4, 9999)]],
    )
    app.dependency_overrides[get_session] = _with_session(session)
    body = client.get("/donations/summary").json()
    assert body["total_raised"] == 9999.0
    assert body["per_year"][0]["total"] == 9999.0


# --- write authorization (super_admin only) -----------------------------------


@pytest.mark.parametrize("role", ["view_only", "student", "full_access"])
def test_add_donation_forbidden_below_super_admin(client, role):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
    response = client.post(
        "/donations/alumni/42", json={"amount": "100.00", "year": 2026}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_add_donation_forbidden_for_user_admin_only_role(client):
    # #189: donation writes are gated on the dedicated ``donations.manage``
    # capability, NOT ``user_admin``. A role holding ONLY user_admin (i.e. if
    # user administration were delegated) must NOT gain donation-ledger writes.
    from app.api.dependencies.auth import get_permission_config
    from app.core.capabilities import Capability

    app.dependency_overrides[get_current_db_user] = lambda: _ctx("delegated_admin")
    app.dependency_overrides[get_permission_config] = lambda: {
        "delegated_admin": frozenset({Capability.USER_ADMIN})
    }
    response = client.post(
        "/donations/alumni/42", json={"amount": "100.00", "year": 2026}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_import_forbidden_for_user_admin_only_role(client):
    # Same guarantee on the bulk-import path (#189).
    from app.api.dependencies.auth import get_permission_config
    from app.core.capabilities import Capability

    app.dependency_overrides[get_current_db_user] = lambda: _ctx("delegated_admin")
    app.dependency_overrides[get_permission_config] = lambda: {
        "delegated_admin": frozenset({Capability.USER_ADMIN})
    }
    response = client.post(
        "/donations/import/preview",
        files={"file": ("d.csv", b"Net ID,Name,Month,Year,Amount\n", "text/csv")},
    )
    assert response.status_code == 403


def test_add_donation_requires_auth(client):
    response = client.post(
        "/donations/alumni/42", json={"amount": "100.00", "year": 2026}
    )
    assert response.status_code == 401


def test_add_donation_unknown_alumni_is_404(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")
    app.dependency_overrides[get_session] = _with_session(
        _SeqSession(get_obj=None)
    )
    response = client.post(
        "/donations/alumni/999", json={"amount": "100.00", "year": 2026}
    )
    assert response.status_code == 404


def test_add_donation_rejects_negative_amount(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")
    response = client.post(
        "/donations/alumni/42", json={"amount": "-5", "year": 2026}
    )
    assert response.status_code == 422


def test_add_donation_rejects_bad_month(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")
    response = client.post(
        "/donations/alumni/42", json={"amount": "5", "year": 2026, "month": 13}
    )
    assert response.status_code == 422


def test_add_donation_happy_path_creates_and_audits(client):
    from types import SimpleNamespace

    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")
    alumni = SimpleNamespace(alumni_id=42, archived=False)
    session = _SeqSession(get_obj=alumni)
    app.dependency_overrides[get_session] = _with_session(session)
    response = client.post(
        "/donations/alumni/42",
        json={"amount": "250.50", "year": 2026, "month": 4, "notes": "Spring gift"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["donation_id"] == 555
    assert body["amount"] == 250.5
    assert body["year"] == 2026
    assert body["month"] == 4
    assert session.committed is True
    donations = [o for o in session.added if hasattr(o, "donation_year")]
    audits = [o for o in session.added if hasattr(o, "action_type")]
    assert len(donations) == 1
    assert donations[0].logged_by_user_id == 1
    assert len(audits) == 1
    assert audits[0].entity_type == "donation"
    assert audits[0].action_type == "create"


def test_import_preview_forbidden_for_full_access(client):
    # Bulk donation import is super_admin-only; full_access is rejected.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.post(
        "/donations/import/preview",
        files={
            "file": (
                "d.csv",
                b"MSTID,First name,Last name,Month,Year,Amount\n",
                "text/csv",
            )
        },
    )
    assert response.status_code == 403


# --- QA-hardening regressions -------------------------------------------------


def test_add_donation_rejects_zero_amount(client):
    # A $0 gift carries no financial meaning — rejected at the schema layer.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")
    response = client.post(
        "/donations/alumni/42", json={"amount": "0", "year": 2026}
    )
    assert response.status_code == 422


def test_add_donation_archived_alumni_is_404(client):
    from types import SimpleNamespace

    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")
    archived = SimpleNamespace(alumni_id=42, archived=True)
    app.dependency_overrides[get_session] = _with_session(
        _SeqSession(get_obj=archived)
    )
    response = client.post(
        "/donations/alumni/42", json={"amount": "100", "year": 2026}
    )
    assert response.status_code == 404


def test_update_donation_null_year_is_422_not_500(client):
    # An explicit {"year": null} must not reach the NOT NULL column (would 500).
    from types import SimpleNamespace

    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")
    donation = SimpleNamespace(
        donation_id=5, alumni_id=42, amount=100, donation_year=2026,
        donation_month=4, notes=None,
    )
    app.dependency_overrides[get_session] = _with_session(
        _SeqSession(get_obj=donation)
    )
    response = client.patch("/donations/5", json={"year": None})
    assert response.status_code == 422


def _donation(donation_id, year, month, amount, notes=None):
    from types import SimpleNamespace

    return SimpleNamespace(
        donation_id=donation_id,
        donation_year=year,
        donation_month=month,
        amount=amount,
        notes=notes,
    )


def test_alumni_donations_shows_amounts_for_full_access(client):
    from types import SimpleNamespace

    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    alumni = SimpleNamespace(
        alumni_id=42, first_name="Jane", preferred_first_name=None, last_name="Doe"
    )
    session = _SeqSession(
        get_obj=alumni,
        results=[[_donation(1, 2026, 4, 250, notes="memo")]],
    )
    app.dependency_overrides[get_session] = _with_session(session)
    body = client.get("/donations/alumni/42").json()
    assert body["lifetime_total"] == 250.0
    assert body["donations"][0]["amount"] == 250.0
    assert body["donations"][0]["notes"] == "memo"


# --- H3: amount / year server-side validation ---------------------------------


def test_add_donation_rejects_huge_amount(client):
    # An absurd amount above the numeric(12,2) ceiling is a 422.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")
    response = client.post(
        "/donations/alumni/42",
        json={"amount": "99999999999999.99", "year": 2026},
    )
    assert response.status_code == 422


def test_add_donation_rejects_year_9999(client):
    # An out-of-range far-future year is a 422 (upper bound is current_year + 1).
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")
    response = client.post(
        "/donations/alumni/42", json={"amount": "100", "year": 9999}
    )
    assert response.status_code == 422


def test_add_donation_rejects_year_too_old(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")
    response = client.post(
        "/donations/alumni/42", json={"amount": "100", "year": 1800}
    )
    assert response.status_code == 422


def test_update_donation_rejects_year_9999(client):
    from types import SimpleNamespace

    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")
    donation = SimpleNamespace(
        donation_id=5, alumni_id=42, amount=100, donation_year=2026,
        donation_month=4, notes=None,
    )
    app.dependency_overrides[get_session] = _with_session(
        _SeqSession(get_obj=donation)
    )
    response = client.patch("/donations/5", json={"year": 9999})
    assert response.status_code == 422


# --- donation delete (donations.manage: super_admin / engineer, audited, 204) --


@pytest.mark.parametrize("role", ["view_only", "student", "full_access"])
def test_delete_donation_forbidden_below_donations_manage(client, role):
    # DELETE is gated to the donations.manage tier (super_admin / engineer),
    # matching add / update — view_only / student / full_access are 403.
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
    response = client.delete("/donations/5")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_delete_donation_requires_auth(client):
    assert client.delete("/donations/5").status_code == 401


def test_delete_donation_missing_is_404(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")
    app.dependency_overrides[get_session] = _with_session(
        _SeqSession(get_obj=None)
    )
    response = client.delete("/donations/999")
    assert response.status_code == 404


@pytest.mark.parametrize("role", ["super_admin", "engineer"])
def test_delete_donation_succeeds_and_audits(client, role):
    # donations.manage (super_admin / engineer) may delete; the row is removed,
    # an audit entry is written (FERPA trail), and the response is a bare 204.
    donation = _donation(5, 2026, 4, 250, notes="memo")
    session = _SeqSession(get_obj=donation)
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
    app.dependency_overrides[get_session] = _with_session(session)
    response = client.delete("/donations/5")
    assert response.status_code == 204
    assert response.content == b""
    assert session.committed is True
    assert session.deleted == [donation]
    audits = [o for o in session.added if hasattr(o, "action_type")]
    assert len(audits) == 1
    assert audits[0].entity_type == "donation"
    assert audits[0].action_type == "delete"
    assert audits[0].user_id == 1
