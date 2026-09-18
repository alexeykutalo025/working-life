"""Exporting a search result set, and copying its attachments out."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from recall.export import ExportError
from recall.export.selection import _safe_name, _unique, copy_attachments_out, export_search
from recall.extract import Extractor
from recall.scan.walker import Scanner
from tests.fixtures.generate import generate_eml, generate_ics


@pytest.fixture
def archive(tmp_path: Path, settings, conn):
    fixtures = tmp_path / "corpus"
    generate_eml(fixtures)
    generate_ics(fixtures)
    Scanner(settings, conn).run([fixtures])
    Extractor(settings, conn).run()
    return conn


# --- exporting a search ---------------------------------------------------


def test_a_search_export_writes_only_what_matched(archive, settings):
    result = export_search(archive, settings, fmt="csv", query="invoice")
    assert result["files"]

    data = Path(result["files"][0]["file"])
    with open(data, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))

    assert rows
    assert all("Invoice" in r["subject"] for r in rows)


def test_a_search_export_gets_its_own_statement(archive, settings):
    result = export_search(archive, settings, fmt="csv", query="invoice")
    statement = Path(result["files"][0]["integrity_file"])
    assert statement.exists()
    text = statement.read_text(encoding="utf-8")
    assert "invoice" in text.lower(), "the statement names the search it covers"


def test_the_statement_describes_the_selection_not_the_whole_archive(archive, settings):
    result = export_search(
        archive, settings, fmt="csv", kind="message", date_from="2003", date_to="2003"
    )
    statement = Path(result["files"][0]["integrity_file"]).read_text(encoding="utf-8")
    assert "2003" in statement


def test_each_kind_gets_its_own_file(archive, settings):
    """A calendar entry and a contact do not share a set of columns."""
    result = export_search(archive, settings, fmt="csv", query="")
    kinds = {f["kind"] for f in result["files"]}
    assert len(kinds) >= 2
    assert len(result["files"]) == len(kinds)


def test_filtering_by_kind_gives_one_file(archive, settings):
    result = export_search(archive, settings, fmt="csv", kind="event")
    assert len(result["files"]) == 1
    assert result["files"][0]["kind"] == "event"


def test_a_date_range_narrows_the_export(archive, settings):
    everything = export_search(archive, settings, fmt="csv", kind="message")
    narrowed = export_search(
        archive, settings, fmt="csv", kind="message",
        date_from="2003", date_to="2003",
        out_path=Path(everything["files"][0]["file"]).parent / "narrowed",
    )
    assert narrowed["total_records"] < everything["total_records"]


def test_a_search_matching_nothing_is_refused_with_a_reason(archive, settings):
    with pytest.raises(ExportError, match="matched nothing"):
        export_search(archive, settings, fmt="csv", query="zzzznothingzzzz")


def test_json_export_of_a_search_carries_the_findings_key(archive, settings):
    result = export_search(archive, settings, fmt="json", kind="event")
    document = json.loads(Path(result["files"][0]["file"]).read_text(encoding="utf-8"))
    assert "findings" in document
    assert document["record_count"] == result["files"][0]["count"]


def test_undated_records_can_be_exported_on_their_own(archive, settings):
    result = export_search(archive, settings, fmt="csv", undated=True)
    data = Path(result["files"][0]["file"])
    with open(data, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows
    assert all(not r["date"] for r in rows), "an undated export contains no dates"


# --- copying attachments out ----------------------------------------------


def test_attachments_are_copied_with_a_manifest(archive, settings, tmp_path):
    item_ids = [
        int(r["id"])
        for r in archive.execute("SELECT id FROM items WHERE has_attachments = 1")
    ]
    assert item_ids, "the fixtures include attachments"

    target = tmp_path / "out"
    result = copy_attachments_out(archive, settings, item_ids, target)

    assert result["copied"] >= 1
    assert result["missing"] == 0
    assert (target / "_where these came from.txt").exists()

    manifest = (target / "_where these came from.txt").read_text(encoding="utf-8")
    assert "came from" in manifest


def test_two_attachments_with_the_same_name_both_arrive(archive, settings, tmp_path):
    """Two different invoices both called invoice.pdf must both be saved."""
    item_id = int(archive.execute("SELECT id FROM items LIMIT 1").fetchone()["id"])
    from recall.normalize.attachments import BlobStore

    store = BlobStore(settings.blobs_path)
    for content in (b"first invoice", b"second invoice"):
        blob = store.put(content, "invoice.pdf")
        archive.execute(
            "INSERT INTO attachments(item_id, filename, content_hash, extract_state) "
            "VALUES (?, 'invoice.pdf', ?, 'done')",
            (item_id, blob.content_hash),
        )

    target = tmp_path / "collide"
    result = copy_attachments_out(archive, settings, [item_id], target)

    saved = sorted(p.name for p in target.iterdir() if p.suffix == ".pdf")
    assert len(saved) == 2, saved
    assert result["copied"] == 2


def test_a_missing_blob_is_reported_not_skipped_silently(archive, settings, tmp_path):
    from recall.normalize.attachments import BlobStore

    item_id = int(archive.execute("SELECT id FROM items LIMIT 1").fetchone()["id"])
    store = BlobStore(settings.blobs_path)
    blob = store.put(b"about to vanish", "gone.pdf")
    archive.execute(
        "INSERT INTO attachments(item_id, filename, content_hash, extract_state) "
        "VALUES (?, 'gone.pdf', ?, 'done')",
        (item_id, blob.content_hash),
    )
    blob.path.unlink()

    target = tmp_path / "out"
    result = copy_attachments_out(archive, settings, [item_id], target)

    assert result["missing"] == 1
    manifest = (target / "_where these came from.txt").read_text(encoding="utf-8")
    assert "MISSING" in manifest


def test_copying_into_onedrive_is_refused(archive, settings, tmp_path, monkeypatch):
    from recall.scan.onedrive import HydrationRefused

    onedrive = tmp_path / "OneDrive"
    onedrive.mkdir()
    monkeypatch.setenv("OneDrive", str(onedrive))

    with pytest.raises(HydrationRefused, match="OneDrive"):
        copy_attachments_out(archive, settings, [1], onedrive / "attachments")


# --- safe filenames -------------------------------------------------------


@pytest.mark.parametrize("dangerous", [
    "../../startup/evil.exe",
    "..\\..\\windows\\system32\\evil.dll",
    "with:colon.pdf",
    "pipe|name.pdf",
    'quote"name.pdf',
    "star*.pdf",
    "trailing space .pdf ",
    "\x00null.pdf",
])
def test_a_dangerous_filename_cannot_escape_the_target_folder(dangerous, tmp_path):
    """The property that matters: whatever the attachment was called, the file
    lands inside the folder the user chose and nowhere else."""
    safe = _safe_name(dangerous)

    assert "/" not in safe and "\\" not in safe
    assert not safe.startswith(".")
    assert ".." not in safe

    target = (tmp_path / safe).resolve()
    assert target.parent == tmp_path.resolve()


def test_windows_reserved_names_are_escaped():
    """CON.pdf is not a filename Windows will accept."""
    assert _safe_name("CON.pdf").startswith("_")
    assert _safe_name("LPT1.txt").startswith("_")


def test_an_empty_name_still_gets_one():
    assert _safe_name("") == "attachment"
    assert _safe_name("...") == "attachment"


def test_a_long_name_is_shortened():
    assert len(_safe_name("x" * 500 + ".pdf")) <= 180


def test_unicode_survives():
    assert "é" in _safe_name("devis-été.pdf")


def test_collisions_are_numbered():
    used = {"invoice.pdf"}
    assert _unique("invoice.pdf", used) == "invoice (2).pdf"
    used.add("invoice (2).pdf")
    assert _unique("invoice.pdf", used) == "invoice (3).pdf"


def test_a_name_with_no_extension_collides_safely():
    assert _unique("invoice", {"invoice"}) == "invoice (2)"
