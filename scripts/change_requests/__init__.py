"""Local change-request intake for the BYU Finance Alumni Database.

Jake drags Outlook ``.msg`` files into a folder; ``request import`` turns each
one into a structured Markdown change request; he reviews and approves it by
hand; only then may Claude Code implement it.

The single most important property of this package is the one that is easiest
to lose in a refactor:

    ⚠️ IMPORTING AN EMAIL MUST NEVER SET AN APPROVAL FIELD.

An email body is data that later becomes text an assistant reads as
instruction. Everything in :mod:`~scripts.change_requests.sanitize`,
:mod:`~scripts.change_requests.injection` and
:mod:`~scripts.change_requests.validate` exists to keep that boundary visible
and enforced. There is deliberately no code path from a parsed message to
``Status: Approved`` or ``Approved for Claude: Yes`` — those two strings are
written as their refusing values at import and can only be changed by a human
editing the file.

Nothing in this package touches the network, the application database, or
production data. ``tests/test_change_request_security.py`` pins that down with
a source-level invariant test.
"""

__all__ = [
    "attachments",
    "injection",
    "ledger",
    "msg_reader",
    "paths",
    "render",
    "sanitize",
    "validate",
    "worklog",
]
