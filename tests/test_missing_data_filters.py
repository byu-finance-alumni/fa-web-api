"""The two missing-data report filters added in #775: `missing_linkedin` and
`missing_photo`.

`missing_linkedin` is an ordinary column predicate and is tested as one. Almost
everything here is about `missing_photo`, because a headshot is NOT a column —
it is an object in the `headshots` bucket keyed by the alumnus's net ID — and
answering "who has none" from a SQL query has three ways to go wrong that a
green test suite would otherwise hide:

  1. **N+1 against object storage.** One existence check per row is thousands of
     HTTP calls for one report. The listing here is counted, so a regression to
     per-row probing fails instead of merely being slow.
  2. **NULL swallowing the rows that matter most.** `trim(net_id) NOT IN (...)`
     is NULL for a NULL net ID, and a NULL condition drops the row — silently
     hiding exactly the alumni who most certainly have no photo.
  3. **An unavailable dependency widening the population.** No keys must never
     collapse into "no predicate", which returns everyone.

See `app/services/headshot_index.py` for the design and its staleness bound.
"""

import asyncio

import pytest
from sqlalchemy.dialects import postgresql

from app.core.errors import ServiceError
from app.repositories.alumni import build_alumni_query, has_linkedin_url
from app.services import headshot_index


def _run(coro):
    """Drive one coroutine to completion — this suite has no async plugin."""
    return asyncio.run(coro)


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def _where(stmt) -> str:
    """Just the WHERE clause. The SELECT list names every column, so a substring
    check against the whole statement would pass with no predicate at all."""
    clause = stmt.whereclause
    return "" if clause is None else str(clause.compile(dialect=postgresql.dialect()))


def _params(stmt) -> dict:
    compiled = stmt.compile(dialect=postgresql.dialect())
    return dict(compiled.params)


class FakeBucket:
    """In-memory stand-in for the Storage REST listing.

    Records every call so a test can assert HOW MANY listings one report cost —
    the N+1 this module exists to prevent is invisible in the returned value.
    """

    def __init__(self, names, *, folders=(), staged=(), page_size=100):
        self.names = list(names)
        self.folders = list(folders)
        self.staged = list(staged)
        self.page_size = page_size
        self.calls: list[int] = []
        self.error: Exception | None = None

    async def list_objects(self, bucket, *, prefix="", limit=100, offset=0):
        self.calls.append(offset)
        if self.error is not None:
            raise self.error
        rows = [{"name": name, "metadata": None} for name in self.folders] + [
            {"name": name, "metadata": {"size": 1234, "mimetype": "image/jpeg"}}
            for name in self.names + self.staged
        ]
        rows.sort(key=lambda row: row["name"])
        page = min(limit, self.page_size)
        return rows[offset : offset + page]


@pytest.fixture
def bucket(monkeypatch):
    """Install a fake bucket and start from a cold cache every time."""

    def _install(names, **kwargs):
        fake = FakeBucket(names, **kwargs)
        monkeypatch.setattr(headshot_index.storage, "list_objects", fake.list_objects)
        headshot_index.reset_cache()
        return fake

    yield _install
    headshot_index.reset_cache()


# --- missing_linkedin ---------------------------------------------------------


def test_missing_linkedin_filter_treats_blank_as_missing():
    # An empty spreadsheet cell imports as '', and IS NOT NULL would count it as
    # a URL on file — understating the gap exactly as it did for email (#392).
    where = _where(build_alumni_query(missing_linkedin=True)).lower()
    assert "linkedin_url" in where
    assert "trim" in where and "coalesce" in where


def test_missing_linkedin_is_off_by_default():
    assert "linkedin_url" not in _where(build_alumni_query())


def test_data_quality_count_and_the_list_share_one_linkedin_predicate():
    # The count negates the SAME expression the filter negates, so the tile and
    # the list it deep-links to cannot describe different populations.
    where = _where(build_alumni_query(missing_linkedin=True))
    negated = str((~has_linkedin_url()).compile(dialect=postgresql.dialect()))
    # The filter's clause IS the shared expression, negated — not a lookalike
    # rewritten by hand on this side, which is how a count and its drill-down
    # drift apart.
    assert negated in where


# --- missing_photo: the key set ----------------------------------------------


def test_the_bucket_is_listed_once_for_the_whole_query(bucket):
    fake = bucket(["jdoe12", "asmith3"])

    _run(headshot_index.resolve_missing_photo(True))

    # ONE page for two objects — not one call per alumnus, which is the whole
    # point. The roster is ~1,800 rows.
    assert len(fake.calls) == 1


