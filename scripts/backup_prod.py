"""Full backup of the PRODUCTION Supabase project (api #535).

Run by hand, or every week by the Windows scheduled task that
``scripts/backup-scheduled.ps1`` wraps around it (``--incremental --keep 8``).

Captures the three things a restore needs, stamped as one matched set:

    <BACKUP_DIR>/<UTC timestamp>Z/
      database.dump            pg_dump custom format, --schema=public
      auth.sql                 pg_dump plain SQL, --schema=auth (staff logins)
      storage/headshots/...    every object in the `headshots` bucket
      MANIFEST.json            sizes, sha256s, counts, versions, duration, checks

Then it VERIFIES the result and exits non-zero if anything is off, because a
truncated or empty dump, or one taken from the wrong project, exits 0 from
pg_dump and looks fine until the day it is needed:

  * `pg_restore --list database.dump` parses and lists public.alumni
  * the live alumni row count is >= ALUMNI_ROW_FLOOR (a floor, not a figure)
  * the object count and total bytes downloaded equal what the bucket listing
    reported, and every object's size matches its listing entry

Configuration is environment variables only (see docs/BACKUPS.md):

  BACKUP_DATABASE_URL                 SESSION-pooler URL (port 5432). :6543 is
                                      refused: pg_dump needs session features.
  BACKUP_SUPABASE_URL                 https://<project-ref>.supabase.co
  BACKUP_SUPABASE_SERVICE_ROLE_KEY    service-role key (bucket is private)
  BACKUP_DIR                          destination root, OUTSIDE the repo
  BACKUP_EXPECT_PROJECT_REF           e.g. njobhhdopwdodvzosrns
  BACKUP_SLACK_WEBHOOK_URL            optional; one line posted on FAILURE only

The project ref is asserted against BOTH the database host and the Supabase URL
before anything is dumped. Base URLs in this project are easy to get backwards
(the keepalive workflow asserts its environment for the same reason), and a
"backup" of dev would be worse than no backup because it would look fine.

Flags (all off by default, so a hand run behaves exactly as before):

  --incremental   download only bucket objects that are NEW or CHANGED since the
                  most recent previous run whose MANIFEST says "ok"; copy the
                  unchanged ones from that run's folder (size and sha256 checked
                  against the previous manifest first, otherwise re-download).
                  Every run folder is still complete on its own. The DATABASE is
                  always dumped in full: it is ~40 MB and change detection would
                  cost more than it saves.
  --keep N        AFTER a run finished OK, delete the oldest run folders so that
                  at most N OK runs remain. Never deletes the run just written,
                  the newest OK run, a FAILED run, or a folder without a
                  manifest; refuses a BACKUP_DIR that is a drive root or inside
                  the repo. Prints every folder it deletes.
  --dry-run       validate configuration and tooling, print the incremental and
                  pruning plan, connect to nothing, write nothing.

Every run leaves BACKUP_DIR/LAST-RUN.json behind; a failed run also leaves
BACKUP_DIR/LAST-RUN-FAILED.txt (removed again by the next success) and, when
BACKUP_SLACK_WEBHOOK_URL is set, posts one redacted line to Slack. Nothing is
posted on success.

Deliberately stdlib-only so it runs under any Python 3.12+ on the machine (no
venv needed) and adds nothing to the deployed function's dependencies.
pg_dump / pg_restore / psql are taken from PATH; PostgreSQL 17 is installed on
the Windows machine this was written for.

Secrets are never printed. Every message that could carry the database URL,
the service key or the webhook URL passes through `redact()` first. gitleaks
scans history, so a single careless echo would be permanent.

Phases 1, 2, 4 and 5 of docs/BACKUPS-PLAN.md. Phase 3 (the restore rehearsal)
is a human task and has not been done.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

# --- Constants ---------------------------------------------------------------

BUCKET = "headshots"

#: The alumni count must be at least this or the run fails. ~1,500 real alumni
#: were loaded in 2026-08; a dump that says 12 was taken from the wrong place.
ALUMNI_ROW_FLOOR = 1400

#: Supabase's list endpoint pages at 100 by default; ask explicitly.
LIST_PAGE_SIZE = 100

#: Guard against an endless listing loop (a bug, a bucket that never ends).
MAX_LIST_PAGES = 500

#: The only port pg_dump may use here: the SESSION pooler. The transaction
#: pooler (:6543) lacks the session-level features pg_dump depends on.
SESSION_POOLER_PORT = 5432
TRANSACTION_POOLER_PORT = 6543

REQUIRED_ENV = (
    "BACKUP_DATABASE_URL",
    "BACKUP_SUPABASE_URL",
    "BACKUP_SUPABASE_SERVICE_ROLE_KEY",
    "BACKUP_DIR",
    "BACKUP_EXPECT_PROJECT_REF",
)

REQUIRED_TOOLS = ("pg_dump", "pg_restore", "psql")

HTTP_TIMEOUT_SECONDS = 60
HTTP_RETRIES = 3

#: Row counts recorded in the manifest. Only public.alumni is ASSERTED (the
#: floor above); the rest are informational and a failure to count one is
#: recorded as null rather than failing the run. survey_* are here because the
#: first run of this script is the backup taken BEFORE a survey-state reset.
RECORDED_TABLES = (
    "public.alumni",
    "public.survey_responses",
    "public.survey_send_log",
    "auth.users",
)

#: 2 added the per-object ``etag`` / ``updated_at`` change markers and the
#: incremental bookkeeping under ``storage``. A version-1 manifest is still a
#: valid previous run: its objects just cannot be proven unchanged, so they are
#: re-downloaded once and the run after that is incremental.
MANIFEST_SCHEMA_VERSION = 2

#: Run folders are named by ``utc_stamp``; only folders that look like one are
#: ever considered for reuse or deletion.
RUN_FOLDER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}Z$")

FAILED_MARKER_NAME = "LAST-RUN-FAILED.txt"
LAST_RUN_NAME = "LAST-RUN.json"

WEBHOOK_TIMEOUT_SECONDS = 10
#: The Slack line carries the first failure only, cut to this many characters.
WEBHOOK_REASON_MAX_CHARS = 240


# --- Errors -----------------------------------------------------------------


class BackupError(Exception):
    """Any failure that must stop the run. The message is safe to print."""


class ConfigError(BackupError):
    """Bad or missing configuration; nothing has been contacted."""


class CheckError(BackupError):
    """A Phase 2 verification failed; the artifact must not be trusted."""


# --- Configuration -----------------------------------------------------------


@dataclass(frozen=True)
class BackupConfig:
    database_url: str
    supabase_url: str
    service_role_key: str
    backup_dir: pathlib.Path
    expect_project_ref: str
    #: A Slack incoming-webhook URL is a credential (anyone holding it can post
    #: to the channel), so it is redacted like the key and never logged.
    slack_webhook_url: str | None = None

    @property
    def secrets(self) -> tuple[str, ...]:
        """Every string that must never appear in output."""
        out = [self.database_url, self.service_role_key, self.slack_webhook_url or ""]
        parsed = urllib.parse.urlsplit(self.database_url)
        if parsed.password:
            out.append(parsed.password)
            # A password can be percent-encoded in the URL and decoded by libpq;
            # redact both spellings.
            out.append(urllib.parse.unquote(parsed.password))
        return tuple(s for s in out if s)


#: auth-schema tables whose DATA is never dumped. They hold live session and
#: second-factor material (refresh tokens, sessions, TOTP secrets, one-time
#: tokens, in-flight OAuth/SAML state). A restore does not need them — staff
#: sign in again and re-enrol MFA — and a backup folder holding them would be a
#: ready-made account-takeover kit for every staff login. Table structure is
#: still dumped; only the rows are excluded.
AUTH_SESSION_TABLES: tuple[str, ...] = (
    "refresh_tokens",
    "sessions",
    "mfa_factors",
    "mfa_challenges",
    "mfa_amr_claims",
    "one_time_tokens",
    "flow_state",
    "saml_relay_states",
    "audit_log_entries",
)


# Folder names and env vars that mark a cloud-synced location. Documents and
# Desktop on a managed Windows machine are commonly redirected into OneDrive,
# so "outside the repo" alone is not enough of a check for a full PII dump.
_SYNC_FOLDER_NAMES = ("onedrive", "dropbox", "icloud drive", "iclouddrive", "google drive", "box")
_SYNC_ENV_VARS = ("OneDrive", "OneDriveCommercial", "OneDriveConsumer")


def _cloud_sync_marker(path: pathlib.Path, environ: Mapping[str, str]) -> str | None:
    """Why ``path`` looks cloud-synced, or ``None`` if it does not."""
    resolved = path.resolve()
    for name in _SYNC_ENV_VARS:
        root = environ.get(name)
        if not root:
            continue
        try:
            resolved.relative_to(pathlib.Path(root).resolve())
        except (ValueError, OSError):
            continue
        return f"inside %{name}%"
    for part in resolved.parts:
        if part.lower() in _SYNC_FOLDER_NAMES:
            return f"path segment '{part}'"
    return None


def load_config(env: Mapping[str, str], *, repo_root: pathlib.Path | None = None) -> BackupConfig:
    """Read and validate configuration from ``env``. Contacts nothing."""
    missing = [name for name in REQUIRED_ENV if not (env.get(name) or "").strip()]
    if missing:
        raise ConfigError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". See docs/BACKUPS.md."
        )

    database_url = env["BACKUP_DATABASE_URL"].strip()
    supabase_url = env["BACKUP_SUPABASE_URL"].strip()
    service_role_key = env["BACKUP_SUPABASE_SERVICE_ROLE_KEY"].strip()
    backup_dir = pathlib.Path(env["BACKUP_DIR"].strip()).expanduser()
    expect_project_ref = env["BACKUP_EXPECT_PROJECT_REF"].strip()

    check_database_url(database_url)

    parsed_supabase = urllib.parse.urlsplit(supabase_url)
    if parsed_supabase.scheme != "https" or not parsed_supabase.hostname:
        raise ConfigError(
            "BACKUP_SUPABASE_URL must be an https:// URL (https://<ref>.supabase.co)."
        )

    if not expect_project_ref.isalnum():
        raise ConfigError(
            "BACKUP_EXPECT_PROJECT_REF must be the bare project ref (letters and digits only)."
        )

    if not backup_dir.is_absolute():
        raise ConfigError("BACKUP_DIR must be an absolute path.")
    root = (repo_root or _repo_root()).resolve()
    try:
        backup_dir.resolve().relative_to(root)
    except ValueError:
        pass  # outside the repo: good
    else:
        raise ConfigError(
            "BACKUP_DIR must be OUTSIDE the repository. A dump is every alumnus in one "
            "file and must never sit where a git command could pick it up."
        )
    synced = _cloud_sync_marker(backup_dir, env)
    if synced and env.get("BACKUP_ALLOW_SYNCED_DIR") != "1":
        raise ConfigError(
            f"BACKUP_DIR looks like a cloud-synced folder ({synced}). A dump of every "
            "alumnus plus the staff auth schema must not replicate to a sync service. "
            "Choose a local, unsynced folder, or set BACKUP_ALLOW_SYNCED_DIR=1 if that "
            "is a deliberate, approved destination."
        )

    webhook = (env.get("BACKUP_SLACK_WEBHOOK_URL") or "").strip() or None
    if webhook is not None and not webhook.startswith("https://"):
        raise ConfigError("BACKUP_SLACK_WEBHOOK_URL must be an https:// URL (or unset).")

    cfg = BackupConfig(
        database_url=database_url,
        supabase_url=supabase_url,
        service_role_key=service_role_key,
        backup_dir=backup_dir,
        expect_project_ref=expect_project_ref,
        slack_webhook_url=webhook,
    )
    assert_project_ref(cfg)
    return cfg


def check_database_url(database_url: str) -> None:
    """Refuse anything but a postgres URL on the SESSION pooler port."""
    parsed = urllib.parse.urlsplit(database_url)
    if parsed.scheme not in ("postgresql", "postgres"):
        raise ConfigError("BACKUP_DATABASE_URL must start with postgresql:// (a libpq URL).")
    if not parsed.hostname:
        raise ConfigError("BACKUP_DATABASE_URL has no host.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConfigError("BACKUP_DATABASE_URL has an unreadable port.") from exc
    if port == TRANSACTION_POOLER_PORT:
        raise ConfigError(
            f"BACKUP_DATABASE_URL uses port {TRANSACTION_POOLER_PORT} (the TRANSACTION pooler). "
            "pg_dump needs session-level features the transaction pooler does not provide. "
            f"Use the SESSION pooler connection string on port {SESSION_POOLER_PORT} instead "
            "(Supabase dashboard: Connect -> Session pooler)."
        )
    if port != SESSION_POOLER_PORT:
        raise ConfigError(
            f"BACKUP_DATABASE_URL must use port {SESSION_POOLER_PORT} (the SESSION pooler); "
            f"got {port!r}."
        )


def _has_ref_segment(value: str, ref: str) -> bool:
    """True when ``ref`` is a whole dot-separated segment of ``value``.

    Whole-segment, not substring: ``abc`` must not accept ``abcdef`` as its
    host, and this check is the one the script's safety model leans on.
    """
    return ref.lower() in value.lower().split(".")


def assert_project_ref(cfg: BackupConfig) -> None:
    """Both the DB host and the Supabase URL must name the expected project.

    Mirrors the keepalive workflow's "confirm the target is the expected
    environment" step: one wrong variable and this would faithfully back up the
    dev sandbox while reporting success.
    """
    ref = cfg.expect_project_ref
    db_host = (urllib.parse.urlsplit(cfg.database_url).hostname or "").lower()
    db_user = urllib.parse.urlsplit(cfg.database_url).username or ""
    # Supavisor pooler hosts look like aws-0-us-east-1.pooler.supabase.com and
    # carry the ref in the USERNAME (postgres.<ref>); direct hosts look like
    # db.<ref>.supabase.co and carry it in the host. Accept either place.
    if not _has_ref_segment(db_host, ref) and not _has_ref_segment(db_user, ref):
        raise ConfigError(
            f"BACKUP_DATABASE_URL does not reference project {ref!r} (checked the host and "
            "the username). Wrong project, wrong variable, or a stale connection string."
        )
    supa_host = (urllib.parse.urlsplit(cfg.supabase_url).hostname or "").lower()
    if not _has_ref_segment(supa_host, ref):
        raise ConfigError(
            f"BACKUP_SUPABASE_URL host does not contain project ref {ref!r}. Expected "
            f"https://{ref}.supabase.co."
        )


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent


# --- Redaction ---------------------------------------------------------------


def redact(text: str, secrets: Iterable[str]) -> str:
    """Replace every secret (longest first) with a fixed marker."""
    out = text
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        out = out.replace(secret, "[REDACTED]")
    return out


# --- Tools -------------------------------------------------------------------


def find_tools(which: Callable[[str], str | None] = shutil.which) -> dict[str, str]:
    """Locate pg_dump / pg_restore / psql on PATH; fail naming what is missing."""
    found: dict[str, str] = {}
    missing: list[str] = []
    for name in REQUIRED_TOOLS:
        path = which(name)
        if path:
            found[name] = path
        else:
            missing.append(name)
    if missing:
        raise ConfigError(
            "Not on PATH: "
            + ", ".join(missing)
            + ". Install the PostgreSQL client tools (PostgreSQL 17 on Windows puts them in "
            "C:\\Program Files\\PostgreSQL\\17\\bin) and make sure that folder is on PATH."
        )
    return found


def tool_version(path: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed args, no shell
        [path, "--version"], capture_output=True, text=True, check=False
    )
    return (result.stdout or result.stderr).strip()


# --- Storage listing (pure logic, injectable fetcher) -----------------------


@dataclass(frozen=True)
class StorageObject:
    path: str  # full object key within the bucket, e.g. "survey-pending/abc.jpg"
    size: int
    #: Change markers from the listing, used by ``--incremental`` to decide
    #: whether an object has to be downloaded again. Supabase returns the etag
    #: under ``metadata.eTag`` and the modification time as ``updated_at`` on the
    #: row (``metadata.lastModified`` on older storage versions). Either may be
    #: absent; the comparison copes with that (see ``plan_incremental``).
    etag: str | None = None
    updated_at: str | None = None


ListPageFn = Callable[[str, int, int], list[dict]]
"""(prefix, limit, offset) -> one page of raw listing rows."""


def _clean_marker(value: object) -> str | None:
    """A non-empty string marker, with the quotes an HTTP etag carries removed."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip().strip('"')
    return cleaned or None


