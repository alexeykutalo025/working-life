"""Windows Address Book (.wab) - best effort, and honest about it.

Spec section 2 lists .wab as "best-effort, may skip. Log clearly if
unsupported", and that is exactly what this is.

The WAB format has no public specification. Microsoft shipped wab32.dll with an
API and never documented the file layout, and no maintained Python library
reads it. What is known from inspection:

  * it is a structured-storage-like container with a 16-byte GUID header,
  * contact records hold their strings as null-terminated UTF-16LE runs,
  * the record index is not reliably recoverable without the DLL.

So this reader does the one thing that can be done honestly: it recovers the
readable UTF-16 strings, identifies the email addresses among them, and creates
a contact for each address with whatever name sits nearest it. That is a
salvage operation, not a parse, and every record it produces says so - the
parse confidence is low and a finding explains what was and was not recovered.

The alternative considered and rejected was to skip .wab files entirely. For
someone whose 1998 address book exists only as a .wab, half the addresses with
a warning attached is worth more than nothing with a shrug.
"""

from __future__ import annotations

import re
from typing import Iterator

from ..logging_setup import get_logger
from ..models import Kind, ParsedIdentity, ParsedItem, Role, TimePoint
from .base import Parser, register

log = get_logger("parsers.wab")

#: The WAB file signature, as observed. Not from a specification.
WAB_MAGIC = b"\x9c\xcb\xcb\x8d\x13\x75\xd2\x11"

#: Deliberately permissive. A 1998 address book contains addresses a modern
#: validator rejects, and rejecting a real one loses a real correspondent.
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

#: A plausible human name near an address: letters, spaces and the punctuation
#: that appears in real names.
_NAME = re.compile(r"^[\w][\w \.\-'’]{1,60}$", re.UNICODE)

#: Strings shorter than this are noise from the container's own structures.
_MIN_STRING = 3


@register
class WabParser(Parser):
    """Salvages what it can from a Windows Address Book."""

    extensions = frozenset({".wab"})
    produces = frozenset({Kind.CONTACT})
    name = "wab-salvage"

    def parse(self, kinds: frozenset[str] | None = None) -> Iterator[ParsedItem]:
        if not self._wants(kinds, Kind.CONTACT):
            return

        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            self.outcome.error = f"could not be opened: {exc}"
            self.outcome.error_detail = repr(exc)
            return

        if not raw:
            self.outcome.error = "the file is empty"
            return

        if not raw.startswith(WAB_MAGIC):
            log.info("%s does not start like a Windows Address Book", self.path.name)

        strings = _utf16_strings(raw) + _ascii_strings(raw)
        addresses = _addresses_with_names(strings)

        if not addresses:
            self.outcome.add_finding(
                "read_failure",
                "high",
                f"{self.path.name}: nothing could be recovered from this address book",
                "Windows Address Book files have no published format, and no "
                "library reads them properly. Recall looked through this one for "
                "readable email addresses and found none.\n\n"
                "What to do: open the file in Windows Contacts or an old copy of "
                "Outlook Express, export it as a .vcf or .csv, and let Recall "
                "read that instead. Nothing has been changed on disk.",
                {"path": str(self.path), "size_bytes": len(raw)},
            )
            return

        self.outcome.claimed_count = None   # genuinely unknown, so not claimed
        self.outcome.add_finding(
            "low_confidence_text",
            "medium",
            f"{self.path.name}: {len(addresses)} contacts recovered, but not by reading the format",
            "Windows Address Book files have no published format. Recall did not "
            "parse this file - it searched it for readable text and kept the "
            f"email addresses it found, {len(addresses)} of them, with whatever "
            "name appeared beside each one.\n\n"
            "That means: some contacts may be missing, some names may be attached "
            "to the wrong address, and telephone numbers and postal addresses "
            "have not been recovered at all.\n\n"
            "For a complete result, open this file in Windows Contacts or Outlook "
            "Express, export it as a .vcf, and let Recall read that. Nothing has "
            "been changed on disk.",
            {"path": str(self.path), "recovered": len(addresses)},
        )

        produced = 0
        for address, name in addresses:
            if self._limit_reached(produced):
                return

            item = ParsedItem(kind=Kind.CONTACT, backend=self.name)
            # Low, and deliberately so: this is salvage, not parsing.
            item.parse_confidence = 0.5
            item.subject = name or address
            item.occurred = TimePoint.unknown()
            item.contact = {
                "display_name": name,
                "emails": [address],
                "phones": {},
                "addresses": {},
                "recovery_method": "text salvage - the .wab format has no public specification",
            }
            item.participants.append(
                ParsedIdentity(
                    address=address, address_type="smtp",
                    display_name=name, role=Role.TO,
                )
            )
            item.note(
                "low_confidence_text",
                "This contact was recovered by searching the address book for "
                "readable text, not by reading its format, which is not public. "
                "The name beside the address may not belong to it.",
            )

            produced += 1
            self.outcome.yielded_count = produced
            self.outcome.items_scanned = produced
            yield item


