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

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from ..logging_setup import get_logger
from .base import ExportError, ExportSelection
from .rows import (
    CALENDAR_COLUMNS,
    CALENDAR_EXTRA_COLUMNS,
    CONTACT_COLUMNS,
    MESSAGE_COLUMNS,
    calendar_rows,
    contact_rows,
    message_rows,
)

log = get_logger("export.selection")

_ROWS_FOR_KIND = {
    "event": (calendar_rows, CALENDAR_COLUMNS + CALENDAR_EXTRA_COLUMNS),
    "message": (message_rows, MESSAGE_COLUMNS),
    "contact": (contact_rows, CONTACT_COLUMNS),
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
    from ..search.query import safe_query
    from . import exporter_for

    item_ids = _matching_ids(
        conn,
        query=query, kind=kind, person_id=person_id, source_id=source_id,
        folder_id=folder_id, tag=tag, has_attachments=has_attachments,
        undated=undated, date_from=date_from, date_to=date_to, limit=limit,
    )

    if not item_ids:
        raise ExportError(
            "That search matched nothing, so there is nothing to save. "
            "Change the search or the filters and try again."
        )

    kinds_present = [
        r["kind"]
        for r in conn.execute(
            f"SELECT DISTINCT kind FROM items WHERE id IN ({_marks(item_ids)})",
            item_ids,
        )
    ]

    # One export per kind, because a calendar entry and a contact do not share
    # a set of columns and forcing them into one would give a table that is
    # mostly empty cells.
    written: list[dict[str, Any]] = []
    stamp = datetime.now().strftime("%Y-%m-%d")
    base = Path(out_path) if out_path else settings.exports_path / f"recall-search-{stamp}"

    for item_kind in sorted(kinds_present):
        spec = _ROWS_FOR_KIND.get(item_kind)
        if spec is None:
            continue
        row_fn, columns = spec

        ids_of_kind = [
            int(r["id"])
            for r in conn.execute(
                f"SELECT id FROM items WHERE kind = ? AND id IN ({_marks(item_ids)})",
                (item_kind, *item_ids),
            )
        ]
        if not ids_of_kind:
            continue

        rows: Iterator[dict] = row_fn(
            conn,
            where=f"i.id IN ({_marks(ids_of_kind)})",
            params=ids_of_kind,
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
            period_start=date_from[:7] if date_from else None,
            period_end=date_to[:7] if date_to else None,
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

    if copy_attachments:
        result["attachments"] = copy_attachments_out(
            conn, settings, item_ids, base.parent / f"{base.name}-attachments"
        )

    return result


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
    from ..normalize.attachments import BlobStore
    from ..scan.onedrive import assert_not_onedrive

    target = Path(target)
    assert_not_onedrive(target.parent)
    target.mkdir(parents=True, exist_ok=True)

    store = BlobStore(settings.blobs_path)
    rows = conn.execute(
        f"SELECT a.id, a.filename, a.content_hash, a.is_inline, i.subject, "
        f"i.occurred_utc FROM attachments a JOIN items i ON i.id = a.item_id "
        f"WHERE a.item_id IN ({_marks(item_ids)}) AND a.content_hash IS NOT NULL",
        item_ids,
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
        suffix = Path(str(row["filename"] or "")).suffix.lower()
        data = store.get(row["content_hash"], suffix)
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
