-- Add IP address + approximate location to the login history (login_events).
-- Captured by the Next.js login server action from the incoming request: the
-- client IP (x-forwarded-for) and Vercel's IP-geolocation headers
-- (x-vercel-ip-city / -country-region / -country), forwarded to POST /auth/login.
--
-- All nullable: local dev and non-Vercel paths have no geo headers, and a login
-- recorded before this migration has none. IP-geo is city-level / approximate.
BEGIN;

ALTER TABLE login_events ADD COLUMN IF NOT EXISTS ip_address varchar(64);
ALTER TABLE login_events ADD COLUMN IF NOT EXISTS city       varchar(128);
ALTER TABLE login_events ADD COLUMN IF NOT EXISTS region     varchar(128);
ALTER TABLE login_events ADD COLUMN IF NOT EXISTS country    varchar(64);

COMMIT;
