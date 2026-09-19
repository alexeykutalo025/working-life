"""Turning archive rows into the flat records an export writes.

The calendar columns are fixed by spec section 6, and the requirement attached
to them is the demanding part: *this export alone must be usable in Excel
without further cleanup*. So:

* dates and times are separate columns, in the forms Excel parses;
* a duration is a number of minutes, not "1h 30m";
* attendees are one semicolon-separated cell, with the count beside it;
* a missing value is an empty cell or the word "unknown", never a zero, a
  placeholder date, or the string "None";
* a field whose value is uncertain says so in its own column rather than
  quietly looking certain.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterator

from ..logging_setup import get_logger

log = get_logger("export.rows")

#: Spec section 6, exactly.
CALENDAR_COLUMNS = [
    "date", "start", "end", "duration_min", "subject", "location",
    "organizer", "attendee_count", "attendees", "category", "source_file",
]

#: Everything else the archive knows about an event, for the fuller export.
CALENDAR_EXTRA_COLUMNS = [
    "all_day", "timezone", "timezone_known", "recurring", "recurrence_rule",
    "meeting_status", "busy_status", "responses", "notes", "data_quality",
    "item_id",
]

MESSAGE_COLUMNS = [
    "date", "time", "from_name", "from_address", "to", "cc", "subject",
    "has_attachments", "attachment_names", "folder", "source_file",
    "body_preview", "message_id", "data_quality", "item_id",
]

CONTACT_COLUMNS = [
    "display_name", "given_name", "surname", "organization", "title",
    "emails", "phone_business", "phone_home", "phone_mobile", "address",
    "first_seen", "last_seen", "item_count", "source_file", "item_id",
]

# Outlook tasks and sticky notes. They have no columns of their own in the
# schema - the parsers map them onto the shared item fields - so these say
# plainly what is actually known about them rather than inventing structure
# that is not there. They used to be left out of every export while still
# counting in the totals on screen, which is the one thing this program is not
# allowed to do.
TASK_COLUMNS = [
    "date", "due_date", "subject", "body", "importance", "folder",
    "source_file", "data_quality", "item_id",
]

NOTE_COLUMNS = [
    "date", "subject", "body", "folder", "source_file", "data_quality",
    "item_id",
]


def _parse(utc: str | None) -> datetime | None:
    if not utc:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(utc, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _date(utc: str | None) -> str:
    """ISO date, which every spreadsheet understands in every locale."""
    dt = _parse(utc)
    return dt.strftime("%Y-%m-%d") if dt else ""


def _time(utc: str | None) -> str:
    dt = _parse(utc)
    return dt.strftime("%H:%M") if dt else ""


def _minutes(start: str | None, end: str | None) -> str:
    a, b = _parse(start), _parse(end)
    if not a or not b:
        return ""          # not zero: an unknown duration is not a zero one
    delta = (b - a).total_seconds() / 60
    return str(int(round(delta))) if delta >= 0 else ""


def calendar_rows(conn, *, where: str = "", params: list | None = None) -> Iterator[dict]:
    """Every event, with its people, categories and provenance."""
    sql = f"""
        SELECT i.*,
               (SELECT GROUP_CONCAT(sf.path, ' | ')
                  FROM item_sources isrc
                  JOIN source_files sf ON sf.id = isrc.source_file_id
                 WHERE isrc.item_id = i.id) AS source_files,
               (SELECT GROUP_CONCAT(t.name, '; ')
                  FROM item_tags it JOIN tags t ON t.id = it.tag_id
                 WHERE it.item_id = i.id) AS categories
        FROM items i
        WHERE i.kind = 'event' {('AND ' + where) if where else ''}
        ORDER BY i.occurred_utc IS NULL, i.occurred_utc, i.id
    """
    for row in conn.execute(sql, params or []):
        yield _calendar_row(conn, row)


def _calendar_row(conn, row) -> dict[str, Any]:
    people = _participants(conn, int(row["id"]))
    organizer = next(
        (p for p in people if p["role"] == "organizer"),
        None,
    )
    attendees = [p for p in people if p["role"] in ("attendee", "optional", "resource")]

    recurrence = _json(row["recurrence_json"])
    rule_text = ""
    if recurrence:
        rule_text = (
            recurrence.get("rrule_text")
            or (json.dumps(recurrence.get("rrule")) if recurrence.get("rrule") else "")
            or ("could not be read" if recurrence.get("unparsed") else "")
        )

    quality = _quality_note(conn, int(row["id"]), row)

    return {
        "id": int(row["id"]),
        "item_id": int(row["id"]),
        "date": _date(row["occurred_utc"]),
        "start": "" if row["all_day"] else _time(row["occurred_utc"]),
        "end": "" if row["all_day"] else _time(row["end_utc"]),
        "duration_min": "" if row["all_day"] else _minutes(row["occurred_utc"], row["end_utc"]),
        "subject": row["subject"] or "",
        "location": row["location"] or "",
        "organizer": _label(organizer) if organizer else "",
        "attendee_count": len(attendees),
        "attendees": "; ".join(_label(p) for p in attendees),
        "category": row["categories"] or "",
        "source_file": row["source_files"] or "",
        "all_day": "yes" if row["all_day"] else "no",
        "timezone": row["tz"] or "unknown",
        "timezone_known": "yes" if row["tz"] else "no",
        "recurring": "yes" if row["is_recurring_master"] else "no",
        "recurrence_rule": rule_text,
        "meeting_status": row["meeting_status"] or "",
        "busy_status": row["busy_status"] or "",
        "responses": "; ".join(
            f"{_label(p)}={p['response_status']}" for p in attendees if p["response_status"]
        ),
        "notes": (row["body_text"] or "").strip(),
        "data_quality": quality,
    }


def message_rows(conn, *, where: str = "", params: list | None = None) -> Iterator[dict]:
    sql = f"""
        SELECT i.*,
               (SELECT GROUP_CONCAT(sf.path, ' | ')
                  FROM item_sources isrc
                  JOIN source_files sf ON sf.id = isrc.source_file_id
                 WHERE isrc.item_id = i.id) AS source_files,
               (SELECT f.path FROM folders f WHERE f.id = i.folder_id) AS folder_path,
               (SELECT GROUP_CONCAT(a.filename, '; ')
                  FROM attachments a WHERE a.item_id = i.id) AS attachment_names
        FROM items i
        WHERE i.kind = 'message' {('AND ' + where) if where else ''}
        ORDER BY i.occurred_utc IS NULL, i.occurred_utc, i.id
    """
    for row in conn.execute(sql, params or []):
        people = _participants(conn, int(row["id"]))
        sender = next((p for p in people if p["role"] == "from"), None)
        yield {
            "id": int(row["id"]),
            "item_id": int(row["id"]),
            "date": _date(row["occurred_utc"]),
            "time": _time(row["occurred_utc"]),
            "from_name": (sender or {}).get("display_name") or "",
            "from_address": (sender or {}).get("address") or "",
            "to": "; ".join(_label(p) for p in people if p["role"] == "to"),
            "cc": "; ".join(_label(p) for p in people if p["role"] == "cc"),
            "subject": row["subject"] or "",
            "has_attachments": "yes" if row["has_attachments"] else "no",
            "attachment_names": row["attachment_names"] or "",
            "folder": row["folder_path"] or "",
            "source_file": row["source_files"] or "",
            "body_preview": _preview(row["body_text"]),
            "message_id": row["internet_message_id"] or "",
            "data_quality": _quality_note(conn, int(row["id"]), row),
        }


def contact_rows(conn, *, where: str = "", params: list | None = None) -> Iterator[dict]:
    sql = f"""
        SELECT i.*,
               (SELECT GROUP_CONCAT(sf.path, ' | ')
                  FROM item_sources isrc
                  JOIN source_files sf ON sf.id = isrc.source_file_id
                 WHERE isrc.item_id = i.id) AS source_files
        FROM items i
        WHERE i.kind = 'contact' {('AND ' + where) if where else ''}
        ORDER BY i.subject, i.id
    """
    for row in conn.execute(sql, params or []):
        card = _json(row["contact_json"]) or {}
        phones = card.get("phones") or {}
        address = (card.get("addresses") or {}).get("business") or {}
        yield {
            "id": int(row["id"]),
            "item_id": int(row["id"]),
            "display_name": card.get("display_name") or row["subject"] or "",
            "given_name": card.get("given_name") or "",
            "surname": card.get("surname") or "",
            "organization": card.get("organization") or "",
            "title": card.get("title") or "",
            "emails": "; ".join(card.get("emails") or []),
            "phone_business": phones.get("business") or "",
            "phone_home": phones.get("home") or "",
            "phone_mobile": phones.get("mobile") or "",
            "address": ", ".join(
                v for v in (
                    address.get("street"), address.get("city"),
                    address.get("state"), address.get("postal_code"),
                    address.get("country"),
                ) if v
            ),
            "first_seen": _date(row["occurred_utc"]),
            "last_seen": "",
            "item_count": "",
            "source_file": row["source_files"] or "",
        }


def _simple_rows(conn, kind: str, *, where: str = "", params: list | None = None):
    """The shared query behind tasks and notes.

    Neither has a shape of its own in the schema, so both come out of the same
    columns; what differs is which of them is worth a spreadsheet column.
    """
    sql = f"""
        SELECT i.*,
               (SELECT GROUP_CONCAT(sf.path, ' | ')
                  FROM item_sources isrc
                  JOIN source_files sf ON sf.id = isrc.source_file_id
                 WHERE isrc.item_id = i.id) AS source_files,
               (SELECT f.path FROM folders f WHERE f.id = i.folder_id) AS folder_path
        FROM items i
        WHERE i.kind = ? {('AND ' + where) if where else ''}
        ORDER BY i.occurred_utc IS NULL, i.occurred_utc, i.id
    """
    return conn.execute(sql, [kind, *(params or [])])


def task_rows(conn, *, where: str = "", params: list | None = None) -> Iterator[dict]:
    """Outlook tasks. ``end_utc`` is where the parsers put the due date."""
    for row in _simple_rows(conn, "task", where=where, params=params):
        yield {
            "id": int(row["id"]),
            "item_id": int(row["id"]),
            "date": _date(row["occurred_utc"]),
            "due_date": _date(row["end_utc"]),
            "subject": row["subject"] or "",
            "body": _preview(row["body_text"], 2000),
            "importance": row["importance"] or "",
            "folder": row["folder_path"] or "",
            "source_file": row["source_files"] or "",
            "data_quality": _quality_note(conn, int(row["id"]), row),
        }


def note_rows(conn, *, where: str = "", params: list | None = None) -> Iterator[dict]:
    """Outlook sticky notes. A note is a date and some text; that is all."""
    for row in _simple_rows(conn, "note", where=where, params=params):
        yield {
            "id": int(row["id"]),
            "item_id": int(row["id"]),
            "date": _date(row["occurred_utc"]),
            "subject": row["subject"] or "",
            "body": _preview(row["body_text"], 2000),
            "folder": row["folder_path"] or "",
            "source_file": row["source_files"] or "",
            "data_quality": _quality_note(conn, int(row["id"]), row),
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _participants(conn, item_id: int) -> list[dict]:
    return [
        {
            "role": r["role"],
            "address": r["address"],
            "address_type": r["address_type"],
            "display_name": r["display_name"] or r["raw_display_name"],
            "response_status": r["response_status"],
        }
        for r in conn.execute(
            "SELECT p.role, p.response_status, i.address, i.address_type, "
            "i.raw_display_name, pe.display_name "
            "FROM participations p "
            "JOIN identities i ON i.id = p.identity_id "
            "LEFT JOIN people pe ON pe.id = p.person_id "
            "WHERE p.item_id = ? ORDER BY p.role, i.address",
            (item_id,),
        )
    ]


def _label(person: dict) -> str:
    """"Margaret O'Brien <mobrien@contractmktg.com>", or whichever half exists.

    An Exchange DN is shown as the raw DN. Turning it into an email address
    would be inventing one.
    """
    name = (person.get("display_name") or "").strip()
    address = (person.get("address") or "").strip()
    if name and address and name.casefold() != address.casefold():
        return f"{name} <{address}>"
    return name or address


def _preview(body: str | None, length: int = 300) -> str:
    if not body:
        return ""
    import re

    flat = re.sub(r"\s+", " ", body).strip()
    return flat if len(flat) <= length else flat[:length] + "…"


def _json(text: str | None) -> dict | None:
    if not text:
        return None
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError):
        return None


def _quality_note(conn, item_id: int, row) -> str:
    """A plain-language note on anything uncertain about this record.

    This is the honest-count rule at the level of one row: a record whose
    encoding was guessed or whose timezone is unrecorded says so in the
    spreadsheet, rather than sitting in the column looking as solid as the
    rest.
    """
    notes: list[str] = []

    if row["occurred_utc"] is None:
        notes.append("no date - not in any date range")
    if "tz" in row.keys() and not row["tz"] and row["occurred_utc"]:
        notes.append("timezone not recorded - the exact time is uncertain")
    try:
        confidence = float(row["parse_confidence"] or 1.0)
    except (TypeError, ValueError):
        confidence = 1.0
    if confidence < 1.0:
        notes.append(f"text encoding was guessed (confidence {confidence:.0%})")

    codes = [
        r["code"]
        for r in conn.execute(
            "SELECT DISTINCT code FROM findings WHERE item_id = ? "
            "AND state IN ('open','acknowledged')",
            (item_id,),
        )
    ]
    for code in codes:
        if code == "unresolved_recurrence":
            notes.append("repeat rule could not be read - only this occurrence is shown")
        elif code == "implausible_date":
            notes.append("the date on this record cannot be right, and is kept as found")
        elif code == "orphan_reply":
            notes.append("this is a reply to something not in the archive")
        elif code == "missing_blob":
            notes.append("an attachment's contents are missing from the archive")

    return "; ".join(dict.fromkeys(notes))
