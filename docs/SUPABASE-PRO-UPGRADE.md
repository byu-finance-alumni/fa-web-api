# Supabase Pro Upgrade — Readiness Plan

_Status: written 2026-09-09 for api #523. This is the package to read immediately
before clicking Upgrade. The purchase is the human's; nothing in this document was
executed, no dashboard setting was touched, and the prod database was not read._

Tanya and Amy have already approved paying for Pro (recorded 2026-08-19). This is
**not** a cost justification, and it is explicitly **not** a reason to do
signed-URL / thumbnail / egress-reduction work — upgrading is the decided path.

Every plan and price claim below is cited to current Supabase documentation in
[§7](#7-sources). Anything that could not be confirmed from docs or from the repo
is marked **UNCONFIRMED** rather than guessed, because real money is being spent
against this page.

---

## Infrastructure facts (read first)

- **Two Supabase projects**, split 2026-07-09: dev `tnnhhnzglyfqolxdojyb`, prod
  `njobhhdopwdodvzosrns`. Separate databases, separate Auth, separate settings.
  Anything set in the dashboard is **per project** and applying it to dev does not
  apply it to prod (`DEPLOYMENT.md` §1).
- **Measured baseline** (2026-08-19, from real prod numbers — not re-derived here):
  1,438 active alumni, 583 with a photo, prod DB **41 MB**, headshots bucket
  **94 MB / 583 objects**, MAU **8**, Realtime and Edge Functions **unused**.
- ⚠️ **The dashboard Storage figure is a billing-cycle AVERAGE, not a snapshot.**
  It read 1.18 GB the day after the bucket was actually 94 MB. Do not "correct"
  the bucket size from that number, before or after the upgrade.
- **What binds on this project is egress, and egress binds on the number of staff
  users (~15), not on alumni count.** Adding alumni records is cheap.
- The last clean measurement showed **6.37 GB uncached egress vs 0.076 GB cached**
  in a cycle — i.e. **already over the Free plan's 5 GB uncached quota**. See the
  pre-upgrade checklist item on grace periods.

---

## 1. What to upgrade, in what order, and what it costs

### The billing unit is the ORGANIZATION, not the project

This is the single most misunderstood part of Supabase pricing, and it decides the
number on the invoice:

- **The plan (Free / Pro / Team / Enterprise) belongs to the organization.** An org
  has exactly one subscription. You **cannot** mix a Pro project and a Free project
  inside one organization.
- **Compute belongs to the project.** Every project is its own VM and Postgres
  instance, billed hourly as a per-project add-on that lands on the org's invoice.
  Each additional project adds roughly **$10/month** at the default size.
- So **$25/month buys the plan for the org, not a project.** The projects are then
  billed on top, and every paid org gets **$10/month of Compute Credits** back —
  enough to cover exactly one project at the default size.

### The decision (Jake, 2026-09-15) — dev was moved to a separate org

**Superseded: the single-org ~$35 table below no longer applies.** Jake moved the
dev project out, so the two orgs are real and the prod org is upgraded **alone**.
Dev stays on **Free** in its own org. This is the "two separate organizations →
leave dev on Free" path described in the next section, chosen deliberately.

| Line item | Amount |
|---|---|
| Pro plan (prod organization) | $25.00 |
| Compute — prod project, default size | ~$10.00 |
| Compute Credits (included with any paid plan) | −$10.00 |
| **Expected monthly total** | **~$25.00 + tax** |

Dev adds **nothing** to this invoice while it sits in a Free org.

⚠️ **The one consequence to accept, not discover later:** #743 (Supabase's own
Inactivity timeout) is a **Pro-only, per-project** setting. With dev on Free it
**cannot be enabled or tested on dev** — it would have to be configured directly on
prod and verified there, against real alumni data. That is the cost of the $10
saved. Decide it as part of #743, not on the day.

✅ **Dev auto-pausing is already solved and is NOT a reason to reconsider.** The
api `supabase-keepalive.yml` pings dev and prod `/health/db` daily; dev has stayed
up on it. Free projects pause after ~7 quiet days and the cron is well inside that.

