# CLAUDE.md

This file provides guidance to Claude Code when working in the fa-web-api repository.

# Project Purpose

This repository contains the backend API and database layer for the BYU Finance Alumni Database.

Responsibilities:

* Authentication
* Authorization
* Alumni CRUD operations
* Search and filtering
* CSV imports
* CSV exports
* Event management
* File metadata management
* Audit logging
* Duplicate detection
* Dashboard analytics
* Database migrations

This repository is the source of truth for business logic.

---

# Technology Stack

Framework:

* FastAPI

Database:

* PostgreSQL

ORM:

* SQLAlchemy 2.x

Migrations:

* Plain SQL files in `database/migrations/`, applied by `database/migrate.sh` (no Alembic)

Validation:

* Pydantic

Authentication:

* Supabase Auth

Storage:

* Supabase Storage

Testing:

* Pytest

Documentation:

* OpenAPI / Swagger

---

# Architecture Principles

Business logic belongs in the backend.

Never trust frontend validation.

All permissions must be enforced server-side.

The frontend should never directly access protected business logic.

---

# Project Structure

Recommended structure:

```text
app/
├── api/
│   ├── routes/
│   └── dependencies/
│
├── core/
│   ├── config.py
│   ├── security.py
│   └── database.py
│
├── models/
│
├── schemas/
│
├── services/
│
├── repositories/
│
├── utils/
│
└── main.py

tests/

database/
├── schema.sql          # source-of-truth schema snapshot
└── migrations/         # plain SQL migrations applied by migrate.sh (no Alembic)
```

---

# Database Rules

The PostgreSQL schema is the source of truth.

Never redesign tables without checking:

database/schema.sql

Always preserve:

* Foreign keys
* Constraints
* Indexes
* Audit requirements
* Provenance requirements

---

# Soft Delete Policy

Never hard delete alumni.

Use:

* archived
* deceased

Records should remain recoverable.

Audit history depends on retained records.

---

# Authentication

Authentication is required for all endpoints unless explicitly public.

Use Supabase Auth.

Validate:

* JWT tokens
* User identity
* User permissions

Never trust user-provided role information.

Always verify roles from the database.

---

# Authorization

Three roles exist, most → least privileged: `super_admin` ⊇ `full_access` ⊇
`view_only`. Defined in `app/core/roles.py` (`RoleName`); guards in
`app/api/dependencies/auth.py` (`require_super_admin` / `require_full_access` /
`require_view_only`).

## Super Admin

Everything Full Access can do, plus:

* Create user accounts
* Assign / change roles
* Issue temporary one-time passwords (first-login forced password reset)

Initially assigned to Tanya Harmon. User/role administration requires this role.

## Full Access

Allowed:

* Create
* Update
* Archive
* Import
* Export
* Merge duplicates
* Manage events
* Upload attachments

## View Only

Allowed:

* Read access only

Never allow write operations for view-only users.

Authorization must be enforced server-side. A higher role satisfies every lower
role's guard (super_admin passes full_access and view_only checks).

---

# API Design Standards

Use REST conventions.

Examples:

GET /alumni

GET /alumni/{id}

POST /alumni

PATCH /alumni/{id}

DELETE should generally archive rather than remove records.

---

# Response Standards

Successful responses:

* Consistent structure
* Proper status codes

Error responses:

```json
{
  "error": {
    "code": "validation_error",
    "message": "Description"
  }
}
```

Never expose:

* Stack traces
* Internal SQL
* Sensitive information

---

# Search Requirements

Search is a primary feature.

Support:

* Name
* Employer
* Industry
* Title
* Graduation year
* City
* State
* Tags
* Status labels

Support combined filters.

Filtering should occur in PostgreSQL.

Avoid loading large datasets into memory.

---

# CSV Import Rules

Imports use:

* byu_id

Business rule:

If:

manually_edited_at > last_imported_at

Then imported values must not overwrite manually edited fields.

Manual edits always win.

All imports must:

* Create audit entries
* Track import batch
* Track source

---

# Duplicate Detection

Duplicate detection is advisory.

The system may:

* Identify candidates
* Calculate confidence scores

The system must not:

* Automatically merge records

Human approval is required.

---

# Audit Logging

Every significant modification must create an audit record.

Track:

* User
* Timestamp
* Entity
* Field
* Previous value
* New value

