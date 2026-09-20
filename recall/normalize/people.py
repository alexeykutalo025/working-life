"""Who is who.

Two jobs, kept strictly apart because the spec draws a hard line between them:

**Resolution** is what happens automatically, and only on evidence that cannot
be wrong: the same normalised SMTP address is the same person, always. That is
the only automatic merge in the program.

**Suggestion** is everything else - matching display names, shared surnames and
domains, similar names with a correspondent in common. These are *proposed* in
a review queue with the evidence shown, and a human clicks. Nothing merges
itself on a similarity score.

Merging sets ``people.merged_into``. No ``people`` row is ever deleted, so every
merge can be undone.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

from ..logging_setup import get_logger
from ..models import AddressType

log = get_logger("normalize.people")

#: An Exchange legacy distinguished name, e.g.
#: /o=CONTRACTMKTG/ou=First Administrative Group/cn=Recipients/cn=tmccarthy
_LEGACY_DN = re.compile(r"^/o=[^/]*(?:/ou=[^/]*)*/cn=", re.IGNORECASE)

_ADDRESS_IN_BRACKETS = re.compile(r"<([^>]+)>")

#: Deliberately permissive: 1990s addresses include forms a modern validator
#: rejects, and rejecting a real address would lose a real correspondent.
_LOOKS_LIKE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(slots=True)
class NormalizedAddress:
    """One address, cleaned up, with what kind of thing it is."""

    address: str
    address_type: str
    display_name: str | None = None
    #: The address exactly as it appeared, kept so nothing is invented later.
    raw: str | None = None

    @property
    def domain(self) -> str | None:
        if self.address_type != AddressType.SMTP or "@" not in self.address:
            return None
        return self.address.rsplit("@", 1)[1]

    @property
    def local(self) -> str | None:
        if self.address_type != AddressType.SMTP or "@" not in self.address:
            return None
        return self.address.rsplit("@", 1)[0]


def normalize_address(
    raw: str | None,
    display_name: str | None = None,
    *,
    dot_insensitive_domains: Iterable[str] = (),
) -> NormalizedAddress | None:
    """Clean one address into its canonical form.

    SMTP addresses are lowercased, ``+tags`` are stripped, and dots in the local
    part are removed *only* for the providers configured as ignoring them. That
    last rule is narrow on purpose: at most providers ``j.smith@`` and
    ``jsmith@`` are two different people, and merging them would be a guess.

    An Exchange legacyDN is kept exactly as it is, uppercased for matching. It
    is never turned into an email address - inventing one is precisely the
    fabrication section 9.3 forbids.
    """
    if raw is None and display_name is None:
        return None

    text = (raw or "").strip()
    match = _ADDRESS_IN_BRACKETS.search(text)
    if match:
        text = match.group(1).strip()
    text = text.strip("<>").strip()

    if text.lower().startswith("mailto:"):
        text = text[7:].strip()

    if not text:
        if display_name and display_name.strip():
            return NormalizedAddress(
                address=_fold(display_name),
                address_type=AddressType.NONE,
                display_name=display_name.strip(),
                raw=raw,
            )
        return None

    if _LEGACY_DN.match(text):
        return NormalizedAddress(
            address=text.upper(),
            address_type=AddressType.EX,
            display_name=(display_name or "").strip() or None,
            raw=raw,
        )

    if "@" in text:
        local, _, domain = text.rpartition("@")
        domain = domain.strip().lower().rstrip(".")
        local = local.strip()

        if "+" in local:
            local = local.split("+", 1)[0]
        if domain in {d.lower() for d in dot_insensitive_domains}:
            local = local.replace(".", "")
        local = local.lower()

        if not local or not domain:
            return NormalizedAddress(
                address=_fold(text),
                address_type=AddressType.NONE,
                display_name=(display_name or "").strip() or None,
                raw=raw,
            )
        return NormalizedAddress(
            address=f"{local}@{domain}",
            address_type=AddressType.SMTP,
            display_name=(display_name or "").strip() or None,
            raw=raw,
        )

    digits = re.sub(r"[^\d+]", "", text)
    if len(digits) >= 7 and re.fullmatch(r"\+?\d[\d\s\-().]*", text):
        return NormalizedAddress(
            address=digits,
            address_type=AddressType.PHONE,
            display_name=(display_name or "").strip() or None,
            raw=raw,
        )

    # A name with no address. Kept as an identity of its own, because "Bob from
    # the printers" with no address is still a real correspondent.
    return NormalizedAddress(
        address=_fold(text),
        address_type=AddressType.NONE,
        display_name=(display_name or text).strip() or None,
        raw=raw,
    )


def _fold(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip().casefold()


def looks_like_email(text: str | None) -> bool:
    return bool(text and _LOOKS_LIKE_EMAIL.match(text.strip()))


def is_legacy_dn(text: str | None) -> bool:
    return bool(text and _LEGACY_DN.match(text.strip()))


def legacy_dn_name(dn: str) -> str | None:
    """The trailing cn= of a legacyDN, which is usually the account name.

    This is used as a *label*, never as an address. Turning
    /cn=tmccarthy into tmccarthy@something would be a fabrication.
    """
    if not is_legacy_dn(dn):
        return None
    parts = [p for p in dn.split("/") if p.lower().startswith("cn=")]
    if not parts:
        return None
    return parts[-1][3:].strip() or None


# ---------------------------------------------------------------------------
# Writing identities and people
# ---------------------------------------------------------------------------


class PeopleResolver:
    """Turns participants into rows in ``identities``, ``people``, ``participations``.

    Caches within one extraction run so a mailbox with 200,000 messages does not
    issue 400,000 identical lookups.
    """

    def __init__(self, conn, settings) -> None:
        self.conn = conn
        self.settings = settings
        self._identity_cache: dict[tuple[str, str], int] = {}
        self._person_cache: dict[int, int] = {}
        self._self_addresses = {a.lower() for a in settings.identity.me}
        self._dot_domains = settings.identity.dot_insensitive_domains

    def normalize(self, address: str | None, display_name: str | None) -> NormalizedAddress | None:
        return normalize_address(
            address, display_name, dot_insensitive_domains=self._dot_domains
        )

    def identity_id(self, norm: NormalizedAddress, when_utc: str | None) -> int:
        """Get or create the identity row, and the person behind it.

        One normalised SMTP address means one person. That is the only merge
        this program performs without being asked.
        """
        key = (norm.address, norm.address_type)
        cached = self._identity_cache.get(key)
        if cached is not None:
            self._touch_identity(cached, norm.display_name, when_utc)
            return cached

        row = self.conn.execute(
            "SELECT id, person_id FROM identities WHERE address = ? AND address_type = ?",
            key,
        ).fetchone()

        if row is not None:
            identity_id = int(row["id"])
            self._identity_cache[key] = identity_id
            if row["person_id"] is None:
                person_id = self._create_person(norm, when_utc)
                self.conn.execute(
                    "UPDATE identities SET person_id = ? WHERE id = ?",
                    (person_id, identity_id),
                )
            self._touch_identity(identity_id, norm.display_name, when_utc)
            return identity_id

        person_id = self._create_person(norm, when_utc)
        cur = self.conn.execute(
            "INSERT INTO identities(person_id, address, address_type, raw_display_name, "
            "first_seen_utc, last_seen_utc, use_count) VALUES (?, ?, ?, ?, ?, ?, 1)",
            (person_id, norm.address, norm.address_type, norm.display_name, when_utc, when_utc),
        )
        identity_id = int(cur.lastrowid)
        self._identity_cache[key] = identity_id
        return identity_id

    def _create_person(self, norm: NormalizedAddress, when_utc: str | None) -> int:
        display = norm.display_name or _default_display_name(norm)
        is_self = 1 if norm.address in self._self_addresses else 0
        cur = self.conn.execute(
            "INSERT INTO people(display_name, is_self, first_seen_utc, last_seen_utc, "
            "item_count) VALUES (?, ?, ?, ?, 0)",
            (display, is_self, when_utc, when_utc),
        )
        return int(cur.lastrowid)

    def _touch_identity(self, identity_id: int, display_name: str | None, when_utc: str | None) -> None:
        self.conn.execute(
            "UPDATE identities SET use_count = use_count + 1, "
            "raw_display_name = COALESCE(raw_display_name, ?), "
            "first_seen_utc = CASE WHEN ? IS NOT NULL AND "
            "  (first_seen_utc IS NULL OR ? < first_seen_utc) THEN ? ELSE first_seen_utc END, "
            "last_seen_utc = CASE WHEN ? IS NOT NULL AND "
            "  (last_seen_utc IS NULL OR ? > last_seen_utc) THEN ? ELSE last_seen_utc END "
            "WHERE id = ?",
            (display_name, when_utc, when_utc, when_utc, when_utc, when_utc, when_utc, identity_id),
        )

    def person_for_identity(self, identity_id: int) -> int | None:
        """The person an identity belongs to, following any merge."""
        cached = self._person_cache.get(identity_id)
        if cached is not None:
            return cached
        row = self.conn.execute(
            "SELECT person_id FROM identities WHERE id = ?", (identity_id,)
        ).fetchone()
        if row is None or row["person_id"] is None:
            return None
        person_id = _follow_merges(self.conn, int(row["person_id"]))
        self._person_cache[identity_id] = person_id
        return person_id

    def invalidate(self) -> None:
        """Drop the person cache after a merge, so later rows see the new target."""
        self._person_cache.clear()


def _default_display_name(norm: NormalizedAddress) -> str:
    """A label for a person we only know by address.

    For an Exchange DN the account name is used as a label, clearly marked, and
    never presented as an email address.
    """
    if norm.address_type == AddressType.EX:
        name = legacy_dn_name(norm.raw or norm.address)
        return name or (norm.raw or norm.address)
    return norm.raw or norm.address


def _follow_merges(conn, person_id: int, depth: int = 0) -> int:
    """Follow merged_into to the surviving person, guarding against cycles."""
    seen = {person_id}
    current = person_id
    while depth < 20:
        row = conn.execute(
            "SELECT merged_into FROM people WHERE id = ?", (current,)
        ).fetchone()
        if row is None or row["merged_into"] is None:
            return current
        nxt = int(row["merged_into"])
        if nxt in seen:
            log.warning("people.merged_into forms a loop at person %d", nxt)
            return current
        seen.add(nxt)
        current = nxt
        depth += 1
    return current


def resolve_person(conn, person_id: int) -> int:
    """Public form of the merge-following walk."""
    return _follow_merges(conn, person_id)
