"""Phase 0: find every Outlook-related file on this computer.

No parsing happens here. The job is to know what exists, quickly, and to be
honest about what could not be looked at. A scan of several drives should
finish in minutes.

Files are matched by extension *and* by their internal signature, so a PST
renamed ``backup.dat`` is still found, and a ``.pst`` that is really a Word
document is caught and flagged rather than handed to a parser that will choke
on it.

Nothing in this module opens a file for writing, renames anything, or deletes
anything. The only writes are to Recall's own database.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from ..config import Settings
from ..db import log_error, transaction
from ..logging_setup import get_logger
from ..models import Container
from .fingerprint import file_times, find_duplicate_groups, hash_file, mark_duplicates
from .onedrive import is_onedrive_path, is_placeholder, placeholder_reason

log = get_logger("scan.walker")


# ---------------------------------------------------------------------------
# Magic-byte signatures
# ---------------------------------------------------------------------------
#
# (offset, bytes, the extensions this signature belongs to, a human name)
_SIGNATURES: list[tuple[int, bytes, frozenset[str], str]] = [
    # Outlook personal folders, both ANSI and Unicode. "!BDN"
    (0, b"!BDN", frozenset({".pst", ".ost"}), "Outlook data file (PST/OST)"),
    # OLE2 compound document: .msg, and also .doc/.xls, hence the extension check.
    (0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", frozenset({".msg"}), "Outlook message (MSG)"),
    # Outlook Express 5/6 mail and folder databases.
    (0, b"\xcf\xad\x12\xfe", frozenset({".dbx"}), "Outlook Express database (DBX)"),
    # Outlook Express 4 / Eudora mailbox. "JMF9"
    (0, b"JMF9", frozenset({".mbx"}), "Outlook Express 4 mailbox (MBX)"),
    # Mac Outlook archive: a zip. "PK\x03\x04"
    (0, b"PK\x03\x04", frozenset({".olm"}), "Mac Outlook archive (OLM)"),
    # Windows Address Book.
    (0, b"\x9c\xcb\xcb\x8d\x13\x75\xd2\x11", frozenset({".wab"}), "Windows Address Book (WAB)"),
    # Text formats, checked case-insensitively further down.
    (0, b"BEGIN:VCALENDAR", frozenset({".ics", ".vcs"}), "calendar file"),
    (0, b"BEGIN:VCARD", frozenset({".vcf"}), "contact card (vCard)"),
    (0, b"From ", frozenset({".mbox"}), "mailbox (mbox)"),
]

#: Extensions whose content is plain text, where a missing signature is normal
#: (an .eml may start with any header) and so is not evidence of anything.
_TEXTUAL = frozenset({".eml", ".mbox", ".ics", ".vcs", ".vcf"})

#: How many bytes to read to identify a file.
_PEEK = 32

LOCKED_ADVICE = "Close Outlook and scan again to include this file."


@dataclass(slots=True)
class Candidate:
    """One file the scanner decided is worth recording."""

    path: str
    ext: str
    size_bytes: int
    mtime_utc: str
    ctime_utc: str
    container: str
    is_placeholder: bool
    placeholder_reason: str | None = None
    is_readable: bool = True
    lock_error: str | None = None
    #: What the first bytes say this file is, when they say anything.
    detected_type: str | None = None
    #: True when the extension and the signature disagree. Becomes a
    #: `magic_mismatch` finding; the file is kept, never discarded.
    magic_mismatch: bool = False
    magic_detail: str | None = None


@dataclass(slots=True)
class ScanProgress:
    """Live counters, read by the Sources screen while a scan runs."""

    dirs_seen: int = 0
    files_seen: int = 0
    candidates_found: int = 0
    bytes_hashed: int = 0
    unreadable_dirs: int = 0
    current_path: str = ""
    started_utc: str = ""
    finished_utc: str | None = None
    state: str = "running"
    scan_run_id: int | None = None
    message: str = ""
    roots: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "dirs_seen": self.dirs_seen,
            "files_seen": self.files_seen,
            "candidates_found": self.candidates_found,
            "bytes_hashed": self.bytes_hashed,
            "unreadable_dirs": self.unreadable_dirs,
            "current_path": self.current_path,
            "started_utc": self.started_utc,
            "finished_utc": self.finished_utc,
            "state": self.state,
            "scan_run_id": self.scan_run_id,
            "message": self.message,
            "roots": list(self.roots),
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _excluded(dir_path: Path, exclude_dirs: list[str]) -> bool:
    """Is this directory on the never-search list?

    An entry containing a separator is matched against the tail of the path
    ("AppData\\Local\\Temp"); a bare name matches any directory so named.
    """
    lowered = str(dir_path).lower().replace("/", "\\")
    name = dir_path.name.lower()
    for pattern in exclude_dirs:
        pat = pattern.lower().replace("/", "\\").strip("\\")
        if not pat:
            continue
        if "\\" in pat:
            if lowered.endswith("\\" + pat) or lowered == pat:
                return True
        elif name == pat:
            return True
    return False


def sniff(path: Path, ext: str) -> tuple[str | None, bool, str | None]:
    """Read the first bytes and say what the file really is.

    Returns (detected type, extension-and-content-disagree, explanation).
    Never raises: an unreadable header is a fact to record, not a crash.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(_PEEK)
    except OSError:
        # The caller already records why the file could not be opened.
        return None, False, None

    if not head:
        return None, False, None

    upper = head.upper()
    for offset, magic, owners, label in _SIGNATURES:
        window = head[offset : offset + len(magic)]
        matched = window == magic
        if not matched and magic.isascii() and magic.isupper():
            matched = upper[offset : offset + len(magic)] == magic
        if matched:
            if ext in owners:
                return label, False, None
            return (
                label,
                True,
                f"The name ends in {ext}, but the contents are a {label}.",
            )

    # No signature matched. For text formats that is ordinary; for the binary
    # formats it means the file is not what its name claims.
    if ext in _TEXTUAL:
        return None, False, None
    if ext in {".pst", ".ost", ".msg", ".dbx", ".mbx", ".olm", ".wab"}:
        preview = head[:8].hex(" ")
        return (
            None,
            True,
            f"The name ends in {ext}, but the file does not start the way a "
            f"{ext} file does (first bytes: {preview}).",
        )
    return None, False, None


