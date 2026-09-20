"""Find, download and read, as one pass.

No OneDrive here and no network: a "cloud-only" file is an ordinary file whose
row says is_placeholder = 1, which is exactly what the pass keys off. What is
being tested is the order the four steps run in, what survives a stop, and the
promise that no file this touches is left in a state that means nothing.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from recall.readall import (
    cloud_only_rows,
    forget_missing_copies,
    run_read_all,
)


class RecordingJob:
    """Stands in for JobManager, and remembers the order of the steps."""

    def __init__(self, cancel_after_phase: str | None = None) -> None:
        self.cancel_event = threading.Event()
        self.phases: list[str] = []
        self.messages: list[str] = []
        self.units: list[str] = []
        self._cancel_after = cancel_after_phase

    def begin_phase(self, name, *, index, count, total=None, unit="files", message=""):
        self.phases.append(name)
        self.units.append(unit)
        if message:
            self.messages.append(message)
        if self._cancel_after and self.phases[-1] == self._cancel_after:
            self.cancel_event.set()

    def progress(self, **kwargs):
        pass

    def set_detail(self, **kwargs):
        pass

    def finish(self, message, **detail):
        self.messages.append(message)


@pytest.fixture
def mail_root(tmp_path: Path) -> Path:
    """A folder with one ordinary mailbox and one that pretends to be cloudy."""
    root = tmp_path / "mail"
    root.mkdir()
    (root / "here.eml").write_bytes(
        b"From: a@example.com\r\nTo: b@example.com\r\n"
        b"Subject: on this computer\r\nMessage-ID: <1@x>\r\n"
        b"Date: Mon, 1 Mar 2004 10:00:00 +0000\r\n\r\nBody.\r\n"
    )
    (root / "cloudy.eml").write_bytes(
        b"From: c@example.com\r\nTo: d@example.com\r\n"
        b"Subject: in the cloud\r\nMessage-ID: <2@x>\r\n"
        b"Date: Tue, 2 Mar 2004 10:00:00 +0000\r\n\r\nBody.\r\n"
    )
    return root


@pytest.fixture
def cloud_only(monkeypatch):
    """Make the scanner itself see chosen files as OneDrive placeholders.

    Marking the row afterwards does not work, and should not: the scan reads
    the attribute bits off the disk every time, so a row edited by hand is
    corrected back on the next run. Patching the check is the only way to get
    a placeholder into the database by the route the real one takes.
    """
    from recall.scan import walker as walker_module

    names: set[str] = set()

    def mark(*filenames: str) -> None:
        names.update(filenames)

    # Patched at _inspect rather than at is_placeholder, which is handed a stat
    # result and so cannot tell one file from another.
    real_inspect = walker_module._inspect

    def inspect(path, ext, od_roots):
        candidate = real_inspect(path, ext, od_roots)
        if candidate is not None and path.name in names:
            candidate.is_placeholder = True
            candidate.placeholder_reason = "offline"
        return candidate

    monkeypatch.setattr(walker_module, "_inspect", inspect)
    return mark


def _row_id(conn, name: str) -> int:
    return int(
        conn.execute(
            "SELECT id FROM source_files WHERE path LIKE ?", (f"%{name}",)
        ).fetchone()["id"]
    )


def test_the_four_steps_run_in_order(settings, conn, mail_root):
    job = RecordingJob()
    run_read_all(settings, conn, job=job, roots=[mail_root])
    # Nothing was cloud-only on the first run, so there was nothing to download
    # and nothing new to compare. Those steps are skipped rather than faked.
    assert job.phases[0] == "scanning"
    assert job.phases[-1] == "reading"


def test_a_cloud_only_file_is_downloaded_then_read(
    settings, conn, mail_root, cloud_only
):
    """The whole point, end to end."""
    cloud_only("cloudy.eml")

    job = RecordingJob()
    result = run_read_all(settings, conn, job=job, roots=[mail_root])

    assert "downloading" in job.phases
    assert result.files_copied == 1

    source_id = _row_id(conn, "cloudy.eml")
    row = conn.execute(
        "SELECT local_copy_path, content_hash, parse_state FROM source_files "
        "WHERE id = ?",
        (source_id,),
    ).fetchone()
    assert row["local_copy_path"], "a copy should have been kept"
    assert Path(row["local_copy_path"]).exists()
    assert Path(row["local_copy_path"]).is_relative_to(settings.cloud_path)
    assert row["content_hash"], "the copy should have been hashed on the way past"
    assert row["parse_state"] == "done"


def test_duplicates_are_marked_after_the_download_not_before(
    settings, conn, mail_root, cloud_only
):
    """The one real ordering hazard.

    The scan marks duplicates before any cloud file has a hash, so without a
    second pass afterwards two byte-identical mailboxes are reported as two
    different mailboxes.
    """
    same = b"From: a@example.com\r\nSubject: same\r\nMessage-ID: <9@x>\r\n\r\nBody.\r\n"
    (mail_root / "one.eml").write_bytes(same)
    (mail_root / "two.eml").write_bytes(same)

    cloud_only("two.eml")
    run_read_all(settings, conn, job=RecordingJob(), roots=[mail_root])

    pair = conn.execute(
        "SELECT path, duplicate_of FROM source_files WHERE path LIKE '%one.eml' "
        "OR path LIKE '%two.eml' ORDER BY path"
    ).fetchall()
    assert any(r["duplicate_of"] is not None for r in pair), (
        "the downloaded copy was never compared against the file it duplicates"
    )


def test_nothing_is_left_in_a_state_that_means_nothing(settings, conn, mail_root):
    """Every file ends read or ends failed.

    A row still sitting at 'pending' or 'parsing' contributes to no qualifier
    anywhere, so the archive would report a clean total while being short.
    """
    _ = run_read_all(settings, conn, job=RecordingJob(), roots=[mail_root])

    stuck = conn.execute(
        "SELECT COUNT(*) FROM source_files "
        "WHERE parse_state IN ('pending', 'parsing', 'selected')"
    ).fetchone()[0]
    assert stuck == 0


def test_one_download_that_fails_does_not_end_the_pass(
    settings, conn, mail_root, cloud_only, monkeypatch
):
    cloud_only("cloudy.eml")

    from recall import readall as readall_module

    real = readall_module.hydrate_to

    def one_bad_one(src, dest, **kwargs):
        if src.name == "cloudy.eml":
            from recall.scan.onedrive import CopyResult

            return CopyResult(src=src, dest=dest, error="the connection dropped")
        return real(src, dest, **kwargs)

    monkeypatch.setattr(readall_module, "hydrate_to", one_bad_one)

    result = run_read_all(settings, conn, job=RecordingJob(), roots=[mail_root])

    assert result.downloads_failed == 1
    assert result.stopped_for is None, "the pass should have carried on"
    assert result.files_read >= 1, "the other file should still have been read"


def test_stopping_during_the_search_reads_nothing(settings, conn, mail_root):
    job = RecordingJob(cancel_after_phase="scanning")
    result = run_read_all(settings, conn, job=job, roots=[mail_root])

    assert job.phases == ["scanning"]
    assert result.files_read == 0


def test_skipping_the_download_still_reads_what_is_here(
    settings, conn, mail_root, cloud_only
):
    """The way out of "not enough room" - a choice, not a dead end."""
    cloud_only("cloudy.eml")

    job = RecordingJob()
    result = run_read_all(
        settings, conn, job=job, roots=[mail_root], skip_download=True
    )

    assert "downloading" not in job.phases
    assert result.skipped_download is True
    assert conn.execute(
        "SELECT COUNT(*) FROM source_files WHERE local_copy_path IS NOT NULL"
    ).fetchone()[0] == 0


def test_the_download_reports_bytes_not_files(
    settings, conn, mail_root, cloud_only
):
    """A pass that is one twenty-gigabyte file must not sit at "0 of 1"."""
    cloud_only("cloudy.eml")

    job = RecordingJob()
    run_read_all(settings, conn, job=job, roots=[mail_root])

    assert job.units[job.phases.index("downloading")] == "bytes"


# --- copies that are no longer there ---------------------------------------


def test_a_copy_deleted_by_hand_is_forgotten(
    settings, conn, mail_root, cloud_only
):
    cloud_only("cloudy.eml")
    run_read_all(settings, conn, job=RecordingJob(), roots=[mail_root])

    source_id = _row_id(conn, "cloudy.eml")
    copy = conn.execute(
        "SELECT local_copy_path FROM source_files WHERE id = ?", (source_id,)
    ).fetchone()["local_copy_path"]
    Path(copy).unlink()

    assert forget_missing_copies(conn) == 1
    assert conn.execute(
        "SELECT local_copy_path FROM source_files WHERE id = ?", (source_id,)
    ).fetchone()["local_copy_path"] is None
    # And it is offered for download again rather than being stuck as held.
    assert any(r["id"] == source_id for r in cloud_only_rows(conn))


# --- reading from the copy, but talking about the original -----------------


def test_the_copy_is_read_but_the_original_is_what_is_named(
    settings, conn, mail_root, cloud_only
):
    """Two paths, two jobs.

    The bytes come from Recall's own folder. Everything the user is shown names
    the file where they keep it - nobody recognises
    workdir/cloud/000003/cloudy.eml as their own mail.
    """
    cloud_only("cloudy.eml")
    run_read_all(settings, conn, job=RecordingJob(), roots=[mail_root])

    row = conn.execute(
        "SELECT path, local_copy_path FROM source_files WHERE path LIKE '%cloudy.eml'"
    ).fetchone()

    assert row["path"] == str(mail_root / "cloudy.eml")
    assert str(settings.cloud_path) in row["local_copy_path"]

    # And the records point back at the file the user knows, through the
    # source row, not at the copy.
    linked = conn.execute(
        "SELECT COUNT(*) FROM item_sources JOIN source_files sf "
        "  ON sf.id = item_sources.source_file_id "
        "WHERE sf.path = ?",
        (str(mail_root / "cloudy.eml"),),
    ).fetchone()[0]
    assert linked >= 1


def test_a_rescan_does_not_lose_the_copy(settings, conn, mail_root, cloud_only):
    """OneDrive re-evicts originals on its own schedule.

    The file is still a placeholder on every later scan, and must stay readable
    anyway, because Recall is holding its own copy.
    """
    cloud_only("cloudy.eml")
    run_read_all(settings, conn, job=RecordingJob(), roots=[mail_root])
    first = conn.execute(
        "SELECT local_copy_path FROM source_files WHERE path LIKE '%cloudy.eml'"
    ).fetchone()["local_copy_path"]

    # Scan again with the file still reported as cloud-only.
    run_read_all(settings, conn, job=RecordingJob(), roots=[mail_root])

    row = conn.execute(
        "SELECT local_copy_path, is_placeholder FROM source_files "
        "WHERE path LIKE '%cloudy.eml'"
    ).fetchone()
    assert row["local_copy_path"] == first, "the copy was forgotten on rescan"
    assert row["is_placeholder"] == 1, (
        "the column should still say the truth about the OneDrive original"
    )

    # And it is still offered to the reader despite that.
    from recall.extract import Extractor

    paths = [
        r["path"] for r in Extractor(settings, conn)._sources_to_read(None, False)
    ]
    assert str(mail_root / "cloudy.eml") in paths
