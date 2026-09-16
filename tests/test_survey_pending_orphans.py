"""Tests for the hand-run staged-photo orphan tool (`scripts/survey_pending_orphans`).

Everything here is pure or fed by fakes: the bucket is an in-memory listing that
answers page requests the way Supabase Storage does (one virtual folder level per
call, relative names, folder placeholders with ``metadata: None``), and the
database read is replaced outright. No network, no DB, no bucket.

The script's job is to DELETE things on prod, so most of what is asserted is what
it must refuse to do: delete on a partial listing, delete anything a row still
points at, or run against a project other than the one named.
"""

from __future__ import annotations

import asyncio
import io

import pytest

from app.core.errors import ServiceError
from scripts import survey_pending_orphans as tool


def _run(coro):
    return asyncio.run(coro)


# -------------------------------------------------------------- fakes -----


class FakeListing:
    """Answers `list_objects(bucket, prefix=, limit=, offset=)` from a flat
    dict of full paths -> sizes, one folder level per call like Supabase does.

    ``fail_at`` makes the Nth call raise ``ServiceError`` -- a partial listing.
    """

    def __init__(self, objects: dict[str, int | None], *, fail_at: int | None = None):
        self.objects = objects
        self.fail_at = fail_at
        self.calls: list[tuple[str, int, int]] = []

    async def __call__(self, bucket, *, prefix="", limit=100, offset=0):
        self.calls.append((prefix, limit, offset))
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise ServiceError("The file storage service rejected the listing.")
        files: list[dict] = []
        folders: set[str] = set()
        for path, size in self.objects.items():
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix) :]
            if "/" in rest:
                folders.add(rest.split("/", 1)[0])
            else:
                metadata = {"size": size} if size is not None else {}
                files.append({"name": rest, "metadata": metadata})
        rows = [{"name": f, "metadata": None} for f in sorted(folders)]
        rows += sorted(files, key=lambda r: r["name"])
        return rows[offset : offset + limit]


class FakeDeleter:
    def __init__(self, *, fail: set[str] | None = None):
        self.deleted: list[str] = []
        self.fail = fail or set()

    async def __call__(self, bucket, path):
        if path in self.fail:
            raise ServiceError("The file storage service rejected the delete.")
        self.deleted.append(path)


# ------------------------------------------------------ project-ref guard -----


def test_project_ref_passes_when_both_urls_carry_it():
    storage_problem, db_problem = tool.project_ref_problems(
        "abcdefghijklmnopqrst",
        supabase_url="https://abcdefghijklmnopqrst.supabase.co",
        database_url="postgresql://postgres.abcdefghijklmnopqrst:pw@pooler.example:6543/postgres",
    )
    assert storage_problem is None
    assert db_problem is None


def test_project_ref_flags_the_wrong_storage_project():
    storage_problem, db_problem = tool.project_ref_problems(
        "prodrefprodrefprodre",
        supabase_url="https://devrefdevrefdevrefde.supabase.co",
        database_url="postgresql://postgres.prodrefprodrefprodre:pw@pooler.example:6543/postgres",
    )
    assert storage_problem is not None
    assert "SUPABASE_URL" in storage_problem
    assert db_problem is None


def test_project_ref_flags_a_database_pointing_elsewhere():
    storage_problem, db_problem = tool.project_ref_problems(
        "prodrefprodrefprodre",
        supabase_url="https://prodrefprodrefprodre.supabase.co",
        database_url="postgresql://postgres.devrefdevrefdevrefde:pw@pooler.example:6543/postgres",
    )
    assert storage_problem is None
    assert db_problem is not None
    assert "DATABASE_URL" in db_problem


def test_project_ref_problem_never_echoes_the_url():
    """DATABASE_URL carries a password; the message must name the ref only."""
    secret = "sup3r-s3cret-pw"
    _, db_problem = tool.project_ref_problems(
        "prodrefprodrefprodre",
        supabase_url="https://prodrefprodrefprodre.supabase.co",
        database_url=f"postgresql://postgres.other:{secret}@pooler.example:6543/postgres",
    )
    assert db_problem is not None
    assert secret not in db_problem
    assert "pooler.example" not in db_problem


@pytest.mark.parametrize("ref", ["", "   "])
def test_project_ref_rejects_an_empty_ref(ref):
    storage_problem, db_problem = tool.project_ref_problems(
        ref, supabase_url="https://x.supabase.co", database_url="postgresql://x"
    )
    assert storage_problem and db_problem