def walk_bucket(list_page: ListPageFn, *, page_size: int = LIST_PAGE_SIZE) -> list[StorageObject]:
    """Every OBJECT in the bucket, descending into virtual folders.

    Supabase's list endpoint is one level deep: a row whose ``metadata`` is
    ``None`` (and ``id`` is ``None``) is a folder placeholder synthesised per
    path segment, not a file. Each folder is listed in turn with its path as the
    prefix. Names in a page are relative to the prefix asked for.
    """
    objects: list[StorageObject] = []
    pending = [""]
    pages = 0
    while pending:
        prefix = pending.pop()
        offset = 0
        while True:
            pages += 1
            if pages > MAX_LIST_PAGES:
                raise CheckError(
                    f"Bucket listing exceeded {MAX_LIST_PAGES} pages; refusing to loop forever."
                )
            rows = list_page(prefix, page_size, offset)
            if not isinstance(rows, list):
                raise CheckError("Bucket listing returned something that is not a list.")
            for row in rows:
                name = (row.get("name") or "").strip() if isinstance(row, Mapping) else ""
                if not name:
                    continue
                full = _join_key(prefix, name)
                metadata = row.get("metadata")
                if not isinstance(metadata, Mapping):
                    pending.append(full.rstrip("/") + "/")
                    continue
                size = metadata.get("size")
                if not isinstance(size, int) or size < 0:
                    raise CheckError(
                        f"Bucket listing has no size for {full!r}; cannot verify the download."
                    )
                etag = _clean_marker(metadata.get("eTag")) or _clean_marker(metadata.get("etag"))
                updated_at = _clean_marker(row.get("updated_at")) or _clean_marker(
                    metadata.get("lastModified")
                )
                objects.append(
                    StorageObject(path=full, size=size, etag=etag, updated_at=updated_at)
                )
            if len(rows) < page_size:
                break
            offset += len(rows)
    objects.sort(key=lambda o: o.path)
    return objects


