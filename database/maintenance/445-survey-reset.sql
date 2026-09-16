-- ============================================================================
-- 445-survey-reset.sql
-- Issue #445 — reset survey KPIs and campaign state on PROD before the first
--              real survey campaign goes out.
--
-- Drafted:   2026-08-29
-- Target:    PRODUCTION database (njobhhdopwdodvzosrns). Not dev.
-- Author:    drafted by Claude from the fa-web-api schema; NOT executed.
--            No database was connected to while writing this.
-- Runner:    Jake. Claude does not touch the prod database.
-- Runbook:   docs/SURVEY-RESET-RUNBOOK.md (the order of operations for the day).
--
-- Re-verified 2026-09-16 (against database/schema.sql, database/migrations/*.sql
-- and app/models/*.py at HEAD, and current_cycle_seq in app/services/survey_email.py):
--   * Every table and column this file references exists exactly as named:
--     survey_send_log (survey_send_log_id, graduation_year, alumni_id, stage,
--     cycle_seq, reset_seq, sent_at); survey_responses (survey_response_id,
--     alumni_id, graduation_year NULLABLE, payload, status, staged_photo_path,
--     cycle_seq, stage, submitted_at, reviewed_by_user_id, reviewed_at);
--     survey_schedule (graduation_year, start_date, status, paused_at, cycle_seq);
--     survey_campaign_retirement (graduation_year, cycle_seq);
--     survey_reset_log (alumni_id, reset_seq, reset_at);
--     survey_send_config (id, enabled, daily_limit, monthly_limit,
--     updated_by_user_id, created_at, updated_at); alumni (alumni_id,
--     first_name, last_name, graduation_year); alumni_contact_info (alumni_id,
--     personal_email, work_email); audit_logs.source. No schema drift found.
--   * survey_responses.status still has exactly four values
--     (pending | applied | rejected | confirmed) — matches query 1.4.
--   * Still zero foreign keys BETWEEN the survey tables (no `REFERENCES survey_*`
--     anywhere in schema.sql or the migrations), and no triggers on them. The
--     delete order below is still by meaning, not by constraint.
--   * current_cycle_seq() still resolves in exactly the order this file documents:
--     survey_schedule.cycle_seq -> max(survey_campaign_retirement.cycle_seq) + 1
--     -> 1 (FIRST_CYCLE). Query 1.2's COALESCE and check 3.4 reproduce it exactly.
--   * The Progress tab / applied / rejected counts still derive live from
--     survey_send_log + survey_responses (survey_schedule._cycle_progress); the
--     daily/monthly usage meter reads survey_send_log.sent_at. Clearing those two
--     tables zeroes both KPIs AND today's usage. There is still no KPI table.
--   * NEW SINCE THE DRAFT: `survey_email_message` (migration 2026-09-09, #524) —
--     the staff-edited survey email copy. It is NOT campaign state and this file
--     deliberately does not touch it. Same for the legacy `surveys` table.
--   * This file lives in database/maintenance/, which database/migrate.sh does
--     NOT glob (it only applies database/migrations/*.sql). CI will never run it.
--   * Edits made on re-verification: (1) Q1/Q2/Q4/Q5 marked ANSWERED below with
--     Jake's 2026-09-16 decisions; (2) Step 6's commented UPDATE template for
--     survey_send_config was REMOVED — the Resend plan is Free, the caps stay at
--     the schema defaults (100/day, 3000/month), and the file now only SELECTs
--     that row (1.7, 3.2); (3) check 3.2 now shows the expected cap values beside
--     the live ones. No SQL that writes was changed. Section 2 still ends in
--     ROLLBACK by default.
--
-- WHAT THIS DOES
--   Clears the survey campaign state accumulated during testing so the first
--   real campaign starts from zero: the send log, the responses, the per-year
--   schedule rows, and the deleted-campaign tombstones. It then re-verifies
--   that nothing was left behind in a half-state.
--
-- WHY IT MATTERS MORE THAN IT LOOKS (#357)
--   A graduation year can only be surveyed ONCE PER CYCLE and it FAILS
--   SILENTLY. `survey_send_log` carries the "already emailed" guard, scoped by
--   (graduation_year, alumni_id, stage, cycle_seq, reset_seq). If leftover test
--   rows sit at the same cycle the real campaign resolves to, the sender selects
--   zero targets, the campaign marks itself "completed", and a real cohort is
--   never contacted. There is no error to notice.
--
-- SAFETY MODEL
--   * SECTION 1 is SELECT-only. Safe to run alone, any number of times.
--   * SECTION 2 is the only writing section. It opens a transaction and ends in
--     ROLLBACK. That is the DEFAULT and it is deliberate — run it once as-is to
--     read the row counts, then make the one-line edit to COMMIT.
--     Failsafe: if you uncomment COMMIT but forget to comment out the ROLLBACK,
--     the ROLLBACK still executes first and the COMMIT is a no-op warning.
--     You cannot commit by accident; you have to remove the ROLLBACK.
--   * SECTION 3 is SELECT-only. Run after committing.
--
-- HOW TO RUN
--   Section by section, in order, in a SINGLE session (see the SCOPE PREAMBLE
--   note below). Read the output of each before moving on.
--   If you are using the Supabase SQL editor, remember it CAPS DISPLAY AT 100
--   ROWS — the per-year breakdowns below are aggregated specifically so they fit,
--   but do not trust a raw row listing to be complete.
--
--   PREFER psql FOR SECTION 2. The Supabase SQL editor wraps each Run in its own
--   transaction, so an explicit BEGIN/ROLLBACK inside it may warn or behave
--   differently than written, and the temp scope table may not survive between
--   Runs on a pooled connection. Each section re-creates the scope table at its
--   top so this is handled — but only if you run a whole section as ONE
--   execution. In psql, run the file section by section in a single session
--   (fa-web-api prod credentials, SESSION pooler on :5432).
--
-- ============================================================================
-- OPEN QUESTIONS — ANSWER THESE BEFORE RUNNING SECTION 2
-- ============================================================================
-- Status 2026-09-16: Q1, Q2, Q4 and Q5 ANSWERED by Jake. Q3 and Q6 stay as
-- documented (Q3 is a judgment call on query 1.6's output; Q6 is handled by a
-- separate storage script after the reset — see the runbook).
--
-- Q1. SCOPE: all years, or specific graduation years?
--     ANSWERED 2026-09-16: ALL YEARS. Leave every scope-preamble INSERT
--     commented out. Sections 1.0 and 3.0 must both echo 'ALL YEARS'.
--     (Original reasoning, still valid: the point of #445 is that everything
--     currently in these tables is TEST data from before the first real
--     campaign. A scoped reset is also the shape most likely to produce the
--     half-state trap — year A cleared, year B's stale send rows left behind at
--     a cycle the new campaign will resolve to.)
--
-- Q2. Are ANY rows in `survey_responses` on prod REAL alumni submissions?
--     ANSWERED 2026-09-16: NO real responses to keep — "we need to start both
--     KPIs at zero". Step 2 deletes every response. Still run query 1.5 first
--     and read it: if a row there is unexpectedly a real alum's real answers,
--     stop and re-decide before committing. Deletion is irreversible.
--     (Background: the app's own per-alumnus reset deliberately NEVER deletes
--     responses — Jake's 2026-08-05 call. This one-off does, because a leftover
--     response both inflates the KPIs and holds that alum out of reminders for
--     365 days via the recipient pool's "already replied" test.)
--
-- Q3. Do you also need to UNDO profile edits that a test survey response already
--     APPLIED? An `applied` response wrote its values into `alumni` and the
--     related tables and left an `audit_logs` row with source='survey'.
--     Deleting the response row does NOT revert those writes. This script
--     deliberately does not touch `alumni` or `audit_logs`. If test submissions
--     were applied against real alumni records on prod, that is a SEPARATE
--     cleanup pass and I have not scripted it. Section 1 query 1.6 tells you
--     whether any exist.
--
-- Q4. `survey_reset_log` — keep or clear?
--     ANSWERED 2026-09-16: KEEP (the default). Section 2 step 5 stays commented
--     out. Tradeoff written there; query 1.9 shows who carries a reset count.
--
-- Q5. `survey_send_config` — what are the correct PROD caps?
--     ANSWERED 2026-09-16: the Resend plan is FREE, so the caps stay at the
--     schema defaults — enabled=true, daily_limit=100, monthly_limit=3000.
--     This file only SELECTs the row (1.7 before, 3.2 after); there is no
--     UPDATE anywhere in it. If 1.7 shows anything other than 100/3000/enabled,
--     fix it through the console's send-cap screen, not by hand here.
--     Consequence for today: a cohort larger than 100 has its initial email
--     spread over consecutive noon cron runs. That is expected — the cron is
--     idempotent and only sends what is still owed.
--
-- Q6. Orphaned staged photos. `survey_responses.staged_photo_path` points at a
--     blob in the headshots bucket under `survey-pending/<id>`. Deleting the row
--     does NOT delete the blob, and the nightly headshot sweep explicitly SKIPS
--     the `survey-pending/` prefix (app/services/headshot_sweep.py), so those
--     objects are orphaned permanently. Section 1 query 1.8 lists the paths.
--     Cleaning them is a storage-API job, not SQL — out of scope for this file.
--
-- ============================================================================
-- THE CYCLE COUNTER — READ THIS BEFORE EDITING ANYTHING
-- ============================================================================
--
-- `cycle_seq` is an OPAQUE COUNTER. It must NEVER be derived from a date. A
-- campaign starting in late December sends its reminders in January, and
-- resuming a paused campaign shifts `start_date` forward, so a year-derived
-- cycle flips mid-campaign and re-sends the initial email to the whole cohort.
--
-- Where the current cycle for a year comes from (app/services/survey_email.py,
-- `current_cycle_seq`), in this exact order:
--   1. the year's `survey_schedule.cycle_seq`, if a schedule row exists;
--   2. otherwise max(`survey_campaign_retirement.cycle_seq`) + 1 for that year;
--   3. otherwise 1.
--
-- THIS IS WHY THE SCRIPT IS SAFE AS WRITTEN: deleting the schedule row, the
-- retirement rows AND the send-log rows for a year together drops it to case 3
-- — cycle 1, with no send-log rows at cycle 1 for the guard to trip over. That
-- is a genuinely clean slate, and it needs NO manual cycle number at all.
--
-- THIS IS THE WAY TO BREAK IT: delete the cycle holders (`survey_schedule`
-- and/or `survey_campaign_retirement`) for a year but leave that year's
-- `survey_send_log` rows in place. The year then resolves to cycle 1, the old
-- rows are also at cycle 1, the double-send guard sees them, and the whole
-- cohort is silently skipped. That is #357 exactly.
--
--   RULE: never clear a year's cycle holders without clearing that year's
--         send-log rows in the same transaction. Section 3 check 3.4 exists
--         solely to catch a violation of this rule.
--
-- THIS SCRIPT CONTAINS NO `UPDATE survey_schedule SET cycle_seq = ...`, ON
-- PURPOSE. Do not add one. If you decide to KEEP the send log for audit reasons
-- rather than delete it, the correct move is NOT to hand-pick a higher cycle
-- number — it is to leave the schedule row alone and use "start new cycle" in
-- the engineer console, which increments `cycle_seq` through the code path that
-- knows what the existing log rows are sitting at. A hand-typed number that
-- collides with existing rows fails silently in exactly the way above.
--
-- ============================================================================


-- ############################################################################
-- SCOPE PREAMBLE
-- ############################################################################
--
-- The scope is held in a TEMP table so it is not a write to any real table and
-- disappears when your session ends.
--
-- DEFAULT = ALL YEARS (leave the INSERT commented out).
-- To scope to specific graduation years, uncomment the INSERT and list them.
--
-- >>> IMPORTANT <<<
-- This preamble is repeated VERBATIM at the top of Sections 1, 2 and 3 so each
-- section is independently runnable. If you change the year list, change it in
-- ALL THREE PLACES. Sections 1 and 3 both echo the active scope back to you —
-- if those two echoes disagree, you verified a different scope than you deleted.
-- Stop and re-run.
--
-- ############################################################################


-- ############################################################################
-- ############################################################################
-- SECTION 1 — DRY RUN.  SELECT ONLY.  NOTHING HERE WRITES.
-- ############################################################################
-- ############################################################################

-- --- scope preamble (copy 1 of 3) -------------------------------------------
CREATE TEMP TABLE IF NOT EXISTS _reset_scope (graduation_year int);
DELETE FROM _reset_scope;
-- INSERT INTO _reset_scope (graduation_year) VALUES (2024), (2025);  -- <-- edit for a scoped reset
-- ----------------------------------------------------------------------------

-- 1.0  Echo the active scope. Compare this against the echo in Section 3.
SELECT
    CASE WHEN NOT EXISTS (SELECT 1 FROM _reset_scope)
         THEN 'ALL YEARS'
         ELSE 'SCOPED: ' || (SELECT string_agg(graduation_year::text, ', ' ORDER BY graduation_year)
                             FROM _reset_scope)
    END AS active_scope;


-- 1.1  BEFORE-COUNTS for every table this script touches.
--      `in_scope` is what Section 2 will delete. `total` is the whole table.
SELECT 'survey_send_log' AS table_name,
       (SELECT count(*) FROM survey_send_log) AS total_rows,
       (SELECT count(*) FROM survey_send_log s
         WHERE NOT EXISTS (SELECT 1 FROM _reset_scope)
            OR s.graduation_year IN (SELECT graduation_year FROM _reset_scope)) AS in_scope_rows
UNION ALL
SELECT 'survey_responses',
       (SELECT count(*) FROM survey_responses),
       (SELECT count(*) FROM survey_responses r
         WHERE NOT EXISTS (SELECT 1 FROM _reset_scope)
            OR r.graduation_year IN (SELECT graduation_year FROM _reset_scope))
UNION ALL
SELECT 'survey_schedule',
       (SELECT count(*) FROM survey_schedule),
       (SELECT count(*) FROM survey_schedule sc
         WHERE NOT EXISTS (SELECT 1 FROM _reset_scope)
            OR sc.graduation_year IN (SELECT graduation_year FROM _reset_scope))
UNION ALL
SELECT 'survey_campaign_retirement',
       (SELECT count(*) FROM survey_campaign_retirement),
       (SELECT count(*) FROM survey_campaign_retirement cr
         WHERE NOT EXISTS (SELECT 1 FROM _reset_scope)
            OR cr.graduation_year IN (SELECT graduation_year FROM _reset_scope))
UNION ALL
-- Not deleted by default. Shown so the number is on the record either way.
SELECT 'survey_reset_log (KEPT by default)',
       (SELECT count(*) FROM survey_reset_log),
       0
UNION ALL
-- Single-row config. Never deleted by this script.
SELECT 'survey_send_config (never deleted)',
       (SELECT count(*) FROM survey_send_config),
       0
ORDER BY 1;


-- 1.2  THE CYCLE PICTURE, per graduation year. This is the most important
--      query in Section 1 — read every row.
--
--      `resolved_current_cycle` reproduces `current_cycle_seq()` from the code:
--      schedule cycle, else max retired cycle + 1, else 1.
--
--      `sends_at_resolved_cycle` > 0 means the double-send guard is CURRENTLY
--      live for that year: a campaign started today would skip those alumni.
--      That is the silent-skip condition #445 exists to remove.
SELECT
    y.graduation_year,
    sc.cycle_seq                                  AS schedule_cycle_seq,
    sc.status                                     AS schedule_status,
    sc.start_date                                 AS schedule_start_date,
    sc.paused_at,
    ret.max_retired_cycle,
    COALESCE(sc.cycle_seq, ret.max_retired_cycle + 1, 1) AS resolved_current_cycle,
    COALESCE(sl.total_sends, 0)                   AS total_send_rows,
    COALESCE(sl.max_cycle, 0)                     AS max_send_cycle,
    (SELECT count(*) FROM survey_send_log x
      WHERE x.graduation_year = y.graduation_year
        AND x.cycle_seq = COALESCE(sc.cycle_seq, ret.max_retired_cycle + 1, 1))
                                                  AS sends_at_resolved_cycle,
    COALESCE(rs.total_responses, 0)               AS total_responses
FROM (
    SELECT graduation_year FROM survey_schedule
    UNION
    SELECT graduation_year FROM survey_send_log
    UNION
    SELECT graduation_year FROM survey_campaign_retirement
    UNION
    SELECT graduation_year FROM survey_responses WHERE graduation_year IS NOT NULL
) y
LEFT JOIN survey_schedule sc ON sc.graduation_year = y.graduation_year
LEFT JOIN (
    SELECT graduation_year, max(cycle_seq) AS max_retired_cycle
    FROM survey_campaign_retirement GROUP BY graduation_year
) ret ON ret.graduation_year = y.graduation_year
LEFT JOIN (
    SELECT graduation_year, count(*) AS total_sends, max(cycle_seq) AS max_cycle
    FROM survey_send_log GROUP BY graduation_year
) sl ON sl.graduation_year = y.graduation_year
LEFT JOIN (
    SELECT graduation_year, count(*) AS total_responses
    FROM survey_responses GROUP BY graduation_year
) rs ON rs.graduation_year = y.graduation_year
WHERE NOT EXISTS (SELECT 1 FROM _reset_scope)
   OR y.graduation_year IN (SELECT graduation_year FROM _reset_scope)
ORDER BY y.graduation_year;


-- 1.3  Send log broken down by year / cycle / stage / reset generation.
--      stage: 0 = initial, 1 = 1-week reminder, 2 = 2-week reminder.
--      reset_seq: 0 = sent before that alum was ever engineer-reset.
SELECT graduation_year, cycle_seq, stage, reset_seq,
       count(*)          AS rows,
       min(sent_at)      AS first_sent,
       max(sent_at)      AS last_sent
FROM survey_send_log s
WHERE NOT EXISTS (SELECT 1 FROM _reset_scope)
   OR s.graduation_year IN (SELECT graduation_year FROM _reset_scope)
GROUP BY 1, 2, 3, 4
ORDER BY 1, 2, 3, 4;


-- 1.4  Responses by year and status. These four values ARE the Progress tab and
--      the applied/rejected counts — there is no separate KPI table anywhere.
--      status: pending | applied | rejected | confirmed
--      ('confirmed' = "everything is correct", empty payload, counts as a reply
--       but never enters the review queue or the applied/rejected columns.)
SELECT COALESCE(graduation_year::text, '(null year)') AS graduation_year,
       status,
       count(*)      AS rows,
       min(submitted_at) AS first_submitted,
       max(submitted_at) AS last_submitted
FROM survey_responses r
WHERE NOT EXISTS (SELECT 1 FROM _reset_scope)
   OR r.graduation_year IN (SELECT graduation_year FROM _reset_scope)
GROUP BY 1, 2
ORDER BY 1, 2;


-- 1.5  WHO submitted — for answering Q2 (is any of this a real alum?).
--      Deliberately capped at 100 rows; if there are more than 100 responses on
--      prod, that itself is worth pausing over.
SELECT r.survey_response_id,
       r.graduation_year,
       r.status,
       r.cycle_seq,
       r.stage,
       r.submitted_at,
       a.first_name, a.last_name,
       ci.personal_email, ci.work_email,
       jsonb_object_keys_count.n AS payload_field_count
FROM survey_responses r
JOIN alumni a               ON a.alumni_id = r.alumni_id
LEFT JOIN alumni_contact_info ci ON ci.alumni_id = r.alumni_id
CROSS JOIN LATERAL (SELECT count(*) AS n
                    FROM jsonb_object_keys(COALESCE(r.payload, '{}'::jsonb))) jsonb_object_keys_count
WHERE NOT EXISTS (SELECT 1 FROM _reset_scope)
   OR r.graduation_year IN (SELECT graduation_year FROM _reset_scope)
ORDER BY r.submitted_at DESC
LIMIT 100;


-- 1.6  For Q3: responses already APPLIED to a real profile. Deleting the
--      response row does NOT revert what it wrote into `alumni` and friends.
--      If this returns rows, decide whether a separate revert pass is needed.
SELECT r.survey_response_id, r.alumni_id, a.first_name, a.last_name,
       r.graduation_year, r.reviewed_at, r.reviewed_by_user_id
FROM survey_responses r
JOIN alumni a ON a.alumni_id = r.alumni_id
WHERE r.status = 'applied'
  AND (NOT EXISTS (SELECT 1 FROM _reset_scope)
       OR r.graduation_year IN (SELECT graduation_year FROM _reset_scope))
ORDER BY r.reviewed_at DESC
LIMIT 100;


-- 1.7  For Q5 (answered: Resend FREE plan): the send caps. Expect exactly
--      enabled=true, daily_limit=100, monthly_limit=3000 — the schema defaults.
--      This file never UPDATEs this row; anything else here is fixed through the
--      console's send-cap screen. `enabled = false` means NO internal cap at all —
--      sends are then limited only by Resend.
SELECT id, enabled, daily_limit, monthly_limit,
       updated_by_user_id, created_at, updated_at
FROM survey_send_config;


-- 1.8  For Q6: staged photo blobs that deleting responses will orphan in the
--      headshots bucket. SQL cannot remove these; note the paths if you care.
SELECT survey_response_id, alumni_id, staged_photo_path, status, submitted_at
FROM survey_responses r
WHERE r.staged_photo_path IS NOT NULL
  AND (NOT EXISTS (SELECT 1 FROM _reset_scope)
       OR r.graduation_year IN (SELECT graduation_year FROM _reset_scope))
ORDER BY submitted_at DESC
LIMIT 100;


-- 1.9  Engineer resets on record. NOT deleted by default (Q4). Shown because
--      keeping them has one live consequence: an alum with N reset rows gets
--      `reset_seq = N` stamped on their NEXT survey email rather than 0. That is
--      harmless once the send log is empty — nothing can collide with them and
--      nothing predates them — but it is worth knowing before you see it.
SELECT rl.alumni_id, a.first_name, a.last_name,
       count(*)            AS reset_rows,
       max(rl.reset_seq)   AS max_reset_seq,
       max(rl.reset_at)    AS last_reset_at
FROM survey_reset_log rl
JOIN alumni a ON a.alumni_id = rl.alumni_id
GROUP BY rl.alumni_id, a.first_name, a.last_name
ORDER BY max(rl.reset_at) DESC
LIMIT 100;


-- ############################################################################
-- ############################################################################
-- SECTION 2 — THE WRITE.  DEFAULTS TO ROLLBACK.
-- ############################################################################
--
-- Run this ONCE exactly as written. It will report how many rows each step
-- would delete and then throw all of it away. Compare those numbers against
-- Section 1. Only then make the COMMIT edit at the bottom and run it again.
--
-- DELETE ORDER AND WHY
--   There are NO foreign keys between the survey tables — every FK in them
--   points outward, to alumni(alumni_id) ON DELETE CASCADE or to
--   users(user_id) ON DELETE SET NULL. So nothing here is forced by referential
--   integrity, and no order can make a step fail.
--
--   The order below is chosen for the MEANING of a partial run, not for FKs:
--     1. survey_send_log   — the double-send guard. Goes first, because it is
--                            the only table whose leftovers silently skip a real
--                            cohort. If anything goes wrong after step 1, the
--                            state left behind is harmless.
--     2. survey_responses  — replies. Must go with the send log; clearing one
--                            and not the other is the documented half-state.
--     3. survey_schedule   — the cycle holder.
--     4. survey_campaign_retirement — the other cycle holder.
--   Steps 3 and 4 go LAST because dropping a year's cycle while its send rows
--   still exist is the #357 landmine (see the cycle section at the top). Inside
--   one transaction this ordering is belt-and-braces; it matters if you ever run
--   these statements individually outside the BEGIN/ROLLBACK.
-- ############################################################################
-- ############################################################################

-- --- scope preamble (copy 2 of 3 — MUST MATCH COPY 1) -----------------------
CREATE TEMP TABLE IF NOT EXISTS _reset_scope (graduation_year int);
DELETE FROM _reset_scope;
-- INSERT INTO _reset_scope (graduation_year) VALUES (2024), (2025);  -- <-- edit for a scoped reset
-- ----------------------------------------------------------------------------

BEGIN;

-- Step 0: restate the scope inside the transaction, so the committed run's
-- output carries a record of what it was actually scoped to.
SELECT
    CASE WHEN NOT EXISTS (SELECT 1 FROM _reset_scope)
         THEN 'ALL YEARS'
         ELSE 'SCOPED: ' || (SELECT string_agg(graduation_year::text, ', ' ORDER BY graduation_year)
                             FROM _reset_scope)
    END AS deleting_with_scope;

-- Step 1 — survey_send_log. The double-send guard. Clear this FIRST.
WITH deleted AS (
    DELETE FROM survey_send_log s
    WHERE NOT EXISTS (SELECT 1 FROM _reset_scope)
       OR s.graduation_year IN (SELECT graduation_year FROM _reset_scope)
    RETURNING 1
)
SELECT 'survey_send_log' AS step, count(*) AS rows_deleted FROM deleted;

-- Step 2 — survey_responses. IRREVERSIBLE: these are submitted answers.
--          See Q2 at the top. Do not run this step until Q2 is answered.
WITH deleted AS (
    DELETE FROM survey_responses r
    WHERE NOT EXISTS (SELECT 1 FROM _reset_scope)
       OR r.graduation_year IN (SELECT graduation_year FROM _reset_scope)
    RETURNING 1
)
SELECT 'survey_responses' AS step, count(*) AS rows_deleted FROM deleted;
--
-- NOTE ON SCOPED RUNS ONLY: `survey_responses.graduation_year` is NULLABLE.
-- A scoped run (a non-empty _reset_scope) will NOT match rows whose
-- graduation_year is NULL, and will leave them behind. An ALL-YEARS run
-- deletes them, because the predicate short-circuits to true for every row.
-- If Section 1 query 1.4 showed a '(null year)' bucket and you are running
-- scoped, deal with those rows deliberately — uncomment this if that is what
-- you want:
--
-- WITH deleted AS (
--     DELETE FROM survey_responses WHERE graduation_year IS NULL RETURNING 1
-- )
-- SELECT 'survey_responses (null year)' AS step, count(*) AS rows_deleted FROM deleted;

-- Step 3 — survey_schedule. Per-year campaign rows, including the cycle counter.
WITH deleted AS (
    DELETE FROM survey_schedule sc
    WHERE NOT EXISTS (SELECT 1 FROM _reset_scope)
       OR sc.graduation_year IN (SELECT graduation_year FROM _reset_scope)
    RETURNING 1
)
SELECT 'survey_schedule' AS step, count(*) AS rows_deleted FROM deleted;

-- Step 4 — survey_campaign_retirement. Tombstones of deleted campaigns; the
--          other holder of a year's cycle number.
WITH deleted AS (
    DELETE FROM survey_campaign_retirement cr
    WHERE NOT EXISTS (SELECT 1 FROM _reset_scope)
       OR cr.graduation_year IN (SELECT graduation_year FROM _reset_scope)
    RETURNING 1
)
SELECT 'survey_campaign_retirement' AS step, count(*) AS rows_deleted FROM deleted;

-- ----------------------------------------------------------------------------
-- Step 5 — survey_reset_log.  *** NOT DELETED. LEFT COMMENTED ON PURPOSE. ***
--
-- #445 says to decide this one deliberately, so I have not decided it for you.
--
-- KEEP (the default, and my recommendation):
--   These rows are the audit of who made an alum surveyable again, when, and
--   how much it superseded. That trail is the only record of those actions —
--   nothing else in the database answers "who reset this person and when".
--   Keeping them costs nothing functionally: with the send log emptied there is
--   nothing left for a reset to supersede, and every eligibility query that
--   consults this table is asking "did anything predate the latest reset?",
--   whose answer becomes "no rows" either way.
--   The ONE visible consequence is cosmetic: an alum with N reset rows gets
--   `reset_seq = N` on their next send-log row instead of 0. Section 1 query 1.9
--   lists exactly who that is.
--
-- DELETE (only if you want the KPI surfaces to show a truly virgin state):
--   The profile Surveys tab shows a reset count per alum, and that count will
--   survive this reset if you keep the rows. If "reset 3 times" showing against
--   a real alum on day one of the real campaign is confusing enough to matter,
--   clear it. You lose the audit trail permanently; there is no other copy.
--
-- WITH deleted AS (
--     DELETE FROM survey_reset_log rl
--     WHERE rl.alumni_id IN (
--         SELECT a.alumni_id FROM alumni a
--         WHERE NOT EXISTS (SELECT 1 FROM _reset_scope)
--            OR a.graduation_year IN (SELECT graduation_year FROM _reset_scope)
--     )
--     RETURNING 1
-- )
-- SELECT 'survey_reset_log' AS step, count(*) AS rows_deleted FROM deleted;
-- ----------------------------------------------------------------------------

-- ----------------------------------------------------------------------------
-- Step 6 — survey_send_config.  *** NOT TOUCHED. ***
--
-- This is a single-row table (id pinned to 1 by a CHECK constraint). NEVER
-- DELETE the row — the scheduler expects it to exist.
--
-- Q5 was answered 2026-09-16: the Resend plan is FREE, so the caps stay at the
-- schema defaults (enabled=true, daily_limit=100, monthly_limit=3000) and this
-- file does not UPDATE the row. The commented UPDATE template that used to sit
-- here was removed so nothing in this file can change it by accident. If query
-- 1.7 shows other values, change them through the console's send-cap screen.
--
-- Reminder of the semantics:
--   enabled = true   -> the scheduler paces sends against daily/monthly_limit
--   enabled = false  -> NO internal cap; sends limited only by Resend
-- ----------------------------------------------------------------------------

-- In-transaction verification. With ROLLBACK still in place these numbers are
-- what WOULD be true after committing. Every in_scope count should read 0.
SELECT 'survey_send_log' AS table_name,
       (SELECT count(*) FROM survey_send_log s
         WHERE NOT EXISTS (SELECT 1 FROM _reset_scope)
            OR s.graduation_year IN (SELECT graduation_year FROM _reset_scope)) AS in_scope_remaining
UNION ALL
SELECT 'survey_responses',
       (SELECT count(*) FROM survey_responses r
         WHERE NOT EXISTS (SELECT 1 FROM _reset_scope)
            OR r.graduation_year IN (SELECT graduation_year FROM _reset_scope))
UNION ALL
SELECT 'survey_schedule',
       (SELECT count(*) FROM survey_schedule sc
         WHERE NOT EXISTS (SELECT 1 FROM _reset_scope)
            OR sc.graduation_year IN (SELECT graduation_year FROM _reset_scope))
UNION ALL
SELECT 'survey_campaign_retirement',
       (SELECT count(*) FROM survey_campaign_retirement cr
         WHERE NOT EXISTS (SELECT 1 FROM _reset_scope)
            OR cr.graduation_year IN (SELECT graduation_year FROM _reset_scope))
ORDER BY 1;

-- The #357 landmine check, run BEFORE you commit. This must return ZERO rows.
-- A row here means a year would be left with send-log rows but no cycle holder,
-- i.e. a real cohort that would be silently skipped. If this returns anything,
-- DO NOT COMMIT — widen the scope until it is empty.
SELECT s.graduation_year,
       count(*) AS orphaned_send_rows,
       'SEND ROWS SURVIVE BUT THE YEAR HAS NO SCHEDULE AND NO RETIREMENT — '
       'THE NEXT CAMPAIGN FOR THIS YEAR WOULD RESOLVE TO CYCLE 1 AND SILENTLY '
       'SKIP EVERYONE ALREADY LOGGED AT CYCLE 1' AS warning
FROM survey_send_log s
WHERE NOT EXISTS (SELECT 1 FROM survey_schedule sc WHERE sc.graduation_year = s.graduation_year)
  AND NOT EXISTS (SELECT 1 FROM survey_campaign_retirement cr WHERE cr.graduation_year = s.graduation_year)
  AND s.cycle_seq = 1
GROUP BY s.graduation_year
ORDER BY s.graduation_year;


-- ============================================================================
-- >>>>>>>>>>>>>>>>>>>>>>  THE ONE LINE THAT MATTERS  <<<<<<<<<<<<<<<<<<<<<<<<<
-- ============================================================================
--
-- ROLLBACK is the DEFAULT. Run the section as-is first and read the numbers.
--
-- To actually apply the reset you must do BOTH of these, in this order:
--   (a) comment out the ROLLBACK line below, AND
--   (b) uncomment the COMMIT line below.
--
-- Doing only (b) is safe: the ROLLBACK executes first and the COMMIT becomes a
-- harmless "there is no transaction in progress" warning. You cannot commit by
-- forgetting something — only by deliberately removing the ROLLBACK.
--
ROLLBACK;   -- <<<<< DEFAULT. Comment this out to apply for real.
-- COMMIT;  -- <<<<< Uncomment ONLY after the ROLLBACK line above is commented out.
-- ============================================================================


-- ############################################################################
-- ############################################################################
-- SECTION 3 — RE-VERIFY.  SELECT ONLY.  Run after committing.
-- ############################################################################
-- ############################################################################

-- --- scope preamble (copy 3 of 3 — MUST MATCH COPIES 1 AND 2) ---------------
CREATE TEMP TABLE IF NOT EXISTS _reset_scope (graduation_year int);
DELETE FROM _reset_scope;
-- INSERT INTO _reset_scope (graduation_year) VALUES (2024), (2025);  -- <-- edit for a scoped reset
-- ----------------------------------------------------------------------------

-- 3.0  Echo the scope. This MUST read identically to query 1.0. If it does not,
--      you verified a different scope than you deleted — fix the preamble and
--      re-run this whole section.
SELECT
    CASE WHEN NOT EXISTS (SELECT 1 FROM _reset_scope)
         THEN 'ALL YEARS'
         ELSE 'SCOPED: ' || (SELECT string_agg(graduation_year::text, ', ' ORDER BY graduation_year)
                             FROM _reset_scope)
    END AS active_scope;


-- 3.1  AFTER-COUNTS. Every `in_scope_rows` must be 0.
--      `survey_reset_log` is expected to be UNCHANGED unless you uncommented
--      step 5. `survey_send_config` must still have exactly 1 row.
SELECT 'survey_send_log' AS table_name,
       (SELECT count(*) FROM survey_send_log) AS total_rows,
       (SELECT count(*) FROM survey_send_log s
         WHERE NOT EXISTS (SELECT 1 FROM _reset_scope)
            OR s.graduation_year IN (SELECT graduation_year FROM _reset_scope)) AS in_scope_rows,
       0 AS expected_in_scope
UNION ALL
SELECT 'survey_responses',
       (SELECT count(*) FROM survey_responses),
       (SELECT count(*) FROM survey_responses r
         WHERE NOT EXISTS (SELECT 1 FROM _reset_scope)
            OR r.graduation_year IN (SELECT graduation_year FROM _reset_scope)),
       0
UNION ALL
SELECT 'survey_schedule',
       (SELECT count(*) FROM survey_schedule),
       (SELECT count(*) FROM survey_schedule sc
         WHERE NOT EXISTS (SELECT 1 FROM _reset_scope)
            OR sc.graduation_year IN (SELECT graduation_year FROM _reset_scope)),
       0
UNION ALL
SELECT 'survey_campaign_retirement',
       (SELECT count(*) FROM survey_campaign_retirement),
       (SELECT count(*) FROM survey_campaign_retirement cr
         WHERE NOT EXISTS (SELECT 1 FROM _reset_scope)
            OR cr.graduation_year IN (SELECT graduation_year FROM _reset_scope)),
       0
ORDER BY 1;


-- 3.2  survey_send_config must still exist, with exactly one row at id = 1.
--      If config_rows is 0, something deleted it — restore it before sending.
--      Q5 (answered): Resend Free plan, so expect enabled=true, daily_limit=100,
--      monthly_limit=3000 — `caps_match_free_plan` must read true.
SELECT count(*) AS config_rows,
       bool_and(id = 1) AS singleton_ok,
       max(enabled::int)::bool AS enabled,
       max(daily_limit) AS daily_limit,
       max(monthly_limit) AS monthly_limit,
       100  AS expected_daily_limit,
       3000 AS expected_monthly_limit,
       bool_and(enabled AND daily_limit = 100 AND monthly_limit = 3000) AS caps_match_free_plan
FROM survey_send_config;


-- 3.3  survey_reset_log — confirm it is in the state you chose (Q4).
SELECT count(*) AS reset_log_rows,
       count(DISTINCT alumni_id) AS alumni_with_resets,
       max(reset_at) AS last_reset_at
FROM survey_reset_log;


-- 3.4  *** THE CRITICAL CHECK. THIS MUST RETURN ZERO ROWS. ***
--      Any year that still has send-log rows at the cycle it would resolve to
--      is a cohort that a new campaign will SILENTLY SKIP. This is the #357
--      failure and it is the entire reason #445 exists. If this returns rows,
--      the reset is INCOMPLETE — do not launch the real campaign.
SELECT y.graduation_year,
       COALESCE(sc.cycle_seq, ret.max_retired_cycle + 1, 1) AS resolved_current_cycle,
       count(s.survey_send_log_id) AS sends_blocking_at_that_cycle,
       'INCOMPLETE RESET — a campaign for this year would send to nobody' AS verdict
FROM (SELECT DISTINCT graduation_year FROM survey_send_log) y
LEFT JOIN survey_schedule sc ON sc.graduation_year = y.graduation_year
LEFT JOIN (
    SELECT graduation_year, max(cycle_seq) AS max_retired_cycle
    FROM survey_campaign_retirement GROUP BY graduation_year
) ret ON ret.graduation_year = y.graduation_year
JOIN survey_send_log s
      ON s.graduation_year = y.graduation_year
     AND s.cycle_seq = COALESCE(sc.cycle_seq, ret.max_retired_cycle + 1, 1)
GROUP BY y.graduation_year, sc.cycle_seq, ret.max_retired_cycle
ORDER BY y.graduation_year;


-- 3.5  Whatever survey state remains anywhere in the database, by year.
--      On an ALL-YEARS reset this should return NO ROWS AT ALL.
--      On a scoped reset it should show only the years you deliberately kept.
SELECT y.graduation_year,
       (SELECT count(*) FROM survey_send_log            x WHERE x.graduation_year = y.graduation_year) AS sends,
       (SELECT count(*) FROM survey_responses           x WHERE x.graduation_year = y.graduation_year) AS responses,
       (SELECT count(*) FROM survey_schedule            x WHERE x.graduation_year = y.graduation_year) AS schedules,
       (SELECT count(*) FROM survey_campaign_retirement x WHERE x.graduation_year = y.graduation_year) AS retirements
FROM (
    SELECT graduation_year FROM survey_schedule
    UNION
    SELECT graduation_year FROM survey_send_log
    UNION
    SELECT graduation_year FROM survey_campaign_retirement
    UNION
    SELECT graduation_year FROM survey_responses WHERE graduation_year IS NOT NULL
) y
ORDER BY y.graduation_year;


-- 3.6  Responses with a NULL graduation_year — these are invisible to every
--      per-year query above, so they get their own check. On an ALL-YEARS reset
--      this must be 0.
SELECT count(*) AS null_year_responses_remaining
FROM survey_responses
WHERE graduation_year IS NULL;


-- ############################################################################
-- AFTER THE SQL — CHECK THESE IN THE UI BEFORE THE FIRST REAL SEND
-- ############################################################################
--   * Survey console "Progress" tab: the year you are about to survey should
--     show 0 recipients / 0 replied / 0 awaiting review / 0 applied /
--     0 rejected / 0 confirmed. Those numbers are derived live from
--     survey_send_log + survey_responses — there is no cached KPI table — so if
--     3.1 reads zero and the tab does not, you are looking at a stale page or
--     at DEV. Hard-refresh, then confirm the environment.
--   * Dashboard tiles that count survey activity: same source, same expectation.
--   * Create the real campaign through the CONSOLE, not by hand-inserting a
--     survey_schedule row. The console resolves the cycle through
--     `current_cycle_seq`; a hand-written row does not.
--   * Do a dry-run send first and read the recipient count. A campaign that
--     reports zero recipients is the silent-skip failure, not an empty cohort.
-- ############################################################################
