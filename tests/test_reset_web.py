"""Emptying the archive from the Files found screen.

The CLI's ``reset --all`` deletes archive.db outright. The web server cannot:
it keeps one open SQLite connection per thread, and Windows refuses to unlink a
file that is still open. So the screen goes through a different door, and these
tests hold it shut on the two things that matter - that everything really goes,
and that the connection the server is holding still works afterwards.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from recall.api.app import create_app
from recall.api.jobs import JOBS


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def nothing_left_running():
    """JOBS is one object for the whole test run, not one per test.

    A job another test file started is still that object's job here, and the
    reset refuses while anything is running - so without this, these tests pass
    alone and fail in the suite. Waiting is not enough on its own: the
    16,000-file download in test_large_source_selections runs for minutes after
    the test that started it has returned, so it is asked to stop.
    """
    settle()
    yield
    settle()


def settle(timeout: float = 120.0) -> None:
    """Stop whatever is running and wait for the thread to actually end."""
    if JOBS.running:
        JOBS.request_cancel()
    deadline = time.monotonic() + timeout
    while JOBS.running and time.monotonic() < deadline:
        JOBS.join(1.0)
    assert not JOBS.running, (
        "a job started by another test is still running after being asked to "
        "stop; these tests cannot run against it"
    )


def fill(conn, settings, *, copies: bool = False) -> None:
    """A small archive with something in every corner of it."""
    conn.execute(
        "INSERT INTO scan_runs(id, started_utc, roots_json) "
        "VALUES (1, '2026-01-01T00:00:00Z', '[\"C:\\\\Mail\"]')"
    )
    conn.execute(
        "INSERT INTO source_files(id, path, container, ext, size_bytes, "
        "is_readable, parse_state, scan_run_id) "
        "VALUES (1, 'C:\\Mail\\one.pst', 'local', '.pst', 2048, 1, 'done', 1)"
    )
    # A duplicate pointing at the first file: source_files references itself,
    # which is exactly the shape that makes a full delete awkward.
    conn.execute(
        "INSERT INTO source_files(id, path, container, ext, size_bytes, "
        "is_readable, parse_state, duplicate_of, scan_run_id) "
        "VALUES (2, 'D:\\Backup\\one.pst', 'external', '.pst', 2048, 1, "
        "'done', 1, 1)"
    )
    if copies:
        conn.execute(
            "UPDATE source_files SET local_copy_path = ?, local_copy_bytes = 4096 "
            "WHERE id = 2",
            (str(settings.cloud_path / "one.pst"),),
        )
        (settings.cloud_path / "one.pst").write_bytes(b"x" * 16)

    conn.execute(
        "INSERT INTO items(id, kind, dedup_key, occurred_utc, subject) "
        "VALUES (1, 'message', 'k1', '2013-04-01T09:00:00Z', 'Hello')"
    )
    conn.execute("INSERT INTO item_sources(item_id, source_file_id) VALUES (1, 1)")
    conn.execute(
        "INSERT INTO search_docs(item_id, subject, body) VALUES (1, 'Hello', 'there')"
    )
    conn.execute("INSERT INTO people(id, display_name) VALUES (1, 'Ann Baker')")
    conn.execute(
        "INSERT INTO identities(id, person_id, address, address_type) "
        "VALUES (1, 1, 'ann@example.com', 'smtp')"
    )
    conn.execute(
        "INSERT INTO participations(item_id, person_id, identity_id, role) "
        "VALUES (1, 1, 1, 'from')"
    )
    conn.execute(
        "INSERT INTO attachments(id, item_id, filename, size_bytes, content_hash) "
        "VALUES (1, 1, 'note.pdf', 100, 'abc')"
    )
    conn.execute(
        "INSERT INTO eras(id, name, start_utc) "
        "VALUES (1, 'At the mill', '1998-01-01T00:00:00Z')"
    )
    # Pointed at a real file, item and person, which is how a finding about a
    # parser loss actually looks. A finding left dangling would not exercise
    # the order the tables have to be emptied in.
    conn.execute(
        "INSERT INTO findings(id, code, severity, title, state, "
        "source_file_id, item_id, person_id) "
        "VALUES (1, 'gap', 'warning', 'A gap', 'explained', 1, 1, 1)"
    )
    settings.blobs_path.mkdir(parents=True, exist_ok=True)
    (settings.blobs_path / "abc.bin").write_bytes(b"pdf")


# --- the plan tells the truth before anything goes ------------------------


def test_the_plan_counts_what_would_be_destroyed(client, conn, settings):
    fill(conn, settings)
    plan = client.get("/api/reset/plan").json()

    assert plan["files_found"] == 2
    assert plan["records"] == 1
    assert plan["people"] == 1
    assert plan["attachments"] == 1
    assert plan["eras"] == 1
    assert plan["explanations"] == 1
    assert plan["is_empty"] is False


def test_the_plan_deletes_nothing(client, conn, settings):
    fill(conn, settings)
    client.get("/api/reset/plan")

    assert conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 1
    assert (settings.blobs_path / "abc.bin").exists()


def test_an_empty_archive_says_so_rather_than_offering_a_number(client):
    plan = client.get("/api/reset/plan").json()

    assert plan["is_empty"] is True
    assert "nothing to delete" in plan["sentence"]


def test_the_downloaded_copies_are_counted_separately(client, conn, settings):
    fill(conn, settings, copies=True)
    plan = client.get("/api/reset/plan").json()

    # They cost bandwidth to fetch and the dialog has to be able to say so.
    assert plan["copies"] == 1
    assert plan["copies_bytes"] == 4096


# --- the reset itself ------------------------------------------------------


def test_nothing_is_deleted_without_the_agreement(client, conn, settings):
    fill(conn, settings)
    response = client.post("/api/reset", json={})

    assert response.status_code == 400
    assert conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 1


def test_everything_goes(client, conn, settings):
    fill(conn, settings, copies=True)
    body = client.post("/api/reset", json={"confirm": True}).json()

    assert body["reset"] is True
    for table in (
        "scan_runs", "source_files", "items", "item_sources", "people",
        "participations", "identities", "attachments", "search_docs", "eras",
        "findings",
    ):
        n = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        assert n == 0, f"{table} still has {n} row(s)"


def test_the_saved_files_go_with_the_rows(client, conn, settings):
    fill(conn, settings, copies=True)
    client.post("/api/reset", json={"confirm": True})

    assert not (settings.blobs_path / "abc.bin").exists()
    assert not (settings.cloud_path / "one.pst").exists()
    # The folders themselves stay, so the next run has somewhere to write.
    assert settings.blobs_path.is_dir()
    assert settings.cloud_path.is_dir()


def test_the_search_index_is_emptied_too(client, conn, settings):
    fill(conn, settings)
    client.post("/api/reset", json={"confirm": True})

    n = conn.execute(
        "SELECT COUNT(*) AS n FROM items_fts WHERE items_fts MATCH 'Hello'"
    ).fetchone()["n"]
    assert n == 0


def test_the_server_still_works_afterwards(client, conn, settings):
    """The whole reason this does not delete the file."""
    fill(conn, settings)
    client.post("/api/reset", json={"confirm": True})

    listing = client.get("/api/sources")
    summary = client.get("/api/sources/summary")

    assert listing.status_code == 200
    assert listing.json()["total"] == 0
    assert summary.status_code == 200


def test_the_screen_is_told_what_went(client, conn, settings):
    fill(conn, settings, copies=True)
    body = client.post("/api/reset", json={"confirm": True}).json()

    assert body["deleted"]["sources"] == 2
    assert body["deleted"]["items"] == 1
    assert body["deleted"]["attachments"] == 1
    assert body["deleted"]["copies"] == 1
    assert "not touched" in body["sentence"]


def test_a_second_reset_on_an_empty_archive_is_harmless(client):
    assert client.post("/api/reset", json={"confirm": True}).status_code == 200
    assert client.post("/api/reset", json={"confirm": True}).status_code == 200


# --- not while something is running ---------------------------------------


def test_a_running_job_stops_the_reset(client, conn, settings):
    """Half an archive and no way to tell which half is the failure here."""
    import threading

    fill(conn, settings)
    release = threading.Event()
    JOBS.start("scan", lambda job: release.wait(10))
    try:
        response = client.post("/api/reset", json={"confirm": True})
    finally:
        release.set()
        JOBS.join(10)

    assert response.status_code == 409
    assert conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 1
