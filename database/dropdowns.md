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
- `current_employment.current_industry` (Current Industry)
- `current_employment.current_industry_secondary` (Current Industry 2)
- `employment_history.employment_industry` (Former Company Industry)

Options:
- Asset Management
- Commercial Banking
- Consulting
- Corporate Finance
- Equity Research
- Investment Banking
- Private Banking
- Private Credit
- Private Equity
- Real Estate
- Sales
- Valuation & Advisory
- Venture Capital
- Wealth Management
- Other

---

## Mentor Industries

Used by: `alumni_mentor_industries.industry` (multi-select — one row per industry).

Same list as **Industries** above, plus one extra option:
- Asset Management
- Commercial Banking
- Consulting
- Corporate Finance
- Equity Research
- Investment Banking
- Private Banking
- Private Credit
- Private Equity
- Real Estate
- Sales
- Valuation & Advisory
- Venture Capital
- Wealth Management
- Other
- Law/Government

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
