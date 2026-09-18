"""The integrity engine, against fixtures broken on purpose.

Spec section 13 names the cases and this file follows it exactly:

    a truncated store, a file whose extension lies about its contents, a
    mailbox with every February 2003 message removed (must produce hard_gap),
    a source whose folder names claim 2001 while containing nothing from 2001
    (must produce source_contradiction), one SMTP address carrying twelve
    different display names (must produce over_merged_risk), two stores where
    one is a strict subset of the other (must produce duplicate_account_store
    naming the superset), messages with no date and with a 1961 date, and an
    attachment row whose blob has been deleted.

    Assert equally that clean fixtures produce zero findings - a system that
    cries wolf is as useless as one that stays silent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from recall.config import Settings
from recall.extract import Extractor
from recall.integrity.coverage import coverage_map, rebuild_census
from recall.integrity.engine import (
    Finding,
    open_findings,
    record_finding,
    resolve_absent_findings,
    set_finding_state,
    severity_counts,
)
from recall.integrity.report import format_markdown, format_report, run_audit
from recall.scan.walker import Scanner
from tests.fixtures.generate import (
    generate_contradiction_mailbox,
    generate_damaged,
    generate_gap_mailbox,
    generate_over_merged,
    generate_spotless,
    generate_subset_pair,
)


def build(settings: Settings, conn, fixtures: Path):
    """Scan and extract one fixture directory, then run every check."""
    Scanner(settings, conn).run([fixtures])
    Extractor(settings, conn).run()
    return conn


def spotless(settings: Settings, conn, tmp_path: Path):
    """A genuinely clean archive, including the user saying who he is.

    Without [identity] me, self_identity_unclaimed correctly asks whether the
    biggest sender is him - so an archive with it unset is not clean, it is
    unfinished, and the zero-findings assertion would be testing the wrong
    thing.
    """
    settings.identity.me = ["tmccarthy@contractmktg.com"]
    fixtures = tmp_path / "spotless"
    generate_spotless(fixtures)
    return build(settings, conn, fixtures)


def codes(conn, code: str) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM findings WHERE code = ? AND state IN ('open','acknowledged')",
            (code,),
        )
    ]


# ===========================================================================
# Clean input produces nothing
# ===========================================================================


def test_spotless_fixtures_produce_no_findings(tmp_path: Path, settings, conn):
    """A system that cries wolf is as useless as one that stays silent."""
    spotless(settings, conn, tmp_path)

    findings = open_findings(conn)
    assert findings == [], [
        (f["code"], f["title"]) for f in findings
    ]


def test_spotless_fixtures_produce_records(tmp_path: Path, settings, conn):
    """Guard against the previous test passing because nothing was read."""
    spotless(settings, conn, tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] > 20


def test_a_clean_audit_says_nothing_is_wrong(tmp_path: Path, settings, conn):
    spotless(settings, conn, tmp_path)

    audit = run_audit(conn, settings)
    assert audit["has_critical"] is False
    report = format_report(audit)
    assert "NOTHING IS WRONG" in report
    assert "cannot know about a mailbox that was never on this computer" in report


# ===========================================================================
# 9.1 - corrupt, partial and unreadable sources
# ===========================================================================


def test_an_extension_that_lies_produces_magic_mismatch(tmp_path: Path, settings, conn):
    fixtures = tmp_path / "damaged"
    fixtures.mkdir()
    (fixtures / "not-really.pst").write_bytes(b"PK\x03\x04" + b"\x00" * 3000)

    Scanner(settings, conn).run([fixtures])

    findings = codes(conn, "magic_mismatch")
    assert len(findings) == 1
    assert findings[0]["severity"] == "high"
    assert "not-really.pst" in findings[0]["title"]


def test_a_truncated_store_produces_zero_or_tiny(tmp_path: Path, settings, conn):
    fixtures = tmp_path / "damaged"
    fixtures.mkdir()
    (fixtures / "truncated.pst").write_bytes(b"!BDN" + b"\x00" * 200)

    Scanner(settings, conn).run([fixtures])

    findings = codes(conn, "zero_or_tiny")
    assert len(findings) == 1
    assert "too small" in findings[0]["title"]
    assert findings[0]["evidence_json"]


def test_a_zero_byte_file_says_it_is_empty(tmp_path: Path, settings, conn):
    fixtures = tmp_path / "damaged"
    fixtures.mkdir()
    (fixtures / "nothing.dbx").write_bytes(b"")

    Scanner(settings, conn).run([fixtures])
    findings = codes(conn, "zero_or_tiny")
    assert findings and "empty" in findings[0]["title"]


# ===========================================================================
# 9.2 - coverage gaps
# ===========================================================================


def test_february_2003_removed_produces_a_hard_gap(tmp_path: Path, settings, conn):
    """The spec's named case, exactly."""
    fixtures = tmp_path / "gap"
    fixtures.mkdir()
    generate_gap_mailbox(fixtures)
    build(settings, conn, fixtures)

    gaps = codes(conn, "hard_gap")
    assert gaps, "a whole month missing from a populated span must be reported"

    february = [g for g in gaps if g["period_start"] == "2003-02"]
    assert february, [g["title"] for g in gaps]
    assert february[0]["severity"] == "high"
    assert "February 2003" in february[0]["title"]


