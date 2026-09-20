"""Reading .pst and .ost, two ways, behind one interface.

Spec section 2 requires two interchangeable backends:

``PyPffBackend``
    The default. Reads the file structure directly, needs no Outlook, and is
    fast enough for hundreds of thousands of messages. It does not understand
    modern OST encryption, and appointment fields live in named MAPI properties
    whose ids differ per file, so it cannot always read a calendar.

``OutlookComBackend``
    Drives Microsoft Outlook itself: ``AddStore``, walk ``Folders``/``Items``,
    ``RemoveStore``. Slower by an order of magnitude, and authoritative -
    Outlook understands every OST variant, every ANSI quirk, and appointment
    fields by name rather than by guessed id.

Selection is automatic: pypff first; Outlook if pypff fails, or if the file is
an ``.ost`` that yielded nothing. If both fail the source is marked ``failed``
with the exact error and the run continues. A store never stops a run.

Neither backend writes to the file. ``AddStore`` attaches a store to the Outlook
profile read-only for the duration and ``RemoveStore`` detaches it in a finally
block, including when the user presses Ctrl-C.
"""

from __future__ import annotations

import abc
import json
from pathlib import Path
from typing import Any, Iterator

from ..comguard import (
    CLEANED_UP, ComTimeout, ComUnavailable, end_stranded_outlook,
    is_outlook_registered, outlook_processes, run_with_timeout,
)
from ..logging_setup import get_logger
from ..models import (
    Kind,
    ParsedAttachment,
    ParsedIdentity,
    ParsedItem,
    ParseOutcome,
    Role,
    TimePoint,
)
from ..normalize.text import clean_text, decompress_rtf, html_to_text, is_compressed_rtf, rtf_to_text
from . import mapi
from .base import Parser, ParserError, register

log = get_logger("parsers.pst")

#: Folders that hold no correspondence worth archiving.
_SKIP_FOLDERS = {
    "deleted items", "junk e-mail", "junk email", "spam", "conflicts",
    "local failures", "server failures", "sync issues", "rss feeds",
}


#: Outlook data files, the only kind Outlook itself can be asked to read.
OUTLOOK_STORE_EXTENSIONS = frozenset({".pst", ".ost"})

#: What Windows says when another program is holding a file open.
_LOCK_MARKERS = ("PermissionError", "being used by another")


def looks_locked_by_outlook(ext: str | None, lock_error: str | None) -> bool:
    """Is this a file Outlook is probably holding open, that Outlook could read?

    Deliberately narrow. A locked .mbox is still locked - nothing here can help
    it. A .pst that failed with a disk error is broken, not busy, and sending it
    to Outlook only spends two minutes finding that out again. Only a sharing
    error on an Outlook data file qualifies, because only then is there a real
    chance that the program holding it open is also the program that can read
    it out.

    The SQL form below has to agree with this exactly; there is a test that
    runs the same cases through both.
    """
    if (ext or "").lower() not in OUTLOOK_STORE_EXTENSIONS:
        return False
    err = lock_error or ""
    return any(marker in err for marker in _LOCK_MARKERS)


#: :func:`looks_locked_by_outlook` as a WHERE clause, for picking these rows out
#: of the database without loading all of them first.
LOCKED_BY_OUTLOOK_SQL = (
    "(is_readable = 0 AND is_placeholder = 0 "
    "AND LOWER(ext) IN ('.pst', '.ost') "
    "AND (lock_error LIKE '%PermissionError%' "
    "     OR lock_error LIKE '%being used by another%'))"
)


class PstBackend(abc.ABC):
    """One way of reading a PST or OST."""

    name: str = "unknown"

    def __init__(self, path: Path, *, sample_limit: int = 0) -> None:
        self.path = path
        self.sample_limit = sample_limit
        self.outcome = ParseOutcome(source_path=str(path), backend=self.name)

    @abc.abstractmethod
    def available(self) -> bool:
        """Can this backend be used on this computer at all?"""

    @abc.abstractmethod
    def parse(self, kinds: frozenset[str] | None = None) -> Iterator[ParsedItem]:
        ...

    def close(self) -> None:
        ...


# ---------------------------------------------------------------------------
# pypff
# ---------------------------------------------------------------------------


