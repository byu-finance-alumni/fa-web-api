-- Preserve audit actor identity across user deletion (FERPA, #93).
--
-- audit_logs.user_id is ON DELETE SET NULL, so deleting a staff user anonymizes
-- every past action they performed ("who changed this alumni record?" becomes
-- unanswerable). Snapshot the actor's email + name onto each audit row at INSERT
-- time via a trigger, so the identity survives the later SET NULL. Reads then
-- COALESCE the live join with this snapshot.
--
-- The trigger covers every audit write (current and future) with no application
-- changes, and only fires when actor_email is unset, so an explicit value (e.g.
-- a backfill or a deliberate system event) is never overwritten.
BEGIN;

ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS actor_email varchar(255);
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS actor_name  varchar(255);

-- Backfill existing rows from the users still present.
UPDATE audit_logs a
SET actor_email = u.email,
    actor_name  = NULLIF(TRIM(CONCAT_WS(' ', u.first_name, u.last_name)), '')
FROM users u
WHERE a.user_id = u.user_id
  AND a.actor_email IS NULL;

CREATE OR REPLACE FUNCTION audit_logs_snapshot_actor()
RETURNS trigger AS $$
BEGIN
    IF NEW.actor_email IS NULL AND NEW.user_id IS NOT NULL THEN
        SELECT u.email,
               NULLIF(TRIM(CONCAT_WS(' ', u.first_name, u.last_name)), '')
          INTO NEW.actor_email, NEW.actor_name
          FROM users u
         WHERE u.user_id = NEW.user_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_audit_logs_snapshot_actor ON audit_logs;
CREATE TRIGGER trg_audit_logs_snapshot_actor
    BEFORE INSERT ON audit_logs
    FOR EACH ROW
    EXECUTE FUNCTION audit_logs_snapshot_actor();

COMMIT;
