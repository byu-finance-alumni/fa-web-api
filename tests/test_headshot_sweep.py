"""Tests for the nightly headshot sweep (`services/headshot_sweep`).

This job REWRITES PHOTOS THAT ARE ALREADY STORED, so most of what is asserted
here is what it must NOT do: never touch an object it cannot improve, never
destroy one it cannot read, never go near `survey-pending/`, and never download
something it has already normalised.

The bucket is faked in memory. The image work is real Pillow — the point of
several of these tests is the actual before/after byte count.
"""

import asyncio
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.core.errors import ServiceError
from app.main import app
from app.services import headshot_sweep

THRESHOLD = headshot_sweep._SKIP_UNDER_BYTES


def _run(coro):
    """Drive one coroutine to completion — this suite has no async plugin."""
    return asyncio.run(coro)


# ------------------------------------------------------------- fixtures -----


def _photo(size=(2400, 1800)) -> bytes:
    """A big JPEG that behaves like a real phone photo.

    A flat colour is useless here: it compresses to a few KB and would never be
    eligible. This is a smooth gradient plus grain, which is large before the
    downscale and small after it — exactly the shape the sweep exists for.
    """
    base = Image.linear_gradient("L").resize(size)
    grain = Image.effect_noise(size, 24)
    buf = io.BytesIO()
    Image.merge("RGB", (base, grain, base)).save(buf, format="JPEG", quality=95)
    data = buf.getvalue()
    assert len(data) > THRESHOLD, "fixture is not actually oversized"
    return data


def _small_photo() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (300, 300), (40, 90, 160)).save(buf, format="JPEG", quality=90)
    data = buf.getvalue()
    assert len(data) <= THRESHOLD, "fixture is not actually small"
    return data


class FakeBucket:
    """In-memory stand-in for the Supabase Storage REST calls the sweep uses.

    Records every download and upload so a test can assert that an object was
    never fetched at all — "we did not spend the bandwidth" is a behaviour worth
    pinning, not an implementation detail.
    """

    def __init__(self, objects: dict[str, bytes], *, folders: tuple[str, ...] = ()):
        self.objects = dict(objects)
        self.folders = folders
        self.downloads: list[str] = []
        self.uploads: list[str] = []
        self.download_error: str | None = None

    async def list_objects(self, bucket, *, prefix="", limit=100, offset=0):
        rows = [
            # A folder placeholder: real Supabase rows carry `metadata: None`.
            {"name": name, "metadata": None}
            for name in self.folders
        ] + [
            {"name": name, "metadata": {"size": len(data), "mimetype": "image/jpeg"}}
            for name, data in self.objects.items()
        ]
        rows.sort(key=lambda row: row["name"])
        return rows[offset : offset + limit]

    async def download_object(self, bucket, path):
        self.downloads.append(path)
        if self.download_error is not None and path == self.download_error:
            raise ServiceError("The file storage service rejected the download.")
        return self.objects[path]

    async def upload_object(self, bucket, path, data, content_type):
        self.uploads.append(path)
        self.objects[path] = data


@pytest.fixture
def bucket(monkeypatch):
    def _install(objects, *, folders=()):
        fake = FakeBucket(objects, folders=folders)
        for name in ("list_objects", "download_object", "upload_object"):
            monkeypatch.setattr(headshot_sweep.storage, name, getattr(fake, name))
        return fake

    return _install


# ---------------------------------------------------------- the happy path --


def test_an_oversized_headshot_is_normalised_under_the_same_key(bucket):
    original = _photo()
    fake = bucket({"jdoe12": original})

    summary = _run(headshot_sweep.run_sweep())

    assert summary.eligible == 1 and summary.normalised == 1
    # The KEY is the contract: the database stores no URL, only the net ID, so a
    # rewrite that moved the object would break every headshot on the site.
    assert set(fake.objects) == {"jdoe12"}
    stored = fake.objects["jdoe12"]
    assert len(stored) < len(original)
    assert summary.bytes_reclaimed == len(original) - len(stored)
    # And it is still a usable picture, scaled to the cropper's bound.
    image = Image.open(io.BytesIO(stored))
    assert image.format == "JPEG"
    assert max(image.size) == 1024


