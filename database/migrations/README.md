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

## CI: automatic on both branches

`dev` and `prod` have **separate Supabase databases** (dev = the original
project, which keeps the mock/seed data; prod = a dedicated project, stood up
2026-07-09). Migrations are applied to **each** by its own CI job, automatically:

| Branch pushed | Job                       | Target DB       | Approval |
| ------------- | ------------------------- | --------------- | -------- |
| `dev`         | `Migrate database (dev)`  | the **dev** DB  | none     |
| `prod`        | `Migrate database`        | the **prod** DB | none     |

`.github/workflows/ci.yml` runs them after the gating checks pass on the
post-merge push. Migrations do **not** run on pull requests.

### ⚠️ There is no approval gate, and one must never be added

There used to be one (the `production` Environment's required reviewers). It did
not make prod safer — **it caused an outage**. The approval held the *migration*
while **Vercel deployed the code independently**, so new code ran against the old
schema. It was removed deliberately.

### ⚠️ Sequence the DEPLOY, not the migration

The migrate job trails the Vercel deploy by **minutes**. That gap only hurts in
one direction:

| Combination            | Result                                                  |
| ---------------------- | ------------------------------------------------------- |
| old code + new schema  | **safe** — a widened constraint admits everything the narrow one did |
| new code + old schema  | **broken** — every write needing the change is rejected for the length of the gap |

So for any schema-dependent change: **push a migration-only commit to `prod`
first, let it apply, then promote the code.** Cut that commit from `prod` rather
than `dev` so it carries the `.sql` file and nothing else.

Prefer **widening** changes for this reason (add a nullable column, extend a
CHECK / enum) — they are safe in the gap. A narrowing or destructive change is
not, and needs the code retired first.

### One-time GitHub setup

In the repo on GitHub → **Settings → Environments**, create the **`production`**
environment:

- Add secret `MIGRATIONS_DATABASE_URL` = your Supabase project's **direct**
  (port 5432) connection string.
- ⚠️ **Do NOT enable "Required reviewers" / any deployment protection rule on
  this environment.** There was once an approval gate here and it caused a
  production outage: the approval held the **migration** while Vercel deployed
  the **code** independently, so new code ran against the old schema. The gate
  was removed deliberately. The environment exists only to hold the secret.

Find the connection string in Supabase → **Project Settings → Database →
Connection string → Direct connection** (URI). Use the direct connection, not the
pooler.
