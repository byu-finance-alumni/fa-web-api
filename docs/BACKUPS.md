# Backups: how to take one by hand

`scripts/backup_prod.py` takes a full backup of the production Supabase project
and verifies it. It is run by hand (api #535, Phase 1 + 2 of BACKUPS-PLAN.md).
There is no schedule and no pruning yet.

One run produces one folder:

```
<BACKUP_DIR>/2026-09-16T150000Z/
  database.dump            public schema, pg_dump custom format (restore with pg_restore)
  auth.sql                 auth schema, plain SQL (the staff logins)
  storage/headshots/...    every object in the headshots bucket, same paths as the bucket
  MANIFEST.json            what was captured, sizes, sha256s, row counts, checks
```

Supabase's own backups (Pro, 7 days) do not include the bucket. This folder is
the only copy of the photos anywhere, and the database and the photos are a
matched pair: keep the folder together.

## Before the first run

1. PostgreSQL client tools on PATH: `pg_dump --version` should answer
   `pg_dump (PostgreSQL) 17.x`. On this Windows machine they are in
   `C:\Program Files\PostgreSQL\17\bin`. The major version must be at or above
   the server's (pg_dump 17 can dump a 15 or 17 server; pg_dump 15 cannot dump 17).
2. Python 3.12 or newer. The script is standard library only; no venv needed.
3. A destination folder OUTSIDE the repo, on BYU-controlled hardware (plan
   option A). The script refuses a path inside the repository.

## Environment variables

| Variable | Value |
|---|---|
| `BACKUP_DATABASE_URL` | Session pooler URL, port 5432, password included (see below) |
| `BACKUP_SUPABASE_URL` | `https://njobhhdopwdodvzosrns.supabase.co` |
| `BACKUP_SUPABASE_SERVICE_ROLE_KEY` | The `service_role` key from Project Settings > API keys. It is a secret |
| `BACKUP_DIR` | Absolute path of the destination root, e.g. `D:\fa-backups` |
| `BACKUP_EXPECT_PROJECT_REF` | `njobhhdopwdodvzosrns` (prod). The script refuses to run unless both URLs name this project |

Session pooler URL: Supabase dashboard > the prod project > Connect > Method
"Session pooler". Copy the URI, replace `[YOUR-PASSWORD]` with the database
password. It must end in `:5432/postgres`. The transaction pooler (`:6543`) is
refused: pg_dump needs session-level features it does not provide. If the
password contains `@`, `/`, `:` or `#`, percent-encode those characters
(`@` becomes `%40`).

Set the variables in the shell session only. Never put them in a file inside
the repo and never paste them into chat. gitleaks scans the whole history.

## The command

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

## What a good MANIFEST.json looks like

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
  "storage": { "bucket": "headshots", "listing_object_count": 583,
               "listing_total_bytes": 98000000, "downloaded_object_count": 583,
               "downloaded_total_bytes": 98000000, "file_count_on_disk": 583 },
  "files": { "database.dump": { "bytes": 41000000, "sha256": "..." }, "...": "..." },
  "checks": { "alumni_row_floor": true, "pg_restore_list_public_alumni": true,
              "storage_totals_match": true, "dumps_non_empty": true },
  "failures": []
}
```

Read it as: `status` is `ok`, every value under `checks` is `true`,
`failures` is empty, `public.alumni` is around 1,500, the listing and download
counts match each other, `database.dump` is tens of MB not KB.

`auth_dump_mode` is `schema+data` when the full auth dump worked. If the
`postgres` role could not read Supabase-owned objects in the auth schema, the
script falls back to `data-only` and records the first error in
`auth_dump_note`. Data-only is acceptable: a restore into a fresh Supabase
project already has the auth schema, so the rows are all it needs.

## When it fails

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
| `Storage mismatch: listing reported X ... downloaded Y` | A download was short or failed. Delete the folder and rerun; if it repeats, note the object names in `failures` |
| `pg_restore --list database.dump does not list TABLE public alumni` | The dump is not a usable dump of prod. Delete the folder and rerun |
| `Destination already exists` | Two runs in the same second. Run again |
| Interrupted (Ctrl+C) | The folder is incomplete. Delete it and rerun |

Any folder whose MANIFEST says `"status": "FAILED"` is not a backup. Delete it
or rename it so it is never mistaken for one.

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
