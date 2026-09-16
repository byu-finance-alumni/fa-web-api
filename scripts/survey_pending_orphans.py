"""Find (and optionally delete) orphaned staged survey photos.

WHY THIS EXISTS
---------------
A survey submission can stage a new headshot at ``survey-pending/<survey_response_id>``
in the private ``headshots`` bucket (``survey_responses.staged_photo_path``). The
blob is removed when a reviewer approves or rejects the response -- and by NOTHING
else. Deleting a ``survey_responses`` row directly (a hand-run campaign reset,
issue #445) leaves its blob behind with no row pointing at it, and the nightly
``headshot_sweep`` deliberately never touches ``survey-pending/``. Those blobs are
orphans: unreachable from the app, invisible to every report, billed forever.

This is the hand-run tool that finds them. It is a MAINTENANCE SCRIPT, not app
code: it imports the app's settings, ORM model and Storage client so there is one
definition of the bucket, the prefix and how the REST API is spoken to, but nothing
in the running API imports it.

WHAT IT DOES
------------
1. Lists every object under ``survey-pending/`` (paginated; a Supabase listing is
   one virtual folder level at a time, so sub-folders are walked recursively).
2. Loads every non-null ``survey_responses.staged_photo_path``.
3. Reports ORPHANS (objects no row references) and DANGLING references (rows whose
   object is missing), with counts and total bytes.

THE DEFAULT IS A DRY RUN. Nothing is deleted unless ``--delete`` is given, and even
then the script refuses if the bucket listing did not complete -- a partial listing
cannot tell an orphan from an object that simply was not paged yet, and a delete
decided on that picture would destroy pending submissions.

Run from the repo root with the project venv, env vars set (never printed):

    DATABASE_URL, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY

These are read through the app's own ``Settings`` (``app/core/config.py``), so a
``.env`` in the working directory is read too, exactly as the API would read it.
That is why ``--expect-project-ref`` exists: say which project you mean.

    .venv/Scripts/python -m scripts.survey_pending_orphans
    .venv/Scripts/python -m scripts.survey_pending_orphans --expect-project-ref <ref>
    .venv/Scripts/python -m scripts.survey_pending_orphans --expect-project-ref <ref> --delete

``--expect-project-ref`` aborts unless SUPABASE_URL carries that Supabase project
ref (and, under ``--delete``, unless DATABASE_URL does too) -- the guard against
pointing the storage half at one project and the database half at another, which
would report every staged photo as an orphan.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field

from sqlalchemy import select

from app.core.config import get_settings
from app.core.errors import ServiceError
from app.services import supabase_storage as storage
from app.services.headshot_index import BUCKET, STAGED_PREFIX

#: One Storage listing page. Same size the app's own listings use.
PAGE_SIZE = 100
#: Hard stop on paging, per folder. A run that hits it is INCOMPLETE and may not
#: delete. 200 pages is 20,000 objects -- two orders of magnitude above any
#: plausible number of pending submissions.
MAX_PAGES = 200
#: How many deletes are issued between progress lines.
DEFAULT_BATCH_SIZE = 50

# Signature of one listing page: (bucket, prefix, limit, offset) -> rows.
ListPage = Callable[..., Awaitable[list[dict]]]
# Signature of one delete: (bucket, path) -> None.
DeleteObject = Callable[[str, str], Awaitable[None]]


@dataclass
class Listing:
    """Everything found under the prefix, plus whether the walk finished.

    ``objects`` maps the FULL object path (``survey-pending/123``) to its size
    in bytes (``None`` when the listing carried no size). ``complete`` is False
    when any page could not be fetched or a folder ran past :data:`MAX_PAGES`;
    ``failures`` says why. A caller may REPORT from an incomplete listing but
    must never DELETE from one.
    """

    objects: dict[str, int | None] = field(default_factory=dict)
    pages: int = 0
    folders: int = 0
    complete: bool = True
    failures: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(size for size in self.objects.values() if isinstance(size, int))


@dataclass
class Diff:
    """The reconciliation of storage against the database."""

    orphans: list[tuple[str, int | None]]  # in storage, no row
    referenced: list[tuple[str, int | None]]  # in storage, a row points at it
    dangling: list[str]  # a row points at it, not in storage
    outside_prefix: list[str]  # a row points outside the prefix; not checked

    @property
    def orphan_bytes(self) -> int:
        return sum(size for _, size in self.orphans if isinstance(size, int))

    @property
    def referenced_bytes(self) -> int:
        return sum(size for _, size in self.referenced if isinstance(size, int))


# ------------------------------------------------------------ pure parts -----


def project_ref_problems(
    expected_ref: str,
    *,
    supabase_url: str | None,
    database_url: str | None,
) -> tuple[str | None, str | None]:
    """``(storage_problem, database_problem)`` -- each ``None`` when that URL
    carries the expected project ref.

    Only the ref itself is ever echoed, never a URL: SUPABASE_URL is harmless
    but DATABASE_URL carries a password, and the same code path handles both.
    """
    ref = (expected_ref or "").strip()
    if not ref:
        problem = "--expect-project-ref was given an empty value."
        return problem, problem
    storage_problem = None
    if not supabase_url:
        storage_problem = "SUPABASE_URL is not set, so the project ref cannot be checked."
    elif ref not in supabase_url:
        storage_problem = f"SUPABASE_URL does not contain the expected project ref {ref!r}."
    database_problem = None
    if not database_url:
        database_problem = "DATABASE_URL is not set, so the project ref cannot be checked."
    elif ref not in database_url:
        database_problem = f"DATABASE_URL does not contain the expected project ref {ref!r}."
    return storage_problem, database_problem


async def walk_listing(
    list_page: ListPage,
    *,
    bucket: str = BUCKET,
    prefix: str = STAGED_PREFIX,
    page_size: int = PAGE_SIZE,
    max_pages: int = MAX_PAGES,
) -> Listing:
    """Every object under ``prefix``, walking virtual sub-folders.

    Supabase Storage lists ONE folder level per call: each row's ``name`` is the
    last path segment relative to the prefix, and a row whose ``metadata`` is
    ``None`` is a synthesised folder placeholder, not a file. So the full path
    is ``prefix + name``, and a folder row is a prefix to walk in turn.

    Never raises for a storage failure. A page that cannot be fetched, or a
    folder that runs past ``max_pages``, marks the listing INCOMPLETE and
    records why; whatever was gathered is still returned so a dry run can show
    it. The delete path checks ``complete`` and refuses otherwise.
    """
    listing = Listing()
    pending = [prefix if prefix.endswith("/") else f"{prefix}/"]
    while pending:
        folder = pending.pop(0)
        listing.folders += 1
        offset = 0
        finished = False
        for _ in range(max_pages):
            try:
                rows = await list_page(bucket, prefix=folder, limit=page_size, offset=offset)
            except ServiceError as exc:
                listing.complete = False
                listing.failures.append(f"{folder} (offset {offset}): {exc}")
                finished = True
                break
            listing.pages += 1
            for row in rows:
                name = (row.get("name") or "").strip()
                if not name:
                    continue
                metadata = row.get("metadata")
                if not isinstance(metadata, dict):
                    pending.append(f"{folder}{name}/")
                    continue
                size = metadata.get("size")
                listing.objects[f"{folder}{name}"] = size if isinstance(size, int) else None
            offset += len(rows)
            if len(rows) < page_size:
                finished = True
                break
        if not finished:
            listing.complete = False
            listing.failures.append(
                f"{folder}: more than {max_pages} pages of {page_size}; stopped paging."
            )
    return listing


def diff_references(
    objects: Mapping[str, int | None],
    referenced_paths: Iterable[str],
    *,
    prefix: str = STAGED_PREFIX,
) -> Diff:
    """Reconcile the objects in storage against the paths rows point at.

    Matching is EXACT on the trimmed path, because storage keys are. A row
    whose path lies outside ``prefix`` was never listed and cannot be judged;
    it is reported separately rather than counted as dangling.
    """
    referenced: set[str] = set()
    outside: set[str] = set()
    for raw in referenced_paths:
        path = (raw or "").strip()
        if not path:
            continue
        if path.startswith(prefix):
            referenced.add(path)
        else:
            outside.add(path)
    orphans = sorted((p, s) for p, s in objects.items() if p not in referenced)
    present = sorted((p, s) for p, s in objects.items() if p in referenced)
    dangling = sorted(p for p in referenced if p not in objects)
    return Diff(
        orphans=orphans,
        referenced=present,
        dangling=dangling,
        outside_prefix=sorted(outside),
    )


def batched(items: list, size: int) -> Iterable[list]:
    size = max(1, size)
    for start in range(0, len(items), size):
        yield items[start : start + size]


async def delete_orphans(
    orphans: list[tuple[str, int | None]],
    delete_object: DeleteObject,
    *,
    bucket: str = BUCKET,
    batch_size: int = DEFAULT_BATCH_SIZE,
    out=sys.stdout,
) -> tuple[int, list[str]]:
    """Delete each orphan, printing every path removed. Returns
    ``(deleted_count, failed_paths)``; one failure never stops the rest."""
    deleted = 0
    failed: list[str] = []
    batches = list(batched(orphans, batch_size))
    for index, batch in enumerate(batches, start=1):
        print(f"batch {index}/{len(batches)} ({len(batch)} objects)", file=out)
        for path, size in batch:
            try:
                await delete_object(bucket, path)
            except ServiceError as exc:
                failed.append(path)
                print(f"  FAILED  {path}  ({exc})", file=out)
                continue
            deleted += 1
            print(f"  removed {path}  ({_fmt_bytes(size)})", file=out)
    return deleted, failed


def _fmt_bytes(size: int | None) -> str:
    if not isinstance(size, int):
        return "size unknown"
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.2f} MB"


def render_report(listing: Listing, diff: Diff, *, prefix: str = STAGED_PREFIX) -> str:
    lines = [
        f"Bucket: {BUCKET}    prefix: {prefix}",
        f"Listed {len(listing.objects)} objects ({_fmt_bytes(listing.total_bytes)}) "
        f"across {listing.folders} folder(s), {listing.pages} page(s).",
    ]
    if not listing.complete:
        lines.append("LISTING INCOMPLETE -- the numbers below are a PARTIAL picture:")
        lines.extend(f"  {failure}" for failure in listing.failures)
    lines.append(
        f"Referenced by a survey_responses row and present: {len(diff.referenced)} "
        f"({_fmt_bytes(diff.referenced_bytes)})"
    )
    lines.append(
        f"ORPHANS (in storage, no row references them): {len(diff.orphans)} "
        f"({_fmt_bytes(diff.orphan_bytes)})"
    )
    lines.extend(f"  {path}  ({_fmt_bytes(size)})" for path, size in diff.orphans)
    lines.append(f"DANGLING (a row references them, object missing): {len(diff.dangling)}")
    lines.extend(f"  {path}" for path in diff.dangling)
    if diff.outside_prefix:
        lines.append(
            f"References outside {prefix} (not checked): {len(diff.outside_prefix)}"
        )
        lines.extend(f"  {path}" for path in diff.outside_prefix)
    return "\n".join(lines)


# ------------------------------------------------------- the wired parts -----


async def load_referenced_paths() -> list[str]:
    """Every non-null ``survey_responses.staged_photo_path`` (one SELECT)."""
    # Imported here, not at module top: `app.core.database` builds the engine
    # at import time from DATABASE_URL, and the pure parts above (and their
    # tests) must not need a database to exist.
    from app.core.database import SessionLocal
    from app.models.survey_response import SurveyResponse

    if SessionLocal is None:
        raise RuntimeError("DATABASE_URL is not configured.")
    async with SessionLocal() as session:
        result = await session.execute(
            select(SurveyResponse.staged_photo_path).where(
                SurveyResponse.staged_photo_path.is_not(None)
            )
        )
        return [row[0] for row in result.all()]


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.survey_pending_orphans",
        description=(
            "Report staged survey photos (survey-pending/) with no survey_responses "
            "row, and rows whose staged photo is missing. Dry run by default."
        ),
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="actually delete the orphan objects (refused if the listing is incomplete)",
    )
    parser.add_argument(
        "--expect-project-ref",
        metavar="REF",
        help="abort unless SUPABASE_URL contains this Supabase project ref",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"deletes per progress line (default {DEFAULT_BATCH_SIZE})",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace, out=sys.stdout) -> int:
    settings = get_settings()
    mode = "DELETE" if args.delete else "DRY RUN"
    print(f"survey_pending_orphans -- {mode}", file=out)

    if not settings.supabase_url or not settings.supabase_service_role_key:
        print("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must both be set.", file=out)
        return 2
    if not settings.database_url:
        print("DATABASE_URL must be set.", file=out)
        return 2

    if args.expect_project_ref:
        storage_problem, db_problem = project_ref_problems(
            args.expect_project_ref,
            supabase_url=settings.supabase_url,
            database_url=settings.database_url,
        )
        if storage_problem:
            print(f"ABORT: {storage_problem}", file=out)
            return 2
        if db_problem and args.delete:
            print(f"ABORT: {db_problem} Refusing to delete.", file=out)
            return 2
        if db_problem:
            print(f"WARNING: {db_problem}", file=out)
        print(f"Project ref check passed: {args.expect_project_ref.strip()}", file=out)
    else:
        print(
            "No --expect-project-ref given; not checking which project this is.",
            file=out,
        )

    print("Loading staged_photo_path references from survey_responses...", file=out)
    try:
        referenced = await load_referenced_paths()
    except Exception as exc:
        # The exception TYPE only: a driver error message can echo the DSN.
        print(f"Could not read survey_responses: {type(exc).__name__}", file=out)
        return 2
    print(f"  {len(referenced)} row(s) reference a staged photo.", file=out)

    print(f"Listing {BUCKET}/{STAGED_PREFIX} ...", file=out)
    listing = await walk_listing(storage.list_objects)
    diff = diff_references(listing.objects, referenced)
    print(render_report(listing, diff), file=out)

    if not args.delete:
        print("Dry run: nothing deleted. Re-run with --delete to remove the orphans.", file=out)
        return 0 if listing.complete else 1

    if not listing.complete:
        print(
            "REFUSING TO DELETE: the bucket listing did not complete, so this is a "
            "partial picture and an unlisted object cannot be told from an orphan.",
            file=out,
        )
        return 1
    if not diff.orphans:
        print("Nothing to delete.", file=out)
        return 0

    print(f"Deleting {len(diff.orphans)} orphan object(s)...", file=out)
    deleted, failed = await delete_orphans(
        diff.orphans, storage.delete_object, batch_size=args.batch_size, out=out
    )
    print(f"Deleted {deleted}; failed {len(failed)}.", file=out)
    return 1 if failed else 0


async def _run_and_dispose(args: argparse.Namespace) -> int:
    try:
        return await _run(args)
    finally:
        # Same loop the connections were opened on. A no-op when no database
        # was configured, so it is safe to import and call unconditionally.
        from app.core.database import dispose_engine

        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run_and_dispose(_parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
