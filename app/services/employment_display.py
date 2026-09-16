"""The employer DISPLAY value: the company, or the employment status in its
place when there is no company (#536).

One pure function, one rule, used by every read surface that shows an
alumnus's employer — the alumni list (``AlumniListItem.employer_display``), the
profile page (``ProfileRead.employer_display``), the dashboard / geography
result rows and the CSV export's "Current employer" column. Computing it in
each of those places separately is how the list and the export drifted apart
twice before; they all call this instead so the frontend has nothing to derive.

Nothing here touches stored data: ``current_employer`` is still exposed exactly
as stored, and this value sits NEXT to it as a read-only field.
"""

from __future__ import annotations

from app.core.dropdowns import EMPLOYER_FALLBACK_BY_LOWER


def employer_display(company: str | None, employment_status: str | None) -> str | None:
    """What to show in the employer column for one alumnus.

    * A non-blank *company* wins, returned exactly as stored.
    * Otherwise, if *employment_status* is one of
      :data:`app.core.dropdowns.EMPLOYER_FALLBACK_STATUSES` (matched on the
      trimmed, case-folded value), the status's canonical dropdown label.
    * Otherwise ``None`` — an "employed" status with no company is a data gap
      the UI should still show as blank, and an off-list status is not
      something to guess at.
    """
    if company is not None and company.strip():
        return company
    if employment_status is None:
        return None
    return EMPLOYER_FALLBACK_BY_LOWER.get(employment_status.strip().lower())
