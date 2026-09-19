"""Choosing one folder to search, instead of a whole drive.

Someone who knows their old mail is in D:\\Archive should not have to search
300 GB to reach it. These tests cover the folder chooser and the two scanning
rules a chooser makes matter: a folder picked by hand really is searched, and
picking a drive and a folder on it does not search the folder twice.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from recall.api.app import create_app
from recall.db import get_setting
from recall.scan.browse import (
    RECENT_KEY, RECENT_LIMIT, list_folder, recent_folders, remember_folders,
)
from recall.scan.walker import Scanner, collapse_nested_roots

PST_HEADER = b"!BDNsomething" + b"\0" * 600


@pytest.fixture
def here(tmp_path: Path) -> Path:
    """A folder of our own.

    The settings fixture puts its workdir straight into tmp_path, so listing
    tmp_path itself would see it and every count would be one out.
    """
    folder = tmp_path / "chosen"
    folder.mkdir()
    return folder


@pytest.fixture
def scan_settings(settings):
    """Settings with the default never-search list, which these tests exercise."""
    return settings


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


# --- listing what is inside a folder --------------------------------------


def test_no_path_offers_the_drives(settings):
    listing = list_folder(None, settings)
    assert listing.path is None
    assert listing.label == "This computer"
    assert listing.entries, "this computer has at least one drive"
    assert all(e.path.endswith((":\\", ":/")) for e in listing.entries)


def test_a_folder_lists_the_folders_inside_it(here: Path, settings):
    (here / "Letters").mkdir()
    (here / "Invoices").mkdir()
    (here / "notes.txt").write_text("not a folder")

    listing = list_folder(str(here), settings)

    assert listing.readable
    assert [e.name for e in listing.entries] == ["Invoices", "Letters"]


def test_files_are_not_listed(here: Path, settings):
    """This is a folder chooser. Listing files the user cannot pick is noise."""
    (here / "archive.pst").write_bytes(PST_HEADER)
    assert list_folder(str(here), settings).entries == []


def test_folders_are_sorted_so_the_eye_can_find_one(here: Path, settings):
    for name in ("zebra", "Apple", "mango"):
        (here / name).mkdir()
    listing = list_folder(str(here), settings)
    assert [e.name for e in listing.entries] == ["Apple", "mango", "zebra"]


def test_a_folder_that_is_gone_says_so_rather_than_failing(tmp_path: Path, settings):
    listing = list_folder(str(tmp_path / "nowhere"), settings)
    assert listing.readable is False
    assert "not on this computer" in listing.note


def test_a_file_given_where_a_folder_belongs_says_so(tmp_path: Path, settings):
    target = tmp_path / "archive.pst"
    target.write_bytes(PST_HEADER)

    listing = list_folder(str(target), settings)

    assert listing.readable is False
    assert "a file, not a folder" in listing.note
    assert listing.parent == str(tmp_path), "so the user can step up to where it lives"


def test_a_folder_windows_will_not_open_is_an_answer_not_a_crash(
    tmp_path: Path, settings, monkeypatch
):
    """The same rule the rest of Recall follows: record it, do not fall over."""
    import os

    real = os.scandir

    def refuse(path):
        if str(path) == str(tmp_path):
            raise PermissionError(13, "Access is denied")
        return real(path)

    monkeypatch.setattr(os, "scandir", refuse)

    listing = list_folder(str(tmp_path), settings)

    assert listing.readable is False
    assert "permission" in listing.note.lower()
    assert listing.entries == []


def test_an_empty_folder_can_still_be_searched(here: Path, settings):
    listing = list_folder(str(here), settings)
    assert listing.readable is True
    assert "can still search it" in listing.note


def test_the_path_is_offered_as_clickable_pieces(tmp_path: Path, settings):
    (tmp_path / "Letters").mkdir()
    listing = list_folder(str(tmp_path / "Letters"), settings)

    assert listing.crumbs, "no breadcrumb to climb back out"
    assert listing.crumbs[-1].path == str(tmp_path / "Letters")
    assert listing.parent == str(tmp_path)


def test_a_normally_skipped_folder_is_labelled(here: Path, settings):
    """So the user is not surprised either way about what it will do."""
    (here / "node_modules").mkdir()
    (here / "Letters").mkdir()

    by_name = {e.name: e for e in list_folder(str(here), settings).entries}

    assert by_name["node_modules"].excluded_by_default is True
    assert by_name["Letters"].excluded_by_default is False


# --- through the API ------------------------------------------------------


def test_the_browse_endpoint_returns_the_drives(client):
    body = client.get("/api/folders").json()
    assert body["path"] is None
    assert body["entries"]


def test_the_browse_endpoint_lists_a_folder(client, here: Path):
    (here / "Letters").mkdir()
    body = client.get("/api/folders", params={"path": str(here)}).json()

    assert body["readable"] is True
    assert [e["name"] for e in body["entries"]] == ["Letters"]


def test_the_browse_endpoint_does_not_500_on_a_bad_path(client):
    response = client.get("/api/folders", params={"path": "Z:\\nope\\nowhere"})
    assert response.status_code == 200
    assert response.json()["readable"] is False


# --- remembering folders --------------------------------------------------


def test_a_chosen_folder_is_remembered(conn, tmp_path: Path):
    remember_folders(conn, [str(tmp_path)])
    assert recent_folders(conn) == [str(tmp_path)]


def test_the_newest_choice_comes_first(conn, tmp_path: Path):
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()

    remember_folders(conn, [str(first)])
    remember_folders(conn, [str(second)])

    assert recent_folders(conn) == [str(second), str(first)]


def test_choosing_the_same_folder_twice_does_not_list_it_twice(conn, tmp_path: Path):
    remember_folders(conn, [str(tmp_path)])
    remember_folders(conn, [str(tmp_path)])
    assert recent_folders(conn) == [str(tmp_path)]


def test_drives_are_not_remembered(conn):
    """They are always on the chooser; a recent list full of C:\\ helps nobody."""
    remember_folders(conn, ["C:\\"])
    assert recent_folders(conn) == []


def test_the_list_does_not_grow_without_end(conn, tmp_path: Path):
    for i in range(RECENT_LIMIT + 5):
        folder = tmp_path / f"folder{i}"
        folder.mkdir()
        remember_folders(conn, [str(folder)])

    assert len(recent_folders(conn)) == RECENT_LIMIT


def test_a_folder_that_has_since_gone_is_not_offered(conn, tmp_path: Path):
    folder = tmp_path / "temporary"
    folder.mkdir()
    remember_folders(conn, [str(folder)])
    folder.rmdir()

    assert recent_folders(conn) == []


def test_an_unreadable_memory_starts_again_rather_than_failing(conn):
    from recall.db import set_setting

    set_setting(conn, RECENT_KEY, "{not json at all")
    assert recent_folders(conn) == []


def test_scanning_a_folder_records_it_for_next_time(client, tmp_path: Path, conn):
    (tmp_path / "archive.pst").write_bytes(PST_HEADER)

    assert client.post("/api/scan", json={"roots": [str(tmp_path)]}).status_code == 200

    stored = json.loads(get_setting(conn, RECENT_KEY))
    assert str(tmp_path) in stored


# --- what a scan actually covers ------------------------------------------


def test_a_file_cannot_be_submitted_as_a_folder_to_search(client, tmp_path: Path):
    target = tmp_path / "archive.pst"
    target.write_bytes(PST_HEADER)

    response = client.post("/api/scan", json={"roots": [str(target)]})

    assert response.status_code == 400
    assert str(target) in response.json()["detail"]


def test_a_folder_chosen_by_hand_is_searched_even_if_normally_skipped(
    tmp_path: Path, scan_settings, conn
):
    """The failure this prevents is silence.

    A whole-drive sweep skips node_modules for good reason. Pointing at one
    deliberately and getting nothing back, with nothing said, would be the
    worst kind of wrong answer.
    """
    root = tmp_path / "node_modules"
    root.mkdir()
    (root / "archive.pst").write_bytes(PST_HEADER)

    Scanner(scan_settings, conn).run([root])

    found = conn.execute("SELECT COUNT(*) AS n FROM source_files").fetchone()["n"]
    assert found == 1


def test_a_skipped_folder_underneath_is_still_skipped(tmp_path: Path, scan_settings, conn):
    """Choosing a folder does not turn the never-search list off below it."""
    root = tmp_path / "data"
    (root / "node_modules").mkdir(parents=True)
    (root / "archive.pst").write_bytes(PST_HEADER)
    (root / "node_modules" / "buried.pst").write_bytes(PST_HEADER)

    Scanner(scan_settings, conn).run([root])

    found = [Path(r["path"]).name for r in conn.execute("SELECT path FROM source_files")]
    assert found == ["archive.pst"]


def test_a_folder_inside_another_chosen_folder_is_not_searched_twice(tmp_path: Path):
    parent = tmp_path / "data"
    child = parent / "letters"
    child.mkdir(parents=True)

    keep, covered = collapse_nested_roots([parent, child])

    assert keep == [parent]
    assert covered == [str(child)]


def test_the_order_the_user_chose_is_kept(tmp_path: Path):
    second = tmp_path / "b"
    first = tmp_path / "a"
    for p in (first, second):
        p.mkdir()

    keep, covered = collapse_nested_roots([second, first])

    assert keep == [second, first]
    assert covered == []


def test_a_parent_wins_however_the_roots_are_ordered(tmp_path: Path):
    parent = tmp_path / "data"
    child = parent / "letters"
    child.mkdir(parents=True)

    keep, covered = collapse_nested_roots([child, parent])

    assert keep == [parent], "the child must not survive just by coming first"
    assert covered == [str(child)]


def test_two_separate_folders_are_both_kept(tmp_path: Path):
    one = tmp_path / "one"
    two = tmp_path / "two"
    for p in (one, two):
        p.mkdir()

    keep, covered = collapse_nested_roots([one, two])

    assert keep == [one, two]
    assert covered == []


def test_the_same_folder_twice_is_searched_once(tmp_path: Path):
    keep, covered = collapse_nested_roots([tmp_path, tmp_path])
    assert keep == [tmp_path]
    assert covered == [str(tmp_path)]


def test_a_collapsed_root_is_reported_not_silently_dropped(
    tmp_path: Path, scan_settings, conn
):
    parent = tmp_path / "data"
    child = parent / "letters"
    child.mkdir(parents=True)
    (child / "archive.pst").write_bytes(PST_HEADER)

    progress = Scanner(scan_settings, conn).run([parent, child])

    assert progress.covered_roots == [str(child)]
    assert str(child) not in progress.roots
