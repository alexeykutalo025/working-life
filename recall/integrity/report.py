"""`recall audit` - every check, and a report a person can read.

Spec section 11:

    recall audit [--checks corrupt,gaps,accounts,quality] [--report PATH]
                 # runs every integrity check, prints a severity-grouped report,
                 # exits non-zero if any critical finding is open

The report is grouped by severity and then by class, and each entry says what
is wrong, what it affects, and how many records are involved. It is written to
be sent to somebody for help, so the raw technical detail is in it rather than
behind a click.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..logging_setup import get_logger
from ..models import SEVERITY_ORDER, Severity

log = get_logger("integrity.report")

#: The classes the Problems screen groups by, and their order.
CLASS_ORDER = [
    ("unreadable_files", "Files that could not be read"),
    ("coverage_gaps", "Periods with no data"),
    ("duplicate_accounts", "People and accounts"),
    ("record_quality", "Records with something uncertain about them"),
]

_CLASS_FOR_CODE = {
    "read_failure": "unreadable_files",
    "locked_but_read": "unreadable_files",
    "partial_parse": "unreadable_files",
    "needs_password": "unreadable_files",
    "orphaned_ost": "unreadable_files",
    "zero_or_tiny": "unreadable_files",
    "empty_tree": "unreadable_files",
    "magic_mismatch": "unreadable_files",
    "backend_disagreement": "unreadable_files",
    "attachment_unreadable": "unreadable_files",
    "hard_gap": "coverage_gaps",
    "soft_gap": "coverage_gaps",
    "source_contradiction": "coverage_gaps",
    "edge_truncation": "coverage_gaps",
    "under_merged": "duplicate_accounts",
    "over_merged_risk": "duplicate_accounts",
    "duplicate_account_store": "duplicate_accounts",
    "self_identity_unclaimed": "duplicate_accounts",
    "ambiguous_legacydn": "duplicate_accounts",
    "identity_conflict": "duplicate_accounts",
}


def class_for(code: str) -> str:
    if code.endswith("_summary"):
        return "record_quality"
    return _CLASS_FOR_CODE.get(code, "record_quality")


def run_audit(conn, settings, *, checks: set[str] | None = None) -> dict:
    """Run the checks and gather everything the report needs."""
    from .engine import run_all_checks

    results = run_all_checks(conn, settings, checks=checks)

    findings = [
        dict(r)
        for r in conn.execute(
            """
            SELECT f.*, sf.path AS source_path, i.subject AS item_subject,
                   p.display_name AS person_name
            FROM findings f
            LEFT JOIN source_files sf ON sf.id = f.source_file_id
            LEFT JOIN items i ON i.id = f.item_id
            LEFT JOIN people p ON p.id = f.person_id
            WHERE f.state IN ('open', 'acknowledged')
            ORDER BY CASE f.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                     WHEN 'medium' THEN 2 ELSE 3 END, f.code, f.id
            """
        )
    ]

    counts = {s: 0 for s in Severity}
    for finding in findings:
        counts[finding["severity"]] = counts.get(finding["severity"], 0) + 1

    loss = conn.execute(
        "SELECT SUM(estimated_loss) AS n FROM findings "
        "WHERE state IN ('open','acknowledged') AND estimated_loss > 0"
    ).fetchone()["n"]

    totals = conn.execute(
        "SELECT COUNT(*) AS items, "
        "  SUM(CASE WHEN occurred_utc IS NULL THEN 1 ELSE 0 END) AS undated "
        "FROM items"
    ).fetchone()

    sources = conn.execute(
        "SELECT COUNT(*) AS n, "
        "  SUM(CASE WHEN parse_state = 'done' THEN 1 ELSE 0 END) AS read_ok, "
        "  SUM(CASE WHEN parse_state = 'failed' THEN 1 ELSE 0 END) AS failed, "
        "  SUM(CASE WHEN parse_state = 'pending' THEN 1 ELSE 0 END) AS pending "
        "FROM source_files"
    ).fetchone()

    explained = conn.execute(
        "SELECT COUNT(*) AS n FROM findings WHERE state = 'explained'"
    ).fetchone()["n"]
    resolved = conn.execute(
        "SELECT COUNT(*) AS n FROM findings WHERE state = 'resolved'"
    ).fetchone()["n"]

    return {
        "ran": results,
        "findings": findings,
        "counts": counts,
        "estimated_loss": int(loss or 0),
        "items": int(totals["items"] or 0),
        "undated": int(totals["undated"] or 0),
        "sources": dict(sources),
        "explained": int(explained or 0),
        "resolved": int(resolved or 0),
        "has_critical": counts.get(Severity.CRITICAL, 0) > 0,
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }


def format_report(audit: dict, *, width: int = 78) -> str:
    """The health report, grouped by severity then class."""
    lines: list[str] = []
    add = lines.append

    add("=" * width)
    add("RECALL - WHAT IS WRONG WITH THIS ARCHIVE")
    add("=" * width)
    add("")
    add(f"Checked on {audit['generated_utc']} (UTC)")
    add("")

    # --- the summary, under the honest-count rule ----------------------
    sources = audit["sources"]
    add(f"Records in the archive:  {audit['items']:,}")
    if audit["estimated_loss"]:
        add(
            f"Records NOT read:        about {audit['estimated_loss']:,} "
            "(see the critical and high findings below)"
        )
    if audit["undated"]:
        add(
            f"Records with no date:    {audit['undated']:,} "
            "(in the Undated list, not on the timeline)"
        )
    add(
        f"Files found:             {sources.get('n') or 0:,} "
        f"({sources.get('read_ok') or 0:,} read, "
        f"{sources.get('failed') or 0:,} failed, "
        f"{sources.get('pending') or 0:,} not read yet)"
    )
    add("")

    counts = audit["counts"]
    add("Problems still open:")
    for severity in (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.INFO):
        add(f"    {severity:<10} {counts.get(severity, 0):>6,}")
    if audit["explained"]:
        add(f"    {'explained':<10} {audit['explained']:>6,}  (kept, but not counted above)")
    if audit["resolved"]:
        add(f"    {'resolved':<10} {audit['resolved']:>6,}  (gone on a later run, row kept)")
    add("")

    failed_checks = [name for name, result in audit["ran"].items() if result == -1]
    if failed_checks:
        add("-" * width)
        add("SOME CHECKS COULD NOT RUN")
        add("-" * width)
        add("")
        add(
            "These checks failed, so this report is incomplete and there may be "
            "problems it did not look for:"
        )
        for name in failed_checks:
            add(f"    {name}")
        add("")

    if not audit["findings"]:
        add("-" * width)
        add("NOTHING IS WRONG")
        add("-" * width)
        add("")
        add(
            "Every check passed. Every file was read in full, every record has a "
            "usable date, and no period is unaccounted for."
        )
        add("")
        add(
            "That is a statement about what Recall could check. It cannot know "
            "about a mailbox that was never on this computer."
        )
        add("")
        add("=" * width)
        return "\n".join(lines)

    # --- the findings ---------------------------------------------------
    by_severity: dict[str, list[dict]] = {}
    for finding in audit["findings"]:
        by_severity.setdefault(finding["severity"], []).append(finding)

    explanations = {
        Severity.CRITICAL: "Data is certainly lost. Act on these first.",
        Severity.HIGH: "Data is probably lost or wrong.",
        Severity.MEDIUM: "Something is uncertain, and has been recorded as uncertain.",
        Severity.INFO: "Worth knowing. Nothing is wrong.",
    }

    for severity in sorted(by_severity, key=lambda s: SEVERITY_ORDER.get(s, 9)):
        group = by_severity[severity]
        add("=" * width)
        add(f"{severity.upper()}  ({len(group)})")
        add(explanations.get(severity, ""))
        add("=" * width)

        by_class: dict[str, list[dict]] = {}
        for finding in group:
            by_class.setdefault(class_for(finding["code"]), []).append(finding)

        for class_name, class_label in CLASS_ORDER:
            entries = by_class.get(class_name)
            if not entries:
                continue
            add("")
            add(f"--- {class_label} ({len(entries)}) " + "-" * max(0, width - len(class_label) - 12))

            for finding in entries:
                add("")
                add(f"  [{finding['id']}] {finding['title']}")

                facts = []
                if finding["estimated_loss"]:
                    facts.append(f"records believed unreadable: {finding['estimated_loss']:,}")
                if finding["affected_count"]:
                    facts.append(f"records affected: {finding['affected_count']:,}")
                if finding["period_start"]:
                    period = finding["period_start"]
                    if finding["period_end"] and finding["period_end"] != period:
                        period += f" to {finding['period_end']}"
                    facts.append(f"period: {period}")
                if finding["source_path"]:
                    facts.append(f"file: {finding['source_path']}")
                if finding["person_name"]:
                    facts.append(f"person: {finding['person_name']}")
                if finding["state"] != "open":
                    facts.append(f"state: {finding['state']}")
                if facts:
                    add(f"        ({'; '.join(facts)})")

                if finding["user_note"]:
                    add(f"        your note: {finding['user_note']}")

                for line in (finding["detail"] or "").strip().splitlines():
                    add(f"        {line}" if line.strip() else "")

    add("")
    add("=" * width)
    add(
        "Recall never invents a date, a timezone, a sender or a total. Where "
        "something is missing, it says so - here."
    )
    add("=" * width)
    return "\n".join(lines)


def format_markdown(audit: dict) -> str:
    """The same report as Markdown, for `--report health.md`."""
    lines: list[str] = []
    add = lines.append

    add("# What is wrong with this archive")
    add("")
    add(f"*Checked on {audit['generated_utc']} UTC by Recall.*")
    add("")

    sources = audit["sources"]
    add("| | |")
    add("|---|---|")
    add(f"| Records in the archive | {audit['items']:,} |")
    if audit["estimated_loss"]:
        add(f"| **Records NOT read** | **about {audit['estimated_loss']:,}** |")
    if audit["undated"]:
        add(f"| Records with no date | {audit['undated']:,} |")
    add(
        f"| Files found | {sources.get('n') or 0:,} "
        f"({sources.get('read_ok') or 0:,} read, {sources.get('failed') or 0:,} failed) |"
    )
    counts = audit["counts"]
    for severity in (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.INFO):
        if counts.get(severity):
            add(f"| Problems, {severity} | {counts[severity]:,} |")
    add("")

    if not audit["findings"]:
        add("## Nothing is wrong")
        add("")
        add(
            "Every check passed. That is a statement about what Recall could "
            "check - it cannot know about a mailbox that was never on this "
            "computer."
        )
        add("")
        return "\n".join(lines)

    by_severity: dict[str, list[dict]] = {}
    for finding in audit["findings"]:
        by_severity.setdefault(finding["severity"], []).append(finding)

    for severity in sorted(by_severity, key=lambda s: SEVERITY_ORDER.get(s, 9)):
        group = by_severity[severity]
        add(f"## {severity.capitalize()} ({len(group)})")
        add("")

        by_class: dict[str, list[dict]] = {}
        for finding in group:
            by_class.setdefault(class_for(finding["code"]), []).append(finding)

        for class_name, class_label in CLASS_ORDER:
            entries = by_class.get(class_name)
            if not entries:
                continue
            add(f"### {class_label}")
            add("")
            for finding in entries:
                add(f"#### {finding['title']}")
                add("")
                facts = []
                if finding["estimated_loss"]:
                    facts.append(f"**about {finding['estimated_loss']:,} records unreadable**")
                if finding["affected_count"]:
                    facts.append(f"{finding['affected_count']:,} records affected")
                if finding["source_path"]:
                    facts.append(f"`{finding['source_path']}`")
                if facts:
                    add(" · ".join(facts))
                    add("")
                detail = (finding["detail"] or "").strip()
                if detail:
                    add(detail)
                    add("")
    return "\n".join(lines)
