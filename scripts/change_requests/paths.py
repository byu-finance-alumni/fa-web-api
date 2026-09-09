"""Where the change-request DATA lives, and why it is not in either repo.

The scripts are committed to ``fa-web-api``. The requests themselves are NOT,
and they never can be: they hold an alumnus's or a colleague's email body,
their address, and whatever they attached. That is exactly the material that
must not reach a git remote.

The obvious answer — a gitignored folder inside the repo — is wrong here, and
wrong in a way that fails silently. Both repos use ``.worktrees/`` heavily, and
a gitignored folder inside the main checkout DOES NOT EXIST inside a worktree
checkout. The CLI would then see an empty inbox, report "0 requests imported",
and be telling the truth about the wrong directory. Nobody would notice.

So the data folder sits at the WORKSPACE ROOT, next to ``fa-web-api`` and
``fa-web-app``. It is stable across every branch and every worktree of both
repos, and it cannot be committed by accident because the workspace root is not
a git repository at all.

Resolution order:

1. ``CR_HOME`` environment variable, if set (absolute or ``~``-relative).
2. The nearest ancestor of the repo root that contains a ``fa-web-app``
   directory — ``<that ancestor>/change-requests``. For a normal checkout that
   ancestor IS the immediate parent of the repo root. For a worktree at
   ``fa-web-api/.worktrees/<name>`` the walk continues up two more levels and
   lands on the same workspace root, which is the entire point.
3. Otherwise ``<repo root>/change-requests`` — a last-resort fallback for a
   checkout standing on its own.
"""

from __future__ import annotations

import os
import pathlib

#: ``scripts/change_requests/paths.py`` -> ``scripts/change_requests`` ->
#: ``scripts`` -> the repo root.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Sibling repositories that make up the workspace. ``fa-web-app`` is the
#: marker we look for when walking up, because ``fa-web-api`` is the repo we
#: are already standing in and would match a worktree parent too eagerly.
WORKSPACE_MARKER = "fa-web-app"

#: Repos ``request start`` is allowed to create a branch in.
KNOWN_REPOS = ("fa-web-api", "fa-web-app")

#: Every folder ``request setup`` creates. Order is the lifecycle order.
DATA_FOLDERS = (
    "inbox-msg",
    "ready",
    "approved",
    "completed",
    "rejected",
    "attachments",
    "templates",
)

WORK_LOG_NAME = "work-log.csv"
LEDGER_NAME = ".imported.json"
TEMPLATE_NAME = "change-request.md.tmpl"


def workspace_root() -> pathlib.Path:
    """The directory that holds ``fa-web-api`` and ``fa-web-app`` side by side.

    Falls back to the repo root when no such ancestor exists.
    """
    for candidate in (REPO_ROOT, *REPO_ROOT.parents):
        if (candidate / WORKSPACE_MARKER).is_dir():
            return candidate
    return REPO_ROOT


def home() -> pathlib.Path:
    """Absolute path of the change-request data folder."""
    override = os.environ.get("CR_HOME")
    if override:
        return pathlib.Path(override).expanduser().resolve()
    return (workspace_root() / "change-requests").resolve()


def folder(name: str, *, root: pathlib.Path | None = None) -> pathlib.Path:
    """One of :data:`DATA_FOLDERS`, resolved under the data home."""
    if name not in DATA_FOLDERS:
        raise KeyError(f"unknown change-request folder: {name!r}")
    return (root or home()) / name


def work_log_path(*, root: pathlib.Path | None = None) -> pathlib.Path:
    return (root or home()) / WORK_LOG_NAME


def ledger_path(*, root: pathlib.Path | None = None) -> pathlib.Path:
    """The dedupe ledger. Lives beside the ``.msg`` files it describes."""
    return folder("inbox-msg", root=root) / LEDGER_NAME


def packaged_template() -> pathlib.Path:
    """The template shipped with the scripts (the fallback / the original)."""
    return pathlib.Path(__file__).resolve().parent / "templates" / TEMPLATE_NAME


def template_path(*, root: pathlib.Path | None = None) -> pathlib.Path:
    """Jake's editable copy of the template, if he has one; else the packaged one."""
    local = folder("templates", root=root) / TEMPLATE_NAME
    return local if local.is_file() else packaged_template()


def repo_dir(name: str) -> pathlib.Path:
    """The MAIN checkout of a sibling repo, for branch creation.

    Deliberately resolved from the workspace root rather than from wherever the
    CLI happens to be running, so a worktree does not get a branch created in
    itself.
    """
    if name not in KNOWN_REPOS:
        raise KeyError(f"unknown repo: {name!r} (expected one of {', '.join(KNOWN_REPOS)})")
    return workspace_root() / name


def ensure_layout(root: pathlib.Path) -> list[pathlib.Path]:
    """Create the folder skeleton. Idempotent; never overwrites anything."""
    created: list[pathlib.Path] = []
    for name in (root, *(root / f for f in DATA_FOLDERS)):
        if not name.exists():
            name.mkdir(parents=True, exist_ok=True)
            created.append(name)
    return created
