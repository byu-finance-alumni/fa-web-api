"""The two SURVEY photo paths, hardened by re-encoding rather than sniffing.

A survey respondent is the only genuinely untrusted uploader in the system: a
stranger holding a link that arrived in the post, with no account and nobody
checking the bytes before they land in the same bucket real headshots live in.
Both places their photo can move are covered here:

  * `POST /survey/respond/{token}/photo` — bytes enter the backend, so they are
    normalised BEFORE `stage_photo` writes anything and the hostile file never
    reaches the bucket at all.
  * `apply_response` — the authoritative gate. It catches anything staged BEFORE
    that route change shipped, which is not hypothetical: there is already a
    staged object in the bucket.

⚠️ EVERY hostile test here asserts THE PAYLOAD IS ABSENT FROM THE STORED BYTES.
Asserting a 204 or a completed apply proves nothing — the whole point is that a
polyglot passes `Image.verify()`, passes `Image.load()`, and passes any prefix
sniff. Only the re-encode removes it, so only the output can be the assertion.
"""

import asyncio
import io
import types

import pytest
from PIL import Image
from PIL.TiffImagePlugin import IFDRational

from app.core import rate_limit
from app.core.errors import ServiceError
from app.models.audit import AuditLog
from app.services import supabase_storage
from app.services import survey_responses as sr

PAYLOAD = b"<html><script>alert(document.domain)</script></html>"

_ORIENTATION = 0x0112
_GPS_IFD = 0x8825


def _encoded(fmt="JPEG", size=(400, 300), colour=(120, 90, 200), **save_kw) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format=fmt, **save_kw)
    return buf.getvalue()


def _phone_photo(orientation: int = 6) -> bytes:
    """A JPEG carrying what a phone actually writes: rotation and GPS.

    A headshot is very often taken at home, so the GPS tag in the file an alum
    uploads is their address. It must not survive into the bucket.
    """
    image = Image.new("RGB", (800, 600), (120, 90, 200))
    exif = image.getexif()
    exif[_ORIENTATION] = orientation
    gps = exif.get_ifd(_GPS_IFD)
    gps[1] = "N"
    gps[2] = (IFDRational(40, 1), IFDRational(14, 1), IFDRational(0, 1))
    gps[3] = "W"
    gps[4] = (IFDRational(111, 1), IFDRational(39, 1), IFDRational(0, 1))
    buf = io.BytesIO()
    image.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def _exif(data: bytes) -> tuple[dict, dict]:
    tags = Image.open(io.BytesIO(data)).getexif()
    return dict(tags), dict(tags.get_ifd(_GPS_IFD))


# ================================================ the public upload route =====


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.core.database import get_session
    from app.main import app

    async def _no_db_session():
        yield None

    rate_limit.reset()
    app.dependency_overrides[get_session] = _no_db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    rate_limit.reset()


@pytest.fixture
def staged(monkeypatch):
    """Capture what the route hands to `stage_photo` — the bucket's-eye view."""
    calls = []

    async def fake_stage(_session, _token, response_id, data, content_type):
        calls.append({"id": response_id, "data": data, "content_type": content_type})

    monkeypatch.setattr(sr, "stage_photo", fake_stage)
    return calls


def _post(client, token, name, data, declared):
    return client.post(
        f"/survey/respond/{token}/photo",
        data={"survey_response_id": 1},
        files={"photo": (name, data, declared)},
    )


def test_a_polyglot_is_staged_without_its_payload(client, staged):
    """The headline case: a real JPEG with HTML welded onto the end.

    It decodes, it renders, and every check the route used to run passed it. What
    must be true now is that the bytes in the bucket are not the bytes that were
    uploaded.
    """
    hostile = _encoded() + PAYLOAD
    assert PAYLOAD in hostile, "fixture carries no payload to strip"

    resp = _post(client, "tok-polyglot", "me.jpg", hostile, "image/jpeg")

    assert resp.status_code == 204
    assert len(staged) == 1
    assert PAYLOAD not in staged[0]["data"], "the payload reached storage"
    assert staged[0]["data"].startswith(b"\xff\xd8\xff")
    Image.open(io.BytesIO(staged[0]["data"])).load()  # still a usable picture


