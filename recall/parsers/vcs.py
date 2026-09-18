"""vCalendar 1.0 (.vcs) - the older, different specification.

Not a dialect of iCalendar. vCalendar 1.0 predates RFC 5545 and differs in ways
that break an iCalendar parser fed a .vcs file:

  * ``DTSTART:19970714T173000`` with no Z and no TZID is the norm, and the
    zone is genuinely unrecorded - so almost every .vcs event is a floating
    time. That is a fact about the format, reported as such, not smoothed over.
  * ``RRULE:W1 MO TU 19980101T000000Z`` - a positional grammar, not the
    ``FREQ=WEEKLY;BYDAY=MO,TU`` of RFC 5545.
  * ``QUOTED-PRINTABLE`` encoding on any property, which is how accented names
    survived 1997.
  * ``DALARM`` / ``AALARM`` instead of VALARM.

These files are typically the oldest calendar data a person has, which makes
them exactly the material this archive exists for, so they are parsed properly
rather than passed to the iCalendar reader and half-understood.
"""

from __future__ import annotations

import quopri
import re
from datetime import datetime
from typing import Iterator

from ..logging_setup import get_logger
from ..models import Kind, ParsedIdentity, ParsedItem, Role, TimePoint
from ..normalize.text import clean_text
from .base import Parser, register

log = get_logger("parsers.vcs")

_LINE = re.compile(r"^(?P<name>[A-Za-z0-9\-]+)(?P<params>(?:;[^:]*)*):(?P<value>.*)$")

#: vCalendar 1.0 RRULE frequency letters.
_FREQ = {
    "D": "DAILY",
    "W": "WEEKLY",
    "MP": "MONTHLY",   # by position, e.g. second Tuesday
    "MD": "MONTHLY",   # by day of month
    "YM": "YEARLY",
    "YD": "YEARLY",
}


@register
class VcsParser(Parser):
    """VEVENTs from a vCalendar 1.0 file."""

    extensions = frozenset({".vcs"})
    produces = frozenset({Kind.EVENT})
    name = "vcalendar"

    def parse(self, kinds: frozenset[str] | None = None) -> Iterator[ParsedItem]:
        if not self._wants(kinds, Kind.EVENT):
            return

        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            self.outcome.error = f"could not be opened: {exc}"
            self.outcome.error_detail = repr(exc)
            return

        decoded = clean_text(raw)
        text = _unfold(decoded.text)
        self.outcome.claimed_count = text.upper().count("BEGIN:VEVENT")

        produced = 0
        # A property that says ENCODING=QUOTED-PRINTABLE without saying CHARSET
        # is in the same encoding as the rest of the file. Detecting it again
        # from the handful of bytes in one property gets it wrong: "Premi=E8re"
        # alone looks as much like Central European as it does Western.
        for block in _blocks(text, "VEVENT", default_charset=decoded.encoding):
            if self._limit_reached(produced):
                return
            try:
                item = self._event(block, decoded.confidence)
            except Exception as exc:  # noqa: BLE001 - one bad event, not the file
                log.debug("A vCalendar entry in %s failed: %s", self.path, exc)
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

    def _event(self, lines: list[tuple[str, dict, str]], confidence: float) -> ParsedItem:
        item = ParsedItem(kind=Kind.EVENT, backend=self.name)
        item.parse_confidence = min(confidence, 0.95)  # a 1997 file is never certain
        item.body_format = "plain"

        for name, params, value in lines:
            upper = name.upper()

            if upper == "SUMMARY":
                item.subject = value
            elif upper == "LOCATION":
                item.location = value
            elif upper == "DESCRIPTION":
                item.body_text = value
            elif upper == "UID":
                item.ical_uid = value or None
                item.native_id = value or None
            elif upper == "DTSTART":
                item.occurred, item.all_day = _timepoint(value, params)
            elif upper == "DTEND":
                item.end, _ = _timepoint(value, params)
            elif upper == "CATEGORIES":
                item.categories = [c.strip() for c in value.split(",") if c.strip()]
            elif upper == "PRIORITY":
                item.importance = _priority(value)
            elif upper == "CLASS":
                item.sensitivity = value.lower() or None
            elif upper == "STATUS":
                item.meeting_status = value.lower() or None
            elif upper == "TRANSP":
                # vCalendar uses 0 for opaque (busy) and 1 for transparent.
                item.busy_status = "busy" if value.strip() == "0" else "free"
            elif upper == "ORGANIZER":
                identity = _identity(value, params, Role.ORGANIZER)
                if identity:
                    item.participants.append(identity)
            elif upper == "ATTENDEE":
                identity = _identity(value, params, Role.ATTENDEE)
                if identity:
                    item.participants.append(identity)
            elif upper == "RRULE":
                parsed = _parse_vcal_rrule(value)
                item.is_recurring_master = True
                item.recurrence = parsed
                if parsed.get("unparsed"):
                    item.note(
                        "unresolved_recurrence",
                        f"This entry repeats, but its repeat rule is in the old "
                        f"vCalendar format and could not be read: {value!r}. "
                        "The entry is shown once, on its start date; no repeats "
                        "have been invented.",
                    )

        if item.occurred.utc is None:
            item.occurred = TimePoint.unknown()
            item.note(
                "no_date",
                "This calendar entry has no start date, so it cannot be placed "
                "on the timeline. It is kept in the Undated list.",
            )
        elif not item.occurred.tz_known:
            item.note(
                "unknown_timezone",
                "vCalendar 1.0 files usually record the time without saying which "
                "timezone it was in. The date is right; the exact moment is "
                "uncertain, and no timezone has been assumed.",
            )

        return item