def classify_container(path: Path, onedrive_roots: list[Path]) -> str:
    """local, onedrive, external or network - where this file physically is."""
    p = str(path)
    if p.startswith("\\\\") or p.startswith("//"):
        return Container.NETWORK
    if is_onedrive_path(path, onedrive_roots):
        return Container.ONEDRIVE
    try:
        import ctypes

        drive = os.path.splitdrive(p)[0]
        if drive:
            kind = ctypes.windll.kernel32.GetDriveTypeW(drive + "\\")
            if kind == 4:  # DRIVE_REMOTE
                return Container.NETWORK
            if kind == 2:  # DRIVE_REMOVABLE
                return Container.EXTERNAL
            if kind == 5:  # DRIVE_CDROM
                return Container.EXTERNAL
    except (ImportError, AttributeError, OSError):
        pass
    return Container.LOCAL


def walk_roots(
    roots: list[Path],
    settings: Settings,
    *,
    progress: ScanProgress | None = None,
    cancel: threading.Event | None = None,
    on_unreadable_dir: Callable[[Path, OSError], None] | None = None,
) -> Iterator[Candidate]:
    """Walk the roots and yield every candidate file.

    Directories that cannot be read are counted and reported; they never stop
    the walk. Junctions are not followed unless the user turned that on, because
    a junction loop turns a ten-minute scan into an endless one.
    """
    extensions = set(settings.scan.extensions)
    exclude = settings.scan.exclude_dirs
    follow = settings.scan.follow_symlinks
    from ..config import onedrive_roots as discover_onedrive

    od_roots = discover_onedrive()

    visited: set[tuple[int, int]] = set()

    for root in roots:
        if cancel is not None and cancel.is_set():
            return
        if not root.exists():
            log.warning("Folder to search does not exist, skipping: %s", root)
            continue
        log.info("Searching %s", root)
        yield from _walk_one(
            root,
            extensions,
            exclude,
            follow,
            od_roots,
            visited,
            progress,
            cancel,
            on_unreadable_dir,
        )


