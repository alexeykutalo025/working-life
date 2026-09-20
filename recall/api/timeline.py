"""Timeline data, and the export endpoints.

The rendering rule from spec 9.2 is enforced here rather than left to the
drawing code: every bucket the timeline returns is explicit about whether it
holds data, holds none, or falls outside the archive entirely. There is no way
for the chart to receive a gap as "no row" and quietly close over it, because a
gap arrives as a row with ``has_data: false`` and a reason.
"""

from __future__ import annotations

import calendar as _calendar
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..logging_setup import get_logger

log = get_logger("api.timeline")

router = APIRouter(tags=["timeline"])

KINDS = ("message", "event", "contact", "task", "note")


def _conn(request: Request):
    return request.app.state.db()


def _settings(request: Request):
    return request.app.state.settings


class ExportRequest(BaseModel):
    format: str = "csv"
    kind: str = "calendar"
    full: bool = True
    out_path: str | None = None


class SearchExportRequest(BaseModel):
    """Export exactly what is on the Search screen."""

    format: str = "csv"
    q: str = ""
    kind: str | None = None
    person_id: int | None = None
    source_id: int | None = None
    folder_id: int | None = None
    tag: str | None = None
    has_attachments: bool | None = None
    undated: bool = False
    date_from: str | None = None
    date_to: str | None = None
    copy_attachments: bool = False
    out_path: str | None = None


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------


@router.get("/timeline")
def timeline(
    request: Request,
    level: str = "year",
    year: int | None = None,
    month: int | None = None,
) -> dict[str, Any]:
    """Counts per bucket, with gaps marked rather than omitted.

    ``level`` is year, month or day. ``year`` and ``month`` narrow it when
    drilling in.
    """
    conn = _conn(request)

    if level not in ("year", "month", "day"):
        raise HTTPException(
            status_code=400,
            detail=f"{level!r} is not a level. Use year, month or day.",
        )

    span = conn.execute(
        "SELECT MIN(occurred_utc) AS first, MAX(occurred_utc) AS last, "
        "COUNT(*) AS n FROM items WHERE occurred_utc IS NOT NULL"
    ).fetchone()

    if not span or not span["first"]:
        return {
            "level": level,
            "buckets": [],
            "kinds": list(KINDS),
            "span": None,
            "undated": _undated(conn),
            "eras": _eras(conn),
            "total": {"value": 0, "qualified": False, "qualifiers": [], "sentence": "0 records"},
            "empty_reason": (
                "Nothing has been read into the archive yet, so there is no "
                "timeline to draw."
            ),
        }

    first_year = int(span["first"][:4])
    last_year = int(span["last"][:4])

    if level == "year":
        buckets = _year_buckets(conn, first_year, last_year)
    elif level == "month":
        if year is None:
            raise HTTPException(
                status_code=400, detail="Drilling into months needs a year."
            )
        buckets = _month_buckets(conn, year)
    else:
        if year is None or month is None:
            raise HTTPException(
                status_code=400, detail="Drilling into days needs a year and a month."
            )
        buckets = _day_buckets(conn, year, month)

    from ..integrity.honest import count_items

    total = count_items(conn, label="records")

    return {
        "level": level,
        "year": year,
        "month": month,
        "buckets": buckets,
        "kinds": list(KINDS),
        "span": {"first": span["first"], "last": span["last"], "first_year": first_year,
                 "last_year": last_year},
        "undated": _undated(conn),
        "eras": _eras(conn),
        "total": total.as_dict(),
    }


def _year_buckets(conn, first_year: int, last_year: int) -> list[dict]:
    counts: dict[tuple[str, str], int] = {}
    for row in conn.execute(
        "SELECT substr(occurred_utc, 1, 4) AS y, kind, COUNT(*) AS n "
        "FROM items WHERE occurred_utc IS NOT NULL GROUP BY y, kind"
    ):
        counts[(row["y"], row["kind"])] = int(row["n"])

    sources = _sources_per_period(conn, 4)
    gaps = _gap_classes(conn)

    buckets = []
    for y in range(first_year, last_year + 1):
        key = f"{y:04d}"
        by_kind = {k: counts.get((key, k), 0) for k in KINDS}
        total = sum(by_kind.values())
        # A year is a gap when every month in it is a gap.
        year_gaps = {m: c for m, c in gaps.items() if m.startswith(key)}
        buckets.append({
            "key": key,
            "label": key,
            "year": y,
            "total": total,
            "by_kind": by_kind,
            "has_data": total > 0,
            "source_count": sources.get(key, 0),
            "gap_class": _year_gap_class(total, year_gaps),
            "gap_months": sorted(year_gaps),
            "explained": _all_explained(conn, sorted(year_gaps)),
        })
    return buckets