# ---------------------------------------------------------------------------
# format handling
# ---------------------------------------------------------------------------


def _unfold(text: str) -> str:
    """Join continuation lines. Folded lines begin with a space or tab."""
    return re.sub(r"\n[ \t]", "", text.replace("\r\n", "\n").replace("\r", "\n"))


def _blocks(
    text: str, component: str, default_charset: str | None = None
) -> Iterator[list[tuple[str, dict, str]]]:
    """Yield each component as a list of (name, params, decoded value)."""
    begin = f"BEGIN:{component}"
    end = f"END:{component}"
    current: list[tuple[str, dict, str]] | None = None

    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        upper = stripped.upper()

        if upper == begin:
            current = []
            continue
        if upper == end:
            if current is not None:
                yield current
            current = None
            continue
        if current is None:
            continue

        match = _LINE.match(stripped)
        if not match:
            continue

        name = match.group("name")
        params = _params(match.group("params") or "")
        value = _decode_value(match.group("value"), params, default_charset)
        current.append((name, params, value))


def _params(raw: str) -> dict[str, str]:
    """Property parameters, which in vCalendar are often bare words.

    ``;ENCODING=QUOTED-PRINTABLE`` and the older ``;QUOTED-PRINTABLE`` both
    occur, so a bare parameter is recorded under its own name.
    """
    out: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            key, _, value = part.partition("=")
            out[key.strip().upper()] = value.strip()
        else:
            out[part.upper()] = part.upper()
    return out


def _decode_value(
    value: str, params: dict[str, str], default_charset: str | None = None
) -> str:
    encoding = (params.get("ENCODING") or "").upper()
    quoted = encoding in ("QUOTED-PRINTABLE", "Q") or "QUOTED-PRINTABLE" in params

    if quoted:
        charset = params.get("CHARSET") or default_charset
        try:
            decoded_bytes = quopri.decodestring(value.encode("ascii", errors="replace"))
            value = clean_text(decoded_bytes, charset).text
        except Exception as exc:  # noqa: BLE001 - keep the raw text if it fails
            log.debug("quoted-printable value could not be decoded: %s", exc)

    if encoding in ("BASE64", "B"):
        import base64

        try:
            value = clean_text(base64.b64decode(value + "==")).text
        except Exception as exc:  # noqa: BLE001
            log.debug("base64 value could not be decoded: %s", exc)

    return (
        value.replace("\\n", "\n").replace("\\N", "\n")
        .replace("\\,", ",").replace("\\;", ";").strip()
    )


