#!/usr/bin/env bash
# =============================================================================
# Apply pending SQL migrations to a PostgreSQL/Supabase database.
# -----------------------------------------------------------------------------
# Runs every *.sql file in database/migrations/ that has not been applied yet,
# in lexical filename order, recording each in a `schema_migrations` table so it
# runs exactly once. Re-running is safe: already-applied files are skipped.
#
# Usage (local or CI):
#   DATABASE_URL="postgresql://USER:PASS@HOST:5432/postgres" bash database/migrate.sh
#
# IMPORTANT: use the DIRECT (non-pooled, port 5432) Supabase connection string
# — Supabase's "Session"/direct connection or POSTGRES_URL_NON_POOLING. Do NOT
# use the transaction pooler (port 6543): it can't run the multi-statement DDL
# transactions these migrations rely on.
#
# Migration file convention:
#   - Name files `YYYY-MM-DD_short_description.sql` so lexical order == apply order.
#   - Wrap statements in a single BEGIN; ... COMMIT; so a failure rolls back atomically.
#   - Prefer idempotent DDL (IF NOT EXISTS / ADD COLUMN IF NOT EXISTS) where practical.
# =============================================================================
set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL must be set (direct, non-pooled Supabase connection)}"

MIGRATIONS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/migrations" && pwd)"

# --no-psqlrc: ignore any local ~/.psqlrc. ON_ERROR_STOP: fail the run on the
# first SQL error instead of plowing ahead. -q: quiet (we print our own log).
PSQL=(psql "$DATABASE_URL" --no-psqlrc -q -v ON_ERROR_STOP=1)

# 1. Ensure the bookkeeping table exists.
"${PSQL[@]}" -c "CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   text PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);"

# 2. Apply each not-yet-recorded file, in filename order.
shopt -s nullglob
applied=0
for path in "$MIGRATIONS_DIR"/*.sql; do
  file="$(basename "$path")"
  already="$("${PSQL[@]}" -tAc "SELECT 1 FROM schema_migrations WHERE filename = '${file}';")"
  if [ "${already}" = "1" ]; then
    echo "- skip   ${file} (already applied)"
    continue
  fi
  echo "> apply  ${file}"
  "${PSQL[@]}" -f "${path}"
  # Record only after the migration itself succeeded (ON_ERROR_STOP aborts the
  # whole script before we reach here if it failed). Files are idempotent, so a
  # crash between these two statements is recoverable by simply re-running.
  "${PSQL[@]}" -c "INSERT INTO schema_migrations (filename) VALUES ('${file}') ON CONFLICT DO NOTHING;"
  applied=$((applied + 1))
done

echo "Done. ${applied} migration(s) applied."