class PyPffBackend(PstBackend):
    """The fast path. Reads the store's own structures."""

    name = "pypff"

    def __init__(self, path: Path, *, sample_limit: int = 0) -> None:
        super().__init__(path, sample_limit=sample_limit)
        self._file = None
        self._codepage: str | None = None
        self._named: dict[str, int] | None = None

    def available(self) -> bool:
        try:
            import pypff  # noqa: F401

            return True
        except ImportError:
            return False

    def parse(self, kinds: frozenset[str] | None = None) -> Iterator[ParsedItem]:
        try:
            import pypff
        except ImportError as exc:
            self.outcome.error = "the libpff-python library is not installed"
            self.outcome.error_detail = str(exc)
            raise ParserError(
                "Outlook data files cannot be read quickly because libpff-python "
                "is not installed. Microsoft Outlook will be used instead if it "
                "is available."
            ) from exc

        try:
            self._file = pypff.file()
            self._file.open(str(self.path))
        except Exception as exc:  # noqa: BLE001 - every failure mode is reported
            self.outcome.error = f"{exc.__class__.__name__}: {exc}"
            self.outcome.error_detail = repr(exc)
            log.warning("pypff could not open %s: %s", self.path, exc)
            self._file = None
            return

        self._codepage = self._ascii_codepage()
        self._named = mapi.named_property_ids(self._file)

        try:
            root = self._file.get_root_folder()
        except Exception as exc:  # noqa: BLE001
            self.outcome.error = f"the folder tree could not be read: {exc}"
            self.outcome.error_detail = repr(exc)
            return

        produced = 0
        for item in self._walk_folder(root, "", kinds, depth=0):
            produced += 1
            self.outcome.yielded_count = produced
            yield item
            if self.sample_limit and produced >= self.sample_limit:
                return

    def _ascii_codepage(self) -> str | None:
        """The store's declared ANSI codepage, used as a decoding hint."""
        try:
            cp = self._file.get_ascii_codepage()
            if cp:
                return f"cp{cp}"
        except Exception:  # noqa: BLE001
            pass
        return None

    def _walk_folder(
        self, folder, parent_path: str, kinds: frozenset[str] | None, depth: int
    ) -> Iterator[ParsedItem]:
        if depth > 40:
            # A folder tree this deep is a structural loop, not a filing system.
            log.warning("Stopped at depth %d in %s - the folder tree loops", depth, self.path)
            self.outcome.add_finding(
                "read_failure",
                "high",
                f"{self.path.name} has a folder structure that loops back on itself",
                f"Recall stopped descending at {parent_path!r} after 40 levels. "
                "Anything below that point has not been read.",
                {"path": str(self.path), "folder": parent_path, "depth": depth},
            )
            return

        try:
            name = folder.get_name() or "Top of Outlook Data File"
        except Exception:  # noqa: BLE001
            name = "(unnamed folder)"

        folder_path = f"{parent_path}/{name}" if parent_path else name
        self.outcome.folders_seen += 1

        try:
            n_messages = folder.get_number_of_sub_messages()
        except Exception as exc:  # noqa: BLE001
            n_messages = 0
            log.debug("Message count unavailable for %s: %s", folder_path, exc)

        # The folder's own idea of how much it holds, which is what makes
        # estimated_loss a computed number rather than a feeling.
        claimed = self._folder_claimed_count(folder, n_messages)
        yielded_here = 0
        skipped = name.lower() in _SKIP_FOLDERS

        if not skipped:
            for index in range(n_messages):
                try:
                    message = folder.get_sub_message(index)
                except Exception as exc:  # noqa: BLE001
                    self._record_read_failure(folder_path, index, exc)
                    continue

                try:
                    item = self._build_item(message, folder_path, kinds)
                except Exception as exc:  # noqa: BLE001 - one message, not the store
                    self._record_read_failure(folder_path, index, exc)
                    continue

                if item is None:
                    continue

                self.outcome.last_good_folder = folder_path
                self.outcome.last_good_offset = index
                yielded_here += 1
                yield item

                if self.sample_limit and self.outcome.yielded_count + yielded_here >= self.sample_limit:
                    break

        # Deleted Items and Junk are skipped on purpose, so what they hold was
        # never going to be read. Counting them as claimed-but-not-yielded
        # would manufacture an estimated_loss out of a deliberate choice and
        # report deleted mail as missing mail.
        if not skipped:
            self.outcome.folder_counts[folder_path] = (claimed, yielded_here)
        else:
            self.outcome.folder_counts[folder_path] = (0, 0)

        try:
            n_sub = folder.get_number_of_sub_folders()
        except Exception:  # noqa: BLE001
            n_sub = 0

        for index in range(n_sub):
            try:
                sub = folder.get_sub_folder(index)
            except Exception as exc:  # noqa: BLE001
                self._record_read_failure(folder_path, index, exc, what="subfolder")
                continue
            yield from self._walk_folder(sub, folder_path, kinds, depth + 1)

    def _folder_claimed_count(self, folder, fallback: int) -> int:
        """PR_CONTENT_COUNT - what the folder says it holds."""
        try:
            props = mapi.read_properties(folder, codepage_hint=self._codepage)
            claimed = mapi.as_int(props.get(mapi.PR_CONTENT_COUNT))
            if claimed is not None and claimed >= 0:
                return claimed
        except Exception:  # noqa: BLE001
            pass
        return fallback

    def _record_read_failure(self, folder_path: str, index: int, exc: Exception, what: str = "message") -> None:
        log.debug("%s %d in %s could not be read: %s", what, index, folder_path, exc)
        self.outcome.add_finding(
            "read_failure",
            "critical",
            f"{self.path.name}: a {what} could not be read",
            f"Folder: {folder_path}\nPosition: {index}\n"
            f"Last folder read successfully: {self.outcome.last_good_folder or 'none'}\n"
            f"Exact error: {exc.__class__.__name__}: {exc}",
            {
                "path": str(self.path),
                "folder": folder_path,
                "offset": index,
                "backend": self.name,
                "error": f"{exc.__class__.__name__}: {exc}",
            },
        )

    # -- one message -----------------------------------------------------

    def _build_item(self, message, folder_path: str, kinds: frozenset[str] | None) -> ParsedItem | None:
        props = mapi.read_properties(message, codepage_hint=self._codepage)

        # Counted before the filter: a store full of mail genuinely contains no
        # calendar entries, and that is an answer, not a failure to read.
        self.outcome.items_scanned += 1

        message_class = mapi.as_text(props.get(mapi.PR_MESSAGE_CLASS))
        kind = mapi.kind_for_message_class(message_class)
        if kinds is not None and kind not in kinds:
            return None

        item = ParsedItem(kind=kind, backend=self.name, folder_path=folder_path)
        item.raw_headers = mapi.as_text(props.get(mapi.PR_TRANSPORT_MESSAGE_HEADERS))

        try:
            item.native_id = str(message.get_identifier())
        except Exception:  # noqa: BLE001
            item.native_id = None

        item.subject = _subject(message, props)
        item.conversation_topic = mapi.as_text(props.get(mapi.PR_CONVERSATION_TOPIC))
        item.importance = mapi.IMPORTANCE.get(mapi.as_int(props.get(mapi.PR_IMPORTANCE)) or 1)
        item.sensitivity = mapi.SENSITIVITY.get(mapi.as_int(props.get(mapi.PR_SENSITIVITY)) or 0)

        self._bodies(message, props, item)
        self._times(props, item, kind)
        self._participants(message, props, item, kind)

        item.internet_message_id = mapi.as_text(props.get(mapi.PR_INTERNET_MESSAGE_ID))
        item.in_reply_to = mapi.as_text(props.get(mapi.PR_IN_REPLY_TO_ID))
        references = mapi.as_text(props.get(mapi.PR_INTERNET_REFERENCES))
        if references:
            item.references = [r.strip() for r in references.split() if r.strip()]

        if kind == Kind.EVENT:
            self._appointment_fields(props, item, message_class)
        elif kind == Kind.CONTACT:
            item.contact = self._contact_card(props)

        self._attachments(message, item)

        if message_class:
            extra = {"message_class": message_class}
            item.raw_headers = (item.raw_headers or "") + "\n" + json.dumps(extra)

        return item

    def _subject(self, message, props) -> str | None:  # pragma: no cover - see _subject below
        return _subject(message, props)

    def _bodies(self, message, props: dict, item: ParsedItem) -> None:
        """Plain, HTML and RTF, in that order of preference for body_text."""
        plain = None
        try:
            raw = message.get_plain_text_body()
            if raw:
                decoded = clean_text(raw, self._codepage)
                plain = decoded.text
                item.parse_confidence = min(item.parse_confidence, decoded.confidence)
        except Exception:  # noqa: BLE001
            plain = mapi.as_text(props.get(mapi.PR_BODY))

        html_body = None
        try:
            raw = message.get_html_body()
            if raw:
                decoded = clean_text(raw, self._codepage)
                html_body = decoded.text
                item.parse_confidence = min(item.parse_confidence, decoded.confidence)
        except Exception:  # noqa: BLE001
            pass

        rtf_text = None
        if not plain and not html_body:
            try:
                raw = message.get_rtf_body()
            except Exception:  # noqa: BLE001
                raw = props.get(mapi.PR_RTF_COMPRESSED)
            if raw:
                rtf_text = self._rtf(raw, item)

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

    def _rtf(self, raw, item: ParsedItem) -> str | None:
        """RTF, decompressing PR_RTF_COMPRESSED when that is what it is."""
        try:
            if isinstance(raw, bytes):
                if is_compressed_rtf(raw):
                    return rtf_to_text(decompress_rtf(raw))
                return rtf_to_text(clean_text(raw, self._codepage).text)
            return rtf_to_text(str(raw))
        except ValueError as exc:
            item.note(
                "low_confidence_text",
                f"This message's text is stored in a compressed format that could "
                f"not be unpacked ({exc}). The message is kept, but its body may "
                "be missing.",
            )
            item.parse_confidence = min(item.parse_confidence, 0.5)
            return None
        except Exception as exc:  # noqa: BLE001
            log.debug("RTF body could not be converted: %s", exc)
            return None

    def _times(self, props: dict, item: ParsedItem, kind: str) -> None:
        """MAPI times are UTC, so the instant and the zone are both known."""
        if kind == Kind.EVENT:
            start = mapi.first(props, mapi.PR_START_DATE)
            end = mapi.first(props, mapi.PR_END_DATE)
        else:
            start = mapi.first(
                props,
                mapi.PR_CLIENT_SUBMIT_TIME,
                mapi.PR_MESSAGE_DELIVERY_TIME,
                mapi.PR_CREATION_TIME,
            )
            end = None

        if isinstance(start, str) and start:
            item.occurred = TimePoint(utc=start, local=start[:19], tz="UTC", tz_known=True)
        else:
            item.occurred = TimePoint.unknown()
            item.note(
                "no_date",
                "This record carries no date that Recall could read, so it cannot "
                "be placed on the timeline. It is kept in the Undated list.",
            )

        if isinstance(end, str) and end:
            item.end = TimePoint(utc=end, local=end[:19], tz="UTC", tz_known=True)

    def _participants(self, message, props: dict, item: ParsedItem, kind: str) -> None:
        """The sender, and every recipient from the Recipients sub-item."""
        sender_address = mapi.as_text(
            mapi.first(
                props,
                mapi.PR_SENDER_SMTP_ADDRESS,
                mapi.PR_SENT_REPRESENTING_SMTP_ADDRESS,
                mapi.PR_SENDER_EMAIL_ADDRESS,
                mapi.PR_SENT_REPRESENTING_EMAIL_ADDRESS,
            )
        )
        sender_name = mapi.as_text(
            mapi.first(props, mapi.PR_SENDER_NAME, mapi.PR_SENT_REPRESENTING_NAME)
        )
        if not sender_name:
            try:
                sender_name = clean_text(message.get_sender_name() or "", self._codepage).text or None
            except Exception:  # noqa: BLE001
                sender_name = None

        addr_type = (
            mapi.as_text(mapi.first(props, mapi.PR_SENDER_ADDRTYPE, mapi.PR_SENT_REPRESENTING_ADDRTYPE))
            or ""
        ).upper()

        if sender_address or sender_name:
            item.participants.append(
                ParsedIdentity(
                    address=sender_address,
                    address_type=_address_type(sender_address, addr_type),
                    display_name=sender_name,
                    role=Role.ORGANIZER if kind == Kind.EVENT else Role.FROM,
                )
            )

        for row in self._recipient_rows(message):
            identity = _recipient_identity(row, kind)
            if identity is not None:
                item.participants.append(identity)

        # When there is no Recipients sub-item - some stores drop it - the
        # display strings are the only record of who this went to. They are
        # names without addresses, which is exactly what ParsedIdentity is for.
        if not any(p.role in (Role.TO, Role.CC, Role.BCC, Role.ATTENDEE) for p in item.participants):
            for tag, role in (
                (mapi.PR_DISPLAY_TO, Role.TO),
                (mapi.PR_DISPLAY_CC, Role.CC),
                (mapi.PR_DISPLAY_BCC, Role.BCC),
            ):
                text = mapi.as_text(props.get(tag))
                if not text:
                    continue
                for name in [n.strip() for n in text.split(";") if n.strip()]:
                    item.participants.append(
                        ParsedIdentity(
                            address=name if "@" in name else None,
                            address_type="smtp" if "@" in name else "none",
                            display_name=None if "@" in name else name,
                            role=Role.ATTENDEE if kind == Kind.EVENT else role,
                        )
                    )

    def _recipient_rows(self, message) -> list[dict]:
        """The Recipients sub-item, one record set per person."""
        try:
            n_sub = message.get_number_of_sub_items()
        except Exception:  # noqa: BLE001
            return []

        for index in range(n_sub):
            try:
                sub = message.get_sub_item(index)
                rows = mapi.read_record_sets(sub, codepage_hint=self._codepage)
            except Exception:  # noqa: BLE001
                continue
            if rows and any(mapi.PR_RECIPIENT_TYPE in r or mapi.PR_EMAIL_ADDRESS in r for r in rows):
                return rows
        return []

    def _appointment_fields(self, props: dict, item: ParsedItem, message_class: str | None) -> None:
        """Location, busy status and recurrence, as far as pypff can reach them.

        Location lives in a named property whose id varies per file. Where it
        cannot be resolved the field is left empty and the item is marked, so
        the Outlook backend can be tried - rather than a plausible-looking
        value being invented.
        """
        item.location = mapi.as_text(props.get(0x8208)) or None

        busy = mapi.as_int(props.get(0x8205))
        if busy is not None:
            item.busy_status = {0: "free", 1: "tentative", 2: "busy", 3: "out_of_office"}.get(busy)

        all_day = props.get(0x8215)
        if isinstance(all_day, bool):
            item.all_day = all_day

        if props.get(0x8223) or props.get(0x8216):
            item.is_recurring_master = True
            pattern = props.get(0x8216)
            item.recurrence = {
                "source": "mapi",
                "has_binary_pattern": isinstance(pattern, bytes),
                "pattern_bytes": len(pattern) if isinstance(pattern, bytes) else None,
            }
            item.note(
                "unresolved_recurrence",
                "This appointment repeats. Outlook stores the repeat rule in a "
                "packed binary form that the fast reader cannot decode, so only "
                "the first occurrence is shown. No repeats have been invented. "
                "Reading this file with Microsoft Outlook instead recovers the "
                "full rule.",
            )

        if message_class and "MEETING" in message_class.upper():
            item.meeting_status = "meeting"

        if item.location is None and item.occurred.utc:
            item.note(
                "low_confidence_text",
                "The meeting location could not be read by the fast reader - "
                "Outlook stores it under a name this file numbers differently. "
                "Reading this file with Microsoft Outlook recovers it.",
            )

    def _contact_card(self, props: dict) -> dict:
        """Everything on a contact card, kept verbatim."""
        def text(tag: int) -> str | None:
            return mapi.as_text(props.get(tag))

        emails = [
            e for e in (
                text(0x8083), text(0x8093), text(0x80A3),
                text(mapi.PR_EMAIL_ADDRESS),
            ) if e and "@" in e
        ]

        return {
            "display_name": text(mapi.PR_DISPLAY_NAME),
            "given_name": text(mapi.PR_GIVEN_NAME),
            "surname": text(mapi.PR_SURNAME),
            "middle_name": text(mapi.PR_MIDDLE_NAME),
            "nickname": text(mapi.PR_NICKNAME),
            "title": text(mapi.PR_TITLE),
            "organization": text(mapi.PR_COMPANY_NAME),
            "department": text(mapi.PR_DEPARTMENT_NAME),
            "office": text(mapi.PR_OFFICE_LOCATION),
            "profession": text(mapi.PR_PROFESSION),
            "emails": emails,
            "phones": {
                "business": text(mapi.PR_BUSINESS_TELEPHONE_NUMBER),
                "home": text(mapi.PR_HOME_TELEPHONE_NUMBER),
                "mobile": text(mapi.PR_MOBILE_TELEPHONE_NUMBER),
                "fax": text(mapi.PR_BUSINESS_FAX_NUMBER),
                "other": text(mapi.PR_OTHER_TELEPHONE_NUMBER),
            },
            "addresses": {
                "business": {
                    "street": text(mapi.PR_BUSINESS_ADDRESS_STREET),
                    "city": text(mapi.PR_BUSINESS_ADDRESS_CITY),
                    "state": text(mapi.PR_BUSINESS_ADDRESS_STATE),
                    "postal_code": text(mapi.PR_BUSINESS_ADDRESS_POSTAL_CODE),
                    "country": text(mapi.PR_BUSINESS_ADDRESS_COUNTRY),
                },
                "home": {
                    "street": text(mapi.PR_HOME_ADDRESS_STREET),
                    "city": text(mapi.PR_HOME_ADDRESS_CITY),
                    "state": text(mapi.PR_HOME_ADDRESS_STATE),
                    "postal_code": text(mapi.PR_HOME_ADDRESS_POSTAL_CODE),
                    "country": text(mapi.PR_HOME_ADDRESS_COUNTRY),
                },
            },
            "birthday": props.get(mapi.PR_BIRTHDAY),
            "anniversary": props.get(mapi.PR_WEDDING_ANNIVERSARY),
            "spouse": text(mapi.PR_SPOUSE_NAME),
            "web": text(mapi.PR_BUSINESS_HOME_PAGE) or text(mapi.PR_PERSONAL_HOME_PAGE),
            "notes": text(mapi.PR_BODY),
        }

    def _attachments(self, message, item: ParsedItem) -> None:
        try:
            n = message.get_number_of_attachments()
        except Exception:  # noqa: BLE001
            return

        for index in range(n):
            try:
                attachment = message.get_attachment(index)
            except Exception as exc:  # noqa: BLE001
                item.attachments.append(
                    ParsedAttachment(
                        filename=None,
                        read_error=f"attachment {index} could not be opened: {exc}",
                    )
                )
                continue

            props = mapi.read_properties(attachment, codepage_hint=self._codepage)
            filename = mapi.as_text(
                mapi.first(props, mapi.PR_ATTACH_LONG_FILENAME, mapi.PR_ATTACH_FILENAME)
            )
            method = mapi.as_int(props.get(mapi.PR_ATTACH_METHOD))
            flags = mapi.as_int(props.get(mapi.PR_ATTACH_FLAGS)) or 0
            content_id = mapi.as_text(props.get(mapi.PR_ATTACH_CONTENT_ID))

            data: bytes | None = None
            read_error: str | None = None
            if method == mapi.ATTACH_EMBEDDED_MSG:
                # A message attached to a message. It has no bytes to read -
                # it is a sub-item - so reading it here would record an empty
                # attachment and say nothing. Said plainly instead, so it
                # reaches the Problems screen rather than looking like a file
                # that happened to be empty.
                read_error = (
                    "This attachment is an email attached to an email. Recall "
                    "records that it is here, but does not yet open it to read "
                    "what is inside."
                )
            else:
                try:
                    size = attachment.get_size()
                    if size and size > 0:
                        data = attachment.read_buffer(size)
                except Exception as exc:  # noqa: BLE001
                    read_error = f"{exc.__class__.__name__}: {exc}"

            item.attachments.append(
                ParsedAttachment(
                    filename=filename,
                    mime_type=mapi.as_text(props.get(mapi.PR_ATTACH_MIME_TAG)),
                    size_bytes=mapi.as_int(props.get(mapi.PR_ATTACH_SIZE)) or (len(data) if data else None),
                    data=data,
                    is_inline=bool(content_id) or bool(flags & 0x04),
                    content_id=content_id,
                    read_error=read_error,
                )
            )

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except Exception:  # noqa: BLE001
                pass
            self._file = None