def test_the_gap_month_is_marked_in_the_census(tmp_path: Path, settings, conn):
    fixtures = tmp_path / "gap"
    fixtures.mkdir()
    generate_gap_mailbox(fixtures)
    build(settings, conn, fixtures)

    row = conn.execute(
        "SELECT gap_class, SUM(item_count) AS n FROM coverage_months "
        "WHERE month = '2003-02'"
    ).fetchone()
    assert row["n"] == 0
    assert row["gap_class"] == "hard_gap"


def test_the_months_either_side_are_not_gaps(tmp_path: Path, settings, conn):
    fixtures = tmp_path / "gap"
    fixtures.mkdir()
    generate_gap_mailbox(fixtures)
    build(settings, conn, fixtures)

    for month in ("2003-01", "2003-03"):
        row = conn.execute(
            "SELECT SUM(item_count) AS n, MAX(gap_class) AS gap FROM coverage_months "
            "WHERE month = ?",
            (month,),
        ).fetchone()
        assert row["n"] > 0
        assert row["gap"] is None


def test_a_file_claiming_a_year_it_lacks_produces_source_contradiction(
    tmp_path: Path, settings, conn
):
    """The spec singles this one out as the important one."""
    fixtures = tmp_path / "contradiction"
    fixtures.mkdir()
    generate_contradiction_mailbox(fixtures)
    build(settings, conn, fixtures)

    findings = codes(conn, "source_contradiction")
    assert findings, "a file spanning 1996-2004 with no 2001 is a parse failure"

    year_2001 = [f for f in findings if f["period_start"] == "2001-01"]
    assert year_2001, [f["title"] for f in findings]

    finding = year_2001[0]
    assert finding["severity"] == "high"
    assert "2001" in finding["title"]
    assert "archive1996-2004.mbox" in finding["title"]
    assert "reading failure, not a quiet year" in finding["detail"]


def test_the_census_covers_every_month_including_the_empty_ones(
    tmp_path: Path, settings, conn
):
    """A month with no row would be invisible, and an invisible gap is the
    failure this program exists to prevent."""
    fixtures = tmp_path / "gap"
    fixtures.mkdir()
    generate_gap_mailbox(fixtures)
    build(settings, conn, fixtures)

    months = [
        r["month"]
        for r in conn.execute("SELECT DISTINCT month FROM coverage_months ORDER BY month")
    ]
    assert "2003-02" in months, "the empty month is present, with a zero"
    # 2002-01 through 2004-12 inclusive.
    assert len(months) == 36


def test_an_impossible_date_does_not_stretch_the_span(tmp_path: Path, settings, conn):
    """One message dated 1961 must not manufacture 400 months of gaps."""
    fixtures = tmp_path / "mixed"
    fixtures.mkdir()
    generate_gap_mailbox(fixtures)
    generate_damaged(fixtures)     # includes the 1961 message
    build(settings, conn, fixtures)

    first = conn.execute("SELECT MIN(month) AS m FROM coverage_months").fetchone()["m"]
    assert first >= "1990", f"the census starts at {first}, so 1961 defined the span"


