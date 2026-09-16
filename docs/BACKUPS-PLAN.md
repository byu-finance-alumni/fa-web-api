# Our Own Backups — Build Plan

_Written 2026-09-15 for api #535. Phases 1, 2, 4 and 5 are built (status lines
below); Phase 3 is not. Originally: nothing built, the plan to work from
tomorrow. No credential was read, no dump was taken, no dashboard setting was
touched._

---

## Why we need this at all (the one-paragraph version)

Supabase's Pro backups are **database only**. Their docs say it outright: *"Database
backups do not include objects you store via the Storage API, as the database only
includes metadata about these objects."* The **583 headshots (~94 MB) have no backup
on any plan**. On top of that, Pro keeps only **7 days**, the backups are
**restore-in-place from their dashboard** rather than an artifact we hold, and
**deleting a project destroys its backups with it**. Upgrading to Pro does not close
any of that.

---

## 1. What actually has to be captured

Three things, and the third is the one everybody forgets.

| # | What | Size | Notes |
|---|---|---|---|
| 1 | **Postgres data** — `public` schema: alumni, employment, interactions, survey tables, audit/engineer logs | ~41 MB | The core asset |
| 2 | **Storage bucket** — `headshots` | ~94 MB / 583 objects | **Not in any Supabase backup.** The whole reason for this ticket |
| 3 | **`auth` schema** — the staff user accounts | tiny | ⚠️ If a restore omits this, **every staff login is gone** and all the `created_by` / actor-email FERPA trails point at users who no longer exist |

⚠️ **The database and the bucket must be captured as a matched pair.** `storage.objects`
rows are metadata; the files live in S3. Restore a database without the matching
files and the app renders broken images for photos it believes exist. Stamp both
halves of a run with the same timestamp and keep them together.

---

## 2. The decision that blocks everything: where do backups land?

A dump is the **single most sensitive artifact in this project** — every alumnus,
with contact details, in one file. The destination is a FERPA question before it is
a storage question. **This is Jake's call and it gates the rest.**

| Option | Cost | Verdict |
|---|---|---|
| **A. Local — Jake's machine / a BYU network drive** | $0 | ✅ **Recommended to start.** Data never leaves BYU-controlled hardware, there is no new vendor, no PII in CI, and no approval to chase. We already run a local Windows scheduled task for the change-request intake (`scripts/change-requests-*.ps1`) — the exact same shape. Cost: only runs when the machine is on |
| **B. Backblaze B2 / AWS S3** (object-lock + lifecycle) | pennies (135 MB) | Good *later*: immutable, automatable, real retention. But it is a **new place alumni PII lives** and needs approval first |
| **C. GitHub Actions artifacts** | free | ❌ **No.** Retention caps out at 90 days and it parks a full PII dump in GitHub. Fine for a transient check, never for the backup of record |
| **D. A second Supabase project / bucket** | ~$0 | ❌ Same blast radius. Does nothing for "the org lapsed" or "the project was deleted", which are the scenarios we are insuring against |

**Recommendation: build A first, design so B can be bolted on later.** The script
should write to a directory given by an env var so the destination is configuration,
not code.

Still needed from Jake either way: **cadence** (weekly looks right — this data moves
slowly), **retention** (e.g. 8 weeklies + 12 monthlies), and **who may read the
backups** — which should be *narrower* than app access, not wider.

---

## 3. Build order

Phased so that **the first hour produces a real backup**, rather than a pipeline that
produces one eventually.

### Phase 1 — a script that works by hand ⭐ start here

`scripts/backup_prod.py` (or `.ps1` to match the change-request tooling), run
manually, producing a single timestamped folder:

```
backups/2026-09-16T0900Z/
  database.dump          # pg_dump custom format
  auth.sql               # auth schema
  storage/headshots/...  # every object
  MANIFEST.json          # sizes, counts, checksums, project ref, duration
```

Deliberately **no scheduling and no secrets plumbing yet.** If this step works once,
by hand, we have a backup we did not have this morning.

### Phase 2 — verification that a bad backup fails loudly

A dump that is truncated, empty, or pointed at the wrong project still *exits 0*.
Before it is trusted, every run must assert:

