"""Can we actually reach this alumnus by email — and at which address?

ONE definition, shared by every caller that needs it (#392). Before this module
the answer was written three times and three ways:

* the survey's eligibility SQL asked ``personal_email IS NOT NULL``,
* the survey's send loop asked ``_is_sendable_email(personal_email)`` in Python,
* the dashboard's ``missing_email`` KPI asked
  ``personal_email IS NOT NULL OR work_email IS NOT NULL``.

Three answers to one question is how the console reports a number that does not
match what actually sent — the recurring "the count and the send disagree" bug
class in this codebase. The SQL and Python forms here are deliberate mirrors of
each other: :func:`sendable_email_sql` is :func:`is_sendable_email` expressed as
a Postgres predicate, so a population counted in SQL and a population iterated in
Python are the same people.

Two DIFFERENT questions live here, and keeping them distinct is the point:

* **Has an address at all** (:func:`has_email_value_sql`) — a DATA-QUALITY
  question. Is the column populated? This is what the dashboard's
  ``missing_email`` KPI means.
* **Can we send to it** (:func:`sendable_email_sql`) — a DELIVERABILITY
  question. It adds a shape check and the reserved/placeholder-domain gate, so a
  typo'd or ``@example.com`` address counts as unreachable even though the column
  is populated.

The second is strictly stricter than the first, so
``unreachable >= missing_email`` always. They are reported separately rather than
merged: "we never collected an address" and "the address we hold is unusable" are
different jobs for whoever has to fix them.
"""

from __future__ import annotations

from sqlalchemy import and_, func, or_

# Reserved / placeholder domains that must never be emailed — Resend rejects the
# whole batch if any `to` uses one, and they're never real inboxes anyway. Dev's
# seed data is almost entirely @example.com, which is exactly why the gate has to
# be part of the COUNT and not only of the send loop: without it the console
# promises to email hundreds of addresses that the sender then silently skips.
UNSENDABLE_DOMAINS: frozenset[str] = frozenset(
    {"example.com", "example.org", "example.net", "test", "localhost", "invalid"}
)

# Sorted for a stable rendering of the generated SQL (keeps query plans and test
# assertions reproducible).
_UNSENDABLE_SORTED: tuple[str, ...] = tuple(sorted(UNSENDABLE_DOMAINS))

# Which address won, for display and for the audit trail.
SOURCE_PERSONAL = "personal"
SOURCE_WORK = "work"

# Why an alumnus cannot be reached — the drill-down list's per-row reason, so
# "no address on file" and "the address on file is unusable" stay separable by
# whoever has to act on them.
REASON_NO_EMAIL = "no_email"
REASON_UNUSABLE = "unusable_email"

REASON_LABELS: dict[str, str] = {
    REASON_NO_EMAIL: "No email address on file",
    REASON_UNUSABLE: "Email on file is not a usable address",
}


# ------------------------------------------------------------------ Python ----


def is_sendable_email(email: str | None) -> bool:
    """A minimal deliverability gate: a real-looking address, not a reserved or
    placeholder domain (e.g. the ``REPLACE_WITH_…@example.com`` test stand-ins).

    Deliberately permissive about the local part — this is a "could Resend
    plausibly accept this" check, not RFC 5322 validation. Being stricter here
    would start silently dropping real alumni, which is the failure this whole
    change exists to remove.

    NOTE: ``''`` and ``'   '`` are NOT sendable. The column is nullable but a
    blank string is a perfectly ordinary import artefact, and ``IS NOT NULL``
    happily passes it — which is how an alumnus with no address at all could be
    counted as reachable.
    """
    if not email or "@" not in email:
        return False
    local, _, domain = email.partition("@")
    domain = domain.strip().lower()
    if not local.strip() or "." not in domain:
        return False
    return domain not in UNSENDABLE_DOMAINS


