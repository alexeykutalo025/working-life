"""Outlook Express 5/6 message databases (.dbx), implemented here.

No maintained Python library reads DBX, and the format is undocumented by
Microsoft, so this is a from-scratch reader based on the format as reverse
engineered by the community and confirmed against real files.

**Layout.** A .dbx is a tree of fixed-size blocks:

  bytes 0-3      magic ``CF AD 12 FE``
  bytes 4-7      file type marker; ``C5 FD 74 6F`` is a message database
  bytes 0x1C     offset of the root index node
  bytes 0x24     number of records
  bytes 0xC4     offset of the "last variable segment"

An **index node** is 0x20C bytes:

  +0x00  offset of this node
  +0x04  offset of the parent
  +0x08  offset of the next node at this level
  +0x0C  offset of the child tree
  +0x10  number of entries in this node (max 0x14)
  +0x18  the entries: 12 bytes each, being
           +0  offset of the message's info block
           +4  offset of a child index node below this entry
           +8  how many entries are under that child

A **message info block** begins with:

  +0x00  self offset
  +0x04  length of the body
  +0x0A  count of attributes
  +0x0C  the attribute table: 4 bytes each, a one-byte id and a three-byte
         value that is either a direct value or an offset into the block

Attribute 0x84 is the offset of the first message-data block; the message text
is a chain of those blocks, each with a 16-byte header giving its length and
the offset of the next.

**What this reader does when the structure does not hold.** It stops, and says
where. A DBX with a corrupt index is common - the format was notorious for it -
and the honest response is to report the last offset that made sense and how
many records the header claimed, so ``estimated_loss`` is a computed number
rather than a shrug.
"""

from __future__ import annotations

import struct
from typing import Iterator

from ..logging_setup import get_logger
from ..models import Kind, ParsedItem
from .base import Parser, register

log = get_logger("parsers.dbx")

DBX_MAGIC = b"\xcf\xad\x12\xfe"

#: The file-type marker for a message database. Folders.dbx and the address
#: book use different markers and hold no messages.
MESSAGE_DB_MARKER = b"\xc5\xfd\x74\x6f"

#: Header field offsets.
OFF_ROOT_NODE = 0x1C
OFF_RECORD_COUNT = 0x24
OFF_FILE_INFO_LENGTH = 0x28

#: Index node geometry.
NODE_SIZE = 0x20C
NODE_ENTRY_COUNT = 0x10
NODE_ENTRIES_START = 0x18
NODE_ENTRY_SIZE = 12
MAX_ENTRIES_PER_NODE = 0x14

#: Message info block.
INFO_BODY_LENGTH = 0x04
INFO_ATTR_COUNT = 0x0A
INFO_ATTR_START = 0x0C

#: Attribute ids that matter. There are more; these are the ones that carry
#: anything worth keeping.
ATTR_MESSAGE_TEXT = 0x84       # offset of the first data block
ATTR_FLAGS = 0x01
ATTR_SUBJECT = 0x02
ATTR_SENDER_NAME = 0x04
ATTR_MESSAGE_ID = 0x05
ATTR_SUBJECT_2 = 0x08
ATTR_SENDER_ADDRESS = 0x09
ATTR_RECEIVED = 0x12

#: A data block's header: 16 bytes, of which these two matter.
DATA_NEXT_OFFSET = 0x04
DATA_LENGTH = 0x08
DATA_HEADER_SIZE = 0x10

#: Guards. A .dbx that claims more than this is corrupt, and following it would
#: allocate gigabytes before failing.
MAX_RECORDS = 500_000
MAX_NODES = 200_000
MAX_MESSAGE_BYTES = 64 * 1024 * 1024


class DbxError(Exception):
    """The file's structure does not hold. The message says where."""


