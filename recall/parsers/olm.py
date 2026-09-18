"""Mac Outlook archives (.olm) - a zip of XML, and the easy win the spec calls it.

An .olm is a zip containing:

    Accounts/<account>/Mail/<folder>/Messages_NNN.xml
    Accounts/<account>/Calendar/...
    Accounts/<account>/Contacts/...
    Local/...

Each XML file holds a batch of records with element names prefixed ``OPF``
(Outlook Personal Format). The folder path inside the zip is the folder path in
the mailbox, which is worth keeping - a message filed under
"Clients/Fitzgerald" says something the message body does not.

Two things this reader is careful about:

* **Zip bombs and traversal.** A member's uncompressed size is checked before
  it is read, and member names are never used as filesystem paths.
* **Encodings.** Mac Outlook writes UTF-8, but a message body inside it can
  carry text that was already mangled before it got there, so everything goes
  through the same repair pipeline as every other format.
"""

from __future__ import annotations

import re
import zipfile
from datetime import datetime, timezone
from typing import Iterator
from xml.etree import ElementTree

from ..logging_setup import get_logger
from ..models import Kind, ParsedAttachment, ParsedIdentity, ParsedItem, Role, TimePoint
from ..normalize.text import clean_text, html_to_text
from .base import Parser, register

log = get_logger("parsers.olm")

#: Refuse a member that expands to more than this. A 40 GB member in a 2 MB
#: zip is not a mailbox.
MAX_MEMBER_BYTES = 256 * 1024 * 1024

#: Total expansion ratio beyond which the archive is treated as hostile.
MAX_EXPANSION_RATIO = 200


