# Survey Reset Runbook (#445)

_Status: written 2026-09-16 for the one-off reset that must run on PRODUCTION
immediately before the first real survey campaign (graduation year 2020, #522).
The SQL is `database/maintenance/445-survey-reset.sql`; this page is the order
of operations around it. Hand-run by the engineer. Claude does not touch the
prod database._

## What this does and why

Everything in the survey tables on prod today is test data. Left in place it
does two things: the Progress tab and the applied/rejected counts start non-zero
(there is no KPI table; they derive live from `survey_send_log` +
`survey_responses`), and — worse — leftover send-log rows at the cycle the real
campaign resolves to make the sender skip those alumni **silently** (#357). The
script deletes `survey_send_log`, `survey_responses`, `survey_schedule` and
`survey_campaign_retirement` for ALL years in one transaction, keeps
`survey_reset_log`, and never touches `survey_send_config`, `survey_email_message`,
`alumni` or `audit_logs`.

Neither in-app reset does this. `POST /survey/alumni/{id}/reset` is per-alumnus
and deletes nothing by design; `new-cycle` makes a cohort sendable again but
leaves the historical counts. Hence SQL.

Decisions already made (2026-09-16), baked into the script header:
scope = all years; no real responses to keep ("start both KPIs at zero");
keep `survey_reset_log`; Resend plan is Free, so caps stay 100/day and
3000/month and the script only SELECTs the config row.

## Timing

The daily cron is `0 18 * * *` UTC = 12:00 noon Utah (`POST /survey/cron/run` ->
`run_due_schedules`). It sends stage 0 for any schedule whose `start_date` is
on or before today's UTC date. Everything below, including creating the 2020
campaign, has to be finished **before noon** for the initial email to go out
today.

## Order of operations

### (a) Backup first

Run `scripts/backup_prod.py` (written in parallel with this runbook). If it is
not there or fails, the fallback is a plain `pg_dump` through the **SESSION**
pooler on port **5432** with the prod migrations connection string (the same
one CI's "Migrate database" job uses). At minimum dump the four tables the
script deletes, data-only, so a restore is a single `psql -f`:

```
pg_dump "<prod SESSION-pooler URL, :5432>" --data-only \
  -t survey_send_log -t survey_responses -t survey_schedule -t survey_campaign_retirement \
  > 445-survey-backup-2026-09-16.sql
```

A full-database dump is fine too, just slower. Keep the file off the repo
(`.gitignore` does not know about it and gitleaks scans history).

### (b) Connect with psql, SESSION pooler, port 5432

```
psql "<prod SESSION-pooler URL, :5432>"
```

Never the transaction pooler on `:6543`: it does not keep a session, so the
temp scope table and the explicit `BEGIN` / `ROLLBACK` in Section 2 do not
behave as written there. `psql` is preferred over the Supabase SQL editor for
the same reason — the editor wraps every Run in its own transaction and caps
display at 100 rows. Section 1 and 3 are readable in the editor if needed;
Section 2 should be `psql`.

Confirm the target before anything else:

```
select current_database(), inet_server_addr();
```

and that the project ref in the URL is prod (`njobhhdopwdodvzosrns`), not dev.

### (c) Section 1 — dry run, SELECT only

Paste Section 1 as a whole. Read every query, but **1.2 is the one that
matters**: one row per graduation year with `resolved_current_cycle` (the
cycle `current_cycle_seq()` would pick today) and `sends_at_resolved_cycle`.
Any year with `sends_at_resolved_cycle > 0` is a cohort that would be skipped
right now — that is the condition the reset removes. Note the `total_rows` in
1.1; Section 2 must report the same numbers.

1.5 lists who submitted — the decision is that none of it is real, but read it.
1.6 shows `applied` responses: deleting the row does not undo what it already
wrote into the profile (Q3 in the script header stays a judgment call). 1.8
lists staged photo paths that will be orphaned (see the end of this page).

### (d) Section 2 — as-is first (ROLLBACK), then COMMIT

1. Paste Section 2 exactly as written. It runs the four deletes inside a
   transaction, prints `rows_deleted` per step, runs the in-transaction checks,
   and **rolls back**. Nothing changes. Compare the `rows_deleted` against 1.1.
   The `#357 landmine check` at the bottom must return **zero rows**.
2. Edit two lines at the very bottom: comment out `ROLLBACK;` and uncomment
   `COMMIT;`. Doing only the second is harmless (the ROLLBACK still wins);
   the commit only happens when the ROLLBACK line is gone.
3. Paste Section 2 again. Same numbers, ending in `COMMIT`.

Leave step 5 (`survey_reset_log`) commented out. There is no UPDATE in the
file; do not add one.

### (e) Section 3 — re-verify, SELECT only

Paste Section 3. Expected:

- 3.0 echoes `ALL YEARS`, identical to 1.0.
- 3.1: every `in_scope_rows` is 0 (and, for an all-years run, every `total_rows`).
- 3.2: `config_rows = 1`, `singleton_ok = true`, `caps_match_free_plan = true`
  (enabled, 100, 3000).
- 3.3: `survey_reset_log` unchanged from 1.9.
- **3.4 must return zero rows.** A row here is an incomplete reset — do not
  create the campaign.
- 3.5 and 3.6: zero rows / zero.

Then in the app: hard-refresh the survey console Progress tab and confirm 2020
shows 0 recipients / 0 replied / 0 awaiting review / 0 applied / 0 rejected /
0 confirmed. If the SQL reads zero and the page does not, it is a stale page
or the dev environment.

### (f) Create the 2020 campaign in the console

In the app's survey console, create the campaign for graduation year 2020 with
`start_date` = today. Do it through the console, not by inserting a
`survey_schedule` row — the console resolves the cycle through
`current_cycle_seq` (it will be cycle 1 on a clean slate).

Before noon, run the **dry run** for 2020 (the console's send preview;
`POST /survey/campaigns/2020/send` defaults to `dry_run=true` and claims
nothing). Read the recipient count. A recipient count of **zero** is the
silent-skip failure, not an empty cohort — stop and re-check 3.4. The
recipients breakdown (`/survey/campaigns/2020/recipients`, the console's send
confirmation) partitions the cohort: suppressed (deceased / do not contact),
already responded, unreachable (no usable email), eligible. After the reset
`already_responded` must be 0.

At 12:00 noon Utah the cron sends stage 0. Reminders follow at +7 and +14 days
on their own; do not create them (the one-week reminder is already shipped).

If the campaign is created after the noon run has already happened, the cron
will send stage 0 at the next noon (the day-1 ceiling is still stage 0). To send
today instead, use the console's real send (`dry_run=false`) — a real send to a
year with no campaign creates one anchored to today (#405).

## Traps

- **Free-plan cap.** `survey_send_config` is 100/day, 3000/month. If the 2020
  cohort with an email address is larger than 100, the initial email is spread
  over consecutive noon runs — 100 today, the rest tomorrow, and so on. That is
  expected. The cron is idempotent: it only sends what the log says is still
  owed, and the same cap bounds the console's manual send.
- **The Supabase SQL editor** caps display at 100 rows and wraps each Run in
  its own transaction. Section 2 belongs in `psql`.
- **Never `:6543`** for this. Session pooler `:5432` only.
- **Orphaned staged photos.** Deleted responses leave their `survey-pending/`
  blobs in the headshots bucket; the nightly headshot sweep deliberately skips
  that prefix, so they never age out. A separate storage-API script,
  `scripts/survey_pending_orphans.py` (written in parallel), removes them
  **after** the reset has committed. It is not SQL and not part of the script.
- **Applied test responses** already wrote into `alumni` and left
  `audit_logs.source = 'survey'` rows. The script does not revert those; 1.6
  says whether any exist.
- The script lives in `database/maintenance/`, which `database/migrate.sh` does
  not read — CI will never run it. Do not move it into `database/migrations/`.

## Rollback

`psql "<prod SESSION-pooler URL, :5432>" -f 445-survey-backup-2026-09-16.sql`
restores the four tables from the data-only dump in (a). Only do this before
the real campaign has sent anything; afterwards the send log contains real
rows and a restore would overwrite them.
