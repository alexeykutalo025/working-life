"""What happens when a search matches more records than SQLite has room for.

SQLite takes at most SQLITE_LIMIT_VARIABLE_NUMBER host parameters in one
statement - 32,766 on a current build, and 999 on an older one. Every path here
used to gather the matching ids in Python and hand them back as
``id IN (?,?,?,…)``, one parameter per record. That works on a test archive of
twenty records and stops working on a real one.

It stopped on the client's. A search matching 42,767 records answered

    OperationalError: too many SQL variables

where the table should have been, and the Download button beside it would have
done the same, because it takes the same road.

So these tests are all the same test: build an archive bigger than the limit,
and ask for it. They are slower than the rest of the suite because there is no
way to reproduce a limit on the number of records except with that many
records.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from recall.api.app import create_app

#: Comfortably over 32,766, and small enough to build in a second or two.
MANY = 33_500


def sqlite_variable_limit() -> int:
    c = sqlite3.connect(":memory:")
    try:
        return c.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
    except AttributeError:            # Python < 3.11
        return 999
    finally:
        c.close()


@pytest.fixture(scope="module")
def limit() -> int:
    return sqlite_variable_limit()


@pytest.fixture(scope="module")
def big_settings(tmp_path_factory):
    """The conftest fixtures, at module scope.

    Thirty-three thousand records take a few seconds to write and rather
    longer to index, and building that nine times over would add minutes to
    every run of the suite for no extra coverage. Nothing below writes to the
    archive, so one is enough.
    """
    from recall.config import Settings, WorkdirSettings

    root = tmp_path_factory.mktemp("big")
    s = Settings(workdir=WorkdirSettings(path=str(root / "workdir")))
    s.source_path = root / "config.toml"
    s.ensure_workdir()
    s.extract.pst_backend = "pypff"
    s.extract.cross_check_backends = False
    return s


@pytest.fixture(scope="module")
def big_archive(big_settings):
    """More messages than SQLite will take parameters for."""
    from recall import db as db_module

    conn = db_module.connect(big_settings.db_path)
    conn.executemany(
        "INSERT INTO items(kind, dedup_key, occurred_utc, subject, body_text) "
        "VALUES ('message', ?, ?, ?, 'The lease and the rent review.')",
        [
            (f"message:{i}", f"2003-04-{i % 28 + 1:02d}T09:00:00Z", f"Letter {i}")
            for i in range(MANY)
        ],
    )
    # A second kind, so the table has to count per kind as well as list.
    conn.executemany(
        "INSERT INTO items(kind, dedup_key, occurred_utc, subject) "
        "VALUES ('event', ?, ?, ?)",
        [(f"event:{i}", f"2004-05-{i % 28 + 1:02d}T09:00:00Z", f"Meeting {i}")
         for i in range(10)],
    )
    conn.commit()

    # The search path is the one a client actually uses, and it goes through
    # the index rather than the items table, so the index has to exist.
    from recall.search.indexer import build_index

    build_index(conn)
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def client(big_settings, big_archive):
    app = create_app(big_settings)
    with TestClient(app) as c:
        yield c


def test_the_archive_really_is_over_the_limit(big_archive, limit):
    """Otherwise every test below would pass without proving anything."""
    n = big_archive.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]

    assert n > limit, f"{n} records is not more than SQLite's {limit} parameters"


# --- the screen -----------------------------------------------------------


def test_the_table_answers_for_an_archive_bigger_than_the_limit(client):
    """This is the failure in the screenshot: a red box instead of a table."""
    response = client.get("/api/search/table", params={"limit": 50})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["matched_total"] > MANY
    assert len(body["rows"]) == 50


def test_the_counts_per_kind_are_still_right_at_that_size(client):
    body = client.get("/api/search/table").json()
    counts = {k["kind"]: k["count"] for k in body["kinds"]}

    assert counts["message"] == MANY
    assert counts["event"] == 10


def test_paging_still_works_at_that_size(client):
    first = client.get("/api/search/table", params={"limit": 25}).json()
    second = client.get(
        "/api/search/table", params={"limit": 25, "offset": 25}
    ).json()

    ids = {r["item_id"] for r in first["rows"]}
    assert len(ids) == 25
    assert not ids & {r["item_id"] for r in second["rows"]}


def test_a_search_that_matches_everything_still_answers(client):
    """The FTS path, which is the one a client actually uses."""
    response = client.get("/api/search/table", params={"q": "lease"})

    assert response.status_code == 200, response.text
    assert response.json()["matched_total"] > 0


# --- the download beside it -----------------------------------------------


def test_the_workbook_download_survives_the_same_archive(client):
    response = client.get("/api/export/search/download")

    assert response.status_code == 200, response.text[:400]
    assert len(response.content) > 10_000


def test_exporting_writes_every_record_it_matched(big_archive, big_settings):
    from recall.export.selection import export_search

    result = export_search(big_archive, big_settings, fmt="xlsx")

    assert result["total_records"] == MANY + 10


def test_a_csv_export_survives_it_too(big_archive, big_settings):
    from recall.export.selection import export_search

    result = export_search(big_archive, big_settings, fmt="csv", kind="message")

    assert result["total_records"] == MANY
    assert Path(result["files"][0]["file"]).exists()


# --- the integrity statement the export cannot be written without ---------


def test_the_statement_can_be_built_for_that_many_records(big_archive, big_settings):
    from recall.export.base import ExportSelection, build_statement

    ids = [int(r["id"]) for r in big_archive.execute("SELECT id FROM items")]
    statement = build_statement(
        big_archive, ExportSelection(item_ids=ids), len(ids), big_settings.db_path
    )

    assert statement.exported_count == len(ids)


# --- the scan, which meets the same wall from the other direction ---------


def test_a_scan_with_more_duplicate_groups_than_the_limit_finishes(conn, limit):
    """Decades of saved .msg files is a great many duplicate groups.

    This one is reached by the number of *files*, not records, so it needs its
    own small archive rather than the big one above.
    """
    from recall.scan.fingerprint import DuplicateGroup, mark_duplicates

    n = limit + 40
    conn.executemany(
        "INSERT INTO source_files(path, ext, is_readable) VALUES (?, '.msg', 1)",
        [(f"C:\\{where}\\keep{i}.msg",) for i in range(n) for where in ("Mail", "Backup")],
    )
    ids = {
        str(r["path"]): int(r["id"])
        for r in conn.execute("SELECT id, path FROM source_files")
    }
    groups = [
        DuplicateGroup(
            content_hash=f"hash{i}",
            keeper_id=ids[f"C:\\Mail\\keep{i}.msg"],
            keeper_path=f"C:\\Mail\\keep{i}.msg",
            duplicate_ids=[ids[f"C:\\Backup\\keep{i}.msg"]],
            size_bytes=1024,
        )
        for i in range(n)
    ]

    assert mark_duplicates(conn, groups) == len(groups)

    still_keepers = conn.execute(
        "SELECT COUNT(*) AS n FROM source_files "
        "WHERE path LIKE 'C:\\Mail\\%' AND duplicate_of IS NULL"
    ).fetchone()["n"]
    assert still_keepers == len(groups), "a keeper is never a duplicate"


# --- the rule itself ------------------------------------------------------


def test_an_id_list_is_one_parameter_however_long_it_is(conn, limit):
    """The whole fix in one line, so the reason for it cannot get lost."""
    from recall.db import IN_IDS, ids_param

    ids = list(range(1, limit + 1000))
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM (SELECT 1 WHERE 7 {IN_IDS})",
        (ids_param(ids),),
    ).fetchone()

    assert row["n"] == 1


def test_an_id_list_keeps_ids_as_numbers(conn):
    """A text "7" would match nothing, and match it silently."""
    from recall.db import IN_IDS, ids_param

    found = conn.execute(
        f"SELECT typeof(value) AS t FROM json_each(?) LIMIT 1", (ids_param([7]),)
    ).fetchone()["t"]

    assert found == "integer"
    assert conn.execute(
        f"SELECT 1 AS hit WHERE 7 {IN_IDS}", (ids_param([7]),)
    ).fetchone() is not None
