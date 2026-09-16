# Backups

`scripts/backup_prod.py` takes a full backup of the production Supabase project
and verifies it (api #535). It runs two ways:

- **by hand** — the section right below; nothing is pruned, everything is
  downloaded;
- **every Sunday at 03:00** — the Windows scheduled task described in
  [Weekly automatic backups](#weekly-automatic-backups), which runs the same
  script with `--incremental --keep 8`.

One run produces one folder:

```
<BACKUP_DIR>/2026-09-16T150000Z/
  database.dump            public schema, pg_dump custom format (restore with pg_restore)
  auth.sql                 auth schema, plain SQL (the staff accounts; see below)
  storage/headshots/...    every object in the headshots bucket, same paths as the bucket
  MANIFEST.json            what was captured, sizes, sha256s, row counts, checks
```

Supabase's own backups (Pro, 7 days) do not include the bucket. This folder is
the only copy of the photos anywhere, and the database and the photos are a
matched pair: keep the folder together.

## What the folder is

Treat it as two things at once:

- **Every alumnus in one place.** `database.dump` is the whole `public` schema:
  contact details, employment, interactions, survey answers. FERPA-covered.
- **A credential artifact.** `auth.sql` holds `auth.users`, including the
  `encrypted_password` hashes (kept so a restore keeps logins working), and
  `auth.identities`. Live session material is deliberately excluded from the
  dump — refresh tokens, sessions, MFA/TOTP secrets, one-time tokens, in-flight
  OAuth state (`AUTH_SESSION_TABLES` in the script). After a restore, staff
  sign in again and re-enrol MFA. Even without those rows, a password hash
  file is exactly what an attacker wants: handle the folder like a key, not
  like a spreadsheet.

Rules that follow from that:

1. The destination must be a **local, unsynced** folder on a BYU-managed,
   disk-encrypted machine (BitLocker on). The script refuses a path that looks
   cloud-synced (OneDrive, Dropbox, iCloud, Google Drive, Box, or anything
   under `%OneDrive%`) unless `BACKUP_ALLOW_SYNCED_DIR=1` is set deliberately.
   Documents and Desktop are often redirected into OneDrive on managed
   machines — check before choosing them.
2. Only the engineer opens the folder. Nobody else needs it, including the
   admins who use the app.
3. Old runs: the weekly task prunes to the newest 8 OK runs on its own. A
   hand run never deletes anything, so after a hand run, delete by hand what
   is not needed. A folder of full dumps that nobody deletes is a growing
   liability, not a growing safety margin.
4. Taking a backup is not recorded anywhere except the folder's own
   `MANIFEST.json` and `<BACKUP_DIR>/LAST-RUN.json` — the script runs outside
   the app with the service key and cannot write to the app's audit tables.

## Taking one by hand

### Before the first run

1. PostgreSQL client tools on PATH: `pg_dump --version` should answer
   `pg_dump (PostgreSQL) 17.x`. On this Windows machine they are in
   `C:\Program Files\PostgreSQL\17\bin`. The major version must be at or above
   the server's (pg_dump 17 can dump a 15 or 17 server; pg_dump 15 cannot dump 17).
2. Python 3.12 or newer. The script is standard library only; no venv needed.
3. A destination folder OUTSIDE the repo, on BYU-controlled hardware (plan
   option A). The script refuses a path inside the repository.

### Environment variables

| Variable | Value |
|---|---|
| `BACKUP_DATABASE_URL` | Session pooler URL, port 5432, password included (see below) |
| `BACKUP_SUPABASE_URL` | `https://njobhhdopwdodvzosrns.supabase.co` |
| `BACKUP_SUPABASE_SERVICE_ROLE_KEY` | The `service_role` key from Project Settings > API keys. It is a secret |
| `BACKUP_DIR` | Absolute path of the destination root, e.g. `D:\fa-backups` |
| `BACKUP_EXPECT_PROJECT_REF` | `njobhhdopwdodvzosrns` (prod). The script refuses to run unless both URLs name this project |
| `BACKUP_SLACK_WEBHOOK_URL` | Optional. A Slack incoming-webhook URL; one redacted line is posted on FAILURE only, never on success |

Session pooler URL: Supabase dashboard > the prod project > Connect > Method
"Session pooler". Copy the URI, replace `[YOUR-PASSWORD]` with the database
password. It must end in `:5432/postgres`. The transaction pooler (`:6543`) is
refused: pg_dump needs session-level features it does not provide. If the
password contains `@`, `/`, `:` or `#`, percent-encode those characters
(`@` becomes `%40`).

Set the variables in the shell session only. Never put them in a file inside
the repo and never paste them into chat. gitleaks scans the whole history.

### The command

PowerShell, from the repo root:

```powershell
$env:BACKUP_DATABASE_URL = "postgresql://postgres.njobhhdopwdodvzosrns:...@aws-0-....pooler.supabase.com:5432/postgres"
$env:BACKUP_SUPABASE_URL = "https://njobhhdopwdodvzosrns.supabase.co"
$env:BACKUP_SUPABASE_SERVICE_ROLE_KEY = "..."
$env:BACKUP_DIR = "D:\fa-backups"
$env:BACKUP_EXPECT_PROJECT_REF = "njobhhdopwdodvzosrns"

python scripts\backup_prod.py --dry-run   # validates config and tools, connects to nothing
python scripts\backup_prod.py             # the backup
```

Linux/macOS: same variables with `export`, then `python3 scripts/backup_prod.py`.

Run the dry run first. It prints the destination folder, the tool versions and
the plan, and exits 2 on any configuration problem. The real run takes a few
minutes (583 objects, ~135 MB) and prints progress every 50 objects.

Exit codes: 0 good, 1 backup taken but a check failed (do not trust the
folder), 2 configuration or tooling problem (nothing contacted), 3 unexpected
error. Secrets are redacted from every message.

### What a good MANIFEST.json looks like

```json
{
  "status": "ok",
  "project_ref": "njobhhdopwdodvzosrns",
  "started_at": "2026-09-16T15:00:00Z",
  "finished_at": "2026-09-16T15:03:12Z",
  "duration_seconds": 192.4,
  "tools": { "pg_dump": "pg_dump (PostgreSQL) 17.6", "..." : "..." },
  "server_version": "17.4",
  "database": {
    "row_counts": { "public.alumni": 1523, "public.survey_responses": 41,
                    "public.survey_send_log": 900, "auth.users": 15 },
    "alumni_row_floor": 1400,
    "auth_dump_mode": "schema+data"
  },
  "storage": { "bucket": "headshots", "capture_mode": "full", "previous_run": null,
               "listing_object_count": 583, "listing_total_bytes": 98000000,
               "downloaded_object_count": 583, "downloaded_total_bytes": 98000000,
               "reused_object_count": 0, "reused_total_bytes": 0,
               "removed_count": 0, "removed_since_previous": [],
               "file_count_on_disk": 583 },
  "files": { "database.dump": { "bytes": 41000000, "sha256": "..." },
             "storage/headshots/abc.jpg": { "bytes": 61234, "sha256": "...",
                                            "etag": "...", "updated_at": "..." },
             "...": "..." },
  "checks": { "alumni_row_floor": true, "pg_restore_list_public_alumni": true,
              "storage_totals_match": true, "dumps_non_empty": true },
  "failures": []
}
```

Read it as: `status` is `ok`, every value under `checks` is `true`,
`failures` is empty, `public.alumni` is around 1,500, downloaded plus reused
equals the listing count, `database.dump` is tens of MB not KB.

`capture_mode` is `full` for a hand run and `incremental` for a weekly run
that found a previous OK run to compare against (`previous_run` names it);
`full (no previous OK run)` means the weekly run had nothing to compare
against and downloaded everything. `removed_since_previous` lists objects that
were in the previous run but are no longer in the bucket: expected when a
photo was deleted in the app, worth a look if it is long.

`auth_dump_mode` is `schema+data` when the full auth dump worked. If the
`postgres` role could not read Supabase-owned objects in the auth schema, the
script falls back to `data-only` and records the first error in
`auth_dump_note`. Data-only is acceptable: a restore into a fresh Supabase
project already has the auth schema, so the rows are all it needs.

### When it fails

| Message | What to do |
|---|---|
| `Missing required environment variable(s)` | Set them in this shell session. Each is listed above |
| `uses port 6543 (the TRANSACTION pooler)` | Copy the Session pooler URI instead; it ends in `:5432/postgres` |
| `does not reference project ...` | The URL points at a different project (dev is `tnnhhnzglyfqolxdojyb`). Fix the URL, not the expected ref |
| `Not on PATH: pg_dump ...` | Add `C:\Program Files\PostgreSQL\17\bin` to PATH, open a new shell |
| `BACKUP_DIR must be OUTSIDE the repository` | Point it at a folder that is not inside fa-web-api |
| `public.alumni has N rows, below the floor` | Stops BEFORE dumping. Either the wrong database or the data is not what we think. Check the URL; do not lower the floor to make it pass |
| `pg_dump ... failed (exit 1)` with `server version mismatch` | The client is older than the server. Install the newer PostgreSQL client tools |
| `pg_dump ... failed` with a password or authentication error | The URL password is wrong or a special character needs percent-encoding |
| `Storage listing failed with HTTP 400/401/403` | Wrong or truncated service_role key, or the URL is not this project's |
| `Bucket 'headshots' listed zero objects` | Wrong project or a key without storage access. Prod has ~583 objects |
| `Storage mismatch: listing reported X ... captured Y` | A download was short or failed. Delete the folder and rerun; if it repeats, note the object names in `failures` |
| `pg_restore --list database.dump does not list TABLE public alumni` | The dump is not a usable dump of prod. Delete the folder and rerun |
| `Destination already exists` | Two runs in the same second. Run again |
| `Refusing to prune: BACKUP_DIR resolves to a ... root` / `inside the repository` | `--keep` was given for a folder pruning must never touch. Nothing was dumped or deleted. Use a sub-folder |
| `PRUNE FAILED (the backup itself is OK)` | The run's folder is good and its MANIFEST says `ok`, but an old folder could not be deleted (a file open in another program, permissions). Delete it by hand; exit code is 1 and the failure marker is set so the task shows red |
| Interrupted (Ctrl+C) | The folder is incomplete. Delete it and rerun |

Any folder whose MANIFEST says `"status": "FAILED"` is not a backup. Delete it
or rename it so it is never mistaken for one.

## Weekly automatic backups

The same script, every Sunday at 03:00 local time, from a Windows Scheduled
Task on the engineer's machine (plan Phases 4 and 5, mirroring the
change-request intake task). The task is **prod only by construction**: the
project ref and Supabase URL are literals inside `scripts/backup-scheduled.ps1`,
not values in the config, and the script refuses a database URL that does not
name that project. No config edit can point it at dev.

### Install once

From the **main checkout** (not a worktree: the task remembers the path), in
PowerShell:

```powershell
.\scripts\backup-install-task.ps1
```

It asks, without echoing, for the prod session-pooler URL, the service_role
key, `BACKUP_DIR`, and an optional Slack webhook, then for your Windows
password (Task Scheduler needs it to run the task while you are signed out;
Windows stores it, the script does not). It refuses a URL that is not on
`:5432`, that does not name the prod project, or that names dev: "this task
backs up prod only".

The four values are saved with **DPAPI, encrypted to your Windows account**, in
`%LOCALAPPDATA%\fa-backups\config.xml`. That file is unreadable from any other
account or machine, which is also why the task must run as you. If your Windows
password changes, run the installer again. Running it again at any time is
safe: it overwrites the config and replaces the task.

`.\scripts\backup-install-task.ps1 -WhatIf` walks through the questions and
saves nothing.

### What happens each Sunday

1. Task Scheduler wakes the machine if needed, waits for the network, and runs
   `backup-scheduled.ps1` (2-hour limit; if 03:00 was missed, as soon as
   possible afterwards).
2. The wrapper decrypts the config into `BACKUP_*` variables for that process
   only, pins `BACKUP_EXPECT_PROJECT_REF` to prod, and runs
   `python scripts\backup_prod.py --incremental --keep 8`.
3. The database (`public` + `auth`) is dumped **in full, every time**.
4. The bucket is captured **incrementally**: the fresh listing is compared with
   the most recent previous run whose MANIFEST says `ok`; only new or changed
   objects (size, etag or `updated_at` differ) are downloaded, and the rest are
   copied from that previous folder after their size and sha256 are checked
   against its manifest (a copy that does not verify is downloaded instead).
   Each run folder is still complete on its own. With no previous OK run
   (the first Sunday, or after a failure), everything is downloaded.
5. The usual checks run. Then, **only if the run is OK**, the oldest OK runs
   are deleted so that 8 remain (two months of weeklies). FAILED runs,
   folders without a manifest, and anything not named like a run are never
   deleted; a human looks at those.
6. `<BACKUP_DIR>\LAST-RUN.json` is written every time:
   `{status, run_folder, finished_at_utc, downloaded, reused, pruned}`.

### Where to look

| What | Where |
|---|---|
| The backups | `BACKUP_DIR` (chosen at install) |
| Was the last run good | `<BACKUP_DIR>\LAST-RUN.json` — `status` is `OK` |
| **Failure marker** | `<BACKUP_DIR>\LAST-RUN-FAILED.txt` exists only while the last run failed (UTC time, run folder, redacted reason). The next success removes it |
| Slack | one line, `prod backup FAILED at <time>: <reason>`, only if a webhook was configured; nothing on success |
| Console output of each run | `%LOCALAPPDATA%\fa-backups\logs\backup-<timestamp>.log` (newest 20 kept) |
| Task Scheduler | `Get-ScheduledTaskInfo -TaskName 'FA Backup (prod, weekly)'` — `LastTaskResult` is the script's exit code (0 good) |
| Run it now | `Start-ScheduledTask -TaskName 'FA Backup (prod, weekly)'` |

A Sunday with the machine off is a Sunday with no backup: `StartWhenAvailable`
runs it at the next boot, but nothing runs while the laptop is in a bag. Check
`LAST-RUN.json` on Mondays until that is a habit.

### Changing how many runs are kept

Re-run the installer with a different number; it replaces the task:

```powershell
.\scripts\backup-install-task.ps1 -Keep 12
```

(`-Keep` is passed to `backup-scheduled.ps1`, which passes it to the script as
`--keep`. Hand runs never prune unless `--keep` is given explicitly.)

### Uninstall

```powershell
.\scripts\backup-install-task.ps1 -Uninstall
```

removes the task and deletes `config.xml` (the stored secrets). Backups
already taken and the logs folder are left where they are.

### Still not done: the restore rehearsal

Phase 3 of the plan — restoring a backup into a throwaway Supabase project and
diffing row counts — has **not been done**. Until it is, every folder the task
writes is a backup that has never been proven to restore. It should be done
once, soon, and written up in the restore section below.

## Restore caveats (from the plan)

- Restoring the database does NOT restore the photos. `storage.objects` rows
  are metadata; the files must be uploaded back into the `headshots` bucket
  from `storage/headshots/` separately, at the same paths, or the app renders
  broken images for photos it believes exist.
- Rehearse on a NEW project, never in place. On Pro, create a throwaway
  Supabase project, `pg_restore --no-owner --no-privileges -d <new session URL>
  database.dump`, then `psql -f auth.sql`, then re-upload the photos, then
  diff row counts against the MANIFEST. Delete the throwaway project after.
- Expect `already exists` errors for schemas, extensions and some constraints
  when restoring into a fresh Supabase project; it ships with `auth` and
  `storage` pre-created. Those are normal. Missing tables or a row count that
  does not match the MANIFEST are not.
- The `auth.sql` restore recreates staff accounts and their ids so the
  `created_by` and actor-email trails still point at real users. Restore it
  before anyone logs in to the new project.
- Custom role passwords are not in the dump. If a rebuild needs them, they
  are set again by hand.

Until a restore rehearsal has been done once (plan Phase 3), treat these
backups as probably good, not proven.
