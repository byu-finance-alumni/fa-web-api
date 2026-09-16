"""Unit tests for the pure parts of scripts/backup_prod.py (api #535).

Nothing here touches a network, a database or a PostgreSQL binary: the
listing walker takes an injectable page function, the config loader takes an
env mapping, and the two subprocess-facing helpers are exercised through
monkeypatched ``subprocess.run``. The point is that the SAFETY logic (wrong
project refused, transaction pooler refused, folder escapes refused, a
mismatching download counted as a failure) is proven without a credential.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from scripts import backup_prod as bp

REF = "njobhhdopwdodvzosrns"
KEY = "service-role-key-abcdef0123456789"
PASSWORD = "p%40ss-w0rd"  # percent-encoded "@" to prove both spellings redact


def good_env(tmp_path: pathlib.Path, **overrides: str) -> dict[str, str]:
    env = {
        "BACKUP_DATABASE_URL": (
            f"postgresql://postgres.{REF}:{PASSWORD}@aws-0-us-east-1.pooler.supabase.com"
            ":5432/postgres"
        ),
        "BACKUP_SUPABASE_URL": f"https://{REF}.supabase.co",
        "BACKUP_SUPABASE_SERVICE_ROLE_KEY": KEY,
        "BACKUP_DIR": str(tmp_path / "backups"),
        "BACKUP_EXPECT_PROJECT_REF": REF,
    }
    env.update(overrides)
    return env


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


# --- Configuration -----------------------------------------------------------


def test_load_config_accepts_a_correct_session_pooler_setup(tmp_path):
    cfg = bp.load_config(good_env(tmp_path), repo_root=REPO_ROOT)
    assert cfg.expect_project_ref == REF
    assert cfg.backup_dir == tmp_path / "backups"


def test_load_config_names_every_missing_variable(tmp_path):
    env = good_env(tmp_path)
    del env["BACKUP_DIR"]
    env["BACKUP_SUPABASE_SERVICE_ROLE_KEY"] = "   "
    with pytest.raises(bp.ConfigError) as exc:
        bp.load_config(env, repo_root=REPO_ROOT)
    message = str(exc.value)
    assert "BACKUP_DIR" in message
    assert "BACKUP_SUPABASE_SERVICE_ROLE_KEY" in message
    assert "BACKUP_DATABASE_URL" not in message


def test_transaction_pooler_port_is_refused_with_a_clear_message(tmp_path):
    env = good_env(tmp_path)
    env["BACKUP_DATABASE_URL"] = env["BACKUP_DATABASE_URL"].replace(":5432/", ":6543/")
    with pytest.raises(bp.ConfigError) as exc:
        bp.load_config(env, repo_root=REPO_ROOT)
    message = str(exc.value)
    assert "6543" in message and "5432" in message
    assert "session" in message.lower()
    # The refusal must not echo the URL (it carries the password).
    assert PASSWORD not in message


@pytest.mark.parametrize("port", ["5433", "6544", ""])
def test_any_port_other_than_5432_is_refused(tmp_path, port):
    env = good_env(tmp_path)
    host_port = f":{port}" if port else ""
    env["BACKUP_DATABASE_URL"] = (
        f"postgresql://postgres.{REF}:{PASSWORD}@aws-0-us-east-1.pooler.supabase.com"
        f"{host_port}/postgres"
    )
    with pytest.raises(bp.ConfigError):
        bp.load_config(env, repo_root=REPO_ROOT)


def test_non_postgres_scheme_is_refused():
    with pytest.raises(bp.ConfigError):
        bp.check_database_url("mysql://user:pw@host:5432/db")


def test_direct_host_carries_the_ref_in_the_hostname(tmp_path):
    env = good_env(tmp_path)
    env["BACKUP_DATABASE_URL"] = (
        f"postgresql://postgres:{PASSWORD}@db.{REF}.supabase.co:5432/postgres"
    )
    cfg = bp.load_config(env, repo_root=REPO_ROOT)
    assert cfg.expect_project_ref == REF


def test_wrong_project_ref_in_database_url_aborts(tmp_path):
    env = good_env(tmp_path)
    env["BACKUP_DATABASE_URL"] = (
        f"postgresql://postgres.tnnhhnzglyfqolxdojyb:{PASSWORD}"
        "@aws-0-us-east-1.pooler.supabase.com:5432/postgres"
    )
    with pytest.raises(bp.ConfigError) as exc:
        bp.load_config(env, repo_root=REPO_ROOT)
    assert "BACKUP_DATABASE_URL" in str(exc.value)
    assert PASSWORD not in str(exc.value)


def test_wrong_project_ref_in_supabase_url_aborts(tmp_path):
    env = good_env(tmp_path, BACKUP_SUPABASE_URL="https://tnnhhnzglyfqolxdojyb.supabase.co")
    with pytest.raises(bp.ConfigError) as exc:
        bp.load_config(env, repo_root=REPO_ROOT)
    assert "BACKUP_SUPABASE_URL" in str(exc.value)


def test_expect_ref_matching_neither_side_aborts(tmp_path):
    env = good_env(tmp_path, BACKUP_EXPECT_PROJECT_REF="abcdefabcdefabcdefab")
    with pytest.raises(bp.ConfigError):
        bp.load_config(env, repo_root=REPO_ROOT)


def test_supabase_url_must_be_https(tmp_path):
    env = good_env(tmp_path, BACKUP_SUPABASE_URL=f"http://{REF}.supabase.co")
    with pytest.raises(bp.ConfigError):
        bp.load_config(env, repo_root=REPO_ROOT)


def test_project_ref_must_be_a_whole_segment_not_a_substring(tmp_path):
    longer = REF + "x"
    env = good_env(
        tmp_path,
        BACKUP_DATABASE_URL=(
            f"postgresql://postgres.{longer}:{PASSWORD}@aws-0-us-east-1.pooler.supabase.com"
            ":5432/postgres"
        ),
        BACKUP_SUPABASE_URL=f"https://{longer}.supabase.co",
    )
    with pytest.raises(bp.ConfigError):
        bp.load_config(env, repo_root=REPO_ROOT)
    assert bp._has_ref_segment(f"postgres.{REF}", REF)
    assert bp._has_ref_segment(f"db.{REF}.supabase.co", REF.upper())
    assert not bp._has_ref_segment(f"db.{REF}x.supabase.co", REF)


def test_backup_dir_inside_the_repo_is_refused(tmp_path):
    env = good_env(tmp_path, BACKUP_DIR=str(REPO_ROOT / "backups"))
    with pytest.raises(bp.ConfigError) as exc:
        bp.load_config(env, repo_root=REPO_ROOT)
    assert "OUTSIDE" in str(exc.value)


def test_backup_dir_under_a_onedrive_segment_is_refused(tmp_path):
    env = good_env(tmp_path, BACKUP_DIR=str(tmp_path / "OneDrive" / "Documents" / "fa-backups"))
    with pytest.raises(bp.ConfigError) as exc:
        bp.load_config(env, repo_root=REPO_ROOT)
    assert "cloud-synced" in str(exc.value)
    assert "OneDrive" in str(exc.value)


def test_backup_dir_inside_the_onedrive_env_root_is_refused_even_without_the_name(tmp_path):
    sync_root = tmp_path / "Work Files"
    env = good_env(
        tmp_path, BACKUP_DIR=str(sync_root / "fa-backups"), OneDriveCommercial=str(sync_root)
    )
    with pytest.raises(bp.ConfigError) as exc:
        bp.load_config(env, repo_root=REPO_ROOT)
    assert "%OneDriveCommercial%" in str(exc.value)


def test_synced_backup_dir_allowed_only_with_explicit_override(tmp_path):
    env = good_env(
        tmp_path,
        BACKUP_DIR=str(tmp_path / "Dropbox" / "fa-backups"),
        BACKUP_ALLOW_SYNCED_DIR="1",
    )
    cfg = bp.load_config(env, repo_root=REPO_ROOT)
    assert cfg.backup_dir == tmp_path / "Dropbox" / "fa-backups"


def test_split_password_moves_the_decoded_password_out_of_the_url():
    url = f"postgresql://postgres.{REF}:p%40ss-w0rd%2Fx@aws-0.pooler.supabase.com:5432/postgres"
    stripped, password = bp.split_password(url)
    assert stripped == f"postgresql://postgres.{REF}@aws-0.pooler.supabase.com:5432/postgres"
    assert password == "p@ss-w0rd/x"
    assert bp.split_password(stripped) == (stripped, None)


def test_run_db_tool_passes_the_password_only_through_the_environment(tmp_path, monkeypatch):
    cfg = bp.load_config(good_env(tmp_path), repo_root=REPO_ROOT)
    seen: dict[str, object] = {}

    def fake_run(args, **kwargs):
        seen["args"] = list(args)
        seen["env"] = dict(kwargs["env"])
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(bp.subprocess, "run", fake_run)
    bp.run_db_tool(["psql", "-tAc", "select 1"], cfg, what="probe")
    joined = " ".join(seen["args"])  # type: ignore[arg-type]
    assert PASSWORD not in joined and "%40" not in joined
    assert seen["env"]["PGPASSWORD"] == "p@ss-w0rd"  # decoded for libpq  # type: ignore[index]
    assert seen["env"]["PGSSLMODE"] == "require"  # type: ignore[index]


def test_auth_dump_never_captures_session_or_mfa_rows(tmp_path):
    calls: list[list[str]] = []

    def fake_run_tool(argv, secrets, *, what, env=None):
        calls.append(list(argv))
        pathlib.Path(argv[-2].removeprefix("--file=")).write_text("-- dump")
        return None

    cfg = bp.load_config(good_env(tmp_path), repo_root=REPO_ROOT)
    with patch.object(bp, "run_tool", fake_run_tool):
        mode, note = bp.pg_dump_auth({"pg_dump": "pg_dump"}, cfg, tmp_path / "auth.sql")
    assert mode == "schema+data" and note == ""
    argv = calls[0]
    for table in ("refresh_tokens", "sessions", "mfa_factors", "one_time_tokens", "flow_state"):
        assert f"--exclude-table-data=auth.{table}" in argv
    assert "--schema=auth" in argv


def test_backup_dir_must_be_absolute(tmp_path):
    env = good_env(tmp_path, BACKUP_DIR="backups")
    with pytest.raises(bp.ConfigError):
        bp.load_config(env, repo_root=REPO_ROOT)


# --- Redaction ---------------------------------------------------------------


def test_secrets_include_url_key_and_both_password_spellings(tmp_path):
    cfg = bp.load_config(good_env(tmp_path), repo_root=REPO_ROOT)
    assert cfg.database_url in cfg.secrets
    assert KEY in cfg.secrets
    assert "p%40ss-w0rd" in cfg.secrets
    assert "p@ss-w0rd" in cfg.secrets


def test_redact_replaces_every_secret_longest_first(tmp_path):
    cfg = bp.load_config(good_env(tmp_path), repo_root=REPO_ROOT)
    text = f"failed: {cfg.database_url} key={KEY} pw=p@ss-w0rd"
    out = bp.redact(text, cfg.secrets)
    assert KEY not in out
    assert "p@ss-w0rd" not in out
    assert "pooler.supabase.com" not in out  # the whole URL went, not just the password
    assert out == "failed: [REDACTED] key=[REDACTED] pw=[REDACTED]"


# --- Tools -------------------------------------------------------------------


def test_find_tools_names_what_is_missing():
    with pytest.raises(bp.ConfigError) as exc:
        bp.find_tools(which=lambda name: None if name == "pg_restore" else f"/bin/{name}")
    assert "pg_restore" in str(exc.value)
    assert "pg_dump" not in str(exc.value).split("Not on PATH:")[1].split(".")[0]


def test_find_tools_returns_paths_when_all_present():
    tools = bp.find_tools(which=lambda name: f"C:/pg/bin/{name}.exe")
    assert set(tools) == {"pg_dump", "pg_restore", "psql"}


# --- Bucket walking ----------------------------------------------------------


def make_bucket(tree: dict[str, dict[str, int] | None]) -> bp.ListPageFn:
    """``tree`` maps a prefix ("" for root) to {name: size} rows; ``None`` size
    marks a folder placeholder (metadata None), the way Supabase returns it."""

    calls: list[tuple[str, int, int]] = []

    def list_page(prefix: str, limit: int, offset: int) -> list[dict]:
        calls.append((prefix, limit, offset))
        rows = []
        for name, size in sorted(tree.get(prefix, {}).items()):
            if size is None:
                rows.append({"name": name, "id": None, "metadata": None})
            else:
                rows.append({"name": name, "id": "x", "metadata": {"size": size}})
        return rows[offset : offset + limit]

    list_page.calls = calls  # type: ignore[attr-defined]
    return list_page


def test_walk_bucket_pages_past_100_and_reports_every_object():
    root = {f"{i:04d}.jpg": 10 + i for i in range(250)}
    list_page = make_bucket({"": root})
    objects = bp.walk_bucket(list_page, page_size=100)
    assert len(objects) == 250
    assert sum(o.size for o in objects) == sum(root.values())
    offsets = [offset for prefix, _, offset in list_page.calls if prefix == ""]
    assert offsets == [0, 100, 200]


def test_walk_bucket_stops_on_a_short_page_without_an_extra_request():
    list_page = make_bucket({"": {"a.jpg": 1, "b.jpg": 2}})
    bp.walk_bucket(list_page, page_size=100)
    assert list_page.calls == [("", 100, 0)]


def test_walk_bucket_requests_a_final_empty_page_when_the_page_is_exactly_full():
    list_page = make_bucket({"": {f"{i}.jpg": 1 for i in range(100)}})
    objects = bp.walk_bucket(list_page, page_size=100)
    assert len(objects) == 100
    assert [c[2] for c in list_page.calls] == [0, 100]


def test_walk_bucket_descends_into_virtual_folders_with_full_keys():
    list_page = make_bucket(
        {
            "": {"alice.jpg": 100, "survey-pending": None},
            "survey-pending/": {"tok1.jpg": 7, "nested": None},
            "survey-pending/nested/": {"deep.png": 3},
        }
    )
    objects = bp.walk_bucket(list_page, page_size=100)
    assert [(o.path, o.size) for o in objects] == [
        ("alice.jpg", 100),
        ("survey-pending/nested/deep.png", 3),
        ("survey-pending/tok1.jpg", 7),
    ]
    prefixes = {prefix for prefix, _, _ in list_page.calls}
    assert prefixes == {"", "survey-pending/", "survey-pending/nested/"}


def test_walk_bucket_tolerates_names_that_already_carry_the_prefix():
    def list_page(prefix, limit, offset):
        if prefix == "":
            return [{"name": "f", "metadata": None}]
        if prefix == "f/" and offset == 0:
            return [{"name": "f/x.jpg", "metadata": {"size": 1}}]
        return []

    objects = bp.walk_bucket(list_page, page_size=100)
    assert [o.path for o in objects] == ["f/x.jpg"]


def test_walk_bucket_fails_when_an_object_has_no_size():
    list_page = make_bucket({"": {"a.jpg": 1}})

    def sizeless(prefix, limit, offset):
        rows = list_page(prefix, limit, offset)
        for row in rows:
            row["metadata"] = {"mimetype": "image/jpeg"}
        return rows

    with pytest.raises(bp.CheckError):
        bp.walk_bucket(sizeless, page_size=100)


def test_walk_bucket_refuses_to_page_forever(monkeypatch):
    monkeypatch.setattr(bp, "MAX_LIST_PAGES", 3)

    def endless(prefix, limit, offset):
        return [{"name": f"{offset + i}.jpg", "metadata": {"size": 1}} for i in range(limit)]

    with pytest.raises(bp.CheckError):
        bp.walk_bucket(endless, page_size=10)


# --- Destination safety ------------------------------------------------------


def test_safe_object_destination_nests_under_the_storage_root(tmp_path):
    dest = bp.safe_object_destination(tmp_path, "survey-pending/abc.jpg")
    assert dest == tmp_path / "survey-pending" / "abc.jpg"


@pytest.mark.parametrize(
    "key",
    ["../etc/passwd", "a/../../b", "/abs.jpg", "\\win.jpg", "a\\b.jpg", "C:evil", "a//b", "", "."],
)
def test_safe_object_destination_refuses_escapes(tmp_path, key):
    with pytest.raises(bp.CheckError):
        bp.safe_object_destination(tmp_path, key)


# --- pg_restore TOC ----------------------------------------------------------


TOC = """;
; Archive created at 2026-09-16 09:00:00 MDT
;     dbname: postgres
;     TOC Entries: 412
;     Format: CUSTOM
;
; Selected TOC Entries:
;
5; 2615 16384 SCHEMA - public postgres
215; 1259 16400 TABLE public alumni postgres
216; 1259 16410 TABLE public alumni_contact_info postgres
2861; 0 16400 TABLE DATA public alumni postgres
3001; 2606 16500 CONSTRAINT public alumni alumni_pkey postgres
"""


def test_toc_lists_table_finds_the_table_entry():
    assert bp.toc_lists_table(TOC, "public", "alumni")


def test_toc_lists_table_does_not_match_a_prefix_or_another_schema():
    assert not bp.toc_lists_table(TOC, "public", "alumn")
    assert not bp.toc_lists_table(TOC, "auth", "alumni")
    assert not bp.toc_lists_table(TOC, "public", "users")


def test_toc_lists_table_accepts_a_data_only_toc():
    data_only = "2861; 0 16400 TABLE DATA public alumni postgres\n"
    assert bp.toc_lists_table(data_only, "public", "alumni")


def test_toc_lists_table_ignores_comment_lines_and_junk():
    assert not bp.toc_lists_table("; TABLE public alumni postgres\n\n   \n", "public", "alumni")


# --- Manifest ----------------------------------------------------------------


def test_utc_stamp_has_no_colons_and_is_utc():
    stamp = bp.utc_stamp(datetime(2026, 9, 16, 15, 4, 5, tzinfo=UTC))
    assert stamp == "2026-09-16T150405Z"
    assert ":" not in stamp


def test_build_manifest_shape_and_totals():
    started = datetime(2026, 9, 16, 15, 0, 0, tzinfo=UTC)
    result = bp.RunResult(project_ref=REF, started_at=started)
    result.finished_at = datetime(2026, 9, 16, 15, 0, 42, tzinfo=UTC)
    result.tools = {"pg_dump": "pg_dump (PostgreSQL) 17.6"}
    result.server_version = "17.4"
    result.row_counts = {"public.alumni": 1523, "auth.users": 15}
    result.files = {
        "database.dump": bp.FileRecord(1000, "a" * 64),
        "auth.sql": bp.FileRecord(200, "b" * 64),
        "storage/headshots/x.jpg": bp.FileRecord(50, "c" * 64),
        "storage/headshots/survey-pending/y.jpg": bp.FileRecord(25, "d" * 64),
    }
    result.auth_dump_mode = "schema+data"
    result.listing_object_count = result.downloaded_object_count = 2
    result.listing_total_bytes = result.downloaded_total_bytes = 75
    result.checks = {"pg_restore_list_public_alumni": True, "storage_totals_match": True}

    manifest = bp.build_manifest(result)

    assert manifest["status"] == "ok"
    assert manifest["project_ref"] == REF
    assert manifest["started_at"] == "2026-09-16T15:00:00Z"
    assert manifest["finished_at"] == "2026-09-16T15:00:42Z"
    assert manifest["duration_seconds"] == 42.0
    assert manifest["tools"]["pg_dump"].startswith("pg_dump")
    assert manifest["database"]["row_counts"]["public.alumni"] == 1523
    assert manifest["database"]["alumni_row_floor"] == bp.ALUMNI_ROW_FLOOR
    assert manifest["storage"]["bucket"] == "headshots"
    assert manifest["storage"]["listing_object_count"] == 2
    assert manifest["storage"]["file_count_on_disk"] == 2
    assert manifest["total_bytes"] == 1275
    assert manifest["files"]["database.dump"] == {"bytes": 1000, "sha256": "a" * 64}
    assert list(manifest["files"]) == sorted(manifest["files"])
    assert manifest["failures"] == []
    json.dumps(manifest)  # serialisable


def test_build_manifest_marks_failures():
    result = bp.RunResult(project_ref=REF, started_at=datetime.now(UTC))
    result.failures = ["Storage mismatch: listing reported 583 objects, downloaded 582."]
    manifest = bp.build_manifest(result)
    assert manifest["status"] == "FAILED"
    assert manifest["failures"] == result.failures


def test_write_manifest_round_trips(tmp_path):
    result = bp.RunResult(project_ref=REF, started_at=datetime.now(UTC))
    path = bp.write_manifest(tmp_path, bp.build_manifest(result))
    assert path.name == "MANIFEST.json"
    assert json.loads(path.read_text(encoding="utf-8"))["project_ref"] == REF


def test_sha256_file(tmp_path):
    f = tmp_path / "blob"
    f.write_bytes(b"hello")
    size, digest = bp.sha256_file(f)
    assert size == 5
    assert digest == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


# --- Subprocess wrapper (mocked) ---------------------------------------------


def test_run_tool_redacts_secrets_out_of_a_failure(monkeypatch, tmp_path):
    cfg = bp.load_config(good_env(tmp_path), repo_root=REPO_ROOT)

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args, 1, stdout="", stderr=f"pg_dump: error: connection to {cfg.database_url} failed"
        )

    monkeypatch.setattr(bp.subprocess, "run", fake_run)
    with pytest.raises(bp.BackupError) as exc:
        bp.run_tool(["pg_dump", cfg.database_url], cfg.secrets, what="pg_dump")
    assert cfg.database_url not in str(exc.value)
    assert "[REDACTED]" in str(exc.value)
    assert "exit 1" in str(exc.value)


def test_run_tool_requires_tls_and_a_connect_timeout_by_default(monkeypatch, tmp_path):
    cfg = bp.load_config(good_env(tmp_path), repo_root=REPO_ROOT)
    seen = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    monkeypatch.setattr(bp.subprocess, "run", fake_run)
    monkeypatch.delenv("PGSSLMODE", raising=False)
    bp.run_tool(["psql"], cfg.secrets, what="psql")
    assert seen["PGSSLMODE"] == "require"
    assert seen["PGCONNECT_TIMEOUT"] == "30"


def test_pg_dump_auth_falls_back_to_data_only(monkeypatch, tmp_path):
    cfg = bp.load_config(good_env(tmp_path), repo_root=REPO_ROOT)
    dest = tmp_path / "auth.sql"
    attempts: list[list[str]] = []

    def fake_run(args, **kwargs):
        attempts.append(list(args))
        if "--data-only" not in args:
            dest.write_text("partial", encoding="utf-8")
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr="pg_dump: error: permission denied for schema auth"
            )
        dest.write_text("COPY auth.users ...", encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(bp.subprocess, "run", fake_run)
    mode, note = bp.pg_dump_auth({"pg_dump": "pg_dump"}, cfg, dest)
    assert mode == "data-only"
    assert "permission denied" in note
    assert len(attempts) == 2
    assert "--data-only" in attempts[1]
    assert dest.read_text(encoding="utf-8") == "COPY auth.users ..."
    # The URL is the LAST argument on both attempts and carries NO password —
    # that travels in PGPASSWORD, so a process listing never shows it.
    stripped, _ = bp.split_password(cfg.database_url)
    assert attempts[0][-1] == stripped
    assert attempts[1][-1] == stripped
    assert PASSWORD not in " ".join(attempts[0]) and "p%40ss" not in " ".join(attempts[0])


def test_pg_dump_auth_prefers_a_full_dump(monkeypatch, tmp_path):
    cfg = bp.load_config(good_env(tmp_path), repo_root=REPO_ROOT)
    dest = tmp_path / "auth.sql"
    monkeypatch.setattr(
        bp.subprocess,
        "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
    )
    mode, note = bp.pg_dump_auth({"pg_dump": "pg_dump"}, cfg, dest)
    assert (mode, note) == ("schema+data", "")


def test_count_rows_required_surfaces_the_error(monkeypatch, tmp_path):
    cfg = bp.load_config(good_env(tmp_path), repo_root=REPO_ROOT)
    monkeypatch.setattr(
        bp.subprocess,
        "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 2, stdout="", stderr="boom"),
    )
    assert bp.count_rows({"psql": "psql"}, cfg, "auth.users") is None
    with pytest.raises(bp.BackupError):
        bp.count_rows({"psql": "psql"}, cfg, "public.alumni", required=True)


def test_count_rows_refuses_odd_table_names(tmp_path):
    cfg = bp.load_config(good_env(tmp_path), repo_root=REPO_ROOT)
    with pytest.raises(bp.BackupError):
        bp.count_rows({"psql": "psql"}, cfg, 'public.alumni"; drop table x; --')


# --- main() ------------------------------------------------------------------


def test_main_with_bad_config_exits_2_and_never_prints_the_secret(monkeypatch, capsys, tmp_path):
    env = good_env(tmp_path)
    env["BACKUP_DATABASE_URL"] = env["BACKUP_DATABASE_URL"].replace(":5432/", ":6543/")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    code = bp.main([])
    out = capsys.readouterr()
    assert code == 2
    assert "6543" in out.err
    assert PASSWORD not in out.err and KEY not in out.err


def test_main_dry_run_connects_to_nothing(monkeypatch, capsys, tmp_path):
    for name, value in good_env(tmp_path).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(bp, "_repo_root", lambda: REPO_ROOT)
    monkeypatch.setattr(bp.shutil, "which", lambda name: f"/pg/bin/{name}")
    monkeypatch.setattr(bp, "tool_version", lambda path: "pg (PostgreSQL) 17.6")

    def no_subprocess(*args, **kwargs):
        raise AssertionError("dry run must not start a process")

    def no_network(*args, **kwargs):
        raise AssertionError("dry run must not open a connection")

    monkeypatch.setattr(bp.subprocess, "run", no_subprocess)
    monkeypatch.setattr(bp.urllib.request, "urlopen", no_network)

    code = bp.main(["--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "DRY RUN" in out
    assert REF in out
    assert PASSWORD not in out and KEY not in out
    assert not (tmp_path / "backups").exists()


# --- run_backup end to end, everything faked ---------------------------------


class FakeStorage:
    """Stands in for StorageClient: a two-object bucket, one under a folder."""

    objects = {"alice.jpg": b"A" * 100, "survey-pending/tok.jpg": b"B" * 25}
    short_download: str | None = None  # key whose download comes back truncated

    def __init__(self, *args, **kwargs):
        pass

    def list_page(self, prefix, limit, offset):
        if offset:
            return []
        if prefix == "":
            return [
                {"name": "alice.jpg", "metadata": {"size": 100}},
                {"name": "survey-pending", "metadata": None},
            ]
        if prefix == "survey-pending/":
            return [{"name": "tok.jpg", "metadata": {"size": 25}}]
        return []

    def download(self, key):
        data = self.objects[key]
        if key == self.short_download:
            return data[:-1]
        return data


def fake_pg(counts: dict[str, str], toc: str = TOC):
    def run(args, **kwargs):
        exe = args[0]
        if exe == "pg_dump":
            target = next(a for a in args if a.startswith("--file="))[len("--file=") :]
            pathlib.Path(target).write_bytes(b"PGDMP fake " + " ".join(args).encode())
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if exe == "psql":
            sql = args[args.index("-tAc") + 1]
            if sql == "SHOW server_version":
                return subprocess.CompletedProcess(args, 0, stdout="17.4\n", stderr="")
            for table, value in counts.items():
                schema, name = table.split(".")
                if f'"{schema}"."{name}"' in sql:
                    if value == "ERR":
                        return subprocess.CompletedProcess(args, 1, stdout="", stderr="no")
                    return subprocess.CompletedProcess(args, 0, stdout=value + "\n", stderr="")
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="no such table")
        if exe == "pg_restore":
            return subprocess.CompletedProcess(args, 0, stdout=toc, stderr="")
        raise AssertionError(f"unexpected tool {exe}")

    return run


TOOLS = {"pg_dump": "pg_dump", "pg_restore": "pg_restore", "psql": "psql"}
GOOD_COUNTS = {
    "public.alumni": "1523",
    "public.survey_responses": "40",
    "public.survey_send_log": "ERR",
    "auth.users": "15",
}


@pytest.fixture
def faked(monkeypatch, tmp_path):
    monkeypatch.setattr(bp, "StorageClient", FakeStorage)
    monkeypatch.setattr(bp, "tool_version", lambda path: f"{path} (PostgreSQL) 17.6")
    monkeypatch.setattr(FakeStorage, "short_download", None)
    return bp.load_config(good_env(tmp_path), repo_root=REPO_ROOT)


def read_manifest(cfg: bp.BackupConfig) -> tuple[pathlib.Path, dict]:
    folders = list(cfg.backup_dir.iterdir())
    assert len(folders) == 1
    folder = folders[0]
    return folder, json.loads((folder / "MANIFEST.json").read_text(encoding="utf-8"))


def test_run_backup_happy_path_writes_everything(faked, monkeypatch, capsys):
    cfg = faked
    monkeypatch.setattr(bp.subprocess, "run", fake_pg(GOOD_COUNTS))
    now = datetime(2026, 9, 16, 15, 0, 0, tzinfo=UTC)

    code = bp.run_backup(cfg, TOOLS, now=now)

    assert code == 0
    folder, manifest = read_manifest(cfg)
    assert folder.name == "2026-09-16T150000Z"
    assert (folder / "database.dump").exists()
    assert (folder / "auth.sql").exists()
    headshots = folder / "storage" / "headshots"
    assert (headshots / "alice.jpg").read_bytes() == b"A" * 100
    assert (headshots / "survey-pending" / "tok.jpg").read_bytes() == b"B" * 25
    assert manifest["status"] == "ok"
    assert manifest["database"]["row_counts"] == {
        "public.alumni": 1523,
        "public.survey_responses": 40,
        "public.survey_send_log": None,  # tolerated: informational only
        "auth.users": 15,
    }
    assert manifest["database"]["auth_dump_mode"] == "schema+data"
    assert manifest["storage"]["listing_object_count"] == 2
    assert manifest["storage"]["downloaded_total_bytes"] == 125
    assert manifest["checks"] == {
        "alumni_row_floor": True,
        "pg_restore_list_public_alumni": True,
        "storage_totals_match": True,
        "dumps_non_empty": True,
    }
    assert set(manifest["files"]) == {
        "database.dump",
        "auth.sql",
        "storage/headshots/alice.jpg",
        "storage/headshots/survey-pending/tok.jpg",
    }
    out = capsys.readouterr()
    assert "OK" in out.out
    assert cfg.database_url not in out.out + out.err
    assert KEY not in out.out + out.err


def test_run_backup_refuses_before_dumping_when_alumni_is_below_the_floor(
    faked, monkeypatch, capsys
):
    cfg = faked
    monkeypatch.setattr(bp.subprocess, "run", fake_pg({**GOOD_COUNTS, "public.alumni": "12"}))

    code = bp.run_backup(cfg, TOOLS)

    assert code == 1
    folder, manifest = read_manifest(cfg)
    assert manifest["status"] == "FAILED"
    assert manifest["checks"]["alumni_row_floor"] is False
    assert any("below the floor" in f for f in manifest["failures"])
    assert not (folder / "database.dump").exists()  # nothing was dumped
    assert "BACKUP FAILED" in capsys.readouterr().err


def test_run_backup_fails_when_a_download_is_short(faked, monkeypatch):
    cfg = faked
    monkeypatch.setattr(bp.subprocess, "run", fake_pg(GOOD_COUNTS))
    monkeypatch.setattr(FakeStorage, "short_download", "alice.jpg")

    code = bp.run_backup(cfg, TOOLS)

    assert code == 1
    folder, manifest = read_manifest(cfg)
    assert manifest["status"] == "FAILED"
    assert manifest["checks"]["storage_totals_match"] is False
    assert manifest["storage"]["downloaded_object_count"] == 1
    assert manifest["storage"]["listing_object_count"] == 2
    assert any("alice.jpg" in f for f in manifest["failures"])
    # The truncated object was NOT written as if it were good.
    assert not (folder / "storage" / "headshots" / "alice.jpg").exists()


def test_run_backup_fails_when_the_toc_lacks_public_alumni(faked, monkeypatch):
    cfg = faked
    toc = "216; 1259 16410 TABLE public alumni_contact_info postgres\n"
    monkeypatch.setattr(bp.subprocess, "run", fake_pg(GOOD_COUNTS, toc=toc))

    code = bp.run_backup(cfg, TOOLS)

    assert code == 1
    _, manifest = read_manifest(cfg)
    assert manifest["checks"]["pg_restore_list_public_alumni"] is False
    assert any("pg_restore --list" in f for f in manifest["failures"])


def test_run_backup_records_a_tool_failure_with_redaction(faked, monkeypatch, capsys):
    cfg = faked

    def failing(args, **kwargs):
        if args[0] == "psql" and "SHOW server_version" in args:
            return subprocess.CompletedProcess(args, 0, stdout="17.4\n", stderr="")
        return subprocess.CompletedProcess(
            args, 1, stdout="", stderr=f"FATAL: could not connect to {cfg.database_url}"
        )

    monkeypatch.setattr(bp.subprocess, "run", failing)

    code = bp.run_backup(cfg, TOOLS)

    assert code == 1
    _, manifest = read_manifest(cfg)
    assert manifest["status"] == "FAILED"
    joined = json.dumps(manifest) + capsys.readouterr().err
    assert cfg.database_url not in joined
    assert "[REDACTED]" in joined


def test_run_backup_refuses_an_existing_destination(faked, monkeypatch):
    cfg = faked
    now = datetime(2026, 9, 16, 15, 0, 0, tzinfo=UTC)
    (cfg.backup_dir / bp.utc_stamp(now)).mkdir(parents=True)
    monkeypatch.setattr(bp.subprocess, "run", fake_pg(GOOD_COUNTS))
    with pytest.raises(bp.BackupError):
        bp.run_backup(cfg, TOOLS, now=now)