# ===========================================================================
# 9.3 - duplicate and colliding accounts
# ===========================================================================


def test_twelve_display_names_on_one_address_produces_over_merged_risk(
    tmp_path: Path, settings, conn
):
    fixtures = tmp_path / "roles"
    fixtures.mkdir()
    generate_over_merged(fixtures)
    build(settings, conn, fixtures)

    findings = codes(conn, "over_merged_risk")
    assert findings, "one address wearing twelve names is not one person"

    finding = findings[0]
    assert finding["severity"] == "high"
    assert "info@northstarprint.co.uk" in finding["title"]
    assert "will NOT split it up" in finding["detail"]


def test_over_merged_risk_never_splits_anything(tmp_path: Path, settings, conn):
    fixtures = tmp_path / "roles"
    fixtures.mkdir()
    generate_over_merged(fixtures)
    build(settings, conn, fixtures)

    before = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    from recall.integrity.accounts import account_checks

    account_checks(conn, settings)
    after = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    assert before == after, "flagging must never create or destroy a person"


def test_a_strict_subset_produces_duplicate_account_store_naming_the_superset(
    tmp_path: Path, settings, conn
):
    fixtures = tmp_path / "subset"
    fixtures.mkdir()
    generate_subset_pair(fixtures)
    build(settings, conn, fixtures)

    findings = codes(conn, "duplicate_account_store")
    assert findings, "one mailbox inside another must be reported"

    finding = findings[0]
    evidence = finding["evidence_json"]
    assert "mailbox-full.mbox" in finding["title"]
    assert "mailbox-partial.mbox" in finding["title"]
    assert "mailbox-full" in evidence, "the superset is named in the evidence"
    assert finding["affected_count"] == 12, "the overlap is counted, not estimated"


def test_neither_file_is_deleted_or_hidden(tmp_path: Path, settings, conn):
    fixtures = tmp_path / "subset"
    fixtures.mkdir()
    big, small = generate_subset_pair(fixtures)
    build(settings, conn, fixtures)

    assert big.exists() and small.exists(), "source files are never touched"
    assert conn.execute("SELECT COUNT(*) FROM source_files").fetchone()[0] == 2


# ===========================================================================
# 9.4 - record quality
# ===========================================================================


def test_a_message_with_no_date_goes_to_undated_and_is_reported(
    tmp_path: Path, settings, conn
):
    fixtures = tmp_path / "dates"
    fixtures.mkdir()
    generate_damaged(fixtures)
    build(settings, conn, fixtures)

    undated = conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE occurred_utc IS NULL"
    ).fetchone()["n"]
    assert undated >= 1
    assert codes(conn, "no_date")


def test_a_1961_date_is_flagged_and_kept_verbatim(tmp_path: Path, settings, conn):
    fixtures = tmp_path / "dates"
    fixtures.mkdir()
    generate_damaged(fixtures)
    build(settings, conn, fixtures)

    findings = codes(conn, "implausible_date")
    assert findings, "a message dated before email existed must be flagged"

    item = conn.execute(
        "SELECT occurred_utc FROM items WHERE subject LIKE '%1961%'"
    ).fetchone()
    assert item is not None
    assert item["occurred_utc"].startswith("1961"), "the date is kept, not corrected"


def test_a_deleted_blob_produces_missing_blob(tmp_path: Path, settings, conn):
    from recall.integrity.quality import quality_checks
    from recall.normalize.attachments import BlobStore

    store = BlobStore(settings.blobs_path)
    blob = store.put(b"%PDF-1.4 a real attachment", "invoice.pdf")

    conn.execute("INSERT INTO items(kind, dedup_key, subject) VALUES ('message', 'k', 'Invoice')")
    conn.execute(
        "INSERT INTO attachments(item_id, filename, content_hash, extract_state) "
        "VALUES (1, 'invoice.pdf', ?, 'done')",
        (blob.content_hash,),
    )

    quality_checks(conn, settings)
    assert not codes(conn, "missing_blob"), "the blob is there, so nothing is wrong"

    blob.path.unlink()
    quality_checks(conn, settings)

    findings = codes(conn, "missing_blob")
    assert findings
    assert findings[0]["severity"] == "high"
    assert findings[0]["affected_count"] == 1