def test_running_it_again_does_nothing(bucket):
    """Idempotence, and the whole basis of "already normalised" being a size."""
    fake = bucket({"jdoe12": _photo()})
    _run(headshot_sweep.run_sweep())
    assert len(fake.downloads) == 1

    second = _run(headshot_sweep.run_sweep())

    assert len(fake.objects["jdoe12"]) <= THRESHOLD, (
        "a normalised photo must land under the skip threshold, or the sweep "
        "would re-encode it every night and drift its quality"
    )
    assert second.eligible == 0
    assert second.processed == 0
    assert second.skipped_small == 1
    assert len(fake.downloads) == 1, "an already-swept object was downloaded again"


# ------------------------------------------------ what it must not touch ----


def test_an_already_small_headshot_is_never_even_downloaded(bucket):
    small = _small_photo()
    fake = bucket({"jdoe12": small})

    summary = _run(headshot_sweep.run_sweep())

    assert fake.downloads == [], "paid for a download to discover there was nothing to do"
    assert fake.uploads == []
    assert fake.objects["jdoe12"] == small
    assert summary.skipped_small == 1
    assert summary.scanned == 1 and summary.eligible == 0


def test_an_unreadable_object_is_skipped_not_destroyed(bucket):
    """The disposition that differs from the request path: skip, never reject.

    An upload that will not decode is refused before it is stored. An object
    that will not decode is ALREADY somebody's headshot, and failing to parse it
    is not grounds to delete or overwrite it.
    """
    junk = b"\xff\xd8\xff" + b"not really a jpeg" * 40_000
    assert len(junk) > THRESHOLD
    fake = bucket({"jdoe12": junk})

    summary = _run(headshot_sweep.run_sweep())

    assert summary.skipped_unreadable == 1
    assert summary.normalised == 0
    assert fake.uploads == []
    assert fake.objects["jdoe12"] == junk, "an unreadable object was modified"


def test_a_reencode_that_does_not_shrink_is_not_written(monkeypatch, bucket):
    original = _photo()
    fake = bucket({"jdoe12": original})
    # A re-encode that comes back the same size or larger buys nothing and costs
    # a generation of quality, so it must never reach storage.
    monkeypatch.setattr(
        headshot_sweep, "normalise_headshot", lambda data: data + b"\x00"
    )

    summary = _run(headshot_sweep.run_sweep())

    assert summary.skipped_no_gain == 1
    assert summary.normalised == 0
    assert fake.uploads == []
    assert fake.objects["jdoe12"] == original


def test_staged_survey_photos_are_left_alone(bucket):
    fake = bucket(
        {"jdoe12": _photo(), "survey-pending/417": _photo()},
        folders=("survey-pending",),
    )

    summary = _run(headshot_sweep.run_sweep())

    assert fake.downloads == ["jdoe12"]
    assert fake.uploads == ["jdoe12"]
    # The folder placeholder is not a file and the staged photo is off limits, so
    # neither is even counted as scanned.
    assert summary.scanned == 1


def test_a_download_failure_leaves_the_object_untouched(bucket):
    original = _photo()
    fake = bucket({"jdoe12": original})
    fake.download_error = "jdoe12"

    summary = _run(headshot_sweep.run_sweep())

    assert summary.failed == 1
    assert summary.normalised == 0
    assert fake.uploads == []
    assert fake.objects["jdoe12"] == original


# ------------------------------------------------------- per-run bounding ---


