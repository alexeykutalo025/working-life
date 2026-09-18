"""MAPI property tags, and reading them out of pypff.

pypff exposes a handful of convenience accessors on a message - subject, sender
name, bodies, times - and nothing else. Everything that makes an appointment an
appointment, or a contact a contact, lives in MAPI properties reached through
``record_sets``. This module is the translation layer.

A record set is one row of properties. A message has one; its Recipients
sub-item has one per recipient. Each entry carries an ``entry_type`` (the MAPI
property id) and a ``value_type`` (how the bytes are encoded).

Named properties - anything at 0x8000 and above - are the awkward part. Their
ids are assigned per-file, so 0x8205 in one PST is not necessarily 0x8205 in
another. Where a named property is needed and cannot be resolved, this module
says so and the caller falls back to the Outlook COM backend rather than
reporting a value it is not sure about.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..logging_setup import get_logger
from ..normalize.text import clean_text

log = get_logger("parsers.mapi")

# ---------------------------------------------------------------------------
# Property tags, [MS-OXPROPS]
# ---------------------------------------------------------------------------

# Common
PR_MESSAGE_CLASS = 0x001A
PR_SUBJECT = 0x0037
PR_NORMALIZED_SUBJECT = 0x0E1D
PR_CONVERSATION_TOPIC = 0x0070
PR_BODY = 0x1000
PR_HTML = 0x1013
PR_RTF_COMPRESSED = 0x1009
PR_IMPORTANCE = 0x0017
PR_SENSITIVITY = 0x0036
PR_PRIORITY = 0x0026
PR_CREATION_TIME = 0x3007
PR_LAST_MODIFICATION_TIME = 0x3008
PR_MESSAGE_FLAGS = 0x0E07
PR_MESSAGE_SIZE = 0x0E08
PR_DISPLAY_NAME = 0x3001
PR_INTERNET_CPID = 0x3FDE
PR_MESSAGE_CODEPAGE = 0x3FFD

# Mail
PR_CLIENT_SUBMIT_TIME = 0x0039
PR_MESSAGE_DELIVERY_TIME = 0x0E06
PR_INTERNET_MESSAGE_ID = 0x1035
PR_IN_REPLY_TO_ID = 0x1042
PR_INTERNET_REFERENCES = 0x1039
PR_TRANSPORT_MESSAGE_HEADERS = 0x007D
PR_SENDER_NAME = 0x0C1A
PR_SENDER_EMAIL_ADDRESS = 0x0C1F
PR_SENDER_ADDRTYPE = 0x0C1E
PR_SENDER_SMTP_ADDRESS = 0x5D01
PR_SENT_REPRESENTING_NAME = 0x0042
PR_SENT_REPRESENTING_EMAIL_ADDRESS = 0x0065
PR_SENT_REPRESENTING_ADDRTYPE = 0x0064
PR_SENT_REPRESENTING_SMTP_ADDRESS = 0x5D02
PR_DISPLAY_TO = 0x0E04
PR_DISPLAY_CC = 0x0E03
PR_DISPLAY_BCC = 0x0E02
PR_HASATTACH = 0x0E1B

# Recipients
PR_RECIPIENT_TYPE = 0x0C15
PR_EMAIL_ADDRESS = 0x3003
PR_ADDRTYPE = 0x3002
PR_SMTP_ADDRESS = 0x39FE
PR_RECIPIENT_DISPLAY_NAME = 0x5FF6
PR_RECIPIENT_TRACKSTATUS = 0x5FFF
PR_7BIT_DISPLAY_NAME = 0x39FF

# Appointments. PR_START_DATE / PR_END_DATE are the ordinary tagged properties
# Outlook does set on appointments, which is why they are tried before the
# named ones.
PR_START_DATE = 0x0060
PR_END_DATE = 0x0061
PR_OWNER_APPT_ID = 0x0062
PR_RESPONSE_REQUESTED = 0x0063

# Contacts
PR_GIVEN_NAME = 0x3A06
PR_SURNAME = 0x3A11
PR_MIDDLE_NAME = 0x3A44
PR_NICKNAME = 0x3A4F
PR_TITLE = 0x3A17
PR_COMPANY_NAME = 0x3A16
PR_DEPARTMENT_NAME = 0x3A18
PR_OFFICE_LOCATION = 0x3A19
PR_PROFESSION = 0x3A46
PR_BUSINESS_TELEPHONE_NUMBER = 0x3A08
PR_HOME_TELEPHONE_NUMBER = 0x3A09
PR_MOBILE_TELEPHONE_NUMBER = 0x3A1C
PR_BUSINESS_FAX_NUMBER = 0x3A24
PR_OTHER_TELEPHONE_NUMBER = 0x3A1F
PR_PRIMARY_TELEPHONE_NUMBER = 0x3A1A
PR_BUSINESS_ADDRESS_STREET = 0x3A29
PR_BUSINESS_ADDRESS_CITY = 0x3A27
PR_BUSINESS_ADDRESS_STATE = 0x3A28
PR_BUSINESS_ADDRESS_POSTAL_CODE = 0x3A2A
PR_BUSINESS_ADDRESS_COUNTRY = 0x3A26
PR_HOME_ADDRESS_STREET = 0x3A5D
PR_HOME_ADDRESS_CITY = 0x3A59
PR_HOME_ADDRESS_STATE = 0x3A5C
PR_HOME_ADDRESS_POSTAL_CODE = 0x3A5B
PR_HOME_ADDRESS_COUNTRY = 0x3A5A
PR_BIRTHDAY = 0x3A42
PR_WEDDING_ANNIVERSARY = 0x3A41
PR_SPOUSE_NAME = 0x3A48
PR_PERSONAL_HOME_PAGE = 0x3A50
PR_BUSINESS_HOME_PAGE = 0x3A51
PR_EMAIL_1_ADDRESS = 0x8083   # named, id varies - see resolve_named
PR_NOTE_BODY = 0x1000

# Attachments
PR_ATTACH_FILENAME = 0x3704
PR_ATTACH_LONG_FILENAME = 0x3707
PR_ATTACH_EXTENSION = 0x3703
PR_ATTACH_SIZE = 0x0E20
PR_ATTACH_DATA_BINARY = 0x3701
PR_ATTACH_METHOD = 0x3705
PR_ATTACH_MIME_TAG = 0x370E
PR_ATTACH_CONTENT_ID = 0x3712
PR_ATTACHMENT_HIDDEN = 0x7FFE
PR_ATTACH_FLAGS = 0x3714
PR_RENDERING_POSITION = 0x370B

# Folders
PR_CONTENT_COUNT = 0x3602
PR_CONTENT_UNREAD = 0x3603
PR_SUBFOLDERS = 0x360A

# Value types, [MS-OXCDATA]
PT_UNSPECIFIED = 0x0000
PT_NULL = 0x0001
PT_SHORT = 0x0002
PT_LONG = 0x0003
PT_FLOAT = 0x0004
PT_DOUBLE = 0x0005
PT_BOOLEAN = 0x000B
PT_LONGLONG = 0x0014
PT_STRING8 = 0x001E
PT_UNICODE = 0x001F
PT_SYSTIME = 0x0040
PT_CLSID = 0x0048
PT_BINARY = 0x0102
PT_MV_STRING8 = 0x101E
PT_MV_UNICODE = 0x101F

#: Recipient types, PR_RECIPIENT_TYPE.
RECIPIENT_TYPE = {0: "from", 1: "to", 2: "cc", 3: "bcc"}

#: PR_RECIPIENT_TRACKSTATUS, the meeting response.
TRACK_STATUS = {
    0: None,
    1: "organizer",
    2: "tentative",
    3: "accepted",
    4: "declined",
    5: "not_responded",
}

#: PR_IMPORTANCE
IMPORTANCE = {0: "low", 1: "normal", 2: "high"}

#: PR_SENSITIVITY
SENSITIVITY = {0: "normal", 1: "personal", 2: "private", 3: "confidential"}

#: PR_ATTACH_METHOD
ATTACH_BY_VALUE = 1
ATTACH_EMBEDDED_MSG = 5

#: Message classes, the "what kind of thing is this" property.
MESSAGE_CLASS_KINDS = {
    "IPM.NOTE": "message",
    "IPM.APPOINTMENT": "event",
    "IPM.SCHEDULE.MEETING": "event",
    "IPM.CONTACT": "contact",
    "IPM.DISTLIST": "contact",
    "IPM.TASK": "task",
    "IPM.STICKYNOTE": "note",
    "IPM.ACTIVITY": "note",
    "IPM.POST": "message",
    "REPORT": "message",
}

#: Windows FILETIME epoch: 1601-01-01.
_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


class MapiError(Exception):
    """A MAPI property could not be read. The message names the property."""


def kind_for_message_class(message_class: str | None) -> str:
    """"IPM.Appointment.Recurring" to "event". Unknown classes become messages.

    Falling back to "message" is a deliberate choice: an unrecognised item is
    kept and made searchable rather than dropped, and its exact class is stored
    in the raw headers so nothing about it is lost.
    """
    if not message_class:
        return "message"
    upper = message_class.upper()
    for prefix, kind in MESSAGE_CLASS_KINDS.items():
        if upper.startswith(prefix):
            return kind
    return "message"


def filetime_to_utc(value: int) -> str | None:
    """A Windows FILETIME to an ISO UTC string, or None when it is not a time.

    Zero and the maximum value both mean "not set" in MAPI, and both would
    otherwise become dates in 1601 or 30828 - exactly the invented timestamps
    the spec forbids.
    """
    if not value or value <= 0:
        return None
    try:
        dt = _FILETIME_EPOCH + timedelta(microseconds=value // 10)
    except (OverflowError, ValueError):
        return None
    if dt.year < 1601 or dt.year > 2999:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Reading record sets
# ---------------------------------------------------------------------------


def read_properties(item, *, codepage_hint: str | None = None) -> dict[int, Any]:
    """Every MAPI property on a pypff item, keyed by property id.

    Never raises for one unreadable property: that entry is skipped and noted
    in the log, because one property Recall cannot decode must not cost the
    whole message.
    """
    props: dict[int, Any] = {}
    try:
        n_sets = item.number_of_record_sets
    except Exception as exc:  # noqa: BLE001
        log.debug("Item has no readable record sets: %s", exc)
        return props

    for set_index in range(n_sets):
        try:
            record_set = item.get_record_set(set_index)
            n_entries = record_set.number_of_entries
        except Exception as exc:  # noqa: BLE001
            log.debug("Record set %d could not be read: %s", set_index, exc)
            continue

        for entry_index in range(n_entries):
            try:
                entry = record_set.get_entry(entry_index)
                tag = entry.entry_type
                value = _entry_value(entry, codepage_hint)
            except Exception as exc:  # noqa: BLE001
                log.debug("A property in record set %d was unreadable: %s", set_index, exc)
                continue
            if value is not None and tag not in props:
                props[tag] = value
    return props


def read_record_sets(item, *, codepage_hint: str | None = None) -> list[dict[int, Any]]:
    """One dict per record set - used for recipients, where each row is a person."""
    out: list[dict[int, Any]] = []
    try:
        n_sets = item.number_of_record_sets
    except Exception:  # noqa: BLE001
        return out

    for set_index in range(n_sets):
        row: dict[int, Any] = {}
        try:
            record_set = item.get_record_set(set_index)
            n_entries = record_set.number_of_entries
        except Exception:  # noqa: BLE001
            continue
        for entry_index in range(n_entries):
            try:
                entry = record_set.get_entry(entry_index)
                value = _entry_value(entry, codepage_hint)
                if value is not None:
                    row[entry.entry_type] = value
            except Exception:  # noqa: BLE001
                continue
        if row:
            out.append(row)
    return out


def _entry_value(entry, codepage_hint: str | None) -> Any:
    """One property entry to a Python value, decided by its MAPI value type."""
    value_type = entry.value_type

    if value_type in (PT_UNICODE, PT_STRING8, PT_MV_UNICODE, PT_MV_STRING8):
        return _string_value(entry, value_type, codepage_hint)

    if value_type in (PT_SHORT, PT_LONG, PT_LONGLONG):
        try:
            return entry.get_data_as_integer()
        except Exception:  # noqa: BLE001
            return None

    if value_type == PT_BOOLEAN:
        try:
            return bool(entry.get_data_as_boolean())
        except Exception:  # noqa: BLE001
            return None

    if value_type in (PT_FLOAT, PT_DOUBLE):
        try:
            return entry.get_data_as_floating_point()
        except Exception:  # noqa: BLE001
            return None

    if value_type == PT_SYSTIME:
        try:
            dt = entry.get_data_as_datetime()
            if dt is None:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            # MAPI times are stored in UTC. A 1601 date means "not set".
            if dt.year < 1602 or dt.year > 2999:
                return None
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:  # noqa: BLE001
            try:
                return filetime_to_utc(entry.get_data_as_integer())
            except Exception:  # noqa: BLE001
                return None

    if value_type == PT_BINARY:
        try:
            return entry.get_data()
        except Exception:  # noqa: BLE001
            return None

    try:
        return entry.get_data()
    except Exception:  # noqa: BLE001
        return None


def _string_value(entry, value_type: int, codepage_hint: str | None) -> str | None:
    """A string property, decoded with the store's codepage as a hint.

    PT_STRING8 is bytes in whatever ANSI codepage the store was written with -
    which for a 1998 PST is cp1252, and for others is not. The declared
    codepage is used as a hint and the normal repair pipeline does the rest.
    """
    try:
        text = entry.get_data_as_string()
        if text:
            return clean_text(text).text
    except Exception:  # noqa: BLE001 - fall through to the raw bytes
        pass

    try:
        raw = entry.get_data()
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None

    if value_type in (PT_UNICODE, PT_MV_UNICODE):
        try:
            return clean_text(raw.decode("utf-16-le").rstrip("\x00")).text
        except (UnicodeDecodeError, AttributeError):
            pass

    if isinstance(raw, bytes):
        return clean_text(raw.rstrip(b"\x00"), codepage_hint).text
    return str(raw)


# ---------------------------------------------------------------------------
# Named properties
# ---------------------------------------------------------------------------

#: The named properties an appointment needs, by their [MS-OXOCAL] names.
APPOINTMENT_NAMED = {
    0x820D: "ApptStartWhole",
    0x820E: "ApptEndWhole",
    0x8208: "Location",
    0x8205: "BusyStatus",
    0x8215: "AllDayEvent",
    0x8216: "AppointmentRecur",
    0x8217: "AppointmentStateFlags",
    0x8218: "ResponseStatus",
    0x8223: "IsRecurring",
    0x8228: "ClipStart",
    0x8229: "ClipEnd",
    0x8231: "RecurrenceType",
    0x8232: "RecurrencePattern",
    0x8234: "TimeZoneDescription",
    0x001C: "GlobalObjectId",
}

CONTACT_NAMED = {
    0x8005: "FileUnder",
    0x8083: "Email1EmailAddress",
    0x8080: "Email1DisplayName",
    0x8082: "Email1AddressType",
    0x8093: "Email2EmailAddress",
    0x8090: "Email2DisplayName",
    0x80A3: "Email3EmailAddress",
    0x80A0: "Email3DisplayName",
    0x8062: "InstantMessagingAddress",
}


def named_property_ids(pff_file) -> dict[str, int] | None:
    """Map named-property names to the ids this particular file uses.

    Named property ids are assigned per-file, so the same appointment field has
    different ids in different PSTs. pypff exposes the map but does not always
    populate it; ``None`` means "could not be resolved", and the caller falls
    back to Outlook rather than reading a property it cannot identify.
    """
    try:
        name_map = pff_file.get_name_to_id_map()
    except Exception as exc:  # noqa: BLE001
        log.debug("This file's named-property map could not be read: %s", exc)
        return None
    if name_map is None:
        return None

    out: dict[str, int] = {}
    try:
        for i in range(name_map.get_number_of_entries()):
            entry = name_map.get_entry(i)
            name = getattr(entry, "name", None)
            number = getattr(entry, "number", None)
            identifier = getattr(entry, "entry_type", None)
            key = name if name else (f"0x{number:04x}" if number is not None else None)
            if key and identifier is not None:
                out[str(key)] = int(identifier)
    except Exception as exc:  # noqa: BLE001
        log.debug("This file's named-property map could not be walked: %s", exc)
        return out or None
    return out or None


def first(props: dict[int, Any], *tags: int) -> Any:
    """The first of these properties that is present and not empty."""
    for tag in tags:
        value = props.get(tag)
        if value not in (None, "", b""):
            return value
    return None


def as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return clean_text(value).text or None
    text = str(value).strip()
    return text or None


def as_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
