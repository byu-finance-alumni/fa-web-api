"""Attachment handling: bytes to disk and a hash, and nothing else.

The rule this module exists to enforce is short: **never open, parse, render or
interpret an attachment.** We do not sniff its type, we do not unzip it, we do
not read an embedded ``.msg``, we do not resolve a link inside it. We write the
allowed ones to disk verbatim, we refuse to write the dangerous ones at all,
and we record a SHA-256 of every one either way so a later question about what
arrived has an answer.

Three verdicts:

``BLOCKED``
    Executable or script-like. **Never written to disk.** Name, size and hash
    are still recorded — the point is that the request file says an
    ``.exe`` arrived, not that it silently vanished.
``FLAGGED``
    Written, but carries macros or is an archive. Archives are NEVER
    auto-extracted; a zip is a directory traversal and a zip bomb waiting for
    someone to be helpful.
``ALLOWED``
    Written, and still requires manual review. "Allowed" here means "we are
    willing to store these bytes", not "these bytes are safe".

Filename handling is the other half. An attachment filename is attacker
controlled and is about to become a path. Directory components are stripped,
the result is slugified, Windows reserved device names are rewritten, and the
resolved path is asserted to be inside the target folder before a single byte
is written.
"""

from __future__ import annotations

import hashlib
import pathlib
import re
from dataclasses import dataclass, field

from . import sanitize

BLOCKED = "BLOCKED"
FLAGGED = "FLAGGED"
ALLOWED = "ALLOWED"

#: Extensions that Windows will happily execute, or that execute something on
#: their behalf. These are never written to disk.
BLOCKED_EXTENSIONS = frozenset(
    {
        ".exe", ".com", ".scr", ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe",
        ".js", ".jse", ".wsf", ".wsh", ".hta", ".msi", ".msp", ".cpl", ".dll",
        ".lnk", ".reg", ".jar", ".iso", ".img", ".vhd", ".scf", ".url", ".chm",
        ".pif", ".application", ".gadget", ".msc", ".inf",
    }
)

#: Written, but marked: macro-enabled Office documents and archives.
FLAGGED_EXTENSIONS = frozenset(
    {
        ".docm", ".xlsm", ".pptm", ".xlsb", ".dotm", ".xltm",
        ".zip", ".7z", ".rar",
    }
)

ARCHIVE_EXTENSIONS = frozenset({".zip", ".7z", ".rar"})

#: Device names that are still special on Windows regardless of extension.
WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


@dataclass
class RawAttachment:
    """What the ``.msg`` reader hands over: a claimed name and some bytes."""

    name: str
    data: bytes


@dataclass
class AttachmentRecord:
    """What we are willing to say about an attachment afterwards."""

    original_name: str
    extension: str
    size: int
    sha256: str
    verdict: str
    reason: str
    stored_name: str | None = None
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        """One line for the Security Review section."""
        where = self.stored_name or "not written to disk"
        extra = f" [{'; '.join(self.notes)}]" if self.notes else ""
        return (
            f"{self.original_name} ({self.size} bytes, {self.verdict}: {self.reason}) "
            f"-> {where} sha256={self.sha256}{extra}"
        )


def _basename(name: str) -> str:
    """Strip every directory component, on either path flavour, plus a drive."""
    candidate = (name or "").replace("\\", "/")
    candidate = candidate.split("/")[-1]
    candidate = _DRIVE_PREFIX.sub("", candidate)
    # ``..`` and ``.`` are not filenames.
    return "" if candidate.strip(". ") == "" else candidate.strip()


def extension_of(name: str) -> str:
    base = _basename(name)
    suffix = pathlib.PurePosixPath(base).suffix.lower()
    return suffix