def _timepoint(value: str, params: dict[str, str]) -> tuple[TimePoint, bool]:
    """A vCalendar date-time. Almost always floating, and that is recorded."""
    text = value.strip()
    if not text:
        return TimePoint.unknown(), False

    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", text):
        try:
            d = datetime.strptime(text[:8], "%Y%m%d")
        except ValueError:
            return TimePoint.unknown(), False
        stamp = d.strftime("%Y-%m-%d")
        return TimePoint(f"{stamp}T00:00:00Z", f"{stamp}T00:00:00", None, True), True

    utc = text.endswith("Z")
    core = text[:-1] if utc else text

    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(core, fmt)
        except ValueError:
            continue
        if utc:
            from datetime import timezone

            return TimePoint.from_aware(dt.replace(tzinfo=timezone.utc), "UTC"), False
        tzid = params.get("TZID")
        if tzid:
            # A named zone we have no database for. The name is kept exactly as
            # written and the time is not converted, because converting it would
            # need an assumption about which zone that name means.
            stamp = dt.strftime("%Y-%m-%dT%H:%M:%S")
            return TimePoint(stamp + "Z", stamp, str(tzid), False), False
        return TimePoint.from_naive(dt), False

    return TimePoint.unknown(), False


def _parse_vcal_rrule(value: str) -> dict:
    """vCalendar 1.0 recurrence, e.g. "W1 MO TU #10" or "MD1 15 20000101T000000Z".

    Returns the interpreted rule, or ``{"unparsed": ...}`` when the grammar is
    not one of the documented forms. An uninterpretable rule is reported, never
    approximated - a wrong repeat rule invents meetings that never happened.
    """
    text = value.strip()
    if not text:
        return {"unparsed": value}

    tokens = text.split()
    head = tokens[0].upper()

    match = re.fullmatch(r"([A-Z]{1,2})(\d*)", head)
    if not match:
        return {"unparsed": value, "reason": "the rule does not start with a frequency"}

    letters, interval = match.group(1), match.group(2)
    freq = _FREQ.get(letters)
    if freq is None:
        return {"unparsed": value, "reason": f"unknown frequency {letters!r}"}

    rule: dict = {"FREQ": freq, "INTERVAL": int(interval or 1)}
    modifiers: list[str] = []

    for token in tokens[1:]:
        upper = token.upper()
        if upper.startswith("#"):
            try:
                rule["COUNT"] = int(upper[1:])
            except ValueError:
                pass
        elif re.fullmatch(r"\d{8}T\d{6}Z?", upper):
            rule["UNTIL"] = upper
        elif re.fullmatch(r"(?:\d+[+-]?)?(?:MO|TU|WE|TH|FR|SA|SU)", upper):
            modifiers.append(upper)
        elif re.fullmatch(r"\d+[+-]?", upper):
            modifiers.append(upper)
        else:
            modifiers.append(upper)

    if modifiers:
        rule["BY"] = modifiers
    return {"rrule": rule, "rrule_text": value, "source_format": "vcalendar-1.0"}


def _priority(value: str) -> str | None:
    try:
        n = int(value.strip())
    except (TypeError, ValueError):
        return None
    if n == 0:
        return None
    return "high" if n <= 4 else ("normal" if n == 5 else "low")


def _identity(value: str, params: dict[str, str], role: str) -> ParsedIdentity | None:
    address = value.strip()
    if address.lower().startswith("mailto:"):
        address = address[7:].strip()

    display = params.get("CN") or params.get("X-CN")
    if not address and not display:
        return None

    effective_role = role
    if role == Role.ATTENDEE and params.get("ROLE", "").upper() in ("OPT", "OPT-PARTICIPANT"):
        effective_role = Role.OPTIONAL

    status = params.get("STATUS") or params.get("PARTSTAT")
    address_type = "smtp" if "@" in address else ("none" if not address else "ex")

    return ParsedIdentity(
        address=address or None,
        address_type=address_type,
        display_name=display,
        role=effective_role,
        response_status=status.lower() if status else None,
    )
