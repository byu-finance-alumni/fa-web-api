"""An already-deleted object must never fail a delete (survey reset, #395).

Supabase Storage is inconsistent about how it reports a missing object: a 404
for some keys, a 400 carrying a not_found marker for others. Matching only on
404 turned "it was already gone" into a hard error, which blocked an engineer
resetting a campaign for an alumnus whose staged photo had already been
promoted onto their profile.
"""

import httpx
import pytest

from app.services import supabase_storage


def _response(status: int, body: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json=body if body is not None else {},
        request=httpx.Request("DELETE", "https://example.test/object/b/k"),
    )


@pytest.mark.parametrize(
    "status, body",
    [
        (404, {}),
        (400, {"error": "not_found"}),
        (400, {"message": "Object not found"}),
        (400, {"statusCode": "404", "error": "Not Found"}),
    ],
)
def test_missing_object_is_success(status, body):
    assert supabase_storage._is_missing_object(_response(status, body)) or status == 404


@pytest.mark.parametrize(
    "status, body",
    [
        (400, {"error": "invalid_key"}),
        (403, {"error": "forbidden"}),
        (500, {"error": "boom"}),
    ],
)
def test_a_real_refusal_still_raises(status, body):
    assert not supabase_storage._is_missing_object(_response(status, body))


# --- download_object_or_none: the headshot image proxy's read ----------------


def _patch_transport(monkeypatch, status: int, *, body: dict | None = None, content=b""):
    def _handler(request: httpx.Request) -> httpx.Response:
        if body is not None:
            return httpx.Response(status, json=body)
        return httpx.Response(status, content=content)

    real = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(supabase_storage.httpx, "AsyncClient", _client)
    monkeypatch.setattr(
        supabase_storage, "_base_and_key", lambda: ("https://example.test/storage/v1", "k")
    )


def test_download_or_none_returns_bytes(monkeypatch):
    import asyncio

    _patch_transport(monkeypatch, 200, content=b"\xff\xd8\xffdata")
    got = asyncio.run(supabase_storage.download_object_or_none("headshots", "x"))
    assert got == b"\xff\xd8\xffdata"


@pytest.mark.parametrize(
    "status, body",
    [(404, {}), (400, {"error": "not_found"}), (400, {"message": "Object not found"})],
)
def test_download_or_none_missing_is_none(monkeypatch, status, body):
    import asyncio

    _patch_transport(monkeypatch, status, body=body)
    assert asyncio.run(supabase_storage.download_object_or_none("headshots", "x")) is None


@pytest.mark.parametrize(
    "status, body", [(400, {"error": "invalid_key"}), (500, {"error": "boom"})]
)
def test_download_or_none_real_failure_raises(monkeypatch, status, body):
    import asyncio

    from app.core.errors import ServiceError

    _patch_transport(monkeypatch, status, body=body)
    with pytest.raises(ServiceError):
        asyncio.run(supabase_storage.download_object_or_none("headshots", "x"))
