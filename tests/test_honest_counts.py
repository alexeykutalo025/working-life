"""The honest-count rule, spec 9.5.

> Anywhere the interface shows a count, total, chart, or export, if any open
> finding affects that number it is displayed with a marker and an explanation
> in reach.
"""

from __future__ import annotations

from recall.integrity.engine import Finding, record_finding, set_finding_state
from recall.integrity.honest import Count, Qualifier, count_items, qualifiers_for, undated_count


def _item(conn, key: str, kind: str = "message", occurred: str | None = "2003-04-14T09:30:00Z"):
    conn.execute(
        "INSERT INTO items(kind, dedup_key, occurred_utc) VALUES (?, ?, ?)",
        (kind, key, occurred),
    )
    return conn.execute("SELECT id FROM items WHERE dedup_key = ?", (key,)).fetchone()["id"]


def _source(conn, path: str = "C:\\mail.pst") -> int:
    cur = conn.execute(
        "INSERT INTO source_files(path, ext, size_bytes) VALUES (?, '.pst', 1000000)",
        (path,),
    )
    return int(cur.lastrowid)


# --- a clean count --------------------------------------------------------


def test_a_clean_count_is_not_qualified(conn):
    for i in range(5):
        _item(conn, f"k{i}")
    count = count_items(conn, label="messages")
    assert count.value == 5
    assert count.qualified is False
    assert count.estimated_missing is None
    assert count.sentence() == "5 messages"


def test_zero_is_a_real_answer(conn):
    count = count_items(conn, label="messages")
    assert count.value == 0
    assert count.sentence() == "0 messages"


# --- a qualified count ----------------------------------------------------


def test_a_partial_parse_qualifies_the_total(conn):
    """The headline example from the spec."""
    source_id = _source(conn)
    for i in range(12481):
        pass  # counting 12,481 rows in a test is not worth the seconds
    for i in range(5):
        _item(conn, f"k{i}")

    record_finding(conn, Finding(
        code="partial_parse",
        severity="critical",
        title="archive1998.pst: about 8,000 records could not be read",
        source_file_id=source_id,
        estimated_loss=8000,
    ))

    count = count_items(conn, label="messages")
    assert count.qualified is True
    assert count.estimated_missing == 8000
    assert "about 8,000 more could not be read" in count.sentence()
    assert count.worst_severity == "critical"


def test_the_exact_count_is_never_adjusted_by_the_estimate(conn):
    """A guess added into a total makes the total a guess."""
    source_id = _source(conn)
    for i in range(5):
        _item(conn, f"k{i}")
    record_finding(conn, Finding(
        code="partial_parse", severity="critical", title="x",
        source_file_id=source_id, estimated_loss=8000,
    ))
    count = count_items(conn)
    assert count.value == 5, "value is what was counted, never counted-plus-guessed"
    assert count.estimated_missing == 8000


def test_several_files_are_grouped_into_one_phrase(conn):
    a, b = _source(conn, "C:\\a.pst"), _source(conn, "C:\\b.pst")
    for source_id, loss in ((a, 3000), (b, 5000)):
        record_finding(conn, Finding(
            code="partial_parse", severity="critical", title="x",
            source_file_id=source_id, estimated_loss=loss,
        ))
    quals = qualifiers_for(conn)
    assert len(quals) == 1
    assert "2 files" in quals[0].text
    assert quals[0].estimated_loss == 8000


def test_worst_severity_leads(conn):
    a, b = _source(conn, "C:\\a.pst"), _source(conn, "C:\\b.pst")
    record_finding(conn, Finding(code="needs_password", severity="high",
                                 title="x", source_file_id=a))
    record_finding(conn, Finding(code="read_failure", severity="critical",
                                 title="y", source_file_id=b))
    quals = qualifiers_for(conn)
    assert quals[0].severity == "critical"


# --- which findings qualify a count --------------------------------------


def test_a_quality_finding_does_not_qualify_a_total(conn):
    """An unknown timezone makes a record uncertain, not a total incomplete."""
    item_id = _item(conn, "k1")
    record_finding(conn, Finding(
        code="unknown_timezone", severity="medium", title="x", item_id=item_id,
    ))
    assert count_items(conn).qualified is False


def test_a_resolved_finding_stops_qualifying(conn):
    source_id = _source(conn)
    finding_id = record_finding(conn, Finding(
        code="partial_parse", severity="critical", title="x",
        source_file_id=source_id, estimated_loss=100,
    ))
    assert count_items(conn).qualified is True
    set_finding_state(conn, finding_id, "resolved")
    assert count_items(conn).qualified is False


def test_an_acknowledged_finding_still_qualifies(conn):
    """Acknowledging a problem does not make the data come back."""
    source_id = _source(conn)
    finding_id = record_finding(conn, Finding(
        code="partial_parse", severity="critical", title="x",
        source_file_id=source_id, estimated_loss=100,
    ))
    set_finding_state(conn, finding_id, "acknowledged", "I know about this one")
    assert count_items(conn).qualified is True


def test_an_explained_gap_stops_nagging_the_totals(conn):
    """Spec 9.2: an explained gap leaves the banner but stays on the timeline."""
    finding_id = record_finding(conn, Finding(
        code="hard_gap", severity="high", title="Nothing in Feb 2003",
        period_start="2003-02",
    ))
    assert count_items(conn).qualified is True
    set_finding_state(conn, finding_id, "explained", "I was not using email yet")
    assert count_items(conn).qualified is False


# --- undated --------------------------------------------------------------


def test_undated_records_are_counted_separately(conn):
    _item(conn, "dated", occurred="2003-04-14T09:30:00Z")
    _item(conn, "undated1", occurred=None)
    _item(conn, "undated2", occurred=None)
    assert undated_count(conn) == 2


def test_undated_records_are_not_in_a_period_count(conn):
    """Never assign epoch zero, never sort them in as if dated."""
    _item(conn, "dated", occurred="2003-04-14T09:30:00Z")
    _item(conn, "undated", occurred=None)
    assert count_items(conn, period_start="2003-01", period_end="2003-12").value == 1


# --- the envelope ---------------------------------------------------------


def test_as_dict_carries_the_qualifiers_with_the_number():
    """There is no way to serialise the value without its reasons."""
    count = Count(
        value=12481,
        label="messages",
        qualifiers=[Qualifier("partial_parse", "2 files could not be read in full", 8000, "critical")],
    )
    d = count.as_dict()
    assert d["value"] == 12481
    assert d["qualified"] is True
    assert d["estimated_missing"] == 8000
    assert d["qualifiers"][0]["text"] == "2 files could not be read in full"
    assert "about 8,000 more could not be read" in d["sentence"]


def test_a_qualified_number_is_not_rounded():
    count = Count(value=12481, label="messages",
                  qualifiers=[Qualifier("partial_parse", "x", 8000, "critical")])
    assert "12,481" in count.sentence()
    assert "12,000" not in count.sentence()
    assert "12.5" not in count.sentence()