def test_the_per_run_cap_is_respected_and_the_rest_waits(monkeypatch, bucket):
    """A run must stop at the cap so a big backlog drains over several nights
    instead of timing out mid-flight."""
    # Real image work is irrelevant here and slow at this count; what matters is
    # how many objects the loop is willing to touch.
    monkeypatch.setattr(
        headshot_sweep, "normalise_headshot", lambda data: data[: len(data) // 2]
    )
    blobs = {f"user{i:02d}": b"x" * (THRESHOLD + 1000) for i in range(10)}
    fake = bucket(blobs)

    summary = _run(headshot_sweep.run_sweep(max_objects=3))

    assert summary.eligible == 10
    assert summary.processed == 3
    assert summary.normalised == 3
    assert summary.remaining == 7
    assert len(fake.downloads) == 3


def test_the_time_budget_stops_the_run(monkeypatch, bucket):
    monkeypatch.setattr(headshot_sweep, "normalise_headshot", lambda data: b"tiny")
    fake = bucket({f"user{i}": b"x" * (THRESHOLD + 1000) for i in range(4)})

    summary = _run(headshot_sweep.run_sweep(time_budget_seconds=0))

    assert summary.stopped_on_time_budget is True
    assert summary.processed == 0
    assert summary.remaining == 4
    assert fake.downloads == []


def test_min_bytes_zero_forces_a_full_pass(monkeypatch, bucket):
    """The manual escape hatch for inspecting objects the size rule skips."""
    monkeypatch.setattr(headshot_sweep, "normalise_headshot", lambda data: b"tiny")
    fake = bucket({"jdoe12": _small_photo()})

    summary = _run(headshot_sweep.run_sweep(min_bytes=0))

    assert summary.eligible == 1
    assert fake.downloads == ["jdoe12"]


def test_an_object_with_no_size_in_the_listing_is_inspected_rather_than_skipped(
    monkeypatch, bucket
):
    """Conservative fallback: we would rather pay a download than leave a 9 MB
    photo in place because the listing was missing a field."""
    monkeypatch.setattr(headshot_sweep, "normalise_headshot", lambda data: b"tiny")
    fake = bucket({"jdoe12": _small_photo()})
    original_list = fake.list_objects

    async def list_without_size(bucket_name, **kwargs):
        rows = await original_list(bucket_name, **kwargs)
        for row in rows:
            if isinstance(row["metadata"], dict):
                row["metadata"].pop("size", None)
        return rows

    monkeypatch.setattr(headshot_sweep.storage, "list_objects", list_without_size)

    summary = _run(headshot_sweep.run_sweep())

    assert summary.eligible == 1
    assert fake.downloads == ["jdoe12"]


def test_listing_pages_through_a_bucket_larger_than_one_page(monkeypatch, bucket):
    monkeypatch.setattr(headshot_sweep, "normalise_headshot", lambda data: b"tiny")
    fake = bucket({f"user{i:03d}": b"x" * (THRESHOLD + 1) for i in range(250)})

    summary = _run(headshot_sweep.run_sweep(max_objects=0))

    assert summary.scanned == 250
    assert summary.eligible == 250
    assert fake.downloads == []


# ------------------------------------------------------------- cron route ---


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _set_cron_secret(monkeypatch, value):
    from types import SimpleNamespace

    import app.api.routes.storage as storage_routes

    monkeypatch.setattr(
        storage_routes, "get_settings", lambda: SimpleNamespace(cron_secret=value)
    )


def _stub_sweep(monkeypatch, sink):
    from app.schemas.storage import HeadshotSweepSummary

    async def fake_run(**kwargs):
        sink.append(True)
        return HeadshotSweepSummary(scanned=3, normalised=1)

    monkeypatch.setattr(headshot_sweep, "run_sweep", fake_run)


def test_cron_rejects_a_request_with_no_credentials(client, monkeypatch):
    ran = []
    _set_cron_secret(monkeypatch, "topsecret")
    _stub_sweep(monkeypatch, ran)

    assert client.get("/storage/cron/headshot-sweep").status_code == 401
    assert client.post("/storage/cron/headshot-sweep").status_code == 401
    assert ran == [], "the sweep ran for an unauthenticated caller"


def test_cron_rejects_the_wrong_secret(client, monkeypatch):
    ran = []
    _set_cron_secret(monkeypatch, "topsecret")
    _stub_sweep(monkeypatch, ran)

    resp = client.get(
        "/storage/cron/headshot-sweep", headers={"Authorization": "Bearer nope"}
    )

    assert resp.status_code == 401
    assert ran == []


def test_cron_is_closed_when_no_secret_is_configured(client, monkeypatch):
    """Default-closed. An endpoint that rewrites stored photos must never be
    open just because an env var was forgotten."""
    ran = []
    _set_cron_secret(monkeypatch, None)
    _stub_sweep(monkeypatch, ran)

    resp = client.get(
        "/storage/cron/headshot-sweep", headers={"Authorization": "Bearer anything"}
    )

    assert resp.status_code == 401
    assert ran == []


@pytest.mark.parametrize("method", ["get", "post"])
def test_cron_runs_with_the_right_secret(client, monkeypatch, method):
    ran = []
    _set_cron_secret(monkeypatch, "topsecret")
    _stub_sweep(monkeypatch, ran)

    resp = getattr(client, method)(
        "/storage/cron/headshot-sweep",
        headers={"Authorization": "Bearer topsecret"},
    )

    assert resp.status_code == 200
    assert resp.json()["normalised"] == 1
    assert ran == [True]


def test_the_sweep_is_not_in_the_public_api_schema(client):
    """Kept out of OpenAPI so the generated frontend types never move for a job
    no browser client calls."""
    paths = app.openapi()["paths"]
    assert "/storage/cron/headshot-sweep" not in paths