@register
class DbxParser(Parser):
    """Messages out of an Outlook Express 5/6 folder file."""

    extensions = frozenset({".dbx"})
    produces = frozenset({Kind.MESSAGE})
    name = "dbx"

    def parse(self, kinds: frozenset[str] | None = None) -> Iterator[ParsedItem]:
        if not self._wants(kinds, Kind.MESSAGE):
            return

        try:
            data = self.path.read_bytes()
        except OSError as exc:
            self.outcome.error = f"could not be opened: {exc}"
            self.outcome.error_detail = repr(exc)
            return

        try:
            reader = DbxFile(data)
        except DbxError as exc:
            self.outcome.error = str(exc)
            self.outcome.error_detail = repr(exc)
            self.outcome.add_finding(
                "read_failure", "critical",
                f"{self.path.name} is not an Outlook Express message file Recall can read",
                f"{exc}\n\nThe file has not been changed.",
                {"path": str(self.path), "size_bytes": len(data)},
            )
            return

        self.outcome.claimed_count = reader.record_count

        if not reader.holds_messages:
            # Folders.dbx and similar are real DBX files with no mail in them.
            self.outcome.claimed_count = 0
            log.info("%s is a DBX file that holds no messages", self.path.name)
            return

        from .eml import build_message

        produced = 0
        for offset, raw in reader.messages():
            if self._limit_reached(produced):
                return
            try:
                import email
                import email.policy

                message = email.message_from_bytes(raw, policy=email.policy.default)
                item = build_message(message, backend=self.name)
            except Exception as exc:  # noqa: BLE001 - one message, not the file
                log.debug("A message at 0x%x in %s failed: %s", offset, self.path, exc)
                self.outcome.add_finding(
                    "read_failure", "critical",
                    f"{self.path.name}: a message could not be read",
                    f"Position in file: 0x{offset:x}\n"
                    f"Messages read successfully so far: {produced}\n"
                    f"Exact error: {exc.__class__.__name__}: {exc}",
                    {"path": str(self.path), "offset": offset},
                )
                continue

            produced += 1
            self.outcome.yielded_count = produced
            self.outcome.items_scanned = produced
            self.outcome.last_good_offset = offset
            yield item

        if reader.structure_error:
            self.outcome.add_finding(
                "read_failure", "critical",
                f"{self.path.name}: the file's index is damaged part-way through",
                f"{reader.structure_error}\n\n"
                f"The file's own header says it holds {reader.record_count:,} "
                f"messages. Recall read {produced:,} before the structure stopped "
                "making sense.\n\n"
                "Outlook Express message files were notorious for this. The file "
                "has not been changed and nothing has been repaired - Recall "
                "never writes to your files.",
                {
                    "path": str(self.path),
                    "claimed": reader.record_count,
                    "read": produced,
                    "last_good_offset": reader.last_good_offset,
                },
            )