Audit logs should be immutable.

---

# File Uploads

Store files in Supabase Storage.

Store metadata in PostgreSQL.

Supported examples:

* PDFs
* Resumes
* Meeting notes

Never expose public file URLs.

Require authentication for downloads.

---

# Dashboard Analytics

Support endpoints for:

* Alumni counts
* Employer counts
* Industry counts
* Geographic summaries
* Event statistics
* Missing data metrics
* Duplicate metrics

Prefer database aggregation.

Avoid expensive application-side calculations.

---

# Performance Requirements

Target scale:

* 10,000+ alumni
* 100,000+ interactions

Goals:

* Search under 1 second
* Dashboard under 3 seconds
* Import 10,000+ records

Use indexes where appropriate.

Optimize queries before adding caching.

---

# Security Requirements

Always validate:

* Request body
* Query parameters
* Uploaded files

Use:

* Parameterized queries
* ORM protections
* Input validation

Never:

* Store passwords
* Log PII
* Expose secrets
* Commit credentials

---

# Environment Variables

Expected environment variables:

```env
DATABASE_URL=

SUPABASE_URL=
SUPABASE_ANON_KEY=
SUPABASE_SERVICE_ROLE_KEY=

JWT_SECRET=

ENVIRONMENT=
DEBUG=
```

Never hardcode secrets.

Never commit .env files.

---

# Local Development

Python is pinned to **3.12** (`.python-version`, matches CI and the Vercel runtime).

Setup (using `uv`, which fetches 3.12 automatically):

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv -r requirements.txt
```

Run:

```bash
.venv\Scripts\python -m uvicorn app.main:app --reload   # http://127.0.0.1:8000  (docs at /docs)
```

The app boots with **no** database or secrets — all settings default to `None`.
`/` and `/health` return 200 with nothing configured; `/health/db` returns 503
until `DATABASE_URL` is set.

Environment values come from the Vercel project (`finance-alumni-database-api`,
scope `gunnjakes-projects`):

```bash
vercel link --project finance-alumni-database-api
vercel env pull .env --environment=production   # NOTE: default target is Development, which is empty
```

**Sensitive Vercel vars pull back EMPTY** (they are write-only): `DATABASE_URL`,
`SUPABASE_SERVICE_ROLE_KEY`, and the JWT/secret keys. Fill these from the
Supabase Dashboard (Settings ▸ Database / API). `JWT_SECRET` may stay blank —
the API then verifies tokens via the Supabase JWKS endpoint.

**`DATABASE_URL` / IPv4 gotcha:** the direct host `db.<ref>.supabase.co` is
**IPv6-only**. On an IPv4-only network use the **Session pooler** instead:

```env
DATABASE_URL=postgresql://postgres.<project-ref>:<password>@aws-1-us-east-1.pooler.supabase.com:5432/postgres
```

(`database.py` treats port 6543 as the transaction pooler and 5432 as the
session pooler; asyncpg negotiates SSL automatically — no `?sslmode=` needed,
and query params are stripped anyway.)

---

# Testing Requirements

All new features should include tests.

Minimum coverage:

* Authentication
* Authorization
* Alumni CRUD
* Imports
* Exports
* Duplicate detection
* Audit logging

Use pytest.

---

# Migration Rules

Schema changes require a plain SQL migration in `database/migrations/` (there is
no Alembic). See `database/migrations/README.md` for the workflow.

Never manually modify production databases.

Every schema change must:

* Have a migration (`YYYY-MM-DD_description.sql`, wrapped in `BEGIN; ... COMMIT;`)
* Enable deny-all RLS on any new table (match `database/rls_lockdown.sql`)
* Update `database/schema.sql` to reflect the new end state
* Preserve data

---

# Logging Rules

Log:

* Errors
* Warnings
* Import results
* Security events

Do not log:

* Passwords
* Tokens
* Personal information
* Full request payloads containing alumni data

---

# Out of Scope

Do not build unless specifically requested:

* Email campaigns
* Survey systems
* Alumni self-service accounts
* Public directory
* LinkedIn scraping
* Automated outreach

---

# Development Principles

1. Security first
2. Data integrity first
3. Auditability first
4. Keep business logic in services
5. Keep routes thin
6. Preserve schema conventions
7. Prefer maintainability over cleverness
8. Ask before introducing major architectural changes
