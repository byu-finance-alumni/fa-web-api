-- Pay It Forward Fund donations (#161). A per-alumnus donation ledger: each row
-- is a single gift of a dollar AMOUNT tied to a MONTH + YEAR. Totals are rolled
-- up per-year and lifetime in the API.
--
-- Access is FIELD-LEVEL and enforced in the API, not here: donor identity is
-- visible to any view-access role, but the dollar amounts are gated to
-- full_access and up (the API nulls amount fields for everyone else, so a
-- non-privileged client never receives a value). Writes (add/edit/delete, bulk
-- import) are super_admin-only. logged_by_user_id is ON DELETE SET NULL so a
-- donation survives a later user deletion (the actor identity is preserved in
-- the audit trail via the actor-snapshot trigger).
--
-- month is nullable (a gift may be recorded with only a year); when present it
-- is constrained to 1-12. year is required. amount is non-negative.
BEGIN;

CREATE TABLE IF NOT EXISTS donations (
    donation_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id         bigint NOT NULL,
    amount            numeric(12, 2) NOT NULL,
    donation_month    smallint,
    donation_year     smallint NOT NULL,
    notes             text,
    logged_by_user_id bigint,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_donations_amount_nonneg CHECK (amount >= 0),
    CONSTRAINT ck_donations_month_range
        CHECK (donation_month IS NULL OR donation_month BETWEEN 1 AND 12),
    CONSTRAINT ck_donations_year_range
        CHECK (donation_year BETWEEN 1900 AND 2200),
    CONSTRAINT ck_donations_notes_length CHECK (char_length(notes) <= 10000),
    CONSTRAINT fk_donations_alumni_id
        FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT fk_donations_user_id
        FOREIGN KEY (logged_by_user_id) REFERENCES users (user_id) ON DELETE SET NULL
);

-- Donor list groups by alumnus; per-year roll-ups group by (alumnus, year).
CREATE INDEX IF NOT EXISTS idx_donations_alumni_id ON donations (alumni_id);
CREATE INDEX IF NOT EXISTS idx_donations_year      ON donations (donation_year);

-- Deny-all RLS like every other public table (Supabase auto-exposes the public
-- schema via its Data API; the backend bypasses RLS with a privileged role).
-- Mirrors database/rls_lockdown.sql. Idempotent.
ALTER TABLE donations ENABLE ROW LEVEL SECURITY;

COMMIT;