class DbxFile:
    """The structural reader. Knows about blocks, not about mail."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.structure_error: str | None = None
        self.last_good_offset: int | None = None

        if len(data) < 0x100:
            raise DbxError(
                f"the file is only {len(data)} bytes, too short to hold a DBX header"
            )
        if data[:4] != DBX_MAGIC:
            raise DbxError(
                "the file does not start with the Outlook Express marker "
                f"(found {data[:4].hex(' ')}, expected {DBX_MAGIC.hex(' ')})"
            )

        self.file_marker = data[4:8]
        self.holds_messages = self.file_marker == MESSAGE_DB_MARKER

        self.root_node = self._u32(OFF_ROOT_NODE)
        self.record_count = self._u32(OFF_RECORD_COUNT)

        if self.record_count > MAX_RECORDS:
            raise DbxError(
                f"the header claims {self.record_count:,} messages, which is not "
                "plausible - the header is damaged"
            )

    # -- primitives ------------------------------------------------------

    def _u32(self, offset: int) -> int:
        if offset + 4 > len(self.data):
            raise DbxError(f"the file ends before offset 0x{offset:x}")
        return struct.unpack_from("<I", self.data, offset)[0]

    def _u16(self, offset: int) -> int:
        if offset + 2 > len(self.data):
            raise DbxError(f"the file ends before offset 0x{offset:x}")
        return struct.unpack_from("<H", self.data, offset)[0]

    def _in_range(self, offset: int, length: int = 1) -> bool:
        return 0 < offset and offset + length <= len(self.data)

    # -- the index tree --------------------------------------------------

    def message_offsets(self) -> list[int]:
        """Every message info-block offset, in index order.

        Walks the tree iteratively with a visited set: a corrupt DBX often
        contains a cycle, and recursion would exhaust the stack rather than
        reporting the damage.
        """
        if not self.root_node or not self._in_range(self.root_node, NODE_SIZE):
            self.structure_error = (
                f"the root index is at offset 0x{self.root_node:x}, which is "
                "outside the file"
            )
            return []

        offsets: list[int] = []
        visited: set[int] = set()
        stack: list[int] = [self.root_node]

        while stack:
            if len(visited) > MAX_NODES:
                self.structure_error = (
                    f"the index has more than {MAX_NODES:,} nodes, which means it "
                    "loops back on itself"
                )
                break

            node = stack.pop()
            if node in visited or not node:
                continue
            if not self._in_range(node, NODE_SIZE):
                self.structure_error = (
                    f"an index node points to offset 0x{node:x}, which is outside "
                    "the file"
                )
                continue
            visited.add(node)

            try:
                count = self._u16(node + NODE_ENTRY_COUNT)
            except DbxError as exc:
                self.structure_error = str(exc)
                continue

            if count > MAX_ENTRIES_PER_NODE:
                self.structure_error = (
                    f"the index node at 0x{node:x} claims {count} entries, but a "
                    f"node cannot hold more than {MAX_ENTRIES_PER_NODE}"
                )
                continue

            for i in range(count):
                entry = node + NODE_ENTRIES_START + i * NODE_ENTRY_SIZE
                if not self._in_range(entry, NODE_ENTRY_SIZE):
                    self.structure_error = (
                        f"an index entry at 0x{entry:x} runs past the end of the file"
                    )
                    break
                info_offset = struct.unpack_from("<I", self.data, entry)[0]
                child = struct.unpack_from("<I", self.data, entry + 4)[0]

                if info_offset and self._in_range(info_offset, INFO_ATTR_START):
                    offsets.append(info_offset)
                    self.last_good_offset = info_offset
                if child and child not in visited:
                    stack.append(child)

            # The child tree hanging below the whole node.
            try:
                child_tree = self._u32(node + 0x0C)
            except DbxError:
                child_tree = 0
            if child_tree and child_tree not in visited:
                stack.append(child_tree)

        return offsets

    # -- one message -----------------------------------------------------

    def messages(self) -> Iterator[tuple[int, bytes]]:
        """Yield (offset, raw RFC 822 bytes) for every message in the file."""
        for offset in self.message_offsets():
            try:
                raw = self.message_bytes(offset)
            except DbxError as exc:
                log.debug("Message at 0x%x could not be read: %s", offset, exc)
                if self.structure_error is None:
                    self.structure_error = str(exc)
                continue
            if raw:
                yield offset, raw

    def message_bytes(self, info_offset: int) -> bytes | None:
        """Follow the data-block chain from one message info block."""
        attributes = self._attributes(info_offset)
        first_block = attributes.get(ATTR_MESSAGE_TEXT)
        if not first_block:
            return None

        chunks: list[bytes] = []
        total = 0
        block = first_block
        seen: set[int] = set()

        while block:
            if block in seen:
                raise DbxError(
                    f"the message at 0x{info_offset:x} has a data chain that "
                    "loops back on itself"
                )
            seen.add(block)

            if not self._in_range(block, DATA_HEADER_SIZE):
                raise DbxError(
                    f"the message at 0x{info_offset:x} points to block 0x{block:x}, "
                    "which is outside the file"
                )

            next_block = struct.unpack_from("<I", self.data, block + DATA_NEXT_OFFSET)[0]
            length = struct.unpack_from("<I", self.data, block + DATA_LENGTH)[0]

            start = block + DATA_HEADER_SIZE
            end = start + length
            if length == 0:
                break
            if end > len(self.data):
                # Truncated: keep what is there rather than losing the message.
                end = len(self.data)
                self.structure_error = (
                    f"the message at 0x{info_offset:x} runs past the end of the "
                    "file; what could be read has been kept"
                )

            chunk = self.data[start:end]
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_MESSAGE_BYTES:
                raise DbxError(
                    f"the message at 0x{info_offset:x} claims to be larger than "
                    f"{MAX_MESSAGE_BYTES // (1024 * 1024)} MB, which is not plausible"
                )

            block = next_block

        return b"".join(chunks) if chunks else None

    def _attributes(self, info_offset: int) -> dict[int, int]:
        """The attribute table of one message info block.

        Values are three bytes. A value with the high bit of its id set is
        stored directly; otherwise it is an offset relative to the info block.
        """
        if not self._in_range(info_offset, INFO_ATTR_START):
            raise DbxError(f"the message info block at 0x{info_offset:x} is out of range")

        count = self.data[info_offset + INFO_ATTR_COUNT]
        out: dict[int, int] = {}

        for i in range(count):
            entry = info_offset + INFO_ATTR_START + i * 4
            if not self._in_range(entry, 4):
                raise DbxError(
                    f"the attribute table of the message at 0x{info_offset:x} "
                    "runs past the end of the file"
                )
            attr_id = self.data[entry] & 0x7F
            direct = bool(self.data[entry] & 0x80)
            value = int.from_bytes(self.data[entry + 1 : entry + 4], "little")

            if attr_id == ATTR_MESSAGE_TEXT & 0x7F and not direct:
                out[ATTR_MESSAGE_TEXT] = info_offset + value
            elif direct:
                out[attr_id] = value
            else:
                out[attr_id] = info_offset + value

        # The message-text attribute is 0x84: id 0x04 with the direct bit set.
        # Outlook Express stores it as a direct 32-bit offset into the file.
        if ATTR_MESSAGE_TEXT not in out:
            for i in range(count):
                entry = info_offset + INFO_ATTR_START + i * 4
                if self.data[entry] == 0x84:
                    out[ATTR_MESSAGE_TEXT] = int.from_bytes(
                        self.data[entry + 1 : entry + 4], "little"
                    )
                    break

        return out