# ---------------------------------------------------------------------------
# Outlook COM
# ---------------------------------------------------------------------------


class OutlookComBackend(PstBackend):
    """Outlook itself does the reading. Slower, and it understands everything.

    Every COM call runs behind ``comguard``, so a hidden Outlook dialog
    produces a clear timeout rather than an overnight run that never finishes.
    """

    name = "com"

    #: Items pulled per COM round trip. Outlook's Items collection is slow per
    #: call, so this is the main throughput lever.
    _PAGE = 200

    #: How long Outlook may go without making any progress before Recall gives
    #: up on it. This is a stall timeout, not a total: a mailbox that takes six
    #: hours to walk is fine as long as it keeps moving.
    stall_timeout = 120.0

    def __init__(self, path: Path, *, sample_limit: int = 0, stall_timeout: float | None = None) -> None:
        super().__init__(path, sample_limit=sample_limit)
        self._store_id: str | None = None
        if stall_timeout is not None:
            self.stall_timeout = stall_timeout

    #: Below this a store cannot hold anything: a PST's own header and
    #: allocation maps do not fit. Handing such a file to Outlook gains
    #: nothing and costs the full stall timeout while Outlook decides whether
    #: to offer to repair it.
    _MIN_PLAUSIBLE_BYTES = 256 * 1024

    def available(self) -> bool:
        if not is_outlook_registered():
            return False
        try:
            import win32com.client  # noqa: F401
        except ImportError:
            return False

        try:
            size = self.path.stat().st_size
        except OSError:
            return False
        if size < self._MIN_PLAUSIBLE_BYTES:
            log.info(
                "Not asking Outlook to read %s: at %d bytes it is far too small "
                "to be a mail store, so there is nothing in it for Outlook to find",
                self.path.name, size,
            )
            return False
        return True

    def parse(self, kinds: frozenset[str] | None = None) -> Iterator[ParsedItem]:
        """Read the store through Outlook.

        The whole walk happens inside one COM-initialised worker thread, and the
        items come back as plain dictionaries. COM objects cannot cross threads,
        so they are converted before they are handed out.
        """
        if not self.available():
            self.outcome.error = "Microsoft Outlook is not available on this computer"
            return

        # Anything already running is somebody else's; see end_stranded_outlook.
        outlook_before = outlook_processes()
        try:
            records = run_with_timeout(
                lambda pulse: self._read_everything(kinds, pulse),
                timeout=self.stall_timeout,
                what=f"Outlook reading {self.path.name}",
                heartbeat=True,
            )
        except ComTimeout as exc:
            detail = str(exc)
            if end_stranded_outlook(outlook_before):
                detail += CLEANED_UP
            self.outcome.error = "Outlook stopped responding"
            self.outcome.error_detail = detail
            self.outcome.add_finding(
                "read_failure",
                "critical",
                f"{self.path.name}: Outlook stopped responding while reading it",
                detail,
                {"path": str(self.path), "backend": self.name},
            )
            return
        except ComUnavailable as exc:
            self.outcome.error = str(exc)
            return
        except Exception as exc:  # noqa: BLE001
            self.outcome.error = f"{exc.__class__.__name__}: {exc}"
            self.outcome.error_detail = repr(exc)
            return

        for record in records:
            self.outcome.yielded_count += 1
            yield _item_from_com_record(record)

    def _read_everything(self, kinds: frozenset[str] | None, pulse=None) -> list[dict]:
        """Attach the store, walk it, detach it. Detaching always happens.

        A store Outlook already has open is used where it is, and never
        detached: calling AddStore on the mailbox Outlook is currently using
        blocks indefinitely, and RemoveStore on it would disconnect the user's
        own mail.
        """
        import pythoncom
        import win32com.client

        app = win32com.client.Dispatch("Outlook.Application")
        session = app.GetNamespace("MAPI")

        if pulse is not None:
            pulse.beat()

        already_open = self._find_open_store(session)
        if already_open is not None:
            log.info("%s is already open in Outlook; reading it in place", self.path.name)
            records: list[dict] = []
            self._walk(already_open.GetRootFolder(), "", kinds, records, 0, pulse)
            return records

        before = {self._store_key(session.Stores.Item(i + 1)) for i in range(session.Stores.Count)}

        try:
            session.AddStore(str(self.path))
        except Exception as exc:  # noqa: BLE001
            message = str(exc).lower()
            if "password" in message or "encrypt" in message:
                self.outcome.add_finding(
                    "needs_password",
                    "high",
                    f"{self.path.name} is password-protected",
                    "Outlook asked for a password to open this file. Recall will "
                    "not try to guess or break it.\n\n"
                    "What to do: open the file in Outlook yourself (File, Open, "
                    "Outlook Data File), enter the password and tick "
                    '"Save this password", then use Retry on this row.\n\n'
                    f"Exact error: {exc}",
                    {"path": str(self.path)},
                )
            raise

        store = None
        try:
            for i in range(session.Stores.Count):
                candidate = session.Stores.Item(i + 1)
                if self._store_key(candidate) not in before:
                    store = candidate
                    break
            if store is None:
                raise ComUnavailable(
                    f"Outlook accepted {self.path.name} but did not list it as an "
                    "open data file, so nothing could be read from it."
                )

            self._store_id = self._store_key(store)
            if pulse is not None:
                pulse.beat()
            root = store.GetRootFolder()
            records: list[dict] = []
            self._walk(root, "", kinds, records, 0, pulse)
            return records
        finally:
            try:
                if store is not None:
                    session.RemoveStore(store.GetRootFolder())
            except Exception as exc:  # noqa: BLE001
                log.warning("Outlook did not detach %s cleanly: %s", self.path, exc)
            pythoncom.CoUninitialize  # noqa: B018 - the guard thread handles this

    def _find_open_store(self, session):
        """The store for this file, if Outlook already has it open.

        Outlook exposes each store's file path, so the match is on the path
        rather than on a name that might coincide.
        """
        target = str(self.path).casefold()
        try:
            count = int(session.Stores.Count)
        except Exception:  # noqa: BLE001
            return None

        for i in range(1, count + 1):
            try:
                store = session.Stores.Item(i)
                path = str(getattr(store, "FilePath", "") or "").casefold()
            except Exception:  # noqa: BLE001
                continue
            if path and path == target:
                return store
        return None

    @staticmethod
    def _store_key(store) -> str:
        try:
            return str(store.StoreID)
        except Exception:  # noqa: BLE001
            return str(id(store))

    def _walk(self, folder, parent_path: str, kinds, records: list[dict], depth: int, pulse=None) -> None:
        if depth > 40:
            return
        if pulse is not None:
            pulse.beat()
        try:
            name = str(folder.Name)
        except Exception:  # noqa: BLE001
            name = "(unnamed folder)"
        folder_path = f"{parent_path}/{name}" if parent_path else name
        self.outcome.folders_seen += 1

        if name.lower() not in _SKIP_FOLDERS:
            try:
                items = folder.Items
                total = int(items.Count)
            except Exception as exc:  # noqa: BLE001
                total = 0
                log.debug("Outlook could not list %s: %s", folder_path, exc)

            yielded = 0
            for index in range(1, total + 1):
                if self.sample_limit and len(records) >= self.sample_limit:
                    break
                try:
                    com_item = items.Item(index)
                    record = _com_record(com_item, folder_path)
                except Exception as exc:  # noqa: BLE001
                    self.outcome.add_finding(
                        "read_failure",
                        "critical",
                        f"{self.path.name}: Outlook could not read an item",
                        f"Folder: {folder_path}\nPosition: {index}\n"
                        f"Exact error: {exc.__class__.__name__}: {exc}",
                        {
                            "path": str(self.path),
                            "folder": folder_path,
                            "offset": index,
                            "backend": self.name,
                        },
                    )
                    continue

                if record is None:
                    continue
                if kinds is not None and record["kind"] not in kinds:
                    continue

                records.append(record)
                yielded += 1
                self.outcome.items_scanned += 1
                self.outcome.last_good_folder = folder_path
                self.outcome.last_good_offset = index
                if pulse is not None and len(records) % 25 == 0:
                    pulse.beat()

            self.outcome.folder_counts[folder_path] = (total, yielded)

        try:
            subfolders = folder.Folders
            n_sub = int(subfolders.Count)
        except Exception:  # noqa: BLE001
            return

        for index in range(1, n_sub + 1):
            if self.sample_limit and len(records) >= self.sample_limit:
                return
            try:
                sub = subfolders.Item(index)
            except Exception:  # noqa: BLE001
                continue
            self._walk(sub, folder_path, kinds, records, depth + 1, pulse)


