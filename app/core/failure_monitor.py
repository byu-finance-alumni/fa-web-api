"""The observer half of API failure alerting (#444): an HTTP middleware.

``app/services/failure_alert.py`` owns what an incident IS and who gets emailed.
This module owns only the question "did that request fail, and is it worth
telling the durable store about?" — and its job is mostly to say NO, cheaply.

WHY THERE IS AN IN-PROCESS GATE IN FRONT OF A DURABLE STORE. An outage produces
thousands of errors a minute. Writing one row per error to the database would
make the monitoring feature a load source during the exact minutes the service is
already unwell, and the database may be what is broken. So each process reports
at most one failure every ``_REPORT_INTERVAL_SECONDS`` and runs at most one
recovery probe every ``_PROBE_INTERVAL_SECONDS``; everything else is a couple of
comparisons against module-level floats and no I/O at all.

THIS IN-PROCESS STATE IS NOT THE DEDUP. It is a rate limiter and nothing more.
Per-instance memory could never dedupe an alert on serverless (see the caveat in
``app/core/rate_limit.py``, and the long argument in ``failure_alert``); the
dedup is the ``service_incidents`` row. The one exception is the degraded path,
which is reached only when the durable store cannot be read at all, and which
says so in its own subject line.

A HEALTHY REQUEST COSTS NOTHING. When alerting is unconfigured — local dev, CI,
the test suite, any deployment without ``ALERT_EMAIL_TO`` — the middleware
returns before touching any state. When it is configured, a successful request
costs one float comparison, except for the one sampled probe per minute.
"""

from __future__ import annotations

import logging
import re
import time

from fastapi import Request

from app.services import failure_alert
from app.services.failure_alert import FailureSignal

log = logging.getLogger(__name__)

# Anything at or above this is a server-side failure. 4xx is the CLIENT being
# wrong (a bad token, a missing record, a validation error, a rate limit) and
# must never page anyone — those are load-bearing, expected responses in this app.
_FAILURE_STATUS_FLOOR = 500

# One durable failure report per process per this many seconds. With the alert
# thresholds in failure_alert (3 failures AND 60 seconds), a single warm instance
# alone still trips an alert about a minute into a sustained outage, while a
# thousand-error flood costs the database four writes a minute per instance.
_REPORT_INTERVAL_SECONDS = 15.0

# One recovery probe per process per this many seconds. Recovery has to be
# reasonably prompt — an incident that stays open blocks the alert for the next
# one — but it does not justify a query on every successful request.
_PROBE_INTERVAL_SECONDS = 60.0

# The in-process failure window used ONLY to decide whether the degraded
# (database-unreachable) path may alert. Cleared once this long has passed with
# no failure, so yesterday's blip cannot combine with today's.
_WINDOW_IDLE_RESET_SECONDS = 180.0

# How long a process must have been alive before it is allowed to spend a
# recovery probe. Serverless instances are created constantly and most of them
# serve one short request and freeze; without this, "one probe per process" would
# mean a probe (and, under NullPool, a fresh database connection) on every cold
# start. Anything that lives longer than this is a warm instance serving real
# traffic, and those are the ones that will notice a recovery.
_COLD_START_GRACE_SECONDS = 5.0

# A path segment is echoed into an alert email verbatim only if it looks like a
# fixed route word: starts with a letter, short, no exotic characters. Anything
# else — a record id, a UUID, a signed survey token — becomes ``{id}``. The
# allow-list direction is deliberate: a new route with a new kind of identifier
# in it is scrubbed by default rather than leaking until someone notices.
_SAFE_SEGMENT = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,23}$")

def _first_probe_due_at() -> float:
    """Seed ``last_probe_at`` so the first probe is due ``_COLD_START_GRACE_SECONDS``
    after this process started, rather than on its very first request."""
    return time.monotonic() - _PROBE_INTERVAL_SECONDS + _COLD_START_GRACE_SECONDS


# A failure reports IMMEDIATELY on a cold process (last_report_at = 0.0): fast
# detection is the whole point, and the cost is only paid when something is
# already wrong. A success probe waits out the cold-start grace above.
_state = {
    "last_report_at": 0.0,
    "last_probe_at": _first_probe_due_at(),
    "window_started_at": 0.0,
    "window_last_failure_at": 0.0,
    "window_failures": 0,
}


