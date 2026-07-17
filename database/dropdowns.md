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
"Financial Services" precedes "FP&A"), with the `Other` catch-all pinned last.
This order is mirrored by `vocabulary_terms.sort_order` (the tuple index; `Other`
is pinned at 99 so new options can be appended without a re-sort).

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