def _walk_one(
    root: Path,
    extensions: set[str],
    exclude: list[str],
    follow: bool,
    od_roots: list[Path],
    visited: set[tuple[int, int]],
    progress: ScanProgress | None,
    cancel: threading.Event | None,
    on_unreadable_dir: Callable[[Path, OSError], None] | None,
) -> Iterator[Candidate]:
    stack: list[Path] = [root]

    while stack:
        if cancel is not None and cancel.is_set():
            return
        current = stack.pop()

        if _excluded(current, exclude):
            continue

        # Guard against junction loops by remembering (device, inode).
        try:
            st = current.stat()
            key = (st.st_dev, st.st_ino)
            if key in visited:
                continue
            visited.add(key)
        except OSError:
            pass

        if progress is not None:
            progress.dirs_seen += 1
            progress.current_path = str(current)

        try:
            entries = list(os.scandir(current))
        except PermissionError as exc:
            if progress is not None:
                progress.unreadable_dirs += 1
            if on_unreadable_dir is not None:
                on_unreadable_dir(current, exc)
            continue
        except OSError as exc:
            if progress is not None:
                progress.unreadable_dirs += 1
            if on_unreadable_dir is not None:
                on_unreadable_dir(current, exc)
            continue

        for entry in entries:
            if cancel is not None and cancel.is_set():
                return
            try:
                if entry.is_dir(follow_symlinks=follow):
                    stack.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=follow):
                    continue
            except OSError:
                continue

            if progress is not None:
                progress.files_seen += 1

            ext = os.path.splitext(entry.name)[1].lower()
            if ext not in extensions:
                continue

            candidate = _inspect(Path(entry.path), ext, od_roots)
            if candidate is None:
                continue
            if progress is not None:
                progress.candidates_found += 1
            yield candidate


def _inspect(path: Path, ext: str, od_roots: list[Path]) -> Candidate | None:
    """Gather everything knowable about one file without parsing it."""
    try:
        st = path.stat()
    except PermissionError as exc:
        return Candidate(
            path=str(path),
            ext=ext,
            size_bytes=0,
            mtime_utc=_utc_now(),
            ctime_utc=_utc_now(),
            container=classify_container(path, od_roots),
            is_placeholder=False,
            is_readable=False,
            lock_error=f"PermissionError: {exc}",
        )
    except OSError as exc:
        log.debug("Could not look at %s: %s", path, exc)
        return None

    placeholder = is_placeholder(st)
    mtime, ctime = file_times(st)
    container = classify_container(path, od_roots)

    detected: str | None = None
    mismatch = False
    detail: str | None = None
    readable = True
    lock_error: str | None = None

    if placeholder:
        # Reading the header would download the file. The type stays unknown
        # until the user asks for it, and the screen says so.
        detail = None
    else:
        try:
            with open(path, "rb"):
                pass
        except PermissionError as exc:
            readable = False
            lock_error = f"PermissionError: {exc}"
        except OSError as exc:
            readable = False
            lock_error = f"{exc.__class__.__name__}: {exc}"

        if readable:
            detected, mismatch, detail = sniff(path, ext)

    return Candidate(
        path=str(path),
        ext=ext,
        size_bytes=st.st_size,
        mtime_utc=mtime,
        ctime_utc=ctime,
        container=container,
        is_placeholder=placeholder,
        placeholder_reason=placeholder_reason(st) if placeholder else None,
        is_readable=readable,
        lock_error=lock_error,
        detected_type=detected,
        magic_mismatch=mismatch,
        magic_detail=detail,
    )


# ---------------------------------------------------------------------------
# The scan, start to finish
# ---------------------------------------------------------------------------


