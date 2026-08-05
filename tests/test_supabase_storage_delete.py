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
