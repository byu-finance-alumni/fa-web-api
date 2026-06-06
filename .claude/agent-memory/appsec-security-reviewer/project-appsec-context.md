---
name: project-appsec-context
description: Core security architecture facts for the BYU Finance Alumni API — auth, authz, query patterns, logging, known gaps
metadata:
  type: project
---

## Auth / AuthZ layer (app/api/dependencies/auth.py)
- Supabase JWT verified via JWKS; resolved to DB user row
- Three guards: RequireSuperAdmin > RequireFullAccess > RequireViewAccess (each is a type alias in dependencies/auth.py)
- DeactivatedAccountError (403) is raised BEFORE role checks and is logged as `account_deactivated` security event
- All guards are enforced server-side; no client-side bypass possible

## SQL safety patterns
- All queries use SQLAlchemy ORM parameterized binding — no raw string concatenation in queries
- ILIKE patterns built with Python f-strings (`f"%{q}%"`) but passed as BIND PARAMETERS via SQLAlchemy — this is safe against SQL injection but NOT against LIKE wildcard abuse (% and _ in user input are not escaped)
- Employer and industry use `.ilike(value)` (exact ilike, no wrapping wildcards) — % / _ still dangerous there too

## Known LIKE-wildcard injection (not SQL injection, not critical, but Medium)
- GET /alumni: q param, employer, industry — all use ilike with user-controlled values
- GET /dashboard/activity: q and type params — same
- GET /audit: user param — same
- GET /events: q param — same
- An attacker can inject `%` or `_` to over-match (enumerate all records efficiently) or cause expensive scans with repeated `%` patterns

## Stored XSS blast radius (confirmed: ' OR 1=1;-- stored as first name via Add Alumni)
- Fields containing adversarial text: first_name, last_name, byu_id (at minimum)
- Rendering locations: TopbarSearch dropdown (displayName), AlumniFilters option list (deep-link passthrough), audit page old_value/new_value, activity feed alumni_name, follow-ups alumni_name, contacted-this-month alumni_name
- React JSX rendering uses textContent (not innerHTML) — XSS NOT exploitable in React components that render text as children
- Audit page renders old_value/new_value directly as JSX text — safe from XSS but exposes raw adversarial strings in UI

## Unauthenticated /docs exposure
- FastAPI docs_url and redoc_url default to /docs and /redoc — no auth guard added in main.py
- Root endpoint (/) returns `"docs": "/docs"` pointing to Swagger UI
- Swagger UI exposes full schema of all endpoints, models, filter params — recon aid for attackers (Medium)

## Audit log — no pagination offset (Medium)
- GET /audit accepts limit up to 200 but has NO offset parameter — cannot page through large logs
- Combined with no row count cap enforcement, a large audit log is always truncated to latest 200

## contacted-this-month and follow-ups hardcoded .limit(200)
- No query parameter for pagination on these two KPI endpoints
- Hard limit 200 — acceptable for now but grows unbounded with data

## Event creation (POST /events) — no field length limits except event_name
- event_type, event_location, event_notes have no max-length validators — only blank-to-None strip
- Could store very large strings in these fields

## Security logging (app/core/security_log.py)
- JSON-encoded payload — safe against log injection because newlines/special chars in detail become JSON-escaped
- path is logged WITHOUT query string (correct — avoids PII in logs)
- detail field in unhandled_error handler contains only exception type name, not message
- User-Agent is logged raw (potential log injection vector if log consumer doesn't parse JSON)

## Frontend (TopbarSearch.tsx)
- clientGet fires from browser with Supabase access token — auth required
- Debounce: 250ms, MIN_CHARS=2 — fires at 2+ chars; 100 req/60s/IP WAF limit
- No stored alumni data exposed in browser beyond what the API returns
- No dangerouslySetInnerHTML found in any changed file

## /events/{id}/attendees
- Verifies event exists (404 if not) before returning attendees — no IDOR
- Returns alumni_id, name, graduation_year, attendance_status — minimal field set (correct)

## PATCH /admin/users/{id}
- RequireSuperAdmin guard confirmed
- Self-deactivation rejected (checks actor.user_id == user_id)
- extra="forbid" on UserActiveUpdate schema — mass assignment protected
- Audited with AuditLog entry

## POST /events
- RequireFullAccess guard
- EventCreate uses extra="forbid"
- Only event_name has length limit (255); event_type/location/notes have no length cap

**Why important:** Alumni PII, institutional data — any leak or privilege escalation is high impact.
**How to apply:** Flag any new query param that flows into ilike without LIKE-special-char escaping. Flag any new endpoint that adds field data to audit old/new_value without considering what lower-privileged roles can see in /audit.
