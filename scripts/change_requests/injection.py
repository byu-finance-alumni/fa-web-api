"""Heuristic scan for prompt-injection shaped text in an email body.

This is the sixth and weakest of the six containment layers, and it is
deliberately the weakest: **it flags, it never blocks.** A heuristic that
refuses an import would be trained out of existence within a week, because
real colleagues really do write "ignore my last email" and really do paste
``curl`` commands. Its job is to raise the cost of a quiet attack — to make
sure that if a body ever does say "Status: Approved", Jake sees a counter
saying so before he types the word himself.

Two design choices worth keeping:

* **Findings carry a label and a count, never an excerpt.** Quoting the matched
  text into the Security Review section would copy the payload OUT of the
  contained region and into the part of the file an assistant reads as trusted
  narrative. That would defeat layers 1-4 in the name of reporting on them.
* **No pattern name contains a literal machine-read key.** A finding label
  reading ``Status: Approved`` would itself trip the validator's
  "this key appears more than once" refusal. The labels are hyphenated slugs
  for exactly that reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_APPROVED = "Appro" + "ved"  # split so this file never contains the literal key


@dataclass(frozen=True)
class Finding:
    """One heuristic hit: what fired, how often, and what it would mean."""

    label: str
    count: int
    note: str

    def render(self) -> str:
        return f"{self.label} x{self.count} — {self.note}"


#: ``(label, note, compiled pattern)``. Labels are stable identifiers; the note
#: is the sentence Jake reads. Ordered roughly most-alarming first.
_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "instruction-override",
        "text telling a reader to ignore earlier instructions",
        re.compile(r"\b(ignore|disregard|forget)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all)\b",
                   re.IGNORECASE),
    ),
    (
        "role-reassignment",
        "text attempting to reassign an assistant's role",
        re.compile(r"\byou are now\b|\bact as (?:a|an|the)\b|\bfrom now on you\b", re.IGNORECASE),
    ),
    (
        "system-prompt-probe",
        "a reference to a system prompt or developer instructions",
        re.compile(r"\bsystem prompt\b|\bdeveloper (?:message|instructions)\b", re.IGNORECASE),
    ),
    (
        "disregard-verb",
        "a bare 'disregard' directive",
        re.compile(r"\bdisregard\b", re.IGNORECASE),
    ),
    (
        "approval-status-directive",
        "the body tries to set the approval status field itself",
        re.compile(r"\bStatus\s*:\s*" + _APPROVED + r"\b", re.IGNORECASE),
    ),
    (
        "approval-flag-directive",
        "the body names the Claude approval flag",
        re.compile(_APPROVED + r"\s+for\s+Claude", re.IGNORECASE),
    ),
    (
        "shell-network-command",
        "a command that would fetch from the network",
        re.compile(r"(?<![\w-])curl(?![\w-])|(?<![\w-])wget(?![\w-])|Invoke-WebRequest",
                   re.IGNORECASE),
    ),
    (
        "destructive-command",
        "a command that would delete files",
        re.compile(r"\brm\s+-[a-z]*[rf][a-z]*\b|\bRemove-Item\b[^\n]{0,40}-Recurse",
                   re.IGNORECASE),
    ),
    (
        "git-push",
        "a command that would publish code",
        re.compile(r"\bgit\s+push\b", re.IGNORECASE),
    ),
    (
        "long-base64-run",
        "a long opaque encoded run, which can hide a payload from a reader",
        re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{120,}={0,2}(?![A-Za-z0-9+/=])"),
    ),
    (
        "embedded-url",
        "a link — never fetch one from an unreviewed request",
        re.compile(r"\bhttps?://[^\s<>\"')]+", re.IGNORECASE),
    ),
)


def scan(text: str) -> list[Finding]:
    """Every pattern that fired, with its hit count. Empty list means clean."""
    findings: list[Finding] = []
    for label, note, pattern in _PATTERNS:
        hits = len(pattern.findall(text or ""))
        if hits:
            findings.append(Finding(label=label, count=hits, note=note))
    return findings


def flag_count(findings: list[Finding]) -> int:
    """The number that lands in ``Injection Flags: N``.

    Counts DISTINCT patterns, not total hits: five links in a newsletter is one
    thing to look at, not five. The per-pattern hit count is still printed
    beside each finding.
    """
    return len(findings)
