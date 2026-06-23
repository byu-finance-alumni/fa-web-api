-- Backfill legacy per-row note columns into the unified notes table (#39).
--
-- WHAT IT DOES
-- Two free-text columns predate the notes table introduced in
-- 2026-06-22_unified_notes.sql:
--   interactions.interaction_notes  (text, nullable)
--   events.event_notes              (text, nullable)
-- This migration copies every non-empty value from those columns into notes so
-- that historical notes surface in the new unified notes feed.
--
-- ADDITIVE - the legacy columns are NOT dropped. They remain as the
-- "logged-with" snapshot for interaction/event create flows so a rollback can
-- simply stop reading from notes. Fully migrating the create flow (writing to
-- notes instead of, or in addition to, the legacy columns) is a separate
-- follow-up task.
--
-- IDEMPOTENT - each INSERT is guarded by a NOT EXISTS correlated subquery on
-- (interaction_id / event_id, body). Re-running this migration against a
-- database where it already ran produces zero new rows. Accepted trade-off: if a
-- user already added a note on the same entity whose body is byte-identical to
-- the legacy text, the guard treats it as already-present and skips the backfill
-- row (no duplicate; the content is already in notes; the legacy column is kept).
--
-- BODY LENGTH - left(..., 10000) enforces the ck_notes_body_length constraint
-- (char_length(body) <= 10000). Any legacy note longer than 10 000 characters
-- is silently truncated; this matches the API-layer 10k limit that all new
-- notes already respect.
--
-- SINGLE-TARGET CHECK - ck_notes_single_target requires
-- num_nonnulls(alumni_id, interaction_id, event_id) = 1. Interaction rows set
-- only interaction_id; event rows set only event_id; alumni_id stays NULL in
-- both cases.
BEGIN;

-- -----------------------------------------------------------------
-- 1. Backfill interaction notes
-- -----------------------------------------------------------------
-- One notes row per interaction that has a non-empty interaction_notes value.
-- created_by_user_id maps to interactions.user_id (the recording officer).
-- created_at / updated_at carry the original interaction timestamp so the note
-- appears at the correct point in the timeline.
-- updated_by_user_id is left NULL (no editor yet; this is a synthetic backfill).
INSERT INTO notes (
    interaction_id,
    body,
    created_by_user_id,
    created_at,
    updated_at
)
SELECT
    i.interaction_id,
    left(btrim(i.interaction_notes), 10000),
    i.user_id,
    i.created_at,
    i.created_at
FROM interactions i
WHERE i.interaction_notes IS NOT NULL
  AND btrim(i.interaction_notes) <> ''
  AND NOT EXISTS (
      SELECT 1
      FROM notes n
      WHERE n.interaction_id = i.interaction_id
        AND n.body = left(btrim(i.interaction_notes), 10000)
  );

-- -----------------------------------------------------------------
-- 2. Backfill event notes
-- -----------------------------------------------------------------
-- One notes row per event that has a non-empty event_notes value.
-- created_by_user_id maps to events.logged_by_user_id.
-- created_at / updated_at carry the original event timestamp.
-- updated_by_user_id is left NULL for the same reason as above.
INSERT INTO notes (
    event_id,
    body,
    created_by_user_id,
    created_at,
    updated_at
)
SELECT
    e.event_id,
    left(btrim(e.event_notes), 10000),
    e.logged_by_user_id,
    e.created_at,
    e.created_at
FROM events e
WHERE e.event_notes IS NOT NULL
  AND btrim(e.event_notes) <> ''
  AND NOT EXISTS (
      SELECT 1
      FROM notes n
      WHERE n.event_id = e.event_id
        AND n.body = left(btrim(e.event_notes), 10000)
  );

COMMIT;
