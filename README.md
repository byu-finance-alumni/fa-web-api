# fa-web-api

Backend API and database layer for the **BYU Finance Alumni Database**.

Stack: FastAPI · PostgreSQL · SQLAlchemy 2.x (async) · Pydantic · Alembic · Supabase (auth/storage).

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

## Endpoints (current)

| Method | Path         | Description                                   |
|--------|--------------|-----------------------------------------------|
| GET    | `/`          | Service identification                        |
| GET    | `/health`    | Liveness check (process is up)                |
| GET    | `/health/db` | Readiness check (verifies DB connection)      |

`/health/db` returns `200 {"status":"ok","database":"connected"}` on success, or
`503` with the structured error envelope if the database is unconfigured/unreachable.

## Tests

```bash
pytest
```

## CI Checks

Every **pull request into** and **push to** `dev` and `prod` runs a set of checks
across three GitHub Actions workflows — `ci.yml`, `ferpa-audit.yml`,
`board-in-review.yml` — plus Vercel's own deployment check. This section
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
| **Migrate database** | only on **push to `prod`**, after the base+audit checks pass | Applies pending `database/migrations/*.sql` to the **prod** Supabase DB via `migrate.sh`, **held for manual approval** by the `production` Environment's required reviewers (secret `MIGRATIONS_DATABASE_URL`). The **dev** DB is migrated separately, by hand. Skipped on PRs and on dev pushes. |
| **Move linked issues to In Review** | when a PR is opened / marked ready | Moves the PR's linked board issues into **In Review** on org Project #4. Needs the `PROJECTS_TOKEN` secret; a graceful no-op without it (never fails a PR). |

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
5. **Back-merge `prod → dev` after every release** (end-of-day routine). A
   `dev → prod` merge creates a merge commit that lives only on `prod`, so `dev`
   immediately reads as "N commits behind prod" even though the code is identical
   — one commit per release. Sync it back so the count resets to 0:

   ```bash
   git fetch origin
   git push origin origin/prod:dev   # fast-forward dev up to prod (works while dev has no unmerged work)
   ```

   If branch protection blocks the direct push, open a quick `prod → dev` PR
   instead. Do this at the end of each working day so `dev` never drifts.

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
| `dev` (`dev-fa-web-api`)      | the original project       | mock/seed data — safe to test on  |
| `prod` (`finance-alumni-database-api`) | a new, dedicated project | clean; real alumni data later     |

Each deployment gets its own `DATABASE_URL` / `SUPABASE_*` (pointing at its
project). Schema migrations are applied to each database — see
[`database/migrations/README.md`](database/migrations/README.md).

> ⏳ The dedicated **prod** project is provisioned during the database split.
> Until then prod still points at the original project; the prod Vercel env vars
> and the `production` Environment's `MIGRATIONS_DATABASE_URL` are repointed as
> part of that step.

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
> changes go through Alembic migrations — never modify production directly.
