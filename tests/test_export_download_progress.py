"""Watching a workbook being built, rather than waiting at a link that does nothing.

The Download button used to be a plain link. On a real archive the server spent
minutes reading records out and writing sheets before a single byte came back,
and the screen said nothing at all through it - which is indistinguishable from
a button that does not work.

So these tests are about one promise: while the file is being built, the page
can ask how far along it is and get an honest answer. Honest means the counts
are real, and that the step nothing can count says so rather than claiming a
percentage it cannot know.
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from recall.api.app import create_app

#: Enough rows that the writer reports more than once (it reports every 250).
ROWS = 600


@pytest.fixture
def archive(settings, conn):
    """A small archive, made directly: no parser is under test here."""
    from recall.search.indexer import build_index

    conn.executemany(
        "INSERT INTO items(kind, dedup_key, occurred_utc, subject, body_text) "
        "VALUES ('message', ?, ?, ?, 'The lease and the rent review.')",
        [
            (f"message:{i}", f"2003-04-{i % 28 + 1:02d}T09:00:00Z", f"Letter {i}")
            for i in range(ROWS)
        ],
    )
    conn.executemany(
        "INSERT INTO items(kind, dedup_key, occurred_utc, subject) "
        "VALUES ('event', ?, ?, ?)",
        [(f"event:{i}", f"2004-05-0{i + 1}T09:00:00Z", f"Meeting {i}")
         for i in range(5)],
    )
    conn.commit()
    build_index(conn)
    return conn


@pytest.fixture
def client(settings, archive):
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def nothing_left_running():
    """Every test starts with an empty slate.

    The list of files being built is one per process, deliberately - only one
    is built at a time - so a test that walked away from a running build would
    have the next test refused its file.
    """
    from recall.api import timeline

    yield

    deadline = time.monotonic() + 60
    while any(d.state == "running" for d in timeline._DOWNLOADS.values()):
        assert time.monotonic() < deadline, "a build never finished"
        time.sleep(0.05)
    timeline._DOWNLOADS.clear()


def prepare(client, **params):
    response = client.post(
        "/api/export/search/prepare", json={"format": "xlsx", **params}
    )
    assert response.status_code == 200, response.text
    return response.json()


def wait_for_ready(client, token, timeout=60.0):
    """Poll the way the page does, and keep every reading it saw."""
    seen = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get("/api/export/search/progress", params={"token": token}).json()
        seen.append(body)
        if body["state"] != "running":
            return body, seen
        time.sleep(0.05)
    raise AssertionError(f"still building after {timeout}s: {seen[-1]}")


# --- the road the button takes --------------------------------------------


def test_asking_for_a_workbook_gives_back_something_to_watch(client):
    started = prepare(client)

    assert started["token"]
    assert started["state"] == "running"
    wait_for_ready(client, started["token"])


def test_the_file_arrives_and_is_a_real_workbook(client):
    started = prepare(client)
    final, _ = wait_for_ready(client, started["token"])

    assert final["state"] == "ready", final["error"]
    assert final["records"] == ROWS + 5

    response = client.get(
        "/api/export/search/download", params={"token": started["token"]}
    )

    assert response.status_code == 200, response.text
    assert response.content[:2] == b"PK", "an xlsx is a zip"
    assert response.headers["X-Recall-Records"] == str(ROWS + 5)


def test_the_progress_it_reports_is_the_real_count(client):
    started = prepare(client)
    final, seen = wait_for_ready(client, started["token"])

    assert final["state"] == "ready", final["error"]
    counted = [s for s in seen if s["total"]]
    assert counted, "nothing ever reported a total to measure against"
    for reading in counted:
        assert reading["done"] <= reading["total"], reading
        assert reading["total"] <= ROWS + 5, "a total larger than the archive itself"


def test_a_workbook_that_cannot_be_built_says_why_rather_than_hanging(client):
    """A search matching nothing is the one a user reaches by accident."""
    started = prepare(client, q="nothing in this archive says this")
    final, _ = wait_for_ready(client, started["token"])

    assert final["state"] == "failed"
    assert "nothing to save" in final["message"]


# --- one at a time --------------------------------------------------------


def test_only_one_file_is_built_at_a_time(client, monkeypatch):
    """Two builds would write the same file in the exports folder."""
    from recall.export import selection

    holding = threading.Event()
    real = selection.export_search
    monkeypatch.setattr(
        selection, "export_search",
        lambda *a, **kw: (holding.wait(30), real(*a, **kw))[1],
    )

    first = prepare(client)
    try:
        second = client.post("/api/export/search/prepare", json={"format": "xlsx"})

        assert second.status_code == 409
        assert "already being prepared" in second.json()["detail"]
    finally:
        holding.set()

    wait_for_ready(client, first["token"])


def test_a_token_nobody_recognises_is_told_so_plainly(client):
    response = client.get("/api/export/search/progress", params={"token": "nonsense"})

    assert response.status_code == 404
    assert "exports folder" in response.json()["detail"], (
        "it says where the file would still be"
    )


def test_the_plain_link_still_works_without_a_token(client):
    """The CLI, the tests and anyone who bookmarked it take this road."""
    response = client.get("/api/export/search/download")

    assert response.status_code == 200, response.text
    assert response.content[:2] == b"PK"


# --- what the exporter reports, without the server in the way -------------


def test_the_exporter_reports_both_halves_of_the_wait(archive, settings):
    """Collecting the records is half the wait, and happens before any writing."""
    from recall.export.selection import export_search

    unset = object()
    seen: list[dict] = []

    def on_progress(*, done=None, total=unset, message=None, current=None):
        seen.append({"done": done, "total": total, "message": message})

    export_search(archive, settings, fmt="xlsx", on_progress=on_progress)

    messages = [s["message"] for s in seen if s["message"]]
    assert any("Collecting" in m for m in messages), messages
    assert any("Writing" in m for m in messages), messages
    # The save reports no total at all: there is nothing inside it to count.
    assert any(s["total"] is None for s in seen), "the save claimed a total"


def test_the_counts_climb_as_the_rows_go_in(archive, settings):
    """Within a step. Each step counts from nothing, as a job's phases do."""
    from recall.export.selection import export_search

    unset = object()
    step = "Collecting the records..."
    counts: dict[str, list[int]] = {}

    def on_progress(*, done=None, total=unset, message=None, current=None):
        nonlocal step
        if message is not None:
            step = message
        if done is not None:
            counts.setdefault(step, []).append(done)

    export_search(archive, settings, fmt="xlsx", on_progress=on_progress)

    for reported in (counts["Collecting the records..."],
                     counts["Writing the workbook..."]):
        assert len(reported) > 2, f"only {len(reported)} reading(s) for {ROWS} rows"
        assert max(reported) >= ROWS, "the last rows were never reported"
        assert reported == sorted(reported), f"it went backwards: {reported}"
