"""Which alumni HAVE a headshot — answered once per process, never per row.

WHY THIS EXISTS
---------------
A headshot is not a column. It is an object in the private ``headshots`` bucket
keyed by the alumnus's STORED net ID with no extension (see
``app/api/routes/alumni.py``: every write path — single upload, signed-upload
confirm, bulk import — uses ``(alumnus.net_id or "").strip()`` as the key, and
the read path signs that exact key). So "who has no photo" is a question about
object storage, asked from a SQL query that must stay one statement.

⚠️ THE THING NOT TO DO is a per-row existence check. The roster is ~1,800 rows
and the export is uncapped; one storage round-trip per alumnus is thousands of
HTTP calls to answer a single report, and it would put object storage on the
critical path of the list page. That is the N+1 this module exists to avoid.

WHAT IT DOES INSTEAD
--------------------
ONE bulk listing of the bucket (metadata only — no image bytes cross the wire),
turned into a set of keys, cached per process for :data:`_CACHE_TTL_SECONDS`,
and handed to SQL as a single ``IN`` list. Cost is a handful of round-trips per
five minutes rather than one per row, and the predicate is an ordinary column
comparison Postgres can plan.

ALTERNATIVES CONSIDERED
-----------------------
  * **A stored flag / column on ``alumni``, maintained on upload and by the
    nightly sweep.** Fastest to query, and the only option that would survive a
    bucket of 100k objects — but it needs a migration, and it is authoritative
    right up until it isn't: the bulk importer PUTs straight to storage with a
    signed URL, so bytes can land (or a key be deleted out of band) without any
    row changing. ``services/headshot_sweep`` rejected a swept-marker column for
    the same reason on the same evidence. A flag that silently drifts makes a
    data-quality report lie, which is worse than making it five minutes late.
  * **A materialised view.** There is nothing in Postgres to select FROM — the
    bucket is not in the database.
  * **Listing the bucket on every request, uncached.** Correct and always fresh,
    but it pays ~6 round-trips (today's inventory) on every page of the list and
    every export. The cache is the only difference between the two, and it is
    the cheap one.

STALENESS — SAY IT OUT LOUD
---------------------------
The answer can be up to :data:`_CACHE_TTL_SECONDS` (5 minutes) old, per
serverless instance, and instances hold independent caches — so right after a
bulk photo import two refreshes of the same report can disagree for a few
minutes. :func:`reset_cache` is called by the headshot write paths, which
collapses that window to zero on the instance that served the write and leaves
it for the others. For a "go collect these photos" report that is fine: nobody
acts on a five-minute-old row differently than on a fresh one. It would NOT be
fine for an authorization decision, and must never be used for one.

ALUMNI WITH NO NET ID COUNT AS MISSING A PHOTO
----------------------------------------------
They cannot have one: there is no key to store it under, and
``PUT /alumni/{id}/headshot`` refuses them with exactly that message. The
alternative — treating "no net ID" as "not applicable" and hiding them, the way
``missing_employer`` exempts Unemployed / Graduate Student — was rejected: an
alumnus with no photo AND no net ID is two data-quality problems, not zero, and
suppressing them would let the report read clean while the roster still shows
initials avatars. It changes the number materially, so it is stated in the
filter's API description too, not only here.

⚠️ MATCHING IS EXACT (case-sensitive) on the trimmed net ID, because storage
keys are. An object stored under ``JDoe12`` when the row says ``jdoe12`` is
served to nobody — ``create_signed_url`` looks up the exact key — so the honest
answer for that alumnus is "no photo", which is also what the roster shows. A
case-insensitive match here would report a photo the app cannot display.
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import and_, func

from app.core.errors import ServiceError
from app.models.alumni import Alumni
from app.services import supabase_storage as storage

log = logging.getLogger(__name__)

#: The bucket headshots live in (the same value the routes and the sweep use).
BUCKET = "headshots"

#: Staged survey submissions share the bucket under this prefix. They are NOT
#: anybody's headshot — they await review and are deleted on approve or reject —
#: so an alumnus with only a staged photo is still missing one.
STAGED_PREFIX = "survey-pending/"

#: Listing is metadata only and cheap, but each page is a round-trip. The page
#: cap is a hard stop, not a truncation: see :func:`_list_keys`.
_LIST_PAGE_SIZE = 100
_MAX_LIST_PAGES = 60  # 6,000 objects — ~3x the alumni roster, ~10x today's photos

#: How long one listing is reused within a process. Five minutes: photos arrive
#: in occasional bulk imports, not continuously, and the report is read by
#: people who then go and chase the missing ones.
_CACHE_TTL_SECONDS = 300.0

# (monotonic timestamp, keys) or None. Module-level by design — a pure
# read-through cache that is safe to lose at any moment.
_cached: tuple[float, frozenset[str]] | None = None


def reset_cache() -> None:
    """Drop the process-local listing (headshot write paths + tests).

    Best-effort by construction: it clears THIS instance only, which is why the
    TTL above is the real staleness bound, not this call.
    """
    global _cached
    _cached = None


async def _list_keys() -> frozenset[str]:
    """Every headshot object key in the bucket, straight from storage.

    ⚠️ Raises ``ServiceError`` rather than returning a partial set if the
    listing runs past :data:`_MAX_LIST_PAGES`. A truncated listing is worse than
    no answer: the listing is name-sorted, so the missing tail would be reported
    as "has no photo" — a report that quietly indicts everyone whose net ID
    sorts late.
    """
    keys: set[str] = set()
    offset = 0
    for _ in range(_MAX_LIST_PAGES):
        rows = await storage.list_objects(BUCKET, limit=_LIST_PAGE_SIZE, offset=offset)
        if not rows:
            return frozenset(keys)
        for row in rows:
            name = (row.get("name") or "").strip()
            # A row with no metadata is a FOLDER placeholder Supabase
            # synthesises per path segment ("survey-pending"), not a file.
            if not name or not isinstance(row.get("metadata"), dict):
                continue
            if name.startswith(STAGED_PREFIX):
                continue
            keys.add(name)
        offset += len(rows)
        if len(rows) < _LIST_PAGE_SIZE:
            return frozenset(keys)
    raise ServiceError(
        "The photo report could not be built: there are more stored photos than "
        "it is set up to read in one pass."
    )


async def stored_headshot_keys() -> frozenset[str]:
    """The cached key set, refreshed at most every :data:`_CACHE_TTL_SECONDS`.

    Propagates ``ServiceError`` (-> 502) when storage cannot be listed. It does
    NOT fall back to an empty set: empty means "nobody has a photo", and serving
    that as an answer would turn one unreachable dependency into a report naming
    every alumnus. Callers that would rather degrade than fail (the data-quality
    COUNT) catch it themselves and say so.
    """
    global _cached
    now = time.monotonic()
    if _cached is not None and now - _cached[0] < _CACHE_TTL_SECONDS:
        return _cached[1]
    keys = await _list_keys()
    if not keys:
        # A genuinely empty bucket is a legitimate answer (every alumnus really
        # is missing a photo), but it is also what a misconfigured bucket name
        # looks like, and the difference does not show up in the numbers.
        log.warning(
            "headshot index: the %s bucket listed zero objects; every alumnus "
            "will be reported as missing a photo",
            BUCKET,
        )
    _cached = (now, keys)
    return keys


def missing_photo_condition(keys: frozenset[str]):
    """Predicate on ``Alumni``: no headshot is stored for this alumnus.

    Written as the NEGATION of "has one" deliberately. ``trim(net_id) NOT IN
    (...)`` evaluates to NULL for a NULL net ID, and a NULL condition drops the
    row — so the obvious spelling would silently EXCLUDE exactly the alumni who
    most certainly have no photo. Negating the positive keeps them: for a NULL
    net ID the inner AND is false, and NOT false is true.
    """
    has_photo = and_(
        Alumni.net_id.is_not(None),
        func.trim(Alumni.net_id).in_(sorted(keys)),
    )
    return ~has_photo


async def resolve_missing_photo(missing_photo: bool):
    """``missing_photo=True`` -> the predicate; otherwise ``None`` (no filter).

    THE single entry point, shared by ``GET /alumni``, ``POST /alumni/export``
    and the ``/dashboard/data-quality`` count, so a report, the list it opens and
    the CSV that list exports cannot describe three different populations.
    """
    if not missing_photo:
        return None
    return missing_photo_condition(await stored_headshot_keys())
