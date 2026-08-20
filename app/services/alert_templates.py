"""Owner-editable wording for the Slack alerts.

Every sentence this app sends to Slack used to be a string literal in
``app/services/failure_alert.py`` and ``app/services/login_abuse.py``. They are
the first thing the owner reads when something is wrong, and changing a word of
them cost a code change, a review and two Vercel builds. This module makes the
WORDING data — editable from the engineer Maintenance page — and leaves the FACTS
where they were.

--------------------------------------------------------------------------------
THE SHAPE: A TEMPLATE IS AN OVERRIDE, NEVER THE SOURCE OF TRUTH
--------------------------------------------------------------------------------
Each :data:`KINDS` entry carries a ``default`` compiled into this file. A row in
``alert_message_templates`` REPLACES that default for one kind; deleting the row
restores it. An empty table, an unreadable table, a database that never had the
migration applied, no database at all — every one of those means "use the
default", and the alerts then say exactly what they said before this feature
existed.

That direction is the whole safety argument. A feature that lets someone edit an
alert must not be able to stop one being sent, so nothing on the alerting path
may depend on this table being present, readable, or sensible.

--------------------------------------------------------------------------------
RENDERING IS TOTAL: IT CANNOT RAISE AND CANNOT HALF-RENDER
--------------------------------------------------------------------------------
:func:`render` is called from the alerting path — a path whose entire contract
(see the amplification rule in ``failure_alert``) is that it never raises, on a
request that is usually already failing. So it does not raise. Ever. Concretely:

  * substitution is a SINGLE EXPLICIT SCAN (:func:`_substitute`) over
    ``{name}`` tokens, resolving each against a plain dict. It is deliberately
    NOT ``str.format``: ``"{x.__class__.__mro__}".format(x=incident)`` walks
    attributes out of a database row, and ``"{0.__globals__}"`` on a bound
    method reaches module state — a stored string is untrusted input and
    ``str.format`` is a small expression language. It is likewise not an
    f-string and not ``eval``, which would be arbitrary code from a table row.
  * a stored body is RE-VALIDATED at render time, not merely at write time, so a
    row inserted by hand in psql — or by a future endpoint that forgets — still
    cannot produce a broken message.
  * an unknown placeholder, a stray brace, an over-long body, a control
    character, a missing value, or a body that renders to nothing => the stored
    template is DISCARDED WHOLE and the built-in default is rendered instead. A
    partial substitution is never emitted: the reader must never have to work out
    whether ``{ip}`` in a Slack message is the attacker's address or a typo.
  * if even the default cannot render (which requires the CALLER to pass a
    broken value dict, and is covered by a test that says it cannot happen), the
    result is the kind's ``last_resort`` — a fixed sentence containing no
    placeholders at all.

Every one of those falls back QUIETLY to the reader and LOUDLY to the log: the
alert still goes out, and the platform log says which template was unusable.

--------------------------------------------------------------------------------
A TEMPLATE CANNOT INTRODUCE DATA THE RENDERER DOES NOT ALREADY EXPOSE
--------------------------------------------------------------------------------
This is the non-negotiable one, and it is structural rather than a convention.

A template is not a query language. It can name a placeholder, nothing else, and
:func:`render` resolves names against a dict that the RENDERER built and that
this module has already restricted to the names the kind declares. There is no
attribute access, no indexing, no call syntax, no fall-through to a larger scope.
So the set of facts a template can reach is exactly :data:`KINDS`\\ ``[k].placeholders``
— a hand-written list — and widening it takes a code change and a review.

⚠️ IN PARTICULAR, THE ATTEMPTED EMAIL ADDRESSES ARE UNREACHABLE, AND NO
PLACEHOLDER FOR THEM MAY EVER BE ADDED. They are unverified strings a stranger
typed into a login form, some of them belong to real people, they are the
scraped-and-guessed material the attacker was probing with, and a list of them in
a Slack channel is an enumeration oracle for everyone who can read the channel.
``{addresses}`` is the COUNT (``distinct_email_count``) and is named in the
plural precisely because that is the number a reader acts on. See the PII notes
in ``app/services/login_abuse.py`` and in the two migrations.
:data:`_FORBIDDEN_PLACEHOLDER_TERMS` and the tests in
``tests/test_alert_templates.py`` are the tripwire on that rule.

--------------------------------------------------------------------------------
WHERE THE TEMPLATE IS READ FROM, AND WHY THAT IS OFF THE HOT PATH
--------------------------------------------------------------------------------
:func:`load` is called ONLY on the alerting path — the few lines that run after a
message has already been claimed and immediately before an outbound HTTP POST to
Slack. It is never called on a successful login, never on the login pre-check,
never in a middleware, and never on any authenticated route.

That placement, and not the cache, is what keeps this off the hot path, and it is
the deliberate choice the issue asked to see argued:

  * a real login costs ZERO reads of this table. ``login_abuse.observe_failure``
    already does nothing at all unless the sign-in FAILED, and even then the
    template is read only by the one request in a whole campaign that wins the
    alert claim — the same request that is already paying for a multi-second POST
    to Slack. One small indexed SELECT next to that is free.
  * the outage path is the same shape: ``failure_alert`` reads a template only
    when it has just claimed the opening or the recovery message.

The process-local TTL cache on top (:data:`_CACHE_TTL_SECONDS`, the same
mechanism and the same reasoning as ``maintenance.read_status``) is therefore an
optimisation, not the safety property. It earns its keep in one place:
``login_abuse.sweep_quiet`` can close several campaigns in one pass and send a
report for each, and without the cache that would be one read per message.
Editing a template publishes the new wording to the editing instance
immediately and to every other instance within the TTL.

⚠️ :func:`load` OPENS ITS OWN SESSION rather than borrowing the caller's. Two
reasons, both deliberate: the outage sender has no session to borrow (it has
already closed the one it used, on purpose, so that no connection is held across
a third-party HTTP call), and on the login path a failed read must not be able to
poison the transaction the request is still using. A session of its own makes the
worst case "no overrides today" instead of "the login route's transaction is
aborted".

--------------------------------------------------------------------------------
WHAT IS DELIBERATELY *NOT* TEMPLATED
--------------------------------------------------------------------------------
The DEGRADED outage alert (``failure_alert._degraded_alert``) keeps its hard-coded
wording. It is the message sent when the database could not be reached — reading
a template there would be asking the broken thing to describe its own breakage,
and the fallback would fire every single time. The email bodies' label/value rows
are likewise untouched: they are a forensic artefact with a stable shape, not
prose, and the owner asked to edit the SLACK lines.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database
from app.core.errors import InvalidRequestError
from app.schemas.alumni import _has_control_chars

log = logging.getLogger(__name__)


# ------------------------------------------------------------------- limits --

#: Longest body the editor will store. Slack rejects an oversized payload with a
#: 400 and the whole message is lost, so this is the difference between a wordy
#: alert and no alert. 500 characters is several sentences — far past anything
#: worth reading on a phone at 2am, which is the audience.
#:
#: Mirrored as ``ck_alert_templates_length`` in the migration, because the API is
#: not the only thing that can write a row.
MAX_BODY_CHARS = 500

#: How long a read of the templates is reused within one process. Serverless
#: instances each hold their own, so an edit takes effect everywhere within this
#: many seconds. Longer than ``maintenance``'s five seconds because nothing here
#: gates access — the cost of staleness is one alert in the old wording, not a
#: locked-out user.
_CACHE_TTL_SECONDS = 30.0

#: Time budget for the template read. It sits immediately before an outbound
#: Slack POST on a path that must never hang, so it is short and it fails to
#: "no overrides" rather than delaying the message.
_DB_TIMEOUT_SECONDS = 2.0


# -------------------------------------------------------------- the vocabulary --


@dataclass(frozen=True)
class Placeholder:
    """One name a template may write in braces, and what it expands to.

    ``example`` is shown in the console next to the name so the owner can see
    the shape of the value without triggering a real alert.
    """

    name: str
    description: str
    example: str


@dataclass(frozen=True)
class Kind:
    """One editable message.

    ``default`` is the wording shipped in the code and is what renders when there
    is no stored row (or the stored row is unusable). ``last_resort`` is the
    placeholder-free sentence used if even the default cannot be rendered — see
    the totality argument in the module docstring.
    """

    key: str
    label: str
    description: str
    default: str
    last_resort: str
    placeholders: tuple[Placeholder, ...]

    @property
    def placeholder_names(self) -> frozenset[str]:
        return frozenset(p.name for p in self.placeholders)


# The four keys, as constants, so a call site cannot typo one into a silent
# fallback.
SECURITY_ATTACK_OPENING = "security_attack_opening"
SECURITY_ATTACK_RESOLVED = "security_attack_resolved"
OUTAGE_OPENING = "outage_opening"
OUTAGE_RECOVERED = "outage_recovered"


# --- the shared placeholders -------------------------------------------------
#
# ⚠️ ``{location_phrase}`` AND ``{location_parenthetical}` CARRY THEIR OWN LEADING
# SPACE AND EXPAND TO NOTHING WHEN THE GEOLOCATION IS UNKNOWN. That looks odd
# next to the plain ``{location}`` until you write the sentence out: the wording
# shipped today is "attacked by 1.2.3.4 from Seattle WA." when the edge gave us a
# location and "attacked by 1.2.3.4." when it did not, and a template using a
# bare ``{location}`` would have to say "from unknown". Both forms are offered:
# the conditional one, which is what the defaults use so the sentence reads
# correctly either way, and the plain one for an author who would rather spell
# the sentence out themselves.
_IP = Placeholder("ip", "The source IP address the attempts came from.", "159.26.103.94")
_LOCATION = Placeholder(
    "location",
    "Approximate geolocation of that IP, or the word 'unknown'. Never empty.",
    "Seattle, WA, United States",
)
_LOCATION_PHRASE = Placeholder(
    "location_phrase",
    "' from <place>' when the location is known, and nothing at all when it is "
    "not. Includes its own leading space, so it is written straight after {ip}.",
    " from Seattle, WA, United States",
)
_LOCATION_PARENTHETICAL = Placeholder(
    "location_parenthetical",
    "' (<place>)' when the location is known, and nothing at all when it is not. "
    "Includes its own leading space.",
    " (Seattle, WA, United States)",
)
_ATTEMPTS = Placeholder(
    "attempts", "How many failed sign-ins this source made.", "338"
)
_ADDRESSES = Placeholder(
    "addresses",
    "HOW MANY distinct addresses were attempted — a count, never the addresses "
    "themselves. There is no placeholder for the addresses and there never will "
    "be: they are unverified input, some belong to real people, and a list of "
    "them in a channel is an enumeration oracle.",
    "78",
)
_DURATION = Placeholder(
    "duration", "How long the campaign ran, as a readable span.", "6m 12s"
)
_PATTERN = Placeholder(
    "pattern",
    "The shape of the campaign, in the detector's own words.",
    "spraying: many addresses, a few passwords each",
)
_ACTION = Placeholder(
    "action",
    "One clause saying what the app DID about the source: blocked it, "
    "deliberately did not (the address is exempt), or blocking is switched off.",
    "It is blocked and cannot sign in.",
)
_ENVIRONMENT = Placeholder(
    "environment", "Which deployment this is about.", "production"
)
_STARTED = Placeholder("started", "When the incident began, in UTC.", "2026-08-20 14:02:11 UTC")
_FAILURES = Placeholder(
    "failures",
    "How many failing requests were reported. A floor on the real number, not a "
    "total — each instance throttles its own reporting.",
    "17",
)
_ROUTE = Placeholder(
    "route",
    "The most recent failing route, as a TEMPLATE — never a real path, never a "
    "query string, never an id.",
    "/alumni/{alumni_id}",
)
_STATUS_CODE = Placeholder("status_code", "The HTTP status being returned.", "500")
_ERROR_KIND = Placeholder(
    "error_kind",
    "The exception CLASS NAME, or 'http_500'. Never an exception message, which "
    "can quote a row.",
    "ProgrammingError",
)


KINDS: dict[str, Kind] = {
    SECURITY_ATTACK_OPENING: Kind(
        key=SECURITY_ATTACK_OPENING,
        label="Attack detected (Slack)",
        description=(
            "The single line posted to the security channel the moment sustained "
            "failed sign-ins from one source cross the threshold. It is read on a "
            "phone, at a glance, and it answers one question: are we being "
            "attacked, by whom, from where, and is it already handled. The full "
            "detail goes to the alert mailbox either way."
        ),
        default="You are being attacked by {ip}{location_phrase}. {action}",
        last_resort=(
            "Sustained failed sign-ins from one source were detected. See the "
            "alert email for the detail."
        ),
        placeholders=(
            _IP,
            _LOCATION,
            _LOCATION_PHRASE,
            _LOCATION_PARENTHETICAL,
            _ATTEMPTS,
            _ADDRESSES,
            _DURATION,
            _PATTERN,
            _ACTION,
            _ENVIRONMENT,
        ),
    ),
    SECURITY_ATTACK_RESOLVED: Kind(
        key=SECURITY_ATTACK_RESOLVED,
        label="Attack stopped (Slack)",
        description=(
            "The all-clear, posted once a source has been quiet long enough to "
            "call the campaign over. Sent only for a campaign that was announced "
            "— there is nothing to close for a reader who was never told it "
            "opened."
        ),
        default=(
            "The attack from {ip}{location_parenthetical} has stopped. "
            "{attempts} attempts across {addresses} addresses over {duration}. "
            "Nothing got in."
        ),
        last_resort="A login-abuse campaign has stopped. Nothing got in.",
        placeholders=(
            _IP,
            _LOCATION,
            _LOCATION_PHRASE,
            _LOCATION_PARENTHETICAL,
            _ATTEMPTS,
            _ADDRESSES,
            _DURATION,
            _PATTERN,
            _ENVIRONMENT,
        ),
    ),
    OUTAGE_OPENING: Kind(
        key=OUTAGE_OPENING,
        label="API failing (Slack and email)",
        description=(
            "The opening line of the outage alert — the one that fires when the "
            "API has been failing long enough to be an incident rather than a "
            "blip. Unlike the two security lines this one also heads the alert "
            "email, above the label/value rows, which are not editable."
        ),
        default=(
            "The API has been failing for long enough to be an incident. "
            "You will get one more email when it clears."
        ),
        last_resort="The API is failing.",
        placeholders=(
            _ENVIRONMENT,
            _STARTED,
            _FAILURES,
            _ROUTE,
            _STATUS_CODE,
            _ERROR_KIND,
        ),
    ),
    OUTAGE_RECOVERED: Kind(
        key=OUTAGE_RECOVERED,
        label="API recovered (Slack and email)",
        description=(
            "The line that clears an outage. Sent only for an incident that "
            "actually paged someone."
        ),
        default="The API is serving requests again. This incident is closed.",
        last_resort="The API is serving requests again.",
        placeholders=(
            _ENVIRONMENT,
            _STARTED,
            _DURATION,
            _FAILURES,
            _ROUTE,
        ),
    ),
}


#: Substrings that must never appear in a placeholder NAME. This is a tripwire,
#: not the mechanism — the mechanism is that :data:`KINDS` is a hand-written list
#: and :func:`render` resolves against nothing else. It exists so that a future
#: edit adding ``{emails}`` or ``{attempted_accounts}`` in a hurry goes red in CI
#: with the reason attached, rather than quietly republishing a stranger's guess
#: at somebody's address into a Slack channel. ``{addresses}`` is allowed
#: BECAUSE it is a count; there is a test asserting exactly that.
_FORBIDDEN_PLACEHOLDER_TERMS = ("email", "mail", "account", "username", "netid", "user")

#: A ``{name}``. Lowercase, ASCII, no dots, no indexing, no calls, no format
#: spec — the grammar is deliberately this small so that "what can a template
#: reach" has a one-line answer.
_TOKEN = re.compile(r"\{([a-z][a-z0-9_]*)\}")


class TemplateError(Exception):
    """A body that cannot be rendered. Never escapes this module."""


# ------------------------------------------------------------- substitution --


def _substitute(body: str, values: dict[str, str]) -> str:
    """Replace every ``{name}`` in ``body`` from ``values``. All or nothing.

    Raises :class:`TemplateError` — and produces NO output — if a token names
    something ``values`` does not have, or if a brace survives anywhere in the
    LITERAL text (``"{ip"``, ``"}"``, ``"{Ip}"``, ``"{x.y}"``). The brace check
    runs over the literal segments only, so a substituted VALUE containing a
    brace is data and is never rescanned: substitution happens exactly once and
    cannot recurse.

    That last part is not hypothetical — ``{route}`` expands to a route TEMPLATE
    (``/alumni/{alumni_id}``), which is the correct thing to print and is exactly
    what the alert email's rows already show. Rescanning it would either try to
    resolve ``{alumni_id}`` or reject the whole message.
    """
    out: list[str] = []
    literals: list[str] = []
    cursor = 0
    for match in _TOKEN.finditer(body):
        literal = body[cursor : match.start()]
        literals.append(literal)
        out.append(literal)
        name = match.group(1)
        if name not in values:
            raise TemplateError(f"unknown placeholder {{{name}}}")
        out.append(str(values[name]))
        cursor = match.end()
    tail = body[cursor:]
    literals.append(tail)
    out.append(tail)

    for literal in literals:
        if "{" in literal or "}" in literal:
            raise TemplateError("a brace is not part of a known placeholder")
    return "".join(out)


def validate_body(kind_key: str, body: str) -> str:
    """Check one body against every rule, and return it trimmed.

    Raises :class:`InvalidRequestError` (422, with a message safe to show the
    engineer who typed it) — this is the WRITE-time gate, and it is called again
    at RENDER time so that a row which arrived some other way is held to the same
    rules.

    The rules, and why each one:

      * the kind must be one this code renders, or the row is decoration;
      * non-empty after trimming, and at most :data:`MAX_BODY_CHARS`, because
        Slack answers an oversized payload with a 400 and the alert is lost;
      * no control or invisible characters — these messages are one sentence, and
        a newline or a zero-width character in one is either a slip or an attempt
        to fake structure in a channel (the same helper the alumni name/email
        gates use, so the app has one definition of "invisible");
      * every ``{token}`` must be a placeholder THIS KIND declares, so a body
        cannot name a fact its renderer does not compute;
      * and it must actually render against the kind's own examples, which
        catches a stray brace before it can cost a real alert its wording.
    """
    kind = KINDS.get(kind_key)
    if kind is None:
        raise InvalidRequestError(f"Unknown alert message: {kind_key}.")

    trimmed = (body or "").strip()
    if not trimmed:
        raise InvalidRequestError("The message cannot be empty.")
    if len(trimmed) > MAX_BODY_CHARS:
        raise InvalidRequestError(
            f"The message is too long ({len(trimmed)} characters); "
            f"the limit is {MAX_BODY_CHARS}."
        )
    if _has_control_chars(trimmed):
        raise InvalidRequestError(
            "The message contains a control or invisible character. "
            "It must be a single line of ordinary text."
        )

    allowed = kind.placeholder_names
    unknown = sorted({m.group(1) for m in _TOKEN.finditer(trimmed)} - allowed)
    if unknown:
        raise InvalidRequestError(
            "Unknown placeholder(s) "
            + ", ".join("{" + name + "}" for name in unknown)
            + ". Available here: "
            + ", ".join("{" + name + "}" for name in sorted(allowed))
            + "."
        )

    try:
        rendered = _substitute(trimmed, {p.name: p.example for p in kind.placeholders})
    except TemplateError as exc:
        raise InvalidRequestError(
            f"The message could not be rendered ({exc}). Placeholders look like "
            "{ip} — a lone '{' or '}' is not allowed."
        ) from None
    if not rendered.strip():
        raise InvalidRequestError("The message would render as nothing.")
    return trimmed


def preview(kind_key: str, body: str | None = None) -> str:
    """What ``body`` (or the kind's current default) looks like with example values.

    For the console's live preview, so the owner can see the sentence before an
    attack proves it wrong. Total, exactly like :func:`render`.
    """
    kind = KINDS.get(kind_key)
    if kind is None:
        return ""
    values = {p.name: p.example for p in kind.placeholders}
    return render(kind_key, values, templates={kind_key: body} if body else None)


def render(
    kind_key: str,
    values: dict[str, object],
    *,
    templates: dict[str, str] | None = None,
) -> str:
    """The stored wording for ``kind_key``, or the built-in default. NEVER RAISES.

    ``values`` is what the RENDERER computed. It is filtered down to the names
    this kind declares before anything is substituted, so a caller that passes
    extra keys — now or after some future refactor widens an incident dict —
    cannot make them reachable from a template. That filter is the enforcement
    point for "a template cannot introduce data the renderer does not already
    expose"; see the module docstring.

    ``templates`` is the mapping :func:`load` returns. Passing ``None`` (the
    default) means "no overrides", which is what every unit test and every
    fallback path gets.

    Falls back to the built-in default, whole, on ANY problem with the stored
    body, and logs which kind was unusable. It never emits a partly-substituted
    string: a reader must never have to decide whether ``{ip}`` in a channel is
    an address or a bug.
    """
    try:
        return _render(kind_key, values, templates)
    except Exception:  # noqa: BLE001 - the alerting path must never raise
        log.error("alert_templates: %s could not be rendered at all", kind_key)
        kind = KINDS.get(kind_key)
        return kind.last_resort if kind is not None else "An alert was raised."


def _render(
    kind_key: str, values: dict[str, object], templates: dict[str, str] | None
) -> str:
    kind = KINDS[kind_key]
    safe = {
        name: str(values[name])
        for name in kind.placeholder_names
        if name in values and values[name] is not None
    }

    stored = (templates or {}).get(kind_key)
    if stored:
        try:
            # Re-validated HERE and not only at write time: a row can arrive by
            # psql, by a restored backup, or from a future endpoint that forgets.
            body = validate_body(kind_key, stored)
            rendered = _substitute(body, safe)
            if rendered.strip():
                return rendered
            raise TemplateError("renders as nothing")
        except Exception:  # noqa: BLE001 - a bad template must not cost the alert
            log.error(
                "alert_templates: the stored %s template is unusable; "
                "sending the built-in default instead",
                kind_key,
            )

    try:
        return _substitute(kind.default, safe)
    except Exception:  # noqa: BLE001
        # Only reachable if the CALLER's value dict is missing something the
        # default names — i.e. a bug in the renderer, not in anyone's wording.
        # There is a test asserting every default renders from every real caller.
        log.error(
            "alert_templates: the built-in %s default could not render from the "
            "values supplied; falling back to fixed wording",
            kind_key,
        )
        return kind.last_resort


# ----------------------------------------------------------------- statements --
#
# Raw statements in the style of the neighbouring alerting services
# (``failure_alert``, ``login_abuse``, ``login_block``): the write is an
# idempotent upsert on ``uq_alert_templates_key``, which is what makes "save"
# update the one row for a kind rather than adding a second.
#
# ⚠️ NO ``:name::type`` CASTS ANYWHERE IN THIS MODULE. SQLAlchemy's ``text()``
# does not bind a placeholder followed by a colon, so a Postgres-style cast
# swallows the parameter, the literal text reaches Postgres, and the statement is
# a syntax error against a real database while every faked unit test passes.
# Write ``CAST(:name AS type)`` if one is ever needed. The guard test in
# ``tests/test_login_auto_block.py`` ("every placeholder written is a
# placeholder SQLAlchemy bound") is parametrised over this module for exactly
# that reason.

_SQL_LOAD = text(
    """
    SELECT template_key, body
      FROM alert_message_templates
    """
)

_SQL_LIST = text(
    """
    SELECT template_key, body, updated_at, updated_by_user_id
      FROM alert_message_templates
     ORDER BY template_key
    """
)

_SQL_UPSERT = text(
    """
    INSERT INTO alert_message_templates (template_key, body, updated_by_user_id)
    VALUES (:template_key, :body, :actor_id)
    ON CONFLICT (template_key) DO UPDATE
       SET body               = EXCLUDED.body,
           updated_by_user_id = EXCLUDED.updated_by_user_id,
           updated_at         = now()
    RETURNING template_key, body, updated_at, updated_by_user_id
    """
)

_SQL_DELETE = text(
    """
    DELETE FROM alert_message_templates
     WHERE template_key = :template_key
    RETURNING template_key
    """
)


# ------------------------------------------------------------------- reading --

# (monotonic timestamp, mapping) or None. Module-level by design, exactly like
# ``maintenance``'s: it is a pure read-through cache of four short strings and is
# safe to lose at any moment.
_cached: tuple[float, dict[str, str]] | None = None


def reset_cache() -> None:
    """Drop the process-local cache. Used by the write endpoints and by tests."""
    global _cached
    _cached = None


def _remember(mapping: dict[str, str]) -> dict[str, str]:
    global _cached
    _cached = (time.monotonic(), mapping)
    return mapping


async def load() -> dict[str, str]:
    """The stored overrides, keyed by kind. NEVER RAISES; ``{}`` means "defaults".

    Called only from the alerting path, immediately before a delivery — see the
    placement argument in the module docstring for why that is off the hot path
    and why this opens a session of its own rather than borrowing the caller's.

    Fails to the LAST GOOD VALUE, or to ``{}`` if it has never read one. An
    unreadable table therefore costs the built-in wording and nothing else; it can
    never cost the message.
    """
    global _cached
    now = time.monotonic()
    if _cached is not None and now - _cached[0] < _CACHE_TTL_SECONDS:
        return _cached[1]
    if database.SessionLocal is None:
        return {} if _cached is None else _cached[1]
    try:
        async with database.SessionLocal() as session:
            rows = (
                (await asyncio.wait_for(
                    session.execute(_SQL_LOAD), timeout=_DB_TIMEOUT_SECONDS
                ))
                .mappings()
                .all()
            )
        return _remember(
            {
                str(row["template_key"]): str(row["body"])
                for row in rows
                if row["body"]
            }
        )
    except Exception:  # noqa: BLE001 - alerting must never depend on this read
        log.warning(
            "alert_templates: could not read the templates; using built-in wording"
        )
        return {} if _cached is None else _cached[1]


# ------------------------------------------------------- engineer console ---


async def list_all(session: AsyncSession) -> list[dict]:
    """Every editable message: its default, its current wording, and its state.

    UNCACHED, like ``maintenance.get_state`` — the console wants the truth, not a
    value that might be up to :data:`_CACHE_TTL_SECONDS` old, or the engineer
    saves an edit and appears to see it not take.

    A row whose ``template_key`` is not a kind this code renders is dropped: it is
    an override for a message that no longer exists, and showing it as editable
    would promise something nothing reads. Deliberately fail-safe in the other
    direction too — a kind with no row is listed with its default and
    ``customized=False``.
    """
    stored: dict[str, dict] = {}
    rows = (await session.execute(_SQL_LIST)).mappings().all()
    for row in rows:
        stored[str(row["template_key"])] = dict(row)

    items = []
    for key, kind in KINDS.items():
        row = stored.get(key)
        body = str(row["body"]) if row and row["body"] else kind.default
        items.append(
            {
                "key": key,
                "label": kind.label,
                "description": kind.description,
                "default_body": kind.default,
                "body": body,
                # "Customised" compares against the DEFAULT rather than asking
                # whether a row exists, because the migration SEEDS the defaults
                # so the table is self-describing — and a seeded row is not an
                # edit.
                "customized": body != kind.default,
                "updated_at": row["updated_at"] if row else None,
                "updated_by_user_id": row["updated_by_user_id"] if row else None,
                "preview": preview(key, body),
                "placeholders": [
                    {
                        "name": p.name,
                        "description": p.description,
                        "example": p.example,
                    }
                    for p in kind.placeholders
                ],
                "max_chars": MAX_BODY_CHARS,
            }
        )
    return items


async def set_body(
    session: AsyncSession, *, kind_key: str, body: str, actor_user_id: int
) -> dict:
    """Store one message's wording. Validates first; raises 422 if it will not do.

    Does NOT commit — the route commits alongside its audit row, so a stored
    template and the record of who stored it land together or not at all.
    """
    validated = validate_body(kind_key, body)
    row = (
        (
            await session.execute(
                _SQL_UPSERT,
                {
                    "template_key": kind_key,
                    "body": validated,
                    "actor_id": int(actor_user_id),
                },
            )
        )
        .mappings()
        .first()
    )
    # Publish to THIS process immediately; other instances pick the change up
    # within _CACHE_TTL_SECONDS. Same contract as maintenance mode's switch.
    reset_cache()
    return dict(row) if row is not None else {}


async def clear(session: AsyncSession, *, kind_key: str) -> bool:
    """Delete one override so the built-in default applies again.

    Returns False when there was nothing stored — the caller turns that into a
    404 so a double-click is a clean "already the default" rather than a silent
    success that implies something changed.

    Reset is the ESCAPE HATCH from a template that broke the wording, so it is
    validated against nothing and gated on nothing but the engineer role. Does not
    commit; the route does.
    """
    if kind_key not in KINDS:
        raise InvalidRequestError(f"Unknown alert message: {kind_key}.")
    row = (
        (await session.execute(_SQL_DELETE, {"template_key": kind_key}))
        .mappings()
        .first()
    )
    reset_cache()
    return row is not None
