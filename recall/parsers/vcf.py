"""vCard (.vcf) - contact cards, versions 2.1, 3.0 and 4.0.

The three versions differ in ways that matter for a forty-year archive:

  2.1  ``TEL;WORK;VOICE:...`` - bare parameters, and QUOTED-PRINTABLE encoding
       on any property. This is what Outlook Express and Palm exported.
  3.0  ``TEL;TYPE=WORK,VOICE:...`` - named parameters, UTF-8 by default.
  4.0  ``TEL;TYPE=work:tel:+353...`` - URIs in values.

``vobject`` handles all three, so it does the parsing; this module's job is to
turn whatever comes back into one shape, and to keep everything it cannot
categorise rather than dropping it. A 1998 card's X-PALM-CATEGORY is not
useful, but it is evidence, and evidence is what an archive is for.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterator

from ..logging_setup import get_logger
from ..models import Kind, ParsedIdentity, ParsedItem, Role, TimePoint
from ..normalize.text import clean_text
from .base import Parser, ParserError, register

log = get_logger("parsers.vcf")

#: vCard TEL types mapped to the slots the contact card uses.
_PHONE_SLOTS = {
    "work": "business", "voice": "business", "pref": "business",
    "home": "home", "cell": "mobile", "mobile": "mobile",
    "fax": "fax", "pager": "pager", "car": "mobile", "msg": "other",
}


@register
class VcfParser(Parser):
    """Contact cards from a .vcf file, which may hold hundreds."""

    extensions = frozenset({".vcf"})
    produces = frozenset({Kind.CONTACT})
    name = "vcard"

    def parse(self, kinds: frozenset[str] | None = None) -> Iterator[ParsedItem]:
        if not self._wants(kinds, Kind.CONTACT):
            return

        try:
            import vobject
        except ImportError as exc:
            self.outcome.error = "the vobject library is not installed"
            self.outcome.error_detail = str(exc)
            raise ParserError(
                "Contact cards (.vcf) cannot be read because the vobject library "
                "is not installed. Install it with:  pip install vobject"
            ) from exc

        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            self.outcome.error = f"could not be opened: {exc}"
            self.outcome.error_detail = repr(exc)
            return

        decoded = clean_text(raw)
        self.outcome.claimed_count = decoded.text.upper().count("BEGIN:VCARD")

        produced = 0
        try:
            cards = vobject.readComponents(decoded.text, ignoreUnreadable=True)
            for card in cards:
                if self._limit_reached(produced):
                    return
                try:
                    item = self._contact(card, decoded.confidence)
                except Exception as exc:  # noqa: BLE001 - one card, not the file
                    log.debug("A card in %s could not be read: %s", self.path, exc)
                    self.outcome.add_finding(
                        "read_failure",
                        "medium",
                        f"A contact card in {self.path.name} could not be read",
                        f"One card was skipped.\n\nExact error: {exc}",
                        {"path": str(self.path)},
                    )
                    continue
                if item is not None:
                    produced += 1
                    self.outcome.yielded_count = produced
                    self.outcome.items_scanned = produced
                    yield item
        except Exception as exc:  # noqa: BLE001
            self.outcome.error = f"the contact file could not be read: {exc}"
            self.outcome.error_detail = repr(exc)
            self.outcome.add_finding(
                "read_failure",
                "critical",
                f"{self.path.name} could not be read as contact cards",
                f"The file begins like a vCard file but could not be parsed.\n\n"
                f"Exact error: {exc}",
                {"path": str(self.path)},
            )

    def _contact(self, card, confidence: float) -> ParsedItem | None:
        if getattr(card, "name", "").upper() != "VCARD":
            return None

        item = ParsedItem(kind=Kind.CONTACT, backend=self.name)
        item.parse_confidence = confidence

        display = _first(card, "fn")
        structured = _structured_name(card)

        if not display:
            display = " ".join(
                x for x in (structured.get("given_name"), structured.get("surname")) if x
            ).strip() or None

        emails = _emails(card)
        phones = _phones(card)
        org, department = _organization(card)

        if not display and not emails and not phones:
            # Nothing identifying at all. Kept out rather than invented.
            log.debug("A card in %s had no name, address or number", self.path)
            return None

        item.subject = display
        item.native_id = _first(card, "uid")

        item.contact = {
            "display_name": display,
            "given_name": structured.get("given_name"),
            "surname": structured.get("surname"),
            "middle_name": structured.get("middle_name"),
            "prefix": structured.get("prefix"),
            "suffix": structured.get("suffix"),
            "nickname": _first(card, "nickname"),
            "title": _first(card, "title"),
            "organization": org,
            "department": department,
            "role": _first(card, "role"),
            "emails": emails,
            "phones": phones,
            "addresses": _addresses(card),
            "birthday": _first(card, "bday"),
            "anniversary": _first(card, "anniversary"),
            "web": _first(card, "url"),
            "notes": _first(card, "note"),
            "categories": _categories(card),
            "vcard_version": _first(card, "version"),
            # Anything the vCard carried that has no home above. Kept because
            # an archive's job is to keep, not to curate.
            "other": _leftovers(card),
        }

        item.body_text = item.contact["notes"]
        item.body_format = "plain"

        for address in emails:
            item.participants.append(
                ParsedIdentity(
                    address=address,
                    address_type="smtp",
                    display_name=display,
                    role=Role.TO,
                )
            )
        if not emails and display:
            item.participants.append(
                ParsedIdentity(address=None, address_type="none",
                               display_name=display, role=Role.TO)
            )

        # A contact card has a revision date at best, and often nothing. That
        # is not a date the contact "happened", so it is not made one.
        revision = _first(card, "rev")
        item.occurred = _revision_time(revision)
        # Deliberately not a no_date finding. A contact card records a person,
        # not something that happened, so having no date is its normal
        # condition rather than a defect. Flagging every entry in an address
        # book would put thousands of non-problems on the Problems screen and
        # teach the user to ignore it.

        item.categories = item.contact["categories"] or []
        return item


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _first(card, name: str) -> str | None:
    try:
        value = getattr(card, name).value
    except AttributeError:
        return None
    if value is None:
        return None
    if isinstance(value, bytes):
        return clean_text(value).text or None
    text = str(value).strip()
    return text or None


def _all(card, name: str) -> list:
    try:
        return list(getattr(card, f"{name}_list"))
    except AttributeError:
        try:
            return [getattr(card, name)]
        except AttributeError:
            return []


def _structured_name(card) -> dict[str, str | None]:
    try:
        n = card.n.value
    except AttributeError:
        return {}
    return {
        "surname": str(getattr(n, "family", "") or "").strip() or None,
        "given_name": str(getattr(n, "given", "") or "").strip() or None,
        "middle_name": str(getattr(n, "additional", "") or "").strip() or None,
        "prefix": str(getattr(n, "prefix", "") or "").strip() or None,
        "suffix": str(getattr(n, "suffix", "") or "").strip() or None,
    }


def _emails(card) -> list[str]:
    out: list[str] = []
    for entry in _all(card, "email"):
        value = str(getattr(entry, "value", "") or "").strip()
        if value.lower().startswith("mailto:"):
            value = value[7:]
        if value and value not in out:
            out.append(value)
    return out


def _phones(card) -> dict[str, str]:
    """Phone numbers by slot, keeping every number even when slots collide."""
    out: dict[str, str] = {}
    extras: list[str] = []

    for entry in _all(card, "tel"):
        value = str(getattr(entry, "value", "") or "").strip()
        if not value:
            continue
        if value.lower().startswith("tel:"):
            value = value[4:]

        types = _types(entry)
        slot = None
        for t in types:
            if t in _PHONE_SLOTS:
                slot = _PHONE_SLOTS[t]
                break
        if slot is None:
            slot = "other"

        if slot in out and out[slot] != value:
            extras.append(f"{slot}: {value}")
        else:
            out[slot] = value

    if extras:
        # A second work number is not a reason to lose the first.
        out["additional"] = "; ".join(extras)
    return out


def _addresses(card) -> dict[str, dict[str, str | None]]:
    out: dict[str, dict[str, str | None]] = {}
    for entry in _all(card, "adr"):
        value = getattr(entry, "value", None)
        if value is None:
            continue
        types = _types(entry)
        slot = "home" if "home" in types else ("business" if "work" in types else "other")
        out[slot] = {
            "street": str(getattr(value, "street", "") or "").strip() or None,
            "city": str(getattr(value, "city", "") or "").strip() or None,
            "state": str(getattr(value, "region", "") or "").strip() or None,
            "postal_code": str(getattr(value, "code", "") or "").strip() or None,
            "country": str(getattr(value, "country", "") or "").strip() or None,
            "box": str(getattr(value, "box", "") or "").strip() or None,
            "extended": str(getattr(value, "extended", "") or "").strip() or None,
        }
    return out


def _organization(card) -> tuple[str | None, str | None]:
    try:
        value = card.org.value
    except AttributeError:
        return None, None
    if isinstance(value, list):
        parts = [str(p).strip() for p in value if str(p).strip()]
        org = parts[0] if parts else None
        department = "; ".join(parts[1:]) if len(parts) > 1 else None
        return org, department
    text = str(value).strip()
    return (text or None), None


def _categories(card) -> list[str]:
    out: list[str] = []
    for entry in _all(card, "categories"):
        value = getattr(entry, "value", None)
        if isinstance(value, list):
            out.extend(str(v).strip() for v in value if str(v).strip())
        elif value:
            out.extend(c.strip() for c in str(value).split(",") if c.strip())
    return out


def _types(entry) -> set[str]:
    """Parameter types, handling both 2.1 bare words and 3.0 TYPE= lists."""
    types: set[str] = set()
    params = getattr(entry, "params", {}) or {}
    for key, values in params.items():
        if key.upper() == "TYPE":
            for v in (values if isinstance(values, list) else [values]):
                types.add(str(v).strip().lower())
        elif not values or values == [key]:
            types.add(str(key).strip().lower())
    return types


_KNOWN = {
    "fn", "n", "email", "tel", "adr", "org", "title", "role", "note", "url",
    "bday", "anniversary", "categories", "version", "uid", "rev", "nickname",
    "prodid", "begin", "end", "photo", "logo", "sound", "key", "class",
}


def _leftovers(card) -> dict[str, Any]:
    """Every property with no home above, kept verbatim.

    X-ABLabel, X-PALM-CATEGORY, X-MS-CARDPICTURE and a hundred other extensions
    appear in real address books. They are rarely useful and occasionally the
    only record of something, so they are stored rather than discarded.
    """
    out: dict[str, Any] = {}
    try:
        children = card.getChildren()
    except Exception:  # noqa: BLE001
        return out

    for child in children:
        name = str(getattr(child, "name", "") or "").lower()
        if not name or name in _KNOWN:
            continue
        value = getattr(child, "value", None)
        if value is None:
            continue
        text = str(value).strip()
        if not text or len(text) > 2000:
            continue
        out.setdefault(name, text)
    return out


def _revision_time(revision: str | None) -> TimePoint:
    """A vCard REV timestamp, when there is one."""
    if not revision:
        return TimePoint.unknown()

    text = revision.strip().replace("-", "").replace(":", "")
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        from datetime import timezone

        if fmt.endswith("Z"):
            return TimePoint.from_aware(dt.replace(tzinfo=timezone.utc), "UTC")
        return TimePoint.from_naive(dt)
    return TimePoint.unknown()
