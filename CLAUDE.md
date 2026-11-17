# CLAUDE.md

Backend API (FastAPI + PostgreSQL on Supabase, deployed on Vercel) for the BYU Finance Alumni Database.
The frontend is the separate `fa-web-app` repo.

> **Pasted an email of changes from Slack?** Follow `docs/CHANGE-REQUESTS-FROM-SLACK.md` exactly.
> It covers asking questions, filing issues, putting the work on dev and reporting the time.

- Branches: work branches → PR into `dev` (sandbox) → `prod` (real alumni data). Never touch `prod`
  or the prod database.
- Checks: `ruff check .` and `DATABASE_URL="" pytest -q`.
- Schema: `database/schema.sql`; changes go in `database/migrations/` as new, additive `.sql` files.
- See `README.md` for the CI checks and release flow.