def _join_key(prefix: str, name: str) -> str:
    """``prefix`` ends with "/" (or is empty). Tolerate a name that already
    carries the prefix, which some storage versions return."""
    if not prefix:
        return name
    if name.startswith(prefix):
        return name
    return prefix + name


def safe_object_destination(root: pathlib.Path, key: str) -> pathlib.Path:
    """Where an object lands on disk, refusing anything that escapes ``root``.

    Object keys come from the bucket, which staff and survey uploads write to;
    treat them as untrusted path fragments.
    """
    if not key or key.startswith(("/", "\\")) or "\\" in key:
        raise CheckError(f"Refusing unsafe object key {key!r}.")
    parts = key.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise CheckError(f"Refusing unsafe object key {key!r}.")
    if any(":" in part for part in parts):
        # A drive letter or NTFS alternate data stream on Windows.
        raise CheckError(f"Refusing unsafe object key {key!r}.")
    dest = root.joinpath(*parts)
    root_resolved = root.resolve()
    try:
        dest.resolve().relative_to(root_resolved)
    except ValueError as exc:
        raise CheckError(f"Refusing object key that escapes the backup folder: {key!r}.") from exc
    return dest


# --- Storage HTTP (thin, urllib only) ---------------------------------------


class StorageClient:
    def __init__(self, supabase_url: str, service_role_key: str, secrets: Iterable[str]):
        self._base = supabase_url.rstrip("/") + "/storage/v1"
        self._key = service_role_key
        self._secrets = tuple(secrets)

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        headers = {"apikey": self._key, "Authorization": f"Bearer {self._key}"}
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _request(self, req: urllib.request.Request, what: str) -> bytes:
        last: str = ""
        for attempt in range(1, HTTP_RETRIES + 1):
            try:
                with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:  # noqa: S310
                    return resp.read()
            except urllib.error.HTTPError as exc:
                # 4xx is not going to improve on retry. Body may carry the key
                # back at us in an error echo; never print it.
                if 400 <= exc.code < 500:
                    raise CheckError(f"Storage {what} failed with HTTP {exc.code}.") from None
                last = f"HTTP {exc.code}"
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = redact(
                    type(exc).__name__ + ": " + str(getattr(exc, "reason", exc)), self._secrets
                )
            if attempt < HTTP_RETRIES:
                time.sleep(2 * attempt)
        raise CheckError(f"Storage {what} failed after {HTTP_RETRIES} attempts ({last}).")

    def list_page(self, prefix: str, limit: int, offset: int) -> list[dict]:
        body = json.dumps(
            {
                "prefix": prefix,
                "limit": limit,
                "offset": offset,
                "sortBy": {"column": "name", "order": "asc"},
            }
        ).encode()
        req = urllib.request.Request(
            f"{self._base}/object/list/{BUCKET}",
            data=body,
            headers=self._headers("application/json"),
            method="POST",
        )
        raw = self._request(req, f"listing (prefix={prefix!r}, offset={offset})")
        try:
            rows = json.loads(raw)
        except ValueError as exc:
            raise CheckError("Storage listing returned unreadable JSON.") from exc
        if not isinstance(rows, list):
            raise CheckError("Storage listing did not return a list.")
        return rows

    def download(self, key: str) -> bytes:
        quoted = urllib.parse.quote(key, safe="/")
        req = urllib.request.Request(
            f"{self._base}/object/{BUCKET}/{quoted}", headers=self._headers(), method="GET"
        )
        return self._request(req, f"download of {key!r}")


# --- pg tools ----------------------------------------------------------------


