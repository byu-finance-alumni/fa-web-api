# fa-web-api

Backend API and database layer for the **BYU Finance Alumni Database**.

Stack: FastAPI · PostgreSQL · SQLAlchemy 2.x (async) · Pydantic · Supabase (auth/storage).

⚠️ **There is no Alembic.** Migrations are hand-written `.sql` files applied in
lexical order by `database/migrate.sh` and recorded in `schema_migrations` — see
[`database/migrations/README.md`](database/migrations/README.md).

## Setup

```bash
python -m venv .venv
# Windows PowerShell:  .venv\Scripts\Activate.ps1
# macOS/Linux:         source .venv/bin/activate

pip install -r requirements-dev.txt
cp .env.example .env   # then fill in DATABASE_URL etc.
```

## Run

```bash
uvicorn app.main:app --reload
```

- API root: http://127.0.0.1:8000/
- Swagger docs: http://127.0.0.1:8000/docs

## Endpoints

The surface is far too large to mirror by hand here and a hand-kept list goes
stale immediately. **Read the generated schema instead** — it is the contract the
frontend's types are built from:

- Local: <http://127.0.0.1:8000/docs>
- Dev: <https://dev-fa-web-api.vercel.app/docs>
- ⚠️ **Prod returns 404 for `/docs`, `/redoc` and `/openapi.json` — by design**
  (`DEBUG=false`). That is not an outage; don't chase it, and don't point the
  frontend's type generator at prod.

Routers live in `app/api/routes/` (~19 of them: alumni, survey, dashboard, events,
audit, engineer, storage, notes, geography, vocabulary, …).

**Health checks**, which are stable and worth knowing:

| Method | Path         | Description                                        |
|--------|--------------|----------------------------------------------------|
| GET    | `/`          | Service identification                             |
| GET    | `/health`    | Liveness — also reports `environment` and `version` |
| GET    | `/health/db` | Readiness — verifies the DB connection             |

`/health/db` returns `200 {"status":"ok","database":"connected"}`, or `503` with
the structured error envelope if the database is unconfigured/unreachable.

⚠️ **`/health` reporting `200` proves the process is up, nothing more.** Read its
`environment` field before assuming which deployment you are talking to.

## Tests

```bash
DATABASE_URL="" pytest
```

⚠️ **Clear `DATABASE_URL` when running locally.** The repo `.env` supplies a real
connection string, and ~17 survey tests then talk to an actual database and fail
for reasons that have nothing to do with your change. CI has no `.env`, so it
never sees this — which is why "passes in CI, fails locally" is usually this.

## CI Checks

Every **pull request into** and **push to** `dev` and `prod` runs a set of checks
across the GitHub Actions workflows in `.github/workflows/` — `ci.yml`,
`ferpa-audit.yml` and `security-audit.yml` — plus Vercel's own deployment check.
(`board-in-review.yml` is **gone**, deleted 2026-07-03; see the board note below.) This section
documents **every check**: what it does, why it exists, when it runs, and whether
it's a **required** status check (a required check that isn't green blocks the
merge; required checks are configured in the repo's branch **rulesets**, not in
these files).

Two principles drive the design:
- **`dev` promotes to `prod`** (which holds **real alumni data**), so every gate
  that protects prod also runs on dev — bad code/secrets/data exposure must be
  caught before they can ride a promotion.
- **Tiered:** a *base* tier runs on both branches; a *prod-only* tier adds extra
  hardening that only matters at release time. A prod-only check is **skipped on
  dev** and must therefore **never** be marked required on `dev` (a skipped
  required check blocks merges forever).

### Base tier — runs on `dev` **and** `prod` (required on both)

