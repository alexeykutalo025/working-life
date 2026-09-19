"""Search, and the item viewer behind it.

Two rules from the spec shape what these endpoints return:

* **The honest-count rule.** A result total is a Count envelope, so a search
  over a period containing a gap says so rather than presenting its number as
  the whole answer.
* **Provenance is never lost.** The item endpoint returns every file a record
  was found in, because "this message exists in four of your backups" is a
  fact about the archive worth knowing.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from ..logging_setup import get_logger

log = get_logger("api.search")

router = APIRouter(tags=["search"])


def _conn(request: Request):
    return request.app.state.db()


def _settings(request: Request):
    return request.app.state.settings


@router.get("/search")
def search(
    request: Request,
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
    sort: str = "relevance",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Search the archive. Never returns a syntax error."""
    from ..integrity.honest import qualifiers_for
    from ..search.indexer import index_health
    from ..search.query import safe_query

    conn = _conn(request)
    parsed = safe_query(conn, q)

    where: list[str] = []
    params: list[Any] = []
    joins: list[str] = []

    if parsed.fts:
        joins.append("JOIN items_fts f ON f.rowid = i.id")
        where.append("items_fts MATCH ?")
        params.append(parsed.fts)

    if kind:
        where.append("i.kind = ?")
        params.append(kind)
    if undated:
        where.append("i.occurred_utc IS NULL")
    else:
        if date_from:
            where.append("i.occurred_utc >= ?")
            params.append(_start_of(date_from))
        if date_to:
            where.append("i.occurred_utc < ?")
            params.append(_after(date_to))
    if has_attachments is not None:
        where.append("i.has_attachments = ?")
        params.append(1 if has_attachments else 0)
    if person_id:
        where.append(
            "EXISTS (SELECT 1 FROM participations p WHERE p.item_id = i.id "
            "AND p.person_id = ?)"
        )
        params.append(person_id)
    if source_id:
        where.append(
            "EXISTS (SELECT 1 FROM item_sources s WHERE s.item_id = i.id "
            "AND s.source_file_id = ?)"
        )
        params.append(source_id)
    if folder_id:
        where.append("i.folder_id = ?")
        params.append(folder_id)
    if tag:
        where.append(
            "EXISTS (SELECT 1 FROM item_tags it JOIN tags t ON t.id = it.tag_id "
            "WHERE it.item_id = i.id AND t.name = ?)"
        )
        params.append(tag)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    join_sql = " ".join(joins)

    order = {
        "relevance": "f.rank" if parsed.fts else "i.occurred_utc DESC",
        "newest": "i.occurred_utc IS NULL, i.occurred_utc DESC",
        "oldest": "i.occurred_utc IS NULL, i.occurred_utc ASC",
    }.get(sort, "f.rank" if parsed.fts else "i.occurred_utc DESC")

    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM items i {join_sql} {where_sql}", params
        ).fetchone()["n"]

        snippet = (
            "snippet(items_fts, -1, '<mark>', '</mark>', ' … ', 24)"
            if parsed.fts else "NULL"
        )

        rows = conn.execute(
            f"""
            SELECT i.id, i.kind, i.subject, i.occurred_utc, i.occurred_local, i.tz,
                   i.has_attachments, i.parse_confidence, i.thread_id, i.location,
                   substr(COALESCE(i.body_text, ''), 1, 300) AS preview,
                   {snippet} AS snippet
            FROM items i {join_sql} {where_sql}
            ORDER BY {order}
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 - never show SQL to the user
        log.exception("Search failed for %r", q)
        raise HTTPException(
            status_code=400,
            detail=(
                "That search could not be run. Try putting the whole thing in "
                f'quotation marks: "{q}"'
            ),
        ) from exc

    results = [_result_row(conn, r) for r in rows]

    health = index_health(conn)
    qualifiers = qualifiers_for(
        conn,
        period_start=date_from[:7] if date_from else None,
        period_end=date_to[:7] if date_to else None,
    )

    return {
        "query": parsed.raw,
        "understood": parsed.describe(),
        "fts": parsed.fts,
        "fell_back": parsed.fell_back,
        "total": {
            "value": total,
            "qualified": bool(qualifiers) or not health["complete"],
            "qualifiers": [q.as_dict() for q in qualifiers],
            "index_note": health["note"],
        },
        "results": results,
        "offset": offset,
        "limit": limit,
        "undated_total": conn.execute(
            "SELECT COUNT(*) AS n FROM items WHERE occurred_utc IS NULL"
        ).fetchone()["n"],
    }


#: A table cannot be allowed to become a way to pull the whole archive into a
#: browser tab a row at a time. Beyond this, use the download.
TABLE_MAX = 500


@router.get("/search/table")
def search_table(
    request: Request,
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
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """The same results as a table, with the columns the spreadsheet uses.

    Deliberately built from the export row builders rather than from the search
    payload. Somebody comparing what is on the screen with what is in the
    workbook should never find a difference - that is the whole point of a
    table view for a client who wants to analyse this.

    Each kind has its own columns, because a calendar entry and a contact share
    almost none, so one kind is returned at a time.
    """
    from ..export.selection import _ROWS_FOR_KIND, KIND_SHEET_NAMES, _matching_ids
    from ..integrity.honest import qualifiers_for

    conn = _conn(request)
    limit = max(1, min(int(limit), TABLE_MAX))
    offset = max(0, int(offset))

    item_ids = _matching_ids(
        conn, query=q, kind=kind, person_id=person_id, source_id=source_id,
        folder_id=folder_id, tag=tag, has_attachments=has_attachments,
        undated=undated, date_from=date_from, date_to=date_to, limit=100_000,
    )

    counts: dict[str, int] = {}
    if item_ids:
        counts = {
            r["kind"]: int(r["n"])
            for r in conn.execute(
                f"SELECT kind, COUNT(*) AS n FROM items "
                f"WHERE id IN ({_marks(item_ids)}) GROUP BY kind",
                item_ids,
            )
        }

    kinds = [
        {
            "kind": k,
            "label": KIND_SHEET_NAMES.get(k, k),
            "count": counts[k],
            "can_table": k in _ROWS_FOR_KIND,
        }
        for k in sorted(counts, key=lambda k: -counts[k])
    ]

    # Which kind's table to show: the one asked for, else the biggest.
    showing = kind if kind in counts else (kinds[0]["kind"] if kinds else None)

    columns: list[str] = []
    rows: list[dict[str, Any]] = []
    total_of_kind = counts.get(showing, 0)

    if showing and showing in _ROWS_FOR_KIND:
        row_fn, columns = _ROWS_FOR_KIND[showing]
        page_ids = [
            int(r["id"])
            for r in conn.execute(
                f"SELECT id FROM items WHERE kind = ? AND id IN ({_marks(item_ids)}) "
                "ORDER BY occurred_utc IS NULL, occurred_utc, id LIMIT ? OFFSET ?",
                (showing, *item_ids, limit, offset),
            )
        ]
        if page_ids:
            rows = list(row_fn(
                conn, where=f"i.id IN ({_marks(page_ids)})", params=page_ids
            ))

    qualifiers = qualifiers_for(
        conn,
        kinds=[showing] if showing else None,
        source_ids=[source_id] if source_id else [],
        period_start=date_from[:7] if date_from else None,
        period_end=date_to[:7] if date_to else None,
    )

    return {
        "showing": showing,
        "kinds": kinds,
        "columns": columns,
        "headings": {c: _heading_for(c) for c in columns},
        "widths": {c: _width_for(c) for c in columns},
        "rows": rows,
        "offset": offset,
        "limit": limit,
        "total": {
            "value": total_of_kind,
            "qualified": bool(qualifiers),
            "qualifiers": [q.as_dict() for q in qualifiers],
        },
        "matched_total": len(item_ids),
    }


def _heading_for(column: str) -> str:
    """The same wording as the spreadsheet's header row."""
    from ..export.xlsx_export import _heading

    return _heading(column)


def _width_for(column: str) -> int:
    """The same width as the spreadsheet's column, in Excel character units.

    The screen converts to pixels. Sending it rather than keeping a second
    tuned list in JavaScript means the two cannot drift apart.
    """
    from ..export.xlsx_export import column_width

    return column_width(column)


def _marks(values) -> str:
    return ",".join("?" for _ in values)


def _result_row(conn, row) -> dict[str, Any]:
    people = [
        f"{r['display_name'] or r['raw_display_name'] or r['address']}"
        for r in conn.execute(
            "SELECT p.role, i.address, i.raw_display_name, pe.display_name "
            "FROM participations p JOIN identities i ON i.id = p.identity_id "
            "LEFT JOIN people pe ON pe.id = p.person_id "
            "WHERE p.item_id = ? ORDER BY CASE p.role WHEN 'from' THEN 0 "
            "WHEN 'organizer' THEN 0 ELSE 1 END LIMIT 6",
            (row["id"],),
        )
    ]

    warnings = [
        r["code"]
        for r in conn.execute(
            "SELECT DISTINCT code FROM findings WHERE item_id = ? "
            "AND state IN ('open','acknowledged')",
            (row["id"],),
        )
    ]

    return {
        "id": int(row["id"]),
        "kind": row["kind"],
        "subject": row["subject"],
        "occurred_utc": row["occurred_utc"],
        "occurred_local": row["occurred_local"],
        "timezone_known": bool(row["tz"]),
        "has_attachments": bool(row["has_attachments"]),
        "parse_confidence": row["parse_confidence"],
        "thread_id": row["thread_id"],
        "location": row["location"],
        "people": people,
        "snippet": row["snippet"],
        "preview": (row["preview"] or "").strip(),
        "warnings": warnings,
    }


@router.get("/search/filters")
def filters(request: Request) -> dict[str, Any]:
    """What is available to filter by, with counts."""
    conn = _conn(request)

    return {
        "kinds": [
            {"id": r["kind"], "count": r["n"]}
            for r in conn.execute(
                "SELECT kind, COUNT(*) AS n FROM items GROUP BY kind ORDER BY n DESC"
            )
        ],
        "people": [
            {"id": r["id"], "name": r["display_name"], "count": r["item_count"]}
            for r in conn.execute(
                "SELECT id, display_name, item_count FROM people "
                "WHERE merged_into IS NULL AND item_count > 0 "
                "ORDER BY item_count DESC LIMIT 100"
            )
        ],
        "sources": [
            {"id": r["id"], "path": r["path"], "count": r["item_count"]}
            for r in conn.execute(
                "SELECT id, path, item_count FROM source_files "
                "WHERE item_count > 0 ORDER BY item_count DESC LIMIT 100"
            )
        ],
        "folders": [
            {"id": r["id"], "path": r["path"], "count": r["item_count"]}
            for r in conn.execute(
                "SELECT id, path, item_count FROM folders WHERE item_count > 0 "
                "ORDER BY item_count DESC LIMIT 200"
            )
        ],
        "tags": [
            {"name": r["name"], "count": r["n"]}
            for r in conn.execute(
                "SELECT t.name, COUNT(*) AS n FROM tags t "
                "JOIN item_tags it ON it.tag_id = t.id GROUP BY t.name "
                "ORDER BY n DESC LIMIT 100"
            )
        ],
        "span": dict(
            conn.execute(
                "SELECT MIN(occurred_utc) AS first, MAX(occurred_utc) AS last "
                "FROM items WHERE occurred_utc IS NOT NULL"
            ).fetchone()
        ),
        "undated": conn.execute(
            "SELECT COUNT(*) AS n FROM items WHERE occurred_utc IS NULL"
        ).fetchone()["n"],
    }


# ---------------------------------------------------------------------------
# One item
# ---------------------------------------------------------------------------


@router.get("/items/{item_id}")
def get_item(request: Request, item_id: int) -> dict[str, Any]:
    """Everything about one record, including where it came from."""
    conn = _conn(request)

    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No record with id {item_id}.")

    item = dict(row)

    item["participants"] = [
        {
            "role": r["role"],
            "address": r["address"],
            "address_type": r["address_type"],
            "display_name": r["display_name"] or r["raw_display_name"],
            "person_id": r["person_id"],
            "response_status": r["response_status"],
        }
        for r in conn.execute(
            "SELECT p.role, p.person_id, p.response_status, i.address, "
            "i.address_type, i.raw_display_name, pe.display_name "
            "FROM participations p JOIN identities i ON i.id = p.identity_id "
            "LEFT JOIN people pe ON pe.id = p.person_id "
            "WHERE p.item_id = ? ORDER BY CASE p.role "
            "WHEN 'from' THEN 0 WHEN 'organizer' THEN 0 WHEN 'to' THEN 1 "
            "WHEN 'cc' THEN 2 ELSE 3 END, i.address",
            (item_id,),
        )
    ]

    item["attachments"] = [
        dict(r)
        for r in conn.execute(
            "SELECT id, filename, mime_type, size_bytes, content_hash, is_inline, "
            "content_id, extract_state, "
            "CASE WHEN extracted_text IS NULL THEN 0 ELSE length(extracted_text) END "
            "AS text_length FROM attachments WHERE item_id = ? ORDER BY is_inline, id",
            (item_id,),
        )
    ]

    # Provenance. "This item was found in 4 files" is a fact about the archive
    # that deduplication must not cost the user.
    item["sources"] = [
        {
            "source_file_id": int(r["source_file_id"]),
            "path": r["path"],
            "folder": r["folder_path"],
            "native_id": r["native_id"],
            "parse_backend": r["parse_backend"],
        }
        for r in conn.execute(
            "SELECT s.source_file_id, s.native_id, sf.path, sf.parse_backend, "
            "f.path AS folder_path "
            "FROM item_sources s JOIN source_files sf ON sf.id = s.source_file_id "
            "LEFT JOIN folders f ON f.id = s.folder_id "
            "WHERE s.item_id = ? ORDER BY sf.path",
            (item_id,),
        )
    ]

    item["findings"] = [
        dict(r)
        for r in conn.execute(
            "SELECT id, code, severity, title, detail, state FROM findings "
            "WHERE item_id = ? AND state IN ('open','acknowledged') "
            "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
            "WHEN 'medium' THEN 2 ELSE 3 END",
            (item_id,),
        )
    ]

    if row["thread_id"]:
        item["thread"] = {
            "id": int(row["thread_id"]),
            "messages": [
                dict(r)
                for r in conn.execute(
                    "SELECT id, subject, occurred_utc, kind FROM items "
                    "WHERE thread_id = ? "
                    "ORDER BY occurred_utc IS NULL, occurred_utc",
                    (row["thread_id"],),
                )
            ],
        }
    else:
        item["thread"] = None

    item["tags"] = [
        r["name"]
        for r in conn.execute(
            "SELECT t.name FROM tags t JOIN item_tags it ON it.tag_id = t.id "
            "WHERE it.item_id = ?",
            (item_id,),
        )
    ]

    if item.get("folder_id"):
        folder = conn.execute(
            "SELECT path FROM folders WHERE id = ?", (item["folder_id"],)
        ).fetchone()
        item["folder_path"] = folder["path"] if folder else None

    for column in ("recurrence_json", "references_json", "contact_json"):
        if item.get(column):
            try:
                item[column.removesuffix("_json")] = json.loads(item[column])
            except (TypeError, ValueError):
                pass

    return item


@router.get("/attachments/{attachment_id}")
def get_attachment(request: Request, attachment_id: int):
    """Serve one attachment's bytes out of the blob store."""
    from ..normalize.attachments import BlobStore

    conn = _conn(request)
    settings = _settings(request)

    row = conn.execute(
        "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No such attachment.")
    if not row["content_hash"]:
        raise HTTPException(
            status_code=404,
            detail=(
                "The contents of this attachment were never read, so there is "
                "nothing to open. The Problems screen says why."
            ),
        )

    from pathlib import Path

    suffix = Path(str(row["filename"] or "")).suffix.lower()
    store = BlobStore(settings.blobs_path)
    path = store.path_for(row["content_hash"], suffix)

    if not path.exists():
        data = store.get(row["content_hash"], suffix)
        if data is None:
            raise HTTPException(
                status_code=410,
                detail=(
                    "This attachment is listed in the archive but its saved copy "
                    "is missing from the attachments folder. Re-read the file it "
                    "came from and it will be saved again."
                ),
            )

    return FileResponse(
        path,
        media_type=row["mime_type"] or "application/octet-stream",
        filename=row["filename"] or f"attachment-{attachment_id}",
    )


@router.get("/attachments/{attachment_id}/text", response_class=PlainTextResponse)
def get_attachment_text(request: Request, attachment_id: int) -> str:
    """The text pulled out of an attachment, for reading without opening it."""
    conn = _conn(request)
    row = conn.execute(
        "SELECT extracted_text, extract_state, filename FROM attachments WHERE id = ?",
        (attachment_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No such attachment.")
    if row["extracted_text"]:
        return row["extracted_text"]

    reasons = {
        "unsupported": "Recall cannot read text out of this kind of file.",
        "failed": "Recall tried to read the text out of this file and could not.",
        "skipped": "This file was too large to search inside.",
        "pending": "This file has not been looked inside yet.",
    }
    return reasons.get(
        row["extract_state"], "There is no text in this attachment."
    )


class IndexRequest(BaseModel):
    rebuild: bool = False


@router.get("/index/health")
def index_status(request: Request) -> dict[str, Any]:
    from ..search.indexer import index_health

    return index_health(_conn(request))


@router.post("/index")
def rebuild_index(request: Request, body: IndexRequest) -> dict[str, Any]:
    """Build or rebuild the search index."""
    from ..db import connect, transaction
    from ..search.indexer import build_index

    settings = _settings(request)
    own = connect(settings.db_path)
    try:
        with transaction(own):
            result = build_index(own, rebuild=body.rebuild)
    finally:
        own.close()

    return {
        **result,
        "message": (
            f"{result['indexed']:,} of {result['total_items']:,} records are now "
            "searchable."
        ),
    }


def _start_of(value: str) -> str:
    """'2003' or '2003-04' or '2003-04-14' to an inclusive lower bound."""
    value = value.strip()
    if len(value) == 4:
        return f"{value}-01-01T00:00:00Z"
    if len(value) == 7:
        return f"{value}-01T00:00:00Z"
    return f"{value[:10]}T00:00:00Z"


def _after(value: str) -> str:
    """An exclusive upper bound, so 'to 2003' includes all of 2003."""
    value = value.strip()
    if len(value) == 4:
        return f"{int(value) + 1:04d}-01-01T00:00:00Z"
    if len(value) == 7:
        year, month = int(value[:4]), int(value[5:7])
        if month >= 12:
            return f"{year + 1:04d}-01-01T00:00:00Z"
        return f"{year:04d}-{month + 1:02d}-01T00:00:00Z"
    from datetime import datetime, timedelta

    try:
        day = datetime.strptime(value[:10], "%Y-%m-%d") + timedelta(days=1)
        return day.strftime("%Y-%m-%dT00:00:00Z")
    except ValueError:
        return f"{value[:10]}T23:59:59Z"