#: Outlook's OlObjectClass values for the item types that matter.
_COM_CLASS_KIND = {
    43: Kind.MESSAGE,    # olMail
    26: Kind.EVENT,      # olAppointment
    40: Kind.CONTACT,    # olContact
    48: Kind.TASK,       # olTask
    44: Kind.NOTE,       # olNote
    53: Kind.EVENT,      # olMeetingRequest
    69: Kind.CONTACT,    # olDistributionList
}


def _com_record(com_item, folder_path: str) -> dict | None:
    """One Outlook item to a plain dictionary, off the COM thread.

    Every attribute is read defensively: Outlook raises on properties that do
    not apply to an item class, and on items whose underlying store entry is
    damaged.
    """
    def attr(name: str, default=None):
        try:
            value = getattr(com_item, name)
            return default if value is None else value
        except Exception:  # noqa: BLE001
            return default

    try:
        obj_class = int(com_item.Class)
    except Exception:  # noqa: BLE001
        return None

    kind = _COM_CLASS_KIND.get(obj_class)
    if kind is None:
        return None

    record: dict[str, Any] = {
        "kind": kind,
        "folder_path": folder_path,
        "entry_id": str(attr("EntryID", "")) or None,
        "subject": _com_text(attr("Subject")),
        "body": _com_text(attr("Body")),
        "html": _com_text(attr("HTMLBody")),
        "message_class": _com_text(attr("MessageClass")),
        "importance": {0: "low", 1: "normal", 2: "high"}.get(attr("Importance", 1)),
        "sensitivity": {0: "normal", 1: "personal", 2: "private", 3: "confidential"}.get(
            attr("Sensitivity", 0)
        ),
        "participants": [],
        "attachments": [],
    }

    if kind == Kind.EVENT:
        record["start"] = _com_time(attr("StartUTC") or attr("Start"))
        record["end"] = _com_time(attr("EndUTC") or attr("End"))
        record["all_day"] = bool(attr("AllDayEvent", False))
        record["location"] = _com_text(attr("Location"))
        record["ical_uid"] = _com_text(attr("GlobalAppointmentID"))
        record["busy_status"] = {0: "free", 1: "tentative", 2: "busy", 3: "out_of_office"}.get(
            attr("BusyStatus", 2)
        )
        record["meeting_status"] = {
            0: "non_meeting", 1: "meeting", 3: "received", 5: "canceled", 7: "received_canceled",
        }.get(attr("MeetingStatus", 0))
        record["categories"] = _com_categories(attr("Categories"))
        record["timezone"] = _com_text(attr("StartTimeZone") and attr("StartTimeZone").ID)

        organizer = _com_text(attr("Organizer"))
        if organizer:
            record["participants"].append(
                {"display_name": organizer, "address": None, "role": Role.ORGANIZER}
            )
        if attr("IsRecurring", False):
            record["recurrence"] = _com_recurrence(com_item)

    elif kind == Kind.MESSAGE:
        record["start"] = _com_time(attr("SentOn") or attr("ReceivedTime") or attr("CreationTime"))
        record["internet_message_id"] = _com_text(attr("InternetMessageID"))
        record["conversation_topic"] = _com_text(attr("ConversationTopic"))
        record["headers"] = _com_headers(com_item)
        sender = _com_text(attr("SenderEmailAddress"))
        sender_name = _com_text(attr("SenderName"))
        if sender or sender_name:
            record["participants"].append(
                {"display_name": sender_name, "address": sender, "role": Role.FROM}
            )

    elif kind == Kind.CONTACT:
        record["start"] = _com_time(attr("CreationTime"))
        record["contact"] = {
            "display_name": _com_text(attr("FullName")) or _com_text(attr("Subject")),
            "given_name": _com_text(attr("FirstName")),
            "surname": _com_text(attr("LastName")),
            "title": _com_text(attr("JobTitle")),
            "organization": _com_text(attr("CompanyName")),
            "department": _com_text(attr("Department")),
            "emails": [
                e for e in (
                    _com_text(attr("Email1Address")),
                    _com_text(attr("Email2Address")),
                    _com_text(attr("Email3Address")),
                ) if e
            ],
            "phones": {
                "business": _com_text(attr("BusinessTelephoneNumber")),
                "home": _com_text(attr("HomeTelephoneNumber")),
                "mobile": _com_text(attr("MobileTelephoneNumber")),
                "fax": _com_text(attr("BusinessFaxNumber")),
            },
            "web": _com_text(attr("WebPage")),
            "notes": _com_text(attr("Body")),
        }

    else:
        record["start"] = _com_time(attr("CreationTime"))

    # Recipients apply to mail and to meetings alike.
    try:
        recipients = com_item.Recipients
        for i in range(1, int(recipients.Count) + 1):
            r = recipients.Item(i)
            record["participants"].append(
                {
                    "display_name": _com_text(getattr(r, "Name", None)),
                    "address": _com_text(getattr(r, "Address", None)),
                    "role": {1: Role.TO, 2: Role.CC, 3: Role.BCC}.get(
                        getattr(r, "Type", 1), Role.TO
                    ) if kind == Kind.MESSAGE else Role.ATTENDEE,
                    "response_status": {
                        0: None, 1: "organizer", 2: "tentative",
                        3: "accepted", 4: "declined", 5: "not_responded",
                    }.get(getattr(r, "MeetingResponseStatus", 0)),
                }
            )
    except Exception:  # noqa: BLE001 - items without recipients raise here
        pass

    try:
        attachments = com_item.Attachments
        for i in range(1, int(attachments.Count) + 1):
            a = attachments.Item(i)
            record["attachments"].append(
                {
                    "filename": _com_text(getattr(a, "FileName", None)),
                    "size_bytes": getattr(a, "Size", None),
                    "entry_index": i,
                }
            )
    except Exception:  # noqa: BLE001
        pass

    return record


