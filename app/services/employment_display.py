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

# The one status whose company field is not an employer but a service BRANCH
# (#608): "Air Force" on its own reads as a company, so it is shown as
# "Military/Air Force". Lived in the frontend until #536 moved every display
# rule here; #547 restores it.
_MILITARY = "Military"
_MILITARY_PREFIX = f"{_MILITARY}/"


def employer_display(company: str | None, employment_status: str | None) -> str | None:
    """What to show in the employer column for one alumnus.

    * A non-blank *company* wins, returned exactly as stored — except for a
      Military status, where the company holds the branch and is shown as
      ``Military/<branch>`` (#608, #547): the branch is trimmed, a branch that
      is itself "Military" collapses to plain ``Military`` rather than
      "Military/Military", and a value already carrying the prefix is left
      alone.
    * Otherwise, if *employment_status* is one of
      :data:`app.core.dropdowns.EMPLOYER_FALLBACK_STATUSES` (matched on the
      trimmed, case-folded value), the status's canonical dropdown label.
    * Otherwise ``None`` — an "employed" status with no company is a data gap
      the UI should still show as blank, and an off-list status is not
      something to guess at.
    """
    status_key = employment_status.strip().lower() if employment_status is not None else None
    if company is not None and company.strip():
        if status_key != _MILITARY.lower():
            return company
        branch = company.strip()
        if branch.lower() == _MILITARY.lower():
            return _MILITARY
        if branch.lower().startswith(_MILITARY_PREFIX.lower()):
            return branch
        return f"{_MILITARY_PREFIX}{branch}"
    if status_key is None:
        return None
    return EMPLOYER_FALLBACK_BY_LOWER.get(status_key)
