# Dropdown Option Lists

These are the canonical option lists for the application's dropdown / multi-select
fields. They are **intentionally NOT enforced in the database** — the underlying
columns are plain `varchar` (or `varchar` rows in a child table), so values stay
flexible and can be changed without a migration.

**This file is the single source of truth for the options.** The frontend should
render dropdowns from these lists, and the backend should validate against them
(application-level validation, not a DB constraint). When an option changes, edit
it here and update the frontend/backend lists — no schema change required.

Use exact, case-sensitive strings when storing values so filtering/grouping stays
consistent.

---

## Industries

Used by:
- `current_employment.current_industry` (Current Industry — **primary**)
- `current_employment.current_industry_secondary` (Current Industry 2 — **secondary**)
- `employment_history.employment_industry` (Former Company Industry)

> **This list is machine-checked.** `tests/test_industry_vocab.py` parses the
> bullets below and fails CI if they drift from `app/core/dropdowns.py`
> `INDUSTRIES` or from the `vocabulary_terms` rows seeded by
> `migrations/*.sql`. Change all three together — order included.

Listed in **dropdown order**: alphabetical, compared case-insensitively (so
"Financial Services" precedes "FP&A"), with `Unknown`, `Graduate Student` and the
`Other` catch-all pinned last (in that order). This order is mirrored by
`vocabulary_terms.sort_order` (the tuple index for the alphabetical body;
`Unknown` is pinned at 97, `Graduate Student` at 98 and `Other` at 99 so new
options can be appended without a re-sort).

Options:
- Asset Management
- Commercial Banking
- Consulting
- Corporate Banking *(secondary only)*
- Corporate Finance
- Credit Risk *(secondary only)*
- Equity Research
- Financial Services
- FP&A
- Investment Banking
- Law *(secondary only)*
- Military
- Private Banking
- Private Credit
- Private Equity
- Real Estate
- Sales
- Sales and Trading *(secondary only)*
- Valuation & Advisory
- Venture Capital
- Wealth Management
- Unknown
- Graduate Student
- Other

### Primary vs secondary (#282)

Requested by Tanya, 2026-07-16. The four options marked *(secondary only)* are
**not offered as an alumnus's primary industry** — they aren't dashboard
industries — but they remain fully valid as a **secondary** industry.

**Do not delete them, and do not "restore" them to the primary dropdown.**
Primary and secondary render from the same vocabulary, so deleting them would
remove them from secondary too, which is the opposite of what was asked. The
split is visibility-only and is applied in three places:

- `app/core/dropdowns.py` — `PRIMARY_INDUSTRIES` / `SECONDARY_INDUSTRIES`.
- `GET /vocabulary/industry?scope=primary` — the payload the primary dropdown
  renders from. The default (`scope=all`) still returns the full list.
- Note `Sales and Trading` is stored with the word "and", **not** "Sales &
  Trading".

