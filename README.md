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

GitHub Actions runs on every **pull request** into and **push** to `prod` and
`dev` (see `.github/workflows/ci.yml`). Checks are **two-tiered**:

**Base tier — runs for both `dev` and `prod`:**

| Check | What it runs | Why |
|-------|--------------|-----|
| **Lint (ruff)** | `ruff check .` | lint errors, unused imports, bug-prone patterns |
| **Test (pytest)** | imports the app (cold-start sanity), then `pytest -q` | confirms the app boots and tests pass |
| **Secret scan (gitleaks)** | `gitleaks detect` over full git history | blocks committed secrets (keys, tokens, DB URLs) |

**Prod-only tier — runs only when promoting to `prod`:**

| Check | What it runs | Why |
|-------|--------------|-----|
| **Dependency audit (prod only)** | `pip-audit` | blocks known-vulnerable dependencies before release |

gitleaks false positives (test-only dummy secrets in `tests/`) are allowlisted in
`.gitleaks.toml` — keep entries narrow so real secrets are still caught. No secrets
are required for the run itself; tests don't touch a live database or Supabase.

To view results: repo → **Actions** tab (or a PR's **Checks** section) → **CI** workflow.

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
