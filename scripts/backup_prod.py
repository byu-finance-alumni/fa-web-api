"""Full backup of the PRODUCTION Supabase project, run by hand (api #535).

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

The project ref is asserted against BOTH the database host and the Supabase URL
before anything is dumped. Base URLs in this project are easy to get backwards
(the keepalive workflow asserts its environment for the same reason), and a
"backup" of dev would be worse than no backup because it would look fine.

Runs with `--dry-run` to validate configuration and tooling without opening a
single connection.

Deliberately stdlib-only so it runs under any Python 3.12+ on the machine (no
venv needed) and adds nothing to the deployed function's dependencies.
pg_dump / pg_restore / psql are taken from PATH; PostgreSQL 17 is installed on
the Windows machine this was written for.

Secrets are never printed. Every message that could carry the database URL or
the service key passes through `redact()` first. gitleaks scans history, so a
single careless echo would be permanent.

Phase 1 + 2 of docs/BACKUPS-PLAN.md only: no scheduling, no pruning, no Slack.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import platform
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

MANIFEST_SCHEMA_VERSION = 1


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

    @property
    def secrets(self) -> tuple[str, ...]:
        """Every string that must never appear in output."""
        out = [self.database_url, self.service_role_key]
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

    cfg = BackupConfig(
        database_url=database_url,
        supabase_url=supabase_url,
        service_role_key=service_role_key,
        backup_dir=backup_dir,
        expect_project_ref=expect_project_ref,
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
    if ref.lower() not in db_host and ref.lower() not in db_user.lower():
        raise ConfigError(
            f"BACKUP_DATABASE_URL does not reference project {ref!r} (checked the host and "
            "the username). Wrong project, wrong variable, or a stale connection string."
        )
    supa_host = (urllib.parse.urlsplit(cfg.supabase_url).hostname or "").lower()
    if ref.lower() not in supa_host:
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


ListPageFn = Callable[[str, int, int], list[dict]]
"""(prefix, limit, offset) -> one page of raw listing rows."""


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
                objects.append(StorageObject(path=full, size=size))
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
    checks: dict[str, bool] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)


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
            "listing_object_count": result.listing_object_count,
            "listing_total_bytes": result.listing_total_bytes,
            "downloaded_object_count": result.downloaded_object_count,
            "downloaded_total_bytes": result.downloaded_total_bytes,
            "file_count_on_disk": len(storage_files),
        },
        "files": {
            name: {"bytes": rec.bytes, "sha256": rec.sha256}
            for name, rec in sorted(result.files.items())
        },
        "total_bytes": sum(rec.bytes for rec in result.files.values()),
        "checks": dict(result.checks),
        "failures": list(result.failures),
    }


def write_manifest(folder: pathlib.Path, manifest: dict) -> pathlib.Path:
    path = folder / "MANIFEST.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return path


# --- Orchestration -----------------------------------------------------------


def log(msg: str, secrets: Iterable[str] = ()) -> None:
    print(redact(msg, secrets), flush=True)


def dry_run(cfg: BackupConfig, tools: Mapping[str, str]) -> int:
    now = datetime.now(UTC)
    folder = cfg.backup_dir / utc_stamp(now)
    db_host = urllib.parse.urlsplit(cfg.database_url).hostname
    print("DRY RUN - nothing will be contacted or written.")
    print(f"  project ref          {cfg.expect_project_ref}  (asserted in DB URL and Supabase URL)")
    print(f"  database host        {db_host}:{SESSION_POOLER_PORT}  (session pooler)")
    print(f"  supabase url         {cfg.supabase_url}")
    print(f"  bucket               {BUCKET}")
    print(f"  destination          {folder}")
    for name in REQUIRED_TOOLS:
        print(f"  {name:<20} {tools[name]}  ({tool_version(tools[name])})")
    print("Would:")
    print("  1. pg_dump --format=custom --schema=public  -> database.dump")
    print("  2. pg_dump --format=plain  --schema=auth    -> auth.sql (data-only fallback)")
    print(f"  3. list bucket {BUCKET!r} (pages of {LIST_PAGE_SIZE}, folders walked) and download")
    print("     every object -> storage/headshots/<key>")
    print(
        "  4. verify: pg_restore --list shows public.alumni; alumni rows >= "
        f"{ALUMNI_ROW_FLOOR}; object count + bytes == listing"
    )
    print("  5. write MANIFEST.json; exit non-zero on any failure")
    return 0


def run_backup(cfg: BackupConfig, tools: Mapping[str, str], *, now: datetime | None = None) -> int:
    started = now or datetime.now(UTC)
    secrets = cfg.secrets
    result = RunResult(project_ref=cfg.expect_project_ref, started_at=started)
    result.tools = {name: tool_version(path) for name, path in tools.items()}

    folder = cfg.backup_dir / utc_stamp(started)
    if folder.exists():
        raise BackupError(f"Destination already exists: {folder}")
    storage_root = folder / "storage" / BUCKET
    storage_root.mkdir(parents=True, exist_ok=False)
    log(f"Backup folder: {folder}")
    for name, version in result.tools.items():
        log(f"  {name}: {version}")

    exit_code = 1
    try:
        _run_steps(cfg, tools, folder, storage_root, result)
        exit_code = 0 if not result.failures else 1
    except BackupError as exc:
        result.failures.append(redact(str(exc), secrets))
    except Exception as exc:  # noqa: BLE001 - recorded in the manifest, never re-raised raw
        result.failures.append(redact(f"unexpected {type(exc).__name__}: {exc}", secrets))
    finally:
        result.finished_at = datetime.now(UTC)
        manifest = build_manifest(result)
        path = write_manifest(folder, manifest)
        log(f"Manifest: {path}")

    if result.failures:
        print("\nBACKUP FAILED - do not trust this folder:", file=sys.stderr)
        for failure in result.failures:
            print(f"  - {redact(failure, secrets)}", file=sys.stderr)
        return 1

    log(
        f"OK  {result.downloaded_object_count} objects, "
        f"{manifest['total_bytes']:,} bytes total, "
        f"alumni rows {result.row_counts.get('public.alumni')}, "
        f"{manifest['duration_seconds']}s"
    )
    return exit_code


def _run_steps(
    cfg: BackupConfig,
    tools: Mapping[str, str],
    folder: pathlib.Path,
    storage_root: pathlib.Path,
    result: RunResult,
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

    log("Downloading objects...")
    mismatched: list[str] = []
    for index, obj in enumerate(objects, start=1):
        dest = safe_object_destination(storage_root, obj.path)
        data = client.download(obj.path)
        if len(data) != obj.size:
            mismatched.append(f"{obj.path}: listing {obj.size} bytes, downloaded {len(data)}")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        result.downloaded_object_count += 1
        result.downloaded_total_bytes += len(data)
        result.files[f"storage/{BUCKET}/{obj.path}"] = FileRecord(
            len(data), hashlib.sha256(data).hexdigest()
        )
        if index % 50 == 0 or index == len(objects):
            log(f"  {index}/{len(objects)}")

    # 4. checks.
    log("Verifying...")
    toc = pg_restore_list(tools, cfg, dump)
    ok_toc = toc_lists_table(toc, "public", "alumni")
    result.checks["pg_restore_list_public_alumni"] = ok_toc
    if not ok_toc:
        result.failures.append("pg_restore --list database.dump does not list TABLE public alumni.")

    ok_storage = (
        not mismatched
        and result.downloaded_object_count == result.listing_object_count
        and result.downloaded_total_bytes == result.listing_total_bytes
    )
    result.checks["storage_totals_match"] = ok_storage
    if not ok_storage:
        result.failures.append(
            f"Storage mismatch: listing reported {result.listing_object_count} objects / "
            f"{result.listing_total_bytes} bytes, downloaded {result.downloaded_object_count} / "
            f"{result.downloaded_total_bytes}."
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
    args = parser.parse_args(argv)

    secrets: tuple[str, ...] = tuple(
        v
        for v in (
            os.environ.get("BACKUP_DATABASE_URL"),
            os.environ.get("BACKUP_SUPABASE_SERVICE_ROLE_KEY"),
        )
        if v
    )
    try:
        cfg = load_config(os.environ)
        secrets = cfg.secrets
        tools = find_tools()
        if args.dry_run:
            return dry_run(cfg, tools)
        return run_backup(cfg, tools)
    except BackupError as exc:
        print("ERROR: " + redact(str(exc), secrets), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted. The folder written so far is INCOMPLETE; delete it.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - last resort, must not leak secrets
        print(
            "UNEXPECTED ERROR: " + redact(f"{type(exc).__name__}: {exc}", secrets),
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    sys.exit(main())
