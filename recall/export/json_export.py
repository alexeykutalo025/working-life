"""JSON, for another program to read.

The integrity statement is a top-level ``findings`` key as the spec requires,
and it comes before the records so a streaming reader meets it first. There is
no way to get the records without also getting the key that says what is
missing from them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from .base import Exporter, IntegrityStatement


class JsonExporter(Exporter):
    suffix = ".json"
    format_name = "JSON"
    statement_is_embedded = True

    def __init__(self, conn, settings, columns: list[str]) -> None:
        super().__init__(conn, settings)
        self.columns = columns

    def _write_data(
        self, rows: Iterator[dict], out_path: Path, statement: IntegrityStatement
    ) -> int:
        materialized = [
            {c: _value(row.get(c)) for c in self.columns} for row in rows
        ]

        document = {
            "format": "recall-export",
            "format_version": 1,
            "generated_utc": statement.generated_utc,
            # First, and named exactly as section 9.6 requires. A consumer that
            # reads "records" without reading this has chosen to.
            "findings": statement.as_dict(),
            "record_count": len(materialized),
            "columns": self.columns,
            "records": materialized,
        }

        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(document, fh, indent=2, ensure_ascii=False, default=str)
            fh.write("\n")

        return len(materialized)


def _value(value):
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