| Check | What it runs | Why it exists |
|-------|--------------|---------------|
| **Lint (ruff)** | `ruff check .` | Catches lint errors, unused imports, and bug-prone patterns before they land. |
| **Test (pytest)** | imports `app.main` (cold-start sanity), then `pytest -q` | Confirms the ASGI app boots (what Vercel does at cold start) and the full test suite passes — auth/RBAC, data integrity, FERPA, etc. |
| **Secret scan (gitleaks)** | `gitleaks detect` over **full** git history | Blocks committed secrets (API keys, tokens, DB URLs) anywhere in history, not just the tip. Test-only dummy secrets are narrowly allowlisted in `.gitleaks.toml`. |
| **Deploy deps (pyproject parity)** | installs **only** `pyproject.toml` `[project.dependencies]` into a clean env, then imports `app.main` | Vercel's Python builder installs from `pyproject.toml`, **not** `requirements.txt`. A runtime dep missing there deploys fine in CI but 500s the live function on import — this catches it here instead. |
| **Repo hygiene (no scratch artifacts)** | fails if any tracked file matches scratch patterns (`TEST_*`, `SCRATCH*`, `DRAFT_*`, `*.scratch`, `.board-seed*`, `*DO_NOT_MERGE*`) | `dev` is the AI/testing sandbox that promotes to prod — throwaway files must never ride along. Lowercase `tests/` is unaffected. |
| **FERPA static check** | `python scripts/ferpa_check.py` (deterministic, no API key) | Enforces FERPA/privacy controls statically: **every DB table has deny-all RLS**, `SQL_ECHO` is guarded off in production, and no secrets are committed. See `scripts/FERPA_CHECKS.md`. |

### Prod-only tier — runs only when promoting to `prod` (required on `prod`)

