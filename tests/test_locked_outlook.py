"""Outlook data files that Outlook itself is holding open.

Windows will not let a second program open a .pst that Outlook has mounted, so
Recall used to record it as unreadable and skip it for good. But the user can
see that mailbox in Outlook, and expects to find it in the archive - and
Outlook itself can read a store it already has open.

Two things have to be right for that to be honest rather than merely clever:
the rule for which files qualify must be narrow, and a file that is tried and
not read must end up recorded as failed. A file recorded as read while holding
nothing is the one outcome that makes a short archive look complete.
"""

from __future__ import annotations

import pytest

from recall.db import transaction
from recall.parsers.pst_backend import (
    LOCKED_BY_OUTLOOK_SQL,
    looks_locked_by_outlook,
)

PERMISSION = "PermissionError: [Errno 13] Permission denied: 'C:\\mail.pst'"
IN_USE = "OSError: the file is being used by another process"

# (extension, lock_error, should Outlook be asked)
CASES = [
    (".pst", PERMISSION, True),
    (".PST", IN_USE, True),
    # Outlook cannot help with a format it does not own.
    (".mbox", PERMISSION, False),
    (".dbx", IN_USE, False),
    (".msg", PERMISSION, False),
    # Recall does not read .ost at all, so a busy one has nothing to route:
    # unlocking it would only get it as far as a reader that declines it.
    (".ost", PERMISSION, False),
    (".ost", IN_USE, False),
    # Broken, not busy. Sending this to Outlook spends two minutes finding out
    # what the error already said.
    (".pst", "OSError: [Errno 5] Input/output error", False),
    (".pst", "FileNotFoundError: no such file", False),
    (".pst", None, False),
    (".pst", "", False),
]


@pytest.mark.parametrize("ext,err,expected", CASES)
def test_only_a_busy_outlook_file_qualifies(ext, err, expected):
    assert looks_locked_by_outlook(ext, err) is expected


@pytest.mark.parametrize("ext,err,expected", CASES)
def test_the_sql_and_the_python_agree(conn, ext, err, expected):
    """Two languages, one rule.

    The predicate exists twice - once to pick rows out of the database and once
    to decide what to do with a row already in hand. If they ever disagreed,
    files would be selected for a treatment they then did not get, and would
    silently go unread.
    """
    with transaction(conn):
        conn.execute("DELETE FROM source_files")
        conn.execute(
            "INSERT INTO source_files(path, ext, size_bytes, is_placeholder, "
            "is_readable, lock_error) VALUES ('C:\\x', ?, 1000, 0, 0, ?)",
            (ext, err),
        )

    found = conn.execute(
        f"SELECT COUNT(*) FROM source_files WHERE {LOCKED_BY_OUTLOOK_SQL}"
    ).fetchone()[0]
    assert bool(found) is expected


def test_a_readable_file_never_qualifies(conn):
    """The whole point is a file Windows refused. One it did not is ordinary."""
    with transaction(conn):
        conn.execute("DELETE FROM source_files")
        conn.execute(
            "INSERT INTO source_files(path, ext, size_bytes, is_placeholder, "
            "is_readable, lock_error) VALUES ('C:\\ok.pst', '.pst', 1000, 0, 1, NULL)"
        )
    found = conn.execute(
        f"SELECT COUNT(*) FROM source_files WHERE {LOCKED_BY_OUTLOOK_SQL}"
    ).fetchone()[0]
    assert found == 0


def test_a_cloud_only_file_never_qualifies(conn):
    """A file whose bytes are not here is not a file Outlook has mounted."""
    with transaction(conn):
        conn.execute("DELETE FROM source_files")
        conn.execute(
            "INSERT INTO source_files(path, ext, size_bytes, is_placeholder, "
            "is_readable, lock_error) VALUES ('C:\\c.pst', '.pst', 1000, 1, 0, ?)",
            (PERMISSION,),
        )
    found = conn.execute(
        f"SELECT COUNT(*) FROM source_files WHERE {LOCKED_BY_OUTLOOK_SQL}"
    ).fetchone()[0]
    assert found == 0


# --- what the extractor does with one --------------------------------------


def test_a_locked_store_is_offered_to_the_extractor(settings, conn):
    """It used to be filtered out before a parser was ever chosen."""
    from recall.extract import Extractor

    with transaction(conn):
        conn.execute(
            "INSERT INTO source_files(path, ext, size_bytes, is_placeholder, "
            "is_readable, lock_error) VALUES ('C:\\busy.pst', '.pst', 99999, 0, 0, ?)",
            (PERMISSION,),
        )

    rows = Extractor(settings, conn)._sources_to_read(None, resume=True)
    assert [r["path"] for r in rows] == ["C:\\busy.pst"]


def test_a_locked_file_of_another_kind_is_still_left_alone(settings, conn):
    from recall.extract import Extractor

    with transaction(conn):
        conn.execute(
            "INSERT INTO source_files(path, ext, size_bytes, is_placeholder, "
            "is_readable, lock_error) VALUES ('C:\\busy.mbox', '.mbox', 999, 0, 0, ?)",
            (PERMISSION,),
        )

    rows = Extractor(settings, conn)._sources_to_read(None, resume=True)
    assert rows == []


def test_a_reader_that_produced_nothing_and_errored_is_failed_not_done(
    settings, conn, monkeypatch, tmp_path
):
    """The hole this feature would otherwise open wide.

    PstParser.parse is a generator. When no backend is available - Outlook not
    installed, which is exactly the case for a locked file on a machine without
    it - the loop falls out of the bottom, sets outcome.error and *returns*. A
    generator that returns raises nothing, so the file was recorded 'done' with
    no records and nothing anywhere said it had not been read.
    """
    from recall.extract import Extractor
    from recall.models import ParseOutcome
    from recall.parsers import base as base_module

    real = tmp_path / "busy.pst"
    real.write_bytes(b"!BDN" + b"\0" * 1000)

    class DeclinesEverything:
        name = "pst"
        extensions = frozenset({".pst"})

        def __init__(self, path, **kwargs):
            self.outcome = ParseOutcome(source_path=str(path), backend="pst")
            self.outcome.error = (
                "Neither the built-in reader nor Microsoft Outlook is available."
            )

        def parse(self, kinds=None):
            return iter(())

        def close(self):
            pass

    monkeypatch.setattr(base_module, "parser_for", lambda p, e: DeclinesEverything)
    monkeypatch.setattr(
        "recall.extract.parser_for", lambda p, e: DeclinesEverything
    )

    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO source_files(path, ext, size_bytes, is_placeholder, "
            "is_readable, lock_error) VALUES (?, '.pst', 1004, 0, 0, ?)",
            (str(real), PERMISSION),
        )
        source_id = cur.lastrowid

    extractor = Extractor(settings, conn)
    extractor.run(resume=True)

    row = conn.execute(
        "SELECT parse_state, parse_error, item_count FROM source_files WHERE id = ?",
        (source_id,),
    ).fetchone()
    assert row["parse_state"] == "failed"
    assert row["item_count"] == 0
    assert "Outlook" in (row["parse_error"] or "")