class Scanner:
    """Runs one scan and writes what it finds.

    Re-running is safe and produces no duplicates: ``source_files.path`` is
    unique and each row is updated in place, keeping any parse state and user
    note already attached to it.
    """

    def __init__(self, settings: Settings, conn) -> None:
        self.settings = settings
        self.conn = conn
        self.progress = ScanProgress()
        self.cancel = threading.Event()

    def run(
        self,
        roots: list[Path] | None = None,
        *,
        full_hash: bool = False,
    ) -> ScanProgress:
        """Walk, record, hash, mark duplicates, then run the integrity checks."""
        roots = roots or self.settings.effective_scan_roots()
        self.progress.roots = [str(r) for r in roots]
        self.progress.started_utc = _utc_now()
        self.progress.state = "running"

        import json

        cur = self.conn.execute(
            "INSERT INTO scan_runs(started_utc, roots_json, state) VALUES (?, ?, 'running')",
            (self.progress.started_utc, json.dumps([str(r) for r in roots])),
        )
        run_id = int(cur.lastrowid)
        self.progress.scan_run_id = run_id

        size_cap = 0 if full_hash else self.settings.scan.hash_size_cap_bytes
        candidates: list[Candidate] = []

        try:
            batch: list[Candidate] = []
            for candidate in walk_roots(
                roots,
                self.settings,
                progress=self.progress,
                cancel=self.cancel,
                on_unreadable_dir=self._record_unreadable_dir,
            ):
                batch.append(candidate)
                candidates.append(candidate)
                if len(batch) >= 200:
                    self._write_batch(batch, run_id)
                    batch = []
            if batch:
                self._write_batch(batch, run_id)

            if self.cancel.is_set():
                self.progress.state = "canceled"
                self.progress.message = (
                    "Search stopped. Everything found so far was kept - "
                    "run it again to carry on."
                )
            else:
                self.progress.message = "Fingerprinting files..."
                self._hash_pending(size_cap)
                self.progress.message = "Looking for identical copies..."
                self._mark_duplicates()
                self.progress.state = "done"
                self.progress.message = self.summary_line()

        except Exception as exc:  # noqa: BLE001 - a scan never crashes the program
            self.progress.state = "failed"
            self.progress.message = f"The search stopped with a problem: {exc}"
            log_error(self.conn, "scan", str(exc), detail=repr(exc))
            log.exception("Scan failed")

        self.progress.finished_utc = _utc_now()
        self.conn.execute(
            "UPDATE scan_runs SET finished_utc = ?, files_seen = ?, "
            "candidates_found = ?, state = ? WHERE id = ?",
            (
                self.progress.finished_utc,
                self.progress.files_seen,
                self.progress.candidates_found,
                self.progress.state,
                run_id,
            ),
        )

        # Integrity checks run at the end of every scan (spec section 9).
        if self.progress.state in ("done", "canceled"):
            try:
                from ..integrity.engine import run_scan_checks

                run_scan_checks(self.conn, self.settings, candidates)
            except Exception as exc:  # noqa: BLE001
                log_error(self.conn, "integrity", str(exc), detail=repr(exc))
                log.exception("Integrity checks after scan failed")

        return self.progress

    # -- writing ----------------------------------------------------------

    def _write_batch(self, batch: list[Candidate], run_id: int) -> None:
        with transaction(self.conn):
            for c in batch:
                self.conn.execute(
                    """
                    INSERT INTO source_files
                        (path, container, ext, size_bytes, mtime_utc, ctime_utc,
                         is_placeholder, is_readable, lock_error, scan_run_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        container = excluded.container,
                        ext = excluded.ext,
                        size_bytes = excluded.size_bytes,
                        mtime_utc = excluded.mtime_utc,
                        ctime_utc = excluded.ctime_utc,
                        is_placeholder = excluded.is_placeholder,
                        is_readable = excluded.is_readable,
                        lock_error = excluded.lock_error,
                        scan_run_id = excluded.scan_run_id
                    """,
                    (
                        c.path,
                        c.container,
                        c.ext,
                        c.size_bytes,
                        c.mtime_utc,
                        c.ctime_utc,
                        int(c.is_placeholder),
                        int(c.is_readable),
                        c.lock_error,
                        run_id,
                    ),
                )
                if c.lock_error:
                    log_error(
                        self.conn,
                        "scan",
                        f"Could not open {c.path}. {LOCKED_ADVICE}",
                        detail=c.lock_error,
                    )

    def _record_unreadable_dir(self, path: Path, exc: OSError) -> None:
        log.debug("Folder could not be read: %s (%s)", path, exc)
        log_error(
            self.conn,
            "scan",
            f"Folder could not be read and was skipped: {path}",
            detail=f"{exc.__class__.__name__}: {exc}",
        )

    def _hash_pending(self, size_cap: int) -> None:
        """Hash every real local file we have not hashed yet."""
        rows = self.conn.execute(
            "SELECT id, path, size_bytes, is_placeholder, is_readable "
            "FROM source_files "
            "WHERE content_hash IS NULL AND is_placeholder = 0 AND is_readable = 1"
        ).fetchall()

        for row in rows:
            if self.cancel.is_set():
                return
            self.progress.current_path = row["path"]
            result = hash_file(
                row["path"],
                size_cap_bytes=size_cap,
                size_bytes=row["size_bytes"],
                is_placeholder=bool(row["is_placeholder"]),
                cancel=self.cancel.is_set,
            )
            self.progress.bytes_hashed += result.bytes_read
            if result.ok:
                self.conn.execute(
                    "UPDATE source_files SET content_hash = ? WHERE id = ?",
                    (result.content_hash, row["id"]),
                )
            elif result.error:
                self.conn.execute(
                    "UPDATE source_files SET is_readable = 0, lock_error = ? WHERE id = ?",
                    (result.error, row["id"]),
                )
                log_error(
                    self.conn,
                    "scan",
                    f"Could not read {row['path']} to fingerprint it. {LOCKED_ADVICE}",
                    detail=result.error,
                    source_file_id=row["id"],
                )

    def _mark_duplicates(self) -> None:
        rows = [
            dict(r)
            for r in self.conn.execute(
                "SELECT id, path, content_hash, size_bytes FROM source_files "
                "WHERE content_hash IS NOT NULL"
            )
        ]
        groups = find_duplicate_groups(rows)
        with transaction(self.conn):
            mark_duplicates(self.conn, groups)
        log.info("Found %d groups of identical files", len(groups))

    # -- reporting --------------------------------------------------------

    def summary_line(self) -> str:
        """The one-page summary from spec section 5, stated honestly."""
        return summarize(self.conn)


