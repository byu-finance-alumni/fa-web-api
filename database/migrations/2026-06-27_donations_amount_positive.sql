-- Tighten the donations amount CHECK from >= 0 to > 0 (#161, QA follow-up). A $0
-- gift carries no financial meaning and would skew lifetime/per-year roll-ups;
-- the API already rejects it, this keeps the DB backstop in lockstep. Safe to
-- run on the freshly-created (empty) donations table.
BEGIN;

ALTER TABLE donations DROP CONSTRAINT IF EXISTS ck_donations_amount_nonneg;
ALTER TABLE donations
    ADD CONSTRAINT ck_donations_amount_positive CHECK (amount > 0);

COMMIT;