def test_project_ref_unset_urls_are_problems():
    storage_problem, db_problem = tool.project_ref_problems(
        "someref", supabase_url=None, database_url=None
    )
    assert storage_problem and db_problem


# --------------------------------------------------------------- paging -----


def test_walk_pages_through_a_large_folder_and_reconstructs_full_paths():
    objects = {f"survey-pending/{i}": 1000 + i for i in range(250)}
    fake = FakeListing(objects)
    listing = _run(tool.walk_listing(fake, page_size=100))
    assert listing.complete
    assert listing.objects == objects
    assert listing.total_bytes == sum(objects.values())
    # 3 pages: 100, 100, 50 -- the short page ends the folder.
    assert [c[2] for c in fake.calls] == [0, 100, 200]
    assert all(c[0] == "survey-pending/" for c in fake.calls)
    assert listing.pages == 3


def test_walk_recurses_into_virtual_sub_folders():
    objects = {
        "survey-pending/1": 10,
        "survey-pending/2": 20,
        "survey-pending/old/7": 70,
        "survey-pending/old/deeper/8": 80,
    }
    fake = FakeListing(objects)
    listing = _run(tool.walk_listing(fake))
    assert listing.complete
    assert listing.objects == objects
    assert listing.folders == 3
    prefixes = {c[0] for c in fake.calls}
    assert prefixes == {"survey-pending/", "survey-pending/old/", "survey-pending/old/deeper/"}


def test_walk_never_lists_outside_the_staged_prefix():
    """A real headshot lives at the bucket root (`<net_id>`) -- it must never
    even be listed by this tool, let alone reported as an orphan."""
    objects = {"jdoe1": 5000, "survey-pending/1": 10}
    fake = FakeListing(objects)
    listing = _run(tool.walk_listing(fake))
    assert listing.objects == {"survey-pending/1": 10}
    assert all(c[0].startswith("survey-pending/") for c in fake.calls)


def test_walk_records_an_unknown_size_as_none():
    listing = _run(tool.walk_listing(FakeListing({"survey-pending/1": None})))
    assert listing.objects == {"survey-pending/1": None}
    assert listing.total_bytes == 0


def test_walk_exact_page_boundary_needs_one_more_empty_page():
    objects = {f"survey-pending/{i}": 1 for i in range(200)}
    fake = FakeListing(objects)
    listing = _run(tool.walk_listing(fake, page_size=100))
    assert listing.complete
    assert len(listing.objects) == 200
    assert [c[2] for c in fake.calls] == [0, 100, 200]


def test_walk_marks_a_mid_way_failure_incomplete_but_keeps_what_it_saw():
    objects = {f"survey-pending/{i}": 1 for i in range(250)}
    fake = FakeListing(objects, fail_at=2)  # second page blows up
    listing = _run(tool.walk_listing(fake, page_size=100))
    assert listing.complete is False
    assert len(listing.objects) == 100  # only the first page landed
    assert listing.failures and "offset 100" in listing.failures[0]


def test_walk_marks_a_runaway_folder_incomplete():
    objects = {f"survey-pending/{i}": 1 for i in range(50)}
    listing = _run(tool.walk_listing(FakeListing(objects), page_size=10, max_pages=3))
    assert listing.complete is False
    assert len(listing.objects) == 30
    assert "more than 3 pages" in listing.failures[0]


# --------------------------------------------------------------- diffing -----


def test_diff_splits_orphans_present_and_dangling():
    objects = {
        "survey-pending/1": 100,
        "survey-pending/2": 200,
        "survey-pending/3": None,
    }
    diff = tool.diff_references(objects, ["survey-pending/2", " survey-pending/9 ", None, ""])
    assert diff.orphans == [("survey-pending/1", 100), ("survey-pending/3", None)]
    assert diff.referenced == [("survey-pending/2", 200)]
    assert diff.dangling == ["survey-pending/9"]
    assert diff.outside_prefix == []
    assert diff.orphan_bytes == 100
    assert diff.referenced_bytes == 200


def test_diff_matching_is_exact():
    """Keys are case-sensitive in storage, so a near-miss is NOT a reference."""
    diff = tool.diff_references({"survey-pending/12": 1}, ["survey-pending/012"])
    assert diff.orphans == [("survey-pending/12", 1)]
    assert diff.dangling == ["survey-pending/012"]


