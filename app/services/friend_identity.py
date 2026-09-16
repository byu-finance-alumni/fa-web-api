"""Who a friend-of-the-program row IS (#538): the dedupe rule for the friends
leg of the attendee wizard (``POST /events/{id}/attendees/match/friends``).

Friend records (``alumni.is_alumni = false``) carry no Net ID and no BYU ID, so
``create_alumni``'s exact-id duplicate blocker cannot see one. Before #538 the
only guard was a normalised name + employer key checked against the CURRENT
event's roster, so the same person attending two conferences became two friend
records. Jake's decision (2026-09-16), "email + visible friend id":

  * **Two friend rows are the same person when their email matches** --
    case-insensitive, trimmed, against ``alumni_contact_info.personal_email``
    OR ``work_email`` (the same two columns the attendee matcher's email tier
    reads) -- across ALL events, i.e. the whole table, not the event's roster.
    A hit is REUSED: the existing friend is attached to this event and the
    response says so (``status = "reused"`` + its ``friend_id``) instead of a
    second row being created. The existing record is NOT updated from the
    file (a new employer on the registration stays in the file); editing a
    friend is the profile's job.
  * **A row with no email falls back to the name + employer key**, now ALSO
    searched across every friend in the table rather than this event alone.
    Trade-off, accepted: someone who changes jobs between two events and
    registers without an email still duplicates, and two people with a common
    name at the same large employer still collide. Email is the fix for both;
    the fallback exists only for registrations that did not collect one.
  * **A friend row is never linked to an alumnus.** The reuse leg considers
    only ``is_alumni = false`` rows. If the email belongs to a real alumnus the
    row is NOT created as a friend at all: it comes back as
    ``status = "existing_alumnus"`` (``is_existing_alumnus = true`` with the
    alumnus's id) so the wizard can tell staff to match the row instead. The
    matcher's email tier normally catches this before the reviewer ever reaches
    "create a friend"; this is the belt for those braces.
  * Both lookups skip ``archived`` rows, the same pool the matcher proposes
    from and the approve route accepts. Re-creating an archived friend is
    therefore possible and intentional -- attaching an archived record to an
    event would resurrect it silently.
  * The per-event roster key (name + employer of everyone already attending,
    alumni included) is KEPT as an extra skip so a re-post of the same file is
    idempotent even when the file changed the email between runs.

``create_alumni``'s generic duplicate blocker is deliberately NOT widened: it
is shared with the main alumni import, where an email collision is a warning
at most. All of this runs in the friend path only.

Open follow-up -- "a friend turns out to be an alum" (NOT built here)
----------------------------------------------------------------------
There is no merge path yet. When a friend later matches a real alumnus (gets a
Net ID, or the alumnus record arrives via the registrar import), today the
outcome is two records and this module's alumnus guard stopping a THIRD. A
merge would have to carry, from the friend row onto the alumnus row:

  * every ``event_attendance`` row (re-point ``alumni_id``; the (event,
    alumni) uniqueness means an attendance both already have collapses to one,
    keeping the alumnus's notes);
  * CRM rows keyed on the friend: interactions, tasks, attachments, notes,
    tags / status labels, opportunity links, donations;
  * contact / employment / education detail the alumnus row lacks (fill blanks
    only -- registrar data wins on conflict);
  * the audit trail: an ``AuditLog`` "merged FRIEND-00042 into <alumni_id>"
    entry on BOTH records, then the friend row archived (never deleted -- the
    FERPA trail must survive);
  * the visible id: ``FRIEND-00042`` stops resolving once the row is archived,
    so anything that quoted it (a spreadsheet) needs the audit entry to find
    the survivor.

Until then, staff should match the row to the alumnus in the wizard and leave
the friend record to be archived by hand.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alumni import Alumni
from app.models.contact import AlumniContactInfo
from app.models.employment import CurrentEmployment
from app.models.event import EventAttendance
from app.services.attendee_match import (
    _norm_col,
    _norm_email,
    friend_identity_key,
    surname_keys,
)

# Bounds on the batched lookups so a hostile file cannot turn one request into
# a table scan's worth of rows in Python. A friends leg is capped at
# ``MAX_FRIENDS_PER_REQUEST`` rows (100) upstream, each with at most two
# emails, so these are generous.
_MAX_LOOKUP_ROWS = 2000


@dataclass(frozen=True)
class FriendDecision:
    """What the friends route should do with ONE chosen row."""

    #: ``create`` | ``reuse`` | ``existing_alumnus`` | ``on_roster``
    kind: str
    alumni_id: int | None = None
    #: Which key decided it -- ``email`` or ``name`` -- for the response message.
    matched_on: str | None = None


@dataclass
class FriendIndex:
    """Everything the friends route needs to decide every chosen row without a
    per-row query: built by :func:`build_friend_index` in at most THREE batched
    SELECTs, then consulted (and extended) in Python as rows are created.
    """

    #: alumni_ids already attending this event (alumni and friends alike).
    attending_ids: set[int] = field(default_factory=set)
    #: name + employer keys of everyone already attending this event.
    roster_keys: set[str] = field(default_factory=set)
    #: normalised email -> live ALUMNUS ids carrying it (the guard leg).
    alumni_by_email: dict[str, list[int]] = field(default_factory=dict)
    #: normalised email -> live FRIEND ids carrying it (the reuse leg).
    friends_by_email: dict[str, list[int]] = field(default_factory=dict)
    #: name + employer key -> the oldest live friend id with that key.
    friends_by_key: dict[str, int] = field(default_factory=dict)

    def decide(self, row: dict) -> FriendDecision:
        """Apply the #538 rule to one parsed attendee row (see module doc)."""
        emails: list[str] = list(row.get("emails") or ())
        identity = friend_identity_key(
            row.get("first_name"), row.get("last_name"), row.get("company")
        )

        # 1. An alumnus already owns this email -> never create a friend.
        for email in emails:
            owners = self.alumni_by_email.get(email)
            if owners:
                return FriendDecision("existing_alumnus", alumni_id=owners[0], matched_on="email")

        # 2. This event's roster already has this name + employer (idempotent
        #    re-post, or an alumnus approved earlier in the same wizard).
        if identity in self.roster_keys:
            return FriendDecision("on_roster", matched_on="name")

        # 3. Email present -> email is the identity, across every event.
        if emails:
            for email in emails:
                friends = self.friends_by_email.get(email)
                if friends:
                    return FriendDecision("reuse", alumni_id=friends[0], matched_on="email")
            return FriendDecision("create")

        # 4. No email -> name + employer, across every friend in the table.
        existing = self.friends_by_key.get(identity)
        if existing is not None:
            return FriendDecision("reuse", alumni_id=existing, matched_on="name")
        return FriendDecision("create")

    def remember(self, row: dict, alumni_id: int) -> None:
        """Record a friend this request just created or reused, so a second
        row for the same person later in the SAME file resolves to it instead
        of creating a twin (the DB is not re-queried mid-batch)."""
        self.attending_ids.add(alumni_id)
        identity = friend_identity_key(
            row.get("first_name"), row.get("last_name"), row.get("company")
        )
        self.roster_keys.add(identity)
        self.friends_by_key.setdefault(identity, alumni_id)
        for email in row.get("emails") or ():
            self.friends_by_email.setdefault(email, []).append(alumni_id)


