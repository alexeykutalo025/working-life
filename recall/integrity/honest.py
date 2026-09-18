"""The honest-count rule, spec section 9.5.

> Anywhere the interface shows a count, total, chart, or export, if any open
> finding affects that number it is displayed with a marker and an explanation
> in reach.

The rule is enforced by making the honest form the only form. Every count in
the API is a ``Count`` envelope carrying its qualifiers with it, so a screen
cannot render the number without also having the reason to hand. There is no
``.bare()`` and no way to ask for the number alone - a caller that wants an
integer has to take ``.value`` and thereby say so in its own code.

A qualified number is never rounded into a clean one, and a partial count is
never presented as complete.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..models import SEVERITY_ORDER, Severity

#: Findings that mean records are missing from a count, with how to say so.
#: Anything not listed here affects quality rather than completeness, and does
#: not qualify a total.
_COUNT_AFFECTING = {
    "partial_parse": "could not be read in full",
    "read_failure": "could not be read at all",
    "needs_password": "is locked with a password",
    "empty_tree": "gave up no records at all",
    "backend_disagreement": "was read differently by the two readers",
    "hard_gap": "has a period with nothing in it",
    "source_contradiction": "is missing a period it should cover",
}


@dataclass(slots=True)
class Qualifier:
    """One reason a number cannot be presented as the whole story."""

    code: str
    text: str
    estimated_loss: int | None = None
    severity: str = Severity.MEDIUM
    finding_ids: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "text": self.text,
            "estimated_loss": self.estimated_loss,
            "severity": self.severity,
            "finding_ids": self.finding_ids,
        }


@dataclass(slots=True)
class Count:
    """A number, and everything known about why it might be short.

    ``value`` is what was counted and is always exact - it is never adjusted
    upward by an estimate. ``estimated_missing`` is carried alongside, because
    a guess added into a total would make the total a guess.
    """

    value: int
    qualifiers: list[Qualifier] = field(default_factory=list)
    label: str = ""

    @property
    def qualified(self) -> bool:
        return bool(self.qualifiers)

    @property
    def estimated_missing(self) -> int | None:
        """How many more records we believe exist but could not read.

        None when nothing is known to be missing, which is different from zero:
        zero means "we checked and none", None means "no basis to say".
        """
        losses = [q.estimated_loss for q in self.qualifiers if q.estimated_loss]
        return sum(losses) if losses else None

    @property
    def worst_severity(self) -> str | None:
        if not self.qualifiers:
            return None
        return min((q.severity for q in self.qualifiers), key=lambda s: SEVERITY_ORDER.get(s, 9))

    def sentence(self) -> str:
        """The number as a plain sentence, qualifier attached.

        "12,481 messages - about 8,000 more could not be read from 2 files"
        """
        base = f"{self.value:,}"
        if self.label:
            base += f" {self.label}"
        if not self.qualifiers:
            return base

        missing = self.estimated_missing
        if missing:
            return f"{base} — about {missing:,} more could not be read"
        reasons = "; ".join(q.text for q in self.qualifiers[:2])
        return f"{base} — {reasons}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "label": self.label,
            "qualified": self.qualified,
            "estimated_missing": self.estimated_missing,
            "worst_severity": self.worst_severity,
            "qualifiers": [q.as_dict() for q in self.qualifiers],
            "sentence": self.sentence(),
        }


def qualifiers_for(
    conn,
    *,
    source_ids: list[int] | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
    kinds: list[str] | None = None,
) -> list[Qualifier]:
    """Every open finding that makes a count over this slice incomplete.

    ``source_ids`` restricts to findings about those files. ``period_start`` and
    ``period_end`` are 'YYYY-MM' bounds; a coverage finding overlapping the
    range qualifies a count over that range.
    """
    out: list[Qualifier] = []
    codes = list(_COUNT_AFFECTING)
    placeholders = ",".join("?" * len(codes))

    sql = [
        f"SELECT f.*, sf.path AS source_path FROM findings f "
        f"LEFT JOIN source_files sf ON sf.id = f.source_file_id "
        f"WHERE f.state IN ('open', 'acknowledged') AND f.code IN ({placeholders})"
    ]
    params: list[Any] = list(codes)

    if source_ids:
        sql.append(f"AND f.source_file_id IN ({','.join('?' * len(source_ids))})")
        params.extend(source_ids)

    if period_start or period_end:
        # A coverage finding is in range when its period overlaps. A per-file
        # finding has no period and always applies, because a file that could
        # not be read may have held anything.
        clause = "AND (f.period_start IS NULL"
        if period_start:
            clause += " OR f.period_start >= ?"
            params.append(period_start)
        if period_end:
            clause += " AND f.period_start <= ?" if period_start else " OR f.period_start <= ?"
            params.append(period_end)
        clause += ")"
        sql.append(clause)

    rows = conn.execute(" ".join(sql), params).fetchall()

    # Group by code so the sentence reads "2 files could not be read in full"
    # rather than listing the same phrase twice.
    by_code: dict[str, list] = {}
    for row in rows:
        by_code.setdefault(row["code"], []).append(row)

    for code, group in by_code.items():
        phrase = _COUNT_AFFECTING[code]
        loss = sum(int(r["estimated_loss"] or 0) for r in group) or None
        n = len(group)

        if code in ("hard_gap", "source_contradiction"):
            months = sorted(r["period_start"] for r in group if r["period_start"])
            span = f"{months[0]} to {months[-1]}" if len(months) > 1 else (months[0] if months else "")
            text = (
                f"{n} period{'s' if n != 1 else ''} with no data"
                + (f" ({span})" if span else "")
            )
        else:
            text = f"{n} file{'s' if n != 1 else ''} {phrase}"

        severity = min(
            (r["severity"] for r in group), key=lambda s: SEVERITY_ORDER.get(s, 9)
        )
        out.append(
            Qualifier(
                code=code,
                text=text,
                estimated_loss=loss,
                severity=severity,
                finding_ids=[int(r["id"]) for r in group],
            )
        )

    out.sort(key=lambda q: SEVERITY_ORDER.get(q.severity, 9))
    return out


def count_items(
    conn,
    *,
    kind: str | None = None,
    label: str = "",
    source_ids: list[int] | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
    where: str | None = None,
    params: list | None = None,
) -> Count:
    """Count items, and attach every reason the number may be short."""
    sql = ["SELECT COUNT(*) AS n FROM items i WHERE 1=1"]
    args: list[Any] = []

    if kind:
        sql.append("AND i.kind = ?")
        args.append(kind)
    if period_start:
        sql.append("AND i.occurred_utc >= ?")
        args.append(period_start + "-01T00:00:00Z")
    if period_end:
        sql.append("AND i.occurred_utc < ?")
        args.append(_month_after(period_end))
    if source_ids:
        sql.append(
            f"AND EXISTS (SELECT 1 FROM item_sources s WHERE s.item_id = i.id "
            f"AND s.source_file_id IN ({','.join('?' * len(source_ids))}))"
        )
        args.extend(source_ids)
    if where:
        sql.append(f"AND ({where})")
        args.extend(params or [])

    row = conn.execute(" ".join(sql), args).fetchone()
    value = int(row["n"] or 0)

    return Count(
        value=value,
        label=label,
        qualifiers=qualifiers_for(
            conn,
            source_ids=source_ids,
            period_start=period_start,
            period_end=period_end,
            kinds=[kind] if kind else None,
        ),
    )


def undated_count(conn, kind: str | None = None) -> int:
    """Records with no usable date, which sit outside every period total.

    These are never folded into a dated total, and never given a date. They
    have their own visible bucket.
    """
    sql = "SELECT COUNT(*) AS n FROM items WHERE occurred_utc IS NULL"
    args: list[Any] = []
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    row = conn.execute(sql, args).fetchone()
    return int(row["n"] or 0)


def _month_after(month: str) -> str:
    """'2003-12' to '2004-01-01T00:00:00Z', for an exclusive upper bound."""
    year, _, mon = month.partition("-")
    y, m = int(year), int(mon or 1)
    if m >= 12:
        return f"{y + 1:04d}-01-01T00:00:00Z"
    return f"{y:04d}-{m + 1:02d}-01T00:00:00Z"
