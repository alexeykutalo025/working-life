"""Phase 0: finding files, identifying them, and never touching them."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from recall.config import Settings, WorkdirSettings
from recall.models import Container
from recall.scan.walker import (
    LOCKED_ADVICE,
    Scanner,
    _excluded,
    classify_container,
    human_bytes,
    sniff,
    summarize,
    walk_roots,
)

# Sizes matter here. The integrity engine reports a container file that is too
# small to hold anything, so a fixture meant to look healthy has to be a
# plausible size for its type - otherwise the test would be asserting against a
# finding that is, in fact, correct.
PST_HEADER = b"!BDN" + b"\x00" * (300 * 1024)
OLE_HEADER = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 2048
DBX_HEADER = b"\xcf\xad\x12\xfe" + b"\x00" * 4096
MBX_HEADER = b"JMF9" + b"\x00" * 2048
ZIP_HEADER = b"PK\x03\x04" + b"\x00" * 2048


@pytest.fixture
def scan_settings(tmp_path: Path) -> Settings:
    s = Settings(workdir=WorkdirSettings(path=str(tmp_path / "workdir")))
    s.source_path = tmp_path / "config.toml"
    s.ensure_workdir()
    return s


def make_clean_tree(root: Path) -> None:
    """A tree with nothing wrong with it, for asserting zero findings.

    Deliberately contains no .ost: an .ost that belongs to no Outlook profile
    on this machine really is orphaned, and reporting it is the check working.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "mail.pst").write_bytes(PST_HEADER)
    (root / "note.msg").write_bytes(OLE_HEADER)
    (root / "diary.ics").write_text("BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR\n")
    (root / "card.vcf").write_text("BEGIN:VCARD\nVERSION:3.0\nEND:VCARD\n")