def run_tool(
    args: list[str], secrets: Iterable[str], *, what: str, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a PostgreSQL client tool; on failure raise with REDACTED stderr."""
    run_env = dict(os.environ)
    run_env.setdefault("PGCONNECT_TIMEOUT", "30")
    # PII in transit: require TLS unless the URL itself says otherwise (a
    # ``sslmode=`` in the URL takes precedence over the environment).
    run_env.setdefault("PGSSLMODE", "require")
    if env:
        run_env.update(env)
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            args, capture_output=True, text=True, check=False, env=run_env
        )
    except OSError as exc:
        raise BackupError(f"{what}: could not start {args[0]!r} ({type(exc).__name__}).") from None
    if result.returncode != 0:
        tail = redact((result.stderr or "").strip()[-2000:], secrets)
        raise BackupError(f"{what} failed (exit {result.returncode}):\n{tail}")
    return result


def split_password(database_url: str) -> tuple[str, str | None]:
    """``(url without the password, decoded password)``.

    The pg tools accept the password from ``PGPASSWORD`` in the environment, so
    it never has to sit in argv where a process listing or endpoint telemetry
    would record it for the life of the dump. libpq wants the raw password in
    the environment, so a percent-encoded one in the URL is decoded here.
    """
    parts = urllib.parse.urlsplit(database_url)
    if parts.password is None:
        return database_url, None
    userinfo = urllib.parse.quote(parts.username or "", safe="")
    hostport = parts.hostname or ""
    if ":" in hostport:  # bare IPv6 literal
        hostport = f"[{hostport}]"
    if parts.port is not None:
        hostport = f"{hostport}:{parts.port}"
    netloc = f"{userinfo}@{hostport}" if userinfo else hostport
    return urllib.parse.urlunsplit(parts._replace(netloc=netloc)), urllib.parse.unquote(
        parts.password
    )


def run_db_tool(
    args: list[str], cfg: BackupConfig, *, what: str
) -> subprocess.CompletedProcess[str]:
    """``run_tool`` for a command that ends with the database URL: the password is
    moved out of argv into ``PGPASSWORD`` before the process starts."""
    url, password = split_password(cfg.database_url)
    env = {"PGPASSWORD": password} if password is not None else None
    return run_tool([*args, url], cfg.secrets, what=what, env=env)


def pg_dump_public(tools: Mapping[str, str], cfg: BackupConfig, dest: pathlib.Path) -> None:
    run_db_tool(
        [
            tools["pg_dump"],
            "--format=custom",
            "--schema=public",
            "--no-password",
            f"--file={dest}",
        ],
        cfg,
        what="pg_dump of schema public",
    )


def pg_dump_auth(
    tools: Mapping[str, str], cfg: BackupConfig, dest: pathlib.Path
) -> tuple[str, str]:
    """Dump the auth schema to plain SQL.

    What this captures is the staff ACCOUNTS: ``auth.users`` (including the
    ``encrypted_password`` hashes, so a restore keeps logins working) and
    ``auth.identities``. Live session material is deliberately NOT captured —
    see ``AUTH_SESSION_TABLES``. Even so, the file is a credential artifact and
    the runbook treats it as one.

    Returns ``(mode, note)``. Tries schema+data first. The auth schema's objects
    are owned by ``supabase_auth_admin`` and a full dump can fail on
    Supabase-owned functions, triggers or sequences the ``postgres`` role may not
    read; in that case fall back to ``--data-only``, which is what a restore into
    a fresh Supabase project (where ``auth`` already exists) would apply anyway.
    ``--no-owner --no-privileges`` because plain SQL cannot have those stripped
    at restore time and the destination's auth schema has its own owners.
    """
    base = [
        tools["pg_dump"],
        "--format=plain",
        "--schema=auth",
        "--no-owner",
        "--no-privileges",
        "--no-password",
        *[f"--exclude-table-data=auth.{t}" for t in AUTH_SESSION_TABLES],
        f"--file={dest}",
    ]
    try:
        run_db_tool(base, cfg, what="pg_dump of schema auth")
        return "schema+data", ""
    except BackupError as first:
        note = str(first)
        if dest.exists():
            dest.unlink()
        run_db_tool([*base, "--data-only"], cfg, what="pg_dump of schema auth (data only)")
        return "data-only", note


def psql_scalar(tools: Mapping[str, str], cfg: BackupConfig, sql: str, *, what: str) -> str:
    result = run_db_tool(
        [
            tools["psql"],
            "--no-psqlrc",
            "--no-password",
            "-v",
            "ON_ERROR_STOP=1",
            "-tAc",
            sql,
        ],
        cfg,
        what=what,
    )
    return result.stdout.strip()


def count_rows(
    tools: Mapping[str, str], cfg: BackupConfig, table: str, *, required: bool = False
) -> int | None:
    """Exact ``count(*)``. ``None`` if the table cannot be counted, unless
    ``required`` in which case the underlying error is raised."""
    schema, _, name = table.partition(".")
    if not (schema.isidentifier() and name.isidentifier()):
        raise BackupError(f"Refusing to count oddly named table {table!r}.")
    try:
        raw = psql_scalar(
            tools, cfg, f'SELECT count(*) FROM "{schema}"."{name}"', what=f"count of {table}"
        )
        return int(raw)
    except (BackupError, ValueError):
        if required:
            raise
        return None


def pg_restore_list(tools: Mapping[str, str], cfg: BackupConfig, dump: pathlib.Path) -> str:
    result = run_tool(
        [tools["pg_restore"], "--list", str(dump)], cfg.secrets, what="pg_restore --list"
    )
    return result.stdout


def toc_lists_table(toc: str, schema: str, table: str) -> bool:
    """Whether a ``pg_restore --list`` TOC contains ``TABLE schema table``.

    Entry lines look like ``215; 1259 16400 TABLE public alumni postgres`` and,
    for the rows, ``2861; 0 16400 TABLE DATA public alumni postgres``. Lines
    starting with ``;`` are comments.
    """
    for line in toc.splitlines():
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        fields = line.split()
        if "TABLE" not in fields:
            continue
        rest = fields[fields.index("TABLE") + 1 :]
        if rest[:1] == ["DATA"]:
            rest = rest[1:]
        if rest[:2] == [schema, table]:
            return True
    return False


# --- Manifest ----------------------------------------------------------------


@dataclass
class FileRecord:
    bytes: int
    sha256: str
    #: Only bucket objects carry these (from the listing); dumps leave them None
    #: and the manifest omits them.
    etag: str | None = None
    updated_at: str | None = None

    def as_manifest(self) -> dict:
        out: dict = {"bytes": self.bytes, "sha256": self.sha256}
        if self.etag is not None:
            out["etag"] = self.etag
        if self.updated_at is not None:
            out["updated_at"] = self.updated_at
        return out


@dataclass
class RunResult:
    project_ref: str
    started_at: datetime
    finished_at: datetime | None = None
    tools: dict[str, str] = field(default_factory=dict)
    server_version: str | None = None
    row_counts: dict[str, int | None] = field(default_factory=dict)
    files: dict[str, FileRecord] = field(default_factory=dict)
    auth_dump_mode: str | None = None
    auth_dump_note: str = ""
    listing_object_count: int = 0
    listing_total_bytes: int = 0
    downloaded_object_count: int = 0
    downloaded_total_bytes: int = 0
    #: Objects copied from the previous OK run instead of downloaded.
    reused_object_count: int = 0
    reused_total_bytes: int = 0
    capture_mode: str = "full"
    previous_run: str | None = None
    removed_since_previous: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    @property
    def captured_object_count(self) -> int:
        return self.downloaded_object_count + self.reused_object_count

    @property
    def captured_total_bytes(self) -> int:
        return self.downloaded_total_bytes + self.reused_total_bytes


def utc_stamp(now: datetime) -> str:
    """Folder name for a run. No colons: illegal in Windows file names."""
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H%M%SZ")


def sha256_file(path: pathlib.Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def build_manifest(result: RunResult) -> dict:
    finished = result.finished_at or datetime.now(UTC)
    duration = (finished - result.started_at).total_seconds()
    storage_files = {k: v for k, v in result.files.items() if k.startswith("storage/")}
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "status": "ok" if not result.failures else "FAILED",
        "project_ref": result.project_ref,
        "started_at": result.started_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "finished_at": finished.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "duration_seconds": round(duration, 3),
        "host": socket.gethostname(),
        "platform": platform.platform(),
        "tools": dict(result.tools),
        "server_version": result.server_version,
        "database": {
            "row_counts": dict(result.row_counts),
            "alumni_row_floor": ALUMNI_ROW_FLOOR,
            "auth_dump_mode": result.auth_dump_mode,
            "auth_dump_note": result.auth_dump_note,
        },
        "storage": {
            "bucket": BUCKET,
            "capture_mode": result.capture_mode,
            "previous_run": result.previous_run,
            "listing_object_count": result.listing_object_count,
            "listing_total_bytes": result.listing_total_bytes,
            "downloaded_object_count": result.downloaded_object_count,
            "downloaded_total_bytes": result.downloaded_total_bytes,
            "reused_object_count": result.reused_object_count,
            "reused_total_bytes": result.reused_total_bytes,
            "removed_count": len(result.removed_since_previous),
            "removed_since_previous": list(result.removed_since_previous),
            "file_count_on_disk": len(storage_files),
        },
        "files": {name: rec.as_manifest() for name, rec in sorted(result.files.items())},
        "total_bytes": sum(rec.bytes for rec in result.files.values()),
        "checks": dict(result.checks),
        "failures": list(result.failures),
    }


def write_manifest(folder: pathlib.Path, manifest: dict) -> pathlib.Path:
    path = folder / "MANIFEST.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return path


def read_manifest(folder: pathlib.Path) -> dict | None:
    """The folder's MANIFEST.json as a dict, or ``None`` if absent or unreadable.

    Unreadable is treated like absent on purpose: a folder whose manifest is
    half-written or corrupt is something a human looks at, never something the
    script reuses or deletes.
    """
    path = folder / "MANIFEST.json"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def manifest_is_ok(manifest: dict | None) -> bool:
    return isinstance(manifest, dict) and str(manifest.get("status", "")).lower() == "ok"


# --- Previous runs and incremental capture ----------------------------------


def run_folders(backup_dir: pathlib.Path) -> list[pathlib.Path]:
    """Directories DIRECTLY under ``backup_dir`` named like a run, newest first.

    The stamp format sorts lexically in time order, so sorting by name is
    sorting by time. Symlinks are skipped: deleting through one would delete
    somewhere else.
    """
    if not backup_dir.is_dir():
        return []
    found = [
        child
        for child in backup_dir.iterdir()
        if RUN_FOLDER_RE.match(child.name) and child.is_dir() and not child.is_symlink()
    ]
    return sorted(found, key=lambda p: p.name, reverse=True)


def find_previous_ok_run(
    backup_dir: pathlib.Path, *, exclude: str, project_ref: str
) -> tuple[pathlib.Path, dict] | None:
    """The newest run folder whose manifest says ``ok`` for THIS project.

    A FAILED run, a folder with no manifest, and a run of a different project
    ref are all skipped: files are only ever reused from a folder the checks
    passed for, of the same database.
    """
    for folder in run_folders(backup_dir):
        if folder.name == exclude:
            continue
        manifest = read_manifest(folder)
        if not manifest_is_ok(manifest):
            continue
        assert manifest is not None
        if manifest.get("project_ref") != project_ref:
            continue
        return folder, manifest
    return None


@dataclass
class IncrementalPlan:
    download: list[StorageObject] = field(default_factory=list)
    reuse: list[StorageObject] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    #: Why each object in ``download`` is there (``new``, ``size``, ``etag``,
    #: ``updated_at``, ``no-marker``); only logged and shown in --dry-run.
    reasons: dict[str, str] = field(default_factory=dict)


def previous_storage_records(manifest: dict) -> dict[str, dict]:
    """``{object key: file record}`` for the bucket objects of a manifest."""
    prefix = f"storage/{BUCKET}/"
    files = manifest.get("files")
    if not isinstance(files, Mapping):
        return {}
    out: dict[str, dict] = {}
    for name, record in files.items():
        if isinstance(name, str) and name.startswith(prefix) and isinstance(record, Mapping):
            out[name[len(prefix) :]] = dict(record)
    return out


def plan_incremental(objects: Iterable[StorageObject], previous: dict) -> IncrementalPlan:
    """Decide, per listed object, whether to download it or copy it from the
    previous run. Pure: touches no file.

    An object is reused only when ALL of these hold:

    * the previous manifest has a record for the same key with a sha256
      (that hash is what the copy is verified against),
    * the sizes agree,
    * at least one change marker (etag, updated_at) is present on BOTH sides,
      and every marker present on both sides agrees.

    "No marker on both sides" means download. Size alone cannot tell a
    re-uploaded photo of the same byte length from the original, and a stale
    photo carried forward for ever would be exactly the silent failure this
    script exists to prevent. The cost is one full download after a
    schema-version-1 manifest, which is acceptable.
    """
    plan = IncrementalPlan()
    old = previous_storage_records(previous)
    seen: set[str] = set()
    for obj in objects:
        seen.add(obj.path)
        record = old.get(obj.path)
        if record is None:
            plan.download.append(obj)
            plan.reasons[obj.path] = "new"
            continue
        if not isinstance(record.get("sha256"), str):
            plan.download.append(obj)
            plan.reasons[obj.path] = "no-sha256"
            continue
        if record.get("bytes") != obj.size:
            plan.download.append(obj)
            plan.reasons[obj.path] = "size"
            continue
        compared = 0
        changed: str | None = None
        for marker in ("etag", "updated_at"):
            mine = getattr(obj, marker)
            theirs = _clean_marker(record.get(marker))
            if mine is None or theirs is None:
                continue
            compared += 1
            if mine != theirs:
                changed = marker
                break
        if changed:
            plan.download.append(obj)
            plan.reasons[obj.path] = changed
        elif compared == 0:
            plan.download.append(obj)
            plan.reasons[obj.path] = "no-marker"
        else:
            plan.reuse.append(obj)
    plan.removed = sorted(key for key in old if key not in seen)
    return plan


def reuse_from_previous(
    previous_folder: pathlib.Path,
    previous_record: Mapping,
    obj: StorageObject,
    dest: pathlib.Path,
) -> FileRecord | None:
    """Copy ``obj`` from the previous run folder into ``dest`` and verify it.

    Returns the verified record, or ``None`` (and leaves nothing at ``dest``)
    when the previous file is missing, a different size, or hashes differently
    from what the previous manifest recorded, so the caller downloads instead.
    """
    try:
        source = safe_object_destination(previous_folder / "storage" / BUCKET, obj.path)
    except CheckError:
        return None
    expected_sha = previous_record.get("sha256")
    if not source.is_file() or not isinstance(expected_sha, str):
        return None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        size, digest = sha256_file(dest)
    except OSError:
        size, digest = -1, ""
    if size != obj.size or digest != expected_sha:
        try:
            dest.unlink()
        except OSError:
            pass
        return None
    return FileRecord(size, digest, etag=obj.etag, updated_at=obj.updated_at)


# --- Retention ---------------------------------------------------------------


def check_prunable_root(backup_dir: pathlib.Path, repo_root: pathlib.Path) -> None:
    """Refuse to prune anywhere a mistake would be catastrophic.

    ``load_config`` already refuses a BACKUP_DIR inside the repo, but this is
    called again right before anything is deleted, with the resolved path,
    because "delete the oldest folders under X" must never run with X being a
    drive root, the filesystem root, or the source tree.
    """
    resolved = backup_dir.resolve()
    if resolved.parent == resolved or resolved == pathlib.Path(resolved.anchor):
        raise ConfigError(
            f"Refusing to prune: BACKUP_DIR resolves to a filesystem or drive root ({resolved})."
        )
    try:
        resolved.relative_to(repo_root.resolve())
    except ValueError:
        return
    raise ConfigError(f"Refusing to prune: BACKUP_DIR resolves inside the repository ({resolved}).")


def plan_prune(backup_dir: pathlib.Path, keep: int, *, current: str) -> list[pathlib.Path]:
    """The run folders that ``prune_old_runs`` would delete. Pure.

    Keeps the ``keep`` newest OK runs. A folder is a candidate only when it is
    named like a run, sits directly under ``backup_dir``, has a readable
    manifest, and that manifest says ``ok``. Everything else (FAILED runs,
    manifest-less folders, stray directories) is left for a human. The run
    being written (``current``) and the newest OK run are never candidates
    even if ``keep`` would otherwise say so.
    """
    if keep < 1:
        raise ConfigError("--keep must be at least 1.")
    ok_runs = [f for f in run_folders(backup_dir) if manifest_is_ok(read_manifest(f))]
    newest = ok_runs[0].name if ok_runs else None
    return [f for f in ok_runs[keep:] if f.name not in (current, newest)]


def prune_old_runs(
    backup_dir: pathlib.Path,
    keep: int,
    *,
    current: str,
    repo_root: pathlib.Path | None = None,
    logger: Callable[[str], None] = print,
) -> list[str]:
    """Delete the oldest OK runs beyond ``keep``; return the deleted names."""
    check_prunable_root(backup_dir, repo_root or _repo_root())
    deleted: list[str] = []
    for folder in plan_prune(backup_dir, keep, current=current):
        # Re-check right before the delete: the folder must still be a real,
        # non-symlinked directory directly under BACKUP_DIR.
        if folder.parent != backup_dir or folder.is_symlink() or not folder.is_dir():
            continue
        shutil.rmtree(folder)
        deleted.append(folder.name)
        logger(f"  pruned {folder}")
    return deleted


# --- Outcome files and the failure webhook ----------------------------------


def write_last_run(
    backup_dir: pathlib.Path,
    *,
    status: str,
    run_folder: str | None,
    finished_at: datetime,
    downloaded: int,
    reused: int,
    pruned: list[str],
    reason: str | None = None,
) -> pathlib.Path:
    """BACKUP_DIR/LAST-RUN.json: the one file a monitor needs to read."""
    payload = {
        "status": status,
        "run_folder": run_folder,
        "finished_at_utc": finished_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "downloaded": downloaded,
        "reused": reused,
        "pruned": list(pruned),
    }
    if reason:
        payload["reason"] = reason
    path = backup_dir / LAST_RUN_NAME
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def write_failed_marker(
    backup_dir: pathlib.Path, *, when: datetime, run_folder: str | None, reason: str
) -> pathlib.Path:
    """BACKUP_DIR/LAST-RUN-FAILED.txt. ``reason`` must already be redacted."""
    path = backup_dir / FAILED_MARKER_NAME
    stamp = when.astimezone(UTC).isoformat().replace("+00:00", "Z")
    path.write_text(
        f"prod backup FAILED at {stamp}\nrun folder: {run_folder or '(none created)'}\n"
        f"reason: {reason}\n\nSee docs/BACKUPS.md. This file is removed by the next "
        "successful run.\n",
        encoding="utf-8",
    )
    return path


def clear_failed_marker(backup_dir: pathlib.Path) -> None:
    try:
        (backup_dir / FAILED_MARKER_NAME).unlink()
    except FileNotFoundError:
        pass


def slack_reason(reason: str) -> str:
    """The first line of a failure, with quoted values dropped and cut short.

    The script quotes anything it echoes from the outside world (object keys,
    table names) with ``repr``, so removing quoted runs keeps bucket keys - which
    may be survey tokens - out of the channel. Secrets are redacted by the
    caller before this runs.
    """
    first = reason.strip().splitlines()[0] if reason.strip() else "unknown"
    first = re.sub(r"'[^']*'", "'...'", first)
    first = first.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if len(first) > WEBHOOK_REASON_MAX_CHARS:
        first = first[: WEBHOOK_REASON_MAX_CHARS - 3] + "..."
    return first


def post_failure_webhook(
    url: str,
    *,
    when: datetime,
    run_folder: str | None,
    reason: str,
    opener: Callable[..., object] | None = None,
) -> bool:
    """POST one line to a Slack incoming webhook. FAILURE ONLY, never success.

    Returns whether it was delivered. Never raises: a webhook that is down must
    not change the exit code of a backup that has already failed for its own
    reasons, and it must not print the URL. Retries are deliberately absent for
    the same reason the app's failure alerter has none.
    """
    stamp = when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    where = f" (run {run_folder})" if run_folder else ""
    text = f"prod backup FAILED at {stamp}{where}: {slack_reason(reason)}"
    body = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(req, timeout=WEBHOOK_TIMEOUT_SECONDS) as resp:  # type: ignore[attr-defined]
            code = getattr(resp, "status", 200)
        return 200 <= int(code) < 300
    except Exception:  # noqa: BLE001 - see docstring
        return False


def record_outcome(
    *,
    backup_dir: pathlib.Path | None,
    webhook_url: str | None,
    secrets: Iterable[str],
    status: str,
    run_folder: str | None,
    finished_at: datetime,
    downloaded: int = 0,
    reused: int = 0,
    pruned: list[str] | None = None,
    reason: str | None = None,
) -> None:
    """Write LAST-RUN.json, set or clear the failure marker, alert on failure.

    Best effort throughout: the exit code is decided by the backup itself, and
    a full disk while writing a marker must not mask the real reason.
    """
    secrets = tuple(secrets)
    safe_reason = redact(reason, secrets) if reason else None
    if backup_dir is not None and backup_dir.is_dir():
        try:
            write_last_run(
                backup_dir,
                status=status,
                run_folder=run_folder,
                finished_at=finished_at,
                downloaded=downloaded,
                reused=reused,
                pruned=pruned or [],
                reason=safe_reason,
            )
            if status == "OK":
                clear_failed_marker(backup_dir)
            else:
                write_failed_marker(
                    backup_dir, when=finished_at, run_folder=run_folder, reason=safe_reason or ""
                )
        except OSError as exc:
            print(f"WARNING: could not write the run outcome files ({exc})", file=sys.stderr)
    if status != "OK" and webhook_url:
        delivered = post_failure_webhook(
            webhook_url, when=finished_at, run_folder=run_folder, reason=safe_reason or status
        )
        print(
            "Slack: failure notice " + ("sent." if delivered else "could NOT be delivered."),
            file=sys.stderr,
        )


# --- Orchestration -----------------------------------------------------------


def log(msg: str, secrets: Iterable[str] = ()) -> None:
    print(redact(msg, secrets), flush=True)


def dry_run(
    cfg: BackupConfig,
    tools: Mapping[str, str],
    *,
    incremental: bool = False,
    keep: int | None = None,
) -> int:
    now = datetime.now(UTC)
    folder = cfg.backup_dir / utc_stamp(now)
    db_host = urllib.parse.urlsplit(cfg.database_url).hostname
    print("DRY RUN - nothing will be contacted or written.")
    print(f"  project ref          {cfg.expect_project_ref}  (asserted in DB URL and Supabase URL)")
    print(f"  database host        {db_host}:{SESSION_POOLER_PORT}  (session pooler)")
    print(f"  supabase url         {cfg.supabase_url}")
    print(f"  bucket               {BUCKET}")
    print(f"  destination          {folder}")
    print(f"  capture              {'incremental' if incremental else 'full'}")
    print(f"  keep                 {keep if keep else 'everything (no pruning)'}")
    print(f"  slack on failure     {'yes' if cfg.slack_webhook_url else 'no (not configured)'}")
    for name in REQUIRED_TOOLS:
        print(f"  {name:<20} {tools[name]}  ({tool_version(tools[name])})")
    print("Would:")
    print("  1. pg_dump --format=custom --schema=public  -> database.dump (always in full)")
    print("  2. pg_dump --format=plain  --schema=auth    -> auth.sql (data-only fallback)")
    print(f"  3. list bucket {BUCKET!r} (pages of {LIST_PAGE_SIZE}, folders walked)")
    if incremental:
        previous = find_previous_ok_run(
            cfg.backup_dir, exclude=folder.name, project_ref=cfg.expect_project_ref
        )
        if previous is None:
            print("     no previous OK run found -> would download EVERY object (full)")
        else:
            prev_folder, prev_manifest = previous
            count = len(previous_storage_records(prev_manifest))
            print(f"     previous OK run: {prev_folder.name} ({count} objects recorded)")
            print("     download only NEW/CHANGED objects; copy unchanged ones from that folder")
            print("     after verifying size + sha256; list objects gone from the bucket")
    else:
        print("     and download every object -> storage/headshots/<key>")
    print(
        "  4. verify: pg_restore --list shows public.alumni; alumni rows >= "
        f"{ALUMNI_ROW_FLOOR}; objects on disk + bytes == listing"
    )
    print("  5. write MANIFEST.json, LAST-RUN.json; exit non-zero on any failure")
    if keep:
        check_prunable_root(cfg.backup_dir, _repo_root())
        # The simulated run would be the newest OK run, so of the EXISTING OK
        # runs only the newest keep-1 survive.
        ok_now = [f for f in run_folders(cfg.backup_dir) if manifest_is_ok(read_manifest(f))]
        doomed = ok_now[keep - 1 :]
        print(f"  6. prune to the newest {keep} OK runs. Would delete now: ", end="")
        print(", ".join(sorted(f.name for f in doomed)) if doomed else "nothing")
    return 0


def run_backup(
    cfg: BackupConfig,
    tools: Mapping[str, str],
    *,
    now: datetime | None = None,
    incremental: bool = False,
    keep: int | None = None,
) -> int:
    started = now or datetime.now(UTC)
    secrets = cfg.secrets
    result = RunResult(project_ref=cfg.expect_project_ref, started_at=started)
    result.tools = {name: tool_version(path) for name, path in tools.items()}

    folder = cfg.backup_dir / utc_stamp(started)
    if folder.exists():
        raise BackupError(f"Destination already exists: {folder}")
    if keep:
        # Fail BEFORE dumping, not after: a refusal to prune should not cost a
        # full run to discover.
        check_prunable_root(cfg.backup_dir, _repo_root())

    previous: tuple[pathlib.Path, dict] | None = None
    if incremental:
        previous = find_previous_ok_run(
            cfg.backup_dir, exclude=folder.name, project_ref=cfg.expect_project_ref
        )
        if previous is None:
            result.capture_mode = "full (no previous OK run)"
            log("No previous OK run found under BACKUP_DIR; downloading every object.")
        else:
            result.capture_mode = "incremental"
            result.previous_run = previous[0].name
            log(f"Incremental against previous OK run {previous[0].name}")

    storage_root = folder / "storage" / BUCKET
    storage_root.mkdir(parents=True, exist_ok=False)
    log(f"Backup folder: {folder}")
    for name, version in result.tools.items():
        log(f"  {name}: {version}")

    try:
        _run_steps(cfg, tools, folder, storage_root, result, previous=previous)
    except BackupError as exc:
        result.failures.append(redact(str(exc), secrets))
    except Exception as exc:  # noqa: BLE001 - recorded in the manifest, never re-raised raw
        result.failures.append(redact(f"unexpected {type(exc).__name__}: {exc}", secrets))
    finally:
        result.finished_at = datetime.now(UTC)
        manifest = build_manifest(result)
        path = write_manifest(folder, manifest)
        log(f"Manifest: {path}")

    pruned: list[str] = []
    prune_error: str | None = None
    if not result.failures and keep:
        # LAST: only after the manifest above says ok, and never touching it.
        log(f"Pruning to the newest {keep} OK runs...")
        try:
            pruned = prune_old_runs(
                cfg.backup_dir, keep, current=folder.name, logger=lambda m: log(m, secrets)
            )
        except (BackupError, OSError) as exc:
            prune_error = redact(f"backup {folder.name} is OK but pruning failed: {exc}", secrets)
        if not pruned and not prune_error:
            log("  nothing to prune")

    if result.failures or prune_error:
        reason = prune_error or "\n".join(result.failures)
        record_outcome(
            backup_dir=cfg.backup_dir,
            webhook_url=cfg.slack_webhook_url,
            secrets=secrets,
            status="FAILED",
            run_folder=folder.name,
            finished_at=result.finished_at or datetime.now(UTC),
            downloaded=result.downloaded_object_count,
            reused=result.reused_object_count,
            pruned=pruned,
            reason=reason,
        )
        if result.failures:
            print("\nBACKUP FAILED - do not trust this folder:", file=sys.stderr)
            for failure in result.failures:
                print(f"  - {redact(failure, secrets)}", file=sys.stderr)
        else:
            print(f"\nPRUNE FAILED (the backup itself is OK): {prune_error}", file=sys.stderr)
        return 1

    record_outcome(
        backup_dir=cfg.backup_dir,
        webhook_url=cfg.slack_webhook_url,
        secrets=secrets,
        status="OK",
        run_folder=folder.name,
        finished_at=result.finished_at or datetime.now(UTC),
        downloaded=result.downloaded_object_count,
        reused=result.reused_object_count,
        pruned=pruned,
    )
    log(
        f"OK  {result.captured_object_count} objects "
        f"({result.downloaded_object_count} downloaded, {result.reused_object_count} reused, "
        f"{len(result.removed_since_previous)} removed), "
        f"{manifest['total_bytes']:,} bytes total, "
        f"alumni rows {result.row_counts.get('public.alumni')}, "
        f"{manifest['duration_seconds']}s"
        + (f", pruned {len(pruned)} old run(s)" if pruned else "")
    )
    return 0


def _run_steps(
    cfg: BackupConfig,
    tools: Mapping[str, str],
    folder: pathlib.Path,
    storage_root: pathlib.Path,
    result: RunResult,
    *,
    previous: tuple[pathlib.Path, dict] | None = None,
) -> None:
    secrets = cfg.secrets

    # 0. Prove we are talking to the right server before dumping anything.
    log("Checking the database...")
    result.server_version = psql_scalar(tools, cfg, "SHOW server_version", what="server version")
    log(f"  server version: {result.server_version}")
    for table in RECORDED_TABLES:
        result.row_counts[table] = count_rows(
            tools, cfg, table, required=(table == "public.alumni")
        )
        log(f"  {table}: {result.row_counts[table]}")
    alumni = result.row_counts.get("public.alumni")
    if alumni is None or alumni < ALUMNI_ROW_FLOOR:
        result.checks["alumni_row_floor"] = False
        raise CheckError(
            f"public.alumni has {alumni} rows, below the floor of {ALUMNI_ROW_FLOOR}. "
            "Wrong database, or the data is not what we think it is. Nothing was dumped."
        )
    result.checks["alumni_row_floor"] = True

    # 1. public schema, custom format.
    dump = folder / "database.dump"
    log("Dumping schema public (custom format)...")
    pg_dump_public(tools, cfg, dump)
    result.files["database.dump"] = FileRecord(*sha256_file(dump))
    log(f"  database.dump: {result.files['database.dump'].bytes:,} bytes")

    # 2. auth schema, plain SQL.
    auth_sql = folder / "auth.sql"
    log("Dumping schema auth (plain SQL)...")
    result.auth_dump_mode, result.auth_dump_note = pg_dump_auth(tools, cfg, auth_sql)
    result.files["auth.sql"] = FileRecord(*sha256_file(auth_sql))
    log(f"  auth.sql: {result.files['auth.sql'].bytes:,} bytes ({result.auth_dump_mode})")
    if result.auth_dump_note:
        log("  full auth dump failed, fell back to data-only. First attempt said:")
        log("  " + result.auth_dump_note.replace("\n", "\n  "), secrets)

    # 3. bucket.
    client = StorageClient(cfg.supabase_url, cfg.service_role_key, secrets)
    log(f"Listing bucket {BUCKET!r}...")
    objects = walk_bucket(client.list_page)
    result.listing_object_count = len(objects)
    result.listing_total_bytes = sum(o.size for o in objects)
    log(f"  {result.listing_object_count} objects, {result.listing_total_bytes:,} bytes listed")
    if not objects:
        raise CheckError(f"Bucket {BUCKET!r} listed zero objects. Wrong project or wrong key.")

    # Which objects can be copied from the previous OK run instead of fetched.
    reuse_from: dict[str, dict] = {}
    if previous is not None:
        prev_folder, prev_manifest = previous
        plan = plan_incremental(objects, prev_manifest)
        result.removed_since_previous = plan.removed
        prev_records = previous_storage_records(prev_manifest)
        reuse_from = {obj.path: prev_records[obj.path] for obj in plan.reuse}
        log(
            f"  plan: {len(plan.download)} to download, {len(plan.reuse)} unchanged "
            f"(copy from {prev_folder.name}), {len(plan.removed)} gone from the bucket"
        )
    else:
        prev_folder = None

    log("Capturing objects...")
    mismatched: list[str] = []
    fallback = 0
    for index, obj in enumerate(objects, start=1):
        dest = safe_object_destination(storage_root, obj.path)
        name = f"storage/{BUCKET}/{obj.path}"
        if prev_folder is not None and obj.path in reuse_from:
            record = reuse_from_previous(prev_folder, reuse_from[obj.path], obj, dest)
            if record is not None:
                result.reused_object_count += 1
                result.reused_total_bytes += record.bytes
                result.files[name] = record
                continue
            # Missing, wrong size or wrong hash on disk: the previous folder is
            # not what its manifest says. Download, and say so.
            fallback += 1
        data = client.download(obj.path)
        if len(data) != obj.size:
            mismatched.append(f"{obj.path}: listing {obj.size} bytes, downloaded {len(data)}")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        result.downloaded_object_count += 1
        result.downloaded_total_bytes += len(data)
        result.files[name] = FileRecord(
            len(data), hashlib.sha256(data).hexdigest(), etag=obj.etag, updated_at=obj.updated_at
        )
        if index % 50 == 0 or index == len(objects):
            log(f"  {index}/{len(objects)}")
    if fallback:
        log(f"  {fallback} object(s) failed copy verification and were downloaded instead")

    # 4. checks.
    log("Verifying...")
    toc = pg_restore_list(tools, cfg, dump)
    ok_toc = toc_lists_table(toc, "public", "alumni")
    result.checks["pg_restore_list_public_alumni"] = ok_toc
    if not ok_toc:
        result.failures.append("pg_restore --list database.dump does not list TABLE public alumni.")

    # Parity: what is on disk (downloaded + copied) must equal the listing.
    ok_storage = (
        not mismatched
        and result.captured_object_count == result.listing_object_count
        and result.captured_total_bytes == result.listing_total_bytes
    )
    result.checks["storage_totals_match"] = ok_storage
    if not ok_storage:
        result.failures.append(
            f"Storage mismatch: listing reported {result.listing_object_count} objects / "
            f"{result.listing_total_bytes} bytes, captured {result.captured_object_count} / "
            f"{result.captured_total_bytes} ({result.downloaded_object_count} downloaded, "
            f"{result.reused_object_count} reused)."
        )
        result.failures.extend(mismatched[:20])

    ok_dump_size = result.files["database.dump"].bytes > 0 and result.files["auth.sql"].bytes > 0
    result.checks["dumps_non_empty"] = ok_dump_size
    if not ok_dump_size:
        result.failures.append("A dump file is empty.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="backup_prod.py",
        description="Full backup of the production Supabase project: database, auth, headshots.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate configuration and tooling, connect to nothing, print the plan",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="download only bucket objects that are new or changed since the previous OK run; "
        "copy the rest from that run (verified). The database is always dumped in full.",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=None,
        metavar="N",
        help="after a run finished OK, delete the oldest run folders so at most N OK runs "
        "remain (default: keep everything)",
    )
    args = parser.parse_args(argv)
    if args.keep is not None and args.keep < 1:
        parser.error("--keep must be at least 1")

    secrets: tuple[str, ...] = tuple(
        v
        for v in (
            os.environ.get("BACKUP_DATABASE_URL"),
            os.environ.get("BACKUP_SUPABASE_SERVICE_ROLE_KEY"),
            os.environ.get("BACKUP_SLACK_WEBHOOK_URL"),
        )
        if v
    )
    cfg: BackupConfig | None = None
    try:
        cfg = load_config(os.environ)
        secrets = cfg.secrets
        tools = find_tools()
        if args.dry_run:
            return dry_run(cfg, tools, incremental=args.incremental, keep=args.keep)
        return run_backup(cfg, tools, incremental=args.incremental, keep=args.keep)
    except BackupError as exc:
        message = redact(str(exc), secrets)
        print("ERROR: " + message, file=sys.stderr)
        if not args.dry_run:
            _record_early_failure(cfg, secrets, message)
        return 2
    except KeyboardInterrupt:
        print("Interrupted. The folder written so far is INCOMPLETE; delete it.", file=sys.stderr)
        if not args.dry_run:
            _record_early_failure(cfg, secrets, "interrupted (KeyboardInterrupt)")
        return 130
    except Exception as exc:  # noqa: BLE001 - last resort, must not leak secrets
        message = redact(f"{type(exc).__name__}: {exc}", secrets)
        print("UNEXPECTED ERROR: " + message, file=sys.stderr)
        if not args.dry_run:
            _record_early_failure(cfg, secrets, message)
        return 3


def _record_early_failure(cfg: BackupConfig | None, secrets: Iterable[str], reason: str) -> None:
    """Marker + webhook for a failure BEFORE ``run_backup`` took over.

    Config may be unusable (that is often the failure), so fall back to the
    raw ``BACKUP_DIR`` / ``BACKUP_SLACK_WEBHOOK_URL`` values: an unattended run
    that cannot even load its config must still leave a trace and page.
    """
    backup_dir = cfg.backup_dir if cfg else None
    webhook = cfg.slack_webhook_url if cfg else None
    if backup_dir is None:
        raw = (os.environ.get("BACKUP_DIR") or "").strip()
        candidate = pathlib.Path(raw).expanduser() if raw else None
        if candidate is not None and candidate.is_absolute() and candidate.is_dir():
            backup_dir = candidate
    if webhook is None:
        raw_hook = (os.environ.get("BACKUP_SLACK_WEBHOOK_URL") or "").strip()
        webhook = raw_hook if raw_hook.startswith("https://") else None
    record_outcome(
        backup_dir=backup_dir,
        webhook_url=webhook,
        secrets=secrets,
        status="FAILED",
        run_folder=None,
        finished_at=datetime.now(UTC),
        reason=reason,
    )


if __name__ == "__main__":
    sys.exit(main())
