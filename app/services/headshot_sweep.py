"""Nightly sweep that normalises headshots ALREADY SITTING IN STORAGE.

WHY THIS EXISTS
---------------
Every path that hands bytes to our function normalises them on the way in
(`services/images.normalise_headshot`). The BULK photo import does not, and
cannot: the browser PUTs each file STRAIGHT to Supabase Storage with a signed
URL, precisely because 100 files at up to 20 MB will not fit through a
serverless function that has ~4.5 MB of request body and 2 GB of memory SHARED
with every concurrent request. So bulk-imported photos land byte-for-byte as
they came off someone's phone: multi-megabyte, EXIF and GPS intact, displayed as
a 288px avatar.

This job is the deferred half of that trade. It walks the bucket, downloads what
is still oversized, runs the SAME primitive the request paths use, and writes the
result back UNDER THE SAME KEY. Nothing in the database changes and every
existing headshot URL keeps working, because the key is the alumnus's net ID and
it does not move.

⚠️ IT USES `normalise_headshot`, NOT a local re-encode. The offline
`compress-headshots.py` at the workspace root does the same job by hand and does
NOT apply EXIF orientation — running its `recompress` over the bucket would
permanently rotate every portrait phone photo, because the rotation lives in the
metadata being dropped. The shared primitive bakes the rotation into the pixels
first. Do not "inline" it.

HOW WE KNOW AN OBJECT IS ALREADY NORMALISED
-------------------------------------------
There is no flag, and the choice matters: re-encoding an already-normalised JPEG
every night is wasted download bandwidth AND a slow generational quality drift.

We use the object's SIZE, straight from the bucket listing, and never download
anything at or under `_SKIP_UNDER_BYTES`. The alternatives were considered and
rejected:

  * A database column (or a side table of swept keys) is authoritative until the
    day it isn't: a re-upload through the bulk importer replaces the bytes
    without touching the row, and the sweep then skips a file it has never seen.
    It also needs a migration and gives storage state a second home that can
    drift from storage itself.
  * A marker written into the JPEG (a COM segment, say) travels with the object
    and can be read with a small ranged GET — but only objects WE rewrite carry
    it, so anything the sweep declines to rewrite (no size gain) is re-probed
    and re-downloaded forever.
  * Dimensions would be exact, but they are not in the listing; getting them
    means a ranged read per object per night, i.e. paying a round-trip to
    discover there is nothing to do.

Size wins because it is DERIVED FROM THE OBJECT and therefore cannot drift, it
costs nothing (the listing already carries it), and it CONVERGES: normalising
caps the longest edge at 1024px at quality 90, which lands far under the
threshold, so a swept object is skipped on every later run by the same rule that
selected it. That also makes the job resumable for free — there is no cursor to
keep, because "what's left to do" is recomputed from the bucket each run.

The threshold and its value are `compress-headshots.py`'s, deliberately: that
script was written against the real prod inventory (381 headshots, ~1.28 GB,
averaging 3.4 MB), and a photo that came through the in-app cropper is 100-200 KB
and must not be touched at all.

⚠️ THE COST OF THAT CHOICE: an object that is SMALL but not normalised — a
50 KB JPEG with an HTML payload appended, uploaded through the bulk importer —
is never inspected. The bulk confirm path sniffs magic bytes and deletes
non-conforming objects, which is not the same guarantee. If we ever want that
guarantee, call `run_sweep(min_bytes=0)` ONCE by hand for a full pass; the
"never write unless strictly smaller" rule below caps what a full pass can cost.

SAFETY RULES, all of them from `compress-headshots.py`'s hard-won judgement
--------------------------------------------------------------------------
  * Never write back something that is not STRICTLY SMALLER. Re-encoding can
    grow an already-optimised file, and writing it would burn quota and a
    generation of quality for nothing.
  * NEVER delete. An object that will not decode is SKIPPED and left exactly as
    it is. This differs from the request paths, which reject — there, refusing is
    the safe outcome; here the file is already someone's headshot and a parse
    failure is not evidence that it is worthless.
  * `survey-pending/` is off limits. Those are staged submissions awaiting
    review; they are deleted on approve or reject, and the promotion path
    normalises them anyway.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

from app.core.errors import InvalidRequestError, ServiceError
from app.schemas.storage import HeadshotSweepSummary
from app.services import supabase_storage as storage
from app.services.images import normalise_headshot

log = logging.getLogger(__name__)

BUCKET = "headshots"

# Staged survey photos live in the SAME bucket under this prefix. Supabase's
# listing does not descend into it by default, but we filter explicitly as well:
# a change to how the listing is called must not silently start rewriting
# submissions that are pending review.
STAGED_PREFIX = "survey-pending/"

# Anything at or under this is treated as already normalised — see the module
# docstring. Same constant as `compress-headshots.py`: a cropper upload is
# 100-200 KB and a 1024px/q90 re-encode lands well under 400 KB, so the
# threshold sits above both and below anything worth doing work for.
_SKIP_UNDER_BYTES = 400 * 1024

# --- Per-run bounds ----------------------------------------------------------
#
# A cron invocation is a normal serverless function: a wall-clock limit (60s by
# default on this project — nothing in vercel.json raises it) and memory SHARED
# with whatever real requests are being served by the same instance. So the run
# is bounded twice, and stops at whichever bound it reaches first.
#
# Shape of the work per object: download ~3.4 MB, decode + LANCZOS down to
# 1024px, re-encode, upload ~100-200 KB. The CPU half is cheap — measured
# 2026-08-08, a 12 Mpx 5.7 MB photo normalises in 0.23s locally — so the cost is
# almost entirely the two round-trips, call it ~1-1.5s each. 25 objects is
# therefore roughly what the time budget affords, and whichever bound bites
# first is the right one: TIME protects a slow night, the COUNT protects against
# a bucket of small files that would otherwise churn hundreds of objects.
_MAX_OBJECTS_PER_RUN = 25
_TIME_BUDGET_SECONDS = 45.0

# Listing is metadata only and cheap, but it still costs a round-trip per page,
# and a run that spends its budget paging has done nothing useful. This caps the
# scan at 2,000 objects — an order of magnitude above the current inventory.
_LIST_PAGE_SIZE = 100
_MAX_LIST_PAGES = 20


async def _list_candidates(min_bytes: int) -> tuple[list[tuple[str, int]], int, int]:
    """Return ``([(key, size), ...], scanned, unknown_size)`` — what to download.

    Metadata only: no image bytes are fetched here, which is the whole reason
    the "is it already normalised?" question is answered from size.
    """
    candidates: list[tuple[str, int]] = []
    scanned = 0
    unknown_size = 0
    offset = 0
    for _ in range(_MAX_LIST_PAGES):
        rows = await storage.list_objects(
            BUCKET, limit=_LIST_PAGE_SIZE, offset=offset
        )
        if not rows:
            break
        for row in rows:
            name = (row.get("name") or "").strip()
            metadata = row.get("metadata")
            # A folder placeholder has no metadata and is not a file. Downloading
            # one 404s, and `survey-pending` shows up as exactly this.
            if not name or not isinstance(metadata, dict):
                continue
            if name.startswith(STAGED_PREFIX):
                continue
            scanned += 1
            size = metadata.get("size")
            # An unknown size is treated as a candidate rather than skipped: we
            # would rather pay one download than leave a 9 MB photo in place
            # because the listing was missing a field.
            if not isinstance(size, int):
                unknown_size += 1
            if not isinstance(size, int) or size > min_bytes:
                candidates.append((name, size if isinstance(size, int) else 0))
        offset += len(rows)
        if len(rows) < _LIST_PAGE_SIZE:
            break
    return candidates, scanned, unknown_size


async def _sweep_one(key: str, summary: HeadshotSweepSummary) -> None:
    """Download, normalise and (only if it shrinks) write back ONE object.

    Every failure mode leaves the stored object exactly as it was.
    """
    try:
        original = await storage.download_object(BUCKET, key)
    except ServiceError:
        # Transport or storage failure. Nothing was written; next run retries.
        summary.failed += 1
        log.warning("headshot sweep: could not download %s", key)
        return

    try:
        # ⚠️ Decoding and re-encoding is CPU-bound and holds the GIL for a
        # second or more on a large photo. On the event loop that stalls every
        # co-tenant request this instance is serving, so it goes to a thread.
        normalised = await asyncio.to_thread(normalise_headshot, original)
    except InvalidRequestError:
        # Undecodable. SKIP — never delete. See the module docstring: on the
        # write paths rejecting is right, but this file is already somebody's
        # headshot and we have no grounds to destroy it.
        summary.skipped_unreadable += 1
        log.warning("headshot sweep: %s could not be decoded, left untouched", key)
        return
    except Exception:  # noqa: BLE001 - one bad object must never end the run
        summary.failed += 1
        log.exception("headshot sweep: unexpected failure normalising %s", key)
        return

    if len(normalised) >= len(original):
        summary.skipped_no_gain += 1
        return

    try:
        await storage.upload_object(BUCKET, key, normalised, "image/jpeg")
    except ServiceError:
        summary.failed += 1
        log.warning("headshot sweep: could not write %s back", key)
        return

    summary.normalised += 1
    summary.bytes_before += len(original)
    summary.bytes_after += len(normalised)
    summary.bytes_reclaimed += len(original) - len(normalised)


async def run_sweep(
    *,
    max_objects: int = _MAX_OBJECTS_PER_RUN,
    time_budget_seconds: float = _TIME_BUDGET_SECONDS,
    min_bytes: int = _SKIP_UNDER_BYTES,
) -> HeadshotSweepSummary:
    """Normalise up to ``max_objects`` oversized headshots and report what it did.

    Idempotent and resumable by construction: the work list is recomputed from
    the bucket every run, and a normalised object falls under ``min_bytes`` and
    is never picked again. An interrupted run needs no cleanup — just run again.

    ``min_bytes=0`` forces a full pass over every object (see the module
    docstring); it is not what the cron does.

    There is deliberately no lock. Two overlapping runs (the cron plus a manual
    trigger, say) can land on the same object, and the worst outcome is that one
    photo is re-encoded twice — a second generation, not a loss. That is not
    worth the machinery of a lock, and the random ordering below makes even that
    collision unlikely.
    """
    started = time.monotonic()
    summary = HeadshotSweepSummary()

    candidates, scanned, unknown_size = await _list_candidates(min_bytes)
    summary.scanned = scanned
    summary.eligible = len(candidates)
    summary.skipped_small = scanned - len(candidates)

    # The whole design rests on the listing carrying `metadata.size`. If it ever
    # stops doing so, every object becomes a candidate and the sweep downloads
    # the bucket every night while achieving nothing — an expensive silence.
    # Say so out loud instead.
    if unknown_size:
        log.warning(
            "headshot sweep: %d object(s) had no size in the listing and will be "
            "downloaded to find out; the skip-what-is-already-small rule is not "
            "working for them",
            unknown_size,
        )

    # ⚠️ RANDOM ORDER, NOT NAME ORDER. Objects that can never be improved
    # (undecodable, or already as small as a re-encode will make them) stay
    # eligible forever. In name order a handful of those near the start of the
    # alphabet would eat the whole per-run budget every single night and the
    # real backlog would never drain. Shuffling makes them cost a proportional
    # share instead, so progress continues regardless.
    random.shuffle(candidates)

    for key, _size in candidates:
        if summary.processed >= max_objects:
            break
        if time.monotonic() - started >= time_budget_seconds:
            summary.stopped_on_time_budget = True
            break
        summary.processed += 1
        await _sweep_one(key, summary)

    summary.remaining = summary.eligible - summary.processed
    summary.duration_seconds = round(time.monotonic() - started, 3)

    # The only visibility anyone has into this job. One line, everything in it.
    log.info(
        "headshot sweep: scanned=%d eligible=%d processed=%d normalised=%d "
        "skipped_small=%d skipped_no_gain=%d skipped_unreadable=%d failed=%d "
        "reclaimed=%d bytes remaining=%d in %.1fs%s",
        summary.scanned,
        summary.eligible,
        summary.processed,
        summary.normalised,
        summary.skipped_small,
        summary.skipped_no_gain,
        summary.skipped_unreadable,
        summary.failed,
        summary.bytes_reclaimed,
        summary.remaining,
        summary.duration_seconds,
        " (stopped on time budget)" if summary.stopped_on_time_budget else "",
    )
    return summary