def make_tree(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "mail.pst").write_bytes(PST_HEADER)
    (root / "cache.ost").write_bytes(PST_HEADER)
    (root / "note.msg").write_bytes(OLE_HEADER)
    (root / "diary.ics").write_text("BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR\n")
    (root / "card.vcf").write_text("BEGIN:VCARD\nVERSION:3.0\nEND:VCARD\n")
    (root / "readme.txt").write_text("not an outlook file")
    (root / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0not an outlook file")
    sub = root / "Archive 1998"
    sub.mkdir()
    (sub / "old.dbx").write_bytes(DBX_HEADER)
    (sub / "older.mbx").write_bytes(MBX_HEADER)


# --- signature sniffing ---------------------------------------------------


def test_sniff_recognises_pst(tmp_path: Path):
    p = tmp_path / "a.pst"
    p.write_bytes(PST_HEADER)
    label, mismatch, _ = sniff(p, ".pst")
    assert "PST" in label
    assert mismatch is False


def test_sniff_recognises_ost_using_the_same_signature(tmp_path: Path):
    p = tmp_path / "a.ost"
    p.write_bytes(PST_HEADER)
    _, mismatch, _ = sniff(p, ".ost")
    assert mismatch is False


def test_sniff_catches_a_lying_extension(tmp_path: Path):
    """Spec 9.1 magic_mismatch: the name says .pst, the bytes say otherwise."""
    p = tmp_path / "definitely.pst"
    p.write_bytes(OLE_HEADER)
    label, mismatch, detail = sniff(p, ".pst")
    assert mismatch is True
    assert ".pst" in detail
    assert "MSG" in label


def test_sniff_flags_a_binary_type_with_no_known_signature(tmp_path: Path):
    p = tmp_path / "broken.pst"
    p.write_bytes(b"this is a word document, honestly" + b"\x00" * 50)
    _, mismatch, detail = sniff(p, ".pst")
    assert mismatch is True
    assert "first bytes" in detail


def test_sniff_accepts_text_formats_without_a_signature(tmp_path: Path):
    """An .eml can begin with any header; absence of a magic number is normal."""
    p = tmp_path / "message.eml"
    p.write_text("Subject: hello\r\n\r\nbody")
    _, mismatch, _ = sniff(p, ".eml")
    assert mismatch is False


def test_sniff_is_case_insensitive_for_text_signatures(tmp_path: Path):
    p = tmp_path / "cal.ics"
    p.write_text("begin:vcalendar\r\nend:vcalendar\r\n")
    label, mismatch, _ = sniff(p, ".ics")
    assert mismatch is False
    assert label == "calendar file"


def test_sniff_of_empty_file_says_nothing(tmp_path: Path):
    p = tmp_path / "empty.pst"
    p.write_bytes(b"")
    label, mismatch, _ = sniff(p, ".pst")
    assert label is None
    assert mismatch is False


def test_sniff_of_unreadable_file_does_not_raise(tmp_path: Path):
    label, mismatch, _ = sniff(tmp_path / "gone.pst", ".pst")
    assert label is None and mismatch is False


# --- exclusions -----------------------------------------------------------


@pytest.mark.parametrize("name", ["Windows", "node_modules", "$Recycle.Bin"])
def test_bare_name_exclusions(name):
    excludes = ["Windows", "node_modules", "$Recycle.Bin"]
    assert _excluded(Path(rf"C:\{name}"), excludes) is True


def test_tail_path_exclusion():
    excludes = ["AppData\\Local\\Temp"]
    assert _excluded(Path(r"C:\Users\Tim\AppData\Local\Temp"), excludes) is True
    assert _excluded(Path(r"C:\Users\Tim\AppData\Local\Microsoft"), excludes) is False


def test_similar_name_is_not_excluded():
    assert _excluded(Path(r"C:\Windows Backups"), ["Windows"]) is False


# --- walking --------------------------------------------------------------


def test_walk_finds_every_outlook_file_and_no_others(tmp_path: Path, scan_settings):
    root = tmp_path / "data"
    make_tree(root)
    found = {Path(c.path).name for c in walk_roots([root], scan_settings)}
    assert found == {
        "mail.pst", "cache.ost", "note.msg", "diary.ics", "card.vcf",
        "old.dbx", "older.mbx",
    }
    assert "readme.txt" not in found
    assert "photo.jpg" not in found


def test_walk_descends_into_subfolders(tmp_path: Path, scan_settings):
    root = tmp_path / "data"
    make_tree(root)
    paths = {c.path for c in walk_roots([root], scan_settings)}
    assert any("Archive 1998" in p for p in paths)


def test_walk_honours_exclusions(tmp_path: Path, scan_settings):
    root = tmp_path / "data"
    make_tree(root)
    excluded = root / "node_modules"
    excluded.mkdir()
    (excluded / "hidden.pst").write_bytes(PST_HEADER)
    found = {Path(c.path).name for c in walk_roots([root], scan_settings)}
    assert "hidden.pst" not in found


def test_walk_skips_a_missing_root(tmp_path: Path, scan_settings):
    """A drive that was unplugged is skipped, not a crash."""
    root = tmp_path / "data"
    make_tree(root)
    found = list(walk_roots([tmp_path / "nope", root], scan_settings))
    assert len(found) == 7


def test_walk_records_size_and_times(tmp_path: Path, scan_settings):
    root = tmp_path / "data"
    make_tree(root)
    by_name = {Path(c.path).name: c for c in walk_roots([root], scan_settings)}
    pst = by_name["mail.pst"]
    assert pst.size_bytes == len(PST_HEADER)
    assert pst.mtime_utc.endswith("Z")
    assert pst.ctime_utc.endswith("Z")
    assert pst.ext == ".pst"


def test_walk_marks_the_mismatched_file(tmp_path: Path, scan_settings):
    root = tmp_path / "data"
    root.mkdir()
    (root / "liar.pst").write_bytes(ZIP_HEADER)
    candidate = next(iter(walk_roots([root], scan_settings)))
    assert candidate.magic_mismatch is True
    assert "OLM" in (candidate.detected_type or "")


def test_walk_is_cancelable(tmp_path: Path, scan_settings):
    import threading

    root = tmp_path / "data"
    make_tree(root)
    cancel = threading.Event()
    cancel.set()
    assert list(walk_roots([root], scan_settings, cancel=cancel)) == []


def test_walk_counts_progress(tmp_path: Path, scan_settings):
    from recall.scan.walker import ScanProgress

    root = tmp_path / "data"
    make_tree(root)
    progress = ScanProgress()
    list(walk_roots([root], scan_settings, progress=progress))
    assert progress.candidates_found == 7
    assert progress.files_seen >= 9
    assert progress.dirs_seen >= 2


def test_walk_reports_an_unreadable_directory(tmp_path: Path, scan_settings, monkeypatch):
    root = tmp_path / "data"
    make_tree(root)
    seen: list[Path] = []

    real_scandir = os.scandir

    def deny(path):
        if "Archive 1998" in str(path):
            raise PermissionError(13, "Access is denied")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", deny)
    list(walk_roots([root], scan_settings, on_unreadable_dir=lambda p, e: seen.append(p)))
    assert seen and "Archive 1998" in str(seen[0])


# --- the source files are never touched -----------------------------------


def test_scanning_changes_nothing_on_disk(tmp_path: Path, scan_settings, conn):
    """The single most important promise in the whole program."""
    root = tmp_path / "data"
    make_tree(root)

    before = {
        str(p): (p.stat().st_size, p.stat().st_mtime, p.read_bytes())
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }

    Scanner(scan_settings, conn).run([root])

    after = {
        str(p): (p.stat().st_size, p.stat().st_mtime, p.read_bytes())
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }
    assert before == after, "the scanner modified a source file"


# --- the whole scan -------------------------------------------------------


def test_scan_populates_source_files(tmp_path: Path, scan_settings, conn):
    root = tmp_path / "data"
    make_tree(root)
    result = Scanner(scan_settings, conn).run([root])

    assert result.state == "done"
    assert result.candidates_found == 7
    rows = conn.execute("SELECT path, ext, content_hash FROM source_files").fetchall()
    assert len(rows) == 7
    assert all(r["content_hash"] for r in rows), "every readable local file is fingerprinted"


def test_scan_records_the_run(tmp_path: Path, scan_settings, conn):
    root = tmp_path / "data"
    make_tree(root)
    Scanner(scan_settings, conn).run([root])
    run = conn.execute("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert run["state"] == "done"
    assert run["finished_utc"]
    assert json.loads(run["roots_json"]) == [str(root)]


def test_rescanning_creates_no_duplicates(tmp_path: Path, scan_settings, conn):
    """Spec: re-running any step must never create duplicates."""
    root = tmp_path / "data"
    make_tree(root)
    Scanner(scan_settings, conn).run([root])
    first = conn.execute("SELECT COUNT(*) FROM source_files").fetchone()[0]
    Scanner(scan_settings, conn).run([root])
    second = conn.execute("SELECT COUNT(*) FROM source_files").fetchone()[0]
    assert first == second == 7


def test_rescanning_keeps_the_user_note_and_parse_state(tmp_path: Path, scan_settings, conn):
    root = tmp_path / "data"
    make_tree(root)
    Scanner(scan_settings, conn).run([root])
    conn.execute(
        "UPDATE source_files SET user_note = 'the Contract Marketing mailbox', "
        "parse_state = 'done', item_count = 42 WHERE ext = '.pst'"
    )
    Scanner(scan_settings, conn).run([root])
    row = conn.execute("SELECT * FROM source_files WHERE ext = '.pst'").fetchone()
    assert row["user_note"] == "the Contract Marketing mailbox"
    assert row["parse_state"] == "done"
    assert row["item_count"] == 42


def test_scan_marks_duplicates(tmp_path: Path, scan_settings, conn):
    root = tmp_path / "data"
    root.mkdir()
    (root / "mail.pst").write_bytes(PST_HEADER)
    backup = root / "Backup"
    backup.mkdir()
    (backup / "mail copy.pst").write_bytes(PST_HEADER)

    Scanner(scan_settings, conn).run([root])
    dupes = conn.execute(
        "SELECT path FROM source_files WHERE duplicate_of IS NOT NULL"
    ).fetchall()
    assert len(dupes) == 1
    assert "Backup" in dupes[0]["path"]


def test_scan_raises_magic_mismatch_finding(tmp_path: Path, scan_settings, conn):
    root = tmp_path / "data"
    root.mkdir()
    (root / "liar.pst").write_bytes(OLE_HEADER)
    Scanner(scan_settings, conn).run([root])

    finding = conn.execute(
        "SELECT * FROM findings WHERE code = 'magic_mismatch'"
    ).fetchone()
    assert finding is not None
    assert finding["severity"] == "high"
    assert "liar.pst" in finding["title"]
    assert finding["state"] == "open"


def test_scan_raises_zero_or_tiny_finding(tmp_path: Path, scan_settings, conn):
    root = tmp_path / "data"
    root.mkdir()
    (root / "empty.pst").write_bytes(b"")
    Scanner(scan_settings, conn).run([root])

    finding = conn.execute("SELECT * FROM findings WHERE code = 'zero_or_tiny'").fetchone()
    assert finding is not None
    assert "empty" in finding["title"]
    assert finding["severity"] == "medium"


def test_clean_scan_produces_no_findings(tmp_path: Path, scan_settings, conn):
    """A system that cries wolf is as useless as one that stays silent.

    In particular a one-event .ics and a one-contact .vcf are a few hundred
    bytes and that is entirely normal, so they must not be reported as
    implausibly small.
    """
    root = tmp_path / "data"
    make_clean_tree(root)
    Scanner(scan_settings, conn).run([root])
    findings = conn.execute(
        "SELECT code, title FROM findings WHERE state = 'open'"
    ).fetchall()
    assert [dict(f) for f in findings] == []


def test_scan_twice_does_not_duplicate_findings(tmp_path: Path, scan_settings, conn):
    root = tmp_path / "data"
    root.mkdir()
    (root / "liar.pst").write_bytes(OLE_HEADER)
    Scanner(scan_settings, conn).run([root])
    Scanner(scan_settings, conn).run([root])
    n = conn.execute("SELECT COUNT(*) FROM findings WHERE code='magic_mismatch'").fetchone()[0]
    assert n == 1


# --- summary --------------------------------------------------------------


def test_summary_of_nothing_says_so(conn):
    assert "No Outlook files were found" in summarize(conn)


def test_summary_counts_what_it_can_count(tmp_path: Path, scan_settings, conn):
    root = tmp_path / "data"
    make_tree(root)
    Scanner(scan_settings, conn).run([root])
    line = summarize(conn)
    assert "Found 7 Outlook files" in line


def test_summary_mentions_duplicates(tmp_path: Path, scan_settings, conn):
    """One duplicate reads as one, not as "1 are duplicates".

    The summary is the first sentence the user reads, and broken grammar there
    makes the whole thing look like it was written by a machine that is not
    paying attention.
    """
    root = tmp_path / "data"
    root.mkdir()
    (root / "a.pst").write_bytes(PST_HEADER)
    (root / "bb.pst").write_bytes(PST_HEADER)
    Scanner(scan_settings, conn).run([root])
    assert "1 is a duplicate of another file" in summarize(conn)


def test_summary_counts_several_duplicates_in_the_plural(
    tmp_path: Path, scan_settings, conn
):
    root = tmp_path / "data"
    root.mkdir()
    for name in ("a.pst", "bb.pst", "ccc.pst"):
        (root / name).write_bytes(PST_HEADER)
    Scanner(scan_settings, conn).run([root])
    assert "2 are duplicates of another file" in summarize(conn)


def test_summary_reports_uncompared_files_honestly(tmp_path: Path, scan_settings, conn):
    """A file we could not compare is not reported as unique."""
    root = tmp_path / "data"
    root.mkdir()
    (root / "big.pst").write_bytes(PST_HEADER)
    scan_settings.scan.hash_size_cap_mb = 0
    # A cap of 0 means "no cap", so force the skip through a tiny explicit cap.
    scanner = Scanner(scan_settings, conn)
    scanner.settings.scan.hash_size_cap_mb = 1
    conn.execute("DELETE FROM source_files")
    scanner.run([root])
    conn.execute("UPDATE source_files SET content_hash = NULL")
    assert "not yet known to be unique" in summarize(conn)


# --- byte formatting ------------------------------------------------------


def test_human_bytes_never_rounds_a_real_size_to_zero():
    """16 MB shown as "0.0 GB" is a rounding, and practically a lie."""
    assert human_bytes(16_818_176) == "16 MB"
    assert human_bytes(0) == "0 bytes"
    assert human_bytes(512) == "512 bytes"
    assert human_bytes(2048) == "2.0 KB"
    assert human_bytes(5 * 1024**3) == "5.0 GB"
    assert human_bytes(84 * 1024**3) == "84 GB"


# --- containers -----------------------------------------------------------


def test_network_path_is_classified_as_network():
    assert classify_container(Path(r"\\server\share\mail.pst"), []) == Container.NETWORK


def test_onedrive_path_is_classified_as_onedrive(tmp_path: Path):
    od = tmp_path / "OneDrive"
    od.mkdir()
    assert classify_container(od / "mail.pst", [od]) == Container.ONEDRIVE


def test_locked_advice_is_plain_language():
    assert LOCKED_ADVICE == "Close Outlook and scan again to include this file."
