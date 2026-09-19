"""The Sources screen's API: what was found, and what to do with it."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..config import Settings, fixed_drives, onedrive_roots
from ..logging_setup import get_logger
from .jobs import JOBS, JobBusy

log = get_logger("api.sources")

router = APIRouter(tags=["sources"])


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _conn(request: Request):
    return request.app.state.db()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class DriveOption(BaseModel):
    path: str
    label: str
    kind: Literal["drive", "onedrive", "outlook", "folder"]
    total_bytes: int | None = None
    free_bytes: int | None = None
    exists: bool = True
    note: str | None = None


class ScanRequest(BaseModel):
    roots: list[str] = Field(default_factory=list)
    full_hash: bool = False


class SourceRow(BaseModel):
    id: int
    path: str
    name: str
    folder: str
    ext: str
    container: str
    size_bytes: int | None
    mtime_utc: str | None
    content_hash: str | None
    is_placeholder: bool
    is_readable: bool
    lock_error: str | None
    duplicate_of: int | None
    duplicate_of_path: str | None
    parse_state: str
    parse_backend: str | None
    parse_error: str | None
    item_count: int
    first_item_utc: str | None
    last_item_utc: str | None
    user_note: str | None
    finding_count: int
    worst_severity: str | None
    estimated_loss: int | None
    comparison_state: str


class HydrateRequest(BaseModel):
    ids: list[int]
    confirm: bool = False


class ExtractRequest(BaseModel):
    """Read files into the archive.

    ``ids`` empty means every file not read yet, which is what the big button
    on the Sources screen does.
    """

    ids: list[int] = Field(default_factory=list)
    kinds: str | None = None
    sample: int = 0
    resume: bool = True


class NoteRequest(BaseModel):
    note: str


# ---------------------------------------------------------------------------
# Where to look
# ---------------------------------------------------------------------------


@router.get("/folders")
def browse_folders(request: Request, path: str = "") -> dict[str, Any]:
    """What is inside this folder, for the chooser.

    With no path, the drives. Folders and files both, because somebody who
    knows their mail is in one .pst should be able to point at exactly that.
    A folder that cannot be opened comes back saying so rather than as an error.
    """
    from ..scan.browse import list_folder

    return list_folder(path, _settings(request)).as_dict()


@router.get("/scan/targets", response_model=list[DriveOption])
def scan_targets(request: Request) -> list[DriveOption]:
    """Every place Recall could search, for the drive chooser.

    Ticked by default is everything, which is the spec's default behaviour;
    the user narrows it by unticking. Folders the user chose before are offered
    too, but unticked: they were a deliberate choice last time and should be a
    deliberate choice again.
    """
    from ..scan.browse import recent_folders

    options: list[DriveOption] = []

    for folder in recent_folders(_conn(request)):
        chosen = Path(folder)
        options.append(
            DriveOption(
                path=folder,
                label=chosen.name or folder,
                kind="folder",
                exists=True,
                note=(
                    "One file you chose before."
                    if chosen.is_file() else "A folder you chose before."
                ),
            )
        )

    for drive in fixed_drives():
        total = free = None
        exists = drive.exists()
        if exists:
            try:
                usage = shutil.disk_usage(drive)
                total, free = usage.total, usage.free
            except OSError:
                pass
        options.append(
            DriveOption(
                path=str(drive),
                label=f"Drive {str(drive).rstrip(chr(92))}",
                kind="drive",
                total_bytes=total,
                free_bytes=free,
                exists=exists,
                note=(
                    "Searching a whole drive is thorough but slow - "
                    "allow 10 to 60 minutes."
                ),
            )
        )

    for od in onedrive_roots():
        options.append(
            DriveOption(
                path=str(od),
                label=f"OneDrive ({od.name})",
                kind="onedrive",
                exists=od.exists(),
                note=(
                    "Files stored in the cloud only are listed but never "
                    "downloaded without your say-so."
                ),
            )
        )

    import os

    local = os.environ.get("LOCALAPPDATA")
    if local:
        outlook = Path(local) / "Microsoft" / "Outlook"
        options.append(
            DriveOption(
                path=str(outlook),
                label="Outlook's own folder",
                kind="outlook",
                exists=outlook.is_dir(),
                note="Where Outlook keeps the mailbox it is using right now.",
            )
        )

    return options


# ---------------------------------------------------------------------------
# Running a scan
# ---------------------------------------------------------------------------


@router.post("/scan")
def start_scan(request: Request, body: ScanRequest) -> dict[str, Any]:
    """Start searching. Returns immediately; watch /api/job for progress."""
    settings = _settings(request)
    chosen = bool(body.roots)
    roots = [Path(r) for r in body.roots] if chosen else settings.effective_scan_roots()

    # A root is a folder to look through, or one file chosen directly - the
    # chooser offers both. What it cannot be is something that is no longer
    # there, which happens when a folder is picked and then deleted before the
    # button is pressed.
    missing = [str(r) for r in roots if not (r.is_dir() or r.is_file())]
    roots = [r for r in roots if r.is_dir() or r.is_file()]
    if not roots:
        raise HTTPException(
            status_code=400,
            detail=(
                "None of the chosen folders or files are on this computer: "
                + (", ".join(missing) or "(nothing was chosen)")
            ),
        )

    if chosen:
        from ..scan.browse import remember_folders

        remember_folders(_conn(request), [str(r) for r in roots])

    def work(job) -> None:
        from ..db import connect
        from ..scan.walker import Scanner

        conn = connect(settings.db_path)
        try:
            scanner = Scanner(settings, conn)
            scanner.cancel = job.cancel_event

            watcher_stop = _mirror_progress(job, scanner)
            try:
                result = scanner.run(roots, full_hash=body.full_hash)
            finally:
                watcher_stop.set()

            job.progress(
                done=result.candidates_found,
                total=result.candidates_found,
                current="",
            )
            job.finish(
                result.message,
                files_seen=result.files_seen,
                candidates_found=result.candidates_found,
                unreadable_dirs=result.unreadable_dirs,
                scan_run_id=result.scan_run_id,
                roots=result.roots,
                skipped_roots=missing,
                covered_roots=result.covered_roots,
            )
        finally:
            conn.close()

    try:
        status = JOBS.start("scan", work, message="Looking for Outlook files...")
    except JobBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {"started": True, "roots": [str(r) for r in roots], "job": status.as_dict()}


def _mirror_progress(job, scanner):
    """Copy the scanner's counters into the job status once a second."""
    import threading

    stop = threading.Event()

    def pump() -> None:
        while not stop.wait(0.5):
            p = scanner.progress
            job.progress(
                done=p.candidates_found,
                current=p.current_path,
                message=p.message
                or (
                    f"Looked at {p.files_seen:,} files in {p.dirs_seen:,} folders. "
                    f"Found {p.candidates_found:,} Outlook files so far."
                ),
            )

    threading.Thread(target=pump, daemon=True, name="scan-progress").start()
    return stop


