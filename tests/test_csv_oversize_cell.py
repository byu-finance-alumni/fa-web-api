"""A CSV cell over the ``csv`` module's 128 KB field limit (#597).

``csv.reader`` raises ``csv.Error`` on such a cell — in practice an unclosed
quote mark that swallows the rest of the file. Uncaught it was a 500 from every
importer. Each parser must instead return its ordinary file-level error list,
which the routes already turn into a clean ``columns_ok: false`` / rejected
response.
"""

import csv
import io
import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.main import app
from app.schemas.auth import UserContext
from app.services import attendee_match, import_csv, import_donations, import_events

_HUGE = "x" * (csv.field_size_limit() + 1)


def _file(headers: list[str], cells: list[str], *, huge_in_header: bool = False) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([*headers[:-1], _HUGE] if huge_in_header else headers)
    writer.writerow(cells)
    writer.writerow([_HUGE, *cells[1:]])
    return buf.getvalue().encode("utf-8")


def _assert_clean_error(errors: list[str]) -> None:
    assert len(errors) == 1
    assert "128 KB" in errors[0]
    assert _HUGE[:1000] not in errors[0]  # the cell is never echoed back


def _alumni_args():
    headers = import_csv.EXPECTED_HEADERS
    return headers, [""] * len(headers)


def _events_args():
    return import_events.EXPECTED_HEADERS, ["jdoe", "Jane", "Doe", ""]


def _donations_args():
    return import_donations.EXPECTED_HEADERS, ["1", "Jane", "Doe", "4", "2026", "10"]


@pytest.mark.parametrize("huge_in_header", [False, True])
def test_alumni_parse_returns_a_file_error_not_a_crash(huge_in_header):
    rows, errors = import_csv.parse_and_map(_file(*_alumni_args(), huge_in_header=huge_in_header))
    assert rows == []
    _assert_clean_error(errors)


def test_alumni_partial_parse_returns_a_file_error_not_a_crash():
    rows, errors, _ignored = import_csv.parse_and_map_partial(_file(*_alumni_args()))
    assert rows == []
    _assert_clean_error(errors)


@pytest.mark.parametrize("huge_in_header", [False, True])
def test_events_parse_returns_a_file_error_not_a_crash(huge_in_header):
    rows, errors = import_events.parse_and_map(
        _file(*_events_args(), huge_in_header=huge_in_header)
    )
    assert rows == []
    _assert_clean_error(errors)


@pytest.mark.parametrize("huge_in_header", [False, True])
def test_donations_parse_returns_a_file_error_not_a_crash(huge_in_header):
    rows, errors = import_donations.parse_and_map(
        _file(*_donations_args(), huge_in_header=huge_in_header)
    )
    assert rows == []
    _assert_clean_error(errors)


@pytest.mark.parametrize("huge_in_header", [False, True])
def test_attendee_match_parse_returns_a_file_error_not_a_crash(huge_in_header):
    data = _file(
        ["Name", "Personal Email"], ["Jane Doe", "j@dev.example"], huge_in_header=huge_in_header
    )
    rows, errors, _ignored = attendee_match.parse_and_map(data)
    assert rows == []
    _assert_clean_error(errors)


def test_an_unclosed_quote_is_reported_as_the_likely_cause():
    headers, cells = _events_args()
    text = ",".join(headers) + "\n" + 'jdoe,"Jane,Doe,\n' + ("filler\n" * 20_000)
    rows, errors = import_events.parse_and_map(text.encode("utf-8"))
    assert rows == []
    _assert_clean_error(errors)
    assert "quote" in errors[0]


# --- route level: a clean response, never a 500 ----------------------------


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
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_alumni_import_preview_reports_the_oversize_cell(client):
    response = client.post(
        "/alumni/import/preview",
        files={"file": ("a.csv", _file(*_alumni_args()), "text/csv")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["columns_ok"] is False
    _assert_clean_error(body["header_errors"])


def test_events_import_preview_reports_the_oversize_cell(client):
    response = client.post(
        "/events/import/preview",
        data={"event_name": "Banquet"},
        files={"file": ("a.csv", _file(*_events_args()), "text/csv")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["columns_ok"] is False
    _assert_clean_error(body["header_errors"])
