"""Shared path-parameter types for API routes.

Numeric ``{..._id}`` path params are bounded so an id outside the PostgreSQL
``bigint`` range is rejected with a 422 *before* it reaches asyncpg. A bare
``int`` path param has no upper bound, so a value beyond int64 fails at the
asyncpg bind stage and surfaces as a 500 instead of a clean validation error
(#185). ``IdPath`` applies the same ``ge=1`` .. ``INT64_MAX`` bound everywhere;
the parameter name is still taken from the function argument, so one alias
covers every id (``alumni_id``, ``event_id``, ``donation_id``, ...).
"""

from typing import Annotated

from fastapi import Path

# PostgreSQL ``bigint`` / ``bigserial`` ceiling — the largest id the DB can hold.
INT64_MAX = 9223372036854775807

# A positive-identity path id, bounded to the bigint range. Ids are 1-based
# identities, so ``ge=1`` also rejects 0 / negative segments up front.
IdPath = Annotated[int, Path(ge=1, le=INT64_MAX)]
