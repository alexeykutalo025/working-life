"""What the API does when it is handed something it did not expect.

Every case here is one that answered with a 500 and a stack trace, where the
program had a perfectly good sentence available to say instead. They are
grouped because they share a cause: a value arriving by URL rather than through
the screen, where the screen's own checks never ran.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from recall.api.app import create_app
from recall.api.search import BadDate, month_bound
from recall.integrity.engine import Finding, record_finding
from recall.integrity.honest import _month_after
from recall.normalize.attachments import BlobStore


@pytest.fixture
def client(settings, conn):
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


# --- a date the filter box would have refused ------------------------------
#
# dateInput() in screens/search.js checks what is typed into it. render() reads
# ?from= and ?to= straight out of the URL and never checks those, so a
# bookmarked or hand-edited link reached int() on "20x3" and the search came
# back as a server error.

UNREADABLE = ["20x3", "abcd", "2003-ab", "the nineties", "2003-"]


@pytest.mark.parametrize("value", UNREADABLE)
@pytest.mark.parametrize("field", ["date_from", "date_to"])
def test_an_unreadable_date_is_refused_not_crashed(client, field, value):
    response = client.get("/api/search", params={"q": "letter", field: value})
    assert response.status_code == 400
    assert "not a date Recall can read" in response.json()["detail"]


@pytest.mark.parametrize("value", UNREADABLE)
def test_the_table_refuses_it_too(client, value):
    assert client.get("/api/search/table", params={"date_to": value}).status_code == 400


@pytest.mark.parametrize("value", UNREADABLE)
def test_the_download_refuses_it_too(client, value):
    """The download link carries the same filters, by the same road."""
    response = client.get("/api/export/search/download", params={"format": "csv", "date_to": value})
    assert response.status_code == 400
    assert "not a date Recall can read" in response.json()["detail"]


def test_undated_does_not_smuggle_a_bad_date_past_the_check(client):
    """undated=1 skips the date bounds further down; the check still runs."""
    response = client.get("/api/search", params={"undated": "1", "date_to": "20x3"})
    assert response.status_code == 400


@pytest.mark.parametrize("value", ["2003", "2003-04", "2003-04-14"])
def test_the_dates_the_screen_offers_are_all_accepted(client, value):
    for field in ("date_from", "date_to"):
        assert client.get("/api/search", params={field: value}).status_code == 200


# --- a bare year is a whole year ------------------------------------------


def test_a_bare_year_covers_the_whole_year():
    """"2003" as an end bound means December, not the month before February.

    Taking value[:7] looked like it did this. It left "2003" alone, and a
    period end of "2003" sorts below every month in 2003, so the gaps in the
    year the user asked about were all excluded and the total beside them
    stopped saying anything was missing.
    """
    assert month_bound("2003", end=False) == "2003-01"
    assert month_bound("2003", end=True) == "2003-12"
    assert month_bound("2003-04", end=True) == "2003-04"
    assert month_bound("2003-04-14", end=False) == "2003-04"
    assert month_bound(None, end=True) is None
    with pytest.raises(BadDate):
        month_bound("20x3", end=True)


def test_a_period_must_be_a_month():
    """_month_after used to take a bare year and answer February."""
    assert _month_after("2003-12") == "2004-01-01T00:00:00Z"
    assert _month_after("2003-04") == "2003-05-01T00:00:00Z"
    with pytest.raises(ValueError):
        _month_after("2003")


def test_a_gap_in_the_year_qualifies_a_search_over_that_year(client, conn):
    """The whole point of the bound: a total must admit what it is missing."""
    record_finding(conn, Finding(
        code="hard_gap", severity="high", title="Nothing in June 2003",
        period_start="2003-06",
    ))
    conn.commit()

    response = client.get(
        "/api/search", params={"date_from": "2003", "date_to": "2003"}
    )
    assert response.status_code == 200
    codes = [q["code"] for q in response.json()["total"]["qualifiers"]]
    assert "hard_gap" in codes, "a search over 2003 did not mention the gap in 2003"


# --- an attachment whose extension was refused at write time ---------------


def test_an_attachment_with_an_awkward_extension_downloads(settings, conn, client):
    """The store names blobs with safe_suffix; the download must read them so.

    safe_suffix refuses an extension that is not plain letters and digits, so
    "design.c++" is stored under the bare hash. The endpoint used to recompute
    the suffix as Path(...).suffix, look for a file that had never been
    written, find the real one, and then serve the name it had made up - which
    Starlette answered with a RuntimeError and the user saw as a 500.
    """
    blob = BlobStore(settings.blobs_path).put(b"the drawing", "design.c++")
    assert blob.path.name == blob.content_hash, "expected no suffix on disk"

    conn.execute("INSERT INTO items(kind, dedup_key) VALUES ('message', 'k1')")
    item_id = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    conn.execute(
        "INSERT INTO attachments(item_id, filename, content_hash, mime_type) "
        "VALUES (?, 'design.c++', ?, 'text/plain')",
        (item_id, blob.content_hash),
    )
    conn.commit()
    attachment_id = conn.execute("SELECT id FROM attachments").fetchone()["id"]

    response = client.get(f"/api/attachments/{attachment_id}")
    assert response.status_code == 200
    assert response.content == b"the drawing"


def test_an_attachment_whose_file_is_gone_says_so(settings, conn, client):
    """Still a 410 and a sentence, not a crash."""
    conn.execute("INSERT INTO items(kind, dedup_key) VALUES ('message', 'k2')")
    item_id = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    conn.execute(
        "INSERT INTO attachments(item_id, filename, content_hash) "
        "VALUES (?, 'gone.pdf', ?)",
        (item_id, "0" * 64),
    )
    conn.commit()
    attachment_id = conn.execute("SELECT id FROM attachments").fetchone()["id"]

    assert client.get(f"/api/attachments/{attachment_id}").status_code == 410


# --- an endpoint that does not exist ---------------------------------------


def test_an_unknown_api_path_is_a_404_not_the_page(client):
    """The catch-all serves index.html for every route, which is right for a
    hash-routed page and wrong under /api/. Handing the caller markup with a
    200 told it the call had worked; api.js then passed a page of HTML to a
    screen as if it were data, and the break surfaced somewhere unrelated.
    """
    response = client.get("/api/no-such-endpoint")
    assert response.status_code == 404
    assert "no-such-endpoint" in response.json()["detail"]


def test_an_ordinary_page_still_gets_the_shell(client):
    response = client.get("/timeline")
    assert response.status_code == 200
    assert response.text.lstrip().startswith("<!DOCTYPE html>")


def test_the_real_endpoints_still_answer(client):
    assert client.get("/api/health").json()["ok"] is True
