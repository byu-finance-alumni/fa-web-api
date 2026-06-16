# Deploying fa-web-api to Vercel

This API runs on **Vercel's Python serverless runtime** as an ASGI app.

- `pyproject.toml` — declares runtime `dependencies` (so Vercel installs them)
  and `[tool.vercel] entrypoint = "app.main:app"` (the ASGI app Vercel serves).
- `app/main.py` — the FastAPI `app`. Vercel's Python preset auto-routes all
  requests to it (no `vercel.json` rewrites needed).
- `app/core/database.py` — auto-switches to a serverless-safe DB config when the
  `DATABASE_URL` uses Supabase's transaction pooler (port `6543`).

> Dependencies **must** be listed in `pyproject.toml` (or `requirements.txt`). If
> `pyproject.toml` exists with an empty `[project]` and no `dependencies`, Vercel
> installs nothing and the function crashes with `ModuleNotFoundError`.

> ⚠️ This API handles **private alumni PII** and will be publicly reachable once
> deployed. Confirm you are authorized to deploy, and treat the URL as sensitive.

## 0. Before you deploy — rotate secrets

The DB password and service-role key were entered during local setup. Before a
public deploy, rotate them in Supabase and use the new values below:

- DB password: Dashboard → Project Settings → Database → Reset database password
- Service-role key: Dashboard → Project Settings → API keys

## 1. Environment variables (set these in Vercel)

> 🔁 **`dev` and `prod` use separate Supabase projects.** The values below are for
> the **prod** project (a new, dedicated project created in the database split);
> the `dev-fa-web-api` deployment uses the **original** project's values (mock
> data). ⏳ Until the split completes, prod still uses the original project shown
> here (`tnnhhnzglyfqolxdojyb`) — swap in the new prod project's URL / keys /
> `DATABASE_URL` (and the `production` Environment's `MIGRATIONS_DATABASE_URL`)
> when it's provisioned.

| Variable | Value |
|---|---|
| `DATABASE_URL` | **Transaction pooler** URL — same host, **port 6543**: `postgresql://postgres.tnnhhnzglyfqolxdojyb:[NEW_PASSWORD]@aws-1-us-east-1.pooler.supabase.com:6543/postgres` |
| `SUPABASE_URL` | `https://tnnhhnzglyfqolxdojyb.supabase.co` |
| `SUPABASE_ANON_KEY` | publishable key (`sb_publishable_…`) |
| `SUPABASE_SERVICE_ROLE_KEY` | the **rotated** service-role key |
| `JWT_SECRET` | project JWT secret (HS256 project) — Settings → API → JWT Secret |
| `ENVIRONMENT` | `production` |
| `DEBUG` | `false` |

> Use the **transaction pooler (6543)**, not the session pooler (5432), on
> serverless. The DB layer detects `:6543` and disables prepared-statement
> caching + connection pooling automatically.

## 2A. Deploy via GitHub integration (recommended)

1. Push this repo to GitHub (already at `byu-finance-alumni/fa-web-api`).
2. In the Vercel dashboard → **Add New… → Project → Import** the repo.
3. Framework preset: **Other** (Vercel detects the Python function automatically).
4. Add the environment variables from step 1 (Production scope).
5. **Deploy.**

### Continuous deployment (two projects, one per branch)

This repo is connected to **two** Vercel projects so each long-lived branch has a
stable URL:

| Project | Builds | Role |
|---|---|---|
| `finance-alumni-database-api` | `prod` only | production API (`fa-web-api.vercel.app`) |
| `dev-fa-web-api` | `dev` + PR previews | dev API (`dev-fa-web-api.vercel.app`) |

Both projects are linked to the same GitHub repo, so by default each would build
*every* branch (you'd see duplicate deploys/checks on every PR). To scope them, set
each project's **Settings → Git → Ignored Build Step → "Run my own command"**:

- `finance-alumni-database-api`: `[ "$VERCEL_GIT_COMMIT_REF" != "prod" ]`  (build only `prod`)
- `dev-fa-web-api`: `[ "$VERCEL_GIT_COMMIT_REF" = "prod" ]`  (build everything except `prod`)

> Exit **0 = skip** the build, exit **1 = build**; the bracket test returns 0 when
> true. Use the bare `[ … ]` form — **not** `bash -c '…'`, which can lose its `-c`
> in that field and error the deploy with "No such file or directory".

The skipped project reports a passing **"Canceled by Ignored Build Step"** status —
harmless. All merges go through PRs with CI passing (see README).

> The Supabase↔Vercel integration auto-populates `SUPABASE_*` vars, but **not** a
> full `DATABASE_URL` (only `POSTGRES_PASSWORD` / `POSTGRES_DATABASE`). Add
> `DATABASE_URL` manually (transaction pooler, port 6543) for DB connectivity.

## 2B. Deploy via CLI (alternative)

```bash
npm i -g vercel
vercel login
vercel link            # create/link the project
# add env vars (repeat for each, or paste in the dashboard):
vercel env add DATABASE_URL production
vercel env add SUPABASE_URL production
vercel env add SUPABASE_ANON_KEY production
vercel env add SUPABASE_SERVICE_ROLE_KEY production
vercel env add JWT_SECRET production
vercel env add ENVIRONMENT production
vercel env add DEBUG production
vercel --prod          # deploy to production
```

## 3. Verify

```bash
curl https://<your-deployment>.vercel.app/health
curl https://<your-deployment>.vercel.app/health/db   # expect {"status":"ok","database":"connected"}
```

Swagger UI: `https://<your-deployment>.vercel.app/docs`

## Notes & trade-offs

- **Cold starts**: serverless functions sleep when idle; the first request after
  idle is slower. For an internal tool this is usually fine.
- **`/docs` is public** by default. To hide it in production, set
  `docs_url=None` in `app/main.py` when `ENVIRONMENT == "production"`.
- **If pooling/cold starts become painful**, a container host (Railway, Render,
  Fly.io, Google Cloud Run) keeps the app warm with a persistent connection pool
  and maps more cleanly to this stateful FastAPI app. The code runs unchanged.
