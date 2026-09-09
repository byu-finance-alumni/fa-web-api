"""The run digest — what an unattended run did, written where Jake can read it.

``request next --execute`` runs when nobody is watching. The evening run
finishes hours before anybody reads it, so **the digest is the only thing Jake
sees the next morning** — and it is read cold, over coffee, with no memory of
what was approved the day before.

That sets the bar. The digest opens with a one-line verdict, names every
request by id AND title (an id alone means nothing at 8am), and states a parked
request's blocking question in full, so it can be answered without opening the
request file at all.

It lands in ``change-requests/runs/YYYY-MM-DD-HHMM.md``.

⚠️ **The digest obeys the work-log rule.** No email body, no email address, no
attachment content, no alumni data. It is a run log, not a copy of the request.
That matters for the same reason it matters for the CSV: this is a file
somebody pastes into Slack when a run misbehaves, and a request Markdown is
not. Every value written here goes through :func:`scrub`, which flattens
newlines, redacts anything shaped like an email address, and caps the length.

In practice the scrubber never fires. The two things the digest quotes are
validator refusals and a ``--reason`` somebody typed, and no validator refusal
quotes email text — they name folders, fields and filenames. The scrubber is
there so that a refusal message written a year from now cannot leak by
accident, which is the only kind of leak this system is likely to get.

The parked section is appended by ``request park``, not written by ``next``:
``next`` selects and reports, and parking happens later, in the session that
found the blocker. :func:`append_park` finds the newest digest and adds to it,
so one run's story stays in one file.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import re
from dataclasses import dataclass, field

from . import paths

#: ``YYYY-MM-DD-HHMM`` — minute resolution, because a run that takes less than
#: a minute is the common case and a second-resolution name reads as noise.
DIGEST_STEM_FMT = "%Y-%m-%d-%H%M"
TIMESTAMP_FMT = "%Y-%m-%d %H:%M"

#: Deliberately permissive on the local part: this redacts, it does not
#: validate, and a false positive costs nothing but a redaction marker.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_REDACTED = "[address redacted]"

#: Long enough for the longest validator refusal, short enough that no body
#: text could survive intact if one ever reached this file.
MAX_VALUE_CHARS = 400

#: A parked request's blocking question gets more room, because the whole point
#: is that Jake can answer it WITHOUT opening the request. It is human-typed
#: text from a ``--reason`` flag, not email content, so the tighter cap is not
#: buying anything here.
MAX_QUESTION_CHARS = 1_200

#: The outcomes, and the fixed-width prefixes both the digest and the console
#: summary use. These are the machine-readable half of ``request next``: a
#: caller greps for the prefix and reads the id that follows it.
PICKED = "PICKED"
SKIPPED = "SKIPPED"
DEFERRED = "DEFERRED"
PARKED = "PARKED"
OUTCOME_WIDTH = 8


def scrub(value: object, *, limit: int = MAX_VALUE_CHARS) -> str:
    """Make one value safe and boring enough to write into a run log."""
    text = "" if value is None else str(value)
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    text = _EMAIL_RE.sub(_REDACTED, text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit].rstrip() + " …(truncated)"
    return text


@dataclass
class Entry:
    """One request's outcome in one run."""

    request_id: str
    title: str = ""
    outcome: str = SKIPPED
    branch: str = ""
    #: Why. For a skip these are the validator's failures, verbatim.
    detail: list[str] = field(default_factory=list)

    def line(self) -> str:
        """The one-line console form: ``PICKED   CR-2026-001  Title``."""
        parts = [f"{self.outcome:<{OUTCOME_WIDTH}}", self.request_id]
        if self.title:
            parts.append(scrub(self.title))
        return "  ".join(parts)


@dataclass
class Run:
    """Everything one ``request next`` invocation did."""

    started: dt.datetime
    mode: str
    limit: int
    repo: str
    home: pathlib.Path
    imported: int = 0
    approved_seen: int = 0
    still_parked: list[str] = field(default_factory=list)
    entries: list[Entry] = field(default_factory=list)

    def of(self, outcome: str) -> list[Entry]:
        return [entry for entry in self.entries if entry.outcome == outcome]

    def elapsed_seconds(self, finished: dt.datetime) -> int:
        return max(0, int(round((finished - self.started).total_seconds())))


#: The verdict line's fixed opening. ``append_park`` finds it by this prefix.
VERDICT_PREFIX = "**Verdict — "

#: Rewritten in place when a park is appended after the digest was written.
#: The park happens minutes AFTER ``next`` finishes — the session works the
#: request, hits the blocker, and only then parks — so the verdict has to be
#: amendable or it would say "0 parked" on a run that parked something, which
#: is the one sentence Jake would read and believe.
_VERDICT_PARKED = re.compile(r"(\d+) parked(?: \(([^)]*)\))?")


def _count_phrase(label: str, entries: list[Entry]) -> str:
    if not entries:
        return f"0 {label}"
    ids = ", ".join(entry.request_id for entry in entries)
    return f"{len(entries)} {label} ({ids})"


def verdict_line(run: Run) -> str:
    """The one line that has to survive being the only thing anybody reads."""
    return VERDICT_PREFIX + ", ".join(
        (
            _count_phrase("picked up", run.of(PICKED)),
            _count_phrase("parked", run.of(PARKED)),
            _count_phrase("skipped", run.of(SKIPPED)),
            _count_phrase("deferred", run.of(DEFERRED)),
        )
    ) + ".**"


