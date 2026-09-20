"""Exporting whatever is on screen: a search result set, or a filter.

Spec section 10: "Export - from any search result set or filter: CSV, XLSX,
Markdown (one file per item or one combined chronological document), and JSON.
Attachments optionally copied out to a dated folder."

The integrity statement for a selection is narrower and more useful than the
one for the whole archive: it names what was uncertain or missing *in that
exported set*, which for a search over 2001 means the 2001 gap rather than
every gap in forty years.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from ..db import IN_IDS, ids_param
from ..logging_setup import get_logger
from .base import ExportError, ExportSelection
from .rows import (
    CALENDAR_COLUMNS,
    CALENDAR_EXTRA_COLUMNS,
    CONTACT_COLUMNS,
    MESSAGE_COLUMNS,
    NOTE_COLUMNS,
    TASK_COLUMNS,
    calendar_rows,
    contact_rows,
    message_rows,
    note_rows,
    task_rows,
)

log = get_logger("export.selection")

#: Every kind an item can be. Tasks and notes were missing here for a long
#: while, so they counted towards the total on screen and then quietly failed
#: to appear in the file - exactly the kind of silent omission spec 9.6 calls a
#: defect rather than a nicety.
_ROWS_FOR_KIND = {
    "event": (calendar_rows, CALENDAR_COLUMNS + CALENDAR_EXTRA_COLUMNS),
    "message": (message_rows, MESSAGE_COLUMNS),
    "contact": (contact_rows, CONTACT_COLUMNS),
    "task": (task_rows, TASK_COLUMNS),
    "note": (note_rows, NOTE_COLUMNS),
}

#: What each kind is called on a sheet tab and in a filename.
KIND_SHEET_NAMES = {
    "message": "Messages",
    "event": "Calendar",
    "contact": "Contacts",
    "task": "Tasks",
    "note": "Notes",
}


def export_search(
    conn,
    settings,
    *,
    fmt: str,
    query: str = "",
    kind: str | None = None,
    person_id: int | None = None,
    source_id: int | None = None,
    folder_id: int | None = None,
    tag: str | None = None,
    has_attachments: bool | None = None,
    undated: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
    out_path: str | Path | None = None,
    copy_attachments: bool = False,
    limit: int = 100_000,
) -> dict[str, Any]:
    """Export exactly what a search returned, with its own integrity statement."""
    from ..api.search import BadDate, month_bound
    from . import exporter_for

    # The download link carries the same date filters the search screen does,
    # so it can be handed the same unreadable one. Both bounds are checked
    # here, before any work, because "undated" skips them further down and an
    # unreadable date would otherwise surface much later as a crash.
    # ExportError is what the endpoint above already turns into a message.
    try:
        period_start = month_bound(date_from, end=False)
        period_end = month_bound(date_to, end=True)
        item_ids = _matching_ids(
            conn,
            query=query, kind=kind, person_id=person_id, source_id=source_id,
            folder_id=folder_id, tag=tag, has_attachments=has_attachments,
            undated=undated, date_from=date_from, date_to=date_to, limit=limit,
        )
    except BadDate as exc:
        raise ExportError(str(exc)) from exc

    if not item_ids:
        raise ExportError(
            "That search matched nothing, so there is nothing to save. "
            "Change the search or the filters and try again."
        )

    # The whole list as one parameter rather than one per record. A real
    # archive matches more records than SQLite will take parameters for, and
    # this is the road the Download button takes. See recall.db.IN_IDS.
    matched = ids_param(item_ids)

    kinds_present = [
        r["kind"]
        for r in conn.execute(
            f"SELECT DISTINCT kind FROM items WHERE id {IN_IDS}", (matched,)
        )
    ]

    # One file per kind, because a calendar entry and a contact do not share a
    # set of columns and forcing them into one table would be mostly empty
    # cells. Excel is the exception: a workbook holds a sheet per kind, so the
    # user gets one file to open and the integrity statement cannot be
    # separated from the data it describes.
    written: list[dict[str, Any]] = []
    unexportable: list[dict[str, Any]] = []
    stamp = datetime.now().strftime("%Y-%m-%d")
    base = Path(out_path) if out_path else settings.exports_path / f"recall-search-{stamp}"

    if fmt == "xlsx":
        return _combined_workbook(
            conn, settings, base, item_ids, kinds_present,
            query=query, person_id=person_id, source_id=source_id,
            folder_id=folder_id, tag=tag, has_attachments=has_attachments,
            undated=undated, date_from=date_from, date_to=date_to,
            copy_attachments=copy_attachments,
        )

    for item_kind in sorted(kinds_present):
        spec = _ROWS_FOR_KIND.get(item_kind)
        if spec is None:
            # A kind nobody has written columns for. It must not simply vanish
            # between the count on screen and the rows in the file, so it is
            # counted and named in the result.
            missed = conn.execute(
                f"SELECT COUNT(*) AS n FROM items WHERE kind = ? AND id {IN_IDS}",
                (item_kind, matched),
            ).fetchone()["n"]
            log.warning("no export columns for kind %r; %d record(s) reported "
                        "rather than dropped", item_kind, missed)
            unexportable.append({"kind": item_kind, "count": int(missed)})
            continue
        row_fn, columns = spec

        ids_of_kind = [
            int(r["id"])
            for r in conn.execute(
                f"SELECT id FROM items WHERE kind = ? AND id {IN_IDS}",
                (item_kind, matched),
            )
        ]
        if not ids_of_kind:
            continue

        rows: Iterator[dict] = row_fn(
            conn,
            where=f"i.id {IN_IDS}",
            params=[ids_param(ids_of_kind)],
        )

        selection = ExportSelection(
            description=_describe(
                query=query, kind=item_kind, person_id=person_id,
                source_id=source_id, folder_id=folder_id, tag=tag,
                has_attachments=has_attachments, undated=undated,
                date_from=date_from, date_to=date_to, count=len(ids_of_kind),
                conn=conn,
            ),
            kinds=[item_kind],
            query=query or None,
            source_ids=[source_id] if source_id else [],
            period_start=period_start,
            period_end=period_end,
        )

        suffix = f"-{item_kind}" if len(kinds_present) > 1 else ""
        target = base.with_name(base.name + suffix)

        exporter = exporter_for(fmt, conn, settings, columns)
        data_path, statement_path, statement = exporter.write(rows, target, selection)

        written.append({
            "kind": item_kind,
            "file": str(data_path),
            "integrity_file": str(statement_path) if statement_path else None,
            "count": statement.exported_count,
            "estimated_missing": statement.estimated_missing,
            "is_complete": statement.is_clean,
        })

    result: dict[str, Any] = {
        "files": written,
        "folder": str(base.parent),
        "total_records": sum(w["count"] for w in written),
    }
    if unexportable:
        result["left_out"] = unexportable
        result["left_out_total"] = sum(u["count"] for u in unexportable)

    if copy_attachments:
        result["attachments"] = copy_attachments_out(
            conn, settings, item_ids, base.parent / f"{base.name}-attachments"
        )

    return result


def _combined_workbook(
    conn, settings, base: Path, item_ids: list[int], kinds_present: list[str],
    *, query, person_id, source_id, folder_id, tag, has_attachments, undated,
    date_from, date_to, copy_attachments,
) -> dict[str, Any]:
    """Every kind in one workbook: Integrity first, then a sheet per kind.

    The count guarantee is the same one ``Exporter.write`` makes and matters
    more here, not less: a workbook that says it holds 12,481 records must have
    12,481 rows across its sheets, or the user is better off being told nothing
    was written.
    """
    from ..api.search import month_bound
    from ..scan.onedrive import assert_not_onedrive
    from .base import build_statement
    from .xlsx_export import write_combined_workbook

    # Only ever reached through export_search, which has already checked both
    # bounds, so neither of these can raise here.
    period_start = month_bound(date_from, end=False)
    period_end = month_bound(date_to, end=True)

    sections: list[tuple[str, list[str], list[dict]]] = []
    per_kind: list[dict[str, Any]] = []
    unexportable: list[dict[str, Any]] = []
    all_ids: list[int] = []

    for item_kind in sorted(kinds_present, key=_sheet_order):
        ids_of_kind = [
            int(r["id"])
            for r in conn.execute(
                f"SELECT id FROM items WHERE kind = ? AND id {IN_IDS}",
                (item_kind, ids_param(item_ids)),
            )
        ]
        if not ids_of_kind:
            continue

        spec = _ROWS_FOR_KIND.get(item_kind)
        if spec is None:
            log.warning("no export columns for kind %r; %d record(s) reported "
                        "rather than dropped", item_kind, len(ids_of_kind))
            unexportable.append({"kind": item_kind, "count": len(ids_of_kind)})
            continue

        row_fn, columns = spec
        rows = list(row_fn(
            conn, where=f"i.id {IN_IDS}", params=[ids_param(ids_of_kind)]
        ))
        sections.append((KIND_SHEET_NAMES.get(item_kind, item_kind), columns, rows))
        all_ids.extend(int(r["id"]) for r in rows if r.get("id") is not None)
        per_kind.append({
            "kind": item_kind,
            "sheet": KIND_SHEET_NAMES.get(item_kind, item_kind),
            "count": len(rows),
        })

    if not sections:
        raise ExportError(
            "Nothing in that result can be written to a spreadsheet yet. "
            "The Problems screen says what Recall found instead."
        )

    selection = ExportSelection(
        description=_describe(
            query=query, kind=None, person_id=person_id, source_id=source_id,
            folder_id=folder_id, tag=tag, has_attachments=has_attachments,
            undated=undated, date_from=date_from, date_to=date_to,
            count=len(all_ids), conn=conn,
        ),
        kinds=[k["kind"] for k in per_kind],
        query=query or None,
        source_ids=[source_id] if source_id else [],
        period_start=period_start,
        period_end=period_end,
        item_ids=all_ids,
    )

    target = base.with_suffix(".xlsx")
    target.parent.mkdir(parents=True, exist_ok=True)
    assert_not_onedrive(target.parent)

    statement = build_statement(conn, selection, len(all_ids), settings.db_path)
    total = write_combined_workbook(sections, target, statement)

    if total != len(all_ids):
        raise ExportError(
            f"The export wrote {total:,} rows but {len(all_ids):,} were "
            "selected. Rather than hand you a file whose count cannot be "
            "trusted, Recall has stopped."
        )

    result: dict[str, Any] = {
        "files": [{
            "kind": "all",
            "file": str(target),
            "integrity_file": None,       # the Integrity sheet is inside it
            "count": statement.exported_count,
            "estimated_missing": statement.estimated_missing,
            "is_complete": statement.is_clean,
            "sheets": per_kind,
        }],
        "folder": str(target.parent),
        "total_records": statement.exported_count,
    }
    if unexportable:
        result["left_out"] = unexportable
        result["left_out_total"] = sum(u["count"] for u in unexportable)

    if copy_attachments:
        result["attachments"] = copy_attachments_out(
            conn, settings, item_ids, target.parent / f"{target.stem}-attachments"
        )

    log.info("Exported %d records to %s", total, target)
    return result


def _sheet_order(kind: str) -> int:
    """Sheets in the order somebody would look for them, not alphabetical."""
    order = ["message", "event", "contact", "task", "note"]
    return order.index(kind) if kind in order else len(order)


def _matching_ids(conn, *, query, kind, person_id, source_id, folder_id, tag,
                  has_attachments, undated, date_from, date_to, limit) -> list[int]:
    """The ids a search matched, in the order the user saw them."""
    from ..api.search import _after, _start_of
    from ..search.query import safe_query

    parsed = safe_query(conn, query or "")

    where: list[str] = []
    params: list[Any] = []
    joins = ""

    if parsed.fts:
        joins = "JOIN items_fts f ON f.rowid = i.id"
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
            "EXISTS (SELECT 1 FROM participations p WHERE p.item_id = i.id AND p.person_id = ?)"
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
    return [
        int(r["id"])
        for r in conn.execute(
            f"SELECT i.id FROM items i {joins} {where_sql} "
            f"ORDER BY i.occurred_utc IS NULL, i.occurred_utc LIMIT ?",
            (*params, limit),
        )
    ]


def _describe(*, query, kind, person_id, source_id, folder_id, tag,
              has_attachments, undated, date_from, date_to, count, conn) -> str:
    """What this export covers, in words, for the integrity statement."""
    kind_word = {
        "event": "calendar entries", "message": "messages", "contact": "contacts",
        "task": "tasks", "note": "notes",
        # No kind at all means a workbook holding every kind at once.
        None: "records",
    }.get(kind, kind)

    parts = [f"{count:,} {kind_word}"]

    if query:
        parts.append(f'matching the search "{query}"')
    if person_id:
        row = conn.execute(
            "SELECT display_name FROM people WHERE id = ?", (person_id,)
        ).fetchone()
        parts.append(f"involving {row['display_name'] if row else f'person {person_id}'}")
    if source_id:
        row = conn.execute(
            "SELECT path FROM source_files WHERE id = ?", (source_id,)
        ).fetchone()
        parts.append(f"from the file {row['path'] if row else source_id}")
    if folder_id:
        row = conn.execute("SELECT path FROM folders WHERE id = ?", (folder_id,)).fetchone()
        parts.append(f"in the folder {row['path'] if row else folder_id}")
    if tag:
        parts.append(f"in the category {tag}")
    if has_attachments:
        parts.append("with attachments")
    if undated:
        parts.append("that have no date at all")
    elif date_from or date_to:
        parts.append(f"from {date_from or 'the beginning'} to {date_to or 'the end'}")

    return " ".join(parts)


def copy_attachments_out(conn, settings, item_ids: list[int], target: Path) -> dict:
    """Copy the attachments of an exported set into a dated folder.

    Filenames are made safe and de-collided: two different invoices both called
    "invoice.pdf" must both arrive, so the second becomes "invoice (2).pdf".
    """
    from ..normalize.attachments import BlobStore, safe_suffix
    from ..scan.onedrive import assert_not_onedrive

    target = Path(target)
    assert_not_onedrive(target.parent)
    target.mkdir(parents=True, exist_ok=True)

    store = BlobStore(settings.blobs_path)
    rows = conn.execute(
        f"SELECT a.id, a.filename, a.content_hash, a.is_inline, i.subject, "
        f"i.occurred_utc FROM attachments a JOIN items i ON i.id = a.item_id "
        f"WHERE a.item_id {IN_IDS} AND a.content_hash IS NOT NULL",
        (ids_param(item_ids),),
    ).fetchall()

    copied = 0
    missing = 0
    used: set[str] = set()
    manifest: list[str] = [
        "Attachments copied out of the Recall archive",
        f"Copied on {datetime.now():%Y-%m-%d %H:%M}",
        "",
        "file name -> the message it came from",
        "",
    ]

    for row in rows:
        data = store.get(row["content_hash"], safe_suffix(row["filename"]))
        if data is None:
            missing += 1
            manifest.append(
                f"MISSING: {row['filename']} (from: {row['subject'] or 'no subject'})"
            )
            continue

        name = _safe_name(row["filename"] or f"attachment-{row['id']}")
        name = _unique(name, used)
        used.add(name.lower())

        (target / name).write_bytes(data)
        copied += 1
        manifest.append(
            f"{name}  ->  {row['subject'] or '(no subject)'}"
            f"  ({row['occurred_utc'][:10] if row['occurred_utc'] else 'no date'})"
        )

    if missing:
        manifest.insert(
            2,
            f"WARNING: {missing} attachment(s) are listed in the archive but their "
            "saved copies are missing, so they could not be copied. They are "
            "marked MISSING below.",
        )

    (target / "_where these came from.txt").write_text(
        "\n".join(manifest), encoding="utf-8"
    )

    log.info("Copied %d attachment(s) to %s (%d missing)", copied, target, missing)
    return {
        "folder": str(target),
        "copied": copied,
        "missing": missing,
        "manifest": str(target / "_where these came from.txt"),
    }


def _safe_name(name: str) -> str:
    """A filename Windows will accept, keeping as much of the original as it can."""
    import re

    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name))
    # Runs of dots mean nothing in a filename and read as a path fragment even
    # once the separators are gone.
    cleaned = re.sub(r"\.{2,}", ".", cleaned).strip(". _")
    if not cleaned:
        cleaned = "attachment"
    # Windows reserves these, with or without an extension.
    stem = cleaned.split(".")[0].upper()
    if stem in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        cleaned = "_" + cleaned
    return cleaned[:180]


def _unique(name: str, used: set[str]) -> str:
    if name.lower() not in used:
        return name
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        stem, suffix = name, ""
    for n in range(2, 10_000):
        candidate = f"{stem} ({n}){dot}{suffix}" if dot else f"{stem} ({n})"
        if candidate.lower() not in used:
            return candidate
    return f"{stem}-{len(used)}{dot}{suffix}"


def _marks(values) -> str:
    return ",".join("?" * len(values))
