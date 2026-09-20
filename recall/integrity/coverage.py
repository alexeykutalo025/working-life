"""Section 9.2 - coverage gaps, and the census behind them.

After every extraction the monthly census is rebuilt: for every month from the
earliest item to the latest, per kind, how many items and how many distinct
source files contributed. Then each month is classified:

``hard_gap``
    A month with zero items inside an otherwise-populated span.

``soft_gap``
    A month whose count falls below 15% of the trailing 12-month median.

``source_contradiction``
    **The important one.** A file whose own metadata, folder names or header
    date range claims a period, but from which nothing in that period came out.
    That is a parse failure wearing the costume of a quiet year, and it is said
    so plainly: *"archive1998.pst appears to cover 1996-2004, but nothing from
    2001 came out of it."*

``edge_truncation``
    The archive's earliest item is implausibly late compared with the oldest
    source file's own creation date.

A gap the user has explained keeps its row forever and stops appearing in the
banner. Explaining never deletes anything.
"""

from __future__ import annotations

import re
import statistics
from datetime import datetime, timezone
from pathlib import Path

from ..logging_setup import get_logger
from ..models import GapClass, Severity
from .engine import Finding, record_finding, resolve_absent_findings

log = get_logger("integrity.coverage")

#: Years appearing in a file or folder name: "archive1998.pst", "Mail 2001-2004".
_YEAR_IN_NAME = re.compile(r"(?<!\d)(19[7-9]\d|20[0-4]\d)(?!\d)")

#: How many months of history a soft-gap judgement needs before it means
#: anything. Below this the "trailing median" is one or two months of noise.
_MIN_HISTORY = 6


def coverage_checks(conn, settings) -> int:
    """Rebuild the census, classify it, and record the findings."""
    rebuild_census(conn, settings)
    n = 0
    n += _classify_and_report(conn, settings)
    n += _check_source_contradictions(conn, settings)
    n += _check_edge_truncation(conn, settings)
    return n


# ---------------------------------------------------------------------------
# The census
# ---------------------------------------------------------------------------


