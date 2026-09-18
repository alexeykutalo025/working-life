"""RFC 5322 messages: .eml files and mbox archives.

The stdlib does the parsing, which is the right call - ``email`` has had thirty
years of malformed input thrown at it. What this module adds is everything the
stdlib deliberately leaves alone:

* **Header decoding that does not give up.** ``=?iso-8859-1?Q?...?=`` is
  handled by the stdlib; a header that is raw cp1252 bytes with no encoding
  marker at all is not, and that is what a 1997 mailer wrote.
* **Dates that are not quite dates.** ``Date: Tue, 3 Nov 97 14:22:00 +0000``
  has a two-digit year. ``parsedate_to_datetime`` raises on several real
  forms, and a message with an unparseable date keeps the raw string and goes
  to the Undated bucket rather than being dropped.
* **Bodies chosen rather than concatenated.** A multipart/alternative has the
  same message twice; taking both would double every body in the archive.
"""

from __future__ import annotations

import email
import email.policy
import mailbox
import re
from datetime import datetime, timezone
from email.header import decode_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from typing import Iterator

from ..logging_setup import get_logger
from ..models import Kind, ParsedAttachment, ParsedIdentity, ParsedItem, Role, TimePoint
from ..normalize.text import clean_text, html_to_text
from .base import Parser, register

log = get_logger("parsers.eml")

#: Headers kept verbatim in raw_headers. Everything is kept, actually - this is
#: the order they are written in.
_HEADER_ORDER = (
    "Date", "From", "To", "Cc", "Bcc", "Subject", "Message-ID", "In-Reply-To",
    "References", "Reply-To", "Sender", "Return-Path", "Received",
)

_ROLE_FOR_HEADER = {
    "from": Role.FROM,
    "to": Role.TO,
    "cc": Role.CC,
    "bcc": Role.BCC,
}


@register
class EmlParser(Parser):
    """One message per file."""

    extensions = frozenset({".eml"})
    produces = frozenset({Kind.MESSAGE})
    name = "eml"

    def parse(self, kinds: frozenset[str] | None = None) -> Iterator[ParsedItem]:
        if not self._wants(kinds, Kind.MESSAGE):
            return

        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            self.outcome.error = f"could not be opened: {exc}"
            self.outcome.error_detail = repr(exc)
            return

        if not raw.strip():
            self.outcome.error = "the file is empty"
            self.outcome.claimed_count = 0
            return

        self.outcome.claimed_count = 1
        try:
            message = email.message_from_bytes(raw, policy=email.policy.default)
        except Exception as exc:  # noqa: BLE001 - fall back to the compat policy
            log.debug("%s needed the compatibility parser: %s", self.path, exc)
            try:
                message = email.message_from_bytes(raw)
            except Exception as exc2:  # noqa: BLE001
                self.outcome.error = f"the message could not be read: {exc2}"
                self.outcome.error_detail = repr(exc2)
                self.outcome.add_finding(
                    "read_failure", "critical",
                    f"{self.path.name} could not be read as an email message",
                    f"Exact error: {exc2}", {"path": str(self.path)},
                )
                return

        item = build_message(message, backend=self.name)
        self.outcome.yielded_count = 1
        self.outcome.items_scanned = 1
        yield item