| Check | What it runs | Why it exists |
|-------|--------------|---------------|
| **Dependency audit (prod only)** | `pip-audit -r requirements.txt` | Blocks known-vulnerable dependencies before a release. Skipped on `dev` (so routine dev work isn't blocked by a new advisory) — and therefore **not** required on `dev`. |

### Deploy & automation (not pass/fail gates you write code against)

| Check / job | When | What it does |
|-------------|------|--------------|
| **Migrate database** | only on **push to `prod`**, after the base+audit checks pass | Applies pending `database/migrations/*.sql` to the **prod** Supabase DB via `migrate.sh` (secret `MIGRATIONS_DATABASE_URL`). Runs **automatically — there is no approval gate**, see below. Skipped on PRs. |
| **Migrate database (dev)** | only on **push to `dev`** | Same, against the **dev** database. |

> ⚠️ **Never put a manual-approval gate on these jobs.** There used to be one
> (`production` Environment required reviewers). It did not make prod safer — it
> made it worse: the approval held the *migration* while **Vercel deployed the
> code independently**, so new code ran against the old schema and took prod
> down. It was removed and must not come back.
>
> ⚠️ **The job trails the Vercel deploy by minutes.** So sequence the *deploy*,
> not the migration: for any schema-dependent change, push a **migration-only
> commit to `prod` first** (cut it from `prod`, not `dev`), let it apply, and
> only then promote the code. Old code against a new schema is safe; new code
> against an old schema fails on every write that needs it.

> ⚠️ **Board cards are moved BY HAND.** The `board-in-review.yml` workflow that
> used to do it was **deleted from both repos on 2026-07-03**: it regex-matched
> bare `#NNN` in PR text, so a number that meant an issue in *this* repo moved the
> same-numbered issue in the *other* one. When writing PR bodies, avoid bare
> `#NNN` for cross-repo references — write "fa-web-app PR 762".

### External — Vercel deployment check (required)

| Check | Branch | What it does |
|-------|--------|--------------|
| **Vercel – dev-fa-web-api** | `dev` | Vercel builds + deploys the branch to the **dev** API project. Green = the deploy didn't break. Required on `dev`. |
| **Vercel – finance-alumni-database-api** | `prod` | Same for the **prod** API project. Required on `prod`. |

> ⚠️ The Vercel check names contain a real **en-dash `–` (U+2013)**. When editing
> the rulesets' required checks, preserve that exact character — a mangled name
> (e.g. via a Windows cp1252 round-trip) becomes a required check that can never
> report, silently **blocking all merges**.

### Required status checks (summary)

| Branch | Required to merge |
|--------|-------------------|
| **`dev`** | Lint (ruff) · Test (pytest) · Secret scan · Deploy deps · Repo hygiene · FERPA static check · Vercel – dev-fa-web-api |
| **`prod`** | the above **+** Dependency audit (prod only) · Vercel – finance-alumni-database-api |

To view results: repo → **Actions** tab, or a PR's **Checks** section.

## Branch & deploy workflow

| Branch | Role | Protection | Vercel project |
|--------|------|------------|----------------|
| `prod` | production | PR required; base + **Dependency audit** must pass | `finance-alumni-database-api` (builds `prod` only) |
| `dev`  | integration | PR required; base checks must pass | `dev-fa-web-api` (builds `dev` + PR previews) |

Day-to-day flow:

1. Branch off `dev` (e.g. `feat/...`, `docs/...`).
2. Open a PR into `dev`; base checks (lint, test, secret-scan) + the `dev-fa-web-api`
   preview must pass.
3. Merge to `dev`.
4. Release by opening a PR `dev → prod`; the prod-only dependency audit also runs,
   and on merge `finance-alumni-database-api` deploys production.
5. **Back-merge `prod → dev` after every release.** A
   `dev → prod` merge creates a merge commit that lives only on `prod`, so `dev`
   immediately reads as "N commits behind prod" even though the code is identical
   — one commit per release. Sync it back so the count resets to 0:

   ⚠️ **Direct pushes to `dev` are blocked by the ruleset** —
   `git push origin origin/prod:dev` is rejected ("repository rule violations").
   Open a PR; the content is identical so its checks pass quickly:

   ```bash
   git fetch origin
   gh pr create --base dev --head prod --title "chore: back-merge prod into dev"
   ```

   That leaves `dev` 1 ahead (the back-merge commit) and `prod` 0 ahead — the
   correct synced state. Verify `git rev-list --count origin/dev..origin/prod`
   is `0` and `git diff origin/dev origin/prod` is empty.

   Do this **immediately after every promotion**, not only at end of day.

Both branches reject direct pushes — all changes go through pull requests.

**Vercel is split into two projects, one per branch**, both linked to this repo.
Each uses an *Ignored Build Step* (Settings → Git) so it only builds its own branch:

- `dev-fa-web-api` → builds `dev` + PR previews: `[ "$VERCEL_GIT_COMMIT_REF" = "prod" ]`
- `finance-alumni-database-api` → builds `prod` only: `[ "$VERCEL_GIT_COMMIT_REF" != "prod" ]`

(Exit 0 = skip build, exit 1 = build; the bracket test returns 0 when true.) Python
is pinned to **3.12** (`.python-version`) to match CI and the Vercel runtime.

### Databases (Supabase) — one per environment

`dev` and `prod` each have their **own Supabase project** — a separate Postgres
database, Auth users, and keys. They no longer share one database:

| Environment (Vercel)          | Supabase project           | Data                              |
|-------------------------------|----------------------------|-----------------------------------|
| `dev` (`dev-fa-web-api`)      | the original project       | mock/seed data — safe to test on |
| `prod` (`finance-alumni-database-api`) | a dedicated project | **real alumni data** |

Each deployment gets its own `DATABASE_URL` / `SUPABASE_*` (pointing at its
project). Each database is migrated by its own CI job — see
[`database/migrations/README.md`](database/migrations/README.md).

> ✅ **The database split completed 2026-07-09.** prod has its own Supabase
> project, its own Vercel env vars, and its own `MIGRATIONS_DATABASE_URL` on the
> `production` Environment.
>
> ⚠️ **prod is real alumni data. Never run scratch queries or destructive work
> against it** — dev is the sandbox. `curl <api-url>/health` reports which
> `environment` you are talking to; check before assuming.
>
> ⚠️ **The Vercel project names do not match the repo names** — prod API is
> `finance-alumni-database-api`, prod app is `finance-alumni-database`. A
> deployment poll filtered on the repo name waits forever on a deploy that
> already succeeded.

## Project structure

```text
app/
├── api/
│   ├── routes/         # FastAPI routers (health, alumni, ...)
│   └── dependencies/   # shared route dependencies (auth, db session)
├── core/
│   ├── config.py       # env-based settings
│   ├── database.py     # async engine, session, connection check
│   └── security.py     # Supabase JWT verification
├── models/             # SQLAlchemy ORM models
├── schemas/            # Pydantic request/response models
├── services/           # business logic
├── repositories/       # data access
├── utils/
└── main.py             # FastAPI app entrypoint
tests/
database/
└── schema.sql          # source of truth for the schema
```

> The PostgreSQL schema in `database/schema.sql` is the source of truth. Schema
> changes go through **hand-written SQL migrations** in `database/migrations/`
> (there is no Alembic) — never modify production directly.
>
> Every **new table** must get deny-all RLS to match `database/rls_lockdown.sql`;
> the FERPA static check fails the build if one doesn't.
