"""The findings store: how a problem gets recorded, and what may happen to it.

Spec section 9 makes this a feature, not error handling. The rules encoded here:

* A finding is **upserted**, never duplicated. Running ``recall audit`` five
  times produces one row per real problem, with ``last_seen_utc`` moved on.
* **Nothing auto-resolves.** A finding leaves ``open`` only because the user
  acted, or because a later run proved the condition genuinely gone - and even
  then the row stays, with ``resolved_utc`` set.
* **A finding is never deleted.** There is no delete function in this module,
  and that absence is deliberate.
* A user's note and state are **never overwritten** by a later run. If the user
  explained a gap in March, a re-scan in June does not silently reopen it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from ..logging_setup import get_logger
from ..models import FindingState, Severity

log = get_logger("integrity.engine")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(slots=True)
class Finding:
    """One problem, ready to be written."""

    code: str
    severity: str
    title: str
    detail: str | None = None
    source_file_id: int | None = None
    item_id: int | None = None
    person_id: int | None = None
    period_start: str | None = None
    period_end: str | None = None
    affected_count: int | None = None
    estimated_loss: int | None = None
    evidence: dict[str, Any] | None = None

    def key(self) -> tuple:
        return (
            self.code,
            self.source_file_id if self.source_file_id is not None else -1,
            self.item_id if self.item_id is not None else -1,
            self.person_id if self.person_id is not None else -1,
            self.period_start or "",
        )


def record_finding(conn, finding: Finding) -> int:
    """Write a finding, or refresh the one already there. Returns its id.

    What a repeat run updates: title, detail, counts, evidence, last_seen_utc -
    the facts. What it never touches: state, user_note, resolved_utc - the
    user's decisions.

    A finding that was resolved and has come back is reopened, because the
    condition is real again and hiding it would be a lie. That is recorded in
    the detail so the history is visible.
    """
    now = _utc_now()
    evidence_json = json.dumps(finding.evidence, default=str) if finding.evidence else None

    existing = conn.execute(
        """
        SELECT id, state, resolved_utc FROM findings
        WHERE code = ?
          AND COALESCE(source_file_id, -1) = ?
          AND COALESCE(item_id, -1) = ?
          AND COALESCE(person_id, -1) = ?
          AND COALESCE(period_start, '') = ?
        """,
        finding.key(),
    ).fetchone()

    if existing is None:
        cur = conn.execute(
            """
            INSERT INTO findings
                (code, severity, title, detail, source_file_id, item_id, person_id,
                 period_start, period_end, affected_count, estimated_loss,
                 evidence_json, state, first_seen_utc, last_seen_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
            """,
            (
                finding.code,
                finding.severity,
                finding.title,
                finding.detail,
                finding.source_file_id,
                finding.item_id,
                finding.person_id,
                finding.period_start,
                finding.period_end,
                finding.affected_count,
                finding.estimated_loss,
                evidence_json,
                now,
                now,
            ),
        )
        return int(cur.lastrowid)

    finding_id = int(existing["id"])
    was_resolved = existing["state"] == FindingState.RESOLVED

    detail = finding.detail
    if was_resolved:
        detail = (
            f"{detail or ''}\n\n"
            f"(This was marked resolved on {existing['resolved_utc']}, but the "
            f"condition was found again on {now}.)"
        ).strip()

    conn.execute(
        """
        UPDATE findings SET
            severity = ?, title = ?, detail = ?, period_end = ?,
            affected_count = ?, estimated_loss = ?, evidence_json = ?,
            last_seen_utc = ?,
            state = CASE WHEN state = 'resolved' THEN 'open' ELSE state END,
            resolved_utc = CASE WHEN state = 'resolved' THEN NULL ELSE resolved_utc END
        WHERE id = ?
        """,
        (
            finding.severity,
            finding.title,
            detail,
            finding.period_end,
            finding.affected_count,
            finding.estimated_loss,
            evidence_json,
            now,
            finding_id,
        ),
    )
    return finding_id


def resolve_absent_findings(conn, code: str, present_keys: Iterable[tuple]) -> int:
    """Close findings of one code whose condition a fresh run no longer sees.

    This is the only automatic state change in the program, and it is the one
    the spec allows: "a finding closes only when a later run finds the condition
    genuinely gone, and the row is kept with resolved_utc set, never deleted."

    The user's own states are respected: an `explained` or `wont_fix` finding is
    left exactly as it is.
    """
    keep = {tuple(k) for k in present_keys}
    now = _utc_now()
    closed = 0

    rows = conn.execute(
        "SELECT id, source_file_id, item_id, person_id, period_start "
        "FROM findings WHERE code = ? AND state IN ('open', 'acknowledged')",
        (code,),
    ).fetchall()

    for row in rows:
        key = (
            code,
            row["source_file_id"] if row["source_file_id"] is not None else -1,
            row["item_id"] if row["item_id"] is not None else -1,
            row["person_id"] if row["person_id"] is not None else -1,
            row["period_start"] or "",
        )
        if key in keep:
            continue
        conn.execute(
            "UPDATE findings SET state = 'resolved', resolved_utc = ?, "
            "last_seen_utc = ? WHERE id = ?",
            (now, now, row["id"]),
        )
        closed += 1
    return closed


def set_finding_state(
    conn, finding_id: int, state: str, note: str | None = None
) -> bool:
    """Record a decision the user made about a finding.

    ``explained`` requires a note - an unexplained explanation is not one. The
    row is never removed, whatever the state.
    """
    if state not in set(FindingState):
        raise ValueError(
            f"{state!r} is not a finding state. "
            f"Use one of: {', '.join(sorted(FindingState))}"
        )
    if state == FindingState.EXPLAINED and not (note and note.strip()):
        raise ValueError(
            "Explaining a gap needs a note saying what was happening - "
            'for example "I was not using email yet".'
        )

    now = _utc_now()
    resolved = now if state == FindingState.RESOLVED else None
    cur = conn.execute(
        "UPDATE findings SET state = ?, user_note = COALESCE(?, user_note), "
        "resolved_utc = ?, last_seen_utc = ? WHERE id = ?",
        (state, note, resolved, now, finding_id),
    )
    if cur.rowcount and state == FindingState.EXPLAINED:
        # An explained coverage gap stops nagging in the banner but stays on the
        # timeline forever. Spec 9.2: explaining a gap never deletes it.
        row = conn.execute(
            "SELECT period_start FROM findings WHERE id = ?", (finding_id,)
        ).fetchone()
        if row and row["period_start"]:
            conn.execute(
                "UPDATE coverage_months SET explained_by_user = 1 WHERE month = ?",
                (row["period_start"],),
            )
    return bool(cur.rowcount)


def open_findings(conn, *, severity: str | None = None, code: str | None = None) -> list[dict]:
    """Findings still needing attention, worst first."""
    sql = [
        "SELECT * FROM findings WHERE state IN ('open', 'acknowledged')",
    ]
    params: list[Any] = []
    if severity:
        sql.append("AND severity = ?")
        params.append(severity)
    if code:
        sql.append("AND code = ?")
        params.append(code)
    sql.append(
        "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
        "WHEN 'medium' THEN 2 ELSE 3 END, code, id"
    )
    return [dict(r) for r in conn.execute(" ".join(sql), params)]


def severity_counts(conn) -> dict[str, int]:
    """Open findings by severity, for the health banner."""
    counts = {s: 0 for s in Severity}
    for row in conn.execute(
        "SELECT severity, COUNT(*) AS n FROM findings "
        "WHERE state IN ('open', 'acknowledged') GROUP BY severity"
    ):
        counts[row["severity"]] = int(row["n"])
    return counts


def run_scan_checks(conn, settings, candidates: list) -> int:
    """Every check that can be answered without parsing anything.

    Called at the end of each scan. Returns how many findings were recorded.
    """
    from .sources import scan_time_checks

    return scan_time_checks(conn, settings, candidates)


def run_all_checks(conn, settings, *, checks: set[str] | None = None) -> dict[str, int]:
    """`recall audit` - every check family, on demand.

    ``checks`` selects families: corrupt, gaps, accounts, quality.
    """
    from importlib import import_module

    # (family name, module, function). Imported one at a time so a single
    # broken or missing check family never costs the user the other three.
    families = {
        "corrupt": ("sources", "source_checks"),
        "gaps": ("coverage", "coverage_checks"),
        "accounts": ("accounts", "account_checks"),
        "quality": ("quality", "quality_checks"),
    }
    wanted = checks or set(families)
    unknown = wanted - set(families)
    if unknown:
        raise ValueError(
            f"Unknown check name(s): {', '.join(sorted(unknown))}. "
            f"Choose from: {', '.join(sorted(families))}"
        )

    from ..db import log_error

    results: dict[str, int] = {}
    for name in sorted(wanted):
        module_name, func_name = families[name]
        try:
            module = import_module(f".{module_name}", package=__package__)
            results[name] = getattr(module, func_name)(conn, settings)
        except Exception as exc:  # noqa: BLE001 - one broken check never stops the rest
            log_error(
                conn, "integrity", f"The {name} checks could not run: {exc}", detail=repr(exc)
            )
            log.exception("Integrity check family %s failed", name)
            results[name] = -1
    return results
