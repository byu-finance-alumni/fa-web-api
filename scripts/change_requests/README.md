# `scripts/change_requests/`

Local change-request intake. Outlook `.msg` in, reviewed Markdown out, a human
in between.

Full documentation — folder layout, command reference, the workflow for
processing an approved request, and the trust boundary — lives in
[`docs/CHANGE-REQUESTS.md`](../../docs/CHANGE-REQUESTS.md). This file is the
map of the code.

## The two rules

**Importing an email must never approve anything.** `import` writes
`Status: Ready for Review` and `Approved for Claude: No` as literal template
text. There is no argument, no parsed field, and no email body that can change
either one. Approval is Jake editing the file and moving it into `approved/`,
and `validate` refuses everything else.

**The batch command reads `approved/` and nothing else.** `request next` is
what a scheduled, unattended run invokes. It never looks at `inbox-msg/` or
`ready/`, it never writes code, and when it cannot finish a request cleanly the
answer is `request park`, never a half-implementation. See
[`docs/CHANGE-REQUESTS.md`](../../docs/CHANGE-REQUESTS.md) section 5.

## Modules

| Module | What it owns |
| --- | --- |
| `cli.py` | argparse entry point: `setup import list validate start complete log-time next park unpark` |
| `paths.py` | where the DATA folder is, and why it is outside both repos |
| `msg_reader.py` | `.msg` -> dataclass; the **lazy** `extract_msg` import lives here |
| `sanitize.py` | invisible/bidi stripping, newline normalisation, fence sizing, slugs, truncation |
| `injection.py` | the heuristic scan — flags, never blocks |
| `attachments.py` | blocklist, path-traversal defence, SHA-256; bytes to disk and nothing more |
| `render.py` | the template, the quarantine block, and reading a rendered file back |
| `validate.py` | the approval gate — every check is a refusal |
| `ledger.py` | `.imported.json`, so a re-drag of the same email is a no-op |
| `worklog.py` | `work-log.csv`: formula-injection guard, and the blank-not-zero rule |
| `digest.py` | `runs/YYYY-MM-DD-HHMM.md` — the unattended-run record, PII-scrubbed |
| `templates/change-request.md.tmpl` | Jake's specification, field for field |

## The scheduled half

Two PowerShell scripts sit beside this package, not in it:

| Script | What it does |
| --- | --- |
| `scripts/change-requests-scheduled.ps1` | one unattended run: `import` then `next`, a log under `runs/`, and a silent exit when there is nothing to do |
| `scripts/change-requests-install-task.ps1` | registers/removes the twice-daily Windows Scheduled Task; `-WhatIf`-friendly |

Both default to a dry run. Neither ever pushes.

## Data lives outside the repo

Requests are never committed. The data folder resolves to the **workspace
root** — beside `fa-web-api` and `fa-web-app` — so that it is the same folder
from every branch and every worktree of either repo, and so that it cannot be
committed by accident (the workspace root is not a git repository).

Override with `CR_HOME` or `--home` when testing.

## Dependency

`extract-msg` is in **`requirements-dev.txt` only**, and is imported lazily
inside `msg_reader.read_msg`. It must never reach `pyproject.toml`: Vercel's
Python builder installs only `[project.dependencies]`, so putting it there
would ship an unused GPLv3 MAPI parser into the production function and force a
`uv.lock` regeneration whose failure mode is a 500 on every request.

```
pip install -r requirements-dev.txt
```

Everything except `.msg` parsing works without it, which is why most of the
test suite runs on a machine that has never installed it.

## Tests

- `tests/test_change_requests.py` — rendering, fence escaping, id allocation,
  dedupe, work-log arithmetic, validator refusals, and the batch command
  (`next` selection, `--limit`, the park/unpark round trip).
- `tests/test_change_request_security.py` — attachment blocklist, path
  traversal, injection quarantine, CSV formula injection, digest scrubbing,
  and a source-level invariant that no module here imports a network library.

## Commit convention

Every commit implementing a request ends its first line with the id:

```
Add graduation-year filter to the alumni report (CR-2026-001)
```

The requests themselves are never committed, so that trailer is the only
permanent, PII-free record of what was approved and when.
