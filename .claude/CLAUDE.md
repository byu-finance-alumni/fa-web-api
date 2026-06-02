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

* Alembic

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

alembic/

database/
└── schema.sql
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

Only two roles exist.

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

Authorization must be enforced server-side.

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

Schema changes require Alembic migrations.

Never manually modify production databases.

Every schema change must:

* Have a migration
* Be reversible
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
