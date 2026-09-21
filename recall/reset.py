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


#: What a full reset removes on top of the extracted records: the inventory of
#: files found, and the things the user set up around it.
_INVENTORY_TABLES = [
    "tags",
    "eras",
    "settings",
    "source_files",
    "scan_runs",
]


def reset_everything(conn, settings: Settings) -> dict[str, int]:
    """Empty the whole archive through a connection that stays open.

    ``reset_all`` deletes the database file, which the web server cannot do:
    Windows will not unlink a file that the server's own connections still
    hold open, and a connection left pointing at a deleted archive is worse
    than the refusal. So the same job is done by emptying every table, and the
    connection the caller handed in goes on working afterwards.
    """
    tables = _ITEM_TABLES + _INVENTORY_TABLES
    counts = {t: _count(conn, t) for t in tables}

    with db_module.transaction(conn):
        # No ordering of these tables satisfies every foreign key on the way
        # through: findings point at items, items at folders, folders back at
        # source_files, and findings at source_files again. Whichever goes
        # first leaves a reference dangling for the length of one statement.
        # Deferring the check to the commit - by which time every row is gone
        # - is what makes emptying the lot possible at all.
        conn.execute("PRAGMA defer_foreign_keys = ON")
        for table in tables:
            conn.execute(f"DELETE FROM {table}")

    conn.execute("INSERT INTO items_fts(items_fts) VALUES ('rebuild')")
    try:
        conn.execute("VACUUM")
    except Exception:  # reclaiming the space is not the point of this
        # Another connection reading at that moment holds the exclusive lock
        # off. The archive is empty either way; only the size of the file on
        # disk is left behind, and the next VACUUM will get it.
        log.warning("Could not reclaim disk space after the reset", exc_info=True)

    blobs = _empty_blobs(settings.blobs_path)
    copies = _empty_blobs(settings.cloud_path)

    log.info(
        "reset everything: removed %d sources, %d items, %d attachment files, "
        "%d downloaded copies",
        counts["source_files"], counts["items"], blobs, copies,
    )
    return {
        "sources": counts["source_files"],
        "items": counts["items"],
        "attachments": blobs,
        "copies": copies,
    }


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
    # The downloaded copies go too. "Delete the whole archive" leaving eighty
    # gigabytes of copied mailboxes behind is not what anyone means by it.
    # reset_items deliberately keeps them: those were expensive to fetch, and
    # re-reading them is exactly what that command is for.
    copies = _empty_blobs(settings.cloud_path)

    # Recreate an empty archive so the next command finds a working database.
    conn = db_module.connect(settings.db_path)
    conn.close()

    log.info(
        "reset --all removed %d sources, %d items and %d copies",
        sources, items, copies,
    )
    return (
        f"Deleted the whole archive: {sources:,} files found, {items:,} records, "
        f"{blobs:,} saved attachments"
        + (
            f", and {copies:,} file(s) Recall had downloaded from OneDrive"
            if copies else ""
        )
        + ".\n"
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