> Verify at the dashboard before clicking: the prod org's Projects list should show
> **`njobhhdopwdodvzosrns` only**. If `tnnhhnzglyfqolxdojyb` is still listed there,
> the move did not complete and the invoice will be ~$35, not ~$25.

---

### Superseded: the original single-org recommendation (~$35)

_Kept for the reasoning only. Both projects are no longer in one org._

**Upgrade the one organization that holds both projects, and keep both projects in
it.** There is then no "order" to get right — it is a single switch on the org's
billing page and both projects become Pro at the same moment.

| Line item | Amount |
|---|---|
| Pro plan (per organization) | $25.00 |
| Compute — prod project, default size | ~$10.00 |
| Compute — dev project, default size | ~$10.00 |
| Compute Credits (included with any paid plan) | −$10.00 |
| **Expected monthly total** | **~$35.00 + tax** |

That assumes both projects stay on the default compute size and all usage stays
inside the Pro quotas — which, at ~15 users and 94 MB of storage, it will by a
factor of roughly 100. Plan fees are charged **upfront**; compute and usage are
charged **in arrears**.

### If they turn out to be in two separate organizations

Then a single $25 does **not** cover both, and there is a real ordering decision:

1. **Upgrade the prod org first** (~$25 + $10 − $10 = **~$25/month**). Prod gets
   backups, no pausing, and the Pro session controls.
2. **Then deal with dev.** Two options:
   - **Transfer the dev project into the prod org**, then it is covered by the same
     $25 plan and only adds its ~$10 compute → back to ~$35/month total. Transfers
     are self-serve from the project's General settings. Requirements: you must own
     the source org and be a member of the target org, the project must have **no
     GitHub integration connected** and **no log drains**. Billing splits at the
     cycle boundary — the source org pays through the current cycle, the target org
     starts next cycle.
   - **Leave dev on Free** (~$25/month total). ✅ **THIS IS THE CHOSEN PATH
     (2026-09-15).** The cost is that **dev cannot have the Inactivity timeout**,
     so #743 can only be configured and verified on prod. Dev auto-pausing is
     *not* a cost — the daily keepalive cron already covers it.

> ⚠️ Do **not** try to keep prod Pro and dev Free inside one org. Supabase will not
> allow it, and the only way to get there is a project transfer to a second org.

### What NOT to buy while you are in there

Pro unlocks several add-ons that are per-project and billed on top. None are needed:

- **PITR** — ~$100/month for 7-day retention, and it requires at least a **Small**
  compute add-on. The included 7-day daily backups are the right level for a 41 MB
  database. Skip.
- **Read replicas, IPv4, Log drains, Advanced MFA** — no current need.
- **Custom domain** — the prod custom domain (`finance.alumni.byu.edu`) is a Vercel
  domain in front of the app. That is a different product. Do not buy Supabase's
  custom-domain add-on thinking it is the same thing.

---

## 2. What changes the moment Pro is active

**Nothing is destructive.** A plan change is a billing-level change on the
organization. No Supabase doc describes a plan upgrade as touching project data,
and the reverse operation (downgrade) is described purely in terms of credits and
feature access. Databases, storage objects, Auth users, project refs, API keys, JWT
secret and connection strings are not part of the change. §4 still has you verify
the keys by hand rather than trusting that.

| | Free (today) | Pro (after) |
|---|---|---|
| Project auto-pause | after ~1 week of low activity | **never** — paid projects cannot be auto-paused |
| Daily backups | none | **7 days** of daily backups, downloadable |
| Log retention | 1 day | **7 days** |
| Auth session controls | unavailable | **Time-box sessions / Inactivity timeout / Single session per user** |
| Egress quota | 5 GB uncached / 5 GB cached | **250 GB / 250 GB**, then $0.09 / $0.03 per GB |
| File storage | 1 GB | **100 GB**, then ~$0.021 per GB |
| Database | 500 MB per project (hard size limit) | **8 GB disk per project** included, then $0.125 per GB |
| Monthly Active Users | 50,000 | 100,000 |
| Storage Image Transformations | unavailable | available (100 included) |
| Support | community | email support |
| Over-quota behaviour | service **restrictions** | a **bill** (or restriction, if the spend cap is left on) |