@register
class MboxParser(Parser):
    """A Unix mbox: many messages in one file, separated by From_ lines."""

    extensions = frozenset({".mbox"})
    produces = frozenset({Kind.MESSAGE})
    name = "mbox"

    def parse(self, kinds: frozenset[str] | None = None) -> Iterator[ParsedItem]:
        if not self._wants(kinds, Kind.MESSAGE):
            return

        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            self.outcome.error = f"could not be opened: {exc}"
            self.outcome.error_detail = repr(exc)
            return

        # A From_ line at the start of a line is the separator. Counting them
        # gives the store's own idea of how much it holds, which is what makes
        # estimated_loss a computed number.
        self.outcome.claimed_count = len(
            re.findall(rb"^From \S+.*$", raw, flags=re.MULTILINE)
        )

        produced = 0
        try:
            box = mailbox.mbox(str(self.path), create=False)
        except Exception as exc:  # noqa: BLE001
            self.outcome.error = f"the mailbox could not be opened: {exc}"
            self.outcome.error_detail = repr(exc)
            self.outcome.add_finding(
                "read_failure", "critical",
                f"{self.path.name} could not be opened as a mailbox",
                f"Exact error: {exc}", {"path": str(self.path)},
            )
            return

        try:
            for key in box.keys():
                if self._limit_reached(produced):
                    return
                try:
                    message = box[key]
                except Exception as exc:  # noqa: BLE001 - one message, not the file
                    self.outcome.add_finding(
                        "read_failure", "critical",
                        f"{self.path.name}: a message could not be read",
                        f"Position: {key}\n"
                        f"Last message read successfully: {produced}\n"
                        f"Exact error: {exc.__class__.__name__}: {exc}",
                        {
                            "path": str(self.path), "offset": produced,
                            "error": f"{exc.__class__.__name__}: {exc}",
                        },
                    )
                    continue

                try:
                    item = build_message(message, backend=self.name)
                except Exception as exc:  # noqa: BLE001
                    log.debug("A message in %s could not be built: %s", self.path, exc)
                    continue

                produced += 1
                self.outcome.yielded_count = produced
                self.outcome.items_scanned = produced
                self.outcome.last_good_offset = produced
                yield item
        finally:
            try:
                box.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Turning one email.Message into a ParsedItem
# ---------------------------------------------------------------------------


def build_message(
    message: Message, *, backend: str = "eml", folder_path: str | None = None
) -> ParsedItem:
    """One RFC 5322 message to a ParsedItem. Never raises on bad input."""
    item = ParsedItem(kind=Kind.MESSAGE, backend=backend, folder_path=folder_path)

    confidence = 1.0

    subject, subject_confidence = decode_header_value(message.get("Subject"))
    item.subject = subject
    confidence = min(confidence, subject_confidence)

    item.internet_message_id = _clean_id(message.get("Message-ID"))
    item.in_reply_to = _clean_id(message.get("In-Reply-To"))
    item.references = _reference_ids(message.get("References"))
    item.conversation_topic = subject

    item.occurred = _date(message, item)
    item.importance = _importance(message)
    item.sensitivity = (message.get("Sensitivity") or "").strip().lower() or None
    item.raw_headers = _raw_headers(message)

    for header, role in _ROLE_FOR_HEADER.items():
        for identity in _identities(message, header, role):
            item.participants.append(identity)

    body_text, body_html, body_confidence = _bodies(message)
    item.body_html = body_html
    if body_text:
        item.body_text = body_text
        item.body_format = "plain"
    elif body_html:
        item.body_text = html_to_text(body_html)
        item.body_format = "html"
    confidence = min(confidence, body_confidence)

    item.attachments = _attachments(message)

    item.parse_confidence = confidence
    if confidence < 1.0:
        item.note(
            "low_confidence_text",
            "This message did not say what character set it was written in, so "
            f"Recall worked it out from the bytes (confidence {confidence:.0%}). "
            "Accented characters and quotation marks may be wrong.",
        )

    return item


def decode_header_value(raw) -> tuple[str | None, float]:
    """A header to text, with how sure we are of it.

    Handles RFC 2047 words, and the far more common case of a header that is
    just raw 8-bit bytes with nothing declared at all.
    """
    if raw is None:
        return None, 1.0

    if isinstance(raw, bytes):
        decoded = clean_text(raw)
        return decoded.text.strip() or None, decoded.confidence

    text = str(raw)
    if "=?" not in text:
        # No encoded words. If it is pure ASCII it is certain; if it contains
        # high characters the stdlib already decoded it as something.
        decoded = clean_text(text)
        return decoded.text.strip() or None, decoded.confidence

    parts: list[str] = []
    confidence = 1.0
    try:
        for value, charset in decode_header(text):
            if isinstance(value, bytes):
                decoded = clean_text(value, charset)
                parts.append(decoded.text)
                confidence = min(confidence, decoded.confidence)
            else:
                parts.append(str(value))
    except Exception as exc:  # noqa: BLE001 - a malformed encoded word
        log.debug("A header could not be decoded (%s); kept as written", exc)
        return text.strip() or None, 0.7

    joined = "".join(parts).strip()
    return (joined or None), confidence


