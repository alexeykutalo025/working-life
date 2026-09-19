"""One workbook holding every kind of record.

Two things are being fixed here at once.

Tasks and sticky notes were counted on screen and then silently left out of
every export. Both the PST/OST reader and the .msg reader produce them, so a
real fifty-year archive has them, and a number that disagrees with the rows in
the file is the one failure spec 9.6 calls a defect rather than a nicety.

And an Excel export used to arrive as several files, one per kind, each with
its own integrity statement. One workbook with a sheet per kind is what a
person actually wants handed to them: it opens, it emails, and the Integrity
sheet cannot be separated from the data it describes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from recall.export import ExportError
from recall.export.rows import NOTE_COLUMNS, TASK_COLUMNS, note_rows, task_rows
from recall.export.selection import export_search
from recall.extract import Extractor
from recall.scan.walker import Scanner
from tests.fixtures.generate import generate_eml, generate_ics

openpyxl = pytest.importorskip("openpyxl")


def add_item(conn, *, kind: str, subject: str, occurred=None, end=None,
             body="", importance=""):
    """One item straight into the archive.

    No parser produces a task from the synthetic fixtures, and writing a real
    .msg task by hand would be testing extract-msg rather than the exporter.
    """
    cur = conn.execute(
        "INSERT INTO items(kind, dedup_key, occurred_utc, end_utc, subject, "
        "body_text, importance) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (kind, f"{kind}:{subject}", occurred, end, subject, body, importance),
    )
    return int(cur.lastrowid)


@pytest.fixture
def archive(tmp_path: Path, settings, conn):
    fixtures = tmp_path / "corpus"
    generate_eml(fixtures)
    generate_ics(fixtures)
    Scanner(settings, conn).run([fixtures])
    Extractor(settings, conn).run()
    return conn


def sheets(path: Path) -> dict:
    book = openpyxl.load_workbook(path)
    return {name: book[name] for name in book.sheetnames}


def rows_of(sheet) -> list[list]:
    return [[c.value for c in row] for row in sheet.iter_rows()]


# --- tasks and notes reach the file ---------------------------------------


def test_a_task_becomes_a_row(archive):
    add_item(archive, kind="task", subject="Renew the lease",
             occurred="2003-04-01T09:00:00Z", end="2003-05-01T00:00:00Z",
             body="Call the agent first.", importance="high")

    rows = list(task_rows(archive))

    assert len(rows) == 1
    assert rows[0]["subject"] == "Renew the lease"
    assert rows[0]["date"] == "2003-04-01"
    assert rows[0]["due_date"] == "2003-05-01", "a task without its due date is half a task"
    assert rows[0]["importance"] == "high"


def test_a_note_becomes_a_row(archive):
    add_item(archive, kind="note", subject="Bank details",
             occurred="2005-02-02T00:00:00Z", body="Sort code 20-00-00")

    rows = list(note_rows(archive))

    assert len(rows) == 1
    assert rows[0]["subject"] == "Bank details"
    assert rows[0]["body"] == "Sort code 20-00-00"


def test_a_task_with_no_due_date_says_nothing_rather_than_guessing(archive):
    add_item(archive, kind="task", subject="Someday", occurred="2003-04-01T09:00:00Z")
    assert list(task_rows(archive))[0]["due_date"] == ""


def test_every_task_and_note_row_carries_its_record_number(archive):
    add_item(archive, kind="task", subject="A task", occurred="2003-01-01T00:00:00Z")
    add_item(archive, kind="note", subject="A note", occurred="2003-01-01T00:00:00Z")

    for row in [*task_rows(archive), *note_rows(archive)]:
        assert row["id"] == row["item_id"]


def test_the_columns_are_only_what_is_actually_known(archive):
    """Tasks have no shape of their own in the schema, so none is invented."""
    assert "due_date" in TASK_COLUMNS
    assert "due_date" not in NOTE_COLUMNS, "a sticky note has no due date"
    for columns in (TASK_COLUMNS, NOTE_COLUMNS):
        assert columns[-1] == "item_id"
        assert "data_quality" in columns


# --- one workbook, many sheets --------------------------------------------


def test_excel_gives_one_file_not_one_per_kind(archive, settings):
    result = export_search(archive, settings, fmt="xlsx")
    assert len(result["files"]) == 1, "the user should be handed one file to open"
    assert Path(result["files"][0]["file"]).suffix == ".xlsx"


def test_integrity_is_still_the_first_sheet(archive, settings):
    """Spec 9.6. Nobody should open this workbook without seeing it."""
    result = export_search(archive, settings, fmt="xlsx")
    book = openpyxl.load_workbook(result["files"][0]["file"])
    assert book.sheetnames[0] == "Integrity"


def test_there_is_no_stray_integrity_file_beside_the_workbook(archive, settings):
    result = export_search(archive, settings, fmt="xlsx")
    assert result["files"][0]["integrity_file"] is None


def test_each_kind_gets_its_own_sheet(archive, settings):
    add_item(archive, kind="task", subject="Renew the lease",
             occurred="2003-04-01T09:00:00Z")
    add_item(archive, kind="note", subject="Bank details",
             occurred="2003-04-02T09:00:00Z")

    result = export_search(archive, settings, fmt="xlsx")
    names = openpyxl.load_workbook(result["files"][0]["file"]).sheetnames

    assert names[0] == "Integrity"
    for expected in ("Messages", "Calendar", "Tasks", "Notes"):
        assert expected in names, f"{expected} is missing from the workbook"


def test_the_sheets_are_in_the_order_someone_would_look_for_them(archive, settings):
    add_item(archive, kind="task", subject="A task", occurred="2003-04-01T09:00:00Z")
    add_item(archive, kind="note", subject="A note", occurred="2003-04-02T09:00:00Z")

    result = export_search(archive, settings, fmt="xlsx")
    names = openpyxl.load_workbook(result["files"][0]["file"]).sheetnames

    assert names.index("Messages") < names.index("Calendar") < names.index("Tasks")
    assert names.index("Tasks") < names.index("Notes")


def test_a_task_actually_appears_in_the_workbook(archive, settings):
    """The whole point: it used to count on screen and not be in the file."""
    add_item(archive, kind="task", subject="Renew the lease",
             occurred="2003-04-01T09:00:00Z", end="2003-05-01T00:00:00Z")

    result = export_search(archive, settings, fmt="xlsx")
    sheet = openpyxl.load_workbook(result["files"][0]["file"])["Tasks"]
    text = "\n".join(str(c.value) for row in sheet.iter_rows() for c in row)

    assert "Renew the lease" in text
    assert "2003-05-01" in text


def test_the_count_matches_the_rows_actually_written(archive, settings):
    add_item(archive, kind="task", subject="A task", occurred="2003-04-01T09:00:00Z")
    add_item(archive, kind="note", subject="A note", occurred="2003-04-02T09:00:00Z")

    result = export_search(archive, settings, fmt="xlsx")
    book = openpyxl.load_workbook(result["files"][0]["file"])

    written = sum(
        book[name].max_row - 1                      # less the header row
        for name in book.sheetnames if name != "Integrity"
    )
    assert written == result["total_records"]


def test_the_workbook_says_how_many_of_each_kind_it_holds(archive, settings):
    add_item(archive, kind="task", subject="A task", occurred="2003-04-01T09:00:00Z")

    result = export_search(archive, settings, fmt="xlsx")
    sheets_reported = {s["kind"]: s["count"] for s in result["files"][0]["sheets"]}

    assert sheets_reported["task"] == 1
    assert sum(sheets_reported.values()) == result["total_records"]


def test_a_task_only_search_still_produces_a_workbook(archive, settings):
    add_item(archive, kind="task", subject="Renew the lease",
             occurred="2003-04-01T09:00:00Z")

    result = export_search(archive, settings, fmt="xlsx", kind="task")
    book = openpyxl.load_workbook(result["files"][0]["file"])

    assert book.sheetnames == ["Integrity", "Tasks"]


def test_a_sheet_name_excel_would_refuse_is_made_safe():
    from recall.export.xlsx_export import _sheet_name

    assert _sheet_name("a/b:c*d?e[f]") == "a b c d e f"
    assert len(_sheet_name("x" * 60)) == 31
    assert _sheet_name("") == "Records"


# --- the other formats are unchanged --------------------------------------


def test_csv_still_gives_one_file_per_kind(archive, settings):
    """A CSV genuinely cannot hold five tables, so this must not change."""
    result = export_search(archive, settings, fmt="csv")
    kinds = {f["kind"] for f in result["files"]}

    assert len(result["files"]) > 1
    assert "all" not in kinds
    assert all(f["integrity_file"] for f in result["files"])


def test_tasks_reach_a_csv_too(archive, settings):
    add_item(archive, kind="task", subject="Renew the lease",
             occurred="2003-04-01T09:00:00Z")

    result = export_search(archive, settings, fmt="csv", kind="task")
    text = Path(result["files"][0]["file"]).read_text(encoding="utf-8-sig")

    assert "Renew the lease" in text


def test_an_empty_search_still_refuses_rather_than_writing_nothing(archive, settings):
    with pytest.raises(ExportError):
        export_search(archive, settings, fmt="xlsx", query="nothingmatchesthisatall")
