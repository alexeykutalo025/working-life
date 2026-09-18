"""Outlook Express 4 and Eudora mailboxes (.mbx), implemented here.

Two unrelated formats share this extension, and telling them apart is the first
thing this reader does.

**Outlook Express 4** - header ``JMF9`` (``4A 4D 46 39``):

  bytes 0-3    "JMF9"
  bytes 4-7    file size
  bytes 8-11   message count
  bytes 0x54   where the first message starts

  Each message is preceded by a 16-byte record header:
    +0x00  ``JMF6`` (``4A 4D 46 36``), the per-message marker
    +0x04  total length of this record including the header
    +0x08  message number
    +0x0C  flags
  The RFC 822 message follows immediately.

**Eudora** - no header at all. The file is a plain mbox whose messages are
separated by Eudora's own ``From ???@???`` pseudo-From_ line. That is handled
by delegating to the mbox reader rather than duplicating it.

A file claiming to be .mbx that is neither is reported as such and not guessed
at.
"""

from __future__ import annotations

import re
import struct
from typing import Iterator

from ..logging_setup import get_logger
from ..models import Kind, ParsedItem
from .base import Parser, register

log = get_logger("parsers.mbx")

OE4_MAGIC = b"JMF9"
OE4_RECORD_MAGIC = b"JMF6"

OE4_FILE_SIZE = 0x04
OE4_MESSAGE_COUNT = 0x08
OE4_FIRST_MESSAGE = 0x54

OE4_RECORD_HEADER = 16
OE4_RECORD_LENGTH = 0x04

#: Eudora writes this instead of a real From_ line.
EUDORA_SEPARATOR = re.compile(rb"^From \?\?\?@\?\?\?.*$", re.MULTILINE)
GENERIC_FROM_LINE = re.compile(rb"^From \S+.*$", re.MULTILINE)

MAX_MESSAGES = 500_000
MAX_MESSAGE_BYTES = 64 * 1024 * 1024


class MbxError(Exception):
    """The file's structure does not hold. The message says where."""


