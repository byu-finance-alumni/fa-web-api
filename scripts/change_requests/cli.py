"""Change-request intake CLI — the seven commands Jake actually types.

Run it the way every other script in this repo runs::

    python -m scripts.change_requests.cli setup
    python -m scripts.change_requests.cli import
    python -m scripts.change_requests.cli list
    python -m scripts.change_requests.cli validate CR-2026-001
    python -m scripts.change_requests.cli start CR-2026-001 --repo fa-web-api
    python -m scripts.change_requests.cli complete CR-2026-001
    python -m scripts.change_requests.cli log-time CR-2026-001 --testing 15

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
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import subprocess
import sys

from . import attachments, injection, ledger, paths, render, sanitize, validate, worklog

TIMESTAMP_FMT = "%Y-%m-%d %H:%M"

#: CSV column, Time Log label, and the ``log-time`` flag that fills them.
JAKE_TIME_FIELDS = (
    ("request-review", "jake_request_review_minutes", "Jake request review"),
    ("testing", "jake_testing_minutes", "Jake testing"),
    ("correction", "jake_correction_minutes", "Jake corrections"),
    ("deployment", "jake_deployment_minutes", "Jake deployment"),
)

LIFECYCLE_FOLDERS = ("ready", "approved", "completed", "rejected")


def _now() -> dt.datetime:
    return dt.datetime.now()


def _stamp(when: dt.datetime | None = None) -> str:
    return (when or _now()).strftime(TIMESTAMP_FMT)


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
    root = pathlib.Path(args.home).resolve() if args.home else paths.home()
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

    digest = ledger.file_hash(source)
    if digest in entries:
        print(f"  skip {source.name} — already imported as {entries[digest].request_id}")
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
            source_sha256=digest,
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


def cmd_import(args: argparse.Namespace) -> int:
    root = pathlib.Path(args.home).resolve() if args.home else paths.home()
    inbox = root / "inbox-msg"
    if not inbox.is_dir():
        print(f"no inbox at {inbox} — run 'setup' first", file=sys.stderr)
        return 2

    worklog.ensure(paths.work_log_path(root=root))
    entries = ledger.load(paths.ledger_path(root=root))
    sources = sorted(p for p in inbox.iterdir() if p.is_file() and p.suffix.lower() == ".msg")

    print(f"inbox: {inbox} ({len(sources)} .msg file(s))")
    imported = 0
    for source in sources:
        if _import_one(source, root=root, entries=entries, dry_run=args.dry_run) is not None:
            imported += 1

    print(f"{imported} imported, {len(sources) - imported} skipped or failed")
    if imported and not args.dry_run:
        print("Nothing is approved. Review each file in ready/, then move it to approved/.")
    return 0


# --- list --------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    root = pathlib.Path(args.home).resolve() if args.home else paths.home()
    rows: list[tuple[str, str, str, str]] = []
    for folder_name in LIFECYCLE_FOLDERS:
        folder = root / folder_name
        if not folder.is_dir():
            continue
        for entry in sorted(folder.glob("CR-*.md")):
            text = entry.read_text(encoding="utf-8", errors="replace")
            trusted = render.split_trusted(text)
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
    root = pathlib.Path(args.home).resolve() if args.home else paths.home()
    result = validate.validate(args.request_id, root=root)
    print(result.render())
    return 0 if result.ok else 1


# --- start -------------------------------------------------------------------


def branch_name(request_id: str, path: pathlib.Path) -> str:
    match = render.FILENAME_RE.match(path.name)
    slug = match.group(2) if match else "request"
    return f"cr/{request_id}-{slug}"


def _create_branch(repo: pathlib.Path, branch: str) -> tuple[bool, str]:
    if not (repo / ".git").exists():
        return False, f"{repo} is not a git checkout"
    existing = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", branch],
        capture_output=True,
        text=True,
    )
    if existing.returncode == 0:
        return True, f"branch {branch} already exists in {repo.name}"
    result = subprocess.run(
        ["git", "-C", str(repo), "checkout", "-b", branch],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, result.stderr.strip() or "git checkout -b failed"
    return True, f"created branch {branch} in {repo.name}"


def cmd_start(args: argparse.Namespace) -> int:
    root = pathlib.Path(args.home).resolve() if args.home else paths.home()
    result = validate.validate(args.request_id, root=root)
    if not result.ok:
        print(result.render(), file=sys.stderr)
        print("\nrefusing to start — nothing has been changed.", file=sys.stderr)
        return 1
    print(result.render())

    path = result.path
    assert path is not None
    branch = branch_name(args.request_id, path)
    started = _stamp()

    if args.no_branch:
        print(f"branch not created (--no-branch); recording {branch}")
    else:
        ok, message = _create_branch(paths.repo_dir(args.repo), branch)
        print(f"  {message}")
        if not ok:
            print("refusing to start — the branch could not be created.", file=sys.stderr)
            return 1

    text = path.read_text(encoding="utf-8")
    text = render.replace_section(
        text,
        "Claude Implementation",
        f"Branch: `{branch}` in `{args.repo}`\nStarted: {started}\n\nIn progress.",
    )
    text = render.set_time_log(text, "Claude started", started)
    path.write_text(text, encoding="utf-8")

    worklog.update(
        paths.work_log_path(root=root),
        args.request_id,
        {"claude_started": started, "status": "In Progress", "branch": branch},
    )
    print(f"started {args.request_id} on {branch}")
    return 0


# --- complete ----------------------------------------------------------------


def cmd_complete(args: argparse.Namespace) -> int:
    root = pathlib.Path(args.home).resolve() if args.home else paths.home()
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
    root = pathlib.Path(args.home).resolve() if args.home else paths.home()
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


# --- argument parsing --------------------------------------------------------


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