- `pg_restore --list database.dump` parses and lists the expected tables
- the alumni row count is **≥ 1,400** (a floor, not an exact figure)
- the object count and total bytes match what the bucket reports
- ⚠️ **assert the project ref before dumping** — the keepalive workflow already does
  exactly this, because base URLs in this project are easy to get backwards. Copy
  that guard verbatim. A "backup" of dev would be worse than no backup, because it
  would look fine
- failures post to the existing **security/error Slack channel**, not just a log

### Phase 3 — the restore rehearsal (do not skip)

A backup nobody has restored is a guess. Restore into a **throwaway Supabase
project** and diff row counts against prod.

⚠️ **Expect noisy errors and do not read them as failure.** Supabase's own docs
say `object already exists` and `constraint ... already exists` are **normal** when
restoring a full dump into a fresh project, because the project ships with `auth`
and `storage` schemas already created. Write down what a *successful* restore looks
like, or the first real emergency will be spent guessing.

⚠️ **Restoring the database does not restore the photos.** The bucket has to be
re-uploaded separately, and the runbook must say so in the same breath.

### Phase 4 — schedule it

_Status: **built 2026-09-16, on dev.** `scripts/backup-install-task.ps1` +
`scripts/backup-scheduled.ps1`; Sunday 03:00 local, DPAPI-stored secrets,
`--incremental` bucket capture, prod ref pinned as a literal in the wrapper.
See docs/BACKUPS.md "Weekly automatic backups". Phase 3 has NOT been done._

Local scheduled task, mirroring `scripts/change-requests-install-task.ps1`. Weekly.
Only once phases 1–3 pass by hand.

### Phase 5 — retention and pruning

_Status: **built 2026-09-16, on dev.** `backup_prod.py --keep N` (the task uses 8),
runs only after an OK run, deletes only OK run folders, never the newest, never
FAILED or manifest-less ones, refuses a drive root or the repo. Tested against a
directory of dummy folders as this section asks. Failures: `LAST-RUN-FAILED.txt`
+ optional Slack line._

Prune on a documented rule. ⚠️ Write the pruning step **last** and test it against a
directory of dummy files — a rotation bug deletes backups, which is the one failure
mode worse than not having them.

---

## 4. Technical traps to design around

Known before we start, mostly from this project's own history:

- ⚠️ **Dump through the SESSION pooler (`:5432`), not the transaction pooler
  (`:6543`).** `pg_dump` needs session-level features the transaction pooler does not
  provide. Migrations already use `:5432` for this reason.
- ⚠️ **`pg_dump` client version must match the server major version**, or it refuses
  with a version-mismatch error. Pin it explicitly; do not rely on whatever the
  machine has.
- ⚠️ **If this ever runs in GitHub Actions, it inherits the apt trap** — the runner's
  third-party apt sources took down the migrate job, and **that fix is still on dev,
  not promoted to prod**. Another argument for phase 1 being local.
- **Use the S3-compatible endpoint for the bucket**, not per-file API downloads.
  Supabase's docs recommend `aws s3 sync` / rclone against
  `https://<project-ref>.supabase.co/storage/v1/s3` for bulk, and it is dramatically
  faster for 583 objects. ⚠️ This needs an **S3 access key + secret generated in
  Storage → Settings → S3** — a credential that **does not exist yet**; Jake creates
  it, and it is **shown only once**.
- ⚠️ **Custom-role passwords are not in Supabase's daily backups** either. Our own
  logical dump should capture roles (`--role-only`) if we want a clean rebuild.
- **Encrypt if it ever leaves BYU hardware** (age or gpg, key held by Jake). Under
  option A this is optional; under B it is mandatory.

---

## 5. Credentials — who holds what

The job needs the **prod database URL** and a **prod S3 key pair**.

⚠️ **Claude does not get prod credentials.** Jake supplies them the same way as the
Slack webhook: as environment variables for a local run, or repo secrets if this ever
moves to CI. The script reads them from the environment and **must never log or echo
them** — `gitleaks` scans history, so a single careless `echo` is permanent.

---

## 6. What to decide before writing code tomorrow

1. **Destination** — option A, or wait for approval on B? (blocks everything)
2. **Cadence and retention** — weekly + monthlies?
3. **Who may read the backups?**
4. **Does the `auth` schema go in?** (recommend yes — without it a restore has no staff logins)

Everything else in this document can proceed on the recommendations above.
