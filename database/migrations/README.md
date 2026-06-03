# Database migrations

Plain SQL migrations applied to the Supabase PostgreSQL database, in order, each
exactly once. There is **no Alembic** — migrations are hand-written `.sql` files
runnable directly against the database. `../schema.sql` is the full source-of-truth
snapshot of the resulting schema; keep it in sync when you add a migration.

## How it runs

`../migrate.sh` applies every file in this directory that has not yet been applied,
in **lexical filename order**, recording each in a `schema_migrations` table so it
never runs twice. It's invoked automatically by CI after a merge (see below), and
can be run by hand:

```bash
# Use the DIRECT (non-pooled, port 5432) Supabase connection — NOT the
# transaction pooler (6543), which can't run multi-statement DDL transactions.
DATABASE_URL="postgresql://postgres:PASSWORD@db.PROJECT_REF.supabase.co:5432/postgres" \
  bash database/migrate.sh
```

## Authoring a migration

1. Create `YYYY-MM-DD_short_description.sql` (the date prefix makes lexical order
   match chronological order).
2. Wrap the body in a single transaction:
   ```sql
   BEGIN;
   -- ... your DDL ...
   COMMIT;
   ```
   so a failure rolls back atomically.
3. Prefer idempotent DDL (`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`,
   `CREATE INDEX IF NOT EXISTS`) where practical.
4. Every **new table** must get deny-all RLS to match `../rls_lockdown.sql`:
   ```sql
   ALTER TABLE my_new_table ENABLE ROW LEVEL SECURITY;  -- no policies = deny-all
   ```
5. Update `../schema.sql` to reflect the new end state.

## CI: manual-gated on prod promotion

There is a **single Supabase database**, shared by the dev and prod deployments,
so any schema change is global the moment it lands. To avoid a dev merge silently
altering the shared (production) database, migrations are applied only on a **push
to `prod`**, and only **after a human approves**:

| Branch pushed | Job       | Target DB        | Approval                       |
| ------------- | --------- | ---------------- | ------------------------------ |
| `dev`         | —         | —                | (migrations do not run)        |
| `prod`        | `migrate` | the shared DB    | **manual** (required reviewer) |

`.github/workflows/ci.yml` runs `migrate` after the gating checks pass on the
post-merge push to `prod`. Migrations do **not** run on pull requests.

> If you later want dev migrations to apply automatically and safely, create a
> separate (free-tier) Supabase project for dev and split this back into
> per-environment jobs.

### One-time GitHub setup

In the repo on GitHub → **Settings → Environments**, create the **`production`**
environment:

- Add secret `MIGRATIONS_DATABASE_URL` = your Supabase project's **direct**
  (port 5432) connection string.
- Under **Deployment protection rules**, enable **Required reviewers** and add
  yourself / the team. This is the manual approval gate.

Find the connection string in Supabase → **Project Settings → Database →
Connection string → Direct connection** (URI). Use the direct connection, not the
pooler.
