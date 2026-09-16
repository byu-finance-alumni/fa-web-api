"""The approval gate. This is the code that says "no".

``request validate <ID>`` is the only thing standing between "an email arrived"
and "an assistant started editing the codebase". Everything it reports is a
REFUSAL, not a warning, with exactly one exception (a changed body hash), and
that asymmetry is deliberate: a validator that mostly warns gets skimmed, and a
gate that gets skimmed is not a gate.

The refusals fall into three groups:

**Did a human actually approve this?**
    The file is physically in ``approved/``; ``Status:`` is exactly
    ``Approved``; ``Approved for Claude:`` is exactly ``Yes``; Acceptance
    Criteria has been written. Every one of those is a thing Jake has to do by
    hand. None of them can be produced by parsing an email.

**Is the file still shaped the way import shaped it?**
    Sentinels present, balanced, ordered and unnested. Fence closed, and closed
    by a fence at least as long as the one that opened it. No unescaped HTML
    comment anywhere but the two sentinel lines. No bidi or zero-width
    characters. The ``Request ID`` agrees with the filename. Each of these is a
    way a body could have escaped its container, or a file could have been
    hand-edited into ambiguity.

**Are the machine-read keys unambiguous?**
    Each key appears exactly once, and outside the quarantined region. A second
    ``Status:`` line — or one inside the quoted email — makes "what is the
    status of this request" a question with two answers, and the safe answer to
    that question is to refuse.

⚠️ If you are tempted to downgrade one of these to a warning to get something
moving: the failure mode this file exists to prevent is a request that Jake
never approved being implemented as though he had.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field

from . import ledger, paths, render, sanitize


@dataclass
class Result:
    """The verdict, itemised. ``ok`` is true only when ``failures`` is empty."""

    request_id: str
    path: pathlib.Path | None
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def render(self) -> str:
        lines: list[str] = []
        if self.failures:
            lines.append(f"REFUSED: {self.request_id} is not ready for implementation.")
            for number, failure in enumerate(self.failures, start=1):
                lines.append(f"  {number}. {failure}")
        else:
            lines.append(f"OK: {self.request_id} is approved and structurally sound.")
        for warning in self.warnings:
            lines.append(f"  warning: {warning}")
        return "\n".join(lines)


def find_request(request_id: str, *, root: pathlib.Path) -> pathlib.Path | None:
    """Locate a request file anywhere in the lifecycle folders."""
    for folder_name in ("approved", "ready", "parked", "completed", "rejected"):
        folder = root / folder_name
        if not folder.is_dir():
            continue
        for candidate in sorted(folder.glob(f"{request_id}-*.md")):
            return candidate
    return None


def validate_file(
    path: pathlib.Path,
    *,
    request_id: str,
    root: pathlib.Path,
    entries: dict[str, ledger.LedgerEntry] | None = None,
) -> Result:
    """Every check, run to completion so the report is a list and not a race."""
    result = Result(request_id=request_id, path=path)
    text = path.read_text(encoding="utf-8")

    # --- 1. did a human put this in the approved folder? ---------------------
    approved_dir = (root / "approved").resolve()
    if path.resolve().parent != approved_dir:
        result.failures.append(
            f"the file is in '{path.parent.name}/', not 'approved/' — only a request Jake "
            f"has moved into approved/ may be implemented"
        )

    # --- 2. structure: sentinels, fence, comments, invisible characters ------
    sentinel_issues = render.sentinel_problems(text)
    result.failures.extend(sentinel_issues)
    result.failures.extend(render.fence_problems(text))
    result.failures.extend(render.stray_html_comments(text))

    invisible = sanitize.find_invisible(text)
    if invisible:
        result.failures.append(
            "the file contains bidi/zero-width control characters "
            f"({', '.join(invisible)}) — it can render as something other than what it says"
        )

    # --- 3. machine-read keys: exactly once, and outside the quarantine ------
    header_region = render.split_header_region(text)
    region = render.untrusted_region_text(text)

    status_values = render.STATUS_KEY.findall(header_region)
    approved_values = render.APPROVED_KEY.findall(header_region)
    id_values = render.REQUEST_ID_KEY.findall(header_region)

    for label, values in (
        ("Status:", status_values),
        ("Approved for Claude:", approved_values),
        ("Request ID:", id_values),
    ):
        if len(values) > 1:
            result.failures.append(
                f"'{label}' appears {len(values)} times outside the quoted email — "
                "exactly one is required or the file has two answers"
            )
        elif not values and not sentinel_issues:
            result.failures.append(f"'{label}' is missing")

    for label, pattern in render.IN_REGION_KEYS:
        if region and pattern.search(region):
            result.failures.append(
                f"'{label}' appears INSIDE the quoted email body — that is the sender "
                "naming a field we read, not a field. Delete it from the quote if the "
                "request is otherwise genuine"
            )

    status = status_values[0].strip() if len(status_values) == 1 else None
    if status is not None and status != render.STATUS_APPROVED:
        result.failures.append(
            f"Status is '{status}', not exactly 'Approved'"
        )

    approved = approved_values[0].strip() if len(approved_values) == 1 else None
    if approved is not None and approved != "Yes":
        result.failures.append(
            f"'Approved for Claude' is '{approved}', not exactly 'Yes'"
        )

    # --- 4. the request id must agree with the filename ---------------------
    match = render.FILENAME_RE.match(path.name)
    if not match:
        result.failures.append(
            f"the filename '{path.name}' is not CR-YYYY-NNN-short-title.md"
        )
    elif len(id_values) == 1 and id_values[0].strip() != match.group(1):
        result.failures.append(
            f"the Request ID field says '{id_values[0].strip()}' but the filename says "
            f"'{match.group(1)}'"
        )

    # --- 5. Jake has to have written the acceptance criteria -----------------
    criteria = render.section(text, "Acceptance Criteria")
    if not criteria:
        result.failures.append("Acceptance Criteria is empty")
    elif criteria.strip() == render.PLACEHOLDER:
        result.failures.append(
            "Acceptance Criteria still holds the import placeholder "
            f"('{render.PLACEHOLDER}') — nobody has said what 'done' means"
        )

    # --- 6. flagged requests need an explicit sign-off ----------------------
    flags_match = render.INJECTION_FLAGS_KEY.search(header_region)
    flags = int(flags_match.group(1)) if flags_match else 0
    if flags > 0:
        reviewed = render.REVIEWED_KEY.findall(header_region)
        signed = any(value.strip() == "Yes" for value in reviewed)
        if not signed:
            result.failures.append(
                f"Injection Flags: {flags} but Security Review has no 'Reviewed: Yes' "
                "sign-off — somebody has to say they read the flagged text"
            )

    # --- 7. body hash: a WARNING, because editing the quote is legitimate ----
    entries = ledger.load(paths.ledger_path(root=root)) if entries is None else entries
    recorded = ledger.body_hash_for(entries, request_id)
    body = render.extract_body(text)
    if recorded and body is not None and render.body_hash(body) != recorded:
        result.warnings.append(
            "the quoted email body no longer matches what was imported — expected if "
            "Jake trimmed it, worth a glance if he did not"
        )

    return result


def validate(request_id: str, *, root: pathlib.Path | None = None) -> Result:
    root = root or paths.home()
    if not render.REQUEST_ID_RE.match(request_id):
        return Result(
            request_id=request_id,
            path=None,
            failures=[f"'{request_id}' is not a request id (expected CR-YYYY-NNN)"],
        )
    path = find_request(request_id, root=root)
    if path is None:
        return Result(
            request_id=request_id,
            path=None,
            failures=[f"no request file named {request_id}-*.md exists under {root}"],
        )
    return validate_file(path, request_id=request_id, root=root)
