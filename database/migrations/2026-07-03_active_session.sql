-- Single active session per account (#147).
-- Track the Supabase session_id of the most recent sign-in per user. A newer
-- login overwrites it; any earlier device whose session_id no longer matches is
-- rejected on the backend (forced logout). NULL until the first sign-in after
-- this ships, so existing sessions are not disturbed until their next login.

ALTER TABLE users ADD COLUMN IF NOT EXISTS active_session_id text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS active_session_at timestamptz;
