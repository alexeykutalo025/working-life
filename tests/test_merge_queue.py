"""The merge review queue, and the refusal that has to stick.

Spec 9.3: Recall proposes, the user decides, and nothing is merged or split
automatically. A decision the user makes is therefore the most valuable thing
on this screen, and until now "No, they are different people" was not being
recorded anywhere at all - the screen read a `finding_id` that nothing wrote.

Wiring it up turned out to need the finding keyed by the *pair*. Findings are
identified by (code, file, item, person, period), so hanging one off a single
person id produced one row per person rather than one per pair: two hundred
suggestions collapsed into six findings, and turning one down turned down every
other pair that person appeared in. These tests hold the pair as the key.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from recall.api.app import create_app
from recall.integrity.accounts import pair_key


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def add_person(conn, name: str, address: str) -> int:
    cur = conn.execute(
        "INSERT INTO people(display_name, item_count) VALUES (?, 1)", (name,)
    )
    person = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO identities(person_id, address, address_type, use_count) "
        "VALUES (?, ?, 'smtp', 1)",
        (person, address),
    )
    return person


def add_under_merged(conn, a: int, b: int, *, state: str = "open") -> int:
    """A finding shaped exactly as accounts._check_under_merged writes one."""
    cur = conn.execute(
        "INSERT INTO findings(code, severity, state, title, detail, person_id, "
        "period_start, first_seen_utc) "
        "VALUES ('under_merged', 'info', ?, ?, '', ?, ?, '2026-01-01T00:00:00Z')",
        (state, f"person {a} and person {b}", min(a, b), pair_key(a, b)),
    )
    return int(cur.lastrowid)


# --- the key --------------------------------------------------------------


def test_the_key_is_the_pair_not_the_person():
    assert pair_key(4, 9) == "4:9"


def test_the_key_does_not_care_which_way_round_the_pair_arrives():
    """A proposal may name either person first; the finding must not move."""
    assert pair_key(9, 4) == pair_key(4, 9)


def test_two_pairs_sharing_a_person_get_different_keys():
    """The whole bug in one line: these used to collide on person 4."""
    assert pair_key(4, 9) != pair_key(4, 12)


def test_a_pair_key_survives_a_round_trip_through_the_finding(conn):
    a = add_person(conn, "Margaret Hale", "m.hale@example.com")
    b = add_person(conn, "Margaret Hale", "margaret@example.com")
    add_under_merged(conn, a, b)

    stored = conn.execute(
        "SELECT period_start FROM findings WHERE code = 'under_merged'"
    ).fetchone()["period_start"]
    assert stored == pair_key(a, b)


def test_two_pairs_sharing_a_person_are_two_findings(conn):
    """They used to be one row, the second overwriting the first."""
    a = add_person(conn, "Margaret Hale", "m.hale@example.com")
    b = add_person(conn, "Margaret Hale", "margaret@example.com")
    c = add_person(conn, "Margaret Hale", "mh@example.com")

    add_under_merged(conn, a, b)
    add_under_merged(conn, a, c)

    n = conn.execute(
        "SELECT COUNT(*) AS n FROM findings WHERE code = 'under_merged'"
    ).fetchone()["n"]
    assert n == 2


# --- what the queue does with them ----------------------------------------


def test_a_proposal_carries_the_id_of_its_own_finding(client, conn, monkeypatch):
    _two_proposals(conn, monkeypatch)
    first = client.get("/api/people/merge-queue").json()["proposals"][0]

    assert first["evidence"]["finding_id"] is not None


def test_every_proposal_has_a_finding_of_its_own(client, conn, monkeypatch):
    _two_proposals(conn, monkeypatch)
    proposals = client.get("/api/people/merge-queue").json()["proposals"]

    ids = [p["evidence"]["finding_id"] for p in proposals]
    assert len(ids) == 2
    assert len(set(ids)) == 2, "two suggestions sharing one finding is the old bug"


def test_a_refused_pair_is_not_offered_again(client, conn, monkeypatch):
    pairs = _two_proposals(conn, monkeypatch)
    refused = pairs[0]

    conn.execute(
        "UPDATE findings SET state = 'wont_fix' WHERE period_start = ?",
        (pair_key(*refused),),
    )

    body = client.get("/api/people/merge-queue").json()
    assert body["total"] == 1
    assert body["dismissed"] == 1


def test_refusing_one_pair_does_not_refuse_the_other(client, conn, monkeypatch):
    """The failure this guards cost 194 suggestions in one click."""
    pairs = _two_proposals(conn, monkeypatch)
    conn.execute(
        "UPDATE findings SET state = 'wont_fix' WHERE period_start = ?",
        (pair_key(*pairs[0]),),
    )

    proposals = client.get("/api/people/merge-queue").json()["proposals"]

    survivors = {pair_key(p["person_a"], p["person_b"]) for p in proposals}
    assert survivors == {pair_key(*pairs[1])}


def test_nothing_refused_means_nothing_reported_as_refused(client, conn, monkeypatch):
    _two_proposals(conn, monkeypatch)
    body = client.get("/api/people/merge-queue").json()

    assert body["dismissed"] == 0
    assert body["total"] == body["count"] == 2


def test_an_empty_queue_is_an_answer_not_a_failure(client):
    body = client.get("/api/people/merge-queue").json()

    assert body["proposals"] == []
    assert body["total"] == 0
    assert body["note"]


# ---------------------------------------------------------------------------


def _two_proposals(conn, monkeypatch):
    """Two suggestions that deliberately share a person.

    suggest_merges is an O(n squared) pass over real names; the queue's own
    behaviour is what is under test here, so it is fed directly.
    """
    from recall.normalize.merge import MergeProposal

    a = add_person(conn, "Margaret Hale", "m.hale@example.com")
    b = add_person(conn, "Margaret Hale", "margaret@example.com")
    c = add_person(conn, "Margaret Hale", "mh@example.com")

    add_under_merged(conn, a, b)
    add_under_merged(conn, a, c)

    made = [
        MergeProposal(
            person_a=a, person_b=b, confidence=0.9,
            reason="Their names are the same", evidence={},
        ),
        MergeProposal(
            person_a=a, person_b=c, confidence=0.8,
            reason="Their names are the same", evidence={},
        ),
    ]
    monkeypatch.setattr(
        "recall.normalize.merge.suggest_merges", lambda conn, settings: made
    )
    return [(a, b), (a, c)]
