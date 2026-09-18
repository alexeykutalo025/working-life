"""Saved Outlook messages (.msg), via extract-msg.

A .msg is an OLE compound document holding MAPI properties - the same
properties pypff reads out of a PST, in a different container. ``extract-msg``
handles the container; this module maps what it returns onto the same
``ParsedItem`` shape everything else produces, so a message saved as .msg in
2004 and the same message inside a .pst collapse onto one archive entry.

Nested messages matter here. An Outlook .msg can hold another .msg as an
attachment - a forwarded message - and that inner message is often the only
surviving copy of the original. It is extracted as an attachment with its text
recovered, so it is searchable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterator

from ..logging_setup import get_logger
from ..models import Kind, ParsedAttachment, ParsedIdentity, ParsedItem, Role, TimePoint
from ..normalize.text import clean_text, html_to_text, rtf_to_text
from .base import Parser, ParserError, register

log = get_logger("parsers.msg")

#: Outlook message classes to archive kinds, same mapping as the PST reader.
_CLASS_KIND = {
    "IPM.NOTE": Kind.MESSAGE,
    "IPM.APPOINTMENT": Kind.EVENT,
    "IPM.SCHEDULE.MEETING": Kind.EVENT,
    "IPM.CONTACT": Kind.CONTACT,
    "IPM.TASK": Kind.TASK,
    "IPM.STICKYNOTE": Kind.NOTE,
}


@register
class MsgParser(Parser):
    """One saved Outlook message per file."""

    extensions = frozenset({".msg"})
    produces = frozenset({Kind.MESSAGE, Kind.EVENT, Kind.CONTACT, Kind.TASK, Kind.NOTE})
    name = "msg"

    def parse(self, kinds: frozenset[str] | None = None) -> Iterator[ParsedItem]:
        try:
            import extract_msg
        except ImportError as exc:
            self.outcome.error = "the extract-msg library is not installed"
            self.outcome.error_detail = str(exc)
            raise ParserError(
                "Saved Outlook messages (.msg) cannot be read because the "
                "extract-msg library is not installed. Install it with:  "
                "pip install extract-msg"
            ) from exc

        self.outcome.claimed_count = 1

        try:
            message = extract_msg.openMsg(str(self.path))
        except Exception as exc:  # noqa: BLE001 - every failure is reported
            self.outcome.error = f"{exc.__class__.__name__}: {exc}"
            self.outcome.error_detail = repr(exc)
            self.outcome.add_finding(
                "read_failure", "critical",
                f"{self.path.name} could not be opened as an Outlook message",
                "The file looks like a saved Outlook message but could not be "
                f"read.\n\nExact error: {exc.__class__.__name__}: {exc}",
                {"path": str(self.path)},
            )
            return

        try:
            item = self._build(message, kinds)
        except Exception as exc:  # noqa: BLE001
            self.outcome.error = f"{exc.__class__.__name__}: {exc}"
            self.outcome.error_detail = repr(exc)
            self.outcome.add_finding(
                "read_failure", "critical",
                f"{self.path.name} could not be understood",
                f"Exact error: {exc.__class__.__name__}: {exc}",
                {"path": str(self.path)},
            )
            return
        finally:
            try:
                message.close()
            except Exception:  # noqa: BLE001
                pass

        if item is not None:
            self.outcome.yielded_count = 1
            self.outcome.items_scanned = 1
            yield item
        else:
            # The file read fine; it just is not one of the kinds asked for.
            self.outcome.items_scanned = 1

    def _build(self, message, kinds: frozenset[str] | None) -> ParsedItem | None:
        message_class = (_attr(message, "messageClass") or "IPM.Note").upper()
        kind = Kind.MESSAGE
        for prefix, mapped in _CLASS_KIND.items():
            if message_class.startswith(prefix):
                kind = mapped
                break

        if kinds is not None and kind not in kinds:
            return None

        item = ParsedItem(kind=kind, backend=self.name)
        item.subject = _text(_attr(message, "subject"))
        item.conversation_topic = _text(_attr(message, "conversationTopic")) or item.subject
        item.internet_message_id = _clean_id(_attr(message, "messageId"))
        item.in_reply_to = _clean_id(_attr(message, "inReplyTo"))
        item.raw_headers = _text(_attr(message, "headerText")) or _headers_text(message)
        item.importance = _importance(_attr(message, "importance"))

        references = _text(_attr(message, "references"))
        if references:
            import re

            item.references = [m.group(1) for m in re.finditer(r"<([^>]+)>", references)]

        item.occurred = _time(message, item)
        self._bodies(message, item)
        self._participants(message, item, kind)
        self._attachments(message, item)

        if kind == Kind.EVENT:
            item.location = _text(_attr(message, "location"))
            start = _to_timepoint(_attr(message, "startDate"))
            if start.utc:
                item.occurred = start
            end = _to_timepoint(_attr(message, "endDate"))
            if end.utc:
                item.end = end

        if kind == Kind.CONTACT:
            item.contact = {
                "display_name": _text(_attr(message, "displayName")) or item.subject,
                "emails": [
                    e for e in (
                        _text(_attr(message, "email1EmailAddress")),
                        _text(_attr(message, "email2EmailAddress")),
                        _text(_attr(message, "email3EmailAddress")),
                    ) if e
                ],
                "organization": _text(_attr(message, "companyName")),
                "title": _text(_attr(message, "jobTitle")),
                "phones": {
                    "business": _text(_attr(message, "businessTelephoneNumber")),
                    "home": _text(_attr(message, "homeTelephoneNumber")),
                    "mobile": _text(_attr(message, "mobileTelephoneNumber")),
                },
                "notes": item.body_text,
            }

        return item

    def _bodies(self, message, item: ParsedItem) -> None:
        plain = _text(_attr(message, "body"))
        html_body = _attr(message, "htmlBody")
        if isinstance(html_body, bytes):
            decoded = clean_text(html_body)
            html_body = decoded.text
            item.parse_confidence = min(item.parse_confidence, decoded.confidence)
        html_body = _text(html_body)

        rtf_text = None
        if not plain and not html_body:
            rtf = _attr(message, "rtfBody")
            if rtf:
                try:
                    if isinstance(rtf, bytes):
                        rtf = clean_text(rtf).text
                    rtf_text = rtf_to_text(rtf)
                except Exception as exc:  # noqa: BLE001
                    log.debug("RTF body in %s could not be converted: %s", self.path, exc)

        item.body_html = html_body
        if plain:
            item.body_text = plain
            item.body_format = "plain"
        elif html_body:
            item.body_text = html_to_text(html_body)
            item.body_format = "html"
        elif rtf_text:
            item.body_text = rtf_text
            item.body_format = "rtf"

    def _participants(self, message, item: ParsedItem, kind: str) -> None:
        sender_address = _text(_attr(message, "senderEmail")) or _extract_address(
            _text(_attr(message, "sender"))
        )
        sender_name = _text(_attr(message, "senderName")) or _extract_name(
            _text(_attr(message, "sender"))
        )
        if sender_address or sender_name:
            item.participants.append(
                ParsedIdentity(
                    address=sender_address,
                    address_type=_address_type(sender_address),
                    display_name=sender_name,
                    role=Role.ORGANIZER if kind == Kind.EVENT else Role.FROM,
                )
            )

        recipients = _attr(message, "recipients") or []
        for recipient in recipients:
            address = _text(_attr(recipient, "email")) or _extract_address(
                _text(_attr(recipient, "formatted"))
            )
            name = _text(_attr(recipient, "name"))
            if not address and not name:
                continue

            recipient_type = _attr(recipient, "type")
            role = _recipient_role(recipient_type, kind)
            item.participants.append(
                ParsedIdentity(
                    address=address,
                    address_type=_address_type(address),
                    display_name=name,
                    role=role,
                )
            )

        # Some .msg files carry only the display strings.
        if not any(p.role in (Role.TO, Role.CC, Role.ATTENDEE) for p in item.participants):
            for attr_name, role in (("to", Role.TO), ("cc", Role.CC), ("bcc", Role.BCC)):
                text = _text(_attr(message, attr_name))
                if not text:
                    continue
                for piece in [p.strip() for p in text.split(";") if p.strip()]:
                    address = _extract_address(piece)
                    item.participants.append(
                        ParsedIdentity(
                            address=address,
                            address_type=_address_type(address),
                            display_name=_extract_name(piece),
                            role=Role.ATTENDEE if kind == Kind.EVENT else role,
                        )
                    )

    def _attachments(self, message, item: ParsedItem) -> None:
        attachments = _attr(message, "attachments") or []
        for attachment in attachments:
            filename = (
                _text(_attr(attachment, "longFilename"))
                or _text(_attr(attachment, "shortFilename"))
                or _text(_attr(attachment, "name"))
            )
            content_id = _text(_attr(attachment, "cid"))

            data: bytes | None = None
            read_error: str | None = None
            nested_text: str | None = None

            try:
                raw = _attr(attachment, "data")
                if isinstance(raw, bytes):
                    data = raw
                elif raw is not None:
                    # A nested .msg comes back as a message object, not bytes.
                    # It is often the only surviving copy of the original, so
                    # its text is recovered rather than the attachment being
                    # recorded as unreadable.
                    nested_text = _nested_message_text(raw)
                    if nested_text:
                        data = nested_text.encode("utf-8")
                        if not filename:
                            subject = _text(_attr(raw, "subject")) or "message"
                            filename = f"{subject[:80]}.msg.txt"
            except Exception as exc:  # noqa: BLE001
                read_error = f"{exc.__class__.__name__}: {exc}"

            if data is None and read_error is None:
                read_error = "the attachment had no readable content"

            item.attachments.append(
                ParsedAttachment(
                    filename=filename,
                    mime_type=_text(_attr(attachment, "mimetype")),
                    size_bytes=len(data) if data else None,
                    data=data,
                    is_inline=bool(content_id),
                    content_id=content_id,
                    read_error=read_error,
                )
            )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _attr(obj: Any, name: str) -> Any:
    """Read an attribute that may not exist and may raise when it does.

    extract-msg raises for properties a particular message does not carry, and
    the set varies by Outlook version, so every read is guarded.
    """
    try:
        return getattr(obj, name, None)
    except Exception:  # noqa: BLE001
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return clean_text(value).text.strip() or None
    text = str(value).strip()
    return text or None


def _clean_id(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    return text.strip("<> \t\r\n") or None


def _importance(value: Any) -> str | None:
    if value is None:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        text = str(value).strip().lower()
        return text if text in ("low", "normal", "high") else None
    return {0: "low", 1: "normal", 2: "high"}.get(n)


def _time(message, item: ParsedItem) -> TimePoint:
    for attr_name in ("date", "sentDate", "receivedTime", "creationTime"):
        point = _to_timepoint(_attr(message, attr_name))
        if point.utc:
            return point

    item.note(
        "no_date",
        "This saved message carries no date that Recall could read. It is kept "
        "in the Undated list, and no date has been invented for it.",
    )
    return TimePoint.unknown()


def _to_timepoint(value: Any) -> TimePoint:
    if value is None:
        return TimePoint.unknown()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return TimePoint.from_naive(value)
        return TimePoint.from_aware(value)

    text = str(value).strip()
    if not text:
        return TimePoint.unknown()
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            return TimePoint.from_naive(dt)
        return TimePoint.from_aware(dt)
    except Exception:  # noqa: BLE001
        return TimePoint.unknown()


def _address_type(address: str | None) -> str:
    if not address:
        return "none"
    if "@" in address:
        return "smtp"
    if address.startswith("/"):
        return "ex"
    return "smtp"


def _recipient_role(recipient_type: Any, kind: str) -> str:
    if kind == Kind.EVENT:
        return Role.ATTENDEE
    try:
        n = int(recipient_type)
    except (TypeError, ValueError):
        return Role.TO
    return {0: Role.FROM, 1: Role.TO, 2: Role.CC, 3: Role.BCC}.get(n, Role.TO)


def _extract_address(text: str | None) -> str | None:
    if not text:
        return None
    import re

    match = re.search(r"<([^>]+@[^>]+)>", text)
    if match:
        return match.group(1).strip()
    if "@" in text and " " not in text.strip():
        return text.strip()
    return None


def _extract_name(text: str | None) -> str | None:
    if not text:
        return None
    import re

    match = re.match(r"^\s*([^<]+?)\s*<", text)
    if match:
        return match.group(1).strip().strip('"') or None
    return None if "@" in text else text.strip() or None


def _headers_text(message) -> str | None:
    """A readable header block when the raw one is unavailable."""
    parts = []
    for label, attr_name in (
        ("Date", "date"), ("From", "sender"), ("To", "to"), ("Cc", "cc"),
        ("Subject", "subject"), ("Message-ID", "messageId"),
        ("Message-Class", "messageClass"),
    ):
        value = _text(_attr(message, attr_name))
        if value:
            parts.append(f"{label}: {value}")
    return "\n".join(parts) if parts else None


def _nested_message_text(nested) -> str | None:
    """A forwarded .msg rendered as text, so it is searchable."""
    lines = []
    for label, attr_name in (
        ("Date", "date"), ("From", "sender"), ("To", "to"), ("Cc", "cc"),
        ("Subject", "subject"),
    ):
        value = _text(_attr(nested, attr_name))
        if value:
            lines.append(f"{label}: {value}")

    body = _text(_attr(nested, "body"))
    if not body:
        html_body = _attr(nested, "htmlBody")
        if html_body:
            if isinstance(html_body, bytes):
                html_body = clean_text(html_body).text
            body = html_to_text(str(html_body))

    if body:
        lines.append("")
        lines.append(body)

    return "\n".join(lines) if lines else None