def classify(name: str) -> tuple[str, str]:
    """Verdict and human reason for a claimed filename."""
    ext = extension_of(name)
    if ext in BLOCKED_EXTENSIONS:
        return BLOCKED, f"executable or script extension {ext}"
    if ext in ARCHIVE_EXTENSIONS:
        return FLAGGED, f"archive {ext} — never auto-extracted"
    if ext in FLAGGED_EXTENSIONS:
        return FLAGGED, f"macro-enabled or active content {ext}"
    if not ext:
        return ALLOWED, "no extension — manual review required"
    return ALLOWED, f"{ext} — manual review required"


def safe_filename(name: str, *, fallback: str = "attachment") -> str:
    """Attacker-controlled name -> something safe to join onto a path.

    Keeps the extension (it is what the verdict was decided on, so the stored
    name must not disagree with the record) but slugifies the stem and rewrites
    Windows device names.
    """
    base = _basename(name) or fallback
    ext = pathlib.PurePosixPath(base).suffix
    stem = base[: len(base) - len(ext)] if ext else base

    ext = sanitize.slugify(ext, max_len=12, default="")
    ext = f".{ext}" if ext else ""
    stem = sanitize.slugify(stem, max_len=60, default=fallback)

    if stem.upper() in WINDOWS_RESERVED:
        stem = f"{stem}-file"
    return f"{stem}{ext}"


def _unique_path(folder: pathlib.Path, filename: str) -> pathlib.Path:
    """De-collide with a numeric suffix rather than overwriting."""
    candidate = folder / filename
    if not candidate.exists():
        return candidate
    stem = pathlib.PurePosixPath(filename).stem
    ext = pathlib.PurePosixPath(filename).suffix
    for n in range(2, 1000):
        candidate = folder / f"{stem}-{n}{ext}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not find a free filename for {filename!r} in {folder}")


def _assert_inside(folder: pathlib.Path, target: pathlib.Path) -> None:
    """Traversal defence. The resolved path must be inside the target folder.

    ``safe_filename`` already makes this unreachable; the assertion is here so
    that a future change to ``safe_filename`` fails loudly instead of writing
    outside the sandbox.
    """
    resolved_folder = folder.resolve()
    resolved_target = target.resolve()
    if resolved_folder not in resolved_target.parents:
        raise ValueError(
            f"refusing to write outside the attachment folder: {resolved_target} "
            f"is not inside {resolved_folder}"
        )


def store(
    request_id: str,
    raws: list[RawAttachment],
    *,
    attachments_root: pathlib.Path,
    write: bool = True,
) -> list[AttachmentRecord]:
    """Record every attachment; write the ones that are allowed to be written.

    ``write=False`` gives a dry run that still classifies and hashes.
    """
    records: list[AttachmentRecord] = []
    folder = attachments_root / request_id

    for index, raw in enumerate(raws):
        original = sanitize.clean_field(raw.name or f"attachment-{index + 1}", max_len=180)
        data = raw.data or b""
        verdict, reason = classify(raw.name or "")
        record = AttachmentRecord(
            original_name=original or f"attachment-{index + 1}",
            extension=extension_of(raw.name or ""),
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            verdict=verdict,
            reason=reason,
        )

        if verdict == BLOCKED:
            record.notes.append("refused: bytes were never written to disk")
            records.append(record)
            continue

        if verdict == FLAGGED and record.extension in ARCHIVE_EXTENSIONS:
            record.notes.append("archive: not extracted, open only in a sandbox")
        elif verdict == FLAGGED:
            record.notes.append("may contain macros: do not enable content")

        if extension_of(raw.name or "") == ".msg":
            record.notes.append("nested message: recorded, not parsed")

        filename = safe_filename(raw.name or "", fallback=f"attachment-{index + 1}")
        if write:
            folder.mkdir(parents=True, exist_ok=True)
            target = _unique_path(folder, filename)
            _assert_inside(folder, target)
            target.write_bytes(data)
            record.stored_name = f"{request_id}/{target.name}"
        else:
            record.stored_name = f"{request_id}/{filename}"
        records.append(record)

    return records
