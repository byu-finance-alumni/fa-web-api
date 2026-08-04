-- =============================================================================
-- Functional indexes backing conference-attendee matching (#612)
--
-- The attendee matcher resolves a whole uploaded list in a handful of BATCHED
-- queries, each an exact-equality test against a NORMALIZED name/email:
--
--     lower(trim(alumni.last_name))  IN (:surname_keys)
--     lower(trim(alumni.birth_name)) IN (:surname_keys)
--     lower(trim(alumni.first_name)) IN (:given_keys)
--     lower(trim(alumni_contact_info.personal_email)) IN (:emails)
--
-- The existing indexes are on the RAW columns and cannot serve those
-- expressions, so add expression indexes that do. Written to match the SQL the
-- service emits VERBATIM (no cast, trim then lower) -- a mismatched expression
-- silently degrades to a sequential scan.
--
-- The matching legs deliberately never use a leading-wildcard ILIKE; accent,
-- punctuation and hyphen variants are expanded in Python into extra equality
-- keys instead, precisely so these indexes stay usable.
--
-- Index-only additions: no data change, no new tables, so no RLS step needed.
-- Idempotent via IF NOT EXISTS, so safe to re-run and safe to auto-apply.
-- =============================================================================

BEGIN;

-- Surname leg: the file's surname is matched against BOTH the current surname
-- and the maiden/birth name (#216), so an alumna who married after graduating
-- is found under either.
CREATE INDEX IF NOT EXISTS idx_alumni_last_name_norm
    ON alumni (lower(trim(last_name)));

CREATE INDEX IF NOT EXISTS idx_alumni_birth_name_norm
    ON alumni (lower(trim(birth_name)));

-- Given-name leg (the "married surname we have never seen" safety net, which
-- additionally requires an employer hit). preferred_first_name matters because
-- a conference badge carries what the attendee goes by, not their legal name.
CREATE INDEX IF NOT EXISTS idx_alumni_first_name_norm
    ON alumni (lower(trim(first_name)));

CREATE INDEX IF NOT EXISTS idx_alumni_preferred_first_name_norm
    ON alumni (lower(trim(preferred_first_name)));

-- Email leg: the high-confidence key when the conference list carries email.
CREATE INDEX IF NOT EXISTS idx_alumni_contact_info_personal_email_norm
    ON alumni_contact_info (lower(trim(personal_email)));

CREATE INDEX IF NOT EXISTS idx_alumni_contact_info_work_email_norm
    ON alumni_contact_info (lower(trim(work_email)));

COMMIT;
