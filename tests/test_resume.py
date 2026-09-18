"""Stopping and carrying on.

Spec section 12, stated as a requirement to test:

    A crash mid-extraction followed by --resume must produce exactly the same
    final database as an uninterrupted run. Test this.

So that is what this does: two archives built from the same files, one in a
single run and one interrupted part-way and resumed, compared row by row on
everything that is not a timestamp of when the work happened.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from recall.config import Settings, WorkdirSettings
from recall.db import connect
from recall.extract import Extractor
from recall.scan.walker import Scanner
from tests.fixtures.generate import generate_eml, generate_ics, generate_mbox, generate_vcf


def make_settings(tmp_path: Path, name: str) -> Settings:
    s = Settings(workdir=WorkdirSettings(path=str(tmp_path / name)))
    s.source_path = tmp_path / "config.toml"
    s.ensure_workdir()
    s.extract.pst_backend = "pypff"
    s.extract.cross_check_backends = False
    return s


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    fixtures = tmp_path / "corpus"
    generate_eml(fixtures)
    generate_mbox(fixtures)
    generate_ics(fixtures)
    generate_vcf(fixtures)
    return fixtures


def snapshot(conn) -> dict:
    """Everything about an archive that should not depend on how it was built.

    Row ids are deliberately excluded: they are assigned in insertion order, so
    an interrupted run legitimately numbers things differently. What must match
    is the content - the same records, the same people, the same provenance.
    """
    def rows(sql: str) -> list[tuple]:
        return sorted(tuple(r) for r in conn.execute(sql))

    return {
        "items": rows(
            "SELECT dedup_key, kind, occurred_utc, occurred_local, tz, end_utc, "
            "all_day, subject, body_text, body_format, location, importance, "
            "internet_message_id, in_reply_to, ical_uid, is_recurring_master, "
            "has_attachments, parse_confidence FROM items"
        ),
        "provenance": rows(
            "SELECT i.dedup_key, sf.path, s.native_id FROM item_sources s "
            "JOIN items i ON i.id = s.item_id "
            "JOIN source_files sf ON sf.id = s.source_file_id"
        ),
        "participations": rows(
            "SELECT i.dedup_key, ident.address, ident.address_type, p.role "
            "FROM participations p JOIN items i ON i.id = p.item_id "
            "JOIN identities ident ON ident.id = p.identity_id"
        ),
        "people": rows(
            "SELECT display_name, is_self, item_count, first_seen_utc, last_seen_utc "
            "FROM people WHERE merged_into IS NULL"
        ),
        "identities": rows(
            "SELECT address, address_type, use_count FROM identities"
        ),
        "attachments": rows(
            "SELECT i.dedup_key, a.filename, a.content_hash, a.size_bytes, "
            "a.extract_state FROM attachments a JOIN items i ON i.id = a.item_id"
        ),
        "folders": rows("SELECT path FROM folders"),
        "sources": rows(
            "SELECT path, parse_state, item_count, first_item_utc, last_item_utc "
            "FROM source_files"
        ),
        "search_docs": rows(
            "SELECT i.dedup_key, d.subject, d.body, d.participants, "
            "d.attachment_names FROM search_docs d JOIN items i ON i.id = d.item_id"
        ),
        "coverage": rows(
            "SELECT month, kind, item_count, source_count, gap_class FROM coverage_months"
        ),
        "findings": rows(
            "SELECT code, severity, title, period_start, period_end, "
            "affected_count, estimated_loss, state FROM findings"
        ),
        "threads": rows("SELECT subject_normalized, message_count FROM threads"),
    }


def uninterrupted(settings: Settings, corpus: Path) -> dict:
    conn = connect(settings.db_path)
    try:
        Scanner(settings, conn).run([corpus])
        Extractor(settings, conn).run()
        return snapshot(conn)
    finally:
        conn.close()


def interrupted_then_resumed(settings: Settings, corpus: Path, stop_after: int) -> dict:
    """Read some files, stop as if the machine died, then resume."""
    conn = connect(settings.db_path)
    try:
        Scanner(settings, conn).run([corpus])

        cancel = threading.Event()
        extractor = Extractor(settings, conn, cancel=cancel)

        def stop_when_enough(progress) -> None:
            if progress.files_done >= stop_after:
                cancel.set()

        extractor.on_progress = stop_when_enough
        first = extractor.run()
        assert first.state == "canceled", "the run was supposed to be interrupted"
    finally:
        conn.close()

    # A new connection, as a fresh process would have: nothing carried over in
    # memory, only what was committed to disk.
    conn = connect(settings.db_path)
    try:
        second = Extractor(settings, conn).run(resume=True)
        assert second.state == "done"
        return snapshot(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------


def test_resuming_produces_the_same_archive(tmp_path: Path, corpus: Path):
    """The requirement, stated plainly and checked table by table."""
    whole = uninterrupted(make_settings(tmp_path, "whole"), corpus)
    resumed = interrupted_then_resumed(
        make_settings(tmp_path, "resumed"), corpus, stop_after=3
    )

    assert set(whole) == set(resumed)
    for table in sorted(whole):
        assert resumed[table] == whole[table], f"{table} differs after a resume"


def test_an_interrupted_run_really_was_interrupted(tmp_path: Path, corpus: Path):
    """Guard against the comparison passing because nothing was interrupted."""
    settings = make_settings(tmp_path, "partial")
    conn = connect(settings.db_path)
    try:
        Scanner(settings, conn).run([corpus])
        cancel = threading.Event()
        extractor = Extractor(settings, conn, cancel=cancel)
        extractor.on_progress = lambda p: cancel.set() if p.files_done >= 2 else None
        result = extractor.run()

        assert result.state == "canceled"
        assert result.files_done < result.files_total
        assert conn.execute(
            "SELECT COUNT(*) FROM source_files WHERE parse_state = 'pending'"
        ).fetchone()[0] > 0, "there is work left to resume"
    finally:
        conn.close()


def test_resuming_reads_a_file_that_was_half_read_again(tmp_path: Path, corpus: Path):
    """A half-read file is not a read file.

    Interrupting mid-file leaves it 'parsing'. Resuming must read it from the
    start rather than trusting a partial result - the dedup keys make that
    free, and the alternative is a file silently missing its second half.
    """
    settings = make_settings(tmp_path, "halfread")
    conn = connect(settings.db_path)
    try:
        Scanner(settings, conn).run([corpus])
        source = conn.execute(
            "SELECT id FROM source_files WHERE ext = '.mbox' LIMIT 1"
        ).fetchone()
        conn.execute(
            "UPDATE source_files SET parse_state = 'parsing' WHERE id = ?",
            (source["id"],),
        )

        pending = Extractor(settings, conn)._sources_to_read(None, resume=True)
        assert any(int(r["id"]) == int(source["id"]) for r in pending), (
            "a file left mid-read must be read again"
        )
    finally:
        conn.close()


def test_running_twice_adds_nothing(tmp_path: Path, corpus: Path):
    """Re-running any step must never create duplicates."""
    settings = make_settings(tmp_path, "twice")
    conn = connect(settings.db_path)
    try:
        Scanner(settings, conn).run([corpus])
        Extractor(settings, conn).run()
        first = snapshot(conn)

        Scanner(settings, conn).run([corpus])
        Extractor(settings, conn).run(resume=False)
        second = snapshot(conn)

        assert second["items"] == first["items"]
        assert second["provenance"] == first["provenance"]
        assert second["attachments"] == first["attachments"]
    finally:
        conn.close()


def test_a_cancelled_run_keeps_what_it_read(tmp_path: Path, corpus: Path):
    """Ctrl-C finishes the current transaction and exits cleanly."""
    settings = make_settings(tmp_path, "cancelled")
    conn = connect(settings.db_path)
    try:
        Scanner(settings, conn).run([corpus])
        cancel = threading.Event()
        extractor = Extractor(settings, conn, cancel=cancel)
        extractor.on_progress = lambda p: cancel.set() if p.files_done >= 4 else None
        result = extractor.run()

        assert result.state == "canceled"
        assert "saved" in result.message
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] > 0, (
            "everything read before the stop is still there"
        )
    finally:
        conn.close()
