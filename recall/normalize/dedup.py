"""Deciding when two records are the same record.

The archive is built from overlapping backups: the same message can appear in a
2003 PST, a 2011 OST and a folder of .msg files exported in between. All three
are the same message and it should appear once - but the fact that it was found
in three places is itself information worth keeping, so the item is stored once
and an ``item_sources`` row is written for every place it was found.

The keys are exactly as specified in sections 6 and 8. They are deterministic:
the same input always gives the same key, so re-running an extraction collapses
onto the existing rows instead of doubling the archive.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

from ..models import ParsedItem
from .text import normalize_subject


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def normalize_address(address: str | None) -> str:
    """Lowercase, trimmed, with angle brackets and display name removed."""
    if not address:
        return ""
    text = address.strip()
    match = re.search(r"<([^>]+)>", text)
    if match:
        text = match.group(1)
    return unicodedata.normalize("NFKC", text).strip().strip("<>").casefold()


def minute_stamp(utc: str | None) -> str:
    """A UTC timestamp truncated to the minute.

    Seconds are dropped because the same message re-saved through different
    Outlook versions can differ by a second in its stored time, and that must
    not make it look like two messages.
    """
    if not utc:
        return ""
    return utc[:16]  # YYYY-MM-DDTHH:MM


# ---------------------------------------------------------------------------
# Events (spec section 6)
# ---------------------------------------------------------------------------


def event_dedup_key(
    ical_uid: str | None,
    recurrence_id: str | None,
    start_utc: str | None,
    *,
    subject: str | None = None,
    organizer: str | None = None,
) -> str:
    """sha256(ical_uid + recurrence_id + start_utc).

    When the UID is missing - which older .vcs files and some PST appointments
    do not carry - the fallback is
    sha256(normalized_subject + start_utc + normalized_organizer).
    """
    if ical_uid:
        return _sha256(f"event|{ical_uid.strip()}|{recurrence_id or ''}|{start_utc or ''}")
    return _sha256(
        f"event-nouid|{normalize_subject(subject)}|{start_utc or ''}"
        f"|{normalize_address(organizer)}"
    )


# ---------------------------------------------------------------------------
# Mail (spec section 8)
# ---------------------------------------------------------------------------


def message_dedup_key(
    internet_message_id: str | None,
    *,
    sender: str | None = None,
    recipients: list[str] | None = None,
    occurred_utc: str | None = None,
    subject: str | None = None,
    body_text: str | None = None,
) -> str:
    """sha256(internet_message_id) when there is one; the composite otherwise.

    The composite is
    sha256(from + sorted recipients + time-to-the-minute + subject + body[:2000]).
    Recipients are sorted so that a To/Cc order shuffled by one mail client does
    not produce a second copy of the same message.
    """
    if internet_message_id and internet_message_id.strip():
        return _sha256(f"msgid|{internet_message_id.strip().strip('<>').casefold()}")

    addresses = sorted({normalize_address(r) for r in (recipients or []) if r})
    body = (body_text or "")[:2000]
    body = re.sub(r"\s+", " ", body).strip()

    return _sha256(
        "msg|"
        + normalize_address(sender)
        + "|" + ",".join(addresses)
        + "|" + minute_stamp(occurred_utc)
        + "|" + normalize_subject(subject)
        + "|" + body
    )


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------


def contact_dedup_key(
    *,
    emails: list[str] | None = None,
    display_name: str | None = None,
    organization: str | None = None,
    phones: list[str] | None = None,
) -> str:
    """A contact card is identified by its addresses, then by name and company.

    Two cards for the same person from different address books collapse; two
    different people who happen to share a name do not, because the company and
    the phone numbers are part of the key.
    """
    addresses = sorted({normalize_address(e) for e in (emails or []) if e})
    if addresses:
        return _sha256("contact|" + ",".join(addresses))

    digits = sorted({re.sub(r"\D", "", p) for p in (phones or []) if p and re.sub(r"\D", "", p)})
    name = normalize_whitespace_casefold(display_name)
    org = normalize_whitespace_casefold(organization)
    if not name and not org and not digits:
        raise ValueError(
            "a contact with no address, name, organisation or telephone number "
            "cannot be identified, and inventing an identity for it would be a "
            "guess"
        )
    return _sha256(f"contact-noaddr|{name}|{org}|{','.join(digits)}")


def normalize_whitespace_casefold(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip().casefold()


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def dedup_key_for(item: ParsedItem) -> str:
    """The right key for whatever kind this item is."""
    from ..models import Kind, Role

    if item.kind == Kind.EVENT:
        organizer = next(
            (p.address for p in item.participants if p.role == Role.ORGANIZER), None
        )
        return event_dedup_key(
            item.ical_uid,
            item.recurrence_id,
            item.occurred.utc,
            subject=item.subject,
            organizer=organizer,
        )

    if item.kind == Kind.CONTACT:
        card = item.contact or {}
        return contact_dedup_key(
            emails=card.get("emails") or [
                p.address for p in item.participants if p.address
            ],
            display_name=card.get("display_name") or item.subject,
            organization=card.get("organization"),
            phones=[v for v in (card.get("phones") or {}).values() if v]
            if isinstance(card.get("phones"), dict)
            else card.get("phones"),
        )

    if item.kind in (Kind.MESSAGE, Kind.TASK, Kind.NOTE):
        sender = next((p.address for p in item.participants if p.role == Role.FROM), None)
        recipients = [
            p.address
            for p in item.participants
            if p.role in (Role.TO, Role.CC, Role.BCC) and p.address
        ]
        return message_dedup_key(
            item.internet_message_id,
            sender=sender,
            recipients=recipients,
            occurred_utc=item.occurred.utc,
            subject=item.subject,
            body_text=item.body_text,
        )

    raise ValueError(
        f"{item.kind!r} is not a kind Recall knows how to identify. "
        "Every record must have a dedup key, or re-running an extraction would "
        "duplicate it."
    )
