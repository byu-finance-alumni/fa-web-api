"""Read an Outlook ``.msg`` file into a plain dataclass.

``extract_msg`` is imported LAZILY, inside :func:`read_msg`, and that is not a
style preference:

* It is a DEV-ONLY dependency. It is declared in ``requirements-dev.txt`` and
  deliberately NOT in ``pyproject.toml``, because Vercel's Python builder
  installs only ``[project.dependencies]`` and CI's ``deploy-deps`` job imports
  ``app.main`` against exactly that list. Shipping a GPLv3 MAPI parser into the
  production function to serve an offline intake script would be wrong on
  licensing, on bundle size, and on blast radius — a bad ``uv.lock``
  regeneration 500s every request.
* A module-level import would make every other command in this package — and
  every test that renders or validates a request — fail on a machine that has
  not installed the dev extras. Only the one function that actually needs the
  parser is allowed to require it.

HTML-only bodies are converted to text locally with the standard library. We
never fetch a URL, never load a remote image, never resolve a link. An email
body is evidence; resolving anything inside it would turn reading a request
into acting on it.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser

from .attachments import RawAttachment

_INSTALL_HINT = (
    "extract_msg is not installed. It is a development-only dependency:\n"
    "    pip install -r requirements-dev.txt\n"
    "It is intentionally absent from pyproject.toml so it never ships to Vercel."
)


class MsgReaderError(RuntimeError):
    """Raised when a ``.msg`` cannot be read. Always actionable."""


@dataclass
class ParsedMessage:
    """Everything we take from a message. Nothing here is trusted."""

    subject: str
    sender_name: str
    sender_email: str
    sent_at: dt.datetime | None
    body: str
    attachments: list[RawAttachment] = field(default_factory=list)
    body_was_html: bool = False


class _TextExtractor(HTMLParser):
    """Minimal, local HTML -> text. Drops script/style, keeps block breaks."""

    _DROP = {"script", "style", "head", "title", "meta", "link"}
    _BREAK = {"p", "br", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._DROP:
            self._skip += 1
        elif tag in self._BREAK:
            self.chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._DROP and self._skip:
            self._skip -= 1
        elif tag in self._BREAK:
            self.chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.chunks.append(data)

    def text(self) -> str:
        joined = "".join(self.chunks)
        return re.sub(r"\n{3,}", "\n\n", joined).strip()


def html_to_text(html: str) -> str:
    """Local conversion only — no network, no rendering engine."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed HTML must not abort an import
        return unescape(re.sub(r"<[^>]+>", " ", html)).strip()
    return parser.text()


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _split_sender(display: str, email: str) -> tuple[str, str]:
    """Outlook stuffs ``Name <addr>`` into whichever field it feels like."""
    display = _as_text(display).strip()
    email = _as_text(email).strip()
    match = re.match(r"^(.*?)\s*<([^<>]+)>\s*$", display)
    if match:
        name = match.group(1).strip().strip('"')
        addr = match.group(2).strip()
        return name or addr, email or addr
    if not email and "@" in display and " " not in display:
        return display, display
    return display, email


def read_msg(path: pathlib.Path) -> ParsedMessage:
    """Parse one ``.msg``. Raises :class:`MsgReaderError` with a clear reason."""
    try:
        import extract_msg  # noqa: PLC0415 - lazy on purpose, see module docstring
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise MsgReaderError(_INSTALL_HINT) from exc

    try:
        message = extract_msg.openMsg(str(path))
    except Exception as exc:  # noqa: BLE001 - any parser failure is one message
        raise MsgReaderError(f"could not read {path.name}: {exc}") from exc

    try:
        subject = _as_text(getattr(message, "subject", ""))
        body = _as_text(getattr(message, "body", ""))
        was_html = False
        if not body.strip():
            raw_html = getattr(message, "htmlBody", None)
            if raw_html:
                body = html_to_text(_as_text(raw_html))
                was_html = True

        name, email = _split_sender(
            getattr(message, "sender", "") or getattr(message, "sentRepresentingName", ""),
            _as_text(getattr(message, "senderEmail", ""))
            or _as_text(getattr(message, "sentRepresentingEmailAddress", "")),
        )

        sent_at = getattr(message, "date", None)
        if not isinstance(sent_at, dt.datetime):
            sent_at = None

        raws: list[RawAttachment] = []
        for att in getattr(message, "attachments", []) or []:
            filename = (
                _as_text(getattr(att, "longFilename", ""))
                or _as_text(getattr(att, "shortFilename", ""))
                or _as_text(getattr(att, "name", ""))
            )
            data = getattr(att, "data", b"")
            if not isinstance(data, (bytes, bytearray)):
                # A nested message comes back as an object, not bytes. We record
                # it by name and hash of nothing rather than recursing into it.
                data = b""
            raws.append(RawAttachment(name=filename, data=bytes(data)))
    finally:
        close = getattr(message, "close", None)
        if callable(close):
            close()

    return ParsedMessage(
        subject=subject,
        sender_name=name,
        sender_email=email,
        sent_at=sent_at,
        body=body,
        attachments=raws,
        body_was_html=was_html,
    )
