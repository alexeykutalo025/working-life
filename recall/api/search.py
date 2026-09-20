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
import re
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

    # Both bounds checked once, before anything else runs, so an unreadable
    # date cannot reach a helper further down and come back as a stack trace.
    try:
        period_start = month_bound(date_from, end=False)
        period_end = month_bound(date_to, end=True)
    except BadDate as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

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
        conn, period_start=period_start, period_end=period_end
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


def _card(path: str) -> str:
    """One field out of a contact card, as SQL.

    Guarded, because ``json_extract`` on a card that will not parse is an
    error that would take the whole table down rather than leaving one cell
    out of the sort.
    """
    return (
        f"CASE WHEN json_valid(i.contact_json) "
        f"THEN json_extract(i.contact_json, '{path}') END"
    )


#: The body columns show a preview with the leading blank lines taken off, so
#: the sort has to take them off too, or a message that begins with an empty
#: line sorts somewhere its cell on screen gives no reason for.
_BODY_TEXT = (
    "LTRIM(i.body_text, ' ' || char(9) || char(10) || char(13)) COLLATE NOCASE"
)


#: How to order the table by each column, as SQL over ``items i``.
#:
#: A column can be sorted when its cell holds one value the database also
#: holds - a column of the row, or the same sub-select the row builder in
#: ``export.rows`` already uses. A cell assembled in Python out of several
#: values - everyone who was on a message, a sentence about what is uncertain
#: about a record - is deliberately absent. Sorting those would mean writing a
#: second version of that assembly in SQL, and the day the two disagreed the
#: user would see a table that looks unsorted, which is worse than a heading
#: that does not offer to sort at all.
#:
#: Text sorts case-insensitively: somebody clicking "Subject" wants an
#: alphabet, not the ASCII order where every capital comes before every
#: small letter.
_SORT_SQL: dict[str, str] = {
    # Straight off the item row.
    "item_id": "i.id",
    "date": "i.occurred_utc",
    "first_seen": "i.occurred_utc",
    "due_date": "i.end_utc",
    "subject": "i.subject COLLATE NOCASE",
    "location": "i.location COLLATE NOCASE",
    "importance": "i.importance COLLATE NOCASE",
    "message_id": "i.internet_message_id COLLATE NOCASE",
    "meeting_status": "i.meeting_status COLLATE NOCASE",
    "busy_status": "i.busy_status COLLATE NOCASE",
    "timezone": "COALESCE(i.tz, 'unknown') COLLATE NOCASE",
    # The yes/no columns. 0 before 1 is "no" before "yes", which is also what
    # sorting the words on screen would give.
    "has_attachments": "i.has_attachments",
    "all_day": "i.all_day",
    "recurring": "i.is_recurring_master",
    "timezone_known": "i.tz IS NOT NULL",
    # The clock columns, in the form the cell shows: an all-day event has no
    # time in it, and sorts with the other blanks.
    "time": "substr(i.occurred_utc, 12, 5)",
    "start": "CASE WHEN i.all_day THEN NULL ELSE substr(i.occurred_utc, 12, 5) END",
    "end": "CASE WHEN i.all_day THEN NULL ELSE substr(i.end_utc, 12, 5) END",
    "duration_min": (
        "CASE WHEN i.all_day THEN NULL "
        "ELSE julianday(i.end_utc) - julianday(i.occurred_utc) END"
    ),
    "body": _BODY_TEXT,
    "body_preview": _BODY_TEXT,
    "notes": _BODY_TEXT,
    # Looked up elsewhere, but by the same sub-select the row is built from.
    "folder": "(SELECT f.path FROM folders f WHERE f.id = i.folder_id) COLLATE NOCASE",
    "source_file": (
        "(SELECT GROUP_CONCAT(sf.path, ' | ') FROM item_sources isrc "
        "JOIN source_files sf ON sf.id = isrc.source_file_id "
        "WHERE isrc.item_id = i.id) COLLATE NOCASE"
    ),
    "category": (
        "(SELECT GROUP_CONCAT(t.name, '; ') FROM item_tags it "
        "JOIN tags t ON t.id = it.tag_id WHERE it.item_id = i.id) COLLATE NOCASE"
    ),
    "attachment_names": (
        "(SELECT GROUP_CONCAT(a.filename, '; ') FROM attachments a "
        "WHERE a.item_id = i.id) COLLATE NOCASE"
    ),
    # One person, picked the same way the row builder picks them.
    "attendee_count": (
        "(SELECT COUNT(*) FROM participations p WHERE p.item_id = i.id "
        "AND p.role IN ('attendee','optional','resource'))"
    ),
    "from_name": (
        "(SELECT COALESCE(pe.display_name, ident.raw_display_name) "
        "FROM participations p JOIN identities ident ON ident.id = p.identity_id "
        "LEFT JOIN people pe ON pe.id = p.person_id "
        "WHERE p.item_id = i.id AND p.role = 'from' "
        "ORDER BY ident.address LIMIT 1) COLLATE NOCASE"
    ),
    "from_address": (
        "(SELECT ident.address FROM participations p "
        "JOIN identities ident ON ident.id = p.identity_id "
        "WHERE p.item_id = i.id AND p.role = 'from' "
        "ORDER BY ident.address LIMIT 1) COLLATE NOCASE"
    ),
    # A contact's own card.
    "display_name": (
        f"COALESCE(NULLIF({_card('$.display_name')}, ''), i.subject) COLLATE NOCASE"
    ),
    "given_name": f"{_card('$.given_name')} COLLATE NOCASE",
    "surname": f"{_card('$.surname')} COLLATE NOCASE",
    "organization": f"{_card('$.organization')} COLLATE NOCASE",
    "title": f"{_card('$.title')} COLLATE NOCASE",
    "phone_business": f"{_card('$.phones.business')} COLLATE NOCASE",
    "phone_home": f"{_card('$.phones.home')} COLLATE NOCASE",
    "phone_mobile": f"{_card('$.phones.mobile')} COLLATE NOCASE",
}