def test_gps_and_rotation_are_handled_end_to_end(client, staged):
    src = _phone_photo(orientation=6)
    assert _exif(src)[1], "fixture has no GPS to strip"

    assert _post(client, "tok-exif", "me.jpg", src, "image/jpeg").status_code == 204

    out = staged[0]["data"]
    tags, gps = _exif(out)
    assert gps == {}
    assert tags == {}
    # And the rotation the tag described was BAKED IN rather than discarded —
    # otherwise stripping metadata ships every portrait phone photo sideways.
    assert Image.open(io.BytesIO(out)).size == (600, 800)


def test_an_ordinary_photo_still_works(client, staged):
    src = _encoded(size=(500, 500))

    assert _post(client, "tok-normal", "me.jpg", src, "image/jpeg").status_code == 204

    assert Image.open(io.BytesIO(staged[0]["data"])).size == (500, 500)


def test_a_png_is_staged_as_a_jpeg_and_labelled_as_one(client, staged):
    """⚠️ The label has to describe what we STORED, not what was uploaded.

    Normalisation always emits JPEG, so a PNG upload recorded as `image/png` is a
    row that lies about its own object — and that content type is what the bucket
    serves the review preview and the promoted headshot with.
    """
    assert _post(client, "tok-png", "me.png", _encoded("PNG"), "image/png").status_code == 204

    assert staged[0]["content_type"] == "image/jpeg"
    assert staged[0]["data"].startswith(b"\xff\xd8\xff")


def test_html_wearing_a_jpeg_magic_number_is_refused_and_stages_nothing(client, staged):
    resp = _post(client, "tok-fake", "me.jpg", b"\xff\xd8\xff\xe0" + PAYLOAD, "image/jpeg")

    assert resp.status_code == 422
    assert staged == []
    message = resp.json()["error"]["message"]
    # It goes to a member of the public, so it has to help without leaking: no
    # uploaded bytes, no Pillow exception text, no stack.
    assert "could not be read" in message.lower()
    assert "html" not in message.lower()
    assert "Traceback" not in message


def test_a_non_image_never_reaches_the_decoder(client, staged):
    resp = _post(client, "tok-text", "me.jpg", b"not an image at all", "image/jpeg")

    assert resp.status_code == 422
    assert staged == []
    assert "JPEG, PNG, or WebP" in resp.json()["error"]["message"]


def test_a_format_outside_the_allow_list_is_refused_even_though_pillow_reads_it(
    client, staged
):
    """WHY the cheap magic-byte gate is still worth keeping.

    Pillow decodes GIF perfectly well, so without that gate this would be
    accepted and silently converted. Keeping it means hostile input can only ever
    reach the JPEG/PNG/WebP decoders rather than every plugin Pillow ships.
    """
    resp = _post(client, "tok-gif", "me.jpg", _encoded("GIF"), "image/jpeg")

    assert resp.status_code == 422
    assert staged == []


def test_a_mislabelled_but_real_photo_is_now_accepted(client, staged):
    """WHY the declared-type-vs-sniffed-type comparison was DROPPED.

    It guarded storing the uploader's bytes under the uploader's label. We store
    our own JPEG under `image/jpeg` whatever arrives, so the comparison protects
    nothing — while refusing a genuine photo whose browser mislabelled it, which
    is a real failure mode for a member of the public with one shot at a mailed
    link.
    """
    resp = _post(client, "tok-mislabel", "me.jpg", _encoded("WEBP"), "image/jpeg")

    assert resp.status_code == 204

    assert staged[0]["data"].startswith(b"\xff\xd8\xff")


def test_an_empty_file_keeps_its_own_message(client, staged):
    """A browser that submitted an empty part is a different fix for the alum
    than a photo we could not read, which is why this check survives the decode
    that would also have rejected it."""
    resp = _post(client, "tok-empty", "me.jpg", b"", "image/jpeg")

    assert resp.status_code == 422
    assert staged == []
    assert "empty" in resp.json()["error"]["message"].lower()


def test_a_disallowed_declared_type_is_still_refused_before_any_read(client, staged):
    resp = _post(client, "tok-svg", "me.svg", b"<svg/>", "image/svg+xml")

    assert resp.status_code == 422
    assert staged == []


