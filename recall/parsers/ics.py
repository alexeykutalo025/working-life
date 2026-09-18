"""iCalendar (.ics) - RFC 5545.

Timezones are the whole difficulty here, and the spec's rule is absolute:
never assume one. iCalendar has three kinds of time and they are treated
differently because they mean different things:

  DTSTART:19970714T173000Z          UTC. Exact instant, fully known.
  DTSTART;TZID=Europe/Dublin:...    Local time in a named zone. Convertible.
  DTSTART:19970714T173000           "Floating" time - 5:30pm wherever you are.
                                    The date is known; the instant is not.
  DTSTART;VALUE=DATE:19970714       An all-day event. No time at all.

A floating time keeps its date and is marked ``tz_known=False``, which raises an
``unknown_timezone`` finding. Discarding a known date because the zone is
missing would lose more than it protects; silently calling it UTC would be the
guess the spec forbids.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Iterator

from ..logging_setup import get_logger
from ..models import Kind, ParsedIdentity, ParsedItem, Role, TimePoint
from ..normalize.text import clean_text
from .base import Parser, ParserError, register

log = get_logger("parsers.ics")


@register
class IcsParser(Parser):
    """One or many VEVENTs from an iCalendar file."""

    extensions = frozenset({".ics"})
    produces = frozenset({Kind.EVENT})
    name = "icalendar"

    def parse(self, kinds: frozenset[str] | None = None) -> Iterator[ParsedItem]:
        if not self._wants(kinds, Kind.EVENT):
            return

        try:
            from icalendar import Calendar
        except ImportError as exc:
            self.outcome.error = "the icalendar library is not installed"
            self.outcome.error_detail = str(exc)
            raise ParserError(
                "Calendar files (.ics) cannot be read because the icalendar "
                "library is not installed. Install it with:  pip install icalendar"
            ) from exc

        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            self.outcome.error = f"could not be opened: {exc}"
            self.outcome.error_detail = repr(exc)
            return

        decoded = clean_text(raw)
        confidence = decoded.confidence

        # A file may hold several concatenated VCALENDARs - Outlook exports do
        # this. from_ical returns only the first, so they are split by hand.
        blocks = _split_calendars(decoded.text)
        self.outcome.claimed_count = sum(
            b.upper().count("BEGIN:VEVENT") for b in blocks
        )

        produced = 0
        for block in blocks:
            try:
                calendar = Calendar.from_ical(block)
            except Exception as exc:  # noqa: BLE001 - malformed files are common
                self.outcome.error = f"the calendar could not be understood: {exc}"
                self.outcome.error_detail = repr(exc)
                self.outcome.add_finding(
                    "read_failure",
                    "critical",
                    f"{self.path.name} is not a calendar file Recall can read",
                    f"The file begins like a calendar but could not be parsed.\n\n"
                    f"Exact error: {exc}",
                    {"path": str(self.path)},
                )
                continue

            for component in calendar.walk("VEVENT"):
                if self._limit_reached(produced):
                    return
                try:
                    item = self._event(component, confidence)
                except Exception as exc:  # noqa: BLE001 - one bad event, not the file
                    log.debug("An event in %s could not be read: %s", self.path, exc)
                    self.outcome.add_finding(
                        "read_failure",
                        "high",
                        f"An entry in {self.path.name} could not be read",
                        f"One calendar entry was skipped.\n\nExact error: {exc}",
                        {"path": str(self.path)},
                    )
                    continue
                if item is not None:
                    produced += 1
                    self.outcome.yielded_count = produced
                    yield item

    # -- one event -------------------------------------------------------

    def _event(self, component, base_confidence: float) -> ParsedItem | None:
        item = ParsedItem(kind=Kind.EVENT, backend=self.name)
        item.parse_confidence = base_confidence

        item.subject = _text(component.get("SUMMARY"))
        item.location = _text(component.get("LOCATION"))
        item.body_text = _text(component.get("DESCRIPTION"))
        item.body_format = "plain"
        item.ical_uid = _text(component.get("UID")) or None
        item.native_id = item.ical_uid

        start_prop = component.get("DTSTART")
        if start_prop is None:
            item.occurred = TimePoint.unknown()
            item.note(
                "no_date",
                "This calendar entry has no start date at all, so it cannot be "
                "placed on the timeline. It is kept in the Undated list.",
            )
        else:
            item.occurred, all_day = _timepoint(start_prop)
            item.all_day = all_day
            if not item.occurred.tz_known and item.occurred.utc:
                item.note(
                    "unknown_timezone",
                    "This entry gives a time but not which timezone it is in, so "
                    "the exact moment is uncertain. The date is right.",
                )

        end_prop = component.get("DTEND")
        if end_prop is not None:
            item.end, _ = _timepoint(end_prop)
        elif component.get("DURATION") is not None and item.occurred.utc:
            item.end = _end_from_duration(item.occurred, component.get("DURATION"))

        # Recurrence: the rule is stored, the instances are not. Expanding a
        # weekly meeting held for eleven years into the database would bury the
        # archive in rows that are not records of anything that happened.
        rrule = component.get("RRULE")
        if rrule is not None:
            try:
                item.recurrence = {
                    "rrule": _rrule_to_dict(rrule),
                    "rrule_text": rrule.to_ical().decode("ascii", errors="replace"),
                    "exdates": _exdates(component),
                }
                item.is_recurring_master = True
            except Exception as exc:  # noqa: BLE001
                item.recurrence = {"unparsed": str(rrule), "error": str(exc)}
                item.is_recurring_master = True
                item.note(
                    "unresolved_recurrence",
                    f"This entry repeats, but the repeat rule could not be read "
                    f"({exc}). The entry is shown once, on its start date; no "
                    "repeats have been invented.",
                )

        recurrence_id = component.get("RECURRENCE-ID")
        if recurrence_id is not None:
            rid, _ = _timepoint(recurrence_id)
            item.recurrence_id = rid.utc

        item.meeting_status = _text(component.get("STATUS"))
        item.busy_status = _text(component.get("TRANSP"))
        item.importance = _priority(component.get("PRIORITY"))
        item.sensitivity = _text(component.get("CLASS"))
        item.categories = _categories(component)

        organizer = component.get("ORGANIZER")
        if organizer is not None:
            identity = _identity(organizer, Role.ORGANIZER)
            if identity:
                item.participants.append(identity)

        attendees = component.get("ATTENDEE")
        if attendees is not None:
            for attendee in (attendees if isinstance(attendees, list) else [attendees]):
                identity = _identity(attendee, Role.ATTENDEE)
                if identity:
                    item.participants.append(identity)

        return item


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _split_calendars(text: str) -> list[str]:
    """Separate concatenated VCALENDAR blocks."""
    upper = text.upper()
    starts = []
    idx = upper.find("BEGIN:VCALENDAR")
    while idx != -1:
        starts.append(idx)
        idx = upper.find("BEGIN:VCALENDAR", idx + 1)

    if len(starts) <= 1:
        return [text] if text.strip() else []

    blocks = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        blocks.append(text[start:end])
    return blocks


def _text(value) -> str | None:
    if value is None:
        return None
    try:
        raw = value.to_ical()
    except AttributeError:
        raw = value
    if isinstance(raw, bytes):
        raw = clean_text(raw).text
    text = str(raw).strip()
    # icalendar leaves RFC 5545 escapes in place for TEXT values.
    text = (
        text.replace("\\n", "\n").replace("\\N", "\n")
        .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
    )
    return text or None


def _timepoint(prop) -> tuple[TimePoint, bool]:
    """An iCalendar date-time property to a TimePoint. Returns (point, all_day)."""
    value = getattr(prop, "dt", prop)

    if isinstance(value, datetime):
        if value.tzinfo is not None:
            label = _tz_label(prop, value)
            return TimePoint.from_aware(value, label), False
        # Floating time: the wall clock is known, the zone is not.
        return TimePoint.from_naive(value), False

    if isinstance(value, date):
        # An all-day event has no time and no zone. Midnight is the convention
        # for ordering it, not a claim about when it started.
        stamp = value.strftime("%Y-%m-%d")
        return (
            TimePoint(
                utc=f"{stamp}T00:00:00Z",
                local=f"{stamp}T00:00:00",
                tz=None,
                tz_known=True,   # an all-day event has no time to be unsure about
            ),
            True,
        )

    return TimePoint.unknown(), False


def _tz_label(prop, value: datetime) -> str:
    """The timezone as the file stated it - TZID if given, else the offset."""
    params = getattr(prop, "params", {}) or {}
    tzid = params.get("TZID")
    if tzid:
        return str(tzid)
    if value.tzinfo == timezone.utc:
        return "UTC"
    return value.tzname() or str(value.utcoffset())


def _end_from_duration(start: TimePoint, duration_prop) -> TimePoint | None:
    try:
        delta = getattr(duration_prop, "dt", None)
        if delta is None or start.utc is None:
            return None
        begin = datetime.strptime(start.utc, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        return TimePoint.from_aware(begin + delta, start.tz or "UTC")
    except (ValueError, TypeError):
        return None


def _rrule_to_dict(rrule) -> dict:
    out: dict = {}
    for key, value in rrule.items():
        if isinstance(value, list):
            out[key] = [_scalar(v) for v in value]
        else:
            out[key] = _scalar(value)
    return out


def _scalar(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (int, float, str)):
        return value
    return str(value)


def _exdates(component) -> list[str]:
    """Dates removed from a recurring series - the cancelled instances."""
    out: list[str] = []
    exdate = component.get("EXDATE")
    if exdate is None:
        return out
    for entry in (exdate if isinstance(exdate, list) else [exdate]):
        for dt in getattr(entry, "dts", []):
            value = getattr(dt, "dt", None)
            if value is not None:
                out.append(value.isoformat())
    return out


def _categories(component) -> list[str]:
    cats = component.get("CATEGORIES")
    if cats is None:
        return []
    out: list[str] = []
    for entry in (cats if isinstance(cats, list) else [cats]):
        for cat in getattr(entry, "cats", []):
            out.append(str(cat))
    return out


def _priority(value) -> str | None:
    """RFC 5545 priority 1-9 to the words Outlook uses."""
    if value is None:
        return None
    try:
        n = int(str(value))
    except (TypeError, ValueError):
        return None
    if n == 0:
        return None
    if n <= 4:
        return "high"
    if n == 5:
        return "normal"
    return "low"


def _identity(prop, role: str) -> ParsedIdentity | None:
    """An ORGANIZER or ATTENDEE property to a participant."""
    raw = str(prop)
    address = raw
    if ":" in raw:
        scheme, _, rest = raw.partition(":")
        if scheme.lower() in ("mailto", "http", "https"):
            address = rest
    address = address.strip()

    params = getattr(prop, "params", {}) or {}
    display = params.get("CN")
    display = str(display).strip() if display else None

    if not address and not display:
        return None

    partstat = params.get("PARTSTAT")
    cutype = params.get("CUTYPE")
    effective_role = role
    if role == Role.ATTENDEE:
        if str(params.get("ROLE", "")).upper() == "OPT-PARTICIPANT":
            effective_role = Role.OPTIONAL
        elif str(cutype or "").upper() in ("RESOURCE", "ROOM"):
            effective_role = Role.RESOURCE

    address_type = "smtp" if "@" in address else ("none" if not address else "ex")

    return ParsedIdentity(
        address=address or None,
        address_type=address_type,
        display_name=display,
        role=effective_role,
        response_status=str(partstat).lower() if partstat else None,
    )