def rebuild_census(conn, settings=None) -> int:
    """Rebuild ``coverage_months`` from the items. Returns rows written.

    Every month in the span is present, including the empty ones. That is the
    whole point: a month with no row would be invisible, and an invisible gap
    is the failure this program exists to prevent.

    A user's ``explained_by_user`` flag survives the rebuild.
    """
    explained = {
        (r["month"], r["kind"])
        for r in conn.execute(
            "SELECT month, kind FROM coverage_months WHERE explained_by_user = 1"
        )
    }

    # A record whose date cannot be right must not define the span. One message
    # dated 1961 would otherwise make every month from 1961 to the real start of
    # the archive a "gap" - 418 of them, in testing - burying the gaps that are
    # real under an artefact of a single wrong date.
    #
    # The test is on the date itself rather than on whether an implausible_date
    # finding exists yet. Depending on the finding would make this care which
    # order the check families ran in, and they run alphabetically.
    cutoff = settings.integrity.implausible_before_year if settings else 1970
    latest = datetime.now(timezone.utc).year + 1
    plausible = (
        "i.occurred_utc IS NOT NULL "
        "AND substr(i.occurred_utc, 1, 4) >= ? AND substr(i.occurred_utc, 1, 4) <= ?"
    )
    bounds = (f"{cutoff:04d}", f"{latest:04d}")

    span = conn.execute(
        f"SELECT MIN(i.occurred_utc) AS first, MAX(i.occurred_utc) AS last "
        f"FROM items i WHERE {plausible}",
        bounds,
    ).fetchone()

    conn.execute("DELETE FROM coverage_months")
    if not span or not span["first"]:
        return 0

    counts: dict[tuple[str, str], tuple[int, int]] = {}
    for row in conn.execute(
        f"""
        SELECT substr(i.occurred_utc, 1, 7) AS month, i.kind AS kind,
               COUNT(*) AS n,
               COUNT(DISTINCT s.source_file_id) AS sources
        FROM items i
        LEFT JOIN item_sources s ON s.item_id = i.id
        WHERE {plausible}
        GROUP BY month, kind
        """,
        bounds,
    ):
        counts[(row["month"], row["kind"])] = (int(row["n"]), int(row["sources"] or 0))

    kinds = sorted({k for _, k in counts} or {"message"})
    months = list(_month_range(span["first"][:7], span["last"][:7]))

    rows = []
    for month in months:
        for kind in kinds:
            item_count, source_count = counts.get((month, kind), (0, 0))
            rows.append((
                month, kind, item_count, source_count, None,
                1 if (month, kind) in explained else 0,
            ))

    conn.executemany(
        "INSERT INTO coverage_months(month, kind, item_count, source_count, "
        "gap_class, explained_by_user) VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    log.debug("Census rebuilt: %d month/kind rows over %d months", len(rows), len(months))
    return len(rows)


def _month_range(first: str, last: str):
    """Every 'YYYY-MM' from first to last inclusive."""
    y, m = int(first[:4]), int(first[5:7])
    ly, lm = int(last[:4]), int(last[5:7])
    while (y, m) <= (ly, lm):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m > 12:
            y, m = y + 1, 1


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _classify_and_report(conn, settings) -> int:
    """Mark each month hard_gap or soft_gap, and raise a finding for each."""
    ratio = settings.integrity.soft_gap_ratio

    # Classified on the total across kinds: a month with no mail but one
    # calendar entry is not a hole in the archive.
    totals = [
        (r["month"], int(r["n"]))
        for r in conn.execute(
            "SELECT month, SUM(item_count) AS n FROM coverage_months "
            "GROUP BY month ORDER BY month"
        )
    ]
    if len(totals) < 2:
        resolve_absent_findings(conn, "hard_gap", [])
        resolve_absent_findings(conn, "soft_gap", [])
        return 0

    explained_months = {
        r["month"]
        for r in conn.execute(
            "SELECT DISTINCT month FROM coverage_months WHERE explained_by_user = 1"
        )
    }

    n = 0
    hard_present: list[tuple] = []
    soft_present: list[tuple] = []
    hard_runs: list[list[str]] = []
    current_run: list[str] = []

    for index, (month, count) in enumerate(totals):
        if count == 0:
            current_run.append(month)
            conn.execute(
                "UPDATE coverage_months SET gap_class = ? WHERE month = ?",
                (GapClass.HARD_GAP, month),
            )
            continue

        if current_run:
            hard_runs.append(current_run)
            current_run = []

        # Soft gap: below the fraction of the trailing twelve-month median.
        history = [c for _, c in totals[max(0, index - 12) : index] if c > 0]
        if len(history) >= _MIN_HISTORY:
            median = statistics.median(history)
            if median > 0 and count < median * ratio:
                conn.execute(
                    "UPDATE coverage_months SET gap_class = ? WHERE month = ?",
                    (GapClass.SOFT_GAP, month),
                )
                soft_present.append(("soft_gap", -1, -1, -1, month))
                record_finding(
                    conn,
                    Finding(
                        code="soft_gap",
                        severity=Severity.INFO,
                        title=f"{_pretty(month)} is much quieter than the year before it",
                        detail=(
                            f"{count:,} record(s) in {_pretty(month)}, against a "
                            f"typical month of about {median:,.0f} over the "
                            "preceding year.\n\n"
                            "That may be entirely real - a holiday, a quiet spell, "
                            "a change of job. It is listed so you can say which, "
                            "not because anything is known to be wrong.\n\n"
                            "Use Explain on this row to record what was happening. "
                            "It will stop appearing in the banner and stay on the "
                            "timeline."
                        ),
                        period_start=month,
                        period_end=month,
                        affected_count=count,
                        evidence={
                            "month": month,
                            "count": count,
                            "trailing_median": round(median, 1),
                            "threshold": round(median * ratio, 1),
                        },
                    ),
                )
                n += 1

    if current_run:
        # A run of empty months at the very end is not a gap - it is the end of
        # the archive. Reporting "no data since last month" every month would
        # be noise.
        log.debug("Ignoring %d trailing empty month(s)", len(current_run))

    # One finding per run of empty months, not one per month: "nothing from
    # March 1999 to August 2001" is one thing that happened, and thirty
    # identical rows would bury the Problems screen.
    for run in hard_runs:
        first, last = run[0], run[-1]
        if first in explained_months:
            continue
        hard_present.append(("hard_gap", -1, -1, -1, first))

        span = (
            _pretty(first) if first == last
            else f"{_pretty(first)} to {_pretty(last)}"
        )
        record_finding(
            conn,
            Finding(
                code="hard_gap",
                severity=Severity.HIGH,
                title=(
                    f"Nothing at all from {span}"
                    + (f" ({len(run)} months)" if len(run) > 1 else "")
                ),
                detail=(
                    f"There are records before this period and records after it, "
                    f"but nothing in it - {len(run)} month(s) with not one record "
                    "of any kind.\n\n"
                    "That can be real. It can also mean a mailbox from those years "
                    "has not been found, or could not be read. Recall cannot tell "
                    "which, and will not guess: the timeline shows this period "
                    "hatched and empty rather than smoothing over it.\n\n"
                    "Use Explain on this row to record what was happening - "
                    '"I was not using email yet", or "that was the server we lost '
                    'in the move". The note is kept, the gap stays visible on the '
                    "timeline forever, and it stops appearing in the health banner."
                ),
                period_start=first,
                period_end=last,
                affected_count=len(run),
                evidence={
                    "first_month": first,
                    "last_month": last,
                    "months": len(run),
                    "all_months": run,
                },
            ),
        )
        n += 1

    resolve_absent_findings(conn, "hard_gap", hard_present)
    resolve_absent_findings(conn, "soft_gap", soft_present)
    return n


# ---------------------------------------------------------------------------
# source_contradiction - the one that matters most
# ---------------------------------------------------------------------------


def _check_source_contradictions(conn, settings) -> int:
    """A file that claims a period and produced nothing from it.

    Three sources of a claim, in descending order of how much they are worth:

      1. the range of dates the file's own records span - if a file holds 1996
         and 2004 but nothing from 2001, it was there and did not come out;
      2. years named in the file's own name: archive1998.pst, "Mail 1996-2004";
      3. years named in its folder paths: "Clients/2001".

    A claim from the file's own contents is strong evidence. A year in a
    filename is weaker, and is reported at a lower severity, because a file
    called archive1998.pst may simply have been named in 1998.
    """
    present: list[tuple] = []
    n = 0

    sources = conn.execute(
        "SELECT id, path, first_item_utc, last_item_utc, item_count, ctime_utc "
        "FROM source_files WHERE parse_state = 'done' AND item_count > 0"
    ).fetchall()

    for source in sources:
        source_id = int(source["id"])
        months_present = {
            r["month"]
            for r in conn.execute(
                "SELECT DISTINCT substr(i.occurred_utc, 1, 7) AS month "
                "FROM items i JOIN item_sources s ON s.item_id = i.id "
                "WHERE s.source_file_id = ? AND i.occurred_utc IS NOT NULL",
                (source_id,),
            )
        }
        if not months_present:
            continue

        years_present = {m[:4] for m in months_present}
        first_year = min(years_present)
        last_year = max(years_present)

        # (1) Internal holes: a year inside the file's own span with nothing in it.
        internal = [
            f"{y:04d}"
            for y in range(int(first_year) + 1, int(last_year))
            if f"{y:04d}" not in years_present
        ]

        # (2) and (3) Years claimed by the name, outside what came out.
        claimed_years = _years_claimed(conn, source_id, source["path"])
        external = sorted(
            y for y in claimed_years
            if y not in years_present and first_year <= y <= last_year
        )

        for year in internal:
            key_month = f"{year}-01"
            present.append(("source_contradiction", source_id, -1, -1, key_month))
            _mark_contradiction(conn, year)
            record_finding(
                conn,
                Finding(
                    code="source_contradiction",
                    severity=Severity.HIGH,
                    title=(
                        f"{Path(source['path']).name} appears to cover "
                        f"{first_year}–{last_year}, but nothing from {year} "
                        "came out of it"
                    ),
                    detail=(
                        f"This file produced records from {first_year} and from "
                        f"{last_year}, but not one from {year}.\n\n"
                        "**This is a reading failure, not a quiet year.** A "
                        "mailbox that was in use either side of a year was almost "
                        "certainly in use during it. The likeliest explanation is "
                        "that part of this file could not be read.\n\n"
                        "What to do: use Retry on this file to read it with the "
                        "other reader, which sometimes recovers more. The file "
                        "itself has not been changed.\n\n"
                        f"File: {source['path']}\n"
                        f"Records read from it: {source['item_count']:,}"
                    ),
                    source_file_id=source_id,
                    period_start=f"{year}-01",
                    period_end=f"{year}-12",
                    evidence={
                        "path": source["path"],
                        "missing_year": year,
                        "file_spans": f"{first_year}-{last_year}",
                        "years_present": sorted(years_present),
                        "basis": "the file's own records span this year",
                    },
                ),
            )
            n += 1

        for year in external:
            key_month = f"{year}-01"
            if ("source_contradiction", source_id, -1, -1, key_month) in present:
                continue
            present.append(("source_contradiction", source_id, -1, -1, key_month))
            record_finding(
                conn,
                Finding(
                    code="source_contradiction",
                    severity=Severity.MEDIUM,
                    title=(
                        f"{Path(source['path']).name} is named for {year}, but "
                        "nothing from that year came out of it"
                    ),
                    detail=(
                        f"The name of this file, or one of its folders, mentions "
                        f"{year}. No record from {year} was read out of it.\n\n"
                        "A name is weaker evidence than contents - a file called "
                        f"archive{year}.pst may simply have been created in "
                        f"{year}. It is reported so you can judge.\n\n"
                        f"File: {source['path']}"
                    ),
                    source_file_id=source_id,
                    period_start=f"{year}-01",
                    period_end=f"{year}-12",
                    evidence={
                        "path": source["path"],
                        "missing_year": year,
                        "years_present": sorted(years_present),
                        "basis": "a year named in the file or folder name",
                    },
                ),
            )
            n += 1

    resolve_absent_findings(conn, "source_contradiction", present)
    return n


def _years_claimed(conn, source_id: int, path: str) -> set[str]:
    """Years named in the file's own name or in its folder paths."""
    claimed = set(_YEAR_IN_NAME.findall(Path(path).name))
    for row in conn.execute(
        "SELECT path FROM folders WHERE source_file_id = ?", (source_id,)
    ):
        claimed.update(_YEAR_IN_NAME.findall(row["path"] or ""))
    return claimed


def _mark_contradiction(conn, year: str) -> None:
    """Mark the year's months so the timeline hatches them for the right reason."""
    conn.execute(
        "UPDATE coverage_months SET gap_class = ? "
        "WHERE month LIKE ? AND (gap_class IS NULL OR gap_class = 'hard_gap')",
        (GapClass.SOURCE_CONTRADICTION, f"{year}-%"),
    )


# ---------------------------------------------------------------------------
# edge_truncation
# ---------------------------------------------------------------------------


def _check_edge_truncation(conn, settings) -> int:
    """The archive starts implausibly late for the age of its oldest file.

    A .pst created in 1998 whose earliest record is from 2004 has lost six
    years somewhere, and that is worth saying.
    """
    earliest_item = conn.execute(
        "SELECT MIN(occurred_utc) AS first FROM items WHERE occurred_utc IS NOT NULL"
    ).fetchone()
    if not earliest_item or not earliest_item["first"]:
        resolve_absent_findings(conn, "edge_truncation", [])
        return 0

    item_year = int(earliest_item["first"][:4])

    oldest_file = conn.execute(
        "SELECT id, path, ctime_utc, mtime_utc FROM source_files "
        "WHERE parse_state = 'done' AND ctime_utc IS NOT NULL "
        "ORDER BY ctime_utc ASC LIMIT 1"
    ).fetchone()
    if not oldest_file:
        resolve_absent_findings(conn, "edge_truncation", [])
        return 0

    try:
        file_year = int(oldest_file["ctime_utc"][:4])
    except (TypeError, ValueError):
        resolve_absent_findings(conn, "edge_truncation", [])
        return 0

    # A file's creation date is reset by copying, so only a gap of several
    # years is worth reporting - and even then it is reported as a question.
    gap = item_year - file_year
    if gap < 3 or file_year < 1990:
        resolve_absent_findings(conn, "edge_truncation", [])
        return 0

    source_id = int(oldest_file["id"])
    record_finding(
        conn,
        Finding(
            code="edge_truncation",
            severity=Severity.MEDIUM,
            title=(
                f"The archive starts in {item_year}, but the oldest file on this "
                f"computer dates from {file_year}"
            ),
            detail=(
                f"The earliest record Recall could read is from {item_year}. The "
                f"oldest file it found was created in {file_year}, {gap} years "
                "earlier.\n\n"
                "That can mean the early years were never read, or that the file "
                "was copied at some point and Windows reset its date - copying a "
                "file gives it a new creation date, so this is a question rather "
                "than a conclusion.\n\n"
                f"Oldest file: {oldest_file['path']}\n"
                f"Its recorded creation date: {oldest_file['ctime_utc']}"
            ),
            source_file_id=source_id,
            period_start=f"{file_year}-01",
            period_end=f"{item_year}-01",
            evidence={
                "earliest_item_year": item_year,
                "oldest_file_year": file_year,
                "gap_years": gap,
                "path": oldest_file["path"],
            },
        ),
    )
    resolve_absent_findings(
        conn, "edge_truncation", [("edge_truncation", source_id, -1, -1, f"{file_year}-01")]
    )
    return 1


# ---------------------------------------------------------------------------
# The coverage map
# ---------------------------------------------------------------------------


def coverage_map(conn) -> dict:
    """One row per year, one cell per month - the most important picture here.

    Spec, screen 7: "That map is the single most important picture in the
    application - it is the honest answer to 'what do I actually have?'"

    Every month in the span is returned, with its count, its gap class and
    whether it has been explained. A month with no data is a cell that says so,
    never an absent one.
    """
    rows = conn.execute(
        "SELECT month, SUM(item_count) AS n, MAX(gap_class) AS gap_class, "
        "MAX(explained_by_user) AS explained, SUM(source_count) AS sources "
        "FROM coverage_months GROUP BY month ORDER BY month"
    ).fetchall()

    if not rows:
        return {
            "years": [],
            "max_month": 0,
            "total": 0,
            "undated": _undated(conn),
            "empty_reason": (
                "Nothing has been read into the archive yet, so there is no "
                "coverage to show."
            ),
        }

    by_month = {
        r["month"]: {
            "count": int(r["n"] or 0),
            "gap_class": r["gap_class"],
            "explained": bool(r["explained"]),
            "sources": int(r["sources"] or 0),
        }
        for r in rows
    }

    first_year = int(rows[0]["month"][:4])
    last_year = int(rows[-1]["month"][:4])
    counts = [v["count"] for v in by_month.values()]
    max_month = max(counts) if counts else 0

    years = []
    for year in range(first_year, last_year + 1):
        months = []
        for m in range(1, 13):
            key = f"{year:04d}-{m:02d}"
            cell = by_month.get(key)
            if cell is None:
                # Outside the archive's span entirely: not a gap, just not
                # covered. The two look different on the map.
                months.append({
                    "month": key, "number": m, "count": 0, "gap_class": None,
                    "explained": False, "sources": 0, "in_span": False,
                })
            else:
                months.append({
                    "month": key, "number": m, "count": cell["count"],
                    "gap_class": cell["gap_class"], "explained": cell["explained"],
                    "sources": cell["sources"], "in_span": True,
                })
        years.append({
            "year": year,
            "months": months,
            "total": sum(m["count"] for m in months),
            "gap_months": sum(1 for m in months if m["in_span"] and not m["count"]),
        })

    return {
        "years": years,
        "max_month": max_month,
        "total": sum(counts),
        "undated": _undated(conn),
        "first_year": first_year,
        "last_year": last_year,
    }


def _undated(conn) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE occurred_utc IS NULL"
    ).fetchone()
    total = int(row["n"] or 0)
    return {
        "total": total,
        "note": (
            f"{total:,} record(s) have no date at all. They are not on this map, "
            "and no date has been invented for them."
        ) if total else "",
    }


def _pretty(month: str) -> str:
    """'2003-02' to 'February 2003'."""
    try:
        return datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    except ValueError:
        return month
