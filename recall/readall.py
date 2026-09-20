"""One pass: find every Outlook file on this computer, and read the lot.

Recall has always been able to do this in three steps - search, download the
cloud-only files, read them - and has always made the user drive each one
separately, ticking rows in between. On a real machine that means a file stored
in the cloud sits at "not downloaded, so not compared" indefinitely, because
nobody noticed it and clicked through two dialogs. This module is the one step
that does all three.

What it does not change is the promise around downloading. The cost is worked
out and shown first, in full, and nothing is fetched until the user has agreed
to that number. The difference is that they agree once, at the start, instead of
three times in the middle.

Four steps, in this order, and the order matters:

1. **Search.** The scan as it always was, including its own hashing and
   duplicate detection.
2. **Download.** Every cloud-only file is copied into ``workdir/cloud`` and
   hashed on the way past. The copy is kept: OneDrive puts originals back in the
   cloud on its own schedule, and a file Recall owns stays readable.
3. **Compare again.** Step 1 marked duplicates before those copies had hashes,
   so without a second pass two byte-identical mailboxes are reported as two
   different mailboxes.
4. **Read.** Extraction over everything reachable - which now includes the
   copies, and includes Outlook data files that Outlook itself is holding open.

Every file this touches ends up recorded as read or recorded as failed. None is
left in between. A file nothing has an opinion about contributes to no warning
on any screen, so an archive missing it would look complete.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .db import CLOUD_ONLY_SQL, log_error
from .extract import Extractor, parse_kinds
from .logging_setup import get_logger
from .parsers.pst_backend import LOCKED_BY_OUTLOOK_SQL
from .scan.fingerprint import hash_file
from .scan.onedrive import (
    HydrationPlan,
    HydrationRefused,
    copy_destination,
    copy_utc_now,
    hydrate_to,
)
from .scan.walker import Scanner, mark_duplicates_from_db

log = get_logger("readall")

#: The steps the user is shown, in order.
PHASES = ("scanning", "downloading", "comparing", "reading")

#: The same breathing room the up-front plan promises, so a pass that the
#: dialog said would fit does not stop half way through saying it does not.
HEADROOM_BYTES = HydrationPlan.HEADROOM_BYTES


@dataclass
class ReadAllResult:
    """What the pass actually did, for the sentence at the end."""

    files_found: int = 0
    files_copied: int = 0
    bytes_copied: int = 0
    downloads_failed: int = 0
    files_read: int = 0
    items_written: int = 0
    duplicates_collapsed: int = 0
    attachments_written: int = 0
    locked_attempted: int = 0
    skipped_download: bool = False
    stopped_for: str | None = None
    failures: list[tuple[str, str]] = field(default_factory=list)

    def as_detail(self) -> dict:
        return {
            "files_found": self.files_found,
            "files_copied": self.files_copied,
            "bytes_copied": self.bytes_copied,
            "downloads_failed": self.downloads_failed,
            "files_read": self.files_read,
            "items_written": self.items_written,
            "duplicates_collapsed": self.duplicates_collapsed,
            "attachments_written": self.attachments_written,
            "locked_attempted": self.locked_attempted,
            "skipped_download": self.skipped_download,
            "stopped_for": self.stopped_for,
            "failures": self.failures[:50],
        }


def cloud_only_rows(conn) -> list:
    """Files still in the cloud, with no copy here, smallest first.

    Smallest first so that a pass which runs out of room or is stopped early has
    still brought down as many whole files as it could, rather than spending an
    hour on one and finishing none.
    """
    return conn.execute(
        "SELECT id, path, size_bytes FROM source_files "
        f"WHERE {CLOUD_ONLY_SQL} ORDER BY size_bytes ASC, id ASC"
    ).fetchall()


def forget_missing_copies(conn) -> int:
    """Drop copy records whose file the user has since deleted.

    Runs before the download step so the plan and the run agree. Without it, a
    file whose copy was cleared out by hand stays marked as held and is never
    downloaded again, and never read.
    """
    gone = []
    for row in conn.execute(
        "SELECT id, path, local_copy_path FROM source_files "
        "WHERE local_copy_path IS NOT NULL"
    ):
        if not Path(row["local_copy_path"]).exists():
            gone.append((int(row["id"]), row["path"], row["local_copy_path"]))

    for source_id, path, copy in gone:
        conn.execute(
            "UPDATE source_files SET local_copy_path = NULL, "
            "local_copy_bytes = NULL, local_copy_utc = NULL WHERE id = ?",
            (source_id,),
        )
        log_error(
            conn,
            "hydrate",
            f"Recall's copy of {path} is no longer at {copy}, so it will be "
            "downloaded again if you ask for it.",
            source_file_id=source_id,
        )
    return len(gone)


def _free_on(path: Path) -> int:
    import shutil

    try:
        return shutil.disk_usage(path.anchor or str(path)).free
    except OSError:
        return 0


def run_read_all(
    settings: Settings,
    conn,
    *,
    job,
    roots: list[Path] | None = None,
    kinds: str | None = None,
    full_hash: bool = False,
    skip_download: bool = False,
    sample: int = 0,
) -> ReadAllResult:
    """Search, download, compare and read, reporting into ``job`` as it goes.

    ``job`` is anything with the JobManager reporting methods, so a test can
    pass a recorder instead of the real one.
    """
    result = ReadAllResult()
    cancelled = job.cancel_event.is_set
    # Which steps actually happened. A step that was not needed must not be
    # reported as finished - saying work was done when it was not is the same
    # failure as hiding work that was skipped.
    entered: list[str] = []

    def enter(name: str, **kwargs) -> None:
        entered.append(name)
        job.begin_phase(name, count=len(PHASES), **kwargs)
        job.set_detail(phases_entered=list(entered))

    # -- 1. search ----------------------------------------------------------
    enter(
        "scanning",
        index=1,
        message="Looking for Outlook files on this computer...",
    )
    scanner = Scanner(settings, conn)
    scanner.cancel = job.cancel_event
    mirror = _mirror_scan(job, scanner)
    try:
        scanner.run(roots, full_hash=full_hash)
    finally:
        mirror.set()

    result.files_found = scanner.progress.candidates_found
    if cancelled():
        return result
    if scanner.progress.state == "failed":
        result.stopped_for = "scan"
        result.failures.append(("the search", scanner.progress.message))
        return result

    # -- 2. download --------------------------------------------------------
    forget_missing_copies(conn)
    rows = [] if skip_download else cloud_only_rows(conn)
    result.skipped_download = skip_download and bool(cloud_only_rows(conn))

    if rows:
        settings.ensure_workdir()
        total_bytes = sum(int(r["size_bytes"] or 0) for r in rows)
        enter(
            "downloading",
            index=2,
            total=total_bytes,
            unit="bytes",
            message=f"Downloading {len(rows)} file(s) from OneDrive...",
        )
        _download_all(settings, conn, job, rows, result)
        if cancelled():
            return result

    # -- 3. compare ---------------------------------------------------------
    if result.files_copied:
        enter(
            "comparing",
            index=3,
            message="Looking for identical copies...",
        )
        mark_duplicates_from_db(conn)
        if cancelled():
            return result

    # -- 4. read ------------------------------------------------------------
    result.locked_attempted = conn.execute(
        f"SELECT COUNT(*) FROM source_files WHERE {LOCKED_BY_OUTLOOK_SQL}"
    ).fetchone()[0]

    enter(
        "reading",
        index=4,
        message="Reading records into the archive...",
    )
    extractor = Extractor(settings, conn, cancel=job.cancel_event)

    def report(progress) -> None:
        job.progress(
            done=progress.files_done,
            total=progress.files_total,
            current=progress.current_file,
            message=progress.message or "Reading records into the archive...",
        )

    extractor.on_progress = report
    progress = extractor.run(
        kinds=parse_kinds(kinds) if kinds else None,
        source_ids=None,
        sample=sample,
        resume=True,
    )

    result.files_read = progress.files_done
    result.items_written = progress.items_written
    result.duplicates_collapsed = progress.duplicates_collapsed
    result.attachments_written = progress.attachments_written
    result.failures.extend(progress.failures)
    return result


def _download_all(settings: Settings, conn, job, rows, result: ReadAllResult) -> None:
    """Copy every cloud-only file into the workdir, one at a time."""
    cloud_root = settings.cloud_path
    done_bytes = 0

    for row in rows:
        if job.cancel_event.is_set():
            return

        source_id = int(row["id"])
        src = Path(row["path"])
        size = int(row["size_bytes"] or 0)
        dest = copy_destination(cloud_root, source_id, src)

        # Reading a placeholder fills the OneDrive original in as well as
        # writing the copy, so the size is paid twice: once where the original
        # lives and once where the copy goes. On one drive - the ordinary case -
        # that is twice the size on that drive.
        #
        # Checked before every file rather than once at the start, because the
        # drive can fill up for reasons of its own while a three-hour pass runs,
        # and stopping cleanly here beats a half-written file at number 300.
        same_drive = src.anchor.lower() == dest.anchor.lower()
        if (
            _free_on(dest) < size * (2 if same_drive else 1) + HEADROOM_BYTES
            or (not same_drive and _free_on(src) < size + HEADROOM_BYTES)
        ):
            result.stopped_for = "disk"
            result.failures.append(
                (
                    str(src),
                    "There was not enough room left on the drive to download "
                    "this one. Everything downloaded before it was kept.",
                )
            )
            log.warning("Stopping downloads at %s: not enough disk", src)
            return

        job.progress(
            done=done_bytes,
            current=str(src),
            message=f"Downloading {src.name} ({size / (1024**2):,.0f} MB)...",
        )

        def tick(copied: int, _base=done_bytes) -> None:
            job.progress(done=_base + copied)

        try:
            copied = hydrate_to(
                src,
                dest,
                expected_size=size,
                progress=tick,
                cancel=job.cancel_event.is_set,
            )
        except HydrationRefused as exc:
            # The workdir is inside OneDrive. Every copy would be uploaded
            # straight back, so no amount of retrying makes this better.
            result.stopped_for = "workdir"
            result.failures.append((str(src), str(exc)))
            log.error("Refusing to download into OneDrive: %s", exc)
            return

        if copied.canceled:
            return

        if copied.error:
            result.downloads_failed += 1
            result.failures.append((str(src), copied.error))
            conn.execute(
                "UPDATE source_files SET lock_error = ? WHERE id = ?",
                (copied.error, source_id),
            )
            log_error(
                conn,
                "hydrate",
                f"Could not download {src}.",
                detail=copied.error,
                source_file_id=source_id,
            )
            done_bytes += size
            continue

        content_hash = copied.content_hash
        if copied.reused_existing:
            # The copy was already here from an earlier run. It still needs a
            # hash if that run was stopped before writing one.
            done_bytes += size
            content_hash = hash_file(dest, size_cap_bytes=0).content_hash
        else:
            done_bytes += copied.bytes_copied
            result.bytes_copied += copied.bytes_copied

        result.files_copied += 1
        conn.execute(
            "UPDATE source_files SET local_copy_path = ?, local_copy_bytes = ?, "
            "local_copy_utc = ?, content_hash = COALESCE(?, content_hash), "
            "lock_error = NULL WHERE id = ?",
            (
                str(dest),
                dest.stat().st_size if dest.exists() else None,
                copy_utc_now(),
                content_hash,
                source_id,
            ),
        )
        job.progress(done=done_bytes)


def _mirror_scan(job, scanner):
    """Pump the scanner's own counters into the job every half second.

    The scan does not know how many files there are until it has finished
    finding them, so `total` stays None and the bar stays indeterminate - which
    is the honest thing for it to show.
    """
    import threading

    stop = threading.Event()

    def pump() -> None:
        while not stop.wait(0.5):
            p = scanner.progress
            job.progress(
                done=p.candidates_found,
                current=p.current_path,
                message=p.message or "Looking for Outlook files...",
            )

    threading.Thread(target=pump, daemon=True, name="readall-scan-mirror").start()
    return stop
