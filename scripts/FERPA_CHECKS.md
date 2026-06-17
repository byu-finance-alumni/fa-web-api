# FERPA compliance check (fa-web-api)

`scripts/ferpa_check.py` is a **deterministic** static analysis of this repo —
no LLM, no network, no API key. It enforces a handful of FERPA / data-governance
controls so a missing one blocks the merge. CI runs it on push/PR to `dev` and
`prod` via `.github/workflows/ferpa-audit.yml`, and it is safe to require as a
branch-protection status check.

## Run it locally

```bash
python scripts/ferpa_check.py
```

Exit code `1` means a **hard** control is missing; `0` means OK (warnings may
still print). The script ends with:

```
FERPA check: N hard failures, M warnings
```

## Hard checks (exit 1 if violated)

1. **RLS coverage (most important).** Parses every `CREATE TABLE <name>` in
   `database/schema.sql` and every `ENABLE ROW LEVEL SECURITY` target in
   `database/rls_lockdown.sql`. **Fails listing any schema table that is not
   locked down.** Supabase auto-exposes every `public` table through its REST
   Data API using the publishable key shipped in the frontend bundle; RLS
   (enabled with no policies = deny-all for `anon`/`authenticated`) is the only
   thing keeping alumni PII off that public key.

2. **SQL echo production guard.** Inspects `app/core/database.py`. If
   `settings.sql_echo` drives the SQLAlchemy engine `echo`, it must be gated by
   an `environment != "production"` guard nearby. SQLAlchemy echo logs every
   statement with its **bound parameters**, which can carry alumni PII into
   logs; this must be forced off in production.

3. **No tracked secrets / committed `.env`.** Fails if a real `.env` exists in
   the repo root **and is not covered by `.gitignore`** (i.e. committable), or
   if obvious hardcoded secrets appear under `app/` — a `SUPABASE_SERVICE_ROLE_KEY`
   / `*_SECRET` assignment with a value, or a long base64url JWT literal
   (`eyJ…`). `os.environ` / `getenv` reads are ignored (those are not hardcoded
   values). Intentionally conservative to avoid false positives.

## Warnings (printed, never fail the build)

4. **Record-of-disclosure logging.** For the alumni-data GET route files
   (`app/api/routes/alumni.py`, `dashboard.py`, `geography.py`), warns if a file
   has `@router.get` routes but never references `AuditLog` / an `audit` call —
   a possible missing record of disclosure.

## When the RLS check fails

Add the new table to `database/rls_lockdown.sql`:

```sql
ALTER TABLE public.<new_table> ENABLE ROW LEVEL SECURITY;
```

(or run the dynamic `DO` block at the bottom of that file), then re-run the
check.