def test_diff_reports_out_of_prefix_references_separately():
    diff = tool.diff_references({"survey-pending/1": 1}, ["headshots/jdoe1", "survey-pending/1"])
    assert diff.orphans == []
    assert diff.dangling == []
    assert diff.outside_prefix == ["headshots/jdoe1"]


def test_diff_with_nothing_in_storage_makes_every_reference_dangling():
    diff = tool.diff_references({}, ["survey-pending/1", "survey-pending/2"])
    assert diff.orphans == []
    assert diff.dangling == ["survey-pending/1", "survey-pending/2"]


# -------------------------------------------------------------- deleting -----


def test_delete_runs_in_batches_prints_each_path_and_survives_a_failure():
    orphans = [(f"survey-pending/{i}", 10) for i in range(5)]
    deleter = FakeDeleter(fail={"survey-pending/3"})
    out = io.StringIO()
    deleted, failed = _run(
        tool.delete_orphans(orphans, deleter, batch_size=2, out=out)
    )
    assert deleted == 4
    assert failed == ["survey-pending/3"]
    assert deleter.deleted == [
        "survey-pending/0",
        "survey-pending/1",
        "survey-pending/2",
        "survey-pending/4",
    ]
    text = out.getvalue()
    assert "batch 1/3" in text and "batch 3/3" in text
    for path in ("survey-pending/0", "survey-pending/4"):
        assert f"removed {path}" in text
    assert "FAILED  survey-pending/3" in text


def test_batched_never_yields_an_empty_batch_and_tolerates_zero_size():
    assert list(tool.batched([1, 2, 3], 2)) == [[1, 2], [3]]
    assert list(tool.batched([], 2)) == []
    assert list(tool.batched([1, 2], 0)) == [[1], [2]]


# ------------------------------------------------------- the wired run -----


class _Settings:
    def __init__(self, **overrides):
        self.supabase_url = "https://prodrefprodrefprodre.supabase.co"
        self.supabase_service_role_key = "service-role-key-do-not-print"
        self.database_url = (
            "postgresql://postgres.prodrefprodrefprodre:db-password-do-not-print"
            "@pooler.example:6543/postgres"
        )
        for key, value in overrides.items():
            setattr(self, key, value)


def _wire(monkeypatch, *, objects, referenced, settings=None, fail_at=None, deleter=None):
    """Patch the script's three seams: settings, storage client, DB read."""
    listing = FakeListing(objects, fail_at=fail_at)
    deleter = deleter or FakeDeleter()
    monkeypatch.setattr(tool, "get_settings", lambda: settings or _Settings())
    monkeypatch.setattr(tool.storage, "list_objects", listing)
    monkeypatch.setattr(tool.storage, "delete_object", deleter)

    async def _load():
        return list(referenced)

    monkeypatch.setattr(tool, "load_referenced_paths", _load)
    return listing, deleter


def _main(monkeypatch, argv):
    out = io.StringIO()
    args = tool._parse_args(argv)
    code = _run(tool._run(args, out=out))
    return code, out.getvalue()


def test_default_is_a_dry_run_that_deletes_nothing(monkeypatch):
    objects = {"survey-pending/1": 100, "survey-pending/2": 200}
    _, deleter = _wire(monkeypatch, objects=objects, referenced=["survey-pending/2"])
    code, text = _main(monkeypatch, [])
    assert code == 0
    assert deleter.deleted == []
    assert "DRY RUN" in text
    assert "ORPHANS (in storage, no row references them): 1 (100 B)" in text
    assert "  survey-pending/1  (100 B)" in text
    assert "DANGLING (a row references them, object missing): 0" in text
    assert "nothing deleted" in text


def test_dry_run_reports_dangling_references(monkeypatch):
    _wire(monkeypatch, objects={}, referenced=["survey-pending/5"])
    code, text = _main(monkeypatch, [])
    assert code == 0
    assert "DANGLING (a row references them, object missing): 1" in text
    assert "  survey-pending/5" in text


def test_delete_removes_only_the_orphans(monkeypatch):
    objects = {"survey-pending/1": 100, "survey-pending/2": 200, "survey-pending/3": 300}
    _, deleter = _wire(monkeypatch, objects=objects, referenced=["survey-pending/2"])
    code, text = _main(monkeypatch, ["--delete"])
    assert code == 0
    assert deleter.deleted == ["survey-pending/1", "survey-pending/3"]
    assert "removed survey-pending/1" in text
    assert "removed survey-pending/3" in text
    assert "Deleted 2; failed 0." in text


