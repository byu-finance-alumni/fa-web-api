"""Job-posting digest recipient schemas (#567).

The engineer console's read and write shapes for who gets the 6pm e-mail about
job and internship links alumni submitted through the survey.

Addresses are shape-checked, lowercased and deduped here, at the edge, so a bad
list is a 422 before any query runs. There is no email-validator dependency in
this project (see ``app/schemas/support.py``), so the check is the same light
one used everywhere else: it rejects obvious non-addresses, and Resend is the
real authority on whether a mailbox exists.
"""

from __future__ import annotations

import datetime
import re

from pydantic import BaseModel, ConfigDict, field_validator

#: The most recipients the digest may have. Also a CHECK constraint in the
#: migration. Every address is one e-mail out of the survey's daily budget, so
#: this is a budget cap as much as a sanity one.
MAX_RECIPIENTS = 10

_EMAIL_RE = re.compile(r"^[^@\s,;<>\"]+@[^@\s,;<>\"]+\.[^@\s,;<>\"]+$")
_MAX_EMAIL_LEN = 254


def clean_recipients(values: object) -> list[str]:
    """Validate, lowercase and dedupe a recipient list, keeping first-seen order.

    Raises ``ValueError`` (a 422 through pydantic) on anything that is not a list
    of plausible addresses, or on more than :data:`MAX_RECIPIENTS` distinct ones.
    Commas, semicolons and angle brackets are refused inside an address: each
    entry is ONE mailbox, and "a@x.com, b@y.com" typed into one box must not
    quietly become two recipients the console never showed.
    """
    if not isinstance(values, list):
        raise ValueError("Must be a list of email addresses.")
    cleaned: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("Each recipient must be a string.")
        address = value.strip().lower()
        if any(ord(ch) < 32 for ch in address):
            raise ValueError("Must not contain control characters.")
        if len(address) > _MAX_EMAIL_LEN or not _EMAIL_RE.match(address):
            raise ValueError(f"Not a valid email address: {address[:80]!r}")
        if address not in cleaned:
            cleaned.append(address)
    if len(cleaned) > MAX_RECIPIENTS:
        raise ValueError(f"At most {MAX_RECIPIENTS} recipients.")
    return cleaned


class OpportunityLinkDigestState(BaseModel):
    """Engineer-console view of the digest setting.

    ``recipients`` empty means no digest: the per-posting alert to the engineer
    channel (#771) is what fires instead. ``email_configured`` says whether the
    API can send mail at all (Resend key and a From address); when it is false
    the digest cannot go out, so the per-posting alert stays on even with
    recipients set, and the console says so.

    ``reported_through`` is the digest's watermark: every posting submitted up to
    then has been reported. ``None`` until the first digest goes out.
    """

    recipients: list[str]
    email_configured: bool = False
    reported_through: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None
    updated_by_email: str | None = None


class OpportunityLinkDigestUpdate(BaseModel):
    """Replace the recipient list. ``extra="forbid"`` so a typo'd field is a 422
    rather than a silently ignored no-op. An empty list turns the digest off."""

    model_config = ConfigDict(extra="forbid")

    recipients: list[str]

    @field_validator("recipients", mode="before")
    @classmethod
    def _validate(cls, value: object) -> list[str]:
        return clean_recipients(value)