def _month_buckets(conn, year: int) -> list[dict]:
    counts: dict[tuple[str, str], int] = {}
    for row in conn.execute(
        "SELECT substr(occurred_utc, 1, 7) AS m, kind, COUNT(*) AS n "
        "FROM items WHERE occurred_utc IS NOT NULL AND substr(occurred_utc, 1, 4) = ? "
        "GROUP BY m, kind",
        (f"{year:04d}",),
    ):
        counts[(row["m"], row["kind"])] = int(row["n"])

    sources = _sources_per_period(conn, 7)
    gaps = _gap_classes(conn)
    census = _census(conn)

    buckets = []
    for m in range(1, 13):
        key = f"{year:04d}-{m:02d}"
        by_kind = {k: counts.get((key, k), 0) for k in KINDS}
        total = sum(by_kind.values())
        buckets.append({
            "key": key,
            "label": _calendar.month_abbr[m],
            "year": year,
            "month": m,
            "total": total,
            "by_kind": by_kind,
            "has_data": total > 0,
            "source_count": sources.get(key, 0),
            "gap_class": gaps.get(key),
            "explained": bool(census.get(key, {}).get("explained_by_user")),
        })
    return buckets


def _day_buckets(conn, year: int, month: int) -> list[dict]:
    counts: dict[tuple[str, str], int] = {}
    prefix = f"{year:04d}-{month:02d}"
    for row in conn.execute(
        "SELECT substr(occurred_utc, 1, 10) AS d, kind, COUNT(*) AS n "
        "FROM items WHERE occurred_utc IS NOT NULL AND substr(occurred_utc, 1, 7) = ? "
        "GROUP BY d, kind",
        (prefix,),
    ):
        counts[(row["d"], row["kind"])] = int(row["n"])

    days = _calendar.monthrange(year, month)[1]
    buckets = []
    for d in range(1, days + 1):
        key = f"{prefix}-{d:02d}"
        by_kind = {k: counts.get((key, k), 0) for k in KINDS}
        total = sum(by_kind.values())
        buckets.append({
            "key": key,
            "label": str(d),
            "year": year,
            "month": month,
            "day": d,
            "total": total,
            "by_kind": by_kind,
            "has_data": total > 0,
            # A single empty day is not a gap in any meaningful sense, so no
            # day is ever hatched. Gaps are a monthly judgement.
            "gap_class": None,
            "explained": False,
        })
    return buckets


def _sources_per_period(conn, length: int) -> dict[str, int]:
    """How many distinct files contributed to each period.

    One source for a whole decade is a different picture from eleven, and the
    timeline says which.
    """
    out: dict[str, int] = {}
    for row in conn.execute(
        f"SELECT substr(i.occurred_utc, 1, {length}) AS p, "
        f"COUNT(DISTINCT s.source_file_id) AS n "
        f"FROM items i JOIN item_sources s ON s.item_id = i.id "
        f"WHERE i.occurred_utc IS NOT NULL GROUP BY p"
    ):
        out[row["p"]] = int(row["n"])
    return out


def _gap_classes(conn) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in conn.execute(
        "SELECT month, gap_class FROM coverage_months WHERE gap_class IS NOT NULL"
    ):
        out[row["month"]] = row["gap_class"]
    return out


def _census(conn) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in conn.execute(
        "SELECT month, SUM(item_count) AS n, MAX(explained_by_user) AS explained "
        "FROM coverage_months GROUP BY month"
    ):
        out[row["month"]] = {"item_count": row["n"], "explained_by_user": row["explained"]}
    return out


def _year_gap_class(total: int, month_gaps: dict[str, str]) -> str | None:
    if total == 0:
        return "hard_gap"
    if any(c == "source_contradiction" for c in month_gaps.values()):
        return "source_contradiction"
    if month_gaps:
        return "partial"
    return None


def _all_explained(conn, months: list[str]) -> bool:
    if not months:
        return False
    placeholders = ",".join("?" * len(months))
    row = conn.execute(
        f"SELECT MIN(explained_by_user) AS all_explained FROM coverage_months "
        f"WHERE month IN ({placeholders})",
        months,
    ).fetchone()
    return bool(row and row["all_explained"])


def _undated(conn) -> dict[str, Any]:
    """Records with no date. Visible, counted, never placed on the chart."""
    rows = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM items WHERE occurred_utc IS NULL GROUP BY kind"
    ).fetchall()
    by_kind = {r["kind"]: int(r["n"]) for r in rows}
    total = sum(by_kind.values())
    return {
        "total": total,
        "by_kind": by_kind,
        "note": (
            f"{total:,} record(s) have no usable date. They are not on the "
            "timeline and no date has been guessed for them."
        ) if total else "",
    }