That last row is the substantive reason this upgrade matters more than the headroom
does: on Free an overage degrades or stops the site until the cycle resets; on Pro
it is a small charge and nothing breaks.

> ⚠️ **Upgrading does not, by itself, change any behaviour a user would notice.**
> The Inactivity timeout is *available* on Pro — it is not *on*. Sessions behave
> exactly as they do today until someone sets a value (see §5).

---

## 3. Pre-upgrade checklist

Work top to bottom. Items 1–3 are the ones that cost money or data if skipped.

1. **Confirm the prod org holds `njobhhdopwdodvzosrns` and NOT
   `tnnhhnzglyfqolxdojyb`.** Jake moved dev to its own org on/before 2026-09-15, so
   the expected bill is **~$25**. If dev is still listed in the prod org the move
   did not complete and upgrading bills ~$35. Read the list, don't assume.
2. **Read back the current compute size of both projects** (Project Settings →
   Compute and Disk). Free projects run on **Nano**. Write down what each says —
   §6 explains why this matters and it is invisible after the fact.
3. **Take a manual logical backup of prod.** Free has no downloadable backups at
   all, so right now there is no restore point that predates the change.
   `supabase db dump` (read-only, does not touch data). Do this even though the
   upgrade is non-destructive — it is the cheapest insurance available and it stops
   being free the moment something goes wrong.
4. **Screenshot the org Usage page** (egress uncached + cached, storage size,
   database size, MAU) as a "before" record, so the first Pro invoice can be sanity
   checked against something. Remember the storage figure is a cycle average.
5. **Check whether the org is currently in a Fair Use grace period.** The last
   measurement had uncached egress at 6.37 GB against a 5 GB Free quota, so this is
   plausible. A grace-period notice persists in the dashboard even after usage drops
   — do not panic if it is still visible after upgrading; it clears after several
   clean cycles.
6. **Confirm dev is not currently paused.** A paused project has to be *resumed*,
   which is a different flow from upgrading, and the restore window is 90 days.
7. **Get the billing address and Tax ID right BEFORE the first invoice.** Supabase
   is rolling out US sales tax and VAT/GST, and **invoices cannot be regenerated
   after the fact** — a wrong address bills tax that cannot be re-issued. If BYU is
   tax-exempt, the exemption certificate goes to `tax-documents@supabase.io` and
   must be accepted *before* billing starts.
8. **Confirm the payment method is one the department will keep alive.** An overdue
   invoice **pauses every project in the org and downgrades it to Free**. An expired
   card on a system holding real alumni PII is an outage waiting to happen.
9. **Decide the spend-cap posture before clicking.** Pro defaults the Spend Cap
   **on**, which means over-quota usage is blocked rather than billed. At this scale
   nothing will approach a quota, so leaving it **on** is the right default — it
   protects against a bug or an attack, not against normal use. Note that **Compute
   is not covered by the spend cap** either way.
10. **Leave the keepalive cron alone.** Auto-pause disappears for prod once its org
    is Pro, but **dev stays Free and still pauses after ~7 quiet days** — the cron
    is now load-bearing for dev. Do not remove it as "no longer needed"; the prod
    half simply becomes a harmless health ping.

---

## 4. Post-upgrade verification

Walk this in order. It ends in "confirmed" — do not call the upgrade done before
the last line.

1. **Org billing page reads "Pro"**, and the prod ref `njobhhdopwdodvzosrns` is
   listed under it. `tnnhhnzglyfqolxdojyb` (dev) should **not** be — it stays Free
   in its own org, and its presence here means an extra ~$10/month.
2. **Both projects are ACTIVE_HEALTHY** in the dashboard project list.
3. **The app is up on both environments.** `curl <prod-api>/health` returns 200 and
   reports `environment: production`; same for dev. ⚠️ A 200 on `/health` proves the
   API answers, not that anything else works — do step 4 too.
