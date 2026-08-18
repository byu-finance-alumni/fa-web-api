-- =============================================================================
-- Migration: opportunity_links (alumni-submitted internship / job postings)
-- Date: 2026-08-17  (#441)
-- -----------------------------------------------------------------------------
-- Alumni tell us about internships and jobs at their company (or anywhere else).
-- Staff work the list from a "Links" tab; there is NO public or student-facing
-- surface (owner's decision on #441 — distribution stays manual).
--
-- WHY ITS OWN TABLE. Every other survey answer maps to exactly one existing
-- column, which is the rule the whole survey pipeline is built on (`_FIELDS`
-- keys are literally `table.column`, and `apply_response` setattrs them onto the
-- alum's record). An alum can name SEVERAL openings, each with its own url,
-- location, role type, deadline and description, so there is no column to map
-- to — this is one-alum-to-many-rows with structure. It therefore gets its own
-- table and its own write path; it is deliberately NOT in the survey field
-- whitelist and does not go through the response review queue.
--
-- WHY ITS OWN MODERATION STATE. The response review queue is all-or-nothing per
-- submission (`apply_response` takes only a response id), so approving an alum's
-- address correction and approving whatever link rode along with it are the same
-- click today. `status` here is per LINK, moderated through its own endpoints.
--
-- TWO SOURCES, TWO LANDING STATES:
--   * source='survey' -> status='pending'.  A public, token-gated write.
--   * source='staff'  -> status='approved'. A staff member typing it in IS the
--     review, so there is nothing left to moderate.
--
-- SECURITY — READ BEFORE WIDENING ANYTHING HERE. The survey is the only publicly
-- writable surface in this app, and `url` is rendered as a clickable `href` to a
-- signed-in staff member. `javascript:` is a script the reviewer runs by
-- clicking. The LinkedIn column is safe because of a hostname ALLOW-LIST; that
-- defence CANNOT transfer here, because the whole point is that these links
-- point at arbitrary employer sites. The controls that remain are scheme gating
-- (http/https, with the WHATWG/RFC-3986 host-parsing differential closed),
-- the length caps below, and human approval. Human approval does NOT reliably
-- catch a phishing URL — this project's own `_valid_linkedin_url` docstring says
-- so — so it is a governance control, not a technical one. See
-- `app/services/opportunity_links.py` for the validator and the full rationale.
--
-- COLUMN WIDTHS ARE A SECURITY CONTROL on this table, not just hygiene: they are
-- the cap on how much attacker-supplied text one public submit can persist. They
-- are mirrored in `app/schemas/opportunity_link.py` and re-checked in the
-- service, so a value we accept is one the column can hold.
--
-- SECURITY: this NEW table gets `ENABLE ROW LEVEL SECURITY` with NO policies —
-- the deny-all lockdown (mirrors #51). The app connects as the table owner and
-- bypasses RLS; the Supabase anon/authenticated API roles are denied.
--
-- SAFE TO RE-RUN: IF NOT EXISTS on table + indexes.
--
-- NOT RUN by this agent against any DB. Apply via the normal migration path.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS opportunity_links (
    opportunity_link_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- The alum the opportunity came from. NOT NULL even for staff entry: a link
    -- with no alumnus behind it has no provenance, and provenance is the only
    -- reason to trust it at all.
    alumni_id            bigint NOT NULL,
    -- "This is my company." When true the display name comes from the alum's own
    -- `current_employment.current_employer` at READ time (one fact, one home —
    -- if they change employer the entry follows), and `company_name` is NULL.
    -- When false the alum/staff typed a name, and it is required.
    is_own_company       boolean NOT NULL DEFAULT false,
    company_name         varchar(255),
    url                  varchar(2048) NOT NULL,
    location_city        varchar(100),
    location_state       varchar(100),
    role_type            varchar(20) NOT NULL,
    application_deadline date,
    details              text,
    status               varchar(20) NOT NULL DEFAULT 'pending',
    source               varchar(20) NOT NULL,
    submitted_at         timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    -- The staff member who typed a manual entry. NULL for a survey submission
    -- (there is no logged-in actor on that path). SET NULL so deleting a user
    -- never deletes the opportunity.
    created_by_user_id   bigint,
    -- Who moderated, and when. Set on approve/reject, and pre-set on staff entry
    -- (that path is self-reviewed).
    reviewed_by_user_id  bigint,
    reviewed_at          timestamptz,
    CONSTRAINT fk_opportunity_links_alumni FOREIGN KEY (alumni_id)
        REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT fk_opportunity_links_created_by FOREIGN KEY (created_by_user_id)
        REFERENCES users (user_id) ON DELETE SET NULL,
    CONSTRAINT fk_opportunity_links_reviewer FOREIGN KEY (reviewed_by_user_id)
        REFERENCES users (user_id) ON DELETE SET NULL,
    CONSTRAINT ck_opportunity_links_status
        CHECK (status IN ('pending', 'approved', 'rejected')),
    CONSTRAINT ck_opportunity_links_source
        CHECK (source IN ('survey', 'staff')),
    CONSTRAINT ck_opportunity_links_role_type
        CHECK (role_type IN ('internship', 'full_time', 'both')),
    -- Exactly one company identity: either it is the alum's own employer (name
    -- derived at read time) or a name was typed. Both-or-neither is a row nobody
    -- can render.
    CONSTRAINT ck_opportunity_links_company
        CHECK (
            (is_own_company AND company_name IS NULL)
            OR (NOT is_own_company AND company_name IS NOT NULL)
        ),
    -- The one unbounded column, so it carries its cap in the DB as well as in
    -- the schema. A public writer must not be able to persist a megabyte.
    CONSTRAINT ck_opportunity_links_details_length
        CHECK (details IS NULL OR char_length(details) <= 2000),
    CONSTRAINT ck_opportunity_links_url_length
        CHECK (char_length(url) BETWEEN 1 AND 2048)
);

-- The staff list's default view: a status filter, newest first.
CREATE INDEX IF NOT EXISTS idx_opportunity_links_status_submitted
    ON opportunity_links (status, submitted_at DESC);
-- The moderation queue (pending only) and the role-type filter.
CREATE INDEX IF NOT EXISTS idx_opportunity_links_role_type
    ON opportunity_links (role_type);
CREATE INDEX IF NOT EXISTS idx_opportunity_links_alumni_id
    ON opportunity_links (alumni_id);

ALTER TABLE opportunity_links ENABLE ROW LEVEL SECURITY;

COMMIT;

-- =============================================================================
-- ROLLBACK (run by hand if the table must be dropped):
--   DROP TABLE IF EXISTS opportunity_links;
-- =============================================================================
