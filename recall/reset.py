"""Emptying the archive.

Two levels, both destructive only to Recall's own working folder. Nothing in
here ever touches a file the scanner found.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from . import db as db_module
from .config import Settings
from .logging_setup import get_logger

log = get_logger("reset")

# Everything extracted from source files. Order matters: children first.
_ITEM_TABLES = [
    "item_tags",
    "participations",
    "item_sources",
    "attachments",
    "search_docs",
    "items",
    "threads",
    "identities",
    "people",
    "folders",
    "findings",
    "coverage_months",
    "errors",
]


def reset_items(settings: Settings) -> str:
    """Delete everything extracted, keep the inventory of files found."""
    conn = db_module.connect(settings.db_path)
    try:
        counts = {t: _count(conn, t) for t in _ITEM_TABLES}
        with db_module.transaction(conn):
            # search_docs rows fire the FTS delete trigger, so items_fts empties
            # itself. The explicit rebuild afterwards guarantees it.
            for table in _ITEM_TABLES:
                conn.execute(f"DELETE FROM {table}")
            conn.execute(
                "UPDATE source_files SET parse_state = 'pending', "
                "parse_backend = NULL, parse_error = NULL, item_count = 0, "
                "first_item_utc = NULL, last_item_utc = NULL"
            )
        conn.execute("INSERT INTO items_fts(items_fts) VALUES ('rebuild')")
        conn.execute("VACUUM")
    finally:
        conn.close()

    blobs = _empty_blobs(settings.blobs_path)
    total = sum(counts.values())
    kept = _count_source_files(settings)
    log.info("reset --items removed %d rows and %d attachment files", total, blobs)
    return (
        f"Deleted {total:,} extracted records and {blobs:,} saved attachments.\n"
        f"Kept the list of {kept:,} files found by scanning, so you do not have "
        "to search your drives again.\n"
        "Run:  recall extract   to read them again."
    )


def reset_all(settings: Settings) -> str:
    """Delete the whole archive, including the inventory."""
    conn = db_module.connect(settings.db_path)
    try:
        sources = _count(conn, "source_files")
        items = _count(conn, "items")
    finally:
        conn.close()

    for suffix in ("", "-wal", "-shm"):
        p = Path(str(settings.db_path) + suffix)
        if p.exists():
            p.unlink()

    blobs = _empty_blobs(settings.blobs_path)

    # Recreate an empty archive so the next command finds a working database.
    conn = db_module.connect(settings.db_path)
    conn.close()

    log.info("reset --all removed %d sources and %d items", sources, items)
    return (
        f"Deleted the whole archive: {sources:,} files found, {items:,} records, "
        f"{blobs:,} saved attachments.\n"
        "Your original Outlook files were not touched.\n"
        "Run:  recall scan   to start again."
    )


def _empty_blobs(blobs_path: Path) -> int:
    """Remove every saved attachment. Returns how many files went."""
    if not blobs_path.exists():
        return 0
    count = sum(1 for _ in blobs_path.rglob("*") if _.is_file())
    shutil.rmtree(blobs_path, ignore_errors=True)
    blobs_path.mkdir(parents=True, exist_ok=True)
    return count


def _count(conn, table: str) -> int:
    try:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return int(row["n"]) if row else 0
    except Exception:  # noqa: BLE001 - a missing table counts as zero rows
        return 0


def _count_source_files(settings: Settings) -> int:
    conn = db_module.connect(settings.db_path)
    try:
        return _count(conn, "source_files")
    finally:
        conn.close()