async def build_friend_index(session: AsyncSession, event_id: int, rows: list[dict]) -> FriendIndex:
    """Load the roster, the email owners and the name-key friends for ``rows``
    in at most three batched queries (never one per row)."""
    index = FriendIndex()

    roster = (
        await session.execute(
            select(
                Alumni.alumni_id,
                Alumni.first_name,
                Alumni.preferred_first_name,
                Alumni.last_name,
                CurrentEmployment.current_employer,
            )
            .join(EventAttendance, EventAttendance.alumni_id == Alumni.alumni_id)
            .outerjoin(CurrentEmployment, CurrentEmployment.alumni_id == Alumni.alumni_id)
            .where(EventAttendance.event_id == event_id)
        )
    ).all()
    for alumni_id, first, preferred, last, employer in roster:
        index.attending_ids.add(alumni_id)
        index.roster_keys.add(friend_identity_key(first, last, employer))
        if preferred:
            index.roster_keys.add(friend_identity_key(preferred, last, employer))

    emails = sorted({e for r in rows for e in (r.get("emails") or ())})
    if emails:
        hits = (
            await session.execute(
                select(
                    Alumni.alumni_id,
                    Alumni.is_alumni,
                    AlumniContactInfo.personal_email,
                    AlumniContactInfo.work_email,
                )
                .join(AlumniContactInfo, AlumniContactInfo.alumni_id == Alumni.alumni_id)
                .where(
                    Alumni.archived.is_(False),
                    or_(
                        _norm_col(AlumniContactInfo.personal_email).in_(emails),
                        _norm_col(AlumniContactInfo.work_email).in_(emails),
                    ),
                )
                .order_by(Alumni.alumni_id.asc())
                .limit(_MAX_LOOKUP_ROWS)
            )
        ).all()
        for alumni_id, is_alumni, personal, work in hits:
            bucket = index.alumni_by_email if is_alumni else index.friends_by_email
            for raw in (personal, work):
                email = _norm_email(raw)
                if email and email in emails:
                    ids = bucket.setdefault(email, [])
                    if alumni_id not in ids:
                        ids.append(alumni_id)

    surnames = sorted(
        {key for r in rows if not r.get("emails") for key in surname_keys(r.get("last_name"))}
    )
    if surnames:
        friends = (
            await session.execute(
                select(
                    Alumni.alumni_id,
                    Alumni.first_name,
                    Alumni.preferred_first_name,
                    Alumni.last_name,
                    CurrentEmployment.current_employer,
                )
                .outerjoin(CurrentEmployment, CurrentEmployment.alumni_id == Alumni.alumni_id)
                .where(
                    Alumni.archived.is_(False),
                    Alumni.is_alumni.is_(False),
                    _norm_col(Alumni.last_name).in_(surnames),
                )
                .order_by(Alumni.alumni_id.asc())
                .limit(_MAX_LOOKUP_ROWS)
            )
        ).all()
        for alumni_id, first, preferred, last, employer in friends:
            # setdefault: the OLDEST friend with the key wins, deterministically,
            # when prod already holds twins (Jake's read-only count decides
            # whether those get merged by hand).
            index.friends_by_key.setdefault(friend_identity_key(first, last, employer), alumni_id)
            if preferred:
                index.friends_by_key.setdefault(
                    friend_identity_key(preferred, last, employer), alumni_id
                )
    return index
