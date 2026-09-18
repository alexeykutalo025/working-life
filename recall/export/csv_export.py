"""CSV, written so it opens cleanly in Excel with no further work.

Three details decide whether a CSV is usable in Excel, and all three are easy
to get wrong:

* **The BOM.** Without ``utf-8-sig`` Excel on Windows opens a UTF-8 CSV as
  cp1252 and every accented name in the archive turns to mojibake - the exact
  damage this program exists to undo.
* **Line endings.** ``newline=""`` plus ``\\r\\n`` from the csv module. Anything
  else puts stray blank rows between records.
* **Formula injection.** A cell starting with ``=``, ``+``, ``-`` or ``@`` is
  executed by Excel as a formula. Real mail contains subjects like
  "=?utf-8?B?..." and signatures beginning with "--". Those cells are prefixed
  with an apostrophe so they display literally, which is safe and keeps the
  text readable.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterator

from .base import Exporter, IntegrityStatement

#: Excel treats a leading one of these as the start of a formula.
_FORMULA_STARTERS = ("=", "+", "-", "@", "\t", "\r")


class CsvExporter(Exporter):
    suffix = ".csv"
    format_name = "CSV (opens in Excel)"

    def __init__(self, conn, settings, columns: list[str]) -> None:
        super().__init__(conn, settings)
        self.columns = columns

    def _write_data(
        self, rows: Iterator[dict], out_path: Path, statement: IntegrityStatement
    ) -> int:
        written = 0
        with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=self.columns,
                extrasaction="ignore",
                quoting=csv.QUOTE_MINIMAL,
                lineterminator="\r\n",
            )
            writer.writeheader()
            for row in rows:
                writer.writerow({c: _safe(row.get(c, "")) for c in self.columns})
                written += 1
        return written


def _safe(value) -> str:
    """One cell, made safe for Excel without losing what it says."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value)
    if text.startswith(_FORMULA_STARTERS):
        # The apostrophe is Excel's own "treat this as text" marker. It is not
        # shown in a cell, and other spreadsheets ignore it too.
        return "'" + text
    return text