4. **Sign in on prod and on dev, and load the alumni list with photos.** Login and
   headshots are the two paths that actually exercise Auth and Storage.
5. **Keys and URLs are unchanged.** Compare the project ref, `SUPABASE_URL`, the
   publishable/anon key, the service-role key and the JWT secret against the values
   set in the four Vercel projects. If they match, **no redeploy is needed** — the
   upgrade requires no repo change, no env change and no migration.
6. **Database → Backups shows daily backups** on prod. The first backup may take up
   to ~24h to appear; check again the next day rather than concluding it is broken.
7. **Authentication → Sessions now shows the Pro controls** (Time-box user sessions,
   Inactivity timeout, Single session per user) — on **both** projects. They will be
   present but unset. That is expected; setting them is #743, not this issue.
8. **Logs retention now covers 7 days** (Logs → any source; scan back past 24h).
9. **Settings → Compute and Disk on both projects** — read the size back and compare
   against what you wrote down in §3.2. If either still says **Nano**, see §6.
10. **Cost Control reads as decided** in §3.9 (Spend Cap on, unless deliberately off).
11. **The Usage page shows Pro quotas** — 250 GB egress, 100 GB storage, 8 GB disk —
    rather than the Free numbers.
12. **The Upcoming Invoice line items match §1's expectation** (~$35: plan + two
    computes − credits). If it shows something materially different, stop and work
    out why before the cycle closes.

When 1–12 all pass: **confirmed**.

---

## 5. What this unblocks, and the next step on each

| Unblocked | Next step |
|---|---|
| **fa-web-app #743** — inactivity logout via Supabase's own Inactivity timeout | Auth → Sessions → set a positive **Inactivity timeout**, on **BOTH** projects. Duration still undecided; the thing it replaces was 24h. Leave **Single session per user OFF** — #147 already enforces that in code. ⚠️ The real timeout is the configured value **plus** the JWT expiry (~1h), and expired sessions are only cleaned up ~24h later; do not pick a value where that slop matters. |
| **Daily backups** — trigger #1 in the capacity note | Confirm they appear (§4.6), then decide whether the 7-day window is enough for a system holding real alumni PII. It is, at 41 MB. Do **not** buy PITR. |
| **Auto-pause is gone** | The keepalive-cron work becomes redundant for any project inside the Pro org. Coordinate before it ships — and if dev is deliberately left on Free, keepalive is still needed **for dev only**. |
| **7-day log retention** | `docs/SECURITY-MONITORING.md` currently scans Auth logs for the last 24h because that is all Free retains. The weekly routine can now actually cover the week. Worth a follow-up edit to that runbook. |
| **Overage means a bill, not a restriction** | Nothing to do. This is the behavioural change that mattered. |
| **Storage Image Transformations become available** | ⚠️ **Do not build thumbnails as cost avoidance** — that was only ever justified by staying off a paid plan. If it is ever done, the justification is page speed (a 4 MB list view), and only if Jake asks. |
| **Email support** | Available if a platform-level problem ever needs escalating. |

---

## 6. What could surprise us

- **The Nano compute trap.** Free projects run on **Nano**. On a paid plan you
  cannot *launch* Nano — but an existing Nano instance is **not auto-upgraded**, and
  it is **billed at the Micro price anyway**. So a project can sit on the smaller
  instance while paying the larger price, indefinitely, with nothing in the UI
  shouting about it. Supabase's own guidance is to move it to Micro "when
  convenient" — the reason it is not automatic is that the change **incurs
  downtime**. Decide this deliberately (§3.2 / §4.9), and schedule the restart if
  you take it.
- **Compute is not covered by the Spend Cap.** Neither is branching compute, read
  replicas, IPv4, custom domains or PITR. The spend cap protects egress, storage,
  MAU, function invocations and logs — not the predictable, opted-into things. So
  "spend cap on" is not a hard ceiling on the invoice.
