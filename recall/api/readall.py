"""The one-pass endpoints: what it would cost, and starting it.

Two routes. ``GET /api/readall/plan`` works out the whole cost and downloads
nothing; ``POST /api/readall`` does it, and refuses without ``confirm``.

The confirm flag is not ceremony. Downloading someone's mailbox from OneDrive
spends their bandwidth and can cost them money, and the rule this program works
to is that it never happens without them having seen the number first. One
dialog counts as having agreed only if the request actually carries the
agreement.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..config import Settings
from ..db import CLOUD_ONLY_SQL, REACHABLE_SQL, connect
from ..logging_setup import get_logger
from ..parsers.pst_backend import LOCKED_BY_OUTLOOK_SQL
from ..readall import run_read_all
from ..scan.onedrive import plan_hydration
from .jobs import JOBS, JobBusy

log = get_logger("api.readall")

router = APIRouter(tags=["readall"])


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _conn(request: Request):
    return request.app.state.db()


class ReadAllRequest(BaseModel):
    confirm: bool = False
    roots: list[str] = []
    kinds: str | None = None
    full_hash: bool = False
    #: Search and read, but leave the cloud-only files where they are. The way
    #: out when the drive has not the room, so a full disk is a choice rather
    #: than a dead end.
    skip_download: bool = False
    sample: int = 0


def _outlook_available() -> bool:
    try:
        from ..comguard import is_outlook_registered

        return bool(is_outlook_registered())
    except Exception:  # noqa: BLE001 - never let a COM probe break a plan
        return False


def _how_long(minutes: float) -> str:
    """A wait, in the unit a person would use for it.

    "roughly 2048 to 6144 minutes" is arithmetically fine and completely
    unreadable, and a twenty-gigabyte mailbox is exactly the case this feature
    exists for. The unit is chosen from the low end, so a wait is never
    described as "0.7 hours", and stepped up only when the high end has grown
    unreadable in its turn. Always a range, because it is a guess.
    """
    low = max(1, round(minutes))
    high = low * 3

    UNITS = [("minute", 1), ("hour", 60), ("day", 60 * 24)]
    chosen = 0
    for i, (_, scale) in enumerate(UNITS):
        if low >= scale:
            chosen = i
    # Step up when the top of the range has itself become hard to read, but
    # only if the bottom still reads as at least one of the larger unit.
    if chosen + 1 < len(UNITS):
        name, scale = UNITS[chosen + 1]
        if high / UNITS[chosen][1] > 72 and low / scale >= 1:
            chosen += 1

    name, scale = UNITS[chosen]
    a, b = low / scale, high / scale

    def fmt(v: float) -> str:
        return f"{v:,.0f}" if v >= 10 or v == int(v) else f"{v:,.1f}"

    if fmt(a) == fmt(b):
        return f"roughly {fmt(a)} {name}{'' if fmt(a) == '1' else 's'}"
    return f"roughly {fmt(a)} to {fmt(b)} {name}s"


@router.get("/readall/plan")
def readall_plan(request: Request) -> dict:
    """Everything the one dialog needs, in one call. Downloads nothing."""
    settings = _settings(request)
    conn = _conn(request)

    cloud = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes), 0) AS bytes "
        f"FROM source_files WHERE {CLOUD_ONLY_SQL}"
    ).fetchone()
    cloud_rows = conn.execute(
        "SELECT id, path, size_bytes FROM source_files "
        f"WHERE {CLOUD_ONLY_SQL} ORDER BY size_bytes DESC LIMIT 200"
    ).fetchall()

    readable = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes), 0) AS bytes, "
        "  SUM(CASE WHEN parse_state = 'done' THEN 1 ELSE 0 END) AS already_read "
        f"FROM source_files WHERE {REACHABLE_SQL}"
    ).fetchone()

    locked = conn.execute(
        "SELECT COUNT(*) AS n FROM source_files "
        f"WHERE {LOCKED_BY_OUTLOOK_SQL}"
    ).fetchone()
    stuck = conn.execute(
        "SELECT COUNT(*) AS n FROM source_files "
        f"WHERE is_readable = 0 AND is_placeholder = 0 AND NOT {LOCKED_BY_OUTLOOK_SQL}"
    ).fetchone()

    last = conn.execute(
        "SELECT started_utc, state FROM scan_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()

    sizes = {r["path"]: int(r["size_bytes"] or 0) for r in cloud_rows}
    all_sizes = conn.execute(
        f"SELECT path, size_bytes FROM source_files WHERE {CLOUD_ONLY_SQL}"
    ).fetchall()
    sizes.update({r["path"]: int(r["size_bytes"] or 0) for r in all_sizes})

    plan = plan_hydration(
        [Path(r["path"]) for r in all_sizes],
        sizes,
        dest_dir=settings.cloud_path,
    )

    cloud_bytes = int(cloud["bytes"] or 0)
    read_bytes = int(readable["bytes"] or 0)
    # 10 MB/s for a download and 25 MB/s for reading. Both are guesses, said
    # as ranges, and the response says so.
    download_estimate = _how_long(cloud_bytes / (10 * 1024 * 1024)) if cloud_bytes else None
    read_estimate = _how_long(read_bytes / (25 * 1024 * 1024))

    return {
        "scan": {
            "last_scan_utc": last["started_utc"] if last else None,
            "known_files": int(readable["n"] or 0) + int(cloud["n"] or 0),
            "note": (
                "These numbers come from the last search. Recall searches again "
                "first, so it may find more than this - never less."
            ),
        },
        "download": {
            "count": int(cloud["n"] or 0),
            "bytes": cloud_bytes,
            "files": [
                {
                    "id": int(r["id"]),
                    "path": r["path"],
                    "size_bytes": int(r["size_bytes"] or 0),
                }
                for r in cloud_rows
            ],
            "listed": len(cloud_rows),
        },
        "read": {
            "count": int(readable["n"] or 0),
            "bytes": read_bytes,
            "already_read": int(readable["already_read"] or 0),
        },
        "locked": {
            "outlook": int(locked["n"] or 0),
            "other": int(stuck["n"] or 0),
            "outlook_available": _outlook_available(),
        },
        "disk": {
            "dest_path": str(settings.cloud_path),
            "needed_bytes": plan.needed_bytes,
            "free_bytes": plan.free_bytes,
            "fits": plan.fits_on_disk,
            "tight_drives": plan.tight_drives,
            "doubles_up": plan.needed_bytes > plan.total_bytes,
        },
        "estimate": {
            "download": download_estimate,
            "read": read_estimate,
            "note": (
                "Both are guesses. The download depends on your internet "
                "connection and the reading on what is in the files. You can "
                "stop at any point and carry on later."
            ),
        },
        "sentence": plan.describe() if cloud_bytes else (
            "Nothing is stored in the cloud only, so nothing needs downloading."
        ),
        # A fresh machine has an empty list and this is exactly what fills it,
        # so an empty inventory is never a reason to refuse.
        "can_start": True,
    }


@router.post("/readall")
def start_readall(request: Request, body: ReadAllRequest) -> dict:
    """Search, download and read, as one job."""
    settings = _settings(request)

    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail=(
                "Recall did not start, because this request did not carry your "
                "agreement to the download size. Open the Files screen and use "
                "the button there."
            ),
        )

    roots = [Path(r) for r in body.roots] if body.roots else None

    def work(job) -> None:
        # The job runs off the request thread, so it needs its own connection.
        own = connect(settings.db_path)
        try:
            result = run_read_all(
                settings,
                own,
                job=job,
                roots=roots,
                kinds=body.kinds,
                full_hash=body.full_hash,
                skip_download=body.skip_download,
                sample=body.sample,
            )
        finally:
            try:
                own.close()
            except Exception:  # noqa: BLE001
                pass

        job.finish(_sentence(result), **result.as_detail())

    try:
        status = JOBS.start(
            "read_all", work, message="Looking for Outlook files on this computer..."
        )
    except JobBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {"started": True, "job": status.as_dict()}


def _sentence(result) -> str:
    """One plain sentence for the top of the finished card."""
    if result.stopped_for == "disk":
        return (
            f"Stopped because the drive filled up. "
            f"{result.files_copied:,} file(s) were downloaded and "
            f"{result.items_written:,} record(s) were saved before that."
        )
    if result.stopped_for == "workdir":
        return (
            "Stopped: Recall's working folder is inside OneDrive, so copying "
            "files into it would upload them straight back."
        )
    if result.stopped_for == "scan":
        return "The search stopped with a problem, so nothing was downloaded or read."

    parts = [f"Found {result.files_found:,} file(s)"]
    if result.files_copied:
        parts.append(f"downloaded {result.files_copied:,}")
    parts.append(f"read {result.files_read:,}")
    return (
        ", ".join(parts)
        + f", and saved {result.items_written:,} record(s) into the archive."
    )