`validate_industry()` still **accepts all of the above for either field.** That
is deliberate, and matches the soft-delete semantics elsewhere in the vocabulary
("a value still on existing records stays valid, it just disappears from
new-entry dropdowns"): the #282 data migration deliberately skips records that
already had a secondary industry, so some records legitimately keep one of the
four as their primary and must still save without a 422.

`FP&A` is likewise not a dashboard industry, but Tanya did **not** ask to remove
it from primary — leave it in primary unless she says otherwise.

### Graduate Student (#294)

`Graduate Student` was added so alumni currently in graduate school stop landing
in `Other`. It is a **valid primary industry** (selectable as someone's current
industry) but is **not** a dashboard wheel slice — it is in `_NON_WHEEL_INDUSTRIES`
like the other non-wheel values. Unlike them, the dashboard surfaces it as its
own clickable indicator at the **bottom** of the industry breakdown (separate
from the `Other` fold), linking to the alumni list filtered to
`current_industry = "Graduate Student"`. It is pinned in the dropdown just above
`Other` (`sort_order` 98).

### Unknown (#295)

`Unknown` is **distinct from a blank / unset** current industry. A blank value
means "not yet collected" — we simply haven't gathered it. `Unknown` means "we
checked and it is genuinely unknown", so staff can record that explicitly instead
of leaving the field empty.

It is a **valid primary industry** (selectable and filterable, NOT in
`_PRIMARY_EXCLUDED_INDUSTRIES`) but is **not** a dashboard wheel slice — it is in
`_NON_WHEEL_INDUSTRIES` and simply **folds into `Other`** on the wheel. Unlike
`Graduate Student` it gets **no** separate dashboard indicator. It is pinned in
the dropdown just above `Graduate Student` (`sort_order` 97).

### Military (#608)

`Military` was added 2026-08-04 because a service member had **no industry that
fit** and had to be recorded as `Other` or `Unknown`. (Nothing ever *blocked* a
Military alumnus from having an employer and title; that part of #608 was a data
gap, not a bug.)

**Valid as EITHER primary or secondary.** Jake's reservist case is the reason it
lives here rather than only in Employment Status: someone can serve *and* hold a
civilian job — primary `Investment Banking`, secondary `Military`.
`employment_status` is a single value and cannot express that; industry has two
slots.

**Searching `industry=Military` matches BOTH slots.** This is a deliberate,
**Military-only** widening of the 2026-08-03 rule (#584) that `industry` means
the primary industry only — without it, every reservist is missed by the search,
which is most of the people the option was added for. Every other industry stays
primary-only; do not generalize it. `tests/test_alumni_search.py` pins both
halves.

**On the dashboard it folds into `Other` — it gets NO bar of its own.** Jake:
keep the industry chart about finance sectors. It is in `_NON_WHEEL_INDUSTRIES`,
and folding into `Other` is that set's default behaviour, so there is
deliberately no special case for it in the breakdown code (contrast
`Graduate Student`, which *is* special-cased into its own bar). The
`industry_group=other` drill-down therefore includes it, keeping the bar and its
list in sync.

Unlike the three pinned tail options it sits in the **alphabetical body**, between
`Law` and `Private Banking` (`sort_order` 11) — it is an ordinary answer to "what
do you do", and people scan for it under M. Adding it therefore shifted
`Private Banking`..`Wealth Management` from 11..19 to 12..20, which is why
`migrations/2026-08-04_add_military_industry.sql` re-upserts the whole body.

`Military` the **industry** is independent of `Military` the **employment status**
below — they are separate columns and neither is derived from the other, and the
migration does no backfill between them. The staff create/edit forms do *suggest*
the industry when the status is set to Military, but it is only ever a
suggestion the user can ignore or change (see below).

---

## Employment Status

Used by: `alumni.employment_status` (person-level: what the alumnus is currently
doing — **not** an employment-history row).

> **This list is machine-checked.** `tests/test_employment_status_vocab.py`
> parses the bullets below and fails CI if they drift from `app/core/dropdowns.py`
> `EMPLOYMENT_STATUSES`. Change both together — order included.

Listed in **dropdown order**, which is Tanya's (#568), *not* alphabetical, with
`Unknown` pinned last (#377). Unlike Industries this is **not** a
`vocabulary_terms` category — there is no `/vocabulary/employment_status`
endpoint and no seed migration; the list lives in code on both sides.

Options:
- Full-time
- Part-time
- Self-Employed
- Graduate Student
- Military
- Not in the Labor Force
- Unemployed
- Unknown *(not offered in the survey)*

### Not enforced on write

The column is a plain `varchar(50)` and the backend validates only its **length**.
That is deliberate: prod holds off-list legacy values (`Employed`, `Stay at home
parent`, …), and an allow-list would make those records 422 the moment someone
edits an unrelated field. There is no `validate_employment_status()`. The staff
dropdowns re-add whatever is on file via `withValue()`, so editing a profile can
never silently rewrite its stored status.

### Unknown (#377)

`Unknown` means **"we asked and we don't know"** — a recorded non-answer, distinct
from a blank/unset status ("not yet collected"). It arrived on ~65 prod alumni via
the free-text intake sheet (as `unkown` / `UNKOWN`) and was consolidated onto the
literal `Unknown` in Jake's 2026-08-04 prod cleanup, so the app has to treat it as
a first-class value: selectable, filterable, importable, exportable.

**It is excluded from the survey.** "Unknown" is meaningless as a
self-description, and offering it back to an alum just re-collects the non-answer
the survey exists to clear. The survey renders from
`SURVEY_EMPLOYMENT_STATUSES` (`SURVEY_EMPLOYMENT_STATUS_OPTIONS` in the
frontend) = this list minus the placeholders, and it displays a *stored*
`Unknown` as blank (#572) so the alum is prompted for a real answer without the
untouched value being overwritten.

### Statuses where a blank employer is complete data (#608)

`EMPLOYER_NOT_APPLICABLE_STATUSES` in `app/core/dropdowns.py` — the statuses for
which the **"missing employer" hygiene flag is suppressed**, because an employer
is inapplicable or optional and nobody should be chasing one:

- Military
- Unemployed
- Not in the Labor Force
- Graduate Student

`Military` is on the list on Jake's call, 2026-08-04: *"the branch does not
matter."* We still **want** the branch when we know it — it is stored in the
ordinary `current_employer` field and the profile renders it as
`Military/<branch>` — but it is not required and not chased, so a serving alumnus
with no employer must not count as missing one or as an incomplete profile.

One constant drives all three surfaces (the per-record create/edit warning, the
dashboard + Data-quality counts including `complete_alumni`, and the
`?missing_employer=1` drill-down and its export) so they always describe the same
population. Matching is case-insensitive on the trimmed value, because the column
has no write validation and casing drifts through imports.

**Not exempt:** `Self-Employed` (their own company is the employer and we want
its name), `Full-time` / `Part-time`, and `Unknown` (we do not know what they are
doing, so we cannot claim the blank employer was intentional — that *is* the gap
the flag exists to surface). Widening past these four is a decision for Jake, not
a judgement call in code: every status added silently removes people from the
only worklist that would fix them.

### Military status suggests the Military industry (#608)

Status and industry are independent columns, so someone can be `Military` by
status with no industry recorded and then never appear in an industry search for
Military. The staff **create** and **profile-edit** forms close that gap by
*suggesting* the industry when the status is set to Military:

- **Suggest, never force.** The user can clear it or pick something else and that
  choice sticks; nothing is written behind their back on save, and the suggestion
  never fires twice over a value the user has since changed.
- **Never clobbers.** An empty primary industry gets the suggestion. If the
  primary is already filled (e.g. `Investment Banking`) the suggestion goes to the
  **secondary** slot instead — the reservist case. If both slots hold values,
  nothing is suggested.
- **One-way.** Switching the status away from Military does not remove a Military
  industry the user chose.
- **Staff forms only.** The **CSV import** and the **survey** are deliberately
  excluded: a bulk file and an alum's own self-report are taken at face value, not
  silently amended.

---

## Mentor Industries

Used by: `alumni_mentor_industries.industry` (multi-select — one row per industry).

Exactly the **Industries** list above (all of it — the primary/secondary split
does not apply here), plus one extra option:
- Law/Government

The list is intentionally not duplicated here; it mirrors `MENTOR_INDUSTRIES`
in `app/core/dropdowns.py`, which is defined as `(*INDUSTRIES, "Law/Government")`.

---

## Finance Conferences

Used by: `conference_participation.conference`.

Options:
- Corporate Finance Conference
- Financial Services Conference
- Real Estate Conference

---

## Finance Society Leadership Roles

Used by: `finance_society_leadership.leadership_role`.

Options:
- Finance Society President
- PBWMA
- BREA
- CFAA
- IBA
- PEVC
- WIFA

---

## Graduate Degree

Used by: `alumni.graduate_degree`.

Options (open-ended — "etc." in the requirements; extend as needed):
- Medical
- Law
- MBA
- Other

---

## Other Engagement Willingness

These are stored as individual boolean flags on `alumni_program_engagement`
rather than a dropdown, but listed here for reference:

| Option (label)                         | Column                            |
|----------------------------------------|-----------------------------------|
| Guest speaker in a class               | `guest_speaker_willing`           |
| Help at an event                       | `help_at_event_willing`           |
| Willing to host a NetTrek              | `nettrek_host_willing`            |
| Willing to participate in a conference | `finance_conference_willing`      |
| Willing to mentor a finance student    | `mentor_willing`                  |
| Company sponsor for a Finance event    | `company_event_sponsor_willing`   |
| Case Competition host                  | `case_competition_host_willing`   |
| Women in Finance mentor                | `women_in_finance_mentor_willing` |

---

## Designations

Stored as boolean flags on `alumni_program_engagement` (not a dropdown):
- `cfp_designation` — CFP
- `cfa_designation` — CFA