# ======================================================== promotion (apply) ===
#
# `apply_response` is the last gate before bytes become an alum's real headshot,
# and the only one that can see an object staged before the route was fixed.


class _Scalar:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class _Session:
    def __init__(self, obj=None):
        self._obj = obj
        self.added = []
        self.committed = 0

    async def execute(self, _stmt):
        return _Scalar(self._obj)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed += 1


def _resp(**kw):
    base = dict(
        survey_response_id=1,
        alumni_id=5,
        payload={},
        status="pending",
        staged_photo_path="survey-pending/1",
        reviewed_by_user_id=None,
        reviewed_at=None,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def _promote(monkeypatch, resp, alum, staged_bytes, *, delete_raises=False):
    """Run `apply_response` against a fake bucket holding `staged_bytes`."""

    async def fake_get_pending(_s, _rid):
        return resp

    async def fake_side(_s, _ids):
        return ({}, {}, {})

    monkeypatch.setattr(sr, "_get_pending", fake_get_pending)
    monkeypatch.setattr(sr, "_load_side_rows", fake_side)

    calls = {"uploads": [], "deletes": []}

    async def fake_download(_bucket, path):
        if isinstance(staged_bytes, Exception):
            raise staged_bytes
        calls["downloaded"] = path
        return staged_bytes

    async def fake_upload(bucket, path, data, content_type):
        calls["uploads"].append((bucket, path, data, content_type))

    async def fake_delete(bucket, path):
        calls["deletes"].append((bucket, path))
        if delete_raises:
            raise ServiceError("storage is having a day")

    monkeypatch.setattr(supabase_storage, "download_object", fake_download)
    monkeypatch.setattr(supabase_storage, "upload_object", fake_upload)
    monkeypatch.setattr(supabase_storage, "delete_object", fake_delete)

    session = _Session(alum)
    outcome = asyncio.run(sr.apply_response(session, 1, actor_user_id=9))
    return outcome, calls, session


def _audit(session) -> str:
    rows = [o for o in session.added if isinstance(o, AuditLog)]
    assert len(rows) == 1
    return rows[0].new_value


def test_a_hostile_object_staged_before_this_shipped_is_scrubbed_on_promotion(
    monkeypatch,
):
    """The reason normalising at the route is not sufficient on its own.

    There is already a staged object in the bucket that predates that change.
    Approving it must not copy it onto a profile.
    """
    hostile = _encoded() + PAYLOAD
    resp = _resp()
    alum = types.SimpleNamespace(alumni_id=5, net_id="jdoe5")

    outcome, calls, _session = _promote(monkeypatch, resp, alum, hostile)

    assert outcome.photo_dropped is False
    bucket, path, data, content_type = calls["uploads"][0]
    assert (bucket, path, content_type) == ("headshots", "jdoe5", "image/jpeg")
    assert PAYLOAD not in data, "the payload was promoted onto a real profile"
    assert data.startswith(b"\xff\xd8\xff")
    # The staged copy is gone and the row no longer points at it.
    assert ("headshots", "survey-pending/1") in calls["deletes"]
    assert resp.staged_photo_path is None
    assert resp.status == "applied"


def test_gps_is_stripped_at_promotion_too(monkeypatch):
    resp = _resp()
    alum = types.SimpleNamespace(alumni_id=5, net_id="jdoe5")

    _outcome, calls, _session = _promote(monkeypatch, resp, alum, _phone_photo())

    tags, gps = _exif(calls["uploads"][0][2])
    assert (tags, gps) == ({}, {})


def test_an_undecodable_staged_photo_drops_the_photo_and_keeps_the_fields(monkeypatch):
    """⚠️ THE RESPONSE MUST NOT BECOME PERMANENTLY UN-APPROVABLE.

    Raising here would leave the row `pending` forever — every retry re-downloads
    the same bytes and fails the same way — so the only escape would be to reject
    it and throw the alum's field answers away. The judgement call is: apply the
    fields, discard the photo, leave the existing headshot alone, and TELL the
    reviewer.
    """
    resp = _resp(payload={"contact.city": "Provo"})
    alum = types.SimpleNamespace(alumni_id=5, net_id="jdoe5")
    contact = types.SimpleNamespace(alumni_id=5, city=None)

    async def fake_get_pending(_s, _rid):
        return resp

    async def fake_side(_s, _ids):
        return ({5: contact}, {}, {})

    monkeypatch.setattr(sr, "_get_pending", fake_get_pending)
    monkeypatch.setattr(sr, "_load_side_rows", fake_side)
    calls = {"uploads": [], "deletes": []}

    async def fake_download(_b, _p):
        return b"\xff\xd8\xff" + PAYLOAD  # JPEG magic, nothing decodable behind it

    async def fake_upload(*a):
        calls["uploads"].append(a)

    async def fake_delete(bucket, path):
        calls["deletes"].append((bucket, path))

    monkeypatch.setattr(supabase_storage, "download_object", fake_download)
    monkeypatch.setattr(supabase_storage, "upload_object", fake_upload)
    monkeypatch.setattr(supabase_storage, "delete_object", fake_delete)
    session = _Session(alum)
    outcome = asyncio.run(sr.apply_response(session, 1, actor_user_id=9))

    assert outcome.photo_dropped is True
    assert contact.city == "Provo"  # the good half of the submission landed
    assert resp.status == "applied"  # and the row reached a terminal state
    # Nothing was written to the headshot key: an unreadable photo is not a reason
    # to replace a good existing one with nothing.
    assert calls["uploads"] == []
    # The unreadable object does not linger, and nothing points at it any more.
    assert calls["deletes"] == [("headshots", "survey-pending/1")]
    assert resp.staged_photo_path is None
    # The reviewer's screen can be closed; the audit row cannot.
    assert "photo=dropped" in _audit(session)


def test_a_successful_promotion_says_so_in_the_audit_row(monkeypatch):
    resp = _resp()
    alum = types.SimpleNamespace(alumni_id=5, net_id="jdoe5")

    _outcome, _calls, session = _promote(monkeypatch, resp, alum, _encoded())

    assert "photo=applied" in _audit(session)


def test_an_apply_with_no_photo_says_nothing_about_photos(monkeypatch):
    resp = _resp(staged_photo_path=None)
    alum = types.SimpleNamespace(alumni_id=5, net_id="jdoe5")

    outcome, _calls, session = _promote(monkeypatch, resp, alum, _encoded())

    assert outcome.photo_dropped is False
    assert "photo=" not in _audit(session)


def test_cleanup_failing_does_not_wedge_the_drop_path(monkeypatch):
    """The drop path exists so a bad photo cannot make a response un-approvable.
    A storage hiccup while binning that photo must not undo it."""
    resp = _resp()
    alum = types.SimpleNamespace(alumni_id=5, net_id="jdoe5")

    outcome, _calls, _session = _promote(
        monkeypatch, resp, alum, b"\xff\xd8\xffgarbage", delete_raises=True
    )

    assert outcome.photo_dropped is True
    assert resp.status == "applied"


def test_storage_being_unreachable_still_fails_the_apply(monkeypatch):
    """A photo we could not FETCH is not a photo we know to be bad.

    Treating an outage as "drop the photo" would silently bin a real headshot on
    a transient 502, and the alum would have no way to tell. It has to fail so it
    can be retried.
    """
    resp = _resp()
    alum = types.SimpleNamespace(alumni_id=5, net_id="jdoe5")

    with pytest.raises(ServiceError):
        _promote(monkeypatch, resp, alum, ServiceError("storage down"))

    assert resp.status == "pending"
    assert resp.staged_photo_path == "survey-pending/1"


def test_the_apply_route_surfaces_a_dropped_photo(client, monkeypatch):
    """A bool nobody returns is a bool nobody can render — pin the wire format."""
    import uuid

    from app.api.dependencies.auth import get_current_db_user
    from app.main import app
    from app.schemas.auth import UserContext

    app.dependency_overrides[get_current_db_user] = lambda: UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=["super_admin"],
    )

    async def fake_apply(_session, _rid, _actor):
        return sr.ApplyOutcome(duplicate_warnings=[], photo_dropped=True)

    monkeypatch.setattr(sr, "apply_response", fake_apply)
    resp = client.post("/survey/responses/5/apply")

    assert resp.status_code == 200
    assert resp.json() == {"duplicate_warnings": [], "photo_dropped": True}
