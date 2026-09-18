"""Excel workbooks, with the integrity statement as its own sheet.

The statement is a sheet rather than a separate file because a workbook gets
emailed as one attachment, and a companion .txt would be left behind. Spec
section 9.6 asks for "a dedicated Integrity sheet for XLSX", and it is the
first sheet, so nobody opens the workbook without seeing it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from .base import ExportError, Exporter, IntegrityStatement


class XlsxExporter(Exporter):
    suffix = ".xlsx"
    format_name = "Excel workbook"
    statement_is_embedded = True

    def __init__(self, conn, settings, columns: list[str]) -> None:
        super().__init__(conn, settings)
        self.columns = columns

    def _write_data(
        self, rows: Iterator[dict], out_path: Path, statement: IntegrityStatement
    ) -> int:
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Alignment, Font, PatternFill
            from openpyxl.utils import get_column_letter
        except ImportError as exc:
            raise ExportError(
                "Excel workbooks cannot be written because openpyxl is not "
                "installed. Install it with:  pip install openpyxl\n"
                "Saving as CSV works without it and opens in Excel just as well."
            ) from exc

        workbook = Workbook()

        # --- Integrity, first so it cannot be missed ---------------------
        integrity = workbook.active
        integrity.title = "Integrity"
        self._write_integrity_sheet(integrity, statement, Font, PatternFill, Alignment)

        # --- the data -----------------------------------------------------
        sheet = workbook.create_sheet("Records")
        sheet.append([_heading(c) for c in self.columns])

        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill("solid", fgColor="1F3864")
        for cell in sheet[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        sheet.freeze_panes = "A2"

        written = 0
        for row in rows:
            sheet.append([_cell(row.get(c, "")) for c in self.columns])
            written += 1

        if written:
            sheet.auto_filter.ref = (
                f"A1:{get_column_letter(len(self.columns))}{written + 1}"
            )

        for i, column in enumerate(self.columns, start=1):
            width = {
                "subject": 46, "location": 28, "attendees": 52, "notes": 60,
                "source_file": 46, "body_preview": 60, "data_quality": 44,
                "recurrence_rule": 32, "emails": 36, "to": 40, "cc": 40,
            }.get(column, max(12, min(26, len(_heading(column)) + 4)))
            sheet.column_dimensions[get_column_letter(i)].width = width

        workbook.save(out_path)
        return written

    def _write_integrity_sheet(self, sheet, statement, Font, PatternFill, Alignment) -> None:
        title_font = Font(bold=True, size=14)
        label_font = Font(bold=True)
        warn_fill = PatternFill("solid", fgColor="FBE9EA")
        good_fill = PatternFill("solid", fgColor="E3F1E7")

        sheet.column_dimensions["A"].width = 46
        sheet.column_dimensions["B"].width = 96

        sheet["A1"] = "What this export does and does not contain"
        sheet["A1"].font = title_font
        sheet["A2"] = (
            "Read this before relying on the numbers in the Records sheet."
        )
        sheet["A2"].font = Font(italic=True)

        row = 4
        for label, value in statement.rows():
            sheet.cell(row=row, column=1, value=label).font = label_font
            cell = sheet.cell(row=row, column=2, value=value)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            if label.startswith("Problem") or "could NOT" in label or "NO -" in str(value):
                cell.fill = warn_fill
                sheet.cell(row=row, column=1).fill = warn_fill
            row += 1

        row += 1
        if statement.is_clean:
            sheet.cell(row=row, column=1, value="No known problems").font = label_font
            cell = sheet.cell(
                row=row,
                column=2,
                value=(
                    "Recall found nothing wrong with the records in this export. "
                    "Every one has a date, and every file they came from was read "
                    "in full. That is a statement about what Recall could check - "
                    "it cannot know about a mailbox that was never on this computer."
                ),
            )
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            cell.fill = good_fill
            row += 2

        if statement.findings:
            sheet.cell(row=row, column=1, value="Every known problem in detail").font = title_font
            row += 1
            for f in statement.findings:
                sheet.cell(row=row, column=1, value=f"{f['severity']}: {f['code']}").font = label_font
                detail = f["title"]
                if f.get("estimated_loss"):
                    detail += f"\nRecords believed unreadable: {f['estimated_loss']:,}"
                if f.get("detail"):
                    detail += "\n" + f["detail"]
                cell = sheet.cell(row=row, column=2, value=detail)
                cell.alignment = Alignment(wrap_text=True, vertical="top")
                row += 1

        row += 1
        sheet.cell(
            row=row,
            column=1,
            value=(
                "Recall never invents a date, a timezone, a sender or a total. "
                "Where something is missing, it says so - here."
            ),
        ).font = Font(italic=True)


def _heading(column: str) -> str:
    """Column names a person reads, not database names."""
    return {
        "duration_min": "duration (minutes)",
        "attendee_count": "how many attendees",
        "source_file": "found in file",
        "timezone_known": "timezone known?",
        "recurrence_rule": "repeat rule",
        "data_quality": "anything uncertain about this record",
        "body_preview": "first part of the message",
        "item_id": "record number",
        "all_day": "all day?",
        "has_attachments": "attachments?",
        "from_name": "from (name)",
        "from_address": "from (address)",
        "attachment_names": "attachment names",
        "phone_business": "phone (work)",
        "phone_home": "phone (home)",
        "phone_mobile": "phone (mobile)",
    }.get(column, column.replace("_", " "))


def _cell(value):
    """Excel accepts strings and numbers; everything else becomes a string.

    Numeric-looking text is passed through as a number so it sorts and sums
    correctly, which is half the reason to export to Excel at all.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return value
    text = str(value)
    if text.isdigit() and len(text) < 15:
        return int(text)
    return text