# ===========================================================================
# The engine's own rules
# ===========================================================================


def test_a_finding_is_never_duplicated(conn):
    for _ in range(5):
        record_finding(conn, Finding(code="no_date", severity="medium", title="x"))
    assert conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 1


def test_a_repeat_updates_the_facts_but_not_the_users_decision(conn):
    finding_id = record_finding(
        conn, Finding(code="partial_parse", severity="critical", title="first",
                      estimated_loss=100)
    )
    set_finding_state(conn, finding_id, "acknowledged", "I know about this")

    record_finding(
        conn, Finding(code="partial_parse", severity="critical", title="second",
                      estimated_loss=250)
    )

    row = conn.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
    assert row["title"] == "second", "the facts are refreshed"
    assert row["estimated_loss"] == 250
    assert row["state"] == "acknowledged", "the user's decision is not overwritten"
    assert row["user_note"] == "I know about this"


def test_nothing_auto_resolves_while_the_condition_holds(conn):
    finding_id = record_finding(conn, Finding(code="hard_gap", severity="high",
                                              title="x", period_start="2003-02"))
    resolve_absent_findings(conn, "hard_gap", [("hard_gap", -1, -1, -1, "2003-02")])
    assert conn.execute(
        "SELECT state FROM findings WHERE id = ?", (finding_id,)
    ).fetchone()[0] == "open"


def test_a_condition_that_is_gone_resolves_but_keeps_its_row(conn):
    finding_id = record_finding(conn, Finding(code="hard_gap", severity="high",
                                              title="x", period_start="2003-02"))
    resolve_absent_findings(conn, "hard_gap", [])

    row = conn.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
    assert row is not None, "a finding is never deleted"
    assert row["state"] == "resolved"
    assert row["resolved_utc"]


def test_a_returning_condition_reopens_and_says_so(conn):
    finding_id = record_finding(conn, Finding(code="hard_gap", severity="high",
                                              title="x", period_start="2003-02"))
    resolve_absent_findings(conn, "hard_gap", [])
    record_finding(conn, Finding(code="hard_gap", severity="high", title="x again",
                                 period_start="2003-02", detail="it is back"))

    row = conn.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
    assert row["state"] == "open"
    assert row["resolved_utc"] is None
    assert "found again" in row["detail"]


def test_an_explained_finding_is_left_alone_by_a_later_run(conn):
    finding_id = record_finding(conn, Finding(code="hard_gap", severity="high",
                                              title="x", period_start="2003-02"))
    set_finding_state(conn, finding_id, "explained", "I was not using email yet")
    resolve_absent_findings(conn, "hard_gap", [])

    row = conn.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
    assert row["state"] == "explained", "the user's own state survives"
    assert row["user_note"] == "I was not using email yet"


def test_explaining_a_gap_requires_a_note(conn):
    """An unexplained explanation is not one."""
    finding_id = record_finding(conn, Finding(code="hard_gap", severity="high",
                                              title="x", period_start="2003-02"))
    with pytest.raises(ValueError, match="needs a note"):
        set_finding_state(conn, finding_id, "explained", "")


def test_explaining_a_gap_marks_the_census_but_keeps_the_months(conn):
    conn.execute(
        "INSERT INTO coverage_months(month, kind, item_count, gap_class) "
        "VALUES ('2003-02', 'message', 0, 'hard_gap')"
    )
    finding_id = record_finding(conn, Finding(code="hard_gap", severity="high",
                                              title="x", period_start="2003-02"))
    set_finding_state(conn, finding_id, "explained", "the server we lost in the move")

    row = conn.execute(
        "SELECT explained_by_user, gap_class FROM coverage_months WHERE month='2003-02'"
    ).fetchone()
    assert row["explained_by_user"] == 1
    assert row["gap_class"] == "hard_gap", "explaining never removes the gap itself"


def test_an_unknown_state_is_refused(conn):
    finding_id = record_finding(conn, Finding(code="no_date", severity="medium", title="x"))
    with pytest.raises(ValueError, match="not a finding state"):
        set_finding_state(conn, finding_id, "deleted")


