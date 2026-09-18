"""Fingerprinting files, and spotting the same file saved twice.

Hashing is BLAKE2b-256, streamed, so a 20 GB PST never lands in memory. Two
rules from the spec shape this module:

* A OneDrive placeholder is never hashed. Hashing means reading, and reading
  means downloading.
* Files above a configurable size are skipped on the first pass, so an
  inventory of an 84 GB collection finishes in minutes rather than hours. They
  are hashed later, on demand, when the user asks about duplicates.

A file with no hash is reported as "not compared yet", never as "unique".
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from ..logging_setup import get_logger

log = get_logger("scan.fingerprint")

CHUNK_SIZE = 1024 * 1024  # 1 MB


class HashSkipped(Exception):
    """Deliberately not hashed. ``reason`` says which rule applied."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(slots=True)
class HashResult:
    path: str
    content_hash: str | None
    bytes_read: int
    skipped_reason: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.content_hash is not None


def hash_file(
    path: str | Path,
    *,
    size_cap_bytes: int = 0,
    size_bytes: int | None = None,
    is_placeholder: bool = False,
    progress: Callable[[int], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> HashResult:
    """BLAKE2b-256 of a file's contents, streamed.

    ``size_cap_bytes`` of 0 means no cap. ``progress`` is called with the number
    of bytes read so far, so a long hash can show honest movement. ``cancel``
    is polled between chunks; returning True abandons the hash cleanly.
    """
    path = Path(path)
    p = str(path)

    if is_placeholder:
        return HashResult(
            p,
            None,
            0,
            skipped_reason="cloud-only file - hashing it would download it",
        )

    if size_bytes is None:
        try:
            size_bytes = path.stat().st_size
        except OSError as exc:
            return HashResult(p, None, 0, error=f"{exc.__class__.__name__}: {exc}")

    if size_cap_bytes and size_bytes > size_cap_bytes:
        return HashResult(
            p,
            None,
            0,
            skipped_reason=(
                f"larger than the {size_cap_bytes / (1024**3):.1f} GB first-pass "
                "limit - will be compared on request"
            ),
        )

    digest = hashlib.blake2b(digest_size=32)
    read = 0
    try:
        with open(path, "rb") as fh:
            while True:
                if cancel is not None and cancel():
                    return HashResult(p, None, read, skipped_reason="canceled")
                chunk = fh.read(CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                read += len(chunk)
                if progress is not None:
                    progress(read)
    except PermissionError as exc:
        return HashResult(p, None, read, error=f"PermissionError: {exc}")
    except OSError as exc:
        return HashResult(p, None, read, error=f"{exc.__class__.__name__}: {exc}")

    return HashResult(p, digest.hexdigest(), read)


def hash_bytes(data: bytes) -> str:
    """BLAKE2b-256 of an in-memory blob. Used for attachments."""
    return hashlib.blake2b(data, digest_size=32).hexdigest()


def sha256_hex(text: str) -> str:
    """SHA-256 of a string, for the dedup keys the spec specifies."""
    import hashlib as _h

    return _h.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DuplicateGroup:
    """Files whose contents are byte-for-byte identical."""

    content_hash: str
    keeper_id: int
    keeper_path: str
    duplicate_ids: list[int]
    size_bytes: int

    @property
    def wasted_bytes(self) -> int:
        return self.size_bytes * len(self.duplicate_ids)


def find_duplicate_groups(rows: Iterable[dict]) -> list[DuplicateGroup]:
    """Group source files by content hash.

    ``rows`` are dicts with id, path, content_hash and size_bytes. Rows without
    a hash are ignored - they have not been compared, which is not the same as
    being unique, and the caller reports them that way.

    The keeper is chosen deterministically: the shortest path, then the
    alphabetically first. Shortest tends to be the original rather than a copy
    buried under "Documents/Old/Backup of Backup".
    """
    by_hash: dict[str, list[dict]] = {}
    for row in rows:
        h = row.get("content_hash")
        if not h:
            continue
        by_hash.setdefault(h, []).append(row)

    groups: list[DuplicateGroup] = []
    for content_hash, members in by_hash.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda r: (len(str(r["path"])), str(r["path"])))
        keeper = members[0]
        groups.append(
            DuplicateGroup(
                content_hash=content_hash,
                keeper_id=int(keeper["id"]),
                keeper_path=str(keeper["path"]),
                duplicate_ids=[int(m["id"]) for m in members[1:]],
                size_bytes=int(keeper.get("size_bytes") or 0),
            )
        )
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def mark_duplicates(conn, groups: list[DuplicateGroup]) -> int:
    """Write ``duplicate_of`` for every duplicate. Returns rows marked.

    Idempotent: running a scan twice produces the same marks, never a chain of
    duplicates pointing at duplicates.
    """
    marked = 0
    for group in groups:
        for dup_id in group.duplicate_ids:
            conn.execute(
                "UPDATE source_files SET duplicate_of = ? WHERE id = ? "
                "AND (duplicate_of IS NULL OR duplicate_of != ?)",
                (group.keeper_id, dup_id, group.keeper_id),
            )
            marked += 1
    # A keeper is never a duplicate of anything.
    keeper_ids = [g.keeper_id for g in groups]
    if keeper_ids:
        placeholders = ",".join("?" * len(keeper_ids))
        conn.execute(
            f"UPDATE source_files SET duplicate_of = NULL WHERE id IN ({placeholders})",
            keeper_ids,
        )
    return marked


def file_times(stat_result: os.stat_result) -> tuple[str, str]:
    """(mtime, ctime) as UTC ISO strings."""
    from datetime import datetime, timezone

    def iso(ts: float) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return iso(stat_result.st_mtime), iso(stat_result.st_ctime)
