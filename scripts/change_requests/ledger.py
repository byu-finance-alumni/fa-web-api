"""The ``.imported.json`` ledger: what has already been turned into a request.

The source ``.msg`` files stay in ``inbox-msg/`` after import. That is a choice:
the original is the only complete record of what actually arrived, and moving
it would make the request file's ``Source file:`` pointer a lie the first time
somebody re-ran a command. Keeping it means ``import`` must be safe to run
twice, which is what this ledger is for.

Dedupe is keyed on the SHA-256 of the file's BYTES, not on its filename. Jake
drags files out of Outlook; the same email saved twice gets ``(1)`` appended to
the name and would otherwise import as a second request. Content hashing makes
a re-drag a no-op.

The ledger also carries the body hash the validator warns against, and it is
the only place a request's approval state is NOT recorded — that lives in the
Markdown, in a field a human has to type.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
from dataclasses import asdict, dataclass


@dataclass
class LedgerEntry:
    """One imported message."""

    request_id: str
    source_file: str
    source_sha256: str
    body_sha256: str
    imported_at: str
    subject: str


def file_hash(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load(path: pathlib.Path) -> dict[str, LedgerEntry]:
    """Read the ledger. A missing or corrupt ledger reads as empty.

    A corrupt ledger must not stop an import — the worst outcome is a duplicate
    request, which is visible and easy to delete. A crash here would be worse.
    """
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    entries: dict[str, LedgerEntry] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            try:
                entries[key] = LedgerEntry(**value)
            except TypeError:
                continue
    return entries


def save(path: pathlib.Path, entries: dict[str, LedgerEntry]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: asdict(entry) for key, entry in sorted(entries.items())}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def record(
    entries: dict[str, LedgerEntry],
    *,
    source_sha256: str,
    request_id: str,
    source_file: str,
    body_sha256: str,
    subject: str,
    now: dt.datetime | None = None,
) -> LedgerEntry:
    entry = LedgerEntry(
        request_id=request_id,
        source_file=source_file,
        source_sha256=source_sha256,
        body_sha256=body_sha256,
        imported_at=(now or dt.datetime.now()).strftime("%Y-%m-%d %H:%M"),
        subject=subject,
    )
    entries[source_sha256] = entry
    return entry


def body_hash_for(entries: dict[str, LedgerEntry], request_id: str) -> str | None:
    for entry in entries.values():
        if entry.request_id == request_id:
            return entry.body_sha256
    return None