def _clean_id(raw) -> str | None:
    """A Message-ID, without its angle brackets or surrounding noise."""
    if not raw:
        return None
    text = str(raw).strip()
    match = re.search(r"<([^>]+)>", text)
    if match:
        return match.group(1).strip()
    return text.strip("<> \t\r\n") or None


def _reference_ids(raw) -> list[str]:
    if not raw:
        return []
    return [m.group(1).strip() for m in re.finditer(r"<([^>]+)>", str(raw))]


def _date(message: Message, item: ParsedItem) -> TimePoint:
    """The Date header, or the Undated bucket. Never a guess."""
    raw = message.get("Date")
    if not raw:
        item.note(
            "no_date",
            "This message has no Date header at all, so there is nothing to "
            "place it by. It is kept in the Undated list, and no date has been "
            "invented for it.",
        )
        return TimePoint.unknown()

    text = str(raw).strip()
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError) as exc:
        log.debug("Unparseable Date %r: %s", text, exc)
        dt = _salvage_date(text)
        if dt is None:
            item.note(
                "no_date",
                f"This message's date could not be understood. The header said:\n"
                f"    Date: {text}\n"
                "It is kept in the Undated list with that line preserved, rather "
                "than being given a date it does not have.",
            )
            return TimePoint.unknown()

    if dt is None:
        return TimePoint.unknown()

    if dt.tzinfo is None:
        # A date with no offset. The wall clock is known, the zone is not.
        item.note(
            "unknown_timezone",
            f"This message's date gives no timezone:\n    Date: {text}\n"
            "The date is right; the exact moment is uncertain, and no timezone "
            "has been assumed.",
        )
        return TimePoint.from_naive(dt)

    point = TimePoint.from_aware(dt)
    year = int(point.utc[:4]) if point.utc else 0
    if year < 1970 or year > datetime.now(timezone.utc).year + 1:
        item.note(
            "implausible_date",
            f"This message is dated {point.utc[:10]}, which cannot be right. "
            "The date has been kept exactly as the message gives it, and not "
            "corrected.",
        )
    return point