#: What the table is ordered by when nobody has chosen a column: oldest first,
#: with the records that have no date at the end rather than pretending to a
#: place in the run of years.
_DEFAULT_ORDER = "i.occurred_utc IS NULL, i.occurred_utc, i.id"


def _order_by(column: str | None, *, descending: bool) -> str:
    """The ORDER BY for one page of the table.

    An empty cell goes last whichever way the column runs - a screenful of
    blanks is never what somebody clicking a heading was after - and ``i.id``
    breaks every tie, so that two records holding the same value cannot swap
    places between one page and the next and appear twice or not at all.
    """
    if column is None:
        return _DEFAULT_ORDER
    return f"{_SORT_SQL[column]} {'DESC' if descending else 'ASC'} NULLS LAST, i.id"


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
    sort: str | None = None,
    direction: str = "asc",
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

    ``sort`` names a column to order by and ``direction`` is "asc" or "desc".
    The ordering is applied to every matching record before the page is cut,
    not to the page afterwards, so clicking a heading sorts the result set
    rather than the fifty rows that happen to be on screen. A column that
    cannot be sorted faithfully is ignored, and the response says which
    column was actually used and which ones could be.
    """
    from ..db import IN_IDS, ids_param
    from ..export.selection import _ROWS_FOR_KIND, KIND_SHEET_NAMES, _matching_ids
    from ..integrity.honest import qualifiers_for

    conn = _conn(request)
    limit = max(1, min(int(limit), TABLE_MAX))
    offset = max(0, int(offset))

    try:
        period_start = month_bound(date_from, end=False)
        period_end = month_bound(date_to, end=True)
    except BadDate as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    item_ids = _matching_ids(
        conn, query=q, kind=kind, person_id=person_id, source_id=source_id,
        folder_id=folder_id, tag=tag, has_attachments=has_attachments,
        undated=undated, date_from=date_from, date_to=date_to, limit=100_000,
    )
    # The whole list as one parameter, not one parameter per record: a real
    # archive matches more records than SQLite will take parameters for, and
    # this one answered "too many SQL variables" instead of showing a table.
    # See recall.db.IN_IDS.
    matched = ids_param(item_ids)

    counts: dict[str, int] = {}
    if item_ids:
        counts = {
            r["kind"]: int(r["n"])
            for r in conn.execute(
                f"SELECT kind, COUNT(*) AS n FROM items "
                f"WHERE id {IN_IDS} GROUP BY kind",
                (matched,),
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

    sortable: list[str] = []
    descending = str(direction).lower() == "desc"
    sort_column: str | None = None

    if showing and showing in _ROWS_FOR_KIND:
        row_fn, columns = _ROWS_FOR_KIND[showing]
        sortable = [c for c in columns if c in _SORT_SQL]
        sort_column = sort if sort in sortable else None

        page_ids = [
            int(r["id"])
            for r in conn.execute(
                f"SELECT i.id FROM items i WHERE i.kind = ? AND i.id {IN_IDS} "
                f"ORDER BY {_order_by(sort_column, descending=descending)} "
                "LIMIT ? OFFSET ?",
                (showing, matched, limit, offset),
            )
        ]
        if page_ids:
            # One page, so this list is small by construction.
            built = {
                int(r["item_id"]): r
                for r in row_fn(
                    conn, where=f"i.id IN ({_marks(page_ids)})", params=page_ids
                )
            }
            # Each row builder orders its own query, which is not the order
            # the page was chosen in - and for contacts never was. The page
            # order is the one the user asked for, so it is the one that wins.
            rows = [built[i] for i in page_ids if i in built]

    qualifiers = qualifiers_for(
        conn,
        kinds=[showing] if showing else None,
        source_ids=[source_id] if source_id else [],
        period_start=period_start,
        period_end=period_end,
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
        # What the table is *actually* ordered by, not what was asked for: a
        # heading cannot be left drawing an arrow over a sort that was
        # dropped because this kind has no such column.
        "sort": {
            "column": sort_column,
            "direction": "desc" if descending else "asc",
            "sortable": sortable,
        },
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
    from ..normalize.attachments import BlobStore, safe_suffix

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

    # safe_suffix, not Path(...).suffix, because that is the rule the blob was
    # written under. Computing it differently here looked for a file that was
    # never created, found the real one, and then served the name it had made
    # up - so an attachment that was present downloaded as a server error.
    store = BlobStore(settings.blobs_path)
    path = store.locate(row["content_hash"], safe_suffix(row["filename"]))

    if path is None:
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


#: A date the filter boxes will accept: a year, a year and month, or a full
#: date. The same shape the screen enforces before it sends one, so the two
#: cannot disagree about what a date is. See dateInput() in screens/search.js.
_BOUND = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")


class BadDate(ValueError):
    """A date filter Recall cannot read. Carries the sentence to show."""


def _bound(value: str) -> str:
    """The filter value, checked. Raises BadDate with wording for the user.

    Without this the helpers below reached int() on whatever arrived and the
    search answered with a stack trace. The typed-in boxes were already guarded;
    the URL was not, and #/search?to=20x3 is a link somebody can bookmark.
    """
    value = value.strip()
    if not _BOUND.match(value):
        raise BadDate(
            f'"{value}" is not a date Recall can read. '
            "Use a year, a year and month, or a full date."
        )
    return value


def _start_of(value: str) -> str:
    """'2003' or '2003-04' or '2003-04-14' to an inclusive lower bound."""
    value = _bound(value)
    if len(value) == 4:
        return f"{value}-01-01T00:00:00Z"
    if len(value) == 7:
        return f"{value}-01T00:00:00Z"
    return f"{value}T00:00:00Z"


def _after(value: str) -> str:
    """An exclusive upper bound, so 'to 2003' includes all of 2003."""
    value = _bound(value)
    if len(value) == 4:
        return f"{int(value) + 1:04d}-01-01T00:00:00Z"
    if len(value) == 7:
        year, month = int(value[:4]), int(value[5:7])
        if month >= 12:
            return f"{year + 1:04d}-01-01T00:00:00Z"
        return f"{year:04d}-{month + 1:02d}-01T00:00:00Z"
    from datetime import datetime, timedelta

    try:
        day = datetime.strptime(value, "%Y-%m-%d") + timedelta(days=1)
        return day.strftime("%Y-%m-%dT00:00:00Z")
    except ValueError:
        # A date that matched the shape but is not a real day, like 2003-02-31.
        return f"{value}T23:59:59Z"


def month_bound(value: str | None, *, end: bool) -> str | None:
    """A filter value as the 'YYYY-MM' a coverage period is measured in.

    Taking value[:7] looks like it does this and does not: a bare year - which
    the filter box invites, and its placeholder spells out - stays "2003". A
    period end of "2003" then reads as the month before February and excludes
    every gap in the year the user asked about, so the total beside it stops
    admitting what is missing. That is the one thing a count here must do.
    """
    if not value:
        return None
    value = _bound(value)
    if len(value) == 4:
        return f"{value}-12" if end else f"{value}-01"
    return value[:7]
