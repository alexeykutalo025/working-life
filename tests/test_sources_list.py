"""The Files found listing, now that it is paged.

Two things the screen depends on and nothing was holding:

Paging must be *stable*. Nine files all the same size is ordinary - Outlook
writes plenty of small identical-looking data files - and if the sort column
alone decides the order, SQLite is free to return them differently on each
query. Page one and page two would then overlap and something would never be
offered at all. The `, sf.id` tie-break is what stops that, and this pins it.

And `limit` must be clamped. Every other listing does; this one took the number
straight into SQL, where a negative LIMIT means "no limit" - so `?limit=-1`
handed back every Outlook file on the machine.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from recall.api.app import create_app


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def add_files(conn, n: int, *, size: int = 1024) -> None:
    """n files that are deliberately indistinguishable by the sort column."""
    for i in range(n):
        conn.execute(
            "INSERT INTO source_files(path, container, ext, size_bytes, "
            "mtime_utc, is_readable, parse_state) "
            "VALUES (?, 'local', '.pst', ?, '2026-01-01T00:00:00Z', 1, 'pending')",
            (f"C:\\Mail\\archive{i:03d}.pst", size),
        )


def page(client, **params):
    return client.get("/api/sources", params=params).json()


# --- paging ---------------------------------------------------------------


def test_the_total_counts_every_file_not_the_page(client, conn):
    add_files(conn, 9)
    body = page(client, limit=4)

    assert body["total"] == 9
    assert len(body["rows"]) == 4


def test_the_pages_together_are_the_whole_list(client, conn):
    add_files(conn, 9)
    seen = []
    for offset in (0, 4, 8):
        seen += [r["id"] for r in page(client, limit=4, offset=offset)["rows"]]

    assert len(seen) == 9
    assert len(set(seen)) == 9, "a file appearing twice means another appears never"


def test_files_of_identical_size_still_page_without_overlapping(client, conn):
    """Sorting on size alone leaves the order up to SQLite. It must not be."""
    add_files(conn, 9, size=1024)

    first = [r["id"] for r in page(client, sort="size", limit=4)["rows"]]
    second = [r["id"] for r in page(client, sort="size", limit=4, offset=4)["rows"]]

    assert not set(first) & set(second)


def test_asking_past_the_end_is_an_empty_page_not_an_error(client, conn):
    add_files(conn, 9)
    body = page(client, limit=4, offset=400)

    assert body["rows"] == []
    assert body["total"] == 9, "the total still describes the whole list"


# --- the clamp ------------------------------------------------------------


def test_a_negative_limit_does_not_mean_the_whole_table(client, conn):
    """In SQL a negative LIMIT means no limit at all. That is the bug."""
    add_files(conn, 9)
    body = page(client, limit=-1)

    assert len(body["rows"]) == 1
    assert body["limit"] == 1


def test_an_enormous_limit_is_cut_to_a_page(client, conn):
    add_files(conn, 9)
    body = page(client, limit=100_000)

    assert body["limit"] == 500


def test_a_negative_offset_starts_at_the_beginning(client, conn):
    add_files(conn, 9)
    body = page(client, limit=4, offset=-20)

    assert body["offset"] == 0
    assert len(body["rows"]) == 4