@register
class OlmParser(Parser):
    """Mail, calendar and contacts out of a Mac Outlook archive."""

    extensions = frozenset({".olm"})
    produces = frozenset({Kind.MESSAGE, Kind.EVENT, Kind.CONTACT})
    name = "olm"

    def parse(self, kinds: frozenset[str] | None = None) -> Iterator[ParsedItem]:
        try:
            archive = zipfile.ZipFile(self.path)
        except (zipfile.BadZipFile, OSError) as exc:
            self.outcome.error = f"{exc.__class__.__name__}: {exc}"
            self.outcome.error_detail = repr(exc)
            self.outcome.add_finding(
                "read_failure", "critical",
                f"{self.path.name} is not a Mac Outlook archive Recall can open",
                "A Mac Outlook archive is a zip file. This one could not be "
                f"opened as one.\n\nExact error: {exc}",
                {"path": str(self.path)},
            )
            return

        produced = 0
        try:
            members = [
                info for info in archive.infolist()
                if info.filename.lower().endswith(".xml") and not info.is_dir()
            ]

            compressed = sum(i.compress_size for i in members) or 1
            uncompressed = sum(i.file_size for i in members)
            if uncompressed / compressed > MAX_EXPANSION_RATIO:
                self.outcome.error = "the archive expands implausibly, and was not opened"
                self.outcome.add_finding(
                    "read_failure", "critical",
                    f"{self.path.name} expands to an implausible size",
                    f"The archive is {compressed:,} bytes and claims to expand to "
                    f"{uncompressed:,}. Recall has not opened it.\n\n"
                    "The file has not been changed.",
                    {"path": str(self.path)},
                )
                return

            claimed = 0
            for info in members:
                if info.file_size > MAX_MEMBER_BYTES:
                    log.warning(
                        "Skipping %s in %s: %d bytes is too large for one batch",
                        info.filename, self.path.name, info.file_size,
                    )
                    self.outcome.add_finding(
                        "read_failure", "high",
                        f"{self.path.name}: one part was too large to open",
                        f"The part {info.filename!r} claims to be "
                        f"{info.file_size:,} bytes. Recall skipped it rather than "
                        "trying to read it into memory.",
                        {"path": str(self.path), "member": info.filename},
                    )
                    continue

                try:
                    raw = archive.read(info)
                except Exception as exc:  # noqa: BLE001 - one part, not the file
                    self.outcome.add_finding(
                        "read_failure", "critical",
                        f"{self.path.name}: one part could not be read",
                        f"Part: {info.filename}\n"
                        f"Exact error: {exc.__class__.__name__}: {exc}",
                        {"path": str(self.path), "member": info.filename},
                    )
                    continue

                folder_path = _folder_from_member(info.filename)
                claimed += _count_records(raw)

                for item in self._records(raw, folder_path, info.filename, kinds):
                    if self._limit_reached(produced):
                        self.outcome.claimed_count = claimed
                        return
                    produced += 1
                    self.outcome.yielded_count = produced
                    self.outcome.items_scanned = produced
                    yield item

            self.outcome.claimed_count = claimed
            self.outcome.folders_seen = len({_folder_from_member(i.filename) for i in members})
        finally:
            try:
                archive.close()
            except Exception:  # noqa: BLE001
                pass

    def _records(
        self, raw: bytes, folder_path: str, member: str, kinds: frozenset[str] | None
    ) -> Iterator[ParsedItem]:
        decoded = clean_text(raw)
        try:
            root = ElementTree.fromstring(decoded.text)
        except ElementTree.ParseError as exc:
            self.outcome.add_finding(
                "read_failure", "critical",
                f"{self.path.name}: one part is not readable XML",
                f"Part: {member}\nExact error: {exc}",
                {"path": str(self.path), "member": member},
            )
            return

        for element in root.iter():
            tag = element.tag.lower()
            if tag == "email":
                if self._wants(kinds, Kind.MESSAGE):
                    item = self._message(element, folder_path, decoded.confidence)
                    if item is not None:
                        yield item
            elif tag in ("appointment", "event"):
                if self._wants(kinds, Kind.EVENT):
                    item = self._event(element, folder_path, decoded.confidence)
                    if item is not None:
                        yield item
            elif tag == "contact":
                if self._wants(kinds, Kind.CONTACT):
                    item = self._contact(element, folder_path, decoded.confidence)
                    if item is not None:
                        yield item

    # -- record types ----------------------------------------------------

    def _message(self, element, folder_path: str, confidence: float) -> ParsedItem | None:
        item = ParsedItem(kind=Kind.MESSAGE, backend=self.name, folder_path=folder_path)
        item.parse_confidence = confidence

        item.subject = _text(element, "OPFMessageCopySubject")
        item.internet_message_id = _strip_brackets(
            _text(element, "OPFMessageCopyMessageID")
        )
        item.in_reply_to = _strip_brackets(_text(element, "OPFMessageCopyInReplyTo"))
        item.conversation_topic = _text(element, "OPFMessageCopyThreadTopic") or item.subject

        references = _text(element, "OPFMessageCopyReferences")
        if references:
            item.references = [m.group(1) for m in re.finditer(r"<([^>]+)>", references)]

        body = _text(element, "OPFMessageCopyBody")
        html_body = _text(element, "OPFMessageCopyHTMLBody")
        item.body_html = html_body
        if body:
            item.body_text = body
            item.body_format = "plain"
        elif html_body:
            item.body_text = html_to_text(html_body)
            item.body_format = "html"

        item.occurred = _timepoint(
            _text(element, "OPFMessageCopySentTime")
            or _text(element, "OPFMessageCopyReceivedTime"),
            item,
        )

        for tag, role in (
            ("OPFMessageCopyFromAddresses", Role.FROM),
            ("OPFMessageCopySenderAddress", Role.FROM),
            ("OPFMessageCopyToAddresses", Role.TO),
            ("OPFMessageCopyCCAddresses", Role.CC),
            ("OPFMessageCopyBCCAddresses", Role.BCC),
        ):
            for identity in _addresses(element, tag, role):
                item.participants.append(identity)

        for attachment in _attachments(element):
            item.attachments.append(attachment)

        if not item.subject and not item.body_text and not item.participants:
            return None
        return item

    def _event(self, element, folder_path: str, confidence: float) -> ParsedItem | None:
        item = ParsedItem(kind=Kind.EVENT, backend=self.name, folder_path=folder_path)
        item.parse_confidence = confidence

        item.subject = _text(element, "OPFCalendarEventCopySummary")
        item.location = _text(element, "OPFCalendarEventCopyLocation")
        item.body_text = _text(element, "OPFCalendarEventCopyDescription")
        item.body_format = "plain"
        item.ical_uid = _text(element, "OPFCalendarEventCopyUID")

        item.occurred = _timepoint(_text(element, "OPFCalendarEventCopyStartTime"), item)
        end = _text(element, "OPFCalendarEventCopyEndTime")
        if end:
            end_point = _timepoint(end, item, note=False)
            if end_point.utc:
                item.end = end_point

        item.all_day = (_text(element, "OPFCalendarEventCopyIsAllDayEvent") or "").lower() in (
            "true", "1", "yes",
        )

        for identity in _addresses(element, "OPFCalendarEventCopyOrganizer", Role.ORGANIZER):
            item.participants.append(identity)
        for identity in _addresses(element, "OPFCalendarEventCopyAttendeeList", Role.ATTENDEE):
            item.participants.append(identity)

        return item if item.subject or item.occurred.utc else None

    def _contact(self, element, folder_path: str, confidence: float) -> ParsedItem | None:
        item = ParsedItem(kind=Kind.CONTACT, backend=self.name, folder_path=folder_path)
        item.parse_confidence = confidence

        name = (
            _text(element, "OPFContactCopyDisplayName")
            or " ".join(x for x in (
                _text(element, "OPFContactCopyFirstName"),
                _text(element, "OPFContactCopyLastName"),
            ) if x).strip()
        )
        emails = [
            e.strip() for e in (
                _text(element, "OPFContactCopyEmailAddressList") or ""
            ).split(";") if e.strip() and "@" in e
        ]
        for attr_email in element.iter():
            address = attr_email.get("OPFContactEmailAddressAddress")
            if address and address not in emails:
                emails.append(address)

        if not name and not emails:
            return None

        item.subject = name or (emails[0] if emails else None)
        item.occurred = TimePoint.unknown()
        # Deliberately not a no_date finding. A contact card records a person,
        # not something that happened, so having no date is its normal
        # condition rather than a defect. Flagging every entry in an address
        # book would put thousands of non-problems on the Problems screen and
        # teach the user to ignore it.
        item.contact = {
            "display_name": name or None,
            "given_name": _text(element, "OPFContactCopyFirstName"),
            "surname": _text(element, "OPFContactCopyLastName"),
            "organization": _text(element, "OPFContactCopyBusinessCompany"),
            "title": _text(element, "OPFContactCopyBusinessTitle"),
            "emails": emails,
            "phones": {
                "business": _text(element, "OPFContactCopyBusinessPhone"),
                "home": _text(element, "OPFContactCopyHomePhone"),
                "mobile": _text(element, "OPFContactCopyMobilePhone"),
            },
            "notes": _text(element, "OPFContactCopyNotesPlain"),
        }
        for address in emails:
            item.participants.append(
                ParsedIdentity(address=address, address_type="smtp",
                               display_name=name or None, role=Role.TO)
            )
        return item


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _folder_from_member(member: str) -> str:
    """"Accounts/Tim/Mail/Inbox/Messages_001.xml" to "Tim/Mail/Inbox"."""
    parts = [p for p in member.replace("\\", "/").split("/") if p and p != ".."]
    if parts and parts[0].lower() == "accounts":
        parts = parts[1:]
    if parts and parts[-1].lower().endswith(".xml"):
        parts = parts[:-1]
    return "/".join(parts) or "Mac Outlook archive"


