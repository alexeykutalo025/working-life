"""Excel workbooks, built to be analysed as well as read.

The statement is a sheet rather than a separate file because a workbook gets
emailed as one attachment, and a companion .txt would be left behind. Spec
section 9.6 asks for "a dedicated Integrity sheet for XLSX", and it is the
first sheet, so nobody opens the workbook without seeing it.

Everything here follows from one rule: **the data sheets are strict rectangles
and every word of explanation lives somewhere else.** Row 1 is the headings,
row 2 onward is data, and nothing - no banner, no merged cell, no blank spacer,
no totals row - goes above or between. That is what lets the same file be read
by a person in Excel and loaded by a program without either of them having to
guess. The explanation has three sheets of its own, in front of the data where
it will be seen:

    Integrity   what this export does and does not contain (spec 9.6)
    Problems    every known problem, as a table rather than as prose
    Read me     what each column of each sheet holds

The one thing this file will not do is make a value up. A date that does not
parse stays the text it arrived as; an empty cell stays empty.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

from .base import ExportError, Exporter, IntegrityStatement

#: Sheets that explain the file rather than hold records. The count guarantee
#: and anything else counting rows has to know which sheets are which.
REFERENCE_SHEETS = ("Integrity", "Problems", "Read me")

_HEADER_FILL = "1F3864"
_TABLE_STYLE = "TableStyleMedium2"


def _openpyxl() -> SimpleNamespace:
    """openpyxl, with one plain-language failure rather than a traceback."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
        from openpyxl.worksheet.table import Table, TableStyleInfo
    except ImportError as exc:
        raise ExportError(
            "Excel workbooks cannot be written because openpyxl is not "
            "installed. Install it with:  pip install openpyxl\n"
            "Saving as CSV works without it and opens in Excel just as well."
        ) from exc

    return SimpleNamespace(
        Workbook=Workbook, Alignment=Alignment, Font=Font,
        PatternFill=PatternFill, get_column_letter=get_column_letter,
        Table=Table, TableStyleInfo=TableStyleInfo,
    )


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
        xl = _openpyxl()
        workbook = xl.Workbook()

        _write_front_matter(workbook, statement, [("Records", self.columns)], xl)
        written = _write_records_sheet(
            workbook.create_sheet("Records"), self.columns, rows, xl
        )

        workbook.save(out_path)
        return written


def write_combined_workbook(sections, out_path: Path, statement) -> int:
    """One workbook holding every kind of record, Integrity first.

    Messages, calendar entries, contacts, tasks and notes do not share a set of
    columns, so each gets its own sheet rather than one table of mostly empty
    cells. One file is what somebody actually wants to be handed: it opens, it
    emails, and the Integrity sheet cannot be separated from the data it
    describes.

    ``sections`` is an iterable of (sheet name, columns, rows).
    """
    xl = _openpyxl()
    workbook = xl.Workbook()

    sections = [(_sheet_name(name), columns, rows) for name, columns, rows in sections]
    _write_front_matter(
        workbook, statement, [(name, cols) for name, cols, _ in sections], xl
    )

    total = 0
    for name, columns, rows in sections:
        total += _write_records_sheet(workbook.create_sheet(name), columns, rows, xl)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(out_path)
    return total


def _write_front_matter(workbook, statement, layout, xl) -> None:
    """The three explaining sheets, in the order they want reading.

    ``layout`` is (sheet name, columns) for each data sheet that will follow.
    """
    integrity = workbook.active
    integrity.title = "Integrity"
    _write_integrity_sheet(integrity, statement, xl)
    _write_problems_sheet(workbook.create_sheet("Problems"), statement, xl)
    _write_readme_sheet(workbook.create_sheet("Read me"), layout, statement, xl)


# ---------------------------------------------------------------------------
# The data sheets
# ---------------------------------------------------------------------------

#: Columns that hold a date, and are written as one rather than as text. Every
#: date in the archive reaches here as "YYYY-MM-DD"; as text it sorts by
#: accident and groups by nothing, which is most of what a spreadsheet is for.
_DATE_COLUMNS = frozenset({"date", "due_date", "first_seen", "last_seen"})

#: Columns that hold a whole number. Same reason: they are here to be summed.
_NUMBER_COLUMNS = frozenset({
    "duration_min", "attendee_count", "item_count", "item_id",
})

#: Columns whose text runs long enough to need wrapping. _cell keeps newlines,
#: and without this they render as one mangled line.
_WRAPPED_COLUMNS = frozenset({
    "notes", "body", "body_preview", "data_quality", "attendees", "responses",
})