def human_bytes(n: int) -> str:
    """A size in the unit that shows it honestly.

    Reporting 16 MB as "0.0 GB" is technically a rounding and practically a
    lie, so the unit is chosen to fit the number rather than fixed in advance.
    """
    if n <= 0:
        return "0 bytes"
    units = [("TB", 1024**4), ("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)]
    for label, size in units:
        if n >= size:
            value = n / size
            return f"{value:,.1f} {label}" if value < 10 else f"{value:,.0f} {label}"
    return f"{n:,} bytes"


def summarize(conn) -> str:
    """"Found 41 Outlook files totalling 84 GB. 12 are duplicates..."

    Every number here is countable from the database. Nothing is estimated.
    """
    row = conn.execute(
        """
        SELECT COUNT(*) AS n,
               COALESCE(SUM(size_bytes), 0) AS total,
               SUM(CASE WHEN duplicate_of IS NOT NULL THEN 1 ELSE 0 END) AS dupes,
               SUM(CASE WHEN is_placeholder = 1 THEN 1 ELSE 0 END) AS cloud,
               SUM(CASE WHEN is_readable = 0 THEN 1 ELSE 0 END) AS locked,
               SUM(CASE WHEN content_hash IS NULL AND is_placeholder = 0
                        AND is_readable = 1 THEN 1 ELSE 0 END) AS uncompared
        FROM source_files
        """
    ).fetchone()

    n = row["n"] or 0
    if n == 0:
        return (
            "No Outlook files were found in the folders searched. "
            "If you expected some, check that the right drives were ticked."
        )

    parts = [
        f"Found {n:,} Outlook file{'s' if n != 1 else ''} "
        f"totalling {human_bytes(row['total'] or 0)}."
    ]
    def many(n: int, singular: str, plural: str) -> str:
        return f"{n:,} {singular if n == 1 else plural}"

    if row["dupes"]:
        parts.append(
            many(row["dupes"], "is a duplicate of another file",
                 "are duplicates of another file") + "."
        )
    if row["cloud"]:
        parts.append(
            many(row["cloud"], "is stored in the cloud only", "are stored in the cloud only")
            + " and " + ("has" if row["cloud"] == 1 else "have")
            + " not been downloaded."
        )
    if row["locked"]:
        parts.append(
            f"{row['locked']:,} could not be opened - "
            + ("it is" if row["locked"] == 1 else "they are")
            + " probably in use by Outlook."
        )
    if row["uncompared"]:
        parts.append(
            many(row["uncompared"], "is too large to have been compared yet",
                 "are too large to have been compared yet")
            + ", so " + ("it is" if row["uncompared"] == 1 else "they are")
            + " not yet known to be unique."
        )
    return " ".join(parts)