- **Every new project is another ~$10/month.** The Free plan's two-project cap
  disappears, but a staging project stops being free. Compute Credits cover exactly
  one project.
- **The plan is org-wide and cannot be mixed.** There is no way to have prod on Pro
  and dev on Free in the same organization.
- **Downgrading later returns credits, not money.** Unused credits are not
  refundable, and Supabase does not issue refunds. If the wrong org gets upgraded by
  mistake, the fix is a support ticket to move the credits, not a reversal.
- **An overdue invoice pauses everything.** Payments are USD and may appear as a
  Singapore charge — a card that blocks foreign transactions will fail.
- **The grace-period warning is sticky.** It stays visible after the grace period
  ends even once usage is back under limits, and only clears after several clean
  cycles. It is a warning that the next overage gets no grace, not a live problem.
- **Tax is being rolled out.** See §3.7 — invoices cannot be regenerated.
- **Nothing in either repo changes.** No env var, no connection string, no
  migration, no redeploy. If a check or a person says otherwise, they are wrong —
  but §4.5 verifies it rather than assuming.
- **Upgrading changes no user-visible behaviour on its own.** In particular, sessions
  keep working exactly as they do now. The whole reason #743 exists as a separate
  item is that someone has to go set the value, per project, by hand.

### Marked UNCONFIRMED

These could not be settled from documentation or from the repo, and were not
checked in the dashboard (out of scope for this task):

1. **Whether both projects live in the same organization.** Decides ~$35 vs ~$25 +
   a transfer decision. Recorded as one org on 2026-08-19; **verify**.
2. **The current compute size of each project** (Nano vs Micro).
3. **Whether the org is currently under a Fair Use grace period or restriction** —
   plausible given 6.37 GB of uncached egress against a 5 GB Free quota.
4. **Whether BYU wants a department card or a tax-exempt institutional billing
   setup**, and whether an exemption certificate exists.
5. **How the first invoice prorates** the mid-cycle plan change. Docs say the plan
   fee is charged upfront and usage in arrears; the exact proration of a mid-cycle
   upgrade is not stated.
6. **Whether any Auth or project setting is reset by a plan change.** No Supabase
   doc says any is, and none is expected — §4.5 and §4.7 verify by reading the
   settings back rather than trusting it.

---

## 7. Sources

All Supabase docs, retrieved 2026-09-09 via the Supabase documentation search.

- Organization-based billing, plan-vs-project split, per-plan quota table —
  <https://supabase.com/docs/guides/platform/billing-on-supabase>
- Multi-project billing example, mixing free/paid, cancellation, Fair Use Policy,
  grace periods, payments, tax — <https://supabase.com/docs/guides/platform/billing-faq>
- Compute pricing, Compute Credits, **Nano-on-paid-plan billing and the no-auto-upgrade
  note** — <https://supabase.com/docs/guides/platform/manage-your-usage/compute>
- Egress quotas and per-GB pricing, cached vs uncached —
  <https://supabase.com/docs/guides/platform/manage-your-usage/egress>
- Spend Cap, and exactly which usage items it does and does not cover —
  <https://supabase.com/docs/guides/platform/cost-control>
- Free-project auto-pause, the 7-day rule, 90-day restore window, and that paid
  projects cannot be paused — <https://supabase.com/docs/guides/platform/free-project-pausing>
- Daily backups (7 days on Pro), PITR pricing and its Small-compute requirement —
  <https://supabase.com/docs/guides/platform/backups>
- **Session controls are Pro-and-up**, timeout is enforced on next refresh, ~24h
  cleanup — <https://supabase.com/docs/guides/auth/sessions>
- Project transfer prerequisites and billing split —
  <https://supabase.com/docs/guides/platform/project-transfer>
- Free vs Pro headline limits and the $25 plan fee — <https://supabase.com/pricing>

Project-side sources: `DEPLOYMENT.md` §1 (dev/prod project split and per-environment
config), `docs/SECURITY-MONITORING.md` (log-window dependency), fa-web-api #523,
fa-web-app #743, and the 2026-08-19 capacity measurement.
