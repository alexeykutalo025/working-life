"""The findings API - the health banner, and the Problems queue behind it.

Spec 9.6 puts the health banner on Home, always visible and never dismissible.
That means the summary it reads from has to be cheap and always available, so
it lives here rather than being computed by whichever screen happens to ask.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..logging_setup import get_logger
from ..models import SEVERITY_ORDER, FindingState, Severity

log = get_logger("api.findings")

router = APIRouter(tags=["findings"])

#: Findings grouped the way the Problems screen groups them (spec, screen 7).
CLASSES = {
    "unreadable_files": {
        "label": "Files that could not be read",
        "codes": [
            "read_failure", "partial_parse", "needs_password", "orphaned_ost",
            "zero_or_tiny", "empty_tree", "magic_mismatch", "backend_disagreement",
            "attachment_unreadable",
        ],
    },
    "coverage_gaps": {
        "label": "Periods with no data",
        "codes": ["hard_gap", "soft_gap", "source_contradiction", "edge_truncation"],
    },
    "duplicate_accounts": {
        "label": "People and accounts",
        "codes": [
            "under_merged", "over_merged_risk", "duplicate_account_store",
            "self_identity_unclaimed", "ambiguous_legacydn", "identity_conflict",
        ],
    },
    "record_quality": {
        "label": "Records with something uncertain about them",
        "codes": [
            "no_date", "implausible_date", "unknown_timezone",
            "low_confidence_text", "orphan_reply", "missing_blob",
            "unresolved_recurrence",
        ],
    },
}

_CODE_CLASS = {
    code: name for name, spec in CLASSES.items() for code in spec["codes"]
}


def _conn(request: Request):
    return request.app.state.db()


class StateRequest(BaseModel):
    state: str
    note: str | None = None


@router.get("/findings/summary")
def summary(request: Request) -> dict[str, Any]:
    """Counts by severity and by class, plus the banner sentence.

    The sentence is built here rather than in the browser so the CLI, the
    banner and any export all say the same thing about the same archive.
    """
    conn = _conn(request)

    by_severity = {s: 0 for s in Severity}
    for row in conn.execute(
        "SELECT severity, COUNT(*) AS n FROM findings "
        "WHERE state IN ('open', 'acknowledged') GROUP BY severity"
    ):
        by_severity[row["severity"]] = int(row["n"])

    by_class = {name: 0 for name in CLASSES}
    for row in conn.execute(
        "SELECT code, COUNT(*) AS n FROM findings "
        "WHERE state IN ('open', 'acknowledged') GROUP BY code"
    ):
        by_class[_CODE_CLASS.get(row["code"], "record_quality")] += int(row["n"])

    total_open = sum(by_severity.values())

    loss_row = conn.execute(
        "SELECT SUM(estimated_loss) AS loss FROM findings "
        "WHERE state IN ('open', 'acknowledged') AND estimated_loss > 0"
    ).fetchone()
    estimated_loss = int(loss_row["loss"] or 0) if loss_row else 0

    explained = conn.execute(
        "SELECT COUNT(*) AS n FROM findings WHERE state = 'explained'"
    ).fetchone()["n"]
    resolved = conn.execute(
        "SELECT COUNT(*) AS n FROM findings WHERE state = 'resolved'"
    ).fetchone()["n"]

    worst = None
    for severity in (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.INFO):
        if by_severity.get(severity):
            worst = severity
            break

    return {
        "total_open": total_open,
        "by_severity": by_severity,
        "by_class": by_class,
        "estimated_loss": estimated_loss,
        "explained": int(explained or 0),
        "resolved": int(resolved or 0),
        "worst_severity": worst,
        "sentence": _banner_sentence(conn, by_class, by_severity, estimated_loss),
    }


def _banner_sentence(conn, by_class, by_severity, estimated_loss: int) -> str:
    """The banner text, in the shape spec section 10 asks for.

    "2 files could not be fully read - 3 unexplained gaps - 6 people to confirm"
    """
    parts: list[str] = []

    unreadable = conn.execute(
        "SELECT COUNT(DISTINCT source_file_id) AS n FROM findings "
        "WHERE state IN ('open','acknowledged') AND source_file_id IS NOT NULL "
        "AND code IN ('read_failure', 'partial_parse', 'needs_password', 'empty_tree')"
    ).fetchone()["n"]
    if unreadable:
        parts.append(
            f"{unreadable} file{'s' if unreadable != 1 else ''} could not be read in full"
        )

    gaps = conn.execute(
        "SELECT COUNT(*) AS n FROM findings WHERE state IN ('open','acknowledged') "
        "AND code IN ('hard_gap', 'source_contradiction')"
    ).fetchone()["n"]
    if gaps:
        parts.append(f"{gaps} unexplained gap{'s' if gaps != 1 else ''}")

    people = conn.execute(
        "SELECT COUNT(*) AS n FROM findings WHERE state IN ('open','acknowledged') "
        "AND code IN ('under_merged', 'over_merged_risk', 'self_identity_unclaimed')"
    ).fetchone()["n"]
    if people:
        parts.append(f"{people} {'people' if people != 1 else 'person'} to confirm")

    other = by_class.get("record_quality", 0)
    if other:
        parts.append(
            f"{other} record{'s' if other != 1 else ''} with something uncertain"
        )

    mismatches = conn.execute(
        "SELECT COUNT(*) AS n FROM findings WHERE state IN ('open','acknowledged') "
        "AND code IN ('magic_mismatch', 'zero_or_tiny', 'orphaned_ost')"
    ).fetchone()["n"]
    if mismatches:
        parts.append(
            f"{mismatches} file{'s' if mismatches != 1 else ''} that need a look"
        )

    if not parts:
        return "No problems have been found in what has been read so far."

    sentence = " · ".join(parts)
    if estimated_loss:
        sentence += f" · about {estimated_loss:,} records could not be read"
    return sentence


@router.get("/findings")
def list_findings(
    request: Request,
    severity: str | None = None,
    state: str = "open",
    code: str | None = None,
    klass: str | None = None,
    source_id: int | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """The Problems queue, grouped severity then class."""
    conn = _conn(request)

    where = []
    params: list[Any] = []

    if state == "open":
        where.append("f.state IN ('open', 'acknowledged')")
    elif state != "all":
        where.append("f.state = ?")
        params.append(state)

    if severity:
        where.append("f.severity = ?")
        params.append(severity)
    if code:
        where.append("f.code = ?")
        params.append(code)
    if klass:
        codes = CLASSES.get(klass, {}).get("codes", [])
        if not codes:
            raise HTTPException(
                status_code=400,
                detail=f"{klass!r} is not a group of problems. Choose one of: "
                + ", ".join(CLASSES),
            )
        where.append(f"f.code IN ({','.join('?' * len(codes))})")
        params.extend(codes)
    if source_id:
        where.append("f.source_file_id = ?")
        params.append(source_id)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    rows = conn.execute(
        f"""
        SELECT f.*, sf.path AS source_path, i.subject AS item_subject,
               p.display_name AS person_name
        FROM findings f
        LEFT JOIN source_files sf ON sf.id = f.source_file_id
        LEFT JOIN items i ON i.id = f.item_id
        LEFT JOIN people p ON p.id = f.person_id
        {where_sql}
        ORDER BY CASE f.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                 WHEN 'medium' THEN 2 ELSE 3 END, f.code, f.id
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()

    findings = []
    for row in rows:
        item = dict(row)
        item["class"] = _CODE_CLASS.get(row["code"], "record_quality")
        item["class_label"] = CLASSES[item["class"]]["label"]
        findings.append(item)

    return {
        "findings": findings,
        "classes": {k: v["label"] for k, v in CLASSES.items()},
        "count": len(findings),
    }