#: Tall enough for about three lines. Left to itself a wrapped cell makes the
#: row as tall as its longest value, and one forty-line mail body would push
#: every other record off the screen.
_WRAPPED_ROW_HEIGHT = 46


def _write_records_sheet(sheet, columns, rows, xl) -> int:
    """One table of records: headings on row 1, records from row 2, nothing else."""
    sheet.append(_headings(columns))

    header_font = xl.Font(bold=True, color="FFFFFF")
    header_fill = xl.PatternFill("solid", fgColor=_HEADER_FILL)
    for cell in sheet[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = xl.Alignment(vertical="center", wrap_text=True)

    # B2, not A2: the first column is the date or the name, and it is the one
    # you need still on screen when you scroll right across twenty-two columns.
    sheet.freeze_panes = "B2" if len(columns) > 1 else "A2"

    wrap = xl.Alignment(wrap_text=True, vertical="top")
    top = xl.Alignment(vertical="top")

    # Worked out once rather than per row: a fifty-year archive is a lot of
    # rows to look a column name up in.
    wrapped = [i for i, c in enumerate(columns, start=1) if c in _WRAPPED_COLUMNS]
    formatted = [
        (i, "yyyy-mm-dd" if c in _DATE_COLUMNS else ("0" if c == "item_id" else "#,##0"))
        for i, c in enumerate(columns, start=1)
        if c in _DATE_COLUMNS or c in _NUMBER_COLUMNS
    ]

    written = 0
    for row in rows:
        sheet.append([_typed(c, row.get(c, "")) for c in columns])
        written += 1
        line = written + 1
        for i in wrapped:
            sheet.cell(row=line, column=i).alignment = wrap
        for i, number_format in formatted:
            cell = sheet.cell(row=line, column=i)
            # A value that would not parse was left as the text it arrived as,
            # and a date format over text shows the text with a format nobody
            # asked for. Only what actually became a number gets one.
            if isinstance(cell.value, (int, float, date, datetime)):
                cell.number_format = number_format
                cell.alignment = top
        if wrapped:
            sheet.row_dimensions[line].height = _WRAPPED_ROW_HEIGHT

    _add_table(sheet, len(columns), written, xl)

    for i, column in enumerate(columns, start=1):
        sheet.column_dimensions[xl.get_column_letter(i)].width = column_width(column)

    return written


def _add_table(sheet, width: int, rows: int, xl, header_row: int = 1) -> None:
    """Make the range a real Excel table: banded rows, filters, a named range.

    A table brings its own filter, so ``sheet.auto_filter`` is deliberately
    left alone - setting both is what makes Excel offer to repair the file.

    ``header_row`` is where the headings are: row 1 on a data sheet, further
    down on the three that carry a title above their table. Get it wrong and
    the table covers the title instead, which Excel opens and then quietly
    misreads.
    """
    if width < 1:
        return
    last = xl.get_column_letter(width)
    table = xl.Table(
        displayName=_table_name(sheet.title),
        ref=f"A{header_row}:{last}{header_row + rows}",
    )
    table.tableStyleInfo = xl.TableStyleInfo(
        name=_TABLE_STYLE, showRowStripes=True, showColumnStripes=False,
    )
    sheet.add_table(table)


def _headings(columns) -> list[str]:
    """Readable headings, and no two of them the same.

    A table whose header row repeats a name is a workbook Excel will not open,
    so a collision is made visible here rather than at the client's end.
    """
    out: list[str] = []
    for column in columns:
        heading = _heading(column)
        if heading in out:
            heading = f"{heading} ({column})"
        out.append(heading)
    return out


def _table_name(sheet_title: str) -> str:
    """A table name Excel accepts: letters, digits and underscores, no spaces."""
    cleaned = re.sub(r"\W", "_", str(sheet_title)) or "Records"
    if not cleaned[0].isalpha() and cleaned[0] != "_":
        cleaned = "_" + cleaned
    return cleaned[:31]


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------


def _write_integrity_sheet(sheet, statement, xl) -> None:
    """What this export does and does not contain, as two readable columns."""
    sheet.column_dimensions["A"].width = 46
    sheet.column_dimensions["B"].width = 96

    sheet["A1"] = "What this export does and does not contain"
    sheet["A1"].font = xl.Font(bold=True, size=14)
    sheet["A2"] = "Read this before relying on the numbers in the data sheets."
    sheet["A2"].font = xl.Font(italic=True)

    _header_row(sheet, 4, ["What", "Value"], xl)

    warn_fill = xl.PatternFill("solid", fgColor="FBE9EA")
    good_fill = xl.PatternFill("solid", fgColor="E3F1E7")
    label_font = xl.Font(bold=True)
    wrap = xl.Alignment(wrap_text=True, vertical="top")

    row = 5
    for label, value in statement.rows():
        sheet.cell(row=row, column=1, value=label).font = label_font
        sheet.cell(row=row, column=1).alignment = wrap
        cell = sheet.cell(row=row, column=2, value=value)
        cell.alignment = wrap
        alarming = (
            label.startswith(("Problem", "Why"))
            or "could NOT" in label
            or "NO -" in str(value)
        )
        if alarming:
            cell.fill = warn_fill
            sheet.cell(row=row, column=1).fill = warn_fill
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
        cell.alignment = wrap
        cell.fill = good_fill
        row += 1

    _add_table(sheet, 2, row - 5, xl, header_row=4)

    row += 1
    sheet.cell(
        row=row,
        column=1,
        value=(
            "Recall never invents a date, a timezone, a sender or a total. "
            "Where something is missing, it says so - here, and on the "
            "Problems sheet."
        ),
    ).font = xl.Font(italic=True)


# ---------------------------------------------------------------------------
# Problems
# ---------------------------------------------------------------------------

_PROBLEM_COLUMNS = [
    "severity", "code", "what it is", "records affected",
    "records believed lost", "what you can do about it", "state",
]

#: Colour behind the severity cell. It sits behind the word, never instead of
#: it: spec section 10 says nothing may be carried by colour alone, and a
#: printed spreadsheet or a colour-blind reader would lose it.
_SEVERITY_FILL = {
    "critical": "F6C6C9",
    "high": "FBE0C3",
    "medium": "FCF2C6",
}


def _write_problems_sheet(sheet, statement, xl) -> None:
    """Every known problem as a table, because prose cannot be sorted.

    These used to be paragraphs in column B of the Integrity sheet, where a
    critical finding looked exactly like a note and nothing could be counted.
    """
    sheet["A1"] = "Everything Recall knows to be wrong with these records"
    sheet["A1"].font = xl.Font(bold=True, size=14)

    _header_row(sheet, 3, _PROBLEM_COLUMNS, xl)

    wrap = xl.Alignment(wrap_text=True, vertical="top")
    top = xl.Alignment(vertical="top")

    row = 4
    for finding in statement.findings:
        values = [
            finding.get("severity") or "",
            finding.get("code") or "",
            finding.get("title") or "",
            _as_int(finding.get("affected_count")),
            _as_int(finding.get("estimated_loss")),
            finding.get("detail") or "",
            finding.get("state") or "",
        ]
        for i, value in enumerate(values, start=1):
            cell = sheet.cell(row=row, column=i, value=_cell(value))
            cell.alignment = wrap if i in (3, 6) else top
            if isinstance(cell.value, int):
                cell.number_format = "#,##0"

        shade = _SEVERITY_FILL.get(str(finding.get("severity") or "").lower())
        if shade:
            sheet.cell(row=row, column=1).fill = xl.PatternFill("solid", fgColor=shade)

        sheet.row_dimensions[row].height = _WRAPPED_ROW_HEIGHT
        row += 1

    _add_table(sheet, len(_PROBLEM_COLUMNS), row - 4, xl, header_row=3)

    for letter, width in zip("ABCDEFG", (14, 26, 60, 18, 22, 76, 14)):
        sheet.column_dimensions[letter].width = width

    if not statement.findings:
        sheet.cell(
            row=row + 1,
            column=1,
            value=(
                "No problems are known about the records in this export. That "
                "is a statement about what Recall could check - it cannot know "
                "about a mailbox that was never on this computer."
            ),
        ).font = xl.Font(italic=True)


# ---------------------------------------------------------------------------
# Read me
# ---------------------------------------------------------------------------


def _write_readme_sheet(sheet, layout, statement, xl) -> None:
    """A row per column of every data sheet, saying what that column holds.

    This is the sheet that lets somebody - or something - read the data
    without guessing, which is the whole purpose of the file.
    """
    sheet["A1"] = "What is in this workbook"
    sheet["A1"].font = xl.Font(bold=True, size=14)
    sheet["A2"] = (
        "Every sheet after this one holds records: the headings are on row 1 "
        "and the records start on row 2, with nothing in between. Dates are "
        "real dates and counts are real numbers, so they sort and total "
        "properly. An empty cell means Recall did not find a value - it is "
        "never a zero standing in for one."
    )
    sheet["A2"].alignment = xl.Alignment(wrap_text=True, vertical="top")
    sheet.merge_cells("A2:C2")
    sheet.row_dimensions[2].height = 46

    _header_row(sheet, 4, ["sheet", "column", "what it holds"], xl)

    wrap = xl.Alignment(wrap_text=True, vertical="top")
    top = xl.Alignment(vertical="top")

    row = 5
    for name, columns in layout:
        for column, heading in zip(columns, _headings(columns)):
            sheet.cell(row=row, column=1, value=name).alignment = top
            sheet.cell(row=row, column=2, value=heading).alignment = top
            sheet.cell(row=row, column=3, value=column_note(column)).alignment = wrap
            sheet.row_dimensions[row].height = _WRAPPED_ROW_HEIGHT
            row += 1

    _add_table(sheet, 3, row - 5, xl, header_row=4)

    for letter, width in zip("ABC", (18, 38, 96)):
        sheet.column_dimensions[letter].width = width

    sheet.cell(
        row=row + 1,
        column=1,
        value=(
            f"Made by Recall on {statement.generated_utc} UTC from "
            f"{statement.archive_path}. Nothing in this file was invented: "
            "where a value is missing, the cell is empty and the Problems "
            "sheet says why."
        ),
    ).font = xl.Font(italic=True)


def _header_row(sheet, row: int, headings, xl) -> None:
    """The one header style, used by all four kinds of sheet."""
    font = xl.Font(bold=True, color="FFFFFF")
    fill = xl.PatternFill("solid", fgColor=_HEADER_FILL)
    for i, heading in enumerate(headings, start=1):
        cell = sheet.cell(row=row, column=i, value=heading)
        cell.font = font
        cell.fill = fill
        cell.alignment = xl.Alignment(vertical="center", wrap_text=True)


# ---------------------------------------------------------------------------
# What a column means, and how wide and what type it wants to be
# ---------------------------------------------------------------------------


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


#: One sentence per column, for the Read me sheet. Where a column is known to
#: be empty, it says so - a column that is always blank and unexplained is the
#: kind of thing somebody reads as "we had no contact with them".
_COLUMN_NOTES = {
    "address": "The contact's business address, as one line.",
    "all_day": "yes if the entry takes the whole day rather than a set time.",
    "attachment_names": "The file names attached to the message, separated by ; .",
    "attendee_count": "How many people were invited, counting the organiser.",
    "attendees": "Everyone invited, separated by ; .",
    "body": "The full text of the record. Long ones are cut off at 32,000 "
            "characters with a note saying so - Excel cannot hold more.",
    "body_preview": "The opening of the message. The whole message is in "
                    "Recall; this column is here to make the row readable.",
    "busy_status": "How the time was marked in the calendar: busy, free, "
                   "tentative or out of office.",
    "category": "The colour category or label Outlook had on the entry.",
    "cc": "Everyone copied in, separated by ; .",
    "data_quality": "Anything uncertain about this particular record - a "
                    "guessed character set, an unknown timezone. Empty means "
                    "nothing was uncertain about it.",
    "date": "The date the record belongs to: a message sent, an event "
            "starting, a contact first seen. Empty means Recall found no "
            "date and refuses to guess one; those records are counted on the "
            "Integrity sheet.",
    "display_name": "The contact's name as Outlook held it.",
    "due_date": "When the task was due. Empty means no due date was set.",
    "duration_min": "How long the entry lasts, in minutes.",
    "emails": "Every email address on the contact card, separated by ; .",
    "end": "The time the entry finishes, in the timezone of the column beside it.",
    "first_seen": "The earliest date Recall has for this contact.",
    "folder": "The Outlook folder the record was filed in.",
    "from_address": "The sender's email address.",
    "from_name": "The sender's name as it appeared on the message.",
    "given_name": "The contact's first name.",
    "has_attachments": "yes if the message carried attachments.",
    "importance": "How the record was marked in Outlook: high, normal or low.",
    "item_count": "Always empty. Recall does not yet count how many records "
                  "mention a contact; it is not a zero.",
    "item_id": "Recall's own number for the record, so a row here can be "
               "matched to the record on screen.",
    "last_seen": "Always empty. Recall does not yet track the most recent "
                 "record for a contact; it is not a date it failed to find.",
    "location": "Where the entry was to be held, as it was typed.",
    "meeting_status": "Whether the entry is a meeting, and whether it was "
                      "cancelled.",
    "message_id": "The identifier the mail system gave the message. Useful "
                  "for matching this row against another system's copy.",
    "notes": "The body of the calendar entry.",
    "organization": "The company on the contact card.",
    "organizer": "Who called the meeting.",
    "phone_business": "The contact's work telephone number.",
    "phone_home": "The contact's home telephone number.",
    "phone_mobile": "The contact's mobile telephone number.",
    "recurrence_rule": "How the entry repeats, in the words Outlook stored.",
    "recurring": "yes if the entry is one of a repeating series.",
    "responses": "Who accepted, declined or did not answer.",
    "source_file": "The Outlook file this record was read out of. Where the "
                   "same record was found in more than one file, all of them "
                   "are listed, separated by | .",
    "start": "The time the entry begins, in the timezone of the column beside it.",
    "subject": "The subject line, or the title of the entry.",
    "surname": "The contact's family name.",
    "time": "The time of day the message was sent.",
    "timezone": "The timezone the start and end times are given in.",
    "timezone_known": "no means the timezone was not recorded and the times "
                      "are as they were stored. Recall does not shift a time "
                      "it cannot place.",
    "title": "The contact's job title.",
    "to": "Everyone the message was addressed to, separated by ; .",
}


def column_note(column: str) -> str:
    """One sentence saying what a column holds, for the Read me sheet."""
    return _COLUMN_NOTES.get(column, f"The {_heading(column)} of the record.")


#: Columns wide enough to need saying so, in Excel's character units. The rest
#: are sized from the length of their heading.
_WIDE_COLUMNS = {
    "subject": 46, "location": 28, "attendees": 52, "notes": 60,
    "source_file": 46, "body_preview": 60, "data_quality": 44,
    "recurrence_rule": 32, "emails": 36, "to": 40, "cc": 40,
    "body": 60,
}


def column_width(column: str) -> int:
    """How wide this column wants to be, in Excel's character units.

    The table on the Search screen reads this too, converting to pixels, so a
    column that is wide in the workbook is wide on the screen. Two tuned lists
    would drift, and a client comparing the screen against the spreadsheet
    would find the difference before we did.
    """
    return _WIDE_COLUMNS.get(column, max(12, min(26, len(_heading(column)) + 4)))


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------

#: Control characters the XLSX format forbids. Excel will not open a workbook
#: containing them, and openpyxl refuses to write one - which means real mail
#: crashes the export, because real mail is full of stray control bytes from
#: thirty years of mail clients.
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _cell(value):
    """One value, made safe for a spreadsheet and otherwise left alone."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float, date, datetime)):
        return value

    text = _ILLEGAL.sub("", str(value))

    # Only a leading "=" makes a formula in a workbook. A CSV is different -
    # Excel treats + - @ as formula starts when it *imports* text - and the
    # CSV exporter guards all four. Copying that guard here corrupted three
    # values in four: "+44 20 7946 0000" came back with an apostrophe in front
    # of it, visible to every reader, for no reason at all. Tested rather than
    # assumed: written as plain strings, all three store as text.
    if text.startswith("="):
        text = "'" + text

    # A cell cannot hold more than 32,767 characters, and a long mail body
    # easily exceeds that. Truncating with a visible marker is better than an
    # export that will not open.
    if len(text) > 32_000:
        text = text[:32_000] + "… (truncated for Excel)"

    return text


def _typed(column: str, value):
    """A cell of the type the column deserves, or the text it arrived as.

    Which columns are dates and which are numbers is declared, not guessed
    from what the text looks like. Guessing is how "0044123456" used to export
    as the number 44,123,456 - a phone number quietly changed into a different
    phone number, which is the one thing this program is not allowed to do.
    """
    if column in _DATE_COLUMNS:
        parsed = _as_date(value)
        return parsed if parsed is not None else _cell(value)
    if column in _NUMBER_COLUMNS:
        parsed = _as_int(value)
        return parsed if parsed is not None else _cell(value)
    return _cell(value)


def _as_date(value):
    """A real date, or None if this is not one. Never a guess."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _as_int(value):
    """A whole number, or None if this is not one."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _sheet_name(name: str) -> str:
    """Excel refuses these characters and anything over 31 characters."""
    cleaned = re.sub(r"[\/*?:\[\]]", " ", str(name)).strip() or "Records"
    return cleaned[:31]
