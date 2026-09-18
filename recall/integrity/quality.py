"""Section 9.4 - record-level data quality.

| code                   | meaning                                            |
|------------------------|----------------------------------------------------|
| no_date                | no usable timestamp; goes to a visible Undated bucket |
| implausible_date       | before 1970 or in the future; kept verbatim        |
| unknown_timezone       | counted per source; distorts travel analysis       |
| low_confidence_text    | an encoding was guessed; marked in the viewer      |
| orphan_reply           | a reply to something not in the archive            |
| missing_blob           | an attachment row whose file is gone               |
| unresolved_recurrence  | an RRULE that could not be read                    |

These are reported **per source file**, not per record. A mailbox with 4,000
undated messages is one fact about that mailbox; four thousand rows on the
Problems screen would be four thousand ways of saying it, and the screen would
become unreadable - which is the same as being silent.

The parsers already attach a finding to the individual affected items as they
go, so the detail is on the item viewer where it belongs. This module produces
the summary the Problems screen needs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..logging_setup import get_logger
from ..models import Severity
from .engine import Finding, record_finding, resolve_absent_findings

log = get_logger("integrity.quality")

#: Below this many affected records in one source, the per-item findings the
#: parsers already wrote say everything. A summary would just repeat them.
_SUMMARY_THRESHOLD = 5


def quality_checks(conn, settings) -> int:
    """Everything in 9.4, summarised per source file."""
    n = 0
    n += check_item_dates(conn, settings)
    n += _summarize_per_source(conn, settings)
    n += _check_orphan_replies(conn, settings)
    n += _check_missing_blobs(conn, settings)
    return n


# ---------------------------------------------------------------------------
# Per-source summaries
# ---------------------------------------------------------------------------

_SUMMARIES = {
    "no_date": {
        "severity": Severity.MEDIUM,
        "title": "{n:,} records from {name} have no date",
        "detail": (
            "{n:,} record(s) out of {total:,} read from this file carry no date "
            "that Recall could use.\n\n"
            "They are NOT on the timeline, they are NOT in any date range, and no "
            "date has been invented for them. They are in the Undated list, where "
            "they can be searched and read like anything else.\n\n"
            "This is usually a mail program that did not write a Date header, or "
            "a store whose dates were lost in a conversion."
        ),
    },
    "implausible_date": {
        "severity": Severity.MEDIUM,
        "title": "{n:,} records from {name} are dated impossibly",
        "detail": (
            "{n:,} record(s) from this file carry a date before 1970 or in the "
            "future.\n\n"
            "The dates have been kept exactly as the records give them and have "
            "NOT been corrected - a corrected date would be an invention. They "
            "appear on the timeline where their dates put them, which may be far "
            "from everything else.\n\n"
            "A date of 1 January 1601 usually means the field was empty and "
            "something filled it in; a date far in the future usually means a "
            "computer whose clock was wrong."
        ),
    },
    "unknown_timezone": {
        "severity": Severity.MEDIUM,
        "title": "{n:,} records from {name} do not say which timezone they are in",
        "detail": (
            "{n:,} record(s) out of {total:,} from this file give a time but not "
            "a timezone.\n\n"
            "The dates are right. The exact times may be out by a few hours, and "
            "no timezone has been assumed.\n\n"
            "This matters most if you are looking at travel, or at what time of "
            "day you worked: those answers are uncertain for these records. "
            "Wherever one of them is shown, its time is marked."
        ),
    },
    "low_confidence_text": {
        "severity": Severity.MEDIUM,
        "title": "{n:,} records from {name} had their text worked out rather than read",
        "detail": (
            "{n:,} record(s) out of {total:,} from this file did not say what "
            "character set they were written in, so Recall worked it out from the "
            "bytes.\n\n"
            "The text is readable and almost certainly right. Accented letters, "
            "quotation marks, currency symbols and dashes are where a guess goes "
            "wrong, so those may be incorrect in these records.\n\n"
            "Each affected record is marked when you open it."
        ),
    },
    "unresolved_recurrence": {
        "severity": Severity.MEDIUM,
        "title": "{n:,} repeating entries from {name} have a rule Recall could not read",
        "detail": (
            "{n:,} repeating calendar entries from this file have a repeat rule "
            "that could not be understood.\n\n"
            "Each one is shown once, on its start date. The repeats have NOT been "
            "invented - a wrong repeat rule would put meetings in your calendar "
            "that never happened.\n\n"
            "Reading the file with Microsoft Outlook instead usually recovers the "
            "rule, because Outlook stores it in a packed form only Outlook reads."
        ),
    },
}


def _summarize_per_source(conn, settings) -> int:
    """Roll the per-item findings up into one row per source per code.

    ``present`` is keyed by the *summary* codes only. Keying it by the
    underlying codes would make the closing sweep resolve every per-item
    finding it is summarising - this module would quietly close the very
    findings it exists to count, which is precisely the auto-resolving the
    spec forbids.
    """
    n = 0
    present: dict[str, list[tuple]] = {f"{code}_summary": [] for code in _SUMMARIES}

    rows = conn.execute(
        """
        SELECT f.code AS code, s.source_file_id AS source_id, sf.path AS path,
               sf.item_count AS total, COUNT(DISTINCT f.item_id) AS n
        FROM findings f
        JOIN item_sources s ON s.item_id = f.item_id
        JOIN source_files sf ON sf.id = s.source_file_id
        WHERE f.item_id IS NOT NULL
          AND f.code IN ('no_date', 'implausible_date', 'unknown_timezone',
                         'low_confidence_text', 'unresolved_recurrence')
        GROUP BY f.code, s.source_file_id, sf.path, sf.item_count
        """
    ).fetchall()

    for row in rows:
        code = row["code"]
        spec = _SUMMARIES.get(code)
        if spec is None:
            continue
        count = int(row["n"])
        if count < _SUMMARY_THRESHOLD:
            continue

        source_id = int(row["source_id"])
        name = Path(row["path"]).name
        total = int(row["total"] or 0)

        # A summary hangs off a distinct code so it never collides with the
        # per-item findings of the same name.
        summary_code = f"{code}_summary"
        present.setdefault(summary_code, []).append((summary_code, source_id, -1, -1, ""))

        record_finding(
            conn,
            Finding(
                code=summary_code,
                severity=spec["severity"],
                title=spec["title"].format(n=count, name=name),
                detail=spec["detail"].format(n=count, total=total, name=name),
                source_file_id=source_id,
                affected_count=count,
                evidence={
                    "path": row["path"],
                    "affected": count,
                    "total_from_this_file": total,
                    "share": round(count / total, 4) if total else None,
                    "underlying_code": code,
                },
            ),
        )
        n += 1

    for code, keys in present.items():
        resolve_absent_findings(conn, code, keys)
    return n


# ---------------------------------------------------------------------------
# orphan_reply
# ---------------------------------------------------------------------------


def _check_orphan_replies(conn, settings) -> int:
    """Replies to messages that are not in the archive.

    Spec 9.4: "A cluster of these is evidence of a missing source file, and
    should be reported that way." So they are grouped by the file they came
    from, and the finding says what a cluster means.
    """
    from ..normalize.threads import orphan_replies

    orphans = orphan_replies(conn)
    present: list[tuple] = []

    if not orphans:
        resolve_absent_findings(conn, "orphan_reply", present)
        return 0

    by_source: dict[int | None, list[dict]] = {}
    for orphan in orphans:
        by_source.setdefault(orphan.get("source_id"), []).append(orphan)

    total_messages = conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE kind = 'message'"
    ).fetchone()["n"] or 1

    n = 0
    for source_id, group in by_source.items():
        count = len(group)
        # A handful of orphans is normal: the other half of the conversation
        # was simply never in this mailbox. A large share is evidence.
        source_total = conn.execute(
            "SELECT item_count FROM source_files WHERE id = ?", (source_id,)
        ).fetchone() if source_id else None
        denominator = int(source_total["item_count"]) if source_total else total_messages
        share = count / denominator if denominator else 0

        if count < 10 and share < 0.2:
            continue

        path = group[0].get("source_path") or "the archive"
        name = Path(path).name if path != "the archive" else path
        present.append(
            ("orphan_reply", source_id if source_id is not None else -1, -1, -1, "")
        )

        severity = Severity.HIGH if share >= 0.3 else Severity.MEDIUM
        examples = "\n".join(
            f"    {o['occurred_utc'] or 'no date'}  {o['subject'] or '(no subject)'}"
            for o in group[:6]
        )

        record_finding(
            conn,
            Finding(
                code="orphan_reply",
                severity=severity,
                title=(
                    f"{count:,} replies in {name} answer messages that are not in "
                    "the archive"
                ),
                detail=(
                    f"{count:,} message(s) from this file are replies - they name "
                    "the message they are answering - but that message is nowhere "
                    "in the archive.\n\n"
                    + (
                        f"That is {share:.0%} of everything read from this file. A "
                        "share that high is evidence that a mailbox is missing: "
                        "the half of the correspondence that was sent TO these "
                        "people is somewhere Recall has not looked.\n\n"
                        if share >= 0.3 else
                        "A few of these are normal - the other side of a "
                        "conversation was simply never in this mailbox.\n\n"
                    )
                    + "What to do: look for another .pst or .ost from the same "
                    "period, particularly a Sent Items store or a mailbox from a "
                    "different account.\n\n"
                    f"Examples:\n{examples}"
                ),
                source_file_id=source_id,
                affected_count=count,
                evidence={
                    "path": path,
                    "orphans": count,
                    "share_of_file": round(share, 4),
                    "examples": [
                        {"id": o["id"], "subject": o["subject"],
                         "in_reply_to": o["in_reply_to"]}
                        for o in group[:20]
                    ],
                },
            ),
        )
        n += 1

    resolve_absent_findings(conn, "orphan_reply", present)
    return n


# ---------------------------------------------------------------------------
# missing_blob
# ---------------------------------------------------------------------------


def _check_missing_blobs(conn, settings) -> int:
    """Attachment rows whose file is not on disk.

    The archive says an attachment exists; if the bytes are gone the archive is
    wrong about itself, and that is worth a high severity.
    """
    from ..normalize.attachments import BlobStore

    store = BlobStore(settings.blobs_path)
    rows = conn.execute(
        "SELECT a.id, a.item_id, a.filename, a.content_hash, i.subject "
        "FROM attachments a LEFT JOIN items i ON i.id = a.item_id "
        "WHERE a.content_hash IS NOT NULL"
    ).fetchall()

    missing = []
    for row in rows:
        suffix = ""
        if row["filename"]:
            suffix = Path(str(row["filename"])).suffix.lower()
        if not store.exists(row["content_hash"], suffix):
            missing.append(dict(row))

    if not missing:
        resolve_absent_findings(conn, "missing_blob", [])
        return 0

    record_finding(
        conn,
        Finding(
            code="missing_blob",
            severity=Severity.HIGH,
            title=(
                f"{len(missing):,} attachment(s) are listed in the archive but "
                "their contents are gone"
            ),
            detail=(
                f"The archive says {len(missing):,} attachment(s) exist, but the "
                "saved copies are not in the attachments folder.\n\n"
                "That means the archive is wrong about itself. The most likely "
                "causes are that the attachments folder was moved or deleted, or "
                "that a disk error removed the files.\n\n"
                "What to do: the original messages still hold these attachments. "
                "Re-read the affected files on the Files found screen and the "
                "attachments will be saved again.\n\n"
                "Missing:\n"
                + "\n".join(
                    f"    {m['filename'] or '(no name)'}  "
                    f"(from: {m['subject'] or 'no subject'})"
                    for m in missing[:10]
                )
                + ("\n    ..." if len(missing) > 10 else "")
            ),
            affected_count=len(missing),
            evidence={
                "count": len(missing),
                "blobs_path": str(settings.blobs_path),
                "examples": [
                    {"filename": m["filename"], "hash": m["content_hash"]}
                    for m in missing[:20]
                ],
            },
        ),
    )
    resolve_absent_findings(conn, "missing_blob", [("missing_blob", -1, -1, -1, "")])
    return 1


# ---------------------------------------------------------------------------
# Used by the extractor as items are written
# ---------------------------------------------------------------------------


def check_item_dates(conn, settings) -> int:
    """Flag records whose dates cannot be right.

    Run after extraction rather than during it, because it is a cheap sweep
    over a column and doing it per-item would cost a query per record.
    """
    cutoff = settings.integrity.implausible_before_year
    next_year = datetime.now(timezone.utc).year + 1
    n = 0

    rows = conn.execute(
        "SELECT id, subject, occurred_utc FROM items "
        "WHERE occurred_utc IS NOT NULL "
        "AND (substr(occurred_utc, 1, 4) < ? OR substr(occurred_utc, 1, 4) > ?)",
        (f"{cutoff:04d}", f"{next_year:04d}"),
    ).fetchall()

    for row in rows:
        record_finding(
            conn,
            Finding(
                code="implausible_date",
                severity=Severity.MEDIUM,
                title=(
                    f"A record is dated {row['occurred_utc'][:10]}, which cannot "
                    "be right"
                ),
                detail=(
                    f"\"{row['subject'] or '(no subject)'}\" is dated "
                    f"{row['occurred_utc'][:10]}.\n\n"
                    "The date has been kept exactly as the record gives it and "
                    "has NOT been corrected. Correcting it would be inventing a "
                    "date, and the real one is not recoverable.\n\n"
                    "The record appears on the timeline where its date puts it, "
                    "which may be a long way from everything else."
                ),
                item_id=int(row["id"]),
                affected_count=1,
                evidence={"date": row["occurred_utc"], "subject": row["subject"]},
            ),
        )
        n += 1
    return n
