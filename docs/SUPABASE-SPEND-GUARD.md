# Supabase spend guard — keeping the prod bill at $25

_Written 2026-09-16, the day after the prod org went Pro. Prices and the
Spend Cap's coverage list were checked against the Supabase docs that day._

The prod org is expected to cost **$25/month**: $25 Pro plan + ~$10 Micro
compute − $10 compute credits. This page is about the two things that keep it
there, and which one actually does the blocking.

## The one setting that blocks a charge

**The Spend Cap toggle on the org's billing page (Cost Control) must be ON.**
That is the only thing on Supabase's side that refuses to bill. It stops usage
overages: egress, storage, disk size, MAU, function invocations, logs, and so
on. Pro turns it on by default. Nobody should turn it off.

The guard cannot see this toggle and cannot flip it. The Management API does not
expose it (checked against the spec at `https://api.supabase.com/api/v1-json` on
2026-09-16). Confirm it by eye: org → Billing → Cost Control → "Spend cap" = on.

## What the Spend Cap does NOT cover, and the guard does

The Spend Cap explicitly does not cover the opt-in add-ons. Every one of them is
a click in the dashboard and shows up as money:

- a bigger compute instance (Small is ~$15 → bill goes to ~$30)
- another project in the same org (each one is another ~$10 of compute)
- preview branches (branching compute), read replicas
- PITR (~$100), custom domain, dedicated IPv4, log drains, MFA phone
- a paid disk tier (io2) or extra IOPS / throughput

`scripts/supabase_spend_guard.py` reads the org's configuration through the
Management API once a day (`.github/workflows/supabase-spend-guard.yml`) and
fails the run if any of those is switched on. Five named lines, each PASS,
WARN or FAIL:

| Check | What it asserts |
|---|---|
| `plan` | the org's plan is `pro` (not Free, not Team) |
| `org-membership` | the prod org contains exactly the prod project and nothing else — no second project, no branch |
| `compute-variant` | compute is Micro. Nano is a WARN, not a FAIL (see below) |
| `no-other-addons` | nothing in `selected_addons` besides compute; no read replicas; no branches; disk is gp3 at the included IOPS/throughput |
| `projected-monthly` | plan fee + every selected add-on − compute credits ≤ $25.00, with the arithmetic printed |

Any FAIL makes the run red. It does not undo anything — a human does that in the
dashboard. Add-ons are pro-rated hourly, so a red run means at most ~24 hours of
the mistake (cents), instead of finding out on the invoice.

### What it can't do

- It cannot check or set the Spend Cap. Only you can.
- It cannot see usage (egress, storage). That is the Spend Cap's job, and usage
  overages are blocked, not billed, while the cap is on.
- It cannot prevent a purchase. It detects one within a day.
- It stops when the token stops working. A run that cannot reach the API is a
  FAIL, not a PASS, so an expired token still goes red.

### The Nano warning

Prod was a Free project, so it runs on **Nano**. On a paid plan Nano is **billed
at the Micro price** and is **never auto-upgraded**. The guard reports this as
`WARN compute-variant` and still exits 0 — the bill is the same $25 either way.
Bump it to Micro when convenient (Project Settings → Compute and Disk; it
restarts the database, so pick a quiet hour). After that the WARN becomes PASS.

## Setup (one time, ~5 minutes)

1. **Create a personal access token.** Supabase → account menu → Access Tokens
   (`https://supabase.com/dashboard/account/tokens`) → Generate new token. Name
   it `fa-web-api spend guard` so it can be found and revoked. If the page
   offers scopes, pick the least: read-only `projects:read` +
   `organizations:read` is all this needs. If it does not, the PAT is a full
   account token — which is why it goes into exactly one secret and nowhere
   else, and gets revoked if this workflow is ever deleted.
2. **Add the secret.** Repo → Settings → Secrets and variables → Actions →
   Secrets → `SUPABASE_ACCESS_TOKEN`.
3. **Add the variables** (same page, Variables tab):
   - `SUPABASE_PROD_REF` = `njobhhdopwdodvzosrns` (the workflow falls back to
     this value if the variable is missing)
   - `SUPABASE_PROD_ORG` = the prod org's slug (from the org's URL in the
     dashboard). Optional: without it the guard reads the org from the project;
     with it the guard also asserts the project really is in that org.
4. **Run it once by hand today.** Actions → "Supabase spend guard" → Run
   workflow. Expect five lines: four PASS and `WARN compute-variant` until the
   Nano→Micro bump is done. Anything red on the first run is real — read the
   arithmetic block under `projected-monthly`.
5. The schedule (daily, 13:30 UTC ≈ 07:30 Utah) only fires from the default
   branch, `prod`. Until this is promoted, the Run workflow button is the only
   way it runs.

A failed run emails whoever last edited the workflow (GitHub's default) and
posts one line to the security Slack channel via `SLACK_SECURITY_WEBHOOK_URL`,
the same webhook the weekly audit uses. A green run is silent.

## Running it locally

```
SUPABASE_ACCESS_TOKEN=... python -m scripts.supabase_spend_guard --ref njobhhdopwdodvzosrns
python -m scripts.supabase_spend_guard --ref njobhhdopwdodvzosrns --dry-run   # prints the requests, no network
```

Stdlib only — no `pip install` needed. The token is never printed; errors are
redacted (no headers, no query strings) before they reach the log.

## What it reads (Management API)

| Request | Fields used |
|---|---|
| `GET /v1/projects` | `ref`, `organization_slug`, `organization_id`, `name`, `status` |
| `GET /v1/organizations/{slug}` | `plan`, `name` |
| `GET /v1/organizations/{slug}/projects` | `projects[].ref`, `.is_branch`, `.databases[].type` (PRIMARY / READ_REPLICA), `.databases[].infra_compute_size` (nano, micro, …), `pagination.count` |
| `GET /v1/projects/{ref}/billing/addons` | `selected_addons[].type` (compute_instance, pitr, custom_domain, ipv4, auth_mfa_phone, auth_mfa_web_authn, log_drain, etl_pipeline), `.variant.id` (ci_micro, ci_small, pitr_7, …), `.variant.price.amount` + `.interval` (hourly / monthly); `available_addons` for the Micro price when Nano has no add-on entry |
| `GET /v1/projects/{ref}/config/disk` | `attributes.type` (gp3 / io2), `.iops`, `.throughput_mibps` |

Hourly prices are converted at 744 h/month, which is how Supabase turns Micro's
$0.01344/h into the "$10/month" it quotes.