def test_delete_refuses_on_a_partial_listing(monkeypatch):
    objects = {f"survey-pending/{i}": 1 for i in range(250)}
    _, deleter = _wire(monkeypatch, objects=objects, referenced=[], fail_at=2)
    code, text = _main(monkeypatch, ["--delete"])
    assert code == 1
    assert deleter.deleted == []
    assert "LISTING INCOMPLETE" in text
    assert "REFUSING TO DELETE" in text


def test_dry_run_on_a_partial_listing_still_reports_but_exits_nonzero(monkeypatch):
    objects = {f"survey-pending/{i}": 1 for i in range(250)}
    _wire(monkeypatch, objects=objects, referenced=[], fail_at=2)
    code, text = _main(monkeypatch, [])
    assert code == 1
    assert "LISTING INCOMPLETE" in text
    assert "PARTIAL picture" in text


def test_expect_project_ref_aborts_before_touching_storage_or_db(monkeypatch):
    listing, deleter = _wire(monkeypatch, objects={"survey-pending/1": 1}, referenced=[])
    called = []

    async def _load():
        called.append(True)
        return []

    monkeypatch.setattr(tool, "load_referenced_paths", _load)
    code, text = _main(monkeypatch, ["--expect-project-ref", "devrefdevrefdevrefde", "--delete"])
    assert code == 2
    assert "ABORT" in text
    assert listing.calls == []
    assert called == []
    assert deleter.deleted == []


def test_expect_project_ref_passes_on_the_right_project(monkeypatch):
    _wire(monkeypatch, objects={}, referenced=[])
    code, text = _main(monkeypatch, ["--expect-project-ref", "prodrefprodrefprodre"])
    assert code == 0
    assert "Project ref check passed: prodrefprodrefprodre" in text


def test_expect_project_ref_with_database_elsewhere_warns_on_dry_run_aborts_on_delete(
    monkeypatch,
):
    settings = _Settings(
        database_url="postgresql://postgres.devrefdevrefdevrefde:pw@pooler.example:6543/postgres"
    )
    _wire(monkeypatch, objects={"survey-pending/1": 1}, referenced=[], settings=settings)
    code, text = _main(monkeypatch, ["--expect-project-ref", "prodrefprodrefprodre"])
    assert code == 0
    assert "WARNING: DATABASE_URL does not contain" in text

    _, deleter = _wire(
        monkeypatch, objects={"survey-pending/1": 1}, referenced=[], settings=settings
    )
    code, text = _main(monkeypatch, ["--expect-project-ref", "prodrefprodrefprodre", "--delete"])
    assert code == 2
    assert "Refusing to delete" in text
    assert deleter.deleted == []


def test_missing_storage_config_is_a_clean_exit(monkeypatch):
    settings = _Settings(supabase_service_role_key=None)
    _wire(monkeypatch, objects={}, referenced=[], settings=settings)
    code, text = _main(monkeypatch, [])
    assert code == 2
    assert "SUPABASE_SERVICE_ROLE_KEY" in text


def test_missing_database_config_is_a_clean_exit(monkeypatch):
    _wire(monkeypatch, objects={}, referenced=[], settings=_Settings(database_url=None))
    code, text = _main(monkeypatch, [])
    assert code == 2
    assert "DATABASE_URL must be set" in text


def test_a_database_failure_reports_the_type_only(monkeypatch):
    _wire(monkeypatch, objects={}, referenced=[])

    async def _boom():
        raise ConnectionError("postgresql://postgres:db-password-do-not-print@host/db refused")

    monkeypatch.setattr(tool, "load_referenced_paths", _boom)
    code, text = _main(monkeypatch, [])
    assert code == 2
    assert "ConnectionError" in text
    assert "db-password-do-not-print" not in text


def test_output_never_contains_a_secret(monkeypatch):
    objects = {"survey-pending/1": 100}
    _wire(monkeypatch, objects=objects, referenced=[])
    for argv in ([], ["--delete"], ["--expect-project-ref", "prodrefprodrefprodre", "--delete"]):
        _wire(monkeypatch, objects=objects, referenced=[])
        _, text = _main(monkeypatch, argv)
        assert "do-not-print" not in text
        assert "pooler.example" not in text