def reset() -> None:
    """Clear the in-process gate, as if the process had just started and served
    its first request a while ago. For tests (see tests/conftest.py)."""
    _state.update(
        last_report_at=0.0,
        last_probe_at=0.0,
        window_started_at=0.0,
        window_last_failure_at=0.0,
        window_failures=0,
    )
    failure_alert.reset_degraded_state()


def route_template(request: Request) -> str:
    """Render the request path with its identifiers removed.

    ``/alumni/8421`` becomes ``/alumni/{alumni_id}``; ``/survey/<signed token>``
    becomes ``/survey/{token}``. This runs on data that will be EMAILED, so the
    raw path never leaves this function — an alumni id is a pointer to a person's
    record and a survey token is a live credential.

    Two independent defences, because the first is not always available: matched
    path params are substituted by name, and then EVERY remaining segment must
    still pass ``_SAFE_SEGMENT`` or it is replaced. An exception raised before
    routing completes has no path params at all, and the scrubber still covers it.
    """
    path = request.url.path  # never request.url — the query string can carry PII
    params = request.scope.get("path_params") or {}
    by_value = {str(v): k for k, v in params.items() if str(v)}
    out = []
    for segment in path.split("/"):
        if not segment:
            out.append(segment)
        elif segment in by_value:
            out.append("{" + by_value[segment] + "}")
        elif _SAFE_SEGMENT.match(segment):
            out.append(segment)
        else:
            out.append("{id}")
    return "/".join(out)[:200] or "/"


def _note_failure_window(now: float) -> bool:
    """Advance the in-process failure window; return this process's own opinion
    of whether failure is SUSTAINED (used only by the degraded path)."""
    if now - _state["window_last_failure_at"] > _WINDOW_IDLE_RESET_SECONDS:
        _state["window_started_at"] = now
        _state["window_failures"] = 0
    _state["window_last_failure_at"] = now
    _state["window_failures"] += 1
    return (
        _state["window_failures"] >= failure_alert.ALERT_MIN_FAILURES
        and now - _state["window_started_at"] >= failure_alert.ALERT_MIN_SECONDS
    )


async def observe_failure(request: Request, status_code: int, error_kind: str) -> None:
    """Handle one failing request. Throttled, best-effort, never raises."""
    now = time.monotonic()
    sustained = _note_failure_window(now)
    if now - _state["last_report_at"] < _REPORT_INTERVAL_SECONDS:
        return
    # Stamp BEFORE awaiting: concurrent failures in this same process must not
    # all slip through the gate while the first one is in flight.
    _state["last_report_at"] = now
    await failure_alert.note_failure(
        FailureSignal(
            path=route_template(request),
            status_code=status_code,
            error_kind=error_kind,
        ),
        process_sustained=sustained,
    )


async def observe_success() -> None:
    """Handle one non-failing request. Sampled, best-effort, never raises."""
    now = time.monotonic()
    if now - _state["last_probe_at"] < _PROBE_INTERVAL_SECONDS:
        return
    _state["last_probe_at"] = now
    await failure_alert.note_success()


async def failure_alert_middleware(request: Request, call_next):
    """Watch every response for sustained server failure and alert once (#444).

    Sits outside the route stack so it sees BOTH shapes of failure: a 5xx
    response, and an exception that escaped every handler (FastAPI's catch-all
    ``Exception`` handler lives in Starlette's outermost ``ServerErrorMiddleware``,
    so an unhandled error reaches user middleware as a raise, not as a 500).

    Never changes the response and never swallows an exception — a monitor that
    alters behaviour is worse than no monitor.
    """
    if not failure_alert.alerting_enabled():
        # Unconfigured: not a single float is touched. This is the state local
        # dev, CI and the test suite run in.
        return await call_next(request)

    try:
        response = await call_next(request)
    except Exception as exc:
        # The request is already lost; recording it must not lose it differently.
        try:
            await observe_failure(request, 500, type(exc).__name__)
        except Exception:  # noqa: BLE001 - monitoring never masks the real error
            log.exception("failure_alert: monitor failed while recording an exception")
        raise

    try:
        if response.status_code < _FAILURE_STATUS_FLOOR:
            await observe_success()
        elif getattr(request.state, "alert_ignore", False):
            # A deliberate 5xx (site-wide maintenance mode returns 503). The
            # engineer turned it on; paging them about it is noise, and it would
            # also mask a real incident behind an "already open" one.
            pass
        else:
            await observe_failure(request, response.status_code, f"http_{response.status_code}")
    except Exception:  # noqa: BLE001 - monitoring never breaks a served response
        log.exception("failure_alert: monitor failed while recording a response")
    return response
