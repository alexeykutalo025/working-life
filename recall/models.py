"""The normalized records every parser produces, and the vocabulary they use.

A parser's only job is to turn one file format into ``ParsedItem`` objects. All
the format-specific mess stops here; everything downstream - dedup, identity
resolution, threading, integrity checks, search - works on these types alone.

Two rules are enforced by the shape of these classes rather than by discipline:

* ``occurred_utc`` is ``None`` when there is no usable timestamp at all. It is
  never a placeholder, never epoch zero, never the parse date.
* ``tz`` is ``None`` when the source did not say what timezone applied. A record
  can have a known wall-clock time and an unknown zone; that is recorded, not
  resolved. See ``TimePoint``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class Kind(StrEnum):
    """``items.kind``. One table, one chronological spine."""

    MESSAGE = "message"
    EVENT = "event"
    CONTACT = "contact"
    TASK = "task"
    NOTE = "note"


class Role(StrEnum):
    """``participations.role``."""

    FROM = "from"
    TO = "to"
    CC = "cc"
    BCC = "bcc"
    ORGANIZER = "organizer"
    ATTENDEE = "attendee"
    OPTIONAL = "optional"
    RESOURCE = "resource"


class AddressType(StrEnum):
    """``identities.address_type``."""

    SMTP = "smtp"
    EX = "ex"      # Exchange legacyDN, e.g. /o=.../cn=Recipients/cn=tmccarthy
    PHONE = "phone"
    NONE = "none"  # a display name with no address at all


class ParseState(StrEnum):
    """``source_files.parse_state``."""

    PENDING = "pending"
    SELECTED = "selected"
    PARSING = "parsing"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class Container(StrEnum):
    """``source_files.container`` - where the file physically lives."""

    LOCAL = "local"
    ONEDRIVE = "onedrive"
    EXTERNAL = "external"
    NETWORK = "network"


class Severity(StrEnum):
    """``findings.severity``, spec section 9.5."""

    CRITICAL = "critical"  # data certainly lost
    HIGH = "high"          # data probably lost or wrong
    MEDIUM = "medium"      # uncertainty recorded
    INFO = "info"


SEVERITY_ORDER: dict[str, int] = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.INFO: 3,
}


class FindingState(StrEnum):
    """``findings.state``. Nothing ever moves here on its own."""

    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    EXPLAINED = "explained"
    RESOLVED = "resolved"
    WONT_FIX = "wont_fix"


class GapClass(StrEnum):
    """``coverage_months.gap_class``, spec section 9.2."""

    HARD_GAP = "hard_gap"
    SOFT_GAP = "soft_gap"
    SOURCE_CONTRADICTION = "source_contradiction"


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class TimePoint:
    """A moment, with the program's honesty about it attached.

    ``utc``       - the sortable instant, or None when the source gave no date.
    ``local``     - the wall-clock string exactly as the source expressed it.
    ``tz``        - the timezone name/offset the source stated, or None.
    ``tz_known``  - False means "we do not know the zone", never "assume UTC".

    A record with ``utc`` set and ``tz_known`` False is dated but not precisely
    located in time. It keeps its place on the timeline and carries an
    ``unknown_timezone`` finding, because discarding a known date to punish an
    unknown zone would lose more than it protects.
    """

    utc: str | None = None
    local: str | None = None
    tz: str | None = None
    tz_known: bool = True

    @property
    def is_dated(self) -> bool:
        return self.utc is not None

    @classmethod
    def unknown(cls) -> "TimePoint":
        """No usable timestamp. Routed to the Undated bucket."""
        return cls(utc=None, local=None, tz=None, tz_known=False)

    @classmethod
    def from_aware(cls, dt: datetime, tz_label: str | None = None) -> "TimePoint":
        """A timezone-aware datetime: fully known."""
        if dt.tzinfo is None:
            raise ValueError(
                "from_aware requires a timezone-aware datetime; "
                "use TimePoint.from_naive for wall-clock times with no zone"
            )
        from datetime import timezone

        utc = dt.astimezone(timezone.utc)
        label = tz_label or (dt.tzname() or str(dt.utcoffset()))
        return cls(
            utc=utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            local=dt.strftime("%Y-%m-%dT%H:%M:%S"),
            tz=label,
            tz_known=True,
        )

    @classmethod
    def from_naive(cls, dt: datetime) -> "TimePoint":
        """A wall-clock time with no zone. The date is kept; the zone is not guessed."""
        if dt.tzinfo is not None:
            raise ValueError("from_naive requires a naive datetime")
        stamp = dt.strftime("%Y-%m-%dT%H:%M:%S")
        return cls(utc=stamp + "Z", local=stamp, tz=None, tz_known=False)


# ---------------------------------------------------------------------------
# What a parser yields
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ParsedIdentity:
    """One address-shaped thing seen on a record."""

    address: str | None = None
    address_type: str = AddressType.SMTP
    display_name: str | None = None
    role: str = Role.TO
    response_status: str | None = None

    def __post_init__(self) -> None:
        if not self.address and not self.display_name:
            raise ValueError(
                "a participant needs at least an address or a display name; "
                "an empty participant would be an invented person"
            )


@dataclass(slots=True)
class ParsedAttachment:
    """One attachment, with its bytes if we could read them."""

    filename: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    data: bytes | None = None
    is_inline: bool = False
    content_id: str | None = None
    # Set when the attachment could not be read. Becomes an
    # `attachment_unreadable` finding rather than a silent omission.
    read_error: str | None = None


@dataclass(slots=True)
class ParsedItem:
    """One record in the archive, before it is written."""

    kind: str
    native_id: str | None = None
    folder_path: str | None = None

    occurred: TimePoint = field(default_factory=TimePoint.unknown)
    end: TimePoint | None = None
    all_day: bool = False

    subject: str | None = None
    body_text: str | None = None
    body_html: str | None = None
    body_format: str | None = None  # plain|html|rtf

    location: str | None = None
    importance: str | None = None
    sensitivity: str | None = None

    internet_message_id: str | None = None
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    conversation_topic: str | None = None

    ical_uid: str | None = None
    recurrence: dict[str, Any] | None = None
    is_recurring_master: bool = False
    recurrence_id: str | None = None
    meeting_status: str | None = None
    busy_status: str | None = None

    contact: dict[str, Any] | None = None

    participants: list[ParsedIdentity] = field(default_factory=list)
    attachments: list[ParsedAttachment] = field(default_factory=list)

    raw_headers: str | None = None
    categories: list[str] = field(default_factory=list)

    #: 1.0 means nothing was guessed. Lower means an encoding was inferred; the
    #: item is marked in the viewer and counted per source as low_confidence_text.
    parse_confidence: float = 1.0

    #: Which backend produced this item ("pypff", "com", "eml", ...).
    backend: str | None = None

    #: Parser-level notes that become findings, e.g. ("no_date", "...").
    notes: list[tuple[str, str]] = field(default_factory=list)

    def note(self, code: str, detail: str) -> None:
        self.notes.append((code, detail))

    @property
    def has_attachments(self) -> bool:
        return bool(self.attachments)


@dataclass(slots=True)
class ParseOutcome:
    """What a parser reports about a whole file when it is finished.

    ``claimed_count`` is the store's own idea of how many records it holds,
    taken from its header or its folder counts. The gap between that and
    ``yielded_count`` is ``estimated_loss`` - the number that matters to the
    user, and the reason this field exists at all.
    """

    source_path: str
    backend: str
    yielded_count: int = 0
    #: Records the parser actually reached, before any ``kinds`` filter. This is
    #: what distinguishes "this store has no calendar entries" - a true answer -
    #: from "this store could not be read", which is the only thing that should
    #: make the caller try the other backend.
    items_scanned: int = 0
    claimed_count: int | None = None
    folders_seen: int = 0
    last_good_folder: str | None = None
    last_good_offset: int | None = None
    error: str | None = None
    error_detail: str | None = None
    #: (code, severity, title, detail, evidence) tuples raised by the parser.
    findings: list[tuple[str, str, str, str, dict[str, Any]]] = field(
        default_factory=list
    )
    #: Per-folder claimed vs yielded, for folder-level estimated_loss.
    folder_counts: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def estimated_loss(self) -> int | None:
        if self.claimed_count is None:
            return None
        return max(0, self.claimed_count - self.yielded_count)

    @property
    def completeness(self) -> float | None:
        if not self.claimed_count:
            return None
        return self.yielded_count / self.claimed_count

    def add_finding(
        self,
        code: str,
        severity: str,
        title: str,
        detail: str,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        self.findings.append((code, severity, title, detail, evidence or {}))
