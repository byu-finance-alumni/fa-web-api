"""Schemas for the stored-object maintenance jobs (currently the headshot sweep).

The sweep runs unattended on a cron, so this summary is the ONLY visibility
anyone has into it. Every field exists to answer a question an operator will
actually ask: did it do anything, did it break anything, and how many nights are
left before the backlog is gone.
"""

from __future__ import annotations

from pydantic import BaseModel


class HeadshotSweepSummary(BaseModel):
    """What one sweep run did.

    The counts partition the run: ``scanned`` is every object the LISTING
    returned (metadata only, no bytes), and of those ``eligible`` were over the
    size threshold. ``processed`` is how many of the eligible ones this run
    actually downloaded — the rest are ``remaining`` and wait for tomorrow.
    Every processed object lands in exactly one of ``normalised``,
    ``skipped_no_gain``, ``skipped_unreadable`` or ``failed``.
    """

    scanned: int = 0
    eligible: int = 0
    processed: int = 0

    # Rewritten in place: decoded, re-encoded, and strictly smaller than before.
    normalised: int = 0
    # Under the size threshold, so never downloaded at all.
    skipped_small: int = 0
    # Re-encoding produced a file no smaller than the original. Writing it would
    # spend a generation of quality and buy nothing.
    skipped_no_gain: int = 0
    # Pillow could not decode it. LEFT EXACTLY AS IT WAS — a sweep over existing
    # data must never destroy something it merely failed to parse.
    skipped_unreadable: int = 0
    # A storage round-trip failed (download or upload). Also left untouched.
    failed: int = 0

    # Only the objects this run rewrote are counted here, so the number is
    # literally "quota freed tonight".
    bytes_before: int = 0
    bytes_after: int = 0
    bytes_reclaimed: int = 0

    # Eligible objects the per-run bounds stopped us reaching. Non-zero means
    # "run again tomorrow"; a stable non-zero value across nights means the
    # remainder is stuck (unreadable or no-gain) and needs a human.
    remaining: int = 0

    # Whether the run stopped because it hit its wall-clock budget rather than
    # its object cap. Persistently true means the cap is never the real bound.
    stopped_on_time_budget: bool = False
    duration_seconds: float = 0.0
