"""The Problems queue, a page at a time.

The counts matter more here than anywhere else in the application. This is the
screen whose entire job is saying truthfully what is wrong with the archive, so
a heading reading "High (10)" because a page happens to hold ten of them would
be its own small lie - and a nested heading reading "Files that could not be
read (86)" underneath "Medium", when only five of those eighty-six are medium,
would be a larger one.

Both are counted across the whole filtered set, and the nested one is counted
within its severity. These tests hold that.
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


def add_finding(conn, *, code: str, severity: str, title: str, key: str) -> int:
    """One finding.

    ``key`` goes into period_start only to make each row distinct: findings
    carry a unique index over (code, file, item, person, period) so that
    re-running the checks updates a finding rather than duplicating it, and
    twelve rows with nothing but a code would all collide on it.
    """
    cur = conn.execute(
        "INSERT INTO findings(code, severity, state, title, detail, period_start, "
        "first_seen_utc) VALUES (?, ?, 'open', ?, '', ?, '2026-01-01T00:00:00Z')",
        (code, severity, title, key),
    )
    return int(cur.lastrowid)


@pytest.fixture
def queue(conn):
    """A queue shaped like a real one: lopsided, spanning several severities.

    Eighty-one unreadable files is not invented - it is what a real drive scan
    produced, and it is exactly the shape that makes paging necessary.
    """
    for i in range(12):
        add_finding(conn, code="magic_mismatch", severity="high",
                    title=f"File {i}", key=f"mm{i}")
    for i in range(4):
        add_finding(conn, code="hard_gap", severity="high",
                    title=f"Gap {i}", key=f"2003-{i + 1:02d}")
    # The same class as magic_mismatch, but medium - the case that catches a
    # class total counted across every severity instead of within one.
    for i in range(3):
        add_finding(conn, code="zero_or_tiny", severity="medium",
                    title=f"Tiny {i}", key=f"zt{i}")
    for i in range(2):
        add_finding(conn, code="no_date", severity="medium",
                    title=f"Undated {i}", key=f"nd{i}")
    add_finding(conn, code="under_merged", severity="info",
                title="Maybe one person", key="um0")
    return conn


# --- paging ---------------------------------------------------------------


def test_the_total_is_everything_that_matched_not_the_page(client, queue):
    body = client.get("/api/findings", params={"limit": 5}).json()

    assert len(body["findings"]) == 5
    assert body["count"] == 5, "count is what came back"
    assert body["total"] == 22, "total is what matched"


def test_paging_walks_the_whole_queue_without_repeating(client, queue):
    seen: list[int] = []
    offset = 0
    while True:
        body = client.get("/api/findings", params={"limit": 5, "offset": offset}).json()
        if not body["findings"]:
            break
        seen.extend(f["id"] for f in body["findings"])
        offset += 5

    assert len(seen) == 22
    assert len(set(seen)) == 22, "a finding appeared on two pages"


def test_the_order_is_the_same_on_every_page(client, queue):
    """Paging a differently-ordered list shuffles findings between pages."""
    everything = [f["id"] for f in client.get(
        "/api/findings", params={"limit": 500}).json()["findings"]]

    paged: list[int] = []
    for offset in range(0, 25, 5):
        paged.extend(f["id"] for f in client.get(
            "/api/findings", params={"limit": 5, "offset": offset}).json()["findings"])

    assert paged == everything


def test_severity_still_comes_first(client, queue):
    body = client.get("/api/findings", params={"limit": 500}).json()
    order = [f["severity"] for f in body["findings"]]
    rank = {"critical": 0, "high": 1, "medium": 2, "info": 3}
    assert order == sorted(order, key=lambda s: rank[s])


def test_a_page_past_the_end_is_empty_rather_than_an_error(client, queue):
    body = client.get("/api/findings", params={"limit": 5, "offset": 500}).json()
    assert body["findings"] == []
    assert body["total"] == 22, "the total still tells the truth"


def test_the_queue_cannot_be_asked_for_everything_at_once(client, queue):
    body = client.get("/api/findings", params={"limit": 100_000}).json()
    assert body["limit"] == 500


# --- the counts the headings are built from -------------------------------


def test_severity_totals_cover_the_whole_set(client, queue):
    body = client.get("/api/findings", params={"limit": 3}).json()

    assert body["totals"]["by_severity"] == {"high": 16, "medium": 5, "info": 1}
    assert sum(body["totals"]["by_severity"].values()) == body["total"]


def test_a_class_is_counted_inside_its_severity(client, queue):
    """The bug this guards is a real one, and it was on screen.

    magic_mismatch and zero_or_tiny are both "files that could not be read".
    Counting that class across every severity puts 15 under the Medium
    heading, when only 3 of them are medium.
    """
    nested = client.get("/api/findings", params={"limit": 3}).json()["totals"]["by_severity_class"]

    assert nested["high"]["unreadable_files"] == 12
    assert nested["medium"]["unreadable_files"] == 3, (
        "the medium heading must not inherit the high ones"
    )
    assert nested["high"]["coverage_gaps"] == 4
    assert nested["medium"]["record_quality"] == 2
    assert nested["info"]["duplicate_accounts"] == 1


def test_the_nested_totals_add_up_to_the_severity_totals(client, queue):
    totals = client.get("/api/findings", params={"limit": 3}).json()["totals"]

    for severity, classes in totals["by_severity_class"].items():
        assert sum(classes.values()) == totals["by_severity"][severity], (
            f"{severity} does not add up"
        )


def test_the_totals_do_not_move_when_the_page_does(client, queue):
    first = client.get("/api/findings", params={"limit": 5, "offset": 0}).json()
    last = client.get("/api/findings", params={"limit": 5, "offset": 20}).json()

    assert first["total"] == last["total"]
    assert first["totals"] == last["totals"]


def test_the_totals_follow_the_filters(client, queue):
    body = client.get("/api/findings", params={"severity": "medium"}).json()

    assert body["total"] == 5
    assert body["totals"]["by_severity"] == {"medium": 5}
    assert "high" not in body["totals"]["by_severity_class"]


def test_an_empty_queue_reports_nothing_rather_than_failing(client):
    body = client.get("/api/findings").json()

    assert body["findings"] == []
    assert body["total"] == 0
    assert body["totals"]["by_severity"] == {}
    assert body["totals"]["by_severity_class"] == {}
