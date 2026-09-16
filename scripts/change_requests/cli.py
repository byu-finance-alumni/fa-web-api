"""Change-request intake CLI — the ten commands Jake actually types.

Run it the way every other script in this repo runs::

    python -m scripts.change_requests.cli setup
    python -m scripts.change_requests.cli import
    python -m scripts.change_requests.cli list
    python -m scripts.change_requests.cli validate CR-2026-001
    python -m scripts.change_requests.cli start CR-2026-001 --repo fa-web-api
    python -m scripts.change_requests.cli complete CR-2026-001
    python -m scripts.change_requests.cli log-time CR-2026-001 --testing 15
    python -m scripts.change_requests.cli next --execute --limit 1
    python -m scripts.change_requests.cli park CR-2026-001 --reason "..."
    python -m scripts.change_requests.cli unpark CR-2026-001 --answer "..."

or through the thin wrappers, which is what the docs tell him to do::

    .\\scripts\\request.ps1 import          (Windows)
    ./scripts/request.sh import             (bash)

THE SHAPE OF THE WORKFLOW
-------------------------
``import`` is deliberately unable to finish the job. It produces a file in
``ready/`` that says ``Status: Ready for Review`` and ``Approved for Claude:
No``, and there it stops. Jake writes the instructions and the acceptance
criteria, changes both fields by hand, and moves the file into ``approved/``.
Only then will ``validate`` pass, and ``start`` refuses to run until it does.

That refusal is the whole design. An email is a request, not an authorisation,
and the gap between those two words is a human being.

THE AUTOMATED HALF
------------------
``next`` is what a scheduled, unattended run invokes, and it is built around
one restriction:

    ⚠️ ``next`` READS ``approved/`` AND NOTHING ELSE.

Not ``inbox-msg/``, not ``ready/``. By the time a file is in ``approved/`` a
human has read it, written acceptance criteria, and moved it there. A batch
command that watched the inbox would walk straight through the approval gate
the rest of this package exists to enforce — and it would do it on a timer,
unattended, which is the worst possible way to find out.

``next`` also does not write code. It imports, selects, validates, clocks in
and reports; a Claude Code session reads the plan it prints and does the actual
work. Implementing an arbitrary change request is a reasoning task, and a
script that tried to generate the diff would be guessing at exactly the moment
nobody is watching.

Which leads to the other half: ``park``. An unattended run has nobody to ask,
so a request that cannot be finished cleanly is moved to ``parked/`` with the
blocking question written into it — never half-implemented.
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import re
import subprocess
import sys

from . import (
    attachments,
    digest,
    injection,
    ledger,
    paths,
    render,
    sanitize,
    validate,
    worklog,
)

TIMESTAMP_FMT = "%Y-%m-%d %H:%M"

#: CSV column, Time Log label, and the ``log-time`` flag that fills them.
JAKE_TIME_FIELDS = (
    ("request-review", "jake_request_review_minutes", "Jake request review"),
    ("testing", "jake_testing_minutes", "Jake testing"),
    ("correction", "jake_correction_minutes", "Jake corrections"),
    ("deployment", "jake_deployment_minutes", "Jake deployment"),
)

LIFECYCLE_FOLDERS = ("ready", "approved", "parked", "completed", "rejected")

#: Work-log statuses that mean an unattended run must NOT pick a request up
#: again. ``complete`` deliberately leaves a finished request in ``approved/``
#: until Jake confirms it, so "is the file in approved/" cannot be the whole
#: answer to "is there work left to do here".
IN_FLIGHT_STATUSES = ("In Progress", "Implemented", "Completed", "Parked")

#: What ``## Claude Implementation`` says before anything has touched it.
NOT_STARTED = "Not started."


def _now() -> dt.datetime:
    return dt.datetime.now()


def _stamp(when: dt.datetime | None = None) -> str:
    return (when or _now()).strftime(TIMESTAMP_FMT)


def _root(args: argparse.Namespace) -> pathlib.Path:
    return pathlib.Path(args.home).resolve() if args.home else paths.home()


# --- request ids -------------------------------------------------------------


def next_request_id(root: pathlib.Path, *, year: int | None = None) -> str:
    """Allocate the next sequential id for the current year.

    The counter restarts every January: ``CR-2026-412`` is followed by
    ``CR-2027-001``. Ids are never reused, and the scan covers every lifecycle
    folder so a completed or rejected request still holds its number.
    """
    year = year or _now().year
    highest = 0
    for folder_name in LIFECYCLE_FOLDERS:
        folder = root / folder_name
        if not folder.is_dir():
            continue
        for entry in folder.glob(f"CR-{year}-*.md"):
            match = render.FILENAME_RE.match(entry.name)
            if match:
                highest = max(highest, int(match.group(1).split("-")[-1]))
    return f"CR-{year}-{highest + 1:03d}"


def request_filename(request_id: str, title: str) -> str:
    return f"{request_id}-{sanitize.slugify(title, max_len=48, default='request')}.md"


# --- setup -------------------------------------------------------------------


def cmd_setup(args: argparse.Namespace) -> int:
    root = _root(args)
    created = paths.ensure_layout(root)

    template_target = root / "templates" / paths.TEMPLATE_NAME
    if not template_target.exists():
        template_target.write_text(
            paths.packaged_template().read_text(encoding="utf-8"), encoding="utf-8"
        )
        created.append(template_target)

    log = paths.work_log_path(root=root)
    if worklog.ensure(log):
        created.append(log)

    print(f"change-request home: {root}")
    for item in created:
        print(f"  created {item.relative_to(root) if item != root else item}")
    if not created:
        print("  (already set up — nothing changed)")

    if args.with_local_history:
        if (root / ".git").exists():
            print("  local history already initialised")
        else:
            result = subprocess.run(
                ["git", "init", "--quiet", str(root)], capture_output=True, text=True
            )
            if result.returncode == 0:
                (root / ".gitignore").write_text("", encoding="utf-8")
                print("  initialised a LOCAL git repo for history")
                print("  ⚠️ never add a remote to it — it holds email bodies and PII")
            else:
                print(f"  git init failed: {result.stderr.strip()}", file=sys.stderr)
    return 0


# --- import ------------------------------------------------------------------


def _import_one(
    source: pathlib.Path,
    *,
    root: pathlib.Path,
    entries: dict[str, ledger.LedgerEntry],
    dry_run: bool,
) -> str | None:
    """Import one ``.msg``. Returns the new request id, or None if skipped."""
    from .msg_reader import MsgReaderError, read_msg

    source_digest = ledger.file_hash(source)
    if source_digest in entries:
        print(f"  skip {source.name} — already imported as {entries[source_digest].request_id}")
        return None

    try:
        parsed = read_msg(source)
    except MsgReaderError as exc:
        print(f"  FAILED {source.name}: {exc}", file=sys.stderr)
        return None

    body, invisible_removed = sanitize.clean_body(parsed.body)
    body, truncated = sanitize.truncate(body)
    findings = injection.scan(body)

    request_id = next_request_id(root)
    records = attachments.store(
        request_id,
        parsed.attachments,
        attachments_root=root / "attachments",
        write=not dry_run,
    )

    title = sanitize.clean_field(parsed.subject, max_len=120) or "(no subject)"
    text = render.render_request(
        request_id=request_id,
        title=title,
        requested_by=parsed.sender_name,
        requester_email=parsed.sender_email,
        received=parsed.sent_at,
        imported=_now(),
        source_file=source.name,
        body=body,
        truncated=truncated,
        findings=findings,
        records=records,
        invisible_removed=invisible_removed,
        body_was_html=parsed.body_was_html,
        template=paths.template_path(root=root),
    )

    target = root / "ready" / request_filename(request_id, title)
    blocked = [r for r in records if r.verdict == attachments.BLOCKED]
    if dry_run:
        print(f"  would import {source.name} -> ready/{target.name}")
    else:
        if target.exists():
            print(f"  FAILED {source.name}: {target.name} already exists", file=sys.stderr)
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

        ledger.record(
            entries,
            source_sha256=source_digest,
            request_id=request_id,
            source_file=source.name,
            body_sha256=render.body_hash(body),
            subject=title,
        )
        ledger.save(paths.ledger_path(root=root), entries)

        row = worklog.blank_row() | {
            "request_id": request_id,
            "request_title": title,
            # Display name ONLY. The address stays in the Markdown.
            "requester": parsed.sender_name,
            "date_received": _stamp(parsed.sent_at) if parsed.sent_at else "",
            "status": render.STATUS_READY,
        }
        worklog.append(paths.work_log_path(root=root), row)
        print(f"  imported {source.name} -> ready/{target.name}")

    if findings:
        print(f"    injection flags: {len(findings)} ({', '.join(f.label for f in findings)})")
    for record in blocked:
        print(f"    BLOCKED attachment not written: {record.original_name}")
    if truncated:
        print(f"    body truncated at {sanitize.BODY_CHAR_LIMIT} characters")
    return request_id


def _import_all(root: pathlib.Path, *, dry_run: bool, quiet: bool = False) -> int | None:
    """Import every new ``.msg``. Returns the count, or None if there is no inbox.

    ``quiet`` suppresses the header and the summary when nothing happened. That
    exists for ``next``, which runs on a timer: an empty inbox is the common
    case, and two lines of "0 imported" twice a day is how a log stops being
    read.
    """
    inbox = root / "inbox-msg"
    if not inbox.is_dir():
        print(f"no inbox at {inbox} — run 'setup' first", file=sys.stderr)
        return None

    worklog.ensure(paths.work_log_path(root=root))
    entries = ledger.load(paths.ledger_path(root=root))
    sources = sorted(p for p in inbox.iterdir() if p.is_file() and p.suffix.lower() == ".msg")

    if not (quiet and not sources):
        print(f"inbox: {inbox} ({len(sources)} .msg file(s))")
    imported = 0
    for source in sources:
        if _import_one(source, root=root, entries=entries, dry_run=dry_run) is not None:
            imported += 1

    if not (quiet and not sources):
        print(f"{imported} imported, {len(sources) - imported} skipped or failed")
    return imported


def cmd_import(args: argparse.Namespace) -> int:
    root = _root(args)
    imported = _import_all(root, dry_run=args.dry_run)
    if imported is None:
        return 2
    if imported and not args.dry_run:
        print("Nothing is approved. Review each file in ready/, then move it to approved/.")
    return 0


# --- list --------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    root = _root(args)
    rows: list[tuple[str, str, str, str]] = []
    for folder_name in LIFECYCLE_FOLDERS:
        folder = root / folder_name
        if not folder.is_dir():
            continue
        for entry in sorted(folder.glob("CR-*.md")):
            text = entry.read_text(encoding="utf-8", errors="replace")
            trusted = render.split_header_region(text)
            status = render.STATUS_KEY.findall(trusted)
            heading = next(
                (ln[2:].strip() for ln in text.split("\n") if ln.startswith("# ")), entry.stem
            )
            rows.append(
                (entry.stem.split("-")[0] + "-" + "-".join(entry.stem.split("-")[1:3]),
                 folder_name, (status[0].strip() if status else "?"), heading)
            )

    if args.status:
        wanted = args.status.lower()
        rows = [row for row in rows if row[2].lower() == wanted]

    if not rows:
        print(f"no change requests under {root}")
        return 0
    width = max(len(row[0]) for row in rows)
    for request_id, folder_name, status, heading in rows:
        print(f"{request_id:<{width}}  {folder_name:<9}  {status:<18}  {heading}")
    return 0


# --- validate ----------------------------------------------------------------


def cmd_validate(args: argparse.Namespace) -> int:
    root = _root(args)
    result = validate.validate(args.request_id, root=root)
    print(result.render())
    return 0 if result.ok else 1


# --- start -------------------------------------------------------------------


def branch_name(request_id: str, path: pathlib.Path) -> str:
    match = render.FILENAME_RE.match(path.name)
    slug = match.group(2) if match else "request"
    return f"cr/{request_id}-{slug}"


def _create_branch(
    repo: pathlib.Path, branch: str, worktree: pathlib.Path
) -> tuple[bool, str]:
    """Create an isolated WORKTREE for the branch — never touch the main checkout.

    This deliberately does NOT run ``git checkout -b`` in the repo itself. HEAD
    is repo-global: a checkout here would switch the branch under whatever Jake
    (or another agent) has open in that repo. The evening run happens while
    nobody is watching, so that would surface the next morning as work stranded
    on the wrong branch. Both repos already use ``.worktrees/`` for this reason.

    Returns ``(ok, message)``. The worktree is where the implementing session
    must do its work.
    """
    if not (repo / ".git").exists():
        return False, f"{repo} is not a git checkout"

    if worktree.exists():
        return True, f"reusing worktree {worktree}"

    existing = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", branch],
        capture_output=True,
        text=True,
    )
    worktree.parent.mkdir(parents=True, exist_ok=True)

    if existing.returncode == 0:
        cmd = ["git", "-C", str(repo), "worktree", "add", str(worktree), branch]
        note = f"attached worktree to existing branch {branch}"
    else:
        base = _default_base(repo)
        cmd = ["git", "-C", str(repo), "worktree", "add", "-b", branch, str(worktree), base]
        note = f"created worktree on {branch} from {base}"

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return False, result.stderr.strip() or "git worktree add failed"
    return True, note


def _default_base(repo: pathlib.Path) -> str:
    """Base new work on ``dev``; never on ``prod``, which is the deploy branch."""
    for candidate in ("origin/dev", "dev"):
        found = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", candidate],
            capture_output=True,
            text=True,
        )
        if found.returncode == 0:
            return candidate
    return "HEAD"


def _start_request(
    *,
    root: pathlib.Path,
    request_id: str,
    path: pathlib.Path,
    repo: str,
    no_branch: bool,
) -> tuple[bool, str, str]:
    """Branch, clock in, and record it. Returns ``(ok, branch, message)``.

    ⚠️ Assumes validation has ALREADY passed. Both callers validate first and
    refuse before reaching here; this function does not re-check, and must not
    be called from anywhere that has not.
    """
    branch = branch_name(request_id, path)
    started = _stamp()

    if no_branch:
        message = f"branch not created (--no-branch); recording {branch}"
    else:
        worktree = paths.worktree_dir(request_id)
        ok, message = _create_branch(paths.repo_dir(repo), branch, worktree)
        if not ok:
            return False, branch, message

    text = path.read_text(encoding="utf-8")
    text = render.replace_section(
        text,
        "Claude Implementation",
        f"Branch: `{branch}` in `{repo}`\n"
        f"Worktree: `{paths.worktree_dir(request_id)}`\n"
        f"Started: {started}\n\nIn progress.",
    )
    text = render.set_time_log(text, "Claude started", started)
    path.write_text(text, encoding="utf-8")

    worklog.update(
        paths.work_log_path(root=root),
        request_id,
        {"claude_started": started, "status": "In Progress", "branch": branch},
    )
    return True, branch, message


def cmd_start(args: argparse.Namespace) -> int:
    root = _root(args)
    result = validate.validate(args.request_id, root=root)
    if not result.ok:
        print(result.render(), file=sys.stderr)
        print("\nrefusing to start — nothing has been changed.", file=sys.stderr)
        return 1
    print(result.render())

    path = result.path
    assert path is not None
    ok, branch, message = _start_request(
        root=root,
        request_id=args.request_id,
        path=path,
        repo=args.repo,
        no_branch=args.no_branch,
    )
    print(f"  {message}")
    if not ok:
        print("refusing to start — the branch could not be created.", file=sys.stderr)
        return 1
    print(f"started {args.request_id} on {branch}")
    return 0


# --- complete ----------------------------------------------------------------


def cmd_complete(args: argparse.Namespace) -> int:
    root = _root(args)
    path = validate.find_request(args.request_id, root=root)
    if path is None:
        print(f"no request file for {args.request_id} under {root}", file=sys.stderr)
        return 2

    finished = _stamp()
    log = paths.work_log_path(root=root)
    rows = worklog.read(log)
    row = next((r for r in rows if r.get("request_id") == args.request_id), None)
    started = (row or {}).get("claude_started", "")
    runtime = worklog.runtime_minutes(started, finished)

    text = path.read_text(encoding="utf-8")
    text = render.set_time_log(text, "Claude finished", finished)
    if runtime:
        text = render.set_time_log(text, "Claude runtime", f"{runtime} minutes")
    path.write_text(text, encoding="utf-8")

    # NOTE: no jake_* field is written here, ever. Claude does not get to
    # estimate how long Jake spent.
    fields = {
        "claude_finished": finished,
        "claude_runtime_minutes": runtime,
        "status": "Implemented",
    }
    if args.notes:
        fields["notes"] = args.notes
    worklog.update(log, args.request_id, fields)

    if args.move_to_completed:
        target = root / "completed" / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        path.replace(target)
        worklog.update(log, args.request_id, {"status": "Completed"})
        print(f"moved to completed/{target.name}")
    else:
        print("left in place — move it to completed/ only once Jake confirms.")

    print(f"completed {args.request_id} (runtime {runtime or 'unknown'} minutes)")
    return 0


# --- log-time ----------------------------------------------------------------


def cmd_log_time(args: argparse.Namespace) -> int:
    root = _root(args)
    log = paths.work_log_path(root=root)
    if not worklog.has_row(log, args.request_id):
        print(f"no work-log row for {args.request_id}", file=sys.stderr)
        return 2

    fields: dict[str, str] = {}
    for flag, column, _label in JAKE_TIME_FIELDS:
        value = getattr(args, flag.replace("-", "_"))
        if value is not None:
            fields[column] = str(value)
    if not fields:
        print("nothing to log — pass at least one of "
              "--request-review/--testing/--correction/--deployment", file=sys.stderr)
        return 2

    worklog.update(log, args.request_id, fields)

    row = next(
        (r for r in worklog.read(log) if r.get("request_id") == args.request_id), None
    ) or {}
    total = row.get("total_jake_minutes", "")

    path = validate.find_request(args.request_id, root=root)
    if path is not None:
        text = path.read_text(encoding="utf-8")
        for _flag, column, label in JAKE_TIME_FIELDS:
            if column in fields:
                text = render.set_time_log(text, label, f"{fields[column]} minutes")
        if total:
            text = render.set_time_log(text, "Total Jake work time", f"{total} minutes")
        path.write_text(text, encoding="utf-8")

    print(f"{args.request_id}: total Jake time {total or '(not recorded)'} minutes")
    return 0


# --- next: the batch command an unattended run invokes ------------------------


def _request_id_of(path: pathlib.Path) -> str | None:
    match = render.FILENAME_RE.match(path.name)
    return match.group(1) if match else None


def _id_sort_key(path: pathlib.Path) -> tuple[int, int, str]:
    """Sort by year then number, so CR-2026-999 precedes CR-2026-1000.

    Plain filename sort is lexicographic and would put the four-digit id first
    the day the counter passes 999. That day is years away and the bug would be
    silent when it arrived, which is exactly the kind worth spending three lines
    on now.
    """
    request_id = _request_id_of(path)
    if request_id is None:
        return (9999, 9_999_999, path.name)
    _, year, number = request_id.split("-")
    return (int(year), int(number), path.name)


def _title_of(text: str, default: str) -> str:
    """The ``# `` heading, which is the email subject. Never body text.

    The first ``# `` line in the file is the template's title line, and it is
    always above the quarantined region. A quoted body that happens to contain
    a Markdown heading is therefore never the one found.
    """
    return next((line[2:].strip() for line in text.split("\n") if line.startswith("# ")), default)


def _in_flight(text: str, row: dict[str, str] | None) -> str | None:
    """Why this request is already under way, or None if it is fresh.

    ``complete`` leaves a finished request in ``approved/`` until Jake confirms
    it, so without this check a twice-daily run would re-start the same request
    every twelve hours: a new branch, a new ``claude_started``, and a clock that
    resets on work that is already done.

    Every uncertainty here resolves towards "leave it alone". If the request
    file looks touched at all, it is skipped and a human decides.
    """
    row = row or {}
    started = (row.get("claude_started") or "").strip()
    if started:
        branch = (row.get("branch") or "").strip() or "unrecorded"
        return f"already started at {started} on branch {branch}"
    status = (row.get("status") or "").strip()
    if status in IN_FLIGHT_STATUSES:
        return f"the work-log status is '{status}'"
    implementation = render.section(text, "Claude Implementation").strip()
    if implementation and implementation != NOT_STARTED:
        return "'## Claude Implementation' is already filled in"
    return None


def _target_repo(text: str, default: str) -> tuple[str | None, str | None]:
    """``(repo, problem)``. The request may name it; otherwise the run's default.

    ``request start`` takes ``--repo`` because a person is typing it. An
    unattended run has a default instead, and a request may override it with a
    ``Target Repo:`` line. Anything the line says that is not a repo we know is
    a REFUSAL, not a fallback to the default: an unattended run does not guess
    which codebase to branch.
    """
    values = render.TARGET_REPO_KEY.findall(render.split_header_region(text))
    if not values:
        return default, None
    if len(values) > 1:
        return None, (
            f"'Target Repo:' appears {len(values)} times outside the quoted email — "
            "exactly one is required"
        )
    value = values[0].strip()
    if value not in paths.KNOWN_REPOS:
        return None, (
            f"'Target Repo: {digest.scrub(value)}' is not one of "
            f"{', '.join(paths.KNOWN_REPOS)} — an unattended run does not guess a repo"
        )
    return value, None


#: Printed above the plan. The session that reads this output is the one that
#: will write the code, and these are the four rules it most needs in front of
#: it at that moment.
BATCH_RULES = (
    "park, do not guess — ambiguity, a security concern, a missing decision or a "
    "pre-existing failing test are all park conditions",
    "implement only the approved scope; anything else noticed goes in the write-up",
    "never touch production data, never deploy, never promote",
    "commit locally — ONE push per run, for the whole batch, to dev only",
)


def cmd_next(args: argparse.Namespace) -> int:
    """Select approved work, report it, and (with ``--execute``) clock it in."""
    root = _root(args)
    execute = bool(args.execute)
    run = digest.Run(
        started=_now(),
        mode="execute" if execute else "dry-run",
        limit=args.limit,
        repo=args.repo,
        home=root,
    )

    approved_dir = root / "approved"
    if not approved_dir.is_dir():
        print(f"no approved/ folder at {approved_dir} — run 'setup' first", file=sys.stderr)
        return 2

    # 1. Import first, so anything Jake dropped in inbox-msg/ is at least a
    #    ready/ file by the time he next looks. It cannot reach approved/ from
    #    here — only he can move it — so this never widens what gets worked.
    imported = _import_all(root, dry_run=not execute, quiet=True)
    if imported is None:
        return 2
    run.imported = imported

    # 2. approved/ ONLY. Never inbox-msg/, never ready/. This one line is the
    #    safety property of the whole command.
    candidates = sorted(approved_dir.glob("CR-*.md"), key=_id_sort_key)
    run.approved_seen = len(candidates)
    if not candidates:
        # The common case, twice a day, for most of the year. It costs nothing:
        # no digest file, no noise, one line.
        print("approved/ is empty — nothing to do.")
        return 0

    parked_dir = root / "parked"
    if parked_dir.is_dir():
        parked_files = sorted(parked_dir.glob("CR-*.md"), key=_id_sort_key)
        run.still_parked = [
            request_id
            for request_id in (_request_id_of(path) for path in parked_files)
            if request_id
        ]

    log = paths.work_log_path(root=root)
    rows = {row.get("request_id", ""): row for row in worklog.read(log)}

    picked: list[tuple[str, pathlib.Path, str, digest.Entry]] = []
    for path in candidates:
        text = path.read_text(encoding="utf-8", errors="replace")
        request_id = _request_id_of(path)
        entry = digest.Entry(
            request_id=request_id or path.name,
            title=_title_of(text, path.stem),
        )
        run.entries.append(entry)

        if request_id is None:
            entry.outcome = digest.SKIPPED
            entry.detail = [f"the filename '{path.name}' is not CR-YYYY-NNN-short-title.md"]
            continue

        if len(picked) >= args.limit:
            entry.outcome = digest.DEFERRED
            entry.detail = [f"--limit {args.limit} reached; a later run will consider it"]
            continue

        # 3. The existing validator, unchanged, and a failure is a SKIP — never
        #    a downgrade to a warning, and never a reason to stop the batch.
        result = validate.validate_file(path, request_id=request_id, root=root)
        if not result.ok:
            entry.outcome = digest.SKIPPED
            entry.detail = list(result.failures)
            continue

        blocked = _in_flight(text, rows.get(request_id))
        if blocked:
            entry.outcome = digest.SKIPPED
            entry.detail = [blocked]
            continue

        repo, problem = _target_repo(text, args.repo)
        if repo is None:
            entry.outcome = digest.SKIPPED
            entry.detail = [problem or "the target repo could not be determined"]
            continue

        entry.outcome = digest.PICKED
        entry.branch = branch_name(request_id, path)
        entry.detail = [f"warning: {warning}" for warning in result.warnings]
        picked.append((request_id, path, repo, entry))

    # 4. Report, and — only with --execute — clock in.
    print(f"change-request home: {root}")
    print(f"mode: {run.mode}   limit: {args.limit}   default repo: {args.repo}")
    if run.imported:
        print(f"imported from inbox-msg: {run.imported}")
    print(f"approved/: {run.approved_seen} request(s)")
    print("")
    print(digest.verdict_line(run).replace("**", ""))
    print("")
    for entry in run.entries:
        print(f"  {entry.line()}")
        for item in entry.detail:
            print(f"      - {digest.scrub(item)}")

    if execute:
        for request_id, path, repo, entry in list(picked):
            ok, branch, message = _start_request(
                root=root,
                request_id=request_id,
                path=path,
                repo=repo,
                no_branch=args.no_branch,
            )
            print(f"  {message}")
            if not ok:
                entry.outcome = digest.SKIPPED
                entry.detail = [f"could not start: {message}"]
                picked = [item for item in picked if item[3] is not entry]
            else:
                entry.branch = branch

    print("")
    if not picked:
        print("nothing to work — every approved request was skipped or deferred.")
    else:
        print("plan — work these in order, one at a time:")
        for number, (request_id, path, repo, entry) in enumerate(picked, start=1):
            print(f"  {number}. {request_id}  repo {repo}  branch {entry.branch}")
            print(f"     file: approved/{path.name}")
        print("")
        print("rules for this batch:")
        for rule in BATCH_RULES:
            print(f"  - {rule}")

    if execute:
        written = digest.write_digest(root, run, finished=_now())
        print("")
        print(f"digest: {written}")
    else:
        print("")
        print("dry run — nothing was changed. Re-run with --execute to start the work.")
    return 0


# --- park / unpark -----------------------------------------------------------

BLOCKED_ON = "Blocked On"

#: Written into the Blocked On entry and read back by ``unpark``. Deliberately
#: lowercase 's': ``Status:`` at the start of a line is a machine-read key, and
#: this is a note about one, not one.
PREVIOUS_STATUS_LABEL = "Previous status"
PREVIOUS_STATUS_KEY = re.compile(
    rf"^-[ \t]+{PREVIOUS_STATUS_LABEL}:[ \t]*(.*)$", re.MULTILINE
)


def _trusted_status(text: str) -> str | None:
    values = render.STATUS_KEY.findall(render.split_header_region(text))
    return values[0].strip() if len(values) == 1 else None


def _set_status_or_warn(text: str, value: str) -> str:
    """Rewrite ``Status:``, or leave it and say so.

    A malformed file is the one case where refusing outright would be worse
    than continuing: park exists to stop work being lost, and a file too broken
    to rewrite is exactly the file most worth moving out of ``approved/``.
    """
    try:
        return render.set_status(text, value)
    except ValueError as exc:
        print(f"  ⚠️ could not set 'Status: {value}' ({exc}) — set it by hand", file=sys.stderr)
        return text


def cmd_park(args: argparse.Namespace) -> int:
    root = _root(args)
    path = validate.find_request(args.request_id, root=root)
    if path is None:
        print(f"no request file for {args.request_id} under {root}", file=sys.stderr)
        return 2

    when = _now()
    text = path.read_text(encoding="utf-8")
    title = _title_of(text, path.stem)
    previous = _trusted_status(text) or "unknown"
    entry = "\n".join(
        [
            f"### Parked {_stamp(when)}",
            "",
            f"- {PREVIOUS_STATUS_LABEL}: {previous}",
            f"- Parked from: {path.parent.name}/",
            "",
            "Blocking question:",
            "",
            render.blockquote(args.reason),
        ]
    )
    try:
        text = render.append_section(text, BLOCKED_ON, entry)
    except ValueError as exc:
        print(f"cannot park {args.request_id}: {exc}", file=sys.stderr)
        return 1
    text = _set_status_or_warn(text, render.STATUS_PARKED)
    path.write_text(text, encoding="utf-8")

    if path.parent.name != "parked":
        target = root / "parked" / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        path.replace(target)
        path = target
        print(f"moved to parked/{path.name}")
    else:
        print(f"already in parked/ — appended a second blocker to {path.name}")

    log = paths.work_log_path(root=root)
    if not worklog.update(
        log, args.request_id, {"status": render.STATUS_PARKED, "notes": digest.scrub(args.reason)}
    ):
        print("  (no work-log row to update)")

    recorded = digest.append_park(
        root,
        request_id=args.request_id,
        title=title,
        reason=args.reason,
        when=when,
    )
    if recorded is not None:
        print(f"  recorded in {recorded.name}")

    # Nothing here touches git. The branch and every commit on it stay exactly
    # where they are: parking is "stop and ask", not "throw the work away".
    print(f"parked {args.request_id} — branch and commits left untouched")
    print("The question is in the request's '## Blocked On' section. Answer it, then:")
    print(f"  request unpark {args.request_id} --answer \"...\"")
    return 0


def cmd_unpark(args: argparse.Namespace) -> int:
    root = _root(args)
    path = validate.find_request(args.request_id, root=root)
    if path is None:
        print(f"no request file for {args.request_id} under {root}", file=sys.stderr)
        return 2
    if path.parent.name != "parked":
        print(f"{args.request_id} is in {path.parent.name}/, not parked/", file=sys.stderr)
        return 2

    text = path.read_text(encoding="utf-8")
    previous = PREVIOUS_STATUS_KEY.findall(render.split_header_region(text))
    restore = previous[-1].strip() if previous else None

    entry = "\n".join(
        [
            f"### Answered {_stamp()}",
            "",
            "Answer:",
            "",
            render.blockquote(args.answer),
            "",
            "Returned to approved/ with the status it had before it was parked.",
        ]
    )
    try:
        # APPEND. The question above it is the reason this answer means
        # anything, and the pair of them is the record of the decision.
        text = render.append_section(text, BLOCKED_ON, entry)
    except ValueError as exc:
        print(f"cannot unpark {args.request_id}: {exc}", file=sys.stderr)
        return 1

    if restore:
        # RESTORING a status a human previously set and the file recorded — not
        # granting one. When nothing was recorded, the file keeps 'Parked' and
        # the validator refuses it until Jake approves it by hand, which is the
        # correct outcome and not a bug.
        text = _set_status_or_warn(text, restore)
    else:
        print("  no previous status recorded — leaving 'Status: Parked'", file=sys.stderr)
        print("  approve it by hand before the next run can pick it up", file=sys.stderr)
    path.write_text(text, encoding="utf-8")

    target = root / "approved" / path.name
    target.parent.mkdir(parents=True, exist_ok=True)
    path.replace(target)

    worklog.update(
        paths.work_log_path(root=root),
        args.request_id,
        {"status": restore or render.STATUS_PARKED, "notes": ""},
    )
    print(f"unparked {args.request_id} -> approved/{target.name}")
    print(f"status restored to '{restore or render.STATUS_PARKED}'")
    return 0


# --- argument parsing --------------------------------------------------------


def _positive_int(value: str) -> int:
    """``--limit 0`` would make a scheduled run a no-op that looks like it worked."""
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="request",
        description="Local change-request intake. Import never approves anything.",
    )
    parser.add_argument(
        "--home",
        default=None,
        help="override the change-request data folder (same as the CR_HOME env var)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup = subparsers.add_parser("setup", help="create the folder skeleton and work log")
    setup.add_argument(
        "--with-local-history",
        action="store_true",
        help="also 'git init' the data folder for LOCAL history (never add a remote)",
    )
    setup.set_defaults(func=cmd_setup)

    importer = subparsers.add_parser("import", help="turn inbox-msg/*.msg into ready/*.md")
    importer.add_argument("--dry-run", action="store_true", help="report, write nothing")
    importer.set_defaults(func=cmd_import)

    lister = subparsers.add_parser("list", help="list every request and its status")
    lister.add_argument("--status", default=None, help="filter on the Status: field")
    lister.set_defaults(func=cmd_list)

    validator = subparsers.add_parser("validate", help="check a request is genuinely approved")
    validator.add_argument("request_id")
    validator.set_defaults(func=cmd_validate)

    starter = subparsers.add_parser("start", help="validate, branch, and clock in")
    starter.add_argument("request_id")
    starter.add_argument(
        "--repo",
        required=True,
        choices=list(paths.KNOWN_REPOS),
        help="which repository the branch belongs in",
    )
    starter.add_argument(
        "--no-branch", action="store_true", help="record the branch name without creating it"
    )
    starter.set_defaults(func=cmd_start)

    completer = subparsers.add_parser("complete", help="clock out and record the runtime")
    completer.add_argument("request_id")
    completer.add_argument("--notes", default=None, help="one short note for the work log")
    completer.add_argument(
        "--move-to-completed",
        action="store_true",
        help="move the file to completed/ — only after Jake confirms",
    )
    completer.set_defaults(func=cmd_complete)

    timer = subparsers.add_parser("log-time", help="record Jake's minutes (never Claude's)")
    timer.add_argument("request_id")
    for flag, _column, label in JAKE_TIME_FIELDS:
        timer.add_argument(f"--{flag}", type=int, default=None, help=f"minutes: {label}")
    timer.set_defaults(func=cmd_log_time)

    nexter = subparsers.add_parser(
        "next",
        help="the batch command: pick up approved work (approved/ ONLY)",
        description=(
            "Import, then select work from approved/ and ONLY approved/. Never reads "
            "inbox-msg/ or ready/: a file reaches approved/ only because a human read "
            "it, wrote acceptance criteria and moved it there."
        ),
    )
    mode = nexter.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="the DEFAULT: report what would be worked and change nothing",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="clock the selected requests in (branch, timestamp, CSV row) and print the plan",
    )
    nexter.add_argument(
        "--limit",
        type=_positive_int,
        default=1,
        help=(
            "how many requests one run may pick up (default 1). A run that quietly "
            "took on nine is how a batch becomes unreviewable."
        ),
    )
    nexter.add_argument(
        "--repo",
        default=paths.KNOWN_REPOS[0],
        choices=list(paths.KNOWN_REPOS),
        help=(
            f"default repo for branch creation (default {paths.KNOWN_REPOS[0]}); a "
            "request may override it with a 'Target Repo:' line"
        ),
    )
    nexter.add_argument(
        "--no-branch",
        action="store_true",
        help="record branch names without creating them",
    )
    nexter.set_defaults(func=cmd_next)

    parker = subparsers.add_parser(
        "park",
        help="stop cleanly on a blocking question instead of guessing",
        description=(
            "Move a request to parked/, write the blocking question into it, and set "
            "Status: Parked. The branch and every commit on it are left untouched. A "
            "request that cannot be completed cleanly is parked, never half-implemented."
        ),
    )
    parker.add_argument("request_id")
    parker.add_argument(
        "--reason",
        required=True,
        help="the blocking question, in Jake's words or yours — required",
    )
    parker.set_defaults(func=cmd_park)

    unparker = subparsers.add_parser(
        "unpark",
        help="return a parked request to approved/ once it has an answer",
    )
    unparker.add_argument("request_id")
    unparker.add_argument(
        "--answer",
        required=True,
        help="Jake's answer — APPENDED below the question, never replacing it",
    )
    unparker.set_defaults(func=cmd_unpark)

    return parser


def main(argv: list[str] | None = None) -> int:
    # The refusal messages quote the sentinel, which carries an em dash, and a
    # Windows console defaults to a code page that cannot encode it. Without
    # this, a legitimate refusal would surface as a UnicodeEncodeError
    # traceback — the one moment the message actually matters.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
