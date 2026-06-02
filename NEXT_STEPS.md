# Next Steps — fa-web-api

Running checklist of outstanding work and the build roadmap. Update as items
are completed.

## ✅ Done so far

- PostgreSQL schema (`database/schema.sql`) — 25 tables, kept in sync with the
  Notion "Database Schema Planning" page.
- `.gitignore` (Python/FastAPI + guards against committing private alumni data
  and secrets).
- Basic FastAPI app: `GET /`, `GET /health`, `GET /health/db`.
- Supabase JWT auth: `app/core/security.py`, `get_current_user` dependency,
  protected `GET /auth/me`, structured error envelope (401/422). 9 tests passing.
- Live Supabase Postgres connection verified locally (`/health/db` green).
- **Deployed to Vercel** (Python serverless, `app.main:app` entrypoint) — app is
  live and running. Config accepts the Supabase↔Vercel integration's env var
  names (`POSTGRES_URL`, `SUPABASE_JWT_SECRET`, `NEXT_PUBLIC_*`).
- **CI + branch protection**: GitHub Actions runs `Lint (ruff)` and
  `Test (pytest)` on PRs/pushes to `prod` and `dev`; both branches are protected
  and require those checks to pass via pull request. See "Branch & deploy
  workflow" in `README.md`.
- **Python pinned to 3.12** (`.python-version`) across local dev, CI, and Vercel.

## 🔧 Immediate follow-ups (config / unblock)

- [x] **`DATABASE_URL` set in `.env`** — connected via the Supabase session
      pooler (`aws-1-us-east-1`, port 5432). `GET /health/db` returns
      `{"database":"connected"}`.
- [x] **`SUPABASE_SERVICE_ROLE_KEY` set in `.env`** — validated
      (role=service_role, correct project). Not used by code yet; reserved for
      Supabase Storage / auth-admin work.
- [ ] **Add `JWT_SECRET` to `.env`** — the project uses **HS256** signing (the
      service-role key is HS256), so verifying real user logins (`/auth/me`) will
      need the JWT secret from dashboard → Project Settings → API → JWT Secret.
- [x] ~~Python 3.14 + `asyncpg`~~ — `asyncpg` installs cleanly; project is now
      pinned to **Python 3.12** (`.python-version`) to match CI and the Vercel
      runtime.
- [ ] **Confirm `DATABASE_URL` on Vercel** — the Supabase integration only set
      `POSTGRES_PASSWORD` / `POSTGRES_DATABASE`, not a full connection URL. For
      the deployed `/health/db` to go green, add `DATABASE_URL` in Vercel using
      the **transaction pooler (port 6543)**, then redeploy.
- [ ] **Before production**: rotate the DB password, service-role key, and JWT
      secret (shared in plaintext during local setup), and set `DEBUG=false`
      (also silences SQLAlchemy SQL echo). Consider hiding `/docs` in prod.

## 🧱 Next build step: ORM models + migrations

- [ ] Define SQLAlchemy 2.x ORM models under `app/models/` mirroring
      `database/schema.sql` (start with `users`, `roles`, `user_roles`, `alumni`).
- [ ] Initialize **Alembic** (`alembic init alembic`) and wire it to
      `app.core.config` / `app.core.database`.
- [ ] Create the first migration and confirm it round-trips against the schema.
      (Migration rules: every schema change is reversible and preserves data;
      never modify production directly.)

## 🔐 Authorization (depends on ORM models)

- [ ] On first login, upsert a `users` row keyed by `auth_user_id` (from the JWT
      `sub`).
- [ ] Resolve roles from the DB (`users` → `user_roles` → `roles`), **never** from
      the token's `role` claim.
- [ ] Add dependencies `require_full_access` and `require_view_only`, enforced
      server-side. Two roles only: **Full Access** and **View Only**.

## 🚀 Feature roadmap (from CLAUDE.md priorities)

- [ ] Alumni CRUD (archive instead of delete; soft-delete policy).
- [ ] Search & filtering in PostgreSQL (name, employer, industry, title, grad
      year, city, state, tags, status labels; combined filters; < 1s).
- [ ] CSV import (`byu_id` keyed; manual edits win when
      `manually_edited_at > last_imported_at`; audit + import batch + source).
- [ ] CSV export.
- [ ] Interactions, follow-up tasks, events + event attendance.
- [ ] Attachments via Supabase Storage (metadata in PostgreSQL; auth required for
      downloads; no public URLs).
- [ ] Audit logging on every significant modification (immutable).
- [ ] Duplicate detection (advisory only; human approval required to merge).
- [ ] Dashboard analytics (DB aggregation; counts, geographic/industry/employer
      summaries, event stats, missing-data + duplicate metrics).

## 🧪 Testing / quality

- [ ] Add DB-backed integration tests once models + a test database exist.
- [ ] Coverage targets: auth, authorization, alumni CRUD, imports, exports,
      duplicate detection, audit logging.
- [x] `ruff` wired into CI (`ruff check .`). Optional: add `ruff format --check`
      and a pinned lockfile for reproducible installs.

## 🖥️ Frontend (separate repo)

- The Supabase **Next.js** quickstart (`@supabase/ssr`, `.tsx`, middleware) does
  **not** belong in this Python backend repo. If/when a frontend is built, it
  lives in its own repo and talks to this API.
