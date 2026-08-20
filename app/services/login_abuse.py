"""Brute-force / credential-guessing detection on the login path (#456).

On 2026-08-19 three sources hammered the production login endpoint:

    66.234.153.26  (Romania)     190 attempts   68 addresses   over 10 minutes
    159.26.103.94  (Seattle WA)  338 attempts   78 addresses   over  6 minutes
    134.82.68.139  (Miami FL)    222 attempts  202 addresses   over 16 SECONDS

Nothing succeeded. The addresses were fabricated — the operator had scraped the
department's public faculty list and was generating candidate NetIDs from name
patterns. THE OWNER ONLY FOUND OUT BECAUSE HE HAPPENED TO LOOK. This module is
the thing that would have told him.

--------------------------------------------------------------------------------
WHY THE EXISTING PROTECTION SAW NOTHING
--------------------------------------------------------------------------------
Two controls already existed and neither fired.

``app/core/rate_limit.py`` allows 600 pre-checks / 300 records per IP per 600s.
All three sources stayed under it — and it would not have mattered, because it is
an in-memory fixed-window counter, so on Vercel it is per warm instance and
shared by nobody.

``app/services/login_lockout.py`` counts failures PER EMAIL. Its cooldown needs
10 failures against ONE address; the sources above averaged 2.8, 4.3 and 1.1.
A campaign that spreads itself across 200 addresses is invisible to a per-email
counter by construction. That is the gap this module fills: it counts per SOURCE.

Neither of those is replaced.

⚠️ UPDATED BY #457 — THIS MODULE NOW ALSO BLOCKS. The paragraph that used to sit
here said this module notices and tells a human, "who can then block the source
at the edge", and that a detector which silently starts refusing logins can lock
the department out on a false positive. The first half turned out to be false:
Vercel's edge rate limiting is a Pro feature and this account is on Hobby, so
"block it at the edge" is not a control this project has. The throttle therefore
has to live in the app, and ``evaluate`` now calls ``login_block.apply`` on the
same measurement, at the same threshold, in the same transaction.

The second half still stands and is why blocking is built the way it is rather
than as a counter: a block NEVER applies to an address with a recent successful
sign-in, NEVER to one an engineer has signed in from, expires on its own within
the hour, and fails open when its store cannot be read. Read the safety-property
block in ``app/services/login_block.py`` before changing anything here — that
module owns the block, this one owns the measurement and the message.

--------------------------------------------------------------------------------
THE TWO SHAPES, AND WHY ONE RULE IS NOT ENOUGH
--------------------------------------------------------------------------------
"N failures in a window" catches only one of the two things an attacker does.

  SPRAY / ENUMERATION — a few passwords (often one) against MANY addresses. This
  is all three sources above. Per-address volume stays low on purpose, precisely
  to slip under per-account lockouts, so an attempt-count rule has to be set
  absurdly low to catch it. What is anomalous is the ADDRESS COUNT: no honest
  client tries eight different accounts.

  GUESSING — many passwords against ONE address. Here the distinct-address count
  is 1 forever and only the raw volume is anomalous.

So there are two rules and either one alone is sufficient to alert. See the
thresholds below for the numbers and for the arithmetic separating them from a
person mistyping their password.

--------------------------------------------------------------------------------
ONE ALERT PER INCIDENT, NOT PER ATTEMPT
--------------------------------------------------------------------------------
750 attempts must produce ONE message. The dedup discipline is the same one
``service_incidents`` uses for API outages (#444) and the argument is identical:
"have we already alerted about this?" has to be a fact about the SERVICE, and on
Vercel serverless there is no process to keep it in. So it is a row in
``login_abuse_incidents``, with a partial unique index permitting ONE open row
per (environment, source IP), and the alert is CLAIMED with
``UPDATE ... WHERE alert_sent_at IS NULL RETURNING`` — which exactly one
concurrent transaction can win under READ COMMITTED.

An incident is one CAMPAIGN from one source. It closes after
``_INCIDENT_QUIET_SECONDS`` of silence from that source — which also sends the
"that attack has stopped" report — so the same address
coming back next week is a new incident and alerts again — dedup is per incident,
not "one alert ever".

There is deliberately NO "the attack stopped" message. An outage recovery is
worth clearing because someone is waiting on it; an attacker giving up is not,
and every message that is not worth reading trains the reader to ignore the ones
that are.

--------------------------------------------------------------------------------
WHERE IT RUNS, AND WHAT IT COSTS THE LOGIN PATH
--------------------------------------------------------------------------------
It runs on the FAILURE path of ``POST /auth/login/record``, right after the
``login_failures`` row it measures has been committed — the same place, and for
the same reason, as the retention purge already hooked there: that route is the
only thing in the app that creates the rows, so creation and evaluation share one
trigger and the evaluation runs hardest exactly when the abuse is happening.

Not a cron: this project has one cron surface (the daily survey scheduler), a
DAILY probe would have detected the 16-second burst hours late or not at all, and
a second secret-authenticated endpoint is a second thing to own.

Three separate brakes keep it off the hot path:

 1. It never runs on a SUCCESSFUL login, on the pre-check, or on any other route.
    A real sign-in costs exactly nothing.
 2. It never runs when BOTH alerting and blocking are switched off — which is
    what local dev, CI and the test suite mean when they set no webhook and no
    ``LOGIN_AUTO_BLOCK_ENABLED``... except that blocking DEFAULTS ON (#457), so
    in practice this brake is now only reachable by turning the kill switch off
    as well. That is deliberate: alerting is observability and may be unset,
    blocking is protection and a missing env var must not disable it.
 3. When it does run, an in-process gate limits it to ONE query every
    ``_EVAL_INTERVAL_SECONDS`` per process — so the 222-attempt burst costs about
    four queries rather than 222. Normal operation is a handful of failed logins a
    day, and they are far enough apart that each simply gets its one query.

The query itself is a single aggregate over ``login_failures`` restricted to ONE
ip_address inside the window, served by ``idx_login_failures_ip_occurred``.

Everything is best-effort: it runs in its own commit AFTER the throttle counter
and the failure row are committed, and any error is rolled back and swallowed. It
cannot change the response body — which matters more here than usual, because
these routes are contractually identical whatever email you send (see the
anti-enumeration note in ``login_lockout``), and a detector that altered timing
or output would be a side channel.

THE ONE COST IT DOES PAY. Delivering the alert is an outbound HTTP call made
inline, so ONE request in the whole campaign is a couple of seconds slower — the
request that wins the claim, and no other, because every later attempt finds
``alert_sent_at`` already set and sends nothing. It is time-boxed and it can
never fail the response. Paying it once per incident is the same trade
``failure_alert`` makes on the outage path.

--------------------------------------------------------------------------------
WHERE THE ALERT GOES
--------------------------------------------------------------------------------
Delivery is ``failure_alert.deliver_alert`` with ``purpose=SECURITY``, so this is
the one thing in the app that posts to #security-alerts
(``SLACK_SECURITY_WEBHOOK_URL``) rather than #error-alerts. If that webhook is
unset it falls back to the error channel rather than being dropped — a forgotten
env var must never become silence about an attack, which is the exact failure
this module exists to end. Because of that fallback the two kinds can share one
channel, so every Slack message is tagged ``SECURITY`` or ``OUTAGE`` up front.
Email, where configured, gets it too; there is one alert mailbox.

--------------------------------------------------------------------------------
NO PII IN THE ALERT
--------------------------------------------------------------------------------
The message names the source IP, its approximate geography, counts, and a time
window. IT NEVER NAMES THE ATTEMPTED ADDRESSES. Those are unverified strings a
stranger typed into a login form, some of them belong to real people, and a list
of them is exactly the scraped-and-guessed material the attacker was probing
with — putting it in a Slack channel would re-publish it and would also hand the
reader an enumeration oracle. The counts are what you act on; the addresses stay
in the database behind the engineer console.

⚠️ THAT RULE SURVIVES THE MESSAGES BECOMING EDITABLE (2026-08-20). The Slack
wording is now a template the owner edits from the engineer console, and a
template CANNOT widen what a message may say: it names placeholders, and the only
names that resolve are the ones :func:`_template_values` puts in the dict.
``{addresses}`` is ``distinct_email_count``, a COUNT. There is no placeholder that
reaches an attempted address, adding one would take a code change and a review,
and ``app/services/alert_templates.py`` carries both a tripwire and tests against
exactly that edit.
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database
from app.core.config import get_settings
from app.services import alert_templates, failure_alert, login_block

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ thresholds --
#
# TUNED AGAINST BOTH ENDS: the three real sources above must all trip, and a
# staff member who mistypes their password four times in a row must never.
#
# The rolling window every count is measured over. Long enough that the slow
# sources (~19 and ~56 attempts/minute) accumulate well past both thresholds, and
# short enough that a week of unrelated typos from one office cannot add up into
# one. The 16-second burst fits inside it many times over.
WINDOW_MINUTES = 15
_WINDOW_SECONDS = WINDOW_MINUTES * 60

# RULE 1 — SPRAY/ENUMERATION: distinct addresses attempted from one source.
#
#   a person mistyping their own password ...  1 address, forever
#   the entire staff directory ..............  a couple of dozen accounts
#   66.234.153.26 ...........................  68
#   159.26.103.94 ...........................  78
#   134.82.68.139 ........................... 202
#
# Eight is comfortably above anything honest and far below all three sources.
# A real client tries ONE address; even a shared office egress address would need
# eight different people to fail inside the same fifteen minutes, which has never
# happened in this deployment and would itself be worth a look. The sources reach
# eight after roughly 22, 15 and 9 attempts respectively — i.e. within the first
# ~90, ~16 and ~1 seconds of each campaign.
SPRAY_MIN_DISTINCT_EMAILS = 8

# RULE 2 — GUESSING: raw failed attempts from one source, whatever the spread.
#
# This is the rule that covers the shape NOT present in the 2026-08-19 data:
# hundreds of passwords against a single address, where rule 1 stays at 1 forever.
#
#   a person mistyping their password .......  4
#   ... and then some ....................... 10, at which point login_lockout's
#                                              cooldown stops them attempting
#                                              (COOLDOWN_THRESHOLD), and 20 hard-
#                                              locks a registered account
#   66.234.153.26 ........................... 190
#   159.26.103.94 ........................... 338
#   134.82.68.139 ........................... 222
#
# Thirty is 7.5x the fumbling user and cannot be reached by one address at all
# without first tripping the per-email cooldown and then the hard lock, so
# crossing it means either several accounts or a deliberately-paced attacker.
# Every real source above clears it by a factor of six or more.
BURST_MIN_ATTEMPTS = 30

# An incident closes after this long with no failure from the source. It is the
# definition of "that campaign is over", so it decides TWO things at once and
# they cannot be separated: when the "the attack has stopped" report goes out,
# and when the same address returning counts as a NEW campaign worth announcing
# again.
#
# FIVE MINUTES, down from an hour, at the owner's request: "after the attack has
# stopped for 5 minutes from that ip tell me that the attack from that ip is
# done". An hour was chosen to keep a campaign that pauses to re-target as one
# message; five minutes accepts a second message in that case, which is the right
# trade for a report that actually arrives while you still care. It is still long
# enough that the gaps INSIDE a campaign do not split it -- every real source on
# 2026-08-19 ran its whole campaign in under ten minutes, most in seconds.
#
# ⚠️ THE REPORT IS NOT A TIMER. Nothing in this stack runs on its own; the sweep
# rides the next request the API serves after the window (see `note_success`). On
# a quiet night the report waits for the next visitor. Five minutes is when it
# becomes ELIGIBLE, not when it is guaranteed to arrive.
_INCIDENT_QUIET_SECONDS = 300

# One evaluation per process per this many seconds.
#
# FIVE, AND THE 16-SECOND BURST IS WHY. The first draft used thirty, which looks
# obviously cheap and is obviously wrong the moment you replay 134.82.68.139:
# 222 attempts in 16 seconds means the entire campaign lands between two
# evaluations, so the process measures once (one row, nothing to see) and never
# looks again. It detected nothing at all. Any interval in the same order of
# magnitude as an attack is not a throttle, it is a blindfold.
#
# Five turns that same burst into ~4 queries instead of 222 while still alerting
# about five seconds in, and puts the two slower campaigns ~5 and ~10 seconds
# after their eighth distinct address. It costs normal operation nothing: the gate
# is only reached on a FAILED login, and there are a handful of those a day.
_EVAL_INTERVAL_SECONDS = 5.0

# Time budgets. These sit on an unauthenticated public route, so they are short by
# design: better a missed detection than a request held open.
_DB_TIMEOUT_SECONDS = 3.0
_DELIVERY_TIMEOUT_SECONDS = 8.0

# In-process evaluation gate. NOT the dedup — that is the durable row; this is a
# rate limiter and nothing more (the same distinction ``failure_monitor`` draws).
#
# ``None`` means "has never evaluated", so a FRESHLY STARTED process evaluates on
# the first failed login it sees rather than waiting out an interval. That is the
# opposite of the cold-start grace ``failure_monitor`` gives its recovery probe,
# and deliberately so: the 16-second burst is over before any grace period would
# expire, and unlike a success probe this only ever runs on a failed login, which
# is rare enough in normal operation that "evaluate on the first one" costs a
# query a day.
#
# ⚠️ IT MUST NOT BE 0.0 — the exact bug ``failure_alert`` documents at length for
# its degraded-path cooldown. ``time.monotonic()`` is measured from an arbitrary
# origin (on Linux, machine boot), so a freshly started instance returns a small
# number; against a 0.0 baseline the gate below reads as "we just evaluated" and
# swallows detection for the whole first interval of that instance's life. On
# Vercel every cold start is a fresh instance, so the evaluation most likely to be
# suppressed is the first one of a burst.
_last_eval_at: float | None = None


def reset() -> None:
    """Clear the in-process evaluation gate. For tests (see tests/conftest.py)."""
    global _last_eval_at
    _last_eval_at = None


# ----------------------------------------------------------------- statements --
#
# Raw statements rather than ORM writes, for the same reason as
# ``failure_alert``: the guarantee is that POSTGRES evaluates these conditions.
# ``ON CONFLICT ... DO UPDATE`` on a partial unique index and
# ``UPDATE ... WHERE <col> IS NULL RETURNING`` are the two primitives that make
# "exactly one of N concurrent instances sends the message" true, and neither
# survives being re-expressed as read-then-write in Python.

# What one source has been doing lately. One aggregate, one ip_address, one
# index. `count(DISTINCT email)` is the whole point — it is the column the
# per-email lockout cannot see.
_SQL_MEASURE = text(
    """
    SELECT count(*)               AS attempts,
           count(DISTINCT email)  AS distinct_emails,
           min(occurred_at)       AS first_at,
           max(occurred_at)       AS last_at
      FROM login_failures
     WHERE ip_address = :ip
       AND occurred_at >= now() - (:window_seconds * interval '1 second')
    """
)

# Close this source's incident once it has gone quiet, so a later campaign from
# the same address opens a fresh one and is reported again. Scoped to the source
# being evaluated: an incident for some OTHER address staying open costs nothing
# (uniqueness is per source) and reaping it would mean an unindexed sweep on a
# public route.
_SQL_CLOSE_IF_QUIET = text(
    """
    UPDATE login_abuse_incidents
       SET resolved_at = now(),
           updated_at = now()
     WHERE environment = :environment
       AND ip_address = :ip
       AND resolved_at IS NULL
       AND last_seen_at < now() - (:quiet_seconds * interval '1 second')
    RETURNING abuse_incident_id, environment, ip_address, city, region, country,
              started_at, last_seen_at, resolved_at, attempt_count,
              distinct_email_count, pattern, alert_sent_at
    """
)

# The same close, for EVERY source that has gone quiet rather than the one
# currently attacking. `evaluate` only runs on a failed sign-in FROM a source, so
# on its own it can never notice that a campaign STOPPED -- the attacker going
# away is precisely the absence of the event that would trigger it. This is
# swept from the sampled success path instead (see `sweep_quiet`).
#
# ⚠️ `RETURNING` on an `UPDATE ... WHERE resolved_at IS NULL` IS THE CLAIM, and it
# needs no new column: under READ COMMITTED exactly one transaction can flip a
# given row's `resolved_at` from NULL, so of twenty instances sweeping at once,
# one gets the row back and nineteen get nothing. Same trick as the alert claim
# above, one row later in the lifecycle.
_SQL_CLOSE_ALL_QUIET = text(
    """
    UPDATE login_abuse_incidents
       SET resolved_at = now(),
           updated_at = now()
     WHERE environment = :environment
       AND resolved_at IS NULL
       AND last_seen_at < now() - (:quiet_seconds * interval '1 second')
    RETURNING abuse_incident_id, environment, ip_address, city, region, country,
              started_at, last_seen_at, resolved_at, attempt_count,
              distinct_email_count, pattern, alert_sent_at
    """
)

# Open this source's incident, or fold the fresh measurement into the one already
# open. The partial unique index makes the INSERT and the UPDATE the same
# statement, so twenty concurrent instances produce one row rather than twenty.
#
# The counters take GREATEST because the measurement is over a ROLLING window: a
# campaign longer than the window would otherwise make the recorded totals go
# backwards, and the row should hold the high-water mark. `started_at` takes LEAST
# for the mirror-image reason.
_SQL_UPSERT = text(
    """
    INSERT INTO login_abuse_incidents
        (environment, ip_address, started_at, last_seen_at, attempt_count,
         distinct_email_count, window_seconds, city, region, country, pattern)
    VALUES
        (:environment, :ip, :first_at, now(), :attempts,
         :distinct_emails, :window_seconds, :city, :region, :country, :pattern)
    ON CONFLICT (environment, ip_address) WHERE resolved_at IS NULL
    DO UPDATE SET
        started_at           = LEAST(login_abuse_incidents.started_at,
                                     EXCLUDED.started_at),
        last_seen_at         = now(),
        attempt_count        = GREATEST(login_abuse_incidents.attempt_count,
                                        EXCLUDED.attempt_count),
        distinct_email_count = GREATEST(login_abuse_incidents.distinct_email_count,
                                        EXCLUDED.distinct_email_count),
        pattern              = EXCLUDED.pattern,
        city                 = COALESCE(login_abuse_incidents.city, EXCLUDED.city),
        region               = COALESCE(login_abuse_incidents.region, EXCLUDED.region),
        country              = COALESCE(login_abuse_incidents.country, EXCLUDED.country),
        updated_at           = now()
    RETURNING abuse_incident_id
    """
)

# Claim THE message. The thresholds are re-evaluated INSIDE the claim, against the
# committed row, so the decision and the claim are one atomic act and there is no
# window in which two instances both read "over threshold, not yet alerted".
_SQL_CLAIM_ALERT = text(
    """
    UPDATE login_abuse_incidents
       SET alert_sent_at = now(),
           updated_at = now()
     WHERE abuse_incident_id = :incident_id
       AND alert_sent_at IS NULL
       AND resolved_at IS NULL
       AND (distinct_email_count >= :min_distinct OR attempt_count >= :min_attempts)
    RETURNING abuse_incident_id, environment, ip_address, started_at, last_seen_at,
              attempt_count, distinct_email_count, window_seconds,
              city, region, country, pattern
    """
)


# ------------------------------------------------------------------- policy ---


def is_abusive(attempts: int, distinct_emails: int) -> bool:
    """Either rule alone is enough. See the threshold block for the arithmetic.

    ⚠️ SINCE #457 THIS ALSO DECIDES WHO GETS BLOCKED, not only who gets reported.
    ``login_block.apply`` is called on exactly the sources this returns True for,
    so retuning either constant above moves the block, the Slack alert and the
    engineer console's attack table together — which is the point, and is why
    there is no second threshold in ``login_block``. The owner asked for five
    distinct addresses; the argument for keeping it at eight (they stop the same
    three campaigns within seconds of each other, and five is inside the range a
    single confused staff member can reach on their own) is written out in that
    module's docstring.
    """
    return (
        distinct_emails >= SPRAY_MIN_DISTINCT_EMAILS or attempts >= BURST_MIN_ATTEMPTS
    )


def classify(attempts: int, distinct_emails: int) -> str:
    """Name the SHAPE of the campaign, so the reader knows what they are looking at.

    Three fixed strings, never anything derived from input — this value is stored
    and then rendered into a Slack message.
    """
    if distinct_emails < SPRAY_MIN_DISTINCT_EMAILS:
        return "guessing: repeated attempts against few addresses"
    if attempts < distinct_emails * 2:
        return "enumeration: many addresses, about one attempt each"
    return "spraying: many addresses, a few passwords each"


# The label for a source that has failed a few sign-ins and tripped NEITHER rule.
# ``classify`` deliberately has no such branch — it is only ever reached for a
# source already known to be abusive, and giving it a fourth return value would
# mean the alert could one day say "not an attack". So the below-threshold case
# lives here, in the one function that has to render both kinds of source.
NOT_AN_ATTACK = "no attack pattern: isolated failed sign-ins"


def classify_source(attempts: int, distinct_emails: int) -> str:
    """The "type of attack" cell for ONE source on the engineer console.

    The console lists every source with a failed sign-in in the window, not only
    the abusive ones (an empty table is the reassurance the page exists to give,
    and a source at seven addresses is worth seeing before it reaches eight). So
    it needs a label for the honest ones too, which is the only thing this adds.

    ⚠️ IT MUST STAY A WRAPPER. The console and the Slack alert must never
    disagree about what an attack was — reading "spraying" in a message and
    "enumeration" in the table for the same IP would make the reader distrust
    both. That is guaranteed structurally here rather than by discipline: the
    same ``is_abusive`` decides whether it is an attack at all and the same
    ``classify`` names the shape, so a retuned threshold or a reworded pattern
    moves both surfaces in one edit. Do not reimplement either rule here.
    """
    if not is_abusive(attempts, distinct_emails):
        return NOT_AN_ATTACK
    return classify(attempts, distinct_emails)


# ----------------------------------------------------- console: per-source roll-up --

# One row per source IP over a window, for the engineer Maintenance page's
# attack table (the same shape the detector measures for ONE ip, widened to all
# of them). Read-only: it writes nothing, opens no incident and sends no alert.
#
# ⚠️ NO EMAIL COLUMN, AND THERE MUST NEVER BE ONE. `count(DISTINCT email)` is
# the number the reader acts on; the addresses themselves are unverified strings
# a stranger typed, some belong to real people, and returning a list of them
# would hand any engineer-console reader — or anything that later proxies this
# response — the enumeration oracle the whole feature is trying to deny the
# attacker. Same rule as the alert body (see the PII note in this module's
# docstring); tests assert the response cannot carry an address.
#
# `city`/`region`/`country` are taken with `max()` rather than "the latest one".
# They are derived from the IP by the edge, so every row for one source carries
# the same value and any aggregate picks it; `max()` is the cheap one and, unlike
# an ordered pick, it cannot be NULL just because the most recent attempt
# happened to arrive without geo headers.
#
# Attempts with NO ip_address are excluded, not lumped together: this table's
# unit is a SOURCE, and one "unknown" bucket would sum unrelated people into a
# row that looks like a campaign. They are still visible per-attempt on the
# Login-failures page, which the console links to.
#: The aggregate, with the time predicate written so that a NULL window means
#: EVERY attempt ever recorded rather than none of them. One statement, not two:
#: a second copy differing only in a WHERE clause is how the windowed and
#: all-time views end up disagreeing about what an attack was.
_SQL_SOURCES = text(
    """
    SELECT ip_address,
           count(*)              AS attempts,
           count(DISTINCT email) AS distinct_emails,
           min(occurred_at)      AS first_seen,
           max(occurred_at)      AS last_seen,
           max(city)             AS city,
           max(region)           AS region,
           max(country)          AS country
      FROM login_failures
     WHERE (
             CAST(:window_seconds AS integer) IS NULL
             OR occurred_at >= now()
                - (CAST(:window_seconds AS integer) * interval '1 second')
           )
       AND ip_address IS NOT NULL
     GROUP BY ip_address
     ORDER BY count(*) DESC, max(occurred_at) DESC
     LIMIT :limit
    """
)


async def summarize_sources(
    session: AsyncSession, *, window_seconds: int | None, limit: int
) -> list[dict]:
    """Group ``login_failures`` by source IP, busiest first.

    ``window_seconds=None`` means EVERY attempt ever recorded. The console asks
    for that by default: a 24-hour window made yesterday's incident vanish
    overnight, which reads as "the data was deleted" rather than "the window
    moved" -- it did exactly that to the owner the morning after the first real
    campaigns. A summary of attacks is not a live feed; the question it answers
    is "has anyone ever come at us", and that question has no window.

    Returns plain dicts carrying the counts, the window the source was active
    over, and ``classify_source``'s label. Never the attempted addresses.

    PERFORMANCE — WHICH INDEX SERVES THIS. The predicate is a bare time range and
    the grouping key is ``ip_address``, so the index this query wants is the
    time-ordered one that has been there since #423,
    ``idx_login_failures_occurred_at (occurred_at DESC)``: a range scan over the
    window, then a heap fetch for email/ip/geo and a hash aggregate over just
    those rows.

    It is NOT served by ``idx_login_failures_ip_occurred (ip_address,
    occurred_at DESC)``, the composite the detector's migration added — that
    index leads on ``ip_address``, and a query with no ip predicate cannot range-
    scan it, so Postgres would have to read the whole index and still visit the
    heap for ``email``. That index is right for what it was built for (one source
    inside a window, on the login hot path) and simply does not apply here.

    No third index is added for this, INCLUDING for the all-time case, where the
    plan is a sequential scan and an aggregate over the whole table. That is the
    right trade here: ``login_failures`` is an incident log, not a traffic log —
    every row is a FAILED sign-in, it is purged on a retention schedule by the
    record route, and the real production campaigns that made this feature exist
    were 750 rows between them. The reader is one engineer, on one page, behind
    an engineer gate, opening it a handful of times a year. If ``login_failures`` is ever left
    to grow into the millions, the answer is that same retention purge — or
    putting the window back — not another index on a table written to on every
    failed login.
    """
    rows = (
        (
            await session.execute(
                _SQL_SOURCES, {"window_seconds": window_seconds, "limit": limit}
            )
        )
        .mappings()
        .all()
    )
    sources = []
    for row in rows:
        attempts = int(row["attempts"] or 0)
        distinct_emails = int(row["distinct_emails"] or 0)
        sources.append(
            {
                "ip_address": row["ip_address"],
                "city": row["city"],
                "region": row["region"],
                "country": row["country"],
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "attempts": attempts,
                "distinct_emails": distinct_emails,
                "attack_type": classify_source(attempts, distinct_emails),
                "is_attack": is_abusive(attempts, distinct_emails),
            }
        )
    return sources


# ------------------------------------------------------------- state machine ---


async def evaluate(
    session: AsyncSession,
    *,
    ip_address: str,
    city: str | None = None,
    region: str | None = None,
    country: str | None = None,
) -> dict | None:
    """Measure one source, BLOCK it if it is abusive, and fold the result into
    the durable incident state.

    Returns the incident row when THIS caller won the right to send the message,
    and ``None`` in every other case (under threshold, incident merely opened or
    updated, someone else already claimed it, alerting switched off). The returned
    dict carries two extra keys the alert renders — ``block_applied`` and
    ``blocked_until`` — so the message can say what was actually DONE about the
    source, which is the useful half of it.

    ORDER MATTERS AND IT IS BLOCK-FIRST (#457). The block is applied before the
    incident is opened and before anything is claimed, because it is the
    PROTECTION and the rest is the observability. If the incident upsert or the
    claim fails, the source is still blocked; if the block fails, the alert still
    goes out. Neither half can take the other down with it, and the half that
    matters more runs first.

    Commits before returning: the claim must be durable before a message goes out,
    never after. If the commit fails no message is sent — the correct direction to
    fail, because a claim is repeatable and a Slack post is not.
    """
    environment = get_settings().environment
    measured = (
        (
            await session.execute(
                _SQL_MEASURE, {"ip": ip_address, "window_seconds": _WINDOW_SECONDS}
            )
        )
        .mappings()
        .first()
    )
    attempts = int((measured or {}).get("attempts") or 0)
    distinct_emails = int((measured or {}).get("distinct_emails") or 0)

    # Under threshold: no row is written at all, and nothing is blocked.
    # `login_abuse_incidents` holds incidents, not observations, so a directory
    # full of honest typos never appears in it and a row in that table always
    # means something happened. The same is true of `login_ip_blocks`.
    if not is_abusive(attempts, distinct_emails):
        # Close the read transaction the SELECT opened rather than leaving it
        # idle: this session belongs to the request and the route still has work
        # to do on it.
        await session.commit()
        return None

    pattern = classify(attempts, distinct_emails)

    # --- THE BLOCK (#457) ----------------------------------------------------
    # Same measurement, same threshold, same transaction. `apply` returns None
    # when the source is EXEMPT — a recent successful sign-in from that address,
    # an engineer's address, or a block an engineer lifted in the last day — and
    # those three exemptions are `NOT EXISTS` clauses Postgres evaluates inside
    # the INSERT, not conditions this caller checks. See app/services/login_block.py.
    block = await login_block.apply(
        session,
        ip_address=ip_address,
        attempts=attempts,
        distinct_emails=distinct_emails,
        pattern=pattern,
    )

    # --- THE MESSAGE ---------------------------------------------------------
    # Alerting is independently optional (no webhook, no email => nothing to send
    # to). Blocking above is NOT behind this gate, on purpose: a rotated Slack
    # webhook must never silently disable a security control. Commit what was
    # written and stop.
    if not failure_alert.alerting_enabled():
        await session.commit()
        return None

    # A source coming BACK after an hour of silence closes its old campaign so the
    # upsert below opens a new one rather than bumping a dead row.
    #
    # This is now a SAFETY NET rather than the usual path: `observe_failure` runs
    # `sweep_quiet` first, which closes AND reports every quiet campaign including
    # this source's. Reporting cannot happen here -- the close has to be reported
    # by the transaction that wins it, and this function returns the row it won a
    # different race for (the alert claim). A close that lands here is therefore
    # silent, which is why it is not where the closing happens.
    await session.execute(
        _SQL_CLOSE_IF_QUIET,
        {
            "environment": environment,
            "ip": ip_address,
            "quiet_seconds": _INCIDENT_QUIET_SECONDS,
        },
    )
    upserted = (
        (
            await session.execute(
                _SQL_UPSERT,
                {
                    "environment": environment,
                    "ip": ip_address,
                    "first_at": measured["first_at"],
                    "attempts": attempts,
                    "distinct_emails": distinct_emails,
                    "window_seconds": _WINDOW_SECONDS,
                    "city": city,
                    "region": region,
                    "country": country,
                    "pattern": pattern,
                },
            )
        )
        .mappings()
        .first()
    )
    if upserted is None:
        # Raced: the open incident was resolved between the close and the upsert.
        # The next failed login re-opens it. The block above already landed and
        # is unaffected — that is the point of doing it first.
        await session.commit()
        return None

    if block is not None:
        # Best-effort back-link so the console can tie a block to the campaign
        # that caused it. Deliberately AFTER both writes rather than passed into
        # the block above: doing it the other way round would make the block
        # depend on the incident upsert succeeding, i.e. would put the
        # observability on the protection's critical path.
        await login_block.link_incident(
            session,
            block_id=block["block_id"],
            incident_id=upserted["abuse_incident_id"],
        )

    claimed = (
        (
            await session.execute(
                _SQL_CLAIM_ALERT,
                {
                    "incident_id": upserted["abuse_incident_id"],
                    "min_distinct": SPRAY_MIN_DISTINCT_EMAILS,
                    "min_attempts": BURST_MIN_ATTEMPTS,
                },
            )
        )
        .mappings()
        .first()
    )
    await session.commit()
    if claimed is None:
        return None
    incident = dict(claimed)
    # What was DONE about the source, carried on the claimed row so the message
    # can state it. `False` here is not a failure — it is the anti-DoS exemption
    # doing its job, and the alert says which.
    incident["block_applied"] = block is not None
    incident["blocked_until"] = block["blocked_until"] if block is not None else None
    return incident


# ----------------------------------------------------------------- rendering ---


def _location(incident: dict) -> str:
    parts = [
        str(incident.get(key)).strip()
        for key in ("city", "region", "country")
        if incident.get(key)
    ]
    return ", ".join(parts) if parts else "unknown"


def _action_taken(incident: dict) -> str:
    """One sentence naming what the app DID about this source (#457).

    Three outcomes, and the reader has to be able to tell them apart at a glance
    — "blocked", "deliberately not blocked", and "blocking is switched off" are
    very different facts to wake up to. The exempt wording names all three
    exemptions rather than the one that fired: which one it was is a property of
    the address, and spelling it out in a channel would say something about who
    signs in from where.
    """
    if not login_block.blocking_enabled():
        return (
            "NONE — automatic blocking is switched off "
            "(LOGIN_AUTO_BLOCK_ENABLED=false). Nothing is refusing this source."
        )
    if not incident.get("block_applied"):
        return (
            "NOT blocked — this address is exempt: it has a recent successful "
            "sign-in, or an engineer has signed in from it, or an engineer "
            "lifted a block on it in the last 24 hours. The address is "
            "client-supplied, so blocking it could have locked out whoever "
            "really signs in from there."
        )
    until = failure_alert._fmt_ts(incident.get("blocked_until"))
    return (
        f"BLOCKED — sign-in attempts from this address are refused until {until}. "
        "It expires on its own; no action is needed to end it."
    )


def render_alert(incident: dict) -> tuple[str, list[tuple[str, str]]]:
    """Build the alert's subject and its label/value rows.

    Kept separate from sending so the exact wording is unit-testable — including
    the assertion that no attempted address can appear in it.

    Reuses ``failure_alert``'s timestamp/duration/build helpers on purpose: both
    alerts land in the same channel and the same mailbox, and two vocabularies for
    "when did this start" would be two things to read carefully instead of one.
    """
    env = str(incident.get("environment") or "unknown")
    ip = str(incident.get("ip_address") or "unknown")
    attempts = incident.get("attempt_count")
    distinct = incident.get("distinct_email_count")
    minutes = int(incident.get("window_seconds") or _WINDOW_SECONDS) // 60
    subject = (
        f"[fa-web-api {env}] Login abuse from {ip}: "
        f"{distinct} addresses, {attempts} failed attempts"
    )
    rows = [
        ("Environment", env),
        ("Source IP", ip),
        ("Location (IP geolocation, approximate)", _location(incident)),
        ("Failed attempts", f"{attempts} in the last {minutes} minutes"),
        ("Distinct addresses attempted", str(distinct)),
        ("First seen", failure_alert._fmt_ts(incident.get("started_at"))),
        ("Latest attempt", failure_alert._fmt_ts(incident.get("last_seen_at"))),
        (
            "Duration",
            failure_alert._fmt_duration(
                incident.get("started_at"), incident.get("last_seen_at")
            ),
        ),
        ("Pattern", str(incident.get("pattern") or "unknown")),
        # Stated in the message itself so nobody has to wonder whether the list
        # was omitted or the detector failed to collect it.
        (
            "Attempted addresses",
            "withheld on purpose (unverified input; some may be real people)",
        ),
        # THE USEFUL HALF (#457). Before this line the message told the reader
        # about an attack and left them to do something about it at an edge this
        # account cannot configure. Now it says what already happened.
        ("Action taken", _action_taken(incident)),
        (
            "Next step",
            "nothing is required — the block ends by itself. To end it early, "
            "or if this was a false positive, lift it from the engineer "
            "console's Login blocks list; the Login-failures tab has the "
            "per-attempt detail",
        ),
        ("Build", failure_alert._deployment_note()),
        ("Incident", f"#{incident.get('abuse_incident_id')}"),
    ]
    return subject, rows



def _slack_action(incident: dict) -> str:
    """The four-word version of :func:`_action_taken`, for the Slack line.

    Same three outcomes and the same reason they must be told apart; the email
    spells each one out, this says which it was. Kept as a function rather than
    inline in the summary because it is now the ``{action}`` PLACEHOLDER value
    (see ``app/services/alert_templates.py``) — a template may move it, drop it,
    or wrap it in other words, and it must read the same wherever it lands.
    """
    if not login_block.blocking_enabled():
        return "NOT blocked — automatic blocking is switched off."
    if not incident.get("block_applied"):
        return "Not blocked — the address is exempt (see the email)."
    return "It is blocked and cannot sign in."


def _template_values(incident: dict, *, duration: str | None = None) -> dict:
    """The facts a security template may name, and NOTHING ELSE.

    ⚠️ THIS FUNCTION IS THE BOUNDARY. A stored template can reach exactly what is
    in this dict (further narrowed by the placeholders its kind declares), so
    "what can the owner's wording put in a Slack channel" is answerable by
    reading these twelve lines rather than by auditing every call site.

    ⚠️ IT MUST NEVER CARRY AN ATTEMPTED EMAIL ADDRESS. ``addresses`` is
    ``distinct_email_count`` — a COUNT. The addresses themselves are unverified
    strings a stranger typed, some belong to real people, and a list of them is
    the enumeration oracle this whole feature exists to deny the attacker; they
    are not in the incident row either (see the PII note in this module's
    docstring and in the migration). Tests assert that a field planted on the
    incident cannot reach a rendered message.
    """
    where = _location(incident)
    known = where != "unknown"
    return {
        "ip": str(incident.get("ip_address") or "an unknown address"),
        "location": where,
        # The two conditional forms carry their own leading space and vanish when
        # the edge gave us no geolocation, which is what lets one template read
        # correctly in both cases. See the note in alert_templates.
        "location_phrase": f" from {where}" if known else "",
        "location_parenthetical": f" ({where})" if known else "",
        "attempts": str(incident.get("attempt_count") or 0),
        "addresses": str(incident.get("distinct_email_count") or 0),
        "duration": duration
        or failure_alert._fmt_duration(
            incident.get("started_at"), incident.get("last_seen_at")
        ),
        "pattern": str(incident.get("pattern") or "unknown"),
        "action": _slack_action(incident),
        "environment": str(incident.get("environment") or "unknown"),
    }


def render_slack_summary(incident: dict, *, templates: dict | None = None) -> str:
    """The SLACK version of an opening alert: who is attacking, and from where.

    One sentence, because that is the whole job. The email carries fourteen
    labelled rows and it should -- that is the artefact you read when you sit
    down to work out what happened. This is read on a phone, at a glance, and the
    only question it has to answer is the one the owner asked for: are we being
    attacked, by whom, from where.

    Everything else is deliberately left out. Counts, timings, pattern name,
    withheld-addresses note, next steps: all in the mail, none of them the thing
    you need in the first two seconds. The action IS kept, in four words, because
    "and it is already blocked" is the difference between reading this in the
    morning and getting out of bed.

    ``templates`` is the owner's stored wording, from ``alert_templates.load()``.
    Omitting it — which every unit test and every fallback path does — renders the
    built-in default, and the built-in default is this paragraph's wording
    unchanged. This function stays PURE: the read happens at the call site, on the
    alerting path, so a renderer can still be exercised without a database.
    """
    return alert_templates.render(
        alert_templates.SECURITY_ATTACK_OPENING,
        _template_values(incident),
        templates=templates,
    )


def render_resolved(
    incident: dict, *, templates: dict | None = None
) -> tuple[str, list[tuple[str, str]], str]:
    """The "that campaign is over" message: subject, email rows, Slack line.

    The counterpart to the opening alert, and the half the owner asked for
    second: one line while it is happening, a short report once it stops. It
    mirrors the outage path's recovery message exactly -- same shape, same
    reason -- so the two kinds of incident read the same way and neither leaves
    you wondering whether it ever ended.

    Sent only for a campaign that was ANNOUNCED (``alert_sent_at`` is set). There
    is nothing to close for a reader who was never told it opened, and a
    "resolved" message for an event nobody saw is pure noise.

    Only the SLACK line is editable (``templates``, from
    ``alert_templates.load()``). The email's rows are left alone on purpose: they
    are the forensic artefact, and their stable shape is what makes two reports
    from different weeks comparable.
    """
    env = str(incident.get("environment") or "unknown")
    ip = str(incident.get("ip_address") or "unknown")
    where = _location(incident)
    attempts = incident.get("attempt_count")
    distinct = incident.get("distinct_email_count")
    duration = failure_alert._fmt_duration(
        incident.get("started_at"), incident.get("last_seen_at")
    )
    subject = f"[fa-web-api {env}] Login abuse from {ip} has stopped"
    rows = [
        ("Environment", env),
        ("Source IP", ip),
        ("Location (IP geolocation, approximate)", where),
        ("Total failed attempts", str(attempts)),
        ("Distinct addresses attempted", str(distinct)),
        ("First seen", failure_alert._fmt_ts(incident.get("started_at"))),
        ("Last attempt", failure_alert._fmt_ts(incident.get("last_seen_at"))),
        ("Attacked for", duration),
        ("Pattern", str(incident.get("pattern") or "unknown")),
        # The outcome, stated plainly. Every one of these campaigns so far has
        # ended this way and the reader should be told so rather than left to
        # infer it from the absence of bad news.
        ("Outcome", "no sign-in succeeded — these were attempts, not a breach"),
        ("Incident", f"#{incident.get('abuse_incident_id')}"),
    ]
    summary = alert_templates.render(
        alert_templates.SECURITY_ATTACK_RESOLVED,
        _template_values(incident, duration=duration),
        templates=templates,
    )
    return subject, rows, summary


async def _send_resolved(incident: dict, *, templates: dict | None = None) -> None:
    """Deliver one "campaign over" report. Best-effort; never raises.

    Silent for a campaign nobody was told about -- see :func:`render_resolved`.

    ``templates`` is read ONCE by the caller and passed in, not read here: a sweep
    that closes several campaigns at once would otherwise do one read per report.
    """
    if incident is None or incident.get("alert_sent_at") is None:
        return
    subject, rows, summary = render_resolved(incident, templates=templates)
    try:
        await asyncio.wait_for(
            failure_alert.deliver_alert(
                subject,
                "The source has been quiet long enough to call this over.",
                rows,
                purpose=failure_alert.SECURITY,
                slack_summary=summary,
            ),
            timeout=_DELIVERY_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 - the alerter must never raise
        log.error(
            "login_abuse: could not deliver the resolved report for %s",
            incident.get("ip_address"),
        )


async def note_success() -> None:
    """Report any campaign that has gone quiet. Best-effort; never raises.

    Called on a SAMPLED successful response, from the same hook the outage
    recovery uses (``app/core/failure_monitor.py``). This is the ONLY thing that
    can notice an attack ending: every other entry point in this module runs on a
    failed sign-in, and an attacker who gives up produces no more of those.

    Opens its own session -- there is no request session to borrow on that path --
    exactly like ``failure_alert.note_success``.
    """
    if not failure_alert.alerting_enabled():
        return
    if database.SessionLocal is None:
        return
    try:
        async with database.SessionLocal() as session:
            await sweep_quiet(session)
    except Exception:  # noqa: BLE001 - monitoring must never break a request
        log.warning("login_abuse: quiet sweep could not open a session")



async def sweep_quiet(session: AsyncSession) -> None:
    """Close every campaign that has gone quiet and report each one. Never raises.

    ⚠️ WITHOUT THIS, A CAMPAIGN THAT SIMPLY STOPS IS NEVER REPORTED AS OVER.
    ``evaluate`` runs on a failed sign-in FROM a source, so it can only ever
    notice a campaign ending if that same source comes BACK an hour later -- and
    an attacker who gives up is exactly the case where they do not. The end of an
    attack is the absence of an event, and absence needs something else to notice
    it.

    That something is the SAMPLED SUCCESS PATH, the same hook the outage
    recovery uses (``failure_alert.note_success``): real traffic is denser than
    any cron this project can run, and the query is one indexed UPDATE that
    almost always matches nothing. It is called only when there is an open
    incident to find -- see the caller in ``app/core/failure_monitor.py``.

    Commits before sending, like every other claim here: the claim must be
    durable before a message goes out, never after.
    """
    if not failure_alert.alerting_enabled():
        return
    try:
        closed = (
            (
                await asyncio.wait_for(
                    session.execute(
                        _SQL_CLOSE_ALL_QUIET,
                        {
                            "environment": get_settings().environment,
                            "quiet_seconds": _INCIDENT_QUIET_SECONDS,
                        },
                    ),
                    timeout=_DB_TIMEOUT_SECONDS,
                )
            )
            .mappings()
            .all()
        )
        await session.commit()
    except Exception:  # noqa: BLE001 - monitoring must never break a request
        log.warning("login_abuse: quiet sweep failed", exc_info=True)
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return
    if not closed:
        # THE COMMON CASE, and the reason the template read is where it is: this
        # sweep runs on the sampled success path and almost always closes
        # nothing, so almost always costs no template read at all.
        return
    templates = await alert_templates.load()
    for incident in closed:
        await _send_resolved(dict(incident), templates=templates)

# --------------------------------------------------------------- entry point ---

_INTRO = (
    "Sustained failed sign-ins from a single source. No sign-in succeeded — this "
    "reports attempts, not a breach. You get ONE message per source per campaign, "
    "however many more attempts follow."
)


async def observe_failure(
    session: AsyncSession,
    *,
    ip_address: str | None,
    city: str | None = None,
    region: str | None = None,
    country: str | None = None,
) -> None:
    """Called once per failed login attempt. Throttled, best-effort, never raises.

    ⚠️ ``ip_address`` is the value the FRONTEND forwarded from the incoming
    request's ``x-forwarded-for``, i.e. the same client-supplied field that is
    already stored on the ``login_failures`` row and shown in the engineer
    console. It is the only per-attacker identifier this data has — the limiter's
    server-derived key is the frontend function's own egress address, which every
    real login in the organisation also shares (see the topology caveat in
    ``app/core/rate_limit.py``), so grouping by it would put the whole
    organisation in one bucket and detect nothing.

    The consequence, stated plainly: someone calling this API directly can put any
    address in that field, so the IP an alert names is a LEAD, not a verdict — it
    can be forged to make an innocent address look guilty, or rotated per request
    to evade detection entirely. It costs an attacker nothing to do either. What
    it buys them is silence, not access: none of this grants a login, and the
    per-email cooldown and hard lock are unaffected because they key on the email.
    Verify against the edge's own logs before blocking anything.

    ⚠️ SINCE #457 THIS FIELD CAN GET SOMEONE REFUSED, not merely reported, which
    raises the stakes on the paragraph above considerably. Putting an innocent
    address here no longer just makes it look guilty in a Slack message — it
    would refuse that address's sign-ins for an hour, which is a denial of
    service an attacker gets for the price of one header. The answer is NOT to
    stop using the field (there is no other per-attacker identifier in this data);
    it is that ``login_block`` refuses to block any address with a recent
    successful sign-in, or any address an engineer has ever signed in from, and
    those refusals are `NOT EXISTS` clauses inside the INSERT rather than checks
    anyone could forget. Read that module before touching this argument.
    """
    global _last_eval_at
    if not (failure_alert.alerting_enabled() or login_block.blocking_enabled()):
        # Nothing to send a message to AND nothing to enforce: not one query, not
        # one comparison beyond this line. Note the OR (#457) — blocking defaults
        # ON, so an unset webhook alone no longer disables the whole path. A
        # deployment that wants the old "touch nothing" behaviour has to say so
        # with LOGIN_AUTO_BLOCK_ENABLED=false, because a security control that is
        # off until someone remembers an env var is off.
        return
    ip = (ip_address or "").strip()
    if not ip:
        # Nothing to attribute the attempt to. Grouping every unattributed failure
        # together would make one bucket of "unknown" that alerts on the aggregate
        # of unrelated people — worse than not looking.
        return

    now = time.monotonic()
    if _last_eval_at is not None and now - _last_eval_at < _EVAL_INTERVAL_SECONDS:
        return
    # Stamp BEFORE awaiting, so concurrent failures in this process do not all
    # slip through the gate while the first evaluation is in flight.
    _last_eval_at = now

    # Report any campaign that has STOPPED before measuring the one happening
    # now. It runs here rather than inside `evaluate` for one reason: the close
    # has to be committed and reported by the transaction that WINS it, and
    # `evaluate` returns the row it won the ALERT claim for, which is a different
    # race. Doing it first also keeps campaign identity intact -- the upsert below
    # then opens a NEW row for a source that has come back, instead of bumping the
    # one just declared over.
    await sweep_quiet(session)

    try:
        incident = await asyncio.wait_for(
            evaluate(
                session,
                ip_address=ip[:64],
                city=city,
                region=region,
                country=country,
            ),
            timeout=_DB_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 - detection must never break a login response
        log.warning("login_abuse: evaluation failed", exc_info=True)
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return

    if incident is None:
        return

    subject, rows = render_alert(incident)
    # THE TEMPLATE READ, and the only one on this path. It happens here — after
    # `evaluate` returned a CLAIMED incident, i.e. once per campaign — and not
    # inside the renderer, so that a successful login, a failed login that is
    # under threshold, and every later attempt in the same campaign all cost
    # nothing. The one request that does pay is the one already about to spend
    # seconds on an outbound POST to Slack. Never raises; `{}` means "use the
    # built-in wording".
    templates = await alert_templates.load()
    try:
        await asyncio.wait_for(
            # SECURITY, not operational: this routes to #security-alerts
            # (SLACK_SECURITY_WEBHOOK_URL) and tags the message `SECURITY` so it
            # is told apart at a glance from an outage — including in the fallback
            # case where no security webhook is set and both kinds share
            # #error-alerts. See app/services/failure_alert.py.
            failure_alert.deliver_alert(
                subject,
                _INTRO,
                rows,
                purpose=failure_alert.SECURITY,
                # Slack gets ONE line: who, from where, and whether they are
                # blocked. The mail keeps every row. See render_slack_summary.
                slack_summary=render_slack_summary(incident, templates=templates),
            ),
            timeout=_DELIVERY_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 - and neither may delivery
        # The claim is already committed, so this costs one missed message rather
        # than a message per attempt. Deliberately NOT retried and deliberately
        # NOT reported as a failure of its own: alerting about a failed alert is
        # the one way to build a loop here.
        log.error("login_abuse: could not deliver the alert for %s", incident.get("ip_address"))
