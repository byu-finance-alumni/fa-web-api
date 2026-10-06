"""Headshots for non-editors are served through the site, never signed.

A headshot's object key is the alumnus's net ID, and a signed storage URL
carries that key in its path and its token. Net ID is a VIEW_ONLY_HIDDEN_FIELD,
so a caller without ``can_edit_alumni`` must never receive a signed URL — the
URL routes hand them the app-relative proxy path instead, and
``GET /alumni/{id}/headshot/image`` serves the bytes for the app to stream back
(2026-10-02 breach test). Editors keep direct signed URLs.
"""

import io
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.api.dependencies.auth import get_current_db_user
from app.api.routes import alumni as alumni_routes
from app.core.database import get_session
from app.main import app
from app.schemas.auth import UserContext

_NET_ID = "jdoe12"
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _ctx(role: str, user_id: int = 1) -> UserContext:
    return UserContext(
        user_id=user_id,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=[role],
    )


class _Session:
    def __init__(self, rows=(), one=None):
        self._rows = list(rows)
        self._one = one

    async def scalars(self, stmt):
        return SimpleNamespace(all=lambda: list(self._rows))

    async def scalar(self, stmt):
        return self._one


def _alum(alumni_id=5, net_id=_NET_ID, archived=False):
    return SimpleNamespace(alumni_id=alumni_id, net_id=net_id, archived=archived)


@pytest.fixture
def as_role():
    def _set(role, session, user_id=1):
        async def _override():
            yield session

        app.dependency_overrides[get_session] = _override
        app.dependency_overrides[get_current_db_user] = lambda: _ctx(role, user_id)
        return TestClient(app)

    yield _set
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_db_user, None)


@pytest.fixture
def storage(monkeypatch):
    """Fake storage: ``objects`` maps key -> bytes; a missing key has no image."""
    objects: dict[str, bytes] = {_NET_ID: _JPEG}

    async def _sign(bucket, path, **kwargs):
        if path not in objects:
            return None
        return f"https://storage.test/object/sign/{bucket}/{path}?token=t.{path}"

    async def _download(bucket, path):
        assert bucket == "headshots"
        return objects.get(path)

    monkeypatch.setattr(alumni_routes.supabase_storage, "create_signed_url", _sign)
    monkeypatch.setattr(
        alumni_routes.supabase_storage, "download_object_or_none", _download
    )
    return objects


# --- the URL routes ---------------------------------------------------------


@pytest.mark.parametrize("role", ["view_only"])
def test_non_editor_batch_gets_proxy_path_never_a_signed_url(as_role, storage, role):
    session = _Session(rows=[_alum(5), _alum(6, net_id="nophoto1"), _alum(7, net_id=None)])
    with as_role(role, session) as c:
        resp = c.get("/alumni/headshots/urls?alumni_ids=5&alumni_ids=6&alumni_ids=7")
    assert resp.status_code == 200
    assert resp.json()["urls"] == {"5": "/api/headshot/5", "6": None, "7": None}
    assert _NET_ID not in resp.text
    assert "storage.test" not in resp.text


@pytest.mark.parametrize("role", ["student", "full_access", "super_admin", "engineer"])
def test_editor_batch_keeps_direct_signed_url(as_role, storage, role):
    with as_role(role, _Session(rows=[_alum(5)])) as c:
        resp = c.get("/alumni/headshots/urls?alumni_ids=5")
    assert resp.status_code == 200
    assert resp.json()["urls"]["5"].startswith("https://storage.test/object/sign/headshots/")


def test_non_editor_single_url_is_proxy_path(as_role, storage):
    with as_role("view_only", _Session(one=_alum(5))) as c:
        resp = c.get("/alumni/5/headshot")
    assert resp.status_code == 200
    assert resp.json() == {"url": "/api/headshot/5"}
    assert _NET_ID not in resp.text


def test_non_editor_single_url_null_when_no_image(as_role, storage):
    storage.clear()
    with as_role("view_only", _Session(one=_alum(5))) as c:
        resp = c.get("/alumni/5/headshot")
    assert resp.json() == {"url": None}


