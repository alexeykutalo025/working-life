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


# --- sorting by a column --------------------------------------------------
#
# The rule these hold: clicking a heading sorts every record that matched, and
# the page is then cut out of that. Sorting the fifty rows already on screen
# would be a great deal easier and would tell the user something untrue - that
# the top of the table is the top of the result set.


def test_every_sortable_column_is_sql_the_database_accepts(conn):
    """One typo in one expression, and only that column's table is a 500.

    Nothing else here can catch that: the fixtures hold messages, calendar
    entries and contacts, so tasks, notes, and any column a fixture happens
    not to fill, would go out untried.
    """
    from recall.api.search import _SORT_SQL, _order_by

    for column in _SORT_SQL:
        for descending in (False, True):
            conn.execute(
                "SELECT i.id FROM items i "
                f"ORDER BY {_order_by(column, descending=descending)} LIMIT 1"
            ).fetchall()


def test_the_columns_offered_for_sorting_are_columns_the_table_has(client):
    for kind in ("message", "event"):
        body = client.get("/api/search/table", params={"kind": kind}).json()
        assert set(body["sort"]["sortable"]) <= set(body["columns"])
        assert body["sort"]["sortable"], f"nothing on the {kind} table sorts"


def test_clicking_a_heading_sorts_by_that_column(client):
    body = client.get("/api/search/table", params={
        "kind": "message", "limit": 500, "sort": "subject",
    }).json()

    subjects = [r["subject"] for r in body["rows"]]
    assert subjects == sorted(subjects, key=str.casefold)
    assert body["sort"]["column"] == "subject"
    assert body["sort"]["direction"] == "asc"


def test_clicking_it_again_sorts_the_other_way(client):
    up = client.get("/api/search/table", params={
        "kind": "message", "limit": 500, "sort": "subject",
    }).json()
    down = client.get("/api/search/table", params={
        "kind": "message", "limit": 500, "sort": "subject", "direction": "desc",
    }).json()

    assert down["sort"]["direction"] == "desc"
    assert [r["item_id"] for r in down["rows"]] == [
        r["item_id"] for r in reversed(up["rows"])
    ]


def test_an_alphabet_is_not_the_ascii_order(client):
    """A lower-case subject belongs among the words, not after all of them."""
    body = client.get("/api/search/table", params={
        "kind": "message", "limit": 500, "sort": "subject",
    }).json()

    folded = [r["subject"].casefold() for r in body["rows"] if r["subject"]]
    assert folded == sorted(folded)


def test_the_sort_covers_the_whole_result_set_not_just_the_page(client):
    """The failure this exists for: a page sorted after it was cut."""
    everything = client.get("/api/search/table", params={
        "kind": "message", "limit": 500, "sort": "subject",
    }).json()
    first_page = client.get("/api/search/table", params={
        "kind": "message", "limit": 2, "sort": "subject",
    }).json()

    assert len(everything["rows"]) > 2, "this archive is too small to prove it"
    assert [r["item_id"] for r in first_page["rows"]] == [
        r["item_id"] for r in everything["rows"][:2]
    ]


def test_paging_through_a_sorted_table_shows_each_record_once(client):
    everything = client.get("/api/search/table", params={
        "kind": "message", "limit": 500, "sort": "from_name",
    }).json()

    seen = []
    for offset in range(0, len(everything["rows"]), 2):
        page = client.get("/api/search/table", params={
            "kind": "message", "limit": 2, "sort": "from_name", "offset": offset,
        }).json()
        seen.extend(r["item_id"] for r in page["rows"])

    assert seen == [r["item_id"] for r in everything["rows"]]
    assert len(set(seen)) == len(seen), "a record turned up on two pages"


def test_an_empty_cell_goes_to_the_end_whichever_way_the_column_runs(client):
    """A screenful of blanks is never what clicking a heading was for."""
    for direction in ("asc", "desc"):
        body = client.get("/api/search/table", params={
            "kind": "message", "limit": 500, "sort": "attachment_names",
            "direction": direction,
        }).json()

        filled = [bool(r["attachment_names"]) for r in body["rows"]]
        assert filled == sorted(filled, reverse=True), (
            f"blank cells came first going {direction}"
        )


def test_a_column_that_cannot_be_sorted_faithfully_is_not_offered(client):
    """``data_quality`` is a sentence written in Python out of several facts.

    Ordering by an expression that only approximates it would produce a table
    that looks unsorted, which is worse than a heading that does not offer to
    sort at all.
    """
    body = client.get("/api/search/table", params={"kind": "message"}).json()

    assert "data_quality" in body["columns"]
    assert "data_quality" not in body["sort"]["sortable"]


def test_asking_for_a_sort_that_cannot_be_done_is_ignored_not_an_error(client):
    plain = client.get("/api/search/table", params={"kind": "message"}).json()

    for column in ("data_quality", "no_such_column", "x'; DROP TABLE items; --"):
        body = client.get("/api/search/table", params={
            "kind": "message", "sort": column,
        }).json()
        assert body["sort"]["column"] is None, f"{column!r} was let through"
        assert [r["item_id"] for r in body["rows"]] == [
            r["item_id"] for r in plain["rows"]
        ]


def test_a_sort_belonging_to_another_kind_is_dropped_and_said_to_be(client):
    """Switching to the calendar with "From" sorted must not leave a heading
    drawing an arrow over an order that was never applied."""
    body = client.get("/api/search/table", params={
        "kind": "event", "sort": "from_name",
    }).json()

    assert body["sort"]["column"] is None
    assert "from_name" not in body["columns"]


def test_the_rows_come_back_in_the_order_they_were_sorted_into(client):
    """The row builders order their own queries; the page order has to win."""
    body = client.get("/api/search/table", params={
        "kind": "message", "limit": 500, "sort": "date", "direction": "desc",
    }).json()

    dates = [r["date"] for r in body["rows"] if r["date"]]
    assert dates == sorted(dates, reverse=True)


def test_a_contact_card_that_will_not_parse_does_not_break_the_sort(conn):
    """``json_extract`` on broken JSON is an error, not an empty cell."""
    from recall.api.search import _order_by

    conn.execute(
        "INSERT INTO items (kind, dedup_key, subject, contact_json) "
        "VALUES ('contact', 'broken-card', 'Someone', '{not json')"
    )
    for column in ("display_name", "given_name", "phone_mobile"):
        rows = conn.execute(
            "SELECT i.id FROM items i WHERE i.kind = 'contact' "
            f"ORDER BY {_order_by(column, descending=False)}"
        ).fetchall()
        assert rows, f"the broken card took {column} down with it"


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