def test_there_is_no_way_to_delete_a_finding():
    """The absence of this function is deliberate, so it is asserted."""
    from recall.integrity import engine

    assert not hasattr(engine, "delete_finding")
    names = [n for n in dir(engine) if "delete" in n.lower() or "remove" in n.lower()]
    assert names == [], f"the engine exposes {names}"


def test_severity_counts(conn):
    record_finding(conn, Finding(code="read_failure", severity="critical", title="a"))
    record_finding(conn, Finding(code="hard_gap", severity="high", title="b",
                                 period_start="2003-02"))
    counts = severity_counts(conn)
    assert counts["critical"] == 1
    assert counts["high"] == 1
    assert counts["medium"] == 0


# ===========================================================================
# The coverage map
# ===========================================================================


def test_the_map_has_a_cell_for_every_month(tmp_path: Path, settings, conn):
    fixtures = tmp_path / "gap"
    fixtures.mkdir()
    generate_gap_mailbox(fixtures)
    build(settings, conn, fixtures)

    result = coverage_map(conn)
    assert result["years"]
    for year in result["years"]:
        assert len(year["months"]) == 12, "a month is never missing from the picture"


def test_the_map_marks_the_gap_month(tmp_path: Path, settings, conn):
    fixtures = tmp_path / "gap"
    fixtures.mkdir()
    generate_gap_mailbox(fixtures)
    build(settings, conn, fixtures)

    result = coverage_map(conn)
    year_2003 = next(y for y in result["years"] if y["year"] == 2003)
    february = year_2003["months"][1]
    assert february["month"] == "2003-02"
    assert february["count"] == 0
    assert february["in_span"] is True
    assert february["gap_class"] == "hard_gap"


def test_the_map_separates_empty_from_outside_the_archive(tmp_path: Path, settings, conn):
    fixtures = tmp_path / "gap"
    fixtures.mkdir()
    generate_gap_mailbox(fixtures)
    build(settings, conn, fixtures)

    result = coverage_map(conn)
    first_year = result["years"][0]
    kinds = {m["in_span"] for m in first_year["months"]}
    assert kinds <= {True, False}
    # 2002 is fully inside the span in this fixture.
    assert all(m["in_span"] for m in first_year["months"])


def test_the_map_of_an_empty_archive_says_so(conn):
    result = coverage_map(conn)
    assert result["years"] == []
    assert result["empty_reason"]


# ===========================================================================
# The audit report
# ===========================================================================


def test_the_report_groups_by_severity_and_names_the_loss(tmp_path: Path, settings, conn):
    fixtures = tmp_path / "damaged"
    fixtures.mkdir()
    generate_damaged(fixtures)
    build(settings, conn, fixtures)

    audit = run_audit(conn, settings)
    report = format_report(audit)

    assert "WHAT IS WRONG WITH THIS ARCHIVE" in report
    assert "Records in the archive:" in report
    for severity in ("HIGH", "MEDIUM"):
        if audit["counts"].get(severity.lower()):
            assert severity in report


def test_the_markdown_report_is_markdown(tmp_path: Path, settings, conn):
    fixtures = tmp_path / "damaged"
    fixtures.mkdir()
    generate_damaged(fixtures)
    build(settings, conn, fixtures)

    markdown = format_markdown(run_audit(conn, settings))
    assert markdown.startswith("# What is wrong with this archive")
    assert "|---|---|" in markdown


def test_the_audit_reports_a_check_family_that_could_not_run(settings, conn, monkeypatch):
    """A report that silently omits a check is a report that lies."""
    from recall.integrity import engine

    def explode(*args, **kwargs):
        raise RuntimeError("the gaps check is broken")

    monkeypatch.setattr("recall.integrity.coverage.coverage_checks", explode)

    audit = run_audit(conn, settings)
    assert audit["ran"]["gaps"] == -1
    assert "SOME CHECKS COULD NOT RUN" in format_report(audit)


def test_an_unknown_check_name_is_refused(settings, conn):
    with pytest.raises(ValueError, match="Unknown check"):
        run_audit(conn, settings, checks={"nonsense"})