@register
class PabParser(Parser):
    """Outlook personal address book (.pab). Outlook only, and often not then.

    A .pab is a MAPI store that only the Outlook MAPI provider can open, and
    modern Outlook dropped support for them entirely: since Outlook 2010 the
    format cannot be opened at all without importing it first.

    So this parser does not pretend. It reports what the file is, says what the
    user has to do to make it readable, and yields nothing. That is a better
    outcome than a half-parse producing plausible-looking rubbish.
    """

    extensions = frozenset({".pab"})
    produces = frozenset({Kind.CONTACT})
    name = "pab"

    def parse(self, kinds: frozenset[str] | None = None) -> Iterator[ParsedItem]:
        if not self._wants(kinds, Kind.CONTACT):
            return

        try:
            size = self.path.stat().st_size
        except OSError as exc:
            self.outcome.error = f"could not be opened: {exc}"
            return

        self.outcome.error = (
            "Personal Address Book (.pab) files cannot be opened by any version "
            "of Outlook from 2010 onwards, and no library reads them."
        )
        self.outcome.add_finding(
            "read_failure",
            "high",
            f"{self.path.name} is an old Outlook address book that cannot be opened",
            "This is a Personal Address Book, a format Microsoft removed from "
            "Outlook in 2010. Recall cannot read it, and neither can any Outlook "
            "on this computer.\n\n"
            "What to do: the contacts in it were almost certainly copied into "
            "Outlook's own Contacts folder when the address book was retired, so "
            "look for them in a .pst file instead. If this is the only copy, an "
            "installation of Outlook 2007 or earlier can open it and export a "
            ".vcf or .csv that Recall can read.\n\n"
            "Nothing has been changed on disk.",
            {"path": str(self.path), "size_bytes": size},
        )
        return
        yield  # pragma: no cover - makes this a generator, as the interface requires


# ---------------------------------------------------------------------------
# salvage
# ---------------------------------------------------------------------------


def _utf16_strings(data: bytes, min_length: int = _MIN_STRING) -> list[str]:
    """Null-terminated UTF-16LE runs, which is how WAB stores its text."""
    out: list[str] = []
    current: list[str] = []
    i = 0
    n = len(data) - 1

    while i < n:
        lo, hi = data[i], data[i + 1]
        code = lo | (hi << 8)
        if code == 0:
            if len(current) >= min_length:
                out.append("".join(current))
            current = []
            i += 2
            continue
        if 0x20 <= code <= 0xFFFD and code not in (0xFFFE, 0xFFFF):
            current.append(chr(code))
            i += 2
            continue
        if len(current) >= min_length:
            out.append("".join(current))
        current = []
        i += 1     # resynchronise a byte at a time, not two

    if len(current) >= min_length:
        out.append("".join(current))
    return out


def _ascii_strings(data: bytes, min_length: int = _MIN_STRING) -> list[str]:
    """Plain 8-bit runs, for the parts of older files that are not UTF-16."""
    out: list[str] = []
    current: list[str] = []
    for byte in data:
        if 0x20 <= byte < 0x7F:
            current.append(chr(byte))
        else:
            if len(current) >= min_length:
                out.append("".join(current))
            current = []
    if len(current) >= min_length:
        out.append("".join(current))
    return out


def _addresses_with_names(strings: list[str]) -> list[tuple[str, str | None]]:
    """Pair each address with the most plausible name near it.

    "Near" means the closest preceding string that looks like a name and is not
    itself an address. It is a heuristic, which is why every record it produces
    carries a warning rather than being presented as read.
    """
    found: dict[str, str | None] = {}

    for index, text in enumerate(strings):
        for match in _EMAIL.finditer(text):
            address = match.group(0).strip(" .,;:<>\"'").lower()
            if not address or address in found:
                continue
            found[address] = _nearest_name(strings, index)

    return sorted(found.items())


def _nearest_name(strings: list[str], index: int, window: int = 4) -> str | None:
    for back in range(1, window + 1):
        i = index - back
        if i < 0:
            break
        candidate = strings[i].strip()
        if not candidate or _EMAIL.search(candidate):
            continue
        if not _NAME.match(candidate):
            continue
        if candidate.lower() in _NOISE:
            continue
        if " " in candidate or candidate.isalpha():
            return candidate
    return None


#: Strings that appear in every WAB file and are not anybody's name.
_NOISE = {
    "wab", "wabfile", "microsoft", "outlook express", "address book",
    "contacts", "folder", "root", "default", "mapi", "smtp", "none",
}
