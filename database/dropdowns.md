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