@register
class MbxParser(Parser):
    """Messages out of an Outlook Express 4 or Eudora mailbox."""

    extensions = frozenset({".mbx"})
    produces = frozenset({Kind.MESSAGE})
    name = "mbx"

    def parse(self, kinds: frozenset[str] | None = None) -> Iterator[ParsedItem]:
        if not self._wants(kinds, Kind.MESSAGE):
            return

        try:
            data = self.path.read_bytes()
        except OSError as exc:
            self.outcome.error = f"could not be opened: {exc}"
            self.outcome.error_detail = repr(exc)
            return

        if not data:
            self.outcome.error = "the file is empty"
            self.outcome.claimed_count = 0
            return

        if data.startswith(OE4_MAGIC):
            yield from self._parse_oe4(data)
        elif EUDORA_SEPARATOR.search(data) or GENERIC_FROM_LINE.search(data[:8192]):
            yield from self._parse_mbox_style(data)
        else:
            self.outcome.error = (
                "the file is neither an Outlook Express 4 mailbox nor a Eudora one"
            )
            self.outcome.add_finding(
                "magic_mismatch", "high",
                f"{self.path.name} is not a mailbox Recall recognises",
                "A .mbx file is either an Outlook Express 4 mailbox, which starts "
                'with the letters "JMF9", or a Eudora mailbox, whose messages are '
                'separated by lines beginning "From ". This file is neither.\n\n'
                f"It starts with: {data[:16].hex(' ')}\n\n"
                "The file has not been changed. If you know what wrote it, an "
                "export to .eml or .mbox from that program will read correctly.",
                {"path": str(self.path), "first_bytes": data[:16].hex(" ")},
            )

    # -- Outlook Express 4 ----------------------------------------------

    def _parse_oe4(self, data: bytes) -> Iterator[ParsedItem]:
        import email
        import email.policy

        from .eml import build_message

        if len(data) < OE4_FIRST_MESSAGE + 4:
            self.outcome.error = (
                f"the file is only {len(data)} bytes, too short to hold an "
                "Outlook Express 4 header"
            )
            return

        claimed = struct.unpack_from("<I", data, OE4_MESSAGE_COUNT)[0]
        if claimed > MAX_MESSAGES:
            self.outcome.error = (
                f"the header claims {claimed:,} messages, which is not plausible"
            )
            self.outcome.add_finding(
                "read_failure", "critical",
                f"{self.path.name} has a damaged header",
                f"The header claims {claimed:,} messages. That number is not "
                "plausible, so the header cannot be trusted and Recall has not "
                "tried to follow it.",
                {"path": str(self.path), "claimed": claimed},
            )
            return

        self.outcome.claimed_count = claimed
        produced = 0
        offset = OE4_FIRST_MESSAGE
        structure_error: str | None = None

        while offset < len(data):
            if self._limit_reached(produced):
                return

            if offset + OE4_RECORD_HEADER > len(data):
                break

            if data[offset : offset + 4] != OE4_RECORD_MAGIC:
                # Resynchronise: a damaged record is common, and the messages
                # after it are usually fine. Looking for the next marker
                # recovers them instead of abandoning the file.
                next_marker = data.find(OE4_RECORD_MAGIC, offset + 1)
                if next_marker == -1:
                    if produced < claimed:
                        structure_error = (
                            f"the record marker was missing at offset 0x{offset:x} "
                            "and no further records were found"
                        )
                    break
                structure_error = structure_error or (
                    f"the record marker was missing at offset 0x{offset:x}; "
                    f"reading resumed at 0x{next_marker:x}"
                )
                offset = next_marker
                continue

            length = struct.unpack_from("<I", data, offset + OE4_RECORD_LENGTH)[0]
            if length <= OE4_RECORD_HEADER or length > MAX_MESSAGE_BYTES:
                structure_error = structure_error or (
                    f"the record at offset 0x{offset:x} claims a length of "
                    f"{length:,} bytes, which is not plausible"
                )
                next_marker = data.find(OE4_RECORD_MAGIC, offset + 4)
                if next_marker == -1:
                    break
                offset = next_marker
                continue

            start = offset + OE4_RECORD_HEADER
            end = min(offset + length, len(data))
            raw = data[start:end]
            offset = offset + length

            if not raw.strip():
                continue

            try:
                message = email.message_from_bytes(raw, policy=email.policy.default)
                item = build_message(message, backend=self.name)
            except Exception as exc:  # noqa: BLE001
                self.outcome.add_finding(
                    "read_failure", "critical",
                    f"{self.path.name}: a message could not be read",
                    f"Position in file: 0x{start:x}\n"
                    f"Messages read successfully so far: {produced}\n"
                    f"Exact error: {exc.__class__.__name__}: {exc}",
                    {"path": str(self.path), "offset": start},
                )
                continue

            produced += 1
            self.outcome.yielded_count = produced
            self.outcome.items_scanned = produced
            self.outcome.last_good_offset = start
            yield item

        if structure_error:
            self.outcome.add_finding(
                "read_failure", "critical",
                f"{self.path.name}: the file is damaged part-way through",
                f"{structure_error}\n\n"
                f"The header says the mailbox holds {claimed:,} messages. Recall "
                f"read {produced:,}.\n\n"
                "The file has not been changed and nothing has been repaired.",
                {
                    "path": str(self.path),
                    "claimed": claimed,
                    "read": produced,
                    "last_good_offset": self.outcome.last_good_offset,
                },
            )

    # -- Eudora ----------------------------------------------------------

    def _parse_mbox_style(self, data: bytes) -> Iterator[ParsedItem]:
        """A Eudora mailbox is an mbox with a peculiar From_ line."""
        import email
        import email.policy

        from .eml import build_message

        separators = [m.start() for m in GENERIC_FROM_LINE.finditer(data)]
        self.outcome.claimed_count = len(separators)

        if not separators:
            return

        produced = 0
        for i, start in enumerate(separators):
            if self._limit_reached(produced):
                return
            end = separators[i + 1] if i + 1 < len(separators) else len(data)

            block = data[start:end]
            newline = block.find(b"\n")
            if newline == -1:
                continue
            raw = block[newline + 1 :]
            if not raw.strip():
                continue

            try:
                message = email.message_from_bytes(raw, policy=email.policy.default)
                item = build_message(message, backend="eudora")
            except Exception as exc:  # noqa: BLE001
                self.outcome.add_finding(
                    "read_failure", "critical",
                    f"{self.path.name}: a message could not be read",
                    f"Position in file: 0x{start:x}\n"
                    f"Exact error: {exc.__class__.__name__}: {exc}",
                    {"path": str(self.path), "offset": start},
                )
                continue

            produced += 1
            self.outcome.yielded_count = produced
            self.outcome.items_scanned = produced
            self.outcome.last_good_offset = start
            yield item
