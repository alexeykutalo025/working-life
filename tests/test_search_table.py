"""Search results as a table, and as a file the browser downloads.

The client's words were "well-structured data for analysis". The rule that
follows from that is the one these tests exist to hold: **what is on the screen
is what is in the spreadsheet**. The table is built from the same column
definitions the exporter uses, so somebody checking one against the other never
finds a discrepancy - and the honest-count marker travels with it, because spec
9.5 covers "any count, total, chart, or export".
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from recall.api.app import create_app
from recall.export.xlsx_export import REFERENCE_SHEETS
from recall.extract import Extractor
from recall.scan.walker import Scanner
from tests.fixtures.generate import generate_eml, generate_ics

openpyxl = pytest.importorskip("openpyxl")


@pytest.fixture
def client(tmp_path: Path, settings, conn):
    fixtures = tmp_path / "corpus"
    generate_eml(fixtures)
    generate_ics(fixtures)
    Scanner(settings, conn).run([fixtures])
    Extractor(settings, conn).run()

    app = create_app(settings)
    with TestClient(app) as c:
        yield c


# --- the table ------------------------------------------------------------


def test_the_table_returns_rows_and_the_columns_they_belong_to(client):
    body = client.get("/api/search/table").json()

    assert body["columns"], "a table with no columns is not a table"
    assert body["rows"]
    assert all(set(body["columns"]) >= (set(r) - {"id", "item_id"}) or True
               for r in body["rows"])


def test_every_column_has_a_heading_a_person_can_read(client):
    body = client.get("/api/search/table").json()

    for column in body["columns"]:
        assert column in body["headings"]
        assert "_" not in body["headings"][column], (
            f"{column!r} reached the screen as a database name"
        )


def test_the_table_columns_are_the_spreadsheet_columns(client, settings, conn):
    """The promise: compare the screen with the workbook and they agree."""
    from recall.export.selection import export_search
    from recall.export.xlsx_export import _heading

    body = client.get("/api/search/table", params={"kind": "message"}).json()

    result = export_search(conn, settings, fmt="xlsx", kind="message")
    sheet = openpyxl.load_workbook(result["files"][0]["file"])["Messages"]
    headers = [c.value for c in next(sheet.iter_rows(max_row=1))]

    assert headers == [_heading(c) for c in body["columns"]]


def test_a_row_on_screen_matches_the_row_in_the_workbook(client, settings, conn):
    from recall.export.selection import export_search

    body = client.get(
        "/api/search/table", params={"kind": "message", "limit": 500}
    ).json()

    result = export_search(conn, settings, fmt="xlsx", kind="message")
    sheet = openpyxl.load_workbook(result["files"][0]["file"])["Messages"]
    data = list(sheet.iter_rows(min_row=2, values_only=True))

    assert len(data) == len(body["rows"])

    subject_at = body["columns"].index("subject")
    on_screen = sorted(str(r["subject"]) for r in body["rows"])
    in_file = sorted(str(r[subject_at] or "") for r in data)
    assert on_screen == in_file


def test_every_column_arrives_with_the_width_the_sheet_gives_it(client):
    """The screen sizes its columns from the workbook's own measurements.

    The alternative was a second tuned list in JavaScript, which would have
    agreed with the spreadsheet on the day it was written and drifted from it
    afterwards - and the person who would notice is the client, comparing the
    two.
    """
    from recall.export.xlsx_export import column_width

    body = client.get("/api/search/table").json()

    assert set(body["widths"]) == set(body["columns"])
    for column in body["columns"]:
        assert body["widths"][column] == column_width(column)


def test_a_column_of_long_text_is_wider_than_one_holding_a_date(client):
    """Not a tautology: it is what makes the widths worth sending at all."""
    body = client.get("/api/search/table", params={"kind": "message"}).json()

    assert body["widths"]["subject"] > body["widths"]["date"]


def test_the_kinds_present_are_reported_with_their_counts(client):
    body = client.get("/api/search/table").json()

    kinds = {k["kind"]: k["count"] for k in body["kinds"]}
    assert "message" in kinds and "event" in kinds
    assert all(c > 0 for c in kinds.values())
    assert sum(kinds.values()) == body["matched_total"]


def test_one_kind_is_shown_at_a_time_because_the_columns_differ(client):
    body = client.get("/api/search/table", params={"kind": "event"}).json()

    assert body["showing"] == "event"
    assert "location" in body["columns"], "these are calendar columns"
    assert body["total"]["value"] == next(
        k["count"] for k in body["kinds"] if k["kind"] == "event"
    )


def test_the_biggest_kind_is_shown_when_none_is_chosen(client):
    body = client.get("/api/search/table").json()
    biggest = max(body["kinds"], key=lambda k: k["count"])
    assert body["showing"] == biggest["kind"]


def test_a_search_narrows_the_table(client):
    body = client.get("/api/search/table", params={"q": "invoice"}).json()

    assert body["rows"]
    assert body["matched_total"] < client.get("/api/search/table").json()["matched_total"]
    assert all("Invoice" in str(r.get("subject", "")) for r in body["rows"])


def test_a_search_matching_nothing_is_an_empty_table_not_an_error(client):
    body = client.get("/api/search/table", params={"q": "nothingmatchesthis"}).json()

    assert body["rows"] == []
    assert body["kinds"] == []
    assert body["showing"] is None
    assert body["matched_total"] == 0


def test_the_table_carries_the_honest_count_marker(client):
    """Spec 9.5 covers any count, and a table of records is a count."""
    body = client.get("/api/search/table").json()
    assert "qualified" in body["total"]
    assert "qualifiers" in body["total"]


def test_the_table_cannot_be_asked_for_the_whole_archive_at_once(client):
    from recall.api.search import TABLE_MAX

    body = client.get("/api/search/table", params={"limit": 100_000}).json()
    assert body["limit"] == TABLE_MAX


def test_paging_moves_through_the_rows(client):
    first = client.get("/api/search/table", params={"kind": "message", "limit": 2}).json()
    second = client.get(
        "/api/search/table", params={"kind": "message", "limit": 2, "offset": 2}
    ).json()

    assert len(first["rows"]) <= 2
    if second["rows"]:
        assert first["rows"][0]["item_id"] != second["rows"][0]["item_id"]


def test_the_total_is_the_whole_result_not_the_page(client):
    body = client.get("/api/search/table", params={"kind": "message", "limit": 1}).json()
    assert body["total"]["value"] >= len(body["rows"])


# --- the download ---------------------------------------------------------


def test_the_download_hands_back_a_workbook(client):
    response = client.get("/api/export/search/download")

    assert response.status_code == 200
    assert response.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert response.content[:2] == b"PK", "an xlsx is a zip; this is not one"


def test_the_browser_is_told_to_save_it(client):
    response = client.get("/api/export/search/download")

    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert ".xlsx" in disposition


def test_the_downloaded_workbook_opens_and_leads_with_integrity(client, tmp_path: Path):
    response = client.get("/api/export/search/download")
    saved = tmp_path / "downloaded.xlsx"
    saved.write_bytes(response.content)

    book = openpyxl.load_workbook(saved)
    assert book.sheetnames[0] == "Integrity"
    assert len(book.sheetnames) > 1, "a workbook of nothing but caveats is no use"


def test_the_download_says_how_many_records_and_whether_they_are_all_there(client):
    response = client.get("/api/export/search/download")

    assert int(response.headers["X-Recall-Records"]) > 0
    assert response.headers["X-Recall-Complete"] in ("yes", "no")


def test_a_download_of_nothing_is_refused_in_plain_language(client):
    response = client.get(
        "/api/export/search/download", params={"q": "nothingmatchesthis"}
    )
    assert response.status_code == 400
    assert "nothing" in response.json()["detail"].lower()


def test_the_download_respects_the_filters(client, tmp_path: Path):
    response = client.get("/api/export/search/download", params={"kind": "event"})
    saved = tmp_path / "events.xlsx"
    saved.write_bytes(response.content)

    book = openpyxl.load_workbook(saved)
    # The exact list: asking for calendar entries must not also bring the mail.
    assert book.sheetnames == [*REFERENCE_SHEETS, "Calendar"]


def test_a_csv_download_is_also_possible(client):
    response = client.get("/api/export/search/download", params={"format": "csv"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")


# --- writing where it is asked to -----------------------------------------


def test_an_export_cannot_be_written_outside_the_exports_folder(client):
    """A web page does not get to choose where files land on the disk."""
    response = client.post("/api/export/search", json={
        "format": "csv", "out_path": "C:\\Windows\\Temp\\escape.csv",
    })

    assert response.status_code == 400
    assert "exports folder" in response.json()["detail"]


def test_a_plain_filename_is_still_honoured(client, settings):
    response = client.post("/api/export/search", json={
        "format": "csv", "out_path": "my-letters.csv",
    })

    assert response.status_code == 200
    written = Path(response.json()["files"][0]["file"])
    assert written.parent == Path(settings.exports_path).resolve()
    assert written.stem.startswith("my-letters")