def _com_text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _com_time(value) -> str | None:
    """A COM date to an ISO UTC string. Outlook's "no date" is 4501-01-01."""
    if value is None:
        return None
    try:
        from datetime import datetime as _dt

        if isinstance(value, _dt):
            dt = value
        else:
            dt = _dt.fromtimestamp(float(value))
        if dt.year >= 4000 or dt.year < 1601:
            return None
        if dt.tzinfo is None:
            from datetime import timezone as _tz

            dt = dt.replace(tzinfo=_tz.utc)
        return dt.astimezone(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:  # noqa: BLE001
        return None


def _com_categories(value) -> list[str]:
    if not value:
        return []
    return [c.strip() for c in str(value).split(",") if c.strip()]


def _com_headers(com_item) -> str | None:
    """The full internet headers, via the MAPI property Outlook exposes."""
    try:
        accessor = com_item.PropertyAccessor
        return accessor.GetProperty(
            "http://schemas.microsoft.com/mapi/proptag/0x007D001E"
        )
    except Exception:  # noqa: BLE001
        return None


def _com_recurrence(com_item) -> dict:
    try:
        pattern = com_item.GetRecurrencePattern()
        return {
            "source": "outlook",
            "type": int(pattern.RecurrenceType),
            "interval": int(pattern.Interval),
            "occurrences": int(pattern.Occurrences),
            "no_end_date": bool(pattern.NoEndDate),
            "pattern_start": _com_time(pattern.PatternStartDate),
            "pattern_end": _com_time(pattern.PatternEndDate),
            "day_of_week_mask": int(pattern.DayOfWeekMask),
            "day_of_month": int(pattern.DayOfMonth),
            "month_of_year": int(pattern.MonthOfYear),
        }
    except Exception as exc:  # noqa: BLE001
        return {"source": "outlook", "unparsed": True, "error": str(exc)}


def _item_from_com_record(record: dict) -> ParsedItem:
    """A dictionary from the COM thread back into a ParsedItem."""
    item = ParsedItem(kind=record["kind"], backend="com", folder_path=record.get("folder_path"))
    item.native_id = record.get("entry_id")
    item.subject = record.get("subject")
    item.location = record.get("location")
    item.importance = record.get("importance")
    item.sensitivity = record.get("sensitivity")
    item.conversation_topic = record.get("conversation_topic")
    item.internet_message_id = record.get("internet_message_id")
    item.raw_headers = record.get("headers")
    item.ical_uid = record.get("ical_uid")
    item.all_day = bool(record.get("all_day"))
    item.busy_status = record.get("busy_status")
    item.meeting_status = record.get("meeting_status")
    item.categories = record.get("categories") or []
    item.contact = record.get("contact")

    html_body = record.get("html")
    plain = record.get("body")
    item.body_html = html_body
    if plain:
        item.body_text = plain
        item.body_format = "plain"
    elif html_body:
        item.body_text = html_to_text(html_body)
        item.body_format = "html"

    start = record.get("start")
    if start:
        item.occurred = TimePoint(
            utc=start, local=start[:19], tz=record.get("timezone") or "UTC", tz_known=True
        )
    else:
        item.occurred = TimePoint.unknown()
        item.note(
            "no_date",
            "Outlook reports no date for this record, so it cannot be placed on "
            "the timeline. It is kept in the Undated list.",
        )

    end = record.get("end")
    if end:
        item.end = TimePoint(utc=end, local=end[:19], tz=record.get("timezone") or "UTC", tz_known=True)

    recurrence = record.get("recurrence")
    if recurrence:
        item.recurrence = recurrence
        item.is_recurring_master = True
        if recurrence.get("unparsed"):
            item.note(
                "unresolved_recurrence",
                "This appointment repeats, but Outlook would not give up the "
                "repeat rule. Only the first occurrence is shown; no repeats have "
                "been invented.",
            )

    for p in record.get("participants", []):
        address = p.get("address")
        display = p.get("display_name")
        if not address and not display:
            continue
        item.participants.append(
            ParsedIdentity(
                address=address,
                address_type=_address_type(address, ""),
                display_name=display,
                role=p.get("role", Role.TO),
                response_status=p.get("response_status"),
            )
        )

    for a in record.get("attachments", []):
        item.attachments.append(
            ParsedAttachment(
                filename=a.get("filename"),
                size_bytes=a.get("size_bytes"),
                data=None,
                read_error=(
                    "Attachment contents are not read through Outlook automation; "
                    "the file name and size are recorded."
                ),
            )
        )

    return item


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def _recipient_identity(row: dict, kind: str) -> ParsedIdentity | None:
    """One row of the Recipients sub-item as a participant, or None.

    None rather than an empty ParsedIdentity: a row carrying neither an address
    nor a name is a row that says nothing, and ParsedIdentity refuses to be
    built from one - see its __post_init__. Some stores leave such rows behind.
    """
    address = mapi.as_text(mapi.first(row, mapi.PR_SMTP_ADDRESS, mapi.PR_EMAIL_ADDRESS))
    display_name = mapi.as_text(
        mapi.first(row, mapi.PR_RECIPIENT_DISPLAY_NAME, mapi.PR_DISPLAY_NAME)
    )
    if not address and not display_name:
        return None

    declared = (mapi.as_text(row.get(mapi.PR_ADDRTYPE)) or "").upper()

    if kind == Kind.EVENT:
        role = Role.ATTENDEE
        response_status = mapi.TRACK_STATUS.get(
            mapi.as_int(row.get(mapi.PR_RECIPIENT_TRACKSTATUS)) or 0
        )
    else:
        role = {
            "from": Role.FROM, "to": Role.TO, "cc": Role.CC, "bcc": Role.BCC,
        }.get(mapi.RECIPIENT_TYPE.get(mapi.as_int(row.get(mapi.PR_RECIPIENT_TYPE))), Role.TO)
        response_status = None

    return ParsedIdentity(
        address=address,
        address_type=_address_type(address, declared),
        display_name=display_name,
        role=role,
        response_status=response_status,
    )


def _address_type(address: str | None, declared: str) -> str:
    if address and "@" in address:
        return "smtp"
    if address and address.startswith("/"):
        return "ex"
    if declared == "EX":
        return "ex"
    if not address:
        return "none"
    return "smtp" if "@" in address else "ex"


def _subject(message, props: dict) -> str | None:
    subject = mapi.as_text(
        mapi.first(props, mapi.PR_SUBJECT, mapi.PR_NORMALIZED_SUBJECT, mapi.PR_DISPLAY_NAME)
    )
    if subject:
        return subject
    try:
        raw = message.get_subject()
        return clean_text(raw).text or None if raw else None
    except Exception:  # noqa: BLE001
        return None


@register
class PstParser(Parser):
    """The parser the rest of the program uses. Picks a backend and may switch.

    The rule from the spec: try pypff; fall back to Outlook on failure, or when
    an .ost yields nothing. If both fail the source is marked failed with the
    exact error and the run carries on.
    """

    extensions = frozenset({".pst", ".ost"})
    produces = frozenset({Kind.MESSAGE, Kind.EVENT, Kind.CONTACT, Kind.TASK, Kind.NOTE})
    name = "pst"

    def __init__(
        self,
        path: str | Path,
        *,
        sample_limit: int = 0,
        preferred: str = "auto",
        cross_check: bool = False,
        com_stall_timeout: float | None = None,
    ) -> None:
        super().__init__(path, sample_limit=sample_limit)
        self.preferred = preferred
        self.cross_check = cross_check
        self.com_stall_timeout = com_stall_timeout
        self._backend: PstBackend | None = None
        #: Item counts per backend, for the backend_disagreement check.
        self.backend_counts: dict[str, int | None] = {"pypff": None, "com": None}

    def parse(self, kinds: frozenset[str] | None = None) -> Iterator[ParsedItem]:
        order = self._backend_order()
        if not order:
            self.outcome.error = (
                "Neither the built-in reader nor Microsoft Outlook is available, "
                "so Outlook data files cannot be read at all on this computer."
            )
            raise ParserError(self.outcome.error)

        last_error: str | None = None

        for backend_cls in order:
            if backend_cls is OutlookComBackend and self.com_stall_timeout:
                backend = backend_cls(
                    self.path,
                    sample_limit=self.sample_limit,
                    stall_timeout=self.com_stall_timeout,
                )
            else:
                backend = backend_cls(self.path, sample_limit=self.sample_limit)
            if not backend.available():
                continue

            self._backend = backend
            produced = 0
            try:
                for item in backend.parse(kinds):
                    produced += 1
                    yield item
            except ParserError as exc:
                last_error = str(exc)
                log.warning("%s could not read %s: %s", backend.name, self.path, exc)
                backend.close()
                continue
            except Exception as exc:  # noqa: BLE001 - never ends the run
                last_error = f"{exc.__class__.__name__}: {exc}"
                log.warning("%s failed on %s: %s", backend.name, self.path, exc)
                backend.close()
                continue

            self.backend_counts[backend.name] = produced
            self._absorb(backend.outcome, produced)
            scanned = backend.outcome.items_scanned

            if produced > 0 or scanned > 0:
                # Either it found what was asked for, or it walked the store and
                # found records of other kinds. Both mean the store was read.
                # Falling through here because a mail-only mailbox holds no
                # calendar entries would send every extraction to Outlook and
                # turn a 0.2-second read into a ten-minute wait.
                backend.close()
                if self.cross_check and produced > 0:
                    self._cross_check(backend.name, kinds)
                return

            # Nothing at all came out. That is the documented signal to try the
            # other backend - especially for .ost, which pypff often cannot
            # read at all.
            last_error = backend.outcome.error or (
                f"{backend.name} opened the file but could not read a single "
                "record out of it"
            )
            backend.close()

        self.outcome.error = last_error or "the file could not be read"
        self.outcome.error_detail = last_error
        log.error("Every reader failed on %s: %s", self.path, self.outcome.error)

    def _backend_order(self) -> list[type[PstBackend]]:
        if self.preferred == "pypff":
            return [PyPffBackend]
        if self.preferred == "com":
            return [OutlookComBackend]
        # auto: fast first, authoritative second.
        return [PyPffBackend, OutlookComBackend]

    def _absorb(self, outcome: ParseOutcome, produced: int) -> None:
        self.outcome.backend = outcome.backend
        self.outcome.yielded_count = produced
        self.outcome.items_scanned = outcome.items_scanned
        self.outcome.folders_seen = outcome.folders_seen
        self.outcome.last_good_folder = outcome.last_good_folder
        self.outcome.last_good_offset = outcome.last_good_offset
        self.outcome.folder_counts = dict(outcome.folder_counts)
        self.outcome.findings.extend(outcome.findings)
        self.outcome.error = outcome.error
        self.outcome.error_detail = outcome.error_detail
        claimed = sum(c for c, _ in outcome.folder_counts.values())
        if claimed:
            self.outcome.claimed_count = claimed

    def _cross_check(self, used: str, kinds: frozenset[str] | None) -> None:
        """Read a sample with the other backend and compare the counts.

        Disagreement means at least one reader is missing part of the file.
        That is reported as a finding rather than quietly resolved in favour of
        whichever ran first.
        """
        other_cls = OutlookComBackend if used == "pypff" else PyPffBackend
        other = other_cls(self.path, sample_limit=0)
        if not other.available():
            return
        try:
            count = sum(1 for _ in other.parse(kinds))
            self.backend_counts[other.name] = count
        except Exception as exc:  # noqa: BLE001 - a cross-check never fails a run
            log.debug("Cross-check with %s failed on %s: %s", other.name, self.path, exc)
        finally:
            other.close()

    def close(self) -> None:
        if self._backend is not None:
            self._backend.close()
            self._backend = None
