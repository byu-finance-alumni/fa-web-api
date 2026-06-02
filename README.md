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
`dev` (see `.github/workflows/ci.yml`). Two checks must pass before merging
`dev` → `prod`:

| Check | What it runs | Why |
|-------|--------------|-----|
| **Lint (ruff)** | `ruff check .` | catches lint errors, unused imports, bug-prone patterns |
| **Test (pytest)** | imports the app (cold-start sanity), then `pytest -q` | confirms the app boots and tests pass |

No secrets are required — tests don't touch a live database or Supabase.

To view results: open the repo on GitHub → **Actions** tab (or the **Checks**
section of a pull request) → select the **CI** workflow run.

## Project structure

```text
app/
├── api/
│   ├── routes/         # FastAPI routers (health, alumni, ...)
│   └── dependencies/   # shared route dependencies (auth, db session)
├── core/
│   ├── config.py       # env-based settings
│   ├── database.py     # async engine, session, connection check
│   └── security.py     # (auth — later)
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