def resolve_email(
    personal: str | None, work: str | None
) -> tuple[str | None, str | None]:
    """The recipient rule, in one place: ``(address, source)``.

    Personal email is PREFERRED; the work email is the FALLBACK used only when
    there is no usable personal one; ``(None, None)`` means unreachable (#392).

    Returns exactly ONE address, never both — which is what makes double-sending
    structurally impossible rather than something a caller has to remember not to
    do. An alumnus with two good addresses still yields one recipient and
    therefore one email.

    "Usable" is :func:`is_sendable_email`, not "not NULL", so a blank or
    malformed work email does not shadow a missing personal one and get treated
    as reachable.
    """
    if is_sendable_email(personal):
        return personal, SOURCE_PERSONAL
    if is_sendable_email(work):
        return work, SOURCE_WORK
    return None, None


def preferred_display_email(personal: str | None, work: str | None) -> str | None:
    """The address to SHOW for an alumnus — personal preferred, work as fallback.

    Same preference order as :func:`resolve_email` but WITHOUT the deliverability
    gate, because displaying is not sending. A call sheet exists so a human can
    try the address; blanking out an ``@example.com`` or slightly malformed one
    would hide from staff the very value they need to see in order to fix it.

    Use :func:`resolve_email` to decide who actually gets emailed;
    use this to render what we hold.
    """
    for value in (personal, work):
        if value and value.strip():
            return value
    return None


def unreachable_reason(personal: str | None, work: str | None) -> str:
    """Why :func:`resolve_email` came back empty — one of :data:`REASON_LABELS`.

    Only meaningful once the address is known to be unresolvable. "There is
    something in a column but we can't send to it" is a typo someone can fix
    today; "both columns are empty" needs the address chased from scratch.
    """
    has_any = bool((personal or "").strip() or (work or "").strip())
    return REASON_UNUSABLE if has_any else REASON_NO_EMAIL


# --------------------------------------------------------------------- SQL ----


def _nonblank(col):
    """The column holds something other than NULL / '' / whitespace."""
    return and_(col.is_not(None), func.btrim(col) != "")


def sendable_email_sql(col):
    """:func:`is_sendable_email` as a Postgres predicate over *col*.

    Mirrors the Python version clause for clause, INCLUDING the ``partition``
    semantics: the local part is everything before the FIRST ``@`` and the domain
    is everything after it (``substr`` from ``strpos``, not ``split_part``, which
    would truncate at a second ``@``).

    Everything is expressed in SQL so eligibility can be evaluated over the whole
    8,000+ row table by Postgres — the population is never assembled in Python
    and then filtered, which is precisely how the count and the send drifted
    apart before.
    """
    domain = func.lower(func.btrim(func.substr(col, func.strpos(col, "@") + 1)))
    return and_(
        _nonblank(col),
        func.strpos(col, "@") > 0,
        func.btrim(func.split_part(col, "@", 1)) != "",
        func.strpos(domain, ".") > 0,
        domain.notin_(_UNSENDABLE_SORTED),
    )


def has_email_value_sql(personal_col, work_col):
    """Either column holds a NON-BLANK value — the data-quality question.

    Used by the dashboard's ``missing_email`` KPI and the alumni list's
    ``missing_email`` filter. Intentionally does NOT apply the deliverability
    gate: a real-looking-but-undeliverable address is still an address on file,
    and folding the two together would make a data-quality KPI swing on Resend's
    domain rules.

    It DOES reject blank strings, which ``IS NOT NULL`` did not — an alumnus
    whose email column was imported as ``''`` was being counted as having one.
    """
    return or_(_nonblank(personal_col), _nonblank(work_col))


def reachable_email_sql(personal_col, work_col):
    """Either column holds an address we could actually SEND to (#392).

    This is the survey's recipient test. Personal-vs-work preference does not
    matter here — for "is this person reachable at all", either one qualifying is
    enough; :func:`resolve_email` decides WHICH address is used.
    """
    return or_(sendable_email_sql(personal_col), sendable_email_sql(work_col))