def test_staged_survey_photos_are_not_headshots(bucket):
    # `survey-pending/` holds submissions awaiting review; they are deleted on
    # approve or reject. An alumnus with only a staged photo still has none.
    fake = bucket(
        ["jdoe12"],
        folders=("survey-pending",),
        staged=("survey-pending/asmith3-1.jpg",),
    )

    keys = _run(headshot_index.stored_headshot_keys())

    assert keys == {"jdoe12"}
    assert fake.calls  # it really did read the listing


def test_folder_placeholders_are_not_photos(bucket):
    # Supabase synthesises a metadata-less row per path segment. Counting one as
    # an object would credit a photo to an alumnus whose net ID matched it.
    bucket(["jdoe12"], folders=("survey-pending",))
    assert _run(headshot_index.stored_headshot_keys()) == {"jdoe12"}


def test_paging_continues_until_a_short_page(bucket):
    names = [f"user{i:03d}" for i in range(250)]
    fake = bucket(names, page_size=100)

    keys = _run(headshot_index.stored_headshot_keys())

    assert keys == set(names)
    assert fake.calls == [0, 100, 200]


def test_a_bucket_past_the_page_cap_fails_instead_of_truncating(bucket):
    # The listing is name-sorted, so a truncated read would report everyone
    # whose net ID sorts late as having no photo — a report that quietly
    # indicts the back half of the alphabet. Refuse instead.
    names = [f"user{i:05d}" for i in range(headshot_index._MAX_LIST_PAGES * 100 + 10)]
    bucket(names, page_size=100)

    with pytest.raises(ServiceError):
        _run(headshot_index.stored_headshot_keys())


def test_the_listing_is_cached_then_dropped_on_a_write(bucket):
    fake = bucket(["jdoe12"])

    _run(headshot_index.stored_headshot_keys())
    _run(headshot_index.stored_headshot_keys())
    assert len(fake.calls) == 1, "the second read must come from the cache"

    # Every headshot write path calls this, which collapses the staleness window
    # to zero on the instance that served the write.
    headshot_index.reset_cache()
    _run(headshot_index.stored_headshot_keys())
    assert len(fake.calls) == 2


def test_unreachable_storage_raises_rather_than_answering_empty(bucket):
    # An empty key set means "nobody has a photo" and would name every alumnus.
    # Turning an outage into that answer is the widening this filter must never
    # do; the list and the export both surface it as a 502.
    fake = bucket(["jdoe12"])
    fake.error = ServiceError("storage down")

    with pytest.raises(ServiceError):
        _run(headshot_index.resolve_missing_photo(True))


# --- missing_photo: the predicate --------------------------------------------


def test_missing_photo_is_off_by_default(bucket):
    assert _run(headshot_index.resolve_missing_photo(False)) is None
    assert "net_id" not in _where(build_alumni_query())


def test_alumni_with_no_net_id_count_as_missing_a_photo(bucket):
    # ⚠️ THE DELIBERATE PRODUCT DECISION (#775), and a materially different
    # number: they cannot have a photo — there is no key to store one under — so
    # they are INCLUDED. Structurally this is why the predicate is the NEGATION
    # of "has one": `NOT IN` would evaluate to NULL for a NULL net_id and drop
    # precisely these rows.
    bucket(["jdoe12"])
    stmt = build_alumni_query(
        photo_filter=_run(headshot_index.resolve_missing_photo(True))
    )
    sql = _where(stmt).lower()

    assert "not in" not in sql
    assert "is not null" in sql
    assert " in (" in sql


def test_the_predicate_carries_the_stored_keys_as_bind_values(bucket):
    bucket(["jdoe12", "asmith3"])
    stmt = build_alumni_query(
        photo_filter=_run(headshot_index.resolve_missing_photo(True))
    )

    bound = set()
    for value in _params(stmt).values():
        if isinstance(value, str):
            bound.add(value)
        elif isinstance(value, (list, tuple)):
            bound.update(v for v in value if isinstance(v, str))
    assert {"jdoe12", "asmith3"} <= bound


def test_an_empty_bucket_narrows_to_nobody_having_a_photo(bucket):
    # Legitimately every alumnus. What matters is that it stays a PREDICATE
    # rather than degrading into "no filter", which returns everyone for a
    # completely different reason and would look identical in the row count.
    bucket([])
    photo_filter = _run(headshot_index.resolve_missing_photo(True))

    assert photo_filter is not None
    assert _where(build_alumni_query(photo_filter=photo_filter)) != _where(
        build_alumni_query()
    )


def test_matching_is_exact_because_storage_keys_are(bucket):
    # `create_signed_url` looks up the exact key, so an object stored under
    # `JDoe12` is served to nobody when the row says `jdoe12`. Reporting that
    # alumnus as HAVING a photo would name a photo the app cannot display.
    bucket(["JDoe12"])
    where = _where(build_alumni_query(
        photo_filter=_run(headshot_index.resolve_missing_photo(True))
    ))
    assert "lower" not in where.lower()
