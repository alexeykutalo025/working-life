"""The archive database is exactly what build spec section 4 describes."""

from __future__ import annotations

import sqlite3

import pytest

from recall import db as db_module
from recall.db import DatabaseError

# Every table named in spec section 4.
SPEC_TABLES = {
    "scan_runs",
    "source_files",
    "folders",
    "items",
    "item_sources",
    "people",
    "identities",
    "participations",
    "attachments",
    "threads",
    "eras",
    "tags",
    "item_tags",
    "errors",
    "findings",
    "coverage_months",
    "settings",
    "search_docs",
    "items_fts",
}

SPEC_INDEXES = {
    "idx_items_occurred",
    "idx_items_kind_occurred",
    "idx_items_thread",
    "idx_items_msgid",
    "idx_items_uid",
    "idx_part_person",
    "idx_att_hash",
    "idx_findings_state",
}


def test_every_spec_table_exists(conn):
    assert SPEC_TABLES <= db_module.table_names(conn)


def test_every_spec_index_exists(conn):
    assert SPEC_INDEXES <= db_module.index_names(conn)


def test_pragmas(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_integrity_check_is_clean(conn):
    assert db_module.integrity_check(conn) == "ok"


def test_items_columns_match_spec(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
    expected = {
        "id", "kind", "dedup_key", "occurred_utc", "occurred_local", "tz",
        "end_utc", "all_day", "subject", "body_text", "body_html", "body_format",
        "location", "importance", "sensitivity", "internet_message_id",
        "in_reply_to", "references_json", "thread_id", "conversation_topic",
        "ical_uid", "recurrence_json", "is_recurring_master", "recurrence_id",
        "meeting_status", "busy_status", "contact_json", "folder_id",
        "has_attachments", "raw_headers", "parse_confidence", "created_at",
    }
    assert cols == expected


def test_findings_columns_match_spec(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(findings)")}
    expected = {
        "id", "code", "severity", "title", "detail", "source_file_id", "item_id",
        "person_id", "period_start", "period_end", "affected_count",
        "estimated_loss", "evidence_json", "state", "user_note",
        "first_seen_utc", "last_seen_utc", "resolved_utc",
    }
    assert cols == expected


def test_dedup_key_is_unique(conn):
    conn.execute("INSERT INTO items(kind, dedup_key) VALUES ('message', 'abc')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO items(kind, dedup_key) VALUES ('message', 'abc')")


def test_findings_dedupe_despite_nulls(conn):
    """The reason ux_findings_key exists.

    The spec's UNIQUE(code, source_file_id, item_id, person_id, period_start)
    does not stop a second identical row when those columns are NULL, because
    SQLite treats NULLs as distinct. Nearly every finding has NULLs there, so
    without the extra index each `recall audit` would pile up duplicates.
    """
    conn.execute(
        "INSERT INTO findings(code, severity, title) VALUES ('no_date', 'medium', 'x')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO findings(code, severity, title) "
            "VALUES ('no_date', 'medium', 'x again')"
        )
    assert conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 1


def test_findings_with_different_periods_coexist(conn):
    conn.execute(
        "INSERT INTO findings(code, severity, title, period_start) "
        "VALUES ('hard_gap', 'high', 'Feb 2003 missing', '2003-02')"
    )
    conn.execute(
        "INSERT INTO findings(code, severity, title, period_start) "
        "VALUES ('hard_gap', 'high', 'Mar 2003 missing', '2003-03')"
    )
    assert conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 2


def test_fts_triggers_insert_update_delete(conn):
    conn.execute("INSERT INTO items(kind, dedup_key) VALUES ('message', 'k1')")
    conn.execute(
        "INSERT INTO search_docs(item_id, subject, body, participants, location, "
        "attachment_names, attachment_text) "
        "VALUES (1, 'Quarterly review', 'the quick brown fox', "
        "'tim@example.com', 'Dublin', 'notes.pdf', 'annual figures')"
    )

    def hits(q: str) -> list[int]:
        return [
            r[0] for r in conn.execute("SELECT rowid FROM items_fts WHERE items_fts MATCH ?", (q,))
        ]

    assert hits("quick") == [1]
    assert hits("Dublin") == [1]
    assert hits("figures") == [1]

    conn.execute("UPDATE search_docs SET body = 'something else' WHERE item_id = 1")
    assert hits("quick") == []
    assert hits("something") == [1]

    conn.execute("DELETE FROM search_docs WHERE item_id = 1")
    assert hits("something") == []


def test_fts_cascade_from_items(conn):
    conn.execute("INSERT INTO items(kind, dedup_key) VALUES ('message', 'k2')")
    item_id = conn.execute("SELECT id FROM items WHERE dedup_key='k2'").fetchone()[0]
    conn.execute(
        "INSERT INTO search_docs(item_id, subject, body) VALUES (?, 'gone soon', 'body')",
        (item_id,),
    )
    conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    assert conn.execute("SELECT COUNT(*) FROM search_docs").fetchone()[0] == 0
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM items_fts WHERE items_fts MATCH 'gone'"
        ).fetchone()[0]
        == 0
    )


def test_fts_rebuild_reproduces_index(conn):
    conn.execute("INSERT INTO items(kind, dedup_key) VALUES ('message', 'k3')")
    conn.execute(
        "INSERT INTO search_docs(item_id, subject, body) VALUES (1, 'rebuildable', 'text')"
    )
    conn.execute("INSERT INTO items_fts(items_fts) VALUES ('rebuild')")
    assert [
        r[0]
        for r in conn.execute("SELECT rowid FROM items_fts WHERE items_fts MATCH 'rebuildable'")
    ] == [1]


def test_migrate_is_idempotent(settings):
    c1 = db_module.connect(settings.db_path)
    before = db_module.table_names(c1)
    c1.close()
    c2 = db_module.connect(settings.db_path)
    assert db_module.table_names(c2) == before
    assert db_module.migrate(c2) == db_module.SCHEMA_VERSION
    c2.close()


def test_newer_schema_is_refused(settings):
    conn = db_module.connect(settings.db_path)
    conn.execute("INSERT INTO schema_migrations(version) VALUES (999)")
    conn.close()
    with pytest.raises(DatabaseError, match="newer version"):
        db_module.connect(settings.db_path)


def test_transaction_rolls_back(conn):
    with pytest.raises(ValueError):
        with db_module.transaction(conn):
            conn.execute("INSERT INTO items(kind, dedup_key) VALUES ('message', 'rb')")
            raise ValueError("boom")
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_transaction_commits(conn):
    with db_module.transaction(conn):
        conn.execute("INSERT INTO items(kind, dedup_key) VALUES ('message', 'ok')")
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


def test_settings_roundtrip(conn):
    assert db_module.get_setting(conn, "missing") is None
    assert db_module.get_setting(conn, "missing", "fallback") == "fallback"
    db_module.set_setting(conn, "k", "v1")
    db_module.set_setting(conn, "k", "v2")
    assert db_module.get_setting(conn, "k") == "v2"


def test_log_error_records_row(conn):
    rid = db_module.log_error(conn, "scan", "could not open file", detail="PermissionError")
    assert rid > 0
    row = conn.execute("SELECT * FROM errors WHERE id = ?", (rid,)).fetchone()
    assert row["stage"] == "scan"
    assert row["message"] == "could not open file"
    assert row["acknowledged"] == 0


def test_log_error_never_raises():
    """Nowhere left to complain to is not a reason to lose the caller's error."""
    closed = sqlite3.connect(":memory:")
    closed.close()
    assert db_module.log_error(closed, "scan", "x") == 0


def test_foreign_keys_cascade_participations(conn):
    conn.execute("INSERT INTO items(kind, dedup_key) VALUES ('message', 'fk')")
    conn.execute("INSERT INTO people(display_name) VALUES ('Tim')")
    conn.execute(
        "INSERT INTO identities(person_id, address, address_type) "
        "VALUES (1, 'tim@example.com', 'smtp')"
    )
    conn.execute(
        "INSERT INTO participations(item_id, person_id, identity_id, role) "
        "VALUES (1, 1, 1, 'from')"
    )
    conn.execute("DELETE FROM items WHERE id = 1")
    assert conn.execute("SELECT COUNT(*) FROM participations").fetchone()[0] == 0
    # The person survives their messages being deleted.
    assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 1


# --- upgrading an archive made by an earlier version ----------------------


def _version_one_archive(path):
    """An archive as it was before local_copy_path existed."""
    import re
    import sqlite3

    old_additions = re.sub(
        r"-- A cloud-only file.*?ON source_files\(local_copy_path\);",
        "",
        db_module.ADDITIONS_SQL,
        flags=re.S,
    )
    assert "local_copy_path" not in old_additions

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("BEGIN;" + db_module.SCHEMA_SQL + old_additions + "COMMIT;")
    conn.execute("INSERT INTO schema_migrations(version) VALUES (1)")
    conn.commit()
    return conn


def test_an_old_archive_gains_the_copy_columns(tmp_path):
    conn = _version_one_archive(tmp_path / "old.db")
    conn.execute(
        r"INSERT INTO source_files(path, ext, size_bytes) "r"VALUES ('C:\mail.pst', '.pst', 99)"
    )
    conn.commit()

    assert db_module.migrate(conn) == db_module.SCHEMA_VERSION

    columns = {r[1] for r in conn.execute("PRAGMA table_info(source_files)")}
    assert {"local_copy_path", "local_copy_bytes", "local_copy_utc"} <= columns
    # And the archive it was holding is still there.
    assert conn.execute("SELECT path FROM source_files").fetchone()[0] == r"C:\mail.pst"


def test_upgrading_twice_changes_nothing(tmp_path):
    conn = _version_one_archive(tmp_path / "old.db")
    db_module.migrate(conn)
    db_module.migrate(conn)

    versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations")]
    assert sorted(versions) == [1, 2]


def test_a_fresh_archive_and_an_upgraded_one_match(tmp_path):
    """The two paths run the same statements, so they cannot drift apart.

    This is the test that catches a column added to the schema and forgotten in
    the upgrade, which would work perfectly on the developer's machine and fail
    on every archive that already exists.
    """
    upgraded = _version_one_archive(tmp_path / "old.db")
    db_module.migrate(upgraded)
    fresh = db_module.connect(tmp_path / "new.db")

    def columns(conn):
        return [(r[1], r[2]) for r in conn.execute("PRAGMA table_info(source_files)")]

    assert columns(upgraded) == columns(fresh)


def test_an_archive_from_a_newer_recall_is_refused(tmp_path):
    conn = db_module.connect(tmp_path / "future.db")
    conn.execute(
        "INSERT INTO schema_migrations(version) VALUES (?)",
        (db_module.SCHEMA_VERSION + 1,),
    )
    with pytest.raises(db_module.DatabaseError):
        db_module.migrate(conn)
