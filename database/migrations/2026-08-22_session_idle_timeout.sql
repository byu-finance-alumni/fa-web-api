-- =============================================================================
-- Migration: users.session_last_seen_at (24h idle session expiry)
-- Date: 2026-08-22  (#684)
-- -----------------------------------------------------------------------------
-- WHY THIS COLUMN EXISTS. The app's idle timeout was browser-memory only: a
-- timer inside `SessionTimeout` that dies with the tab. Close a laptop and
-- reopen it a week later and the tab is restored, the timer restarts at zero,
-- and the Supabase session -- good for up to 400 days -- is still a live
-- credential. #684 was filed off exactly that: "still signed in after a full
-- laptop restart".
--
-- WHY WE CANNOT USE auth.sessions FOR THIS. It looks like the obvious source:
-- `app/services/auth_sessions.py` already derives a `last_active_at` from
-- GREATEST(created_at, updated_at, refreshed_at). But `refreshed_at` moves on
-- every TOKEN REFRESH, and restoring a tab after 24h idle refreshes immediately
-- -- the access token has long expired, so the Supabase client mints a new one
-- before this API is ever asked a question. By the time we could read it, the
-- session looks seconds old. GoTrue's timestamps measure the CLIENT's liveness,
-- not the user's; only a stamp we write on an authenticated request measures
-- the user's.
--
-- WHY ON users AND NOT A SESSIONS TABLE. Single-active-session (#147) means an
-- account has at most one claimable session at a time, already recorded here as
-- `active_session_id`. Idle is a property of that session, so it belongs in the
-- same row -- which the auth resolver has already SELECTed on every request.
-- A separate table would add a second read per request to store one timestamp
-- about a row we are holding anyway.
--
-- NULL MEANS "NOT YET STAMPED", AND MUST NOT MEAN "IDLE FOREVER". Every session
-- alive at deploy time predates the column, so a NULL cannot be treated as
-- infinitely idle without signing out the whole department on the first request
-- after this ships. The resolver stamps NULL to now() and lets the session live;
-- the 24h clock starts from that first post-deploy request.
-- =============================================================================

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS session_last_seen_at timestamptz;

COMMENT ON COLUMN users.session_last_seen_at IS
    'Last authenticated request made by the account''s active session (#684). '
    'Written by the auth resolver, throttled; NULL means not yet stamped and is '
    'treated as fresh, never as idle. Compared against a 24h limit to expire an '
    'untouched session -- see app/services/session_idle.py.';
