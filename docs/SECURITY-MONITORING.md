# Security Monitoring Runbook

Implements GitHub issue #48 (PRE-LAUNCH §8). Turns the ad-hoc security monitoring
into a repeatable routine an operator — or a scheduled agent — can run verbatim.
It does **not** build the optional security-events table / in-app Security screen /
SIEM log drain; those are deferred (see [§4](#4-deferred-future-work)).

## Infrastructure facts (read first)

- **4 Vercel projects:** prod app `finance-alumni-database`, prod api
  `finance-alumni-database-api`, dev app `dev-fa-web-app`, dev api `dev-fa-web-api`.
- **Supabase:** dev and prod currently **share one DB** (the #41/#42 split is on
  hold), so the SQL/Auth checks below cover both environments at once — a dev test
  login and a prod sign-in land in the same tables.
- **Controls being monitored:**
  - Pre-login lockout (`app/services/login_lockout.py`): soft **cooldown at 10**
    failed attempts / 60-min window (any email, 5-min auto-clear); **hard lock at
    20** (registered users, sticky until a super_admin reset). State in
    `login_attempts` + `users.locked_at` / `locked_reason`.
  - `login_events`: one row per **successful** sign-in (ip_address, city, region,
    country); 90-day pg_cron retention purge.
  - `audit_logs`: record-change trail with `actor_email`/`actor_name` snapshot.
  - `security_event` structured stdout logs (`app/core/security_log.py`):
    `auth_failed` (401), `not_provisioned` (403), `forbidden` (403),
    `account_deactivated` (403), `password_change_required` (403),
    `upstream_service_error` (502), `unhandled_error` (500).
  - Vercel WAF rate-limit rules on all 4 projects, all keyed on `ip` with `deny`
    on exceed. **Verified against the live config 2026-08-29**: prod app and prod
    api **1000/60s**; `dev-fa-web-app` 300/60s; `dev-fa-web-api` 100/60s. Only the
    app rules exclude `/_next/*`. Automatic DDoS mitigation always on.
    - ⚠️ **Do not trust this line — or a rule's name — over a read-back.** From
      2026-06-18 to 2026-08-29 this document asserted "app projects 300/60s" as an
      infrastructure fact while prod was **live at 60/60s**, denying ordinary staff
      paging with a bare edge 403 (app #796). The rule's own name and description
      also said 300, and its id still reads `rule_rate_limit_100_...`. Dev was
      correct at 300 the whole time, so every test on dev passed.
      **A firewall change applied to dev is not applied to prod.** Read the value
      back per project: `vercel firewall rules list --expand`.
    - ⚠️ Per-IP keying is a shared bucket in practice — staff share BYU's campus
      NAT egress, so the effective ceiling is the limit divided by however many
      people are working at once.
    - Canonical inventory: **`fa-web-app/docs/FIREWALL.md`**. Update it in the same
      change as any firewall edit.

> **All SQL here is read-only (SELECT only). Never run mutating SQL from this
> routine.** Run via `fa-web-api/.env` `DATABASE_URL` (session pooler `:5432`,
> `PGCLIENTENCODING=UTF8`, ASCII-only) or the Supabase SQL editor. Times are UTC.

---

## 1. The three sources

### Source A — Supabase Auth + auth-state SQL

**A0. Dashboard → Logs → Auth (manual).** Last 24h; scan for repeated
`400`/`invalid_grant` clustered on one IP or one email.
*Anomaly:* >20 failed sign-ins from one IP in 1h, or failures against >5 distinct
emails from one IP (spray). *Escalate:* add a temporary IP Deny rule (Source B) +
notify the super_admin.

**A1. Active cooldowns / near-lock accounts**
```sql
SELECT email_lc, failed_count, first_failed_at, last_failed_at, cooldown_until
FROM login_attempts
WHERE last_failed_at >= now() - interval '24 hours'
ORDER BY failed_count DESC, last_failed_at DESC;
```
*Anomaly:* any `failed_count >= 10`, or several emails climbing past 5 in one
window. *Escalate:* cross-reference the IP in A0/Source C; deny if it spans many
emails.

**A2. Hard-locked accounts**
```sql
SELECT user_id, email, locked_at, locked_reason, last_login_at
FROM users WHERE locked_at IS NOT NULL ORDER BY locked_at DESC;
```
*Anomaly:* any NEW row since the last scan; multiple locks in one window = active
attack or lockout-DoS. *Escalate:* confirm with the user; super_admin reset clears
it; if it looks like a deliberate lockout-DoS, deny the source IP.

**A3. Anomalous successful logins — new country for that user**
```sql
SELECT e.occurred_at, e.email, e.ip_address, e.city, e.region, e.country
FROM login_events e
WHERE e.occurred_at >= now() - interval '24 hours'
  AND NOT EXISTS (
    SELECT 1 FROM login_events p
    WHERE p.user_id = e.user_id
      AND p.occurred_at <  now() - interval '24 hours'
      AND p.occurred_at >= now() - interval '30 days'
      AND COALESCE(p.country,'') = COALESCE(e.country,''))
ORDER BY e.occurred_at DESC;
```
*Anomaly:* a successful login from a never-before-seen country (esp. non-US).
*Escalate:* confirm with the user; if unconfirmed, deactivate + force reset.

**A3b. Impossible travel (same user, two countries <2h apart)**
```sql
SELECT a.email, a.occurred_at AS first_at, a.country AS first_country,
       b.occurred_at AS second_at, b.country AS second_country
FROM login_events a
JOIN login_events b ON a.user_id = b.user_id
 AND b.occurred_at > a.occurred_at
 AND b.occurred_at < a.occurred_at + interval '2 hours'
 AND COALESCE(a.country,'') <> COALESCE(b.country,'')
WHERE a.occurred_at >= now() - interval '24 hours'
ORDER BY a.occurred_at DESC;
```
*Anomaly:* any row. *Escalate:* same as A3.

**A4. Sensitive identity/role actions (catch-all, last 24h)**
```sql
SELECT created_at, actor_email, action_type, entity_type, entity_id
FROM audit_logs
WHERE created_at >= now() - interval '24 hours'
  AND entity_type IN ('user','role','user_role')
ORDER BY created_at DESC;
```
*Anomaly:* any role change / user create / user delete whose `actor_email` is NOT
a known super_admin/engineer; a burst of such actions; or `actor_email IS NULL` on
a sensitive action (the snapshot trigger should always populate it). *Escalate:*
verify with the named actor; if unexpected, treat as compromise (deactivate +
reset) and review what changed.

### Source B — Vercel Firewall (per project)

Use a temp dir so the repo's `.vercel` link is never touched:
```bash
tmp="$TEMP/wf-check"; mkdir -p "$tmp"
vercel link --yes --project finance-alumni-database-api --scope byu-finance-db --cwd "$tmp"
vercel firewall overview --cwd "$tmp"        # repeat per project slug
```
Look at blocked/challenged totals, top offending IPs, which rule fired, and confirm
Attack Challenge Mode is **OFF**. *Anomaly:* a sharp jump vs the prior day, one IP
responsible for a large share, or sustained rate-limit denials against `/auth/*` on
the api projects. *Escalate:* add a temporary IP Deny; if distributed/availability-
impacting, enable Attack Challenge Mode on the affected prod project + notify the
super_admin. Record the rule + reason.

### Source C — Runtime logs: `security_event` + error spikes (per project)

Search each project's logs (dashboard → Logs, or `vercel logs <url>`) for
`security_event`, tally by event type and source IP, and check 4xx/5xx volume.
Daily liveness smoke test:
```bash
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer garbage" \
  https://fa-web-api.vercel.app/auth/me      # expect 401 + an auth_failed log line
```
*Anomaly thresholds (per project, 24h):* **any** `not_provisioned` (valid token,
no users row — investigate each); `auth_failed` burst from one IP (>50/24h);
`forbidden`/`account_deactivated` >10 from one principal; any `unhandled_error`
(500) / `upstream_service_error` (502) spike; general 4xx/5xx step-change.
*Escalate:* deny hostile IPs (Source B); pull tracebacks for 5xx spikes; if the
smoke test produces NO log line, treat logging as broken and escalate (blind spot).

---

## 2. Cadence

- **Daily quick scan (~5 min, automatable):** A0–A4 + Source C tally/smoke test +
  Source B overview for the two **prod** projects. Run ~13:00 UTC (07:00 Mountain)
  so an overnight attack is caught before the workday.
- **Weekly deeper review (~30 min, operator):** all 4 projects' firewall overviews
  incl. dev; 7-day `login_events` geo + `audit_logs` privilege trend; prune
  temporary Deny rules; confirm Attack Challenge Mode OFF everywhere; re-tune
  lockout/WAF thresholds if the baseline shifted.

The real threats are online credential attacks against a small known-population
login and misuse of admin privilege over alumni PII — both surface within hours
and are cheap to detect daily from the four built-in signals. Daily automation =
fast, near-zero-cost detection; the weekly human pass catches slow trends and keeps
the firewall state clean.

## 3. Scheduled-agent spec (daily scan)

Register via the scheduling skill (suggested `0 13 * * *` UTC). The agent has repo
access (`fa-web-api/.env` psql creds), the authed `vercel` CLI, and psql.

> Run the read-only daily scan from `docs/SECURITY-MONITORING.md` §1. Do NOT run
> mutating SQL, change firewall rules, or alter any config — observe and report
> only. (1) Run A1–A4b; capture rows + counts. (2) For the two prod projects, run
> `vercel firewall overview` (temp-linked dir); record blocked/challenged totals +
> top IPs; confirm Attack Challenge Mode OFF. (3) For both prod projects, count
> each `security_event` type + top IPs over 24h; note any 4xx/5xx spike. (4) Run
> the smoke test; confirm 401 + a fresh `auth_failed` line. (5) Apply the §1
> thresholds. Output **ALL CLEAR** only if every check is under threshold and the
> smoke test passed, else **ESCALATE** with the offending evidence + recommended
> (not executed) remediation, and tag the super_admin. Never remediate yourself.

## 4. Deferred future work

Detection works today without these; not yet scoped:
- **`security_events` persistence table** — durable storage of the stdout
  `security_event` stream for historical query/correlation.
- **Super_admin in-app "Security" review screen** — UI for events/lockouts instead
  of the SQL + logs workflow.
- **SIEM log drain** — Vercel Log Drain (Pro) to an external SIEM for retention +
  alerting.

Revisit Source A/C scoping per-environment once the dev/prod Supabase split
(#41/#42) lands.