def _salvage_date(text: str) -> datetime | None:
    """A last attempt at a date the stdlib refused.

    Two-digit years and missing weekdays are the common cases in 1990s mail.
    Nothing here invents a value: if no pattern matches, the message is undated.
    """
    cleaned = re.sub(r"\([^)]*\)", "", text).strip()
    formats = (
        "%a, %d %b %y %H:%M:%S %z",
        "%d %b %y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S",
        "%d %b %Y %H:%M:%S",
        "%a %b %d %H:%M:%S %Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    )
    for fmt in formats:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


def _importance(message: Message) -> str | None:
    for header in ("Importance", "X-Priority", "Priority", "X-MSMail-Priority"):
        raw = message.get(header)
        if not raw:
            continue
        text = str(raw).strip().lower()
        if text.startswith(("1", "2")) or "high" in text or "urgent" in text:
            return "high"
        if text.startswith(("4", "5")) or "low" in text or "non-urgent" in text:
            return "low"
        if text.startswith("3") or "normal" in text:
            return "normal"
    return None


def _identities(message: Message, header: str, role: str) -> list[ParsedIdentity]:
    """Every address on one header, with its display name."""
    values = message.get_all(header)
    if not values:
        return []

    out: list[ParsedIdentity] = []
    seen: set[str] = set()

    decoded_values = []
    for value in values:
        text, _ = decode_header_value(value)
        if text:
            decoded_values.append(text)

    for display, address in getaddresses(decoded_values):
        display = (display or "").strip().strip('"')
        address = (address or "").strip()
        if not address and not display:
            continue
        key = f"{address.lower()}|{display.lower()}"
        if key in seen:
            continue
        seen.add(key)

        out.append(
            ParsedIdentity(
                address=address or None,
                address_type="smtp" if "@" in address else ("none" if not address else "ex"),
                display_name=display or None,
                role=role,
            )
        )
    return out


def _bodies(message: Message) -> tuple[str | None, str | None, float]:
    """The plain and HTML bodies, chosen rather than concatenated.

    A multipart/alternative holds the same message twice. Walking every part
    and joining them would double the body of every HTML message in the
    archive, which would then break dedup as well as being wrong.
    """
    plain: str | None = None
    html: str | None = None
    confidence = 1.0

    if not message.is_multipart():
        text, conf = _part_text(message)
        confidence = min(confidence, conf)
        if _content_type(message) == "text/html":
            return None, text, confidence
        return text, None, confidence

    for part in message.walk():
        if part.is_multipart():
            continue
        content_type = _content_type(part)
        disposition = str(part.get("Content-Disposition") or "").lower()
        if "attachment" in disposition:
            continue

        if content_type == "text/plain" and plain is None:
            plain, conf = _part_text(part)
            confidence = min(confidence, conf)
        elif content_type == "text/html" and html is None:
            html, conf = _part_text(part)
            confidence = min(confidence, conf)

        if plain is not None and html is not None:
            break

    return plain, html, confidence


def _content_type(part: Message) -> str:
    try:
        return (part.get_content_type() or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def _part_text(part: Message) -> tuple[str | None, float]:
    try:
        payload = part.get_payload(decode=True)
    except Exception as exc:  # noqa: BLE001
        log.debug("A message part could not be decoded: %s", exc)
        return None, 0.5

    if payload is None:
        try:
            raw = part.get_payload()
            return (str(raw).strip() or None), 0.9
        except Exception:  # noqa: BLE001
            return None, 0.5

    charset = part.get_content_charset()
    decoded = clean_text(payload, charset)
    return (decoded.text.strip() or None), decoded.confidence


def _attachments(message: Message) -> list[ParsedAttachment]:
    if not message.is_multipart():
        return []

    out: list[ParsedAttachment] = []
    for part in message.walk():
        if part.is_multipart():
            continue

        disposition = str(part.get("Content-Disposition") or "")
        filename = part.get_filename()
        if filename:
            filename, _ = decode_header_value(filename)

        content_id = (part.get("Content-ID") or "").strip().strip("<>") or None
        is_inline = "inline" in disposition.lower() or bool(content_id)
        content_type = _content_type(part)

        # A text part with no filename is the body, not an attachment.
        if not filename and not content_id:
            if content_type in ("text/plain", "text/html") or content_type.startswith("multipart"):
                continue

        data: bytes | None = None
        read_error: str | None = None
        try:
            data = part.get_payload(decode=True)
        except Exception as exc:  # noqa: BLE001
            read_error = f"{exc.__class__.__name__}: {exc}"

        if data is None and read_error is None:
            read_error = "the attachment had no readable content"

        out.append(
            ParsedAttachment(
                filename=filename,
                mime_type=content_type or None,
                size_bytes=len(data) if data else None,
                data=data,
                is_inline=is_inline,
                content_id=content_id,
                read_error=read_error,
            )
        )
    return out


def _raw_headers(message: Message) -> str:
    """Every header, in a stable order, for the disclosure in the item viewer."""
    lines: list[str] = []
    seen: set[str] = set()

    for name in _HEADER_ORDER:
        for value in message.get_all(name) or []:
            lines.append(f"{name}: {value}")
            seen.add(name.lower())

    for name, value in message.items():
        if name.lower() in seen:
            continue
        lines.append(f"{name}: {value}")

    return "\n".join(lines)