@router.get("/job")
def job_status() -> dict[str, Any]:
    return JOBS.status.as_dict()


@router.post("/job/cancel")
def job_cancel() -> dict[str, Any]:
    stopped = JOBS.request_cancel()
    if not stopped:
        raise HTTPException(status_code=409, detail="Nothing is running.")
    return {"canceling": True}


# ---------------------------------------------------------------------------
# Listing what was found
# ---------------------------------------------------------------------------


@router.get("/sources")
def list_sources(
    request: Request,
    sort: str = "size",
    order: str = "desc",
    container: str | None = None,
    ext: str | None = None,
    state: str | None = None,
    only_duplicates: bool = False,
    only_placeholders: bool = False,
    only_problems: bool = False,
    search: str | None = None,
    limit: int = 500,
    offset: int = 0,
) -> dict[str, Any]:
    conn = _conn(request)

    # A page is a page - the same ceiling the people, problems and search
    # listings keep. Without it, ?limit=-1 goes straight into SQL, where a
    # negative LIMIT means "no limit" and the screen is handed the lot.
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))

    sort_columns = {
        "size": "sf.size_bytes",
        "name": "sf.path",
        "modified": "sf.mtime_utc",
        "type": "sf.ext",
        "items": "sf.item_count",
        "state": "sf.parse_state",
    }
    sort_sql = sort_columns.get(sort, "sf.size_bytes")
    order_sql = "DESC" if order.lower() == "desc" else "ASC"

    where: list[str] = []
    params: list[Any] = []
    if container:
        where.append("sf.container = ?")
        params.append(container)
    if ext:
        where.append("sf.ext = ?")
        params.append(ext if ext.startswith(".") else "." + ext)
    if state:
        where.append("sf.parse_state = ?")
        params.append(state)
    if only_duplicates:
        where.append("sf.duplicate_of IS NOT NULL")
    if only_placeholders:
        where.append("sf.is_placeholder = 1")
    if only_problems:
        where.append(
            "EXISTS (SELECT 1 FROM findings f WHERE f.source_file_id = sf.id "
            "AND f.state IN ('open','acknowledged'))"
        )
    if search:
        where.append("sf.path LIKE ?")
        params.append(f"%{search}%")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM source_files sf {where_sql}", params
    ).fetchone()["n"]

    rows = conn.execute(
        f"""
        SELECT sf.*,
               dup.path AS duplicate_of_path,
               (SELECT COUNT(*) FROM findings f
                 WHERE f.source_file_id = sf.id
                   AND f.state IN ('open','acknowledged')) AS finding_count,
               (SELECT f.severity FROM findings f
                 WHERE f.source_file_id = sf.id
                   AND f.state IN ('open','acknowledged')
                 ORDER BY CASE f.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                          WHEN 'medium' THEN 2 ELSE 3 END LIMIT 1) AS worst_severity,
               (SELECT SUM(f.estimated_loss) FROM findings f
                 WHERE f.source_file_id = sf.id
                   AND f.state IN ('open','acknowledged')) AS estimated_loss
        FROM source_files sf
        LEFT JOIN source_files dup ON dup.id = sf.duplicate_of
        {where_sql}
        ORDER BY {sort_sql} {order_sql}, sf.id
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "rows": [_row_to_source(r) for r in rows],
    }


def _row_to_source(r) -> dict[str, Any]:
    path = Path(r["path"])
    if r["is_placeholder"]:
        comparison = "not downloaded, so not compared"
    elif not r["is_readable"]:
        comparison = "could not be opened, so not compared"
    elif r["content_hash"]:
        comparison = "compared"
    else:
        comparison = "too large to compare yet"

    return {
        "id": r["id"],
        "path": r["path"],
        "name": path.name,
        "folder": str(path.parent),
        "ext": r["ext"],
        "container": r["container"],
        "size_bytes": r["size_bytes"],
        "mtime_utc": r["mtime_utc"],
        "content_hash": r["content_hash"],
        "is_placeholder": bool(r["is_placeholder"]),
        "is_readable": bool(r["is_readable"]) if r["is_readable"] is not None else True,
        "lock_error": r["lock_error"],
        "duplicate_of": r["duplicate_of"],
        "duplicate_of_path": r["duplicate_of_path"],
        "parse_state": r["parse_state"],
        "parse_backend": r["parse_backend"],
        "parse_error": r["parse_error"],
        "item_count": r["item_count"] or 0,
        "first_item_utc": r["first_item_utc"],
        "last_item_utc": r["last_item_utc"],
        "user_note": r["user_note"],
        "finding_count": r["finding_count"] or 0,
        "worst_severity": r["worst_severity"],
        "estimated_loss": r["estimated_loss"],
        "comparison_state": comparison,
    }


@router.get("/sources/summary")
def sources_summary(request: Request) -> dict[str, Any]:
    """The one-page summary, with every number countable from the database."""
    conn = _conn(request)
    from ..scan.walker import summarize

    row = conn.execute(
        """
        SELECT COUNT(*) AS n,
               COALESCE(SUM(size_bytes), 0) AS total_bytes,
               SUM(CASE WHEN duplicate_of IS NOT NULL THEN 1 ELSE 0 END) AS duplicates,
               SUM(CASE WHEN is_placeholder = 1 THEN 1 ELSE 0 END) AS placeholders,
               SUM(CASE WHEN is_readable = 0 THEN 1 ELSE 0 END) AS unreadable,
               SUM(CASE WHEN parse_state = 'done' THEN 1 ELSE 0 END) AS parsed,
               SUM(CASE WHEN parse_state = 'failed' THEN 1 ELSE 0 END) AS failed,
               SUM(CASE WHEN parse_state = 'pending' THEN 1 ELSE 0 END) AS pending,
               SUM(CASE WHEN content_hash IS NULL AND is_placeholder = 0
                        AND is_readable = 1 THEN 1 ELSE 0 END) AS uncompared,
               COALESCE(SUM(CASE WHEN is_placeholder = 1 THEN size_bytes ELSE 0 END), 0)
                   AS placeholder_bytes
        FROM source_files
        """
    ).fetchone()

    by_type = [
        {"ext": r["ext"], "count": r["n"], "bytes": r["b"] or 0}
        for r in conn.execute(
            "SELECT ext, COUNT(*) AS n, SUM(size_bytes) AS b FROM source_files "
            "GROUP BY ext ORDER BY b DESC"
        )
    ]

    last_scan = conn.execute(
        "SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()

    return {
        "sentence": summarize(conn),
        "counts": {k: (row[k] or 0) for k in row.keys()},
        "by_type": by_type,
        "last_scan": dict(last_scan) if last_scan else None,
    }


@router.get("/sources/{source_id}")
def get_source(request: Request, source_id: int) -> dict[str, Any]:
    conn = _conn(request)
    row = conn.execute(
        """
        SELECT sf.*, dup.path AS duplicate_of_path,
               0 AS finding_count, NULL AS worst_severity, NULL AS estimated_loss
        FROM source_files sf
        LEFT JOIN source_files dup ON dup.id = sf.duplicate_of
        WHERE sf.id = ?
        """,
        (source_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No file with id {source_id}.")

    findings = [
        dict(f)
        for f in conn.execute(
            "SELECT * FROM findings WHERE source_file_id = ? "
            "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
            "WHEN 'medium' THEN 2 ELSE 3 END, id",
            (source_id,),
        )
    ]
    duplicates = [
        {"id": d["id"], "path": d["path"]}
        for d in conn.execute(
            "SELECT id, path FROM source_files WHERE duplicate_of = ?", (source_id,)
        )
    ]

    detail = _row_to_source(row)
    detail["findings"] = findings
    detail["duplicates_of_this"] = duplicates
    return detail


@router.post("/sources/{source_id}/note")
def set_note(request: Request, source_id: int, body: NoteRequest) -> dict[str, Any]:
    conn = _conn(request)
    cur = conn.execute(
        "UPDATE source_files SET user_note = ? WHERE id = ?", (body.note, source_id)
    )
    if not cur.rowcount:
        raise HTTPException(status_code=404, detail=f"No file with id {source_id}.")
    return {"saved": True}


# ---------------------------------------------------------------------------
# OneDrive hydration - never without an explicit yes
# ---------------------------------------------------------------------------


@router.post("/sources/hydrate/plan")
def hydrate_plan(request: Request, body: HydrateRequest) -> dict[str, Any]:
    """What downloading these would cost. Downloads nothing."""
    conn = _conn(request)
    if not body.ids:
        raise HTTPException(status_code=400, detail="No files were chosen.")

    placeholders = ",".join("?" * len(body.ids))
    rows = conn.execute(
        f"SELECT id, path, size_bytes FROM source_files "
        f"WHERE id IN ({placeholders}) AND is_placeholder = 1",
        body.ids,
    ).fetchall()

    if not rows:
        raise HTTPException(
            status_code=400,
            detail="None of the chosen files are cloud-only, so there is nothing to download.",
        )

    from ..scan.onedrive import plan_hydration

    sizes = {r["path"]: (r["size_bytes"] or 0) for r in rows}
    plan = plan_hydration([Path(r["path"]) for r in rows], sizes)
    settings = _settings(request)
    cap_bytes = settings.onedrive.max_hydrate_batch_gb * (1024**3)

    return {
        "count": len(rows),
        "total_bytes": plan.total_bytes,
        "total_gb": round(plan.total_gb, 2),
        "free_bytes": plan.free_bytes,
        "fits_on_disk": plan.fits_on_disk,
        "over_batch_limit": plan.total_bytes > cap_bytes,
        "batch_limit_gb": settings.onedrive.max_hydrate_batch_gb,
        "sentence": plan.describe(),
        "files": [
            {"id": r["id"], "path": r["path"], "size_bytes": r["size_bytes"]}
            for r in rows
        ],
    }


@router.post("/sources/hydrate")
def hydrate_start(request: Request, body: HydrateRequest) -> dict[str, Any]:
    """Download cloud-only files. Requires confirm=true, sent after the plan."""
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="Downloading was not confirmed. Ask for the plan first, then confirm.",
        )

    settings = _settings(request)
    conn = _conn(request)
    placeholders = ",".join("?" * len(body.ids)) if body.ids else "NULL"
    rows = conn.execute(
        f"SELECT id, path, size_bytes FROM source_files "
        f"WHERE id IN ({placeholders}) AND is_placeholder = 1",
        body.ids,
    ).fetchall()
    if not rows:
        raise HTTPException(status_code=400, detail="Nothing to download.")

    total_bytes = sum(r["size_bytes"] or 0 for r in rows)
    cap_bytes = settings.onedrive.max_hydrate_batch_gb * (1024**3)
    if total_bytes > cap_bytes:
        raise HTTPException(
            status_code=400,
            detail=(
                f"That is {total_bytes / (1024**3):,.1f} GB, over the "
                f"{settings.onedrive.max_hydrate_batch_gb} GB safety limit for one "
                "batch. Choose fewer files, or raise max_hydrate_batch_gb in "
                "config.toml."
            ),
        )

    targets = [(int(r["id"]), r["path"], r["size_bytes"] or 0) for r in rows]

    def work(job) -> None:
        from ..db import connect, log_error
        from ..scan.fingerprint import hash_file
        from ..scan.onedrive import hydrate, still_placeholder

        own = connect(settings.db_path)
        try:
            job.progress(done=0, total=len(targets))
            for i, (sid, path, size) in enumerate(targets, start=1):
                if job.cancel_event.is_set():
                    return
                job.progress(
                    done=i - 1,
                    current=path,
                    message=f"Downloading {Path(path).name} ({size / (1024**2):,.0f} MB)...",
                )
                try:
                    hydrate(Path(path))
                except OSError as exc:
                    log_error(
                        own,
                        "hydrate",
                        f"Could not download {path}: {exc}",
                        detail=repr(exc),
                        source_file_id=sid,
                    )
                    own.execute(
                        "UPDATE source_files SET lock_error = ? WHERE id = ?",
                        (f"Download failed: {exc}", sid),
                    )
                    continue

                if still_placeholder(Path(path)):
                    own.execute(
                        "UPDATE source_files SET lock_error = ? WHERE id = ?",
                        (
                            "The download finished but Windows still reports this "
                            "file as cloud-only.",
                            sid,
                        ),
                    )
                    continue

                result = hash_file(path, size_cap_bytes=0, size_bytes=size)
                own.execute(
                    "UPDATE source_files SET is_placeholder = 0, content_hash = ?, "
                    "lock_error = NULL WHERE id = ?",
                    (result.content_hash, sid),
                )
            job.progress(done=len(targets))
            job.finish(f"Downloaded {len(targets)} file(s) from OneDrive.")
        finally:
            own.close()

    try:
        status = JOBS.start("hydrate", work, message="Downloading from OneDrive...")
    except JobBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"started": True, "job": status.as_dict()}


# ---------------------------------------------------------------------------
# Reading files into the archive
# ---------------------------------------------------------------------------


@router.post("/extract")
def start_extract(request: Request, body: ExtractRequest) -> dict[str, Any]:
    """Start reading. Returns immediately; watch /api/job for progress."""
    from ..extract import parse_kinds

    settings = _settings(request)
    conn = _conn(request)

    try:
        kinds = parse_kinds(body.kinds)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if body.ids:
        placeholders = ",".join("?" * len(body.ids))
        rows = conn.execute(
            f"SELECT id, path, is_placeholder, is_readable, parse_state "
            f"FROM source_files WHERE id IN ({placeholders})",
            body.ids,
        ).fetchall()

        blocked = [r for r in rows if r["is_placeholder"] or not r["is_readable"]]
        if blocked and len(blocked) == len(rows):
            raise HTTPException(
                status_code=400,
                detail=(
                    "None of the chosen files can be read yet. "
                    + (
                        "Some are stored in the cloud only and need downloading first. "
                        if any(r["is_placeholder"] for r in blocked) else ""
                    )
                    + (
                        "Some could not be opened - close Outlook and search again."
                        if any(not r["is_readable"] for r in blocked) else ""
                    )
                ),
            )
        source_ids = [
            int(r["id"]) for r in rows if not r["is_placeholder"] and r["is_readable"]
        ]
        skipped = len(blocked)
    else:
        source_ids = None
        skipped = 0

    def work(job) -> None:
        from ..db import connect
        from ..extract import Extractor

        own = connect(settings.db_path)
        try:
            extractor = Extractor(settings, own, cancel=job.cancel_event)

            def report(progress) -> None:
                job.progress(
                    done=progress.files_done,
                    total=progress.files_total,
                    current=(
                        f"{Path(progress.current_file).name}"
                        + (f"  -  {progress.current_folder}" if progress.current_folder else "")
                    ),
                    message=(
                        f"{progress.items_written:,} records added"
                        + (
                            f", {progress.duplicates_collapsed:,} already in the archive"
                            if progress.duplicates_collapsed else ""
                        )
                        + (
                            f", {progress.attachments_written:,} attachments saved"
                            if progress.attachments_written else ""
                        )
                    ),
                )

            extractor.on_progress = report
            result = extractor.run(
                kinds=kinds, source_ids=source_ids, sample=body.sample,
                resume=body.resume,
            )

            job.progress(done=result.files_done, total=result.files_total)
            job.finish(
                result.message,
                items_written=result.items_written,
                duplicates_collapsed=result.duplicates_collapsed,
                attachments_written=result.attachments_written,
                failures=result.failures,
                threads=result.detail_threads,
                skipped_unreadable=skipped,
                sampled=body.sample,
            )
        finally:
            own.close()

    try:
        status = JOBS.start("extract", work, message="Opening the first file...")
    except JobBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {
        "started": True,
        "files": len(source_ids) if source_ids is not None else None,
        "skipped_unreadable": skipped,
        "job": status.as_dict(),
    }


@router.get("/extract/plan")
def extract_plan(request: Request, ids: str = "") -> dict[str, Any]:
    """What reading would involve, before starting it."""
    conn = _conn(request)

    where = "WHERE is_placeholder = 0 AND is_readable = 1"
    params: list[Any] = []
    if ids:
        try:
            chosen = [int(i) for i in ids.split(",") if i.strip()]
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="The file numbers were not numbers."
            ) from exc
        where += f" AND id IN ({','.join('?' * len(chosen))})"
        params.extend(chosen)

    row = conn.execute(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes), 0) AS bytes, "
        f"SUM(CASE WHEN parse_state = 'done' THEN 1 ELSE 0 END) AS already_read, "
        f"SUM(CASE WHEN parse_state IN ('pending','selected') THEN 1 ELSE 0 END) AS to_read "
        f"FROM source_files {where}",
        params,
    ).fetchone()

    blocked = conn.execute(
        "SELECT SUM(is_placeholder) AS cloud, "
        "SUM(CASE WHEN is_readable = 0 THEN 1 ELSE 0 END) AS locked FROM source_files"
    ).fetchone()

    total_bytes = int(row["bytes"] or 0)
    to_read = int(row["to_read"] or 0)

    # A rough shape of the wait, from the spec's 500 items/sec target and the
    # size of what is queued. Said as a range, because it is an estimate and
    # pretending otherwise would be the kind of false precision this program
    # exists to avoid.
    minutes = max(1, round(total_bytes / (25 * 1024 * 1024)))

    return {
        "files": int(row["n"] or 0),
        "to_read": to_read,
        "already_read": int(row["already_read"] or 0),
        "total_bytes": total_bytes,
        "cloud_only": int(blocked["cloud"] or 0),
        "locked": int(blocked["locked"] or 0),
        "estimate": (
            "a few seconds" if minutes <= 1
            else f"roughly {minutes} to {minutes * 3} minutes"
        ),
        "note": (
            "This is a guess from the size of the files. A mailbox full of "
            "attachments takes longer than one of short notes. You can stop at "
            "any time and carry on later."
        ),
    }
