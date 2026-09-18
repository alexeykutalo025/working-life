"""Exports, each with its integrity statement.

One entry point, ``run_export``, so the CLI and the web page cannot drift apart
about what an export contains.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from ..logging_setup import get_logger
from .base import ExportError, ExportSelection, Exporter, IntegrityStatement, build_statement
from .rows import (
    CALENDAR_COLUMNS,
    CALENDAR_EXTRA_COLUMNS,
    CONTACT_COLUMNS,
    MESSAGE_COLUMNS,
    calendar_rows,
    contact_rows,
    message_rows,
)

log = get_logger("export")

#: What each kind of export contains, and which columns it writes.
KIND_SPECS: dict[str, dict[str, Any]] = {
    "calendar": {
        "rows": calendar_rows,
        "columns": CALENDAR_COLUMNS,
        "full_columns": CALENDAR_COLUMNS + CALENDAR_EXTRA_COLUMNS,
        "item_kind": "event",
        "description": "every calendar entry in the archive",
    },
    "mail": {
        "rows": message_rows,
        "columns": MESSAGE_COLUMNS,
        "full_columns": MESSAGE_COLUMNS,
        "item_kind": "message",
        "description": "every message in the archive",
    },
    "contacts": {
        "rows": contact_rows,
        "columns": CONTACT_COLUMNS,
        "full_columns": CONTACT_COLUMNS,
        "item_kind": "contact",
        "description": "every contact in the archive",
    },
}

FORMATS = ("csv", "xlsx", "markdown", "json")


def exporter_for(fmt: str, conn, settings, columns: list[str]) -> Exporter:
    fmt = fmt.lower().strip()
    if fmt == "csv":
        from .csv_export import CsvExporter

        return CsvExporter(conn, settings, columns)
    if fmt == "xlsx":
        from .xlsx_export import XlsxExporter

        return XlsxExporter(conn, settings, columns)
    if fmt in ("markdown", "md"):
        from .markdown_export import MarkdownExporter

        return MarkdownExporter(conn, settings, columns)
    if fmt == "json":
        from .json_export import JsonExporter

        return JsonExporter(conn, settings, columns)
    raise ExportError(
        f"{fmt!r} is not a format Recall can write. Choose one of: "
        + ", ".join(FORMATS)
    )


def run_export(
    conn,
    settings,
    *,
    fmt: str,
    kind: str = "calendar",
    out_path: str | Path | None = None,
    full: bool = True,
    where: str = "",
    params: list | None = None,
    description: str | None = None,
    query: str | None = None,
) -> tuple[Path, Path | None, IntegrityStatement]:
    """Write one export and its integrity statement.

    ``full`` writes the extra columns as well as the ones the spec fixes. The
    spec's columns always come first and in order, so a file opened in Excel
    looks the same whether or not the extras are there.
    """
    if kind not in KIND_SPECS:
        raise ExportError(
            f"{kind!r} is not something Recall can export. Choose one of: "
            + ", ".join(sorted(KIND_SPECS))
        )

    spec = KIND_SPECS[kind]
    columns = spec["full_columns"] if full else spec["columns"]

    if out_path is None:
        stamp = datetime.now().strftime("%Y-%m-%d")
        out_path = settings.exports_path / f"recall-{kind}-{stamp}"

    rows: Iterator[dict] = spec["rows"](conn, where=where, params=params)

    selection = ExportSelection(
        description=description or spec["description"],
        kinds=[spec["item_kind"]],
        query=query,
    )

    exporter = exporter_for(fmt, conn, settings, columns)
    return exporter.write(rows, out_path, selection)


__all__ = [
    "CALENDAR_COLUMNS",
    "ExportError",
    "ExportSelection",
    "Exporter",
    "FORMATS",
    "IntegrityStatement",
    "KIND_SPECS",
    "build_statement",
    "exporter_for",
    "run_export",
]