def amend_verdict(text: str, request_id: str) -> str:
    """Add one parked request to an already-written verdict line."""
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if not line.startswith(VERDICT_PREFIX):
            continue
        match = _VERDICT_PARKED.search(line)
        if not match:
            return text
        count = int(match.group(1)) + 1
        ids = [value for value in (match.group(2) or "").split(", ") if value]
        ids.append(request_id)
        lines[index] = (
            line[: match.start()] + f"{count} parked ({', '.join(ids)})" + line[match.end():]
        )
        return "\n".join(lines)
    return text


def _section(title: str, entries: list[Entry], *, empty: str) -> list[str]:
    lines = [f"## {title}", ""]
    if not entries:
        lines += [empty, ""]
        return lines
    for entry in entries:
        head = f"- **{entry.request_id}**"
        if entry.title:
            head += f" — {scrub(entry.title)}"
        lines.append(head)
        if entry.branch:
            lines.append(f"  - branch: `{scrub(entry.branch)}`")
        for item in entry.detail:
            lines.append(f"  - {scrub(item)}")
    lines.append("")
    return lines


def render_digest(run: Run, *, finished: dt.datetime) -> str:
    """The digest Markdown for one run."""
    lines = [
        f"# Change-request run {run.started.strftime(TIMESTAMP_FMT)}",
        "",
        verdict_line(run),
        "",
        "Nothing was pushed, deployed or promoted by this run — that is a",
        "deliberate step somebody takes, once per batch, to `dev`. Anything",
        "under **Parked** below is waiting on an answer; the question is quoted",
        "in full, so it can be answered without opening the request.",
        "",
        f"- Mode: {run.mode}",
        f"- Limit: {run.limit}",
        f"- Default repo: {run.repo}",
        f"- Home: `{run.home}`",
        f"- Imported from inbox-msg: {run.imported}",
        f"- Requests in approved/: {run.approved_seen}",
        f"- Elapsed: {run.elapsed_seconds(finished)}s",
        "",
        "> Run log only. No email body, no address, no attachment content, no",
        "> alumni data — the same rule as `work-log.csv`.",
        "",
    ]
    lines += _section("Picked up", run.of(PICKED), empty="Nothing was picked up.")
    lines += _section(
        "Skipped", run.of(SKIPPED), empty="Nothing was skipped."
    )
    lines += _section(
        "Deferred to the next run", run.of(DEFERRED), empty="Nothing was deferred."
    )
    lines += _section(
        "Parked",
        run.of(PARKED),
        empty="Nothing was parked during this run.",
    )
    lines += ["## Already parked", ""]
    if run.still_parked:
        lines += [f"- {request_id}" for request_id in run.still_parked]
    else:
        lines.append("Nothing is sitting in parked/.")
    lines.append("")
    return "\n".join(lines)


def digest_path(root: pathlib.Path, when: dt.datetime) -> pathlib.Path:
    """``runs/YYYY-MM-DD-HHMM.md``, suffixed if that minute is already taken.

    Two runs in one minute is unusual but not impossible — Jake running it by
    hand while the scheduled task fires. Overwriting the first one would delete
    the only record of it.
    """
    folder = root / "runs"
    stem = when.strftime(DIGEST_STEM_FMT)
    candidate = folder / f"{stem}.md"
    counter = 2
    while candidate.exists():
        candidate = folder / f"{stem}-{counter}.md"
        counter += 1
    return candidate


def write_digest(root: pathlib.Path, run: Run, *, finished: dt.datetime) -> pathlib.Path:
    path = digest_path(root, run.started)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_digest(run, finished=finished), encoding="utf-8")
    return path


def latest_digest(root: pathlib.Path) -> pathlib.Path | None:
    """The newest digest file, or None when no run has been recorded yet."""
    folder = root / "runs"
    if not folder.is_dir():
        return None
    candidates = sorted(folder.glob("*.md"))
    return candidates[-1] if candidates else None


def append_park(
    root: pathlib.Path,
    *,
    request_id: str,
    title: str,
    reason: str,
    when: dt.datetime,
) -> pathlib.Path | None:
    """Record a park in the newest digest, under its ``## Parked`` heading.

    Also amends the verdict line, which was written before the session had any
    way of knowing this would park.

    Returns the digest it wrote to, or None when there is no digest to write to
    — parking outside a run is legitimate (Jake parks by hand), and inventing a
    run digest for it would put a run in the log that never happened.
    """
    path = latest_digest(root)
    if path is None:
        return None
    text = amend_verdict(path.read_text(encoding="utf-8"), scrub(request_id))
    heading = f"- **{scrub(request_id)}**"
    if title:
        heading += f" — {scrub(title)}"
    entry = "\n".join(
        [
            heading,
            f"  - parked at {when.strftime(TIMESTAMP_FMT)}",
            f"  - **question:** {scrub(reason, limit=MAX_QUESTION_CHARS)}",
            f"  - answer it with: `request unpark {scrub(request_id)} --answer \"...\"`",
        ]
    )
    marker = "## Parked\n\nNothing was parked during this run.\n"
    if marker in text:
        text = text.replace(marker, f"## Parked\n\n{entry}\n", 1)
    elif "## Parked\n" in text:
        head, _, tail = text.partition("## Parked\n")
        # Insert immediately after the blank line that follows the heading, so
        # the entries stay in the order they happened.
        tail_lines = tail.split("\n")
        insert_at = 1 if tail_lines and tail_lines[0] == "" else 0
        body = tail_lines[:insert_at] + entry.split("\n") + tail_lines[insert_at:]
        text = head + "## Parked\n" + "\n".join(body)
    else:  # pragma: no cover - only reachable if a digest was hand-edited
        text = text.rstrip("\n") + f"\n\n## Parked\n\n{entry}\n"
    path.write_text(text, encoding="utf-8")
    return path


def ensure_runs_folder(root: pathlib.Path | None = None) -> pathlib.Path:
    folder = (root or paths.home()) / "runs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder
