"""Choosing more files than SQLite will take parameters for.

A companion to test_large_result_sets, which covers the same limit on the
search and download paths. recall.db.IN_IDS was written for those and the
Sources screen was left on the old ``id IN (?,?,?,…)``, one parameter per file.

A drive scan of a machine with twenty years of mail on it finds tens of
thousands of .msg files, and the screen lets every one of them be ticked. Past
32,766 the plan, the download and the read all answered

    OperationalError: too many SQL variables

which the user saw as "Something went wrong inside Recall" on the screen whose
whole job is choosing files.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from recall.api.app import create_app

#: Comfortably over 32,766, and quick to write as bare rows.
MANY = 33_500


def sqlite_variable_limit() -> int:
    c = sqlite3.connect(":memory:")
    try:
        return c.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
    except AttributeError:            # pragma: no cover - Python < 3.11
        return 999
    finally:
        c.close()


@pytest.fixture(scope="module")
def big_settings(tmp_path_factory):
    from recall.config import Settings, WorkdirSettings

    root = tmp_path_factory.mktemp("bigsources")
    s = Settings(workdir=WorkdirSettings(path=str(root / "workdir")))
    s.source_path = root / "config.toml"
    s.ensure_workdir()
    s.extract.pst_backend = "pypff"
    s.extract.cross_check_backends = False
    return s


@pytest.fixture(scope="module")
def big_archive(big_settings):
    """More source files than SQLite will take parameters for.

    Half of them cloud-only, because the hydrate endpoints only look at those
    and a test where none matched would pass without touching the query.
    """
    from recall import db as db_module

    conn = db_module.connect(big_settings.db_path)
    conn.executemany(
        "INSERT INTO source_files(path, ext, size_bytes, is_placeholder, "
        "is_readable, parse_state) VALUES (?, '.msg', 2048, ?, 1, 'pending')",
        [(f"C:\\Mail\\letter-{i}.msg", 1 if i % 2 == 0 else 0) for i in range(MANY)],
    )
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def all_ids(big_archive) -> list[int]:
    return [int(r["id"]) for r in big_archive.execute("SELECT id FROM source_files")]


@pytest.fixture(scope="module")
def client(big_settings, big_archive):
    app = create_app(big_settings)
    with TestClient(app) as c:
        yield c


def test_the_archive_really_is_over_the_limit(all_ids):
    assert len(all_ids) > sqlite_variable_limit()


def test_planning_a_download_of_everything(client, all_ids):
    response = client.post("/api/sources/hydrate/plan", json={"ids": all_ids})
    assert response.status_code == 200
    assert len(response.json()["files"]) == len(all_ids) // 2


def test_starting_a_download_of_everything(client, all_ids):
    """Whatever it answers, it must be a sentence rather than a SQL error.

    The batch cap may well refuse a download this size, which is the cap doing
    its job. What must not happen is the query failing before anything has
    decided anything.
    """
    response = client.post(
        "/api/sources/hydrate", json={"ids": all_ids, "confirm": True}
    )
    assert response.status_code in (200, 400)
    if response.status_code == 400:
        assert "SQL variable" not in response.json()["detail"]


def test_the_reader_can_be_handed_every_id(big_settings, big_archive, all_ids):
    """Extractor._sources_to_read takes the same list by the same road.

    This is the one that matters most of the four: POST /api/sources/extract
    carries the ids in a JSON body, so a selection really can arrive here at
    any size. Half the rows are cloud-only with no local copy, and those are
    not reachable to read, so half is the right answer - the point is that the
    query runs at all.
    """
    from recall.extract import Extractor

    sources = Extractor(big_settings, big_archive)._sources_to_read(all_ids, True)
    assert len(sources) == len(all_ids) // 2


def test_planning_a_read_is_bounded_by_the_url_not_by_sql(client, all_ids):
    """/api/extract/plan takes its ids in the query string.

    A URL holding 33,500 of them is about 200 KB, which no browser or server
    will carry, so this path hits that limit long before SQLite's. It is on
    IN_IDS with the others for consistency; the honest test of it is a list
    that fits in a URL.
    """
    some = all_ids[:2000]
    response = client.get(
        "/api/extract/plan", params={"ids": ",".join(str(i) for i in some)}
    )
    assert response.status_code == 200
    assert response.json()["files"] == len(some) // 2