def _eras(conn) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute("SELECT * FROM eras ORDER BY start_utc, id")
    ]


# ---------------------------------------------------------------------------
# Eras
# ---------------------------------------------------------------------------


class EraRequest(BaseModel):
    name: str
    start_utc: str | None = None
    end_utc: str | None = None
    color: str | None = None
    notes: str | None = None


def normalize_era_date(value: str | None, *, end: bool, label: str) -> str | None:
    """Turn what a person types into a date, or say plainly why it is not one.

    A period is something like "Harrow & Sons, 1984 to 1997", and nobody wants
    to type 1984-01-01 to say that. A year on its own means the whole year, a
    year and month means the whole month, and the end of a period is inclusive
    - so "to 1997" ends on the last day of 1997, not the first.

    An unparseable date is refused rather than dropped. A period silently
    missing one end would shade the wrong stretch of the chart, and the chart
    is the thing the user is trying to make sense of.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None

    # Accept an ISO timestamp by keeping only the date part of it.
    text = text.split("T")[0].strip()

    unreadable = HTTPException(
        status_code=400,
        detail=(
            f"{label} is not a date I can read. Write a year (1984), "
            f"a year and month (1984-06), or a full date (1984-06-01)."
        ),
    )

    raw = text.split("-")
    if len(raw) > 3 or not all(p.isdigit() for p in raw):
        raise unreadable

    # A four-digit year, always. "84" could be 1984 or 2084 and this program
    # does not guess dates - not here, and not in the archive.
    if len(raw[0]) != 4:
        raise HTTPException(
            status_code=400,
            detail=f"{label} needs the year written out in full, like 1984.",
        )

    parts = [int(p) for p in raw]
    year = parts[0]
    month = parts[1] if len(parts) > 1 else (12 if end else 1)

    if year < 1:
        raise unreadable
    if not 1 <= month <= 12:
        raise HTTPException(
            status_code=400, detail=f"{label} has month {month}, and months run 1 to 12."
        )

    last = _calendar.monthrange(year, month)[1]
    day = parts[2] if len(parts) > 2 else (last if end else 1)

    if not 1 <= day <= last:
        raise HTTPException(
            status_code=400,
            detail=f"{label} has day {day}, and that month has {last} days.",
        )

    return f"{year:04d}-{month:02d}-{day:02d}"


@router.get("/eras")
def list_eras(request: Request) -> list[dict]:
    return _eras(_conn(request))


@router.post("/eras")
def create_era(request: Request, body: EraRequest) -> dict:
    conn = _conn(request)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="A period needs a name.")

    start = normalize_era_date(body.start_utc, end=False, label="The start date")
    end = normalize_era_date(body.end_utc, end=True, label="The end date")

    if start and end and end < start:
        raise HTTPException(
            status_code=400,
            detail=f"This period ends ({end}) before it starts ({start}).",
        )

    cur = conn.execute(
        "INSERT INTO eras(name, start_utc, end_utc, color, notes) VALUES (?, ?, ?, ?, ?)",
        (name, start, end, body.color, body.notes),
    )
    return {"id": int(cur.lastrowid), "saved": True}


@router.delete("/eras/{era_id}")
def delete_era(request: Request, era_id: int) -> dict:
    conn = _conn(request)
    cur = conn.execute("DELETE FROM eras WHERE id = ?", (era_id,))
    if not cur.rowcount:
        raise HTTPException(status_code=404, detail=f"No period with id {era_id}.")
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------


@router.get("/export/formats")
def export_formats() -> dict[str, Any]:
    from ..export import KIND_SPECS

    return {
        "formats": [
            {"id": "csv", "label": "CSV — opens in Excel"},
            {"id": "xlsx", "label": "Excel workbook (.xlsx)"},
            {"id": "markdown", "label": "Markdown — one readable document"},
            {"id": "json", "label": "JSON — for another program to read"},
        ],
        "kinds": [
            {"id": k, "label": spec["description"]} for k, spec in KIND_SPECS.items()
        ],
        "note": (
            "Every export is accompanied by a plain-language statement of what "
            "is missing or uncertain in it."
        ),
    }


def _confined_out_path(raw: str | None, settings) -> str | None:
    """Keep a requested filename inside the exports folder.

    The browser could ask the server to write anywhere on the disk, guarded
    only by the OneDrive check. Nothing in the interface does that, but a local
    server is still a server, and "write this file here" is not a decision a
    web page gets to make about somebody's whole computer.
    """
    if not raw:
        return None

    exports = Path(settings.exports_path).resolve()
    asked = Path(raw)

    # A bare filename means "call it this"; anything with a folder in it is
    # asking for a location, and that is refused rather than quietly redirected
    # - a file that turns up somewhere other than where it was asked for is a
    # worse surprise than being told no.
    if asked.parent != Path("."):
        try:
            where = asked.parent.resolve()
        except OSError:
            where = asked.parent
        if where != exports:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Exports are written to Recall's own exports folder, so "
                    "that nothing is scattered around your computer. "
                    f"That folder is: {exports}"
                ),
            )

    return str(exports / asked.name)


@router.post("/export")
def export(request: Request, body: ExportRequest) -> dict[str, Any]:
    from ..export import ExportError, run_export

    conn = _conn(request)
    settings = _settings(request)

    try:
        data_path, statement_path, statement = run_export(
            conn,
            settings,
            fmt=body.format,
            kind=body.kind,
            out_path=_confined_out_path(body.out_path, _settings(request)),
            full=body.full,
        )
    except ExportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"The file could not be written: {exc}",
        ) from exc

    return {
        "file": str(data_path),
        "integrity_file": str(statement_path) if statement_path else None,
        "folder": str(Path(data_path).parent),
        "exported_count": statement.exported_count,
        "estimated_missing": statement.estimated_missing,
        "is_complete": statement.is_clean,
        "statement": statement.as_dict(),
    }


@router.post("/export/search")
def export_search_results(request: Request, body: SearchExportRequest) -> dict[str, Any]:
    """Save a search result set, with an integrity statement for that set."""
    from ..export import ExportError
    from ..export.selection import export_search

    try:
        result = export_search(
            _conn(request),
            _settings(request),
            fmt=body.format,
            query=body.q,
            kind=body.kind,
            person_id=body.person_id,
            source_id=body.source_id,
            folder_id=body.folder_id,
            tag=body.tag,
            has_attachments=body.has_attachments,
            undated=body.undated,
            date_from=body.date_from,
            date_to=body.date_to,
            out_path=_confined_out_path(body.out_path, _settings(request)),
            copy_attachments=body.copy_attachments,
        )
    except ExportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=400, detail=f"The file could not be written: {exc}"
        ) from exc

    result["message"] = _export_message(result)
    return result


def _export_message(result: dict[str, Any]) -> str:
    """What to say about a finished export, including what is not in it."""
    incomplete = [f for f in result["files"] if not f["is_complete"]]
    message = (
        f"Saved {result['total_records']:,} record(s) to "
        f"{len(result['files'])} file(s)."
    )
    if incomplete:
        message += " Some of them are not complete - the note beside each one says why."
    if result.get("left_out_total"):
        kinds = ", ".join(f"{u['count']:,} {u['kind']}" for u in result["left_out"])
        message += (
            f" {result['left_out_total']:,} record(s) could not be written to this "
            f"kind of file ({kinds}) and are NOT included in the count above."
        )
    return message


@router.get("/export/search/download")
def download_search_results(
    request: Request,
    format: str = "xlsx",
    q: str = "",
    kind: str | None = None,
    person_id: int | None = None,
    source_id: int | None = None,
    folder_id: int | None = None,
    tag: str | None = None,
    has_attachments: bool | None = None,
    undated: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
) -> FileResponse:
    """The same export, handed straight to the browser.

    A GET, because that is what an ordinary download link is, and a link is
    what somebody expects to click. The file is still written into the exports
    folder on the way past, so nothing is lost if the download is cancelled or
    the user wants it again later.

    Formats that produce one file per kind give back the first; Excel gives one
    workbook holding every kind, which is why it is the default here.
    """
    from ..export import ExportError
    from ..export.selection import export_search

    try:
        result = export_search(
            _conn(request), _settings(request),
            fmt=format, query=q, kind=kind, person_id=person_id,
            source_id=source_id, folder_id=folder_id, tag=tag,
            has_attachments=has_attachments, undated=undated,
            date_from=date_from, date_to=date_to,
        )
    except ExportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=400, detail=f"The file could not be written: {exc}"
        ) from exc

    path = Path(result["files"][0]["file"])
    if not path.exists():  # pragma: no cover - the writer would have raised
        raise HTTPException(status_code=500, detail="The file was not written.")

    return FileResponse(
        path,
        filename=path.name,
        media_type=_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"),
        headers={
            # So a browser saves it rather than trying to display it.
            "Content-Disposition": f'attachment; filename="{path.name}"',
            # What is missing from the file, for anyone reading the response
            # rather than opening the workbook. The Integrity sheet is inside.
            "X-Recall-Records": str(result["total_records"]),
            "X-Recall-Complete": "yes" if all(
                f["is_complete"] for f in result["files"]
            ) else "no",
        },
    )


_MEDIA_TYPES = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
    ".json": "application/json",
    ".md": "text/markdown",
}