def test_editor_single_url_is_signed(as_role, storage):
    with as_role("full_access", _Session(one=_alum(5))) as c:
        resp = c.get("/alumni/5/headshot")
    assert resp.json()["url"].startswith("https://storage.test/")


# --- the image route --------------------------------------------------------


def test_image_requires_auth():
    with TestClient(app) as c:
        assert c.get("/alumni/5/headshot/image").status_code == 401


@pytest.mark.parametrize("role", ["view_only", "student", "full_access"])
def test_image_streams_bytes_with_private_cache(as_role, storage, role):
    with as_role(role, _Session(one=_alum(5))) as c:
        resp = c.get("/alumni/5/headshot/image")
    assert resp.status_code == 200
    assert resp.content == _JPEG
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.headers["cache-control"] == "private, max-age=600"
    assert resp.headers["x-content-type-options"] == "nosniff"
    # Nothing in the headers names the object key.
    assert _NET_ID not in str(resp.headers).lower()


def test_image_content_type_comes_from_the_bytes(as_role, storage):
    storage[_NET_ID] = _PNG
    with as_role("view_only", _Session(one=_alum(5))) as c:
        resp = c.get("/alumni/5/headshot/image")
    assert resp.headers["content-type"] == "image/png"


@pytest.mark.parametrize(
    "alum",
    [None, _alum(5, net_id=None), _alum(5, net_id="  "), _alum(5, net_id="nophoto1")],
)
def test_image_404_when_nothing_to_show(as_role, storage, alum):
    with as_role("view_only", _Session(one=alum)) as c:
        resp = c.get("/alumni/5/headshot/image")
    assert resp.status_code == 404
    assert _NET_ID not in resp.text


def test_image_refuses_non_image_bytes(as_role, storage):
    storage[_NET_ID] = b"<html><script>alert(1)</script></html>"
    with as_role("view_only", _Session(one=_alum(5))) as c:
        resp = c.get("/alumni/5/headshot/image")
    assert resp.status_code == 404
    assert b"script" not in resp.content


@pytest.mark.parametrize(
    ("role", "status"), [("view_only", 404), ("student", 404), ("full_access", 200)]
)
def test_image_archived_follows_the_url_route_rule(as_role, storage, role, status):
    with as_role(role, _Session(one=_alum(5, archived=True))) as c:
        assert c.get("/alumni/5/headshot/image").status_code == status


def test_oversized_raw_object_is_reencoded_not_passed_through(as_role, storage, monkeypatch):
    monkeypatch.setattr(alumni_routes, "_HEADSHOT_IMAGE_MAX_PASSTHROUGH_BYTES", 1024)
    buf = io.BytesIO()
    Image.effect_noise((1600, 1200), 64).convert("RGB").save(buf, format="PNG")
    big = buf.getvalue()
    assert len(big) > 1024
    storage[_NET_ID] = big
    with as_role("view_only", _Session(one=_alum(5))) as c:
        resp = c.get("/alumni/5/headshot/image")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert max(Image.open(io.BytesIO(resp.content)).size) == 1024


def test_image_has_its_own_budget_and_brakes(as_role, storage, monkeypatch):
    from app.services import failure_alert

    monkeypatch.setattr(failure_alert, "alerting_enabled", lambda: False)
    per_minute = alumni_routes._HEADSHOT_IMAGE_WINDOWS[0][0]
    with as_role("view_only", _Session(one=_alum(5))) as c:
        statuses = [c.get("/alumni/5/headshot/image").status_code for _ in range(per_minute)]
        assert set(statuses) == {200}
        assert c.get("/alumni/5/headshot/image").status_code == 429
        # The browse budget was not spent by photos.
        assert c.get("/alumni/5/headshot").status_code == 200


def test_image_budget_covers_fast_roster_paging():
    # A roster page renders 25 photos; ~10 pages a minute must never 429.
    (per_min, _), (per_hour, _) = alumni_routes._HEADSHOT_IMAGE_WINDOWS
    assert per_min >= 25 * 10
    assert per_hour >= 25 * 72 * 2  # the whole roster, twice an hour
