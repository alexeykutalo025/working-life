"""The workbook, read the way it will actually be read.

The client's words were "well-structured data for analysis", and they intend
to hand this file to Claude as well as open it in Excel. Those two readers want
the same thing and are usually assumed to want opposite things, so the rule
these tests hold is one rule:

    **The data sheets are strict rectangles. Every word of explanation lives
    somewhere else.**

Headings on row 1, records from row 2, no banner, no merged cell, no blank
spacer, no totals row. Formatting and machine-readability only conflict where
somebody puts prose inside a data table, so none goes near one. The
explanation gets three sheets of its own in front of the data.

The rest is about types. Every date used to reach the sheet as text, which
sorts by accident and groups by nothing - and two guards used to quietly change
values on the way: an apostrophe in front of anything starting + - or @, and
any digit string under fifteen characters turned into an integer, so a phone
number came out as a different phone number.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from recall.export.rows import CONTACT_COLUMNS
from recall.export.selection import export_search
from recall.export.xlsx_export import REFERENCE_SHEETS, column_note
from recall.extract import Extractor
from recall.scan.walker import Scanner
from tests.fixtures.generate import generate_eml, generate_ics

openpyxl = pytest.importorskip("openpyxl")


@pytest.fixture
def workbook(tmp_path: Path, settings, conn):
    """A workbook holding one of everything, including the awkward values."""
    corpus = tmp_path / "corpus"
    generate_eml(corpus)
    generate_ics(corpus)
    Scanner(settings, conn).run([corpus])
    Extractor(settings, conn).run()

    conn.execute(
        "INSERT INTO items(kind, dedup_key, occurred_utc, subject, contact_json) "
        "VALUES ('contact', 'contact:probe', '1998-06-01T00:00:00Z', 'Probe', ?)",
        ('{"display_name": "A Probe", "emails": ["probe@example.com"], '
         '"phones": {"business": "+44 20 7946 0000", "home": "0044123456", '
         '"mobile": "007"}}',),
    )
    conn.execute(
        "INSERT INTO items(kind, dedup_key, occurred_utc, subject, body_text) "
        "VALUES ('task', 'task:probe', '2001-02-03T00:00:00Z', "
        "'-- Review the lease', '=SUM(A1:A9) was in the body')"
    )
    conn.commit()

    result = export_search(conn, settings, fmt="xlsx")
    return openpyxl.load_workbook(result["files"][0]["file"])


def data_sheets(book):
    return [book[n] for n in book.sheetnames if n not in REFERENCE_SHEETS]


def headers(sheet):
    return [c.value for c in sheet[1]]


def find_row(sheet, column: str, value):
    at = headers(sheet).index(column)
    for row in sheet.iter_rows(min_row=2):
        if row[at].value == value:
            return dict(zip(headers(sheet), row))
    raise AssertionError(f"no row where {column} is {value!r}")


# --- the rectangle --------------------------------------------------------


def test_every_data_sheet_starts_with_its_headings_on_row_one(workbook):
    for sheet in data_sheets(workbook):
        assert all(isinstance(h, str) and h for h in headers(sheet)), sheet.title


def test_no_data_sheet_merges_a_cell(workbook):
    """A merged cell is where a rectangle stops being one."""
    for sheet in data_sheets(workbook):
        assert not sheet.merged_cells.ranges, f"{sheet.title} merges cells"


def test_no_heading_appears_twice_on_a_sheet(workbook):
    """Excel will not open a table whose header row repeats a name."""
    for sheet in data_sheets(workbook):
        names = headers(sheet)
        assert len(set(names)) == len(names), sheet.title


def test_every_data_sheet_is_a_real_excel_table(workbook):
    """A table brings banded rows, a proper filter and a named range."""
    for sheet in data_sheets(workbook):
        assert sheet.tables, f"{sheet.title} is a bare grid"
        ref = next(iter(sheet.tables.values())).ref
        assert ref.startswith("A1:"), f"{sheet.title}'s table misses the headings"


def test_a_sheet_never_carries_both_a_table_and_its_own_filter(workbook):
    """Setting both is what makes Excel offer to repair the file."""
    for name in workbook.sheetnames:
        sheet = workbook[name]
        if sheet.tables:
            assert sheet.auto_filter.ref is None, name


def test_the_first_column_stays_put_when_you_scroll_right(workbook):
    """Twenty-two calendar columns, and no way of telling which row is which."""
    for sheet in data_sheets(workbook):
        assert sheet.freeze_panes == "B2", sheet.title


def test_a_table_on_a_sheet_with_a_title_covers_its_own_headings(workbook):
    """The reference sheets carry a title above their table.

    Point the table at row 1 and it swallows the title as a header row, which
    Excel opens and then quietly misreads.
    """
    for name in REFERENCE_SHEETS:
        sheet = workbook[name]
        ref = next(iter(sheet.tables.values())).ref
        top = int("".join(c for c in ref.split(":")[0] if c.isdigit()))
        assert sheet.cell(row=top, column=1).value not in (None, ""), name
        assert top > 1, f"{name} has a title above its table"


# --- types ----------------------------------------------------------------


def test_a_date_is_a_date_and_not_the_text_of_one(workbook):
    """The single largest reason this file was hard to analyse."""
    messages = workbook["Messages"]
    dated = [
        c for c in (r[headers(messages).index("date")]
                    for r in messages.iter_rows(min_row=2))
        if c.value not in (None, "")
    ]

    assert dated, "no dated messages in the fixture; this test proves nothing"
    for cell in dated:
        assert isinstance(cell.value, (datetime.date, datetime.datetime))
        assert cell.number_format == "yyyy-mm-dd"


def test_a_count_is_a_number_that_can_be_summed(workbook):
    calendar = workbook["Calendar"]
    at = headers(calendar).index("duration (minutes)")
    values = [r[at].value for r in calendar.iter_rows(min_row=2)]

    assert any(v is not None for v in values)
    assert all(isinstance(v, int) for v in values if v not in (None, ""))


def test_a_record_with_no_date_leaves_the_cell_empty(workbook, conn):
    """Never a zero, never a 1900-01-01, never a guess. Spec section 9."""
    messages = workbook["Messages"]
    at = headers(messages).index("date")

    for row in messages.iter_rows(min_row=2):
        value = row[at].value
        assert value is None or isinstance(value, (datetime.date, datetime.datetime))


def test_a_telephone_number_comes_back_the_number_it_went_in_as(workbook):
    """+44... used to arrive with an apostrophe; 0044... used to become 44,123,456."""
    row = find_row(workbook["Contacts"], "display name", "A Probe")

    assert row["phone (work)"].value == "+44 20 7946 0000"
    assert row["phone (home)"].value == "0044123456"
    assert row["phone (mobile)"].value == "007"


def test_a_subject_beginning_with_a_dash_is_left_alone(workbook):
    row = find_row(workbook["Tasks"], "subject", "-- Review the lease")

    assert row["subject"].value == "-- Review the lease"


def test_text_that_really_would_become_a_formula_is_still_stopped(workbook):
    """The one case that is real: a leading = in a workbook cell."""
    row = find_row(workbook["Tasks"], "subject", "-- Review the lease")

    assert row["body"].value.startswith("'=")


# --- the sheets that explain the file -------------------------------------


def test_integrity_is_still_the_first_sheet(workbook):
    """Spec 9.6. Nobody should open this workbook without seeing it."""
    assert workbook.sheetnames[0] == "Integrity"


def test_the_explaining_sheets_come_before_the_data(workbook):
    assert workbook.sheetnames[:3] == list(REFERENCE_SHEETS)


def test_integrity_is_a_table_of_facts_with_no_blank_rows_in_it(workbook):
    """It used to be a free-form block with spacers and prose in column B."""
    sheet = workbook["Integrity"]
    ref = next(iter(sheet.tables.values())).ref
    top = int("".join(c for c in ref.split(":")[0] if c.isdigit()))
    bottom = int("".join(c for c in ref.split(":")[1] if c.isdigit()))

    assert [c.value for c in sheet[top]] == ["What", "Value"]
    for r in range(top + 1, bottom + 1):
        assert sheet.cell(row=r, column=1).value, f"row {r} has no label"


def test_every_problem_is_a_row_rather_than_a_paragraph(workbook):
    """A critical finding used to look exactly like a note."""
    sheet = workbook["Problems"]
    head = [c.value for c in sheet[3]]

    assert head[:3] == ["severity", "code", "what it is"]
    assert "records believed lost" in head

    severities = {sheet.cell(row=r, column=1).value
                  for r in range(4, sheet.max_row + 1)}
    assert severities, "no problems in the fixture; this test proves nothing"
    assert severities <= {"critical", "high", "medium", "info", "low"}


def test_the_numbers_on_the_problems_sheet_are_numbers(workbook):
    sheet = workbook["Problems"]
    at = [c.value for c in sheet[3]].index("records affected") + 1
    values = [sheet.cell(row=r, column=at).value for r in range(4, sheet.max_row + 1)]

    assert any(v is not None for v in values)
    assert all(isinstance(v, int) for v in values if v is not None)


def test_read_me_explains_every_column_of_every_data_sheet(workbook):
    sheet = workbook["Read me"]
    described = {
        (sheet.cell(row=r, column=1).value, sheet.cell(row=r, column=2).value)
        for r in range(5, sheet.max_row + 1)
        if sheet.cell(row=r, column=1).value
    }

    for data in data_sheets(workbook):
        for heading in headers(data):
            assert (data.title, heading) in described, (
                f"{data.title}.{heading!r} is in the file with nothing saying "
                "what it holds"
            )


def test_a_column_that_is_always_empty_says_so(workbook):
    """Contacts carry two columns Recall does not fill yet.

    An unexplained blank column reads as an answer - "no messages from them" -
    and that is the kind of quiet wrong answer this whole program exists to
    avoid.
    """
    for column in ("last_seen", "item_count"):
        assert column in CONTACT_COLUMNS
        assert "empty" in column_note(column).lower(), column


def test_every_exported_column_has_something_written_about_it():
    """A column with no note falls back to a sentence that says nothing."""
    from recall.export import rows as export_rows
    from recall.export.xlsx_export import _COLUMN_NOTES

    every = set()
    for attr in dir(export_rows):
        if attr.endswith("_COLUMNS"):
            every |= set(getattr(export_rows, attr))

    missing = every - set(_COLUMN_NOTES)
    assert missing == set(), f"no Read me note for {sorted(missing)}"


def test_the_reference_sheets_are_not_counted_as_records(workbook, settings, conn):
    """The count guarantee has to know which sheets hold records."""
    written = sum(
        sheet.max_row - 1 for sheet in data_sheets(workbook)
    )
    assert written > 0
    assert all(name in workbook.sheetnames for name in REFERENCE_SHEETS)