#: Element opens, with a real boundary after the name. A plain substring count
#: of "<email" also matches "<emails>" (the wrapper) and "<emailAddress" (a
#: participant), which made a four-record claim out of a one-message file - and
#: then a critical "3 records could not be read" finding about a file that was
#: read perfectly.
_RECORD_ELEMENT = re.compile(
    rb"<(email|appointment|event|contact)(?=[\s/>])", re.IGNORECASE
)


def _count_records(raw: bytes) -> int:
    return len(_RECORD_ELEMENT.findall(raw))


def _text(element, tag: str) -> str | None:
    """A child element's text, matched case-insensitively."""
    wanted = tag.lower()
    for child in element.iter():
        if child.tag.lower() == wanted:
            value = (child.text or "").strip()
            if value:
                return clean_text(value).text
    return None


def _strip_brackets(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().strip("<>").strip() or None


def _addresses(element, tag: str, role: str) -> list[ParsedIdentity]:
    """Addresses inside a container element.

    Mac Outlook writes them as attributes on ``<emailAddress>`` children, so
    the container is found first and its descendants read.
    """
    out: list[ParsedIdentity] = []
    wanted = tag.lower()

    for child in element.iter():
        if child.tag.lower() != wanted:
            continue
        for node in child.iter():
            address = node.get("OPFContactEmailAddressAddress")
            name = node.get("OPFContactEmailAddressName")
            if not address and not name:
                continue
            out.append(
                ParsedIdentity(
                    address=(address or "").strip() or None,
                    address_type="smtp" if address and "@" in address else "none",
                    display_name=(name or "").strip() or None,
                    role=role,
                )
            )
        # A bare text value, which some versions write instead.
        text = (child.text or "").strip()
        if text and not out:
            for piece in [p.strip() for p in text.split(";") if p.strip()]:
                out.append(
                    ParsedIdentity(
                        address=piece if "@" in piece else None,
                        address_type="smtp" if "@" in piece else "none",
                        display_name=None if "@" in piece else piece,
                        role=role,
                    )
                )
    return out


def _attachments(element) -> list[ParsedAttachment]:
    """Attachment metadata.

    An .olm stores attachment bytes as separate zip members referenced by a
    path. The name and size are recorded here; the contents are not read,
    which is stated on the record rather than left to look like an empty file.
    """
    out: list[ParsedAttachment] = []
    for child in element.iter():
        if child.tag.lower() not in ("messageattachment", "attachment"):
            continue
        name = (
            child.get("OPFAttachmentName")
            or child.get("OPFAttachmentContentID")
            or (child.text or "").strip()
        )
        if not name:
            continue
        size = child.get("OPFAttachmentContentLength")
        out.append(
            ParsedAttachment(
                filename=name,
                mime_type=child.get("OPFAttachmentContentType"),
                size_bytes=int(size) if size and size.isdigit() else None,
                data=None,
                content_id=child.get("OPFAttachmentContentID"),
                read_error=(
                    "Mac Outlook archives keep attachment contents in a separate "
                    "part of the file that Recall does not read. The name and "
                    "size are recorded; the contents are not in the archive."
                ),
            )
        )
    return out


def _timepoint(value: str | None, item: ParsedItem, note: bool = True) -> TimePoint:
    if not value:
        if note:
            item.note(
                "no_date",
                "This record carries no date in the Mac Outlook archive. It is "
                "kept in the Undated list, and no date has been invented.",
            )
        return TimePoint.unknown()

    text = value.strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt.endswith("Z"):
            return TimePoint.from_aware(dt.replace(tzinfo=timezone.utc), "UTC")
        if dt.tzinfo is not None:
            return TimePoint.from_aware(dt)
        if note:
            item.note(
                "unknown_timezone",
                f"This record's time, {text!r}, does not say which timezone it is "
                "in. The date is right; the exact moment is uncertain.",
            )
        return TimePoint.from_naive(dt)

    if note:
        item.note(
            "no_date",
            f"This record's date could not be understood: {text!r}. It is kept "
            "in the Undated list with the raw value preserved.",
        )
    return TimePoint.unknown()
