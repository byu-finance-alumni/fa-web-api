-- Unified notes (#39). One table backs free-text notes at three levels — an
-- alumni profile, a single interaction, or an event. The attach target is three
-- nullable FKs with a CHECK that EXACTLY ONE is set, so the "unified" surface
-- keeps real referential integrity and ON DELETE CASCADE per target (a note
-- never outlives its parent) rather than a loose (entity_type, entity_id) pair.
--
-- Write access (create/edit/delete) is enforced in the API at full_access and
-- up; read is any view-access role. created_by/updated_by are ON DELETE SET
-- NULL so a note survives a later user deletion (its actor identity is preserved
-- independently in the audit trail, per the audit actor-snapshot approach).
--
-- NOTE: this is ADDITIVE. The existing interaction_notes / event_notes columns
-- are left in place; migrating them into this table is a separate follow-up.
BEGIN;

CREATE TABLE IF NOT EXISTS notes (
    note_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id          bigint,
    interaction_id     bigint,
    event_id           bigint,
    body               text NOT NULL,
    created_by_user_id bigint,
    updated_by_user_id bigint,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_notes_single_target
        CHECK (num_nonnulls(alumni_id, interaction_id, event_id) = 1),
    CONSTRAINT fk_notes_alumni_id
        FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT fk_notes_interaction_id
        FOREIGN KEY (interaction_id) REFERENCES interactions (interaction_id) ON DELETE CASCADE,
    CONSTRAINT fk_notes_event_id
        FOREIGN KEY (event_id) REFERENCES events (event_id) ON DELETE CASCADE,
    CONSTRAINT fk_notes_created_by
        FOREIGN KEY (created_by_user_id) REFERENCES users (user_id) ON DELETE SET NULL,
    CONSTRAINT fk_notes_updated_by
        FOREIGN KEY (updated_by_user_id) REFERENCES users (user_id) ON DELETE SET NULL
);

-- The notes card lists by target newest-first; index each FK for that lookup.
CREATE INDEX IF NOT EXISTS idx_notes_alumni_id      ON notes (alumni_id);
CREATE INDEX IF NOT EXISTS idx_notes_interaction_id ON notes (interaction_id);
CREATE INDEX IF NOT EXISTS idx_notes_event_id       ON notes (event_id);

-- Deny-all RLS like every other public table (Supabase auto-exposes the public
-- schema via its Data API; the backend bypasses RLS with a privileged role).
-- Mirrors database/rls_lockdown.sql. Idempotent.
ALTER TABLE notes ENABLE ROW LEVEL SECURITY;

COMMIT;