@router.get("/coverage-map")
def coverage(request: Request) -> dict[str, Any]:
    """The coverage map: one row per year, one cell per month.

    Spec, screen 7: the single most important picture in the application.
    """
    from ..integrity.coverage import coverage_map

    return coverage_map(_conn(request))


@router.post("/findings/{finding_id}/retry")
def retry(request: Request, finding_id: int) -> dict[str, Any]:
    """Read a file again with the other reader, and report what changed."""
    from ..db import connect
    from ..extract import Extractor

    conn = _conn(request)
    settings = request.app.state.settings

    row = conn.execute(
        "SELECT f.source_file_id, sf.path, sf.parse_backend, sf.item_count "
        "FROM findings f JOIN source_files sf ON sf.id = f.source_file_id "
        "WHERE f.id = ?",
        (finding_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Problem {finding_id} is not about a file, so there is nothing "
                "to read again."
            ),
        )

    source_id = int(row["source_file_id"])
    was_count = int(row["item_count"] or 0)
    was_backend = row["parse_backend"]
    other = "com" if was_backend == "pypff" else "pypff"

    # A separate connection: the retry writes, and the request connection is
    # shared with whatever else this thread is doing.
    own = connect(settings.db_path)
    try:
        from copy import deepcopy

        retry_settings = deepcopy(settings)
        retry_settings.extract.pst_backend = other

        own.execute(
            "UPDATE source_files SET parse_state = 'pending' WHERE id = ?", (source_id,)
        )
        result = Extractor(retry_settings, own).run(source_ids=[source_id])

        after = own.execute(
            "SELECT item_count, parse_backend FROM source_files WHERE id = ?",
            (source_id,),
        ).fetchone()
        now_count = int(after["item_count"] or 0)
        now_backend = after["parse_backend"]
    finally:
        own.close()

    if now_count > was_count:
        message = (
            f"Better: {now_count:,} records now, against {was_count:,} before. "
            f"The extra {now_count - was_count:,} have been added to the archive. "
            "The problem stays on the list with a note that a retry did better."
        )
    elif now_count == was_count:
        message = (
            f"The same: {now_count:,} records, as before. The other reader did no "
            "better, so what is missing is genuinely unreadable."
        )
    else:
        message = (
            f"Worse: {now_count:,} records, against {was_count:,} before. Nothing "
            "was lost - the records already in the archive stay there."
        )

    return {
        "was_count": was_count,
        "was_backend": was_backend,
        "now_count": now_count,
        "now_backend": now_backend,
        "message": message,
        "extract_message": result.message,
    }


@router.post("/findings/{finding_id}/state")
def set_state(request: Request, finding_id: int, body: StateRequest) -> dict[str, Any]:
    """Record the user's decision. Nothing here ever deletes a finding."""
    from ..integrity.engine import set_finding_state

    conn = _conn(request)
    try:
        changed = set_finding_state(conn, finding_id, body.state, body.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not changed:
        raise HTTPException(status_code=404, detail=f"No problem with id {finding_id}.")
    return {"saved": True, "state": body.state}
