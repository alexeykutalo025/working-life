"""Exports, and the integrity statement none of them may leave without.

Spec section 9.6, stated as a defect rather than a nicety:

> Every export ships a companion integrity statement ... It names exactly what
> was uncertain or missing in *that exported set*. An export that silently omits
> known problems is a defect, not a nicety.

So the statement is not a step an exporter can forget. ``Exporter.write`` is
final: it calls the subclass for the data and then writes the statement itself.
A subclass that wants to skip it has nowhere to put that decision.
"""

from __future__ import annotations

import abc
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..db import IN_IDS, ids_param
from ..integrity.honest import Count, Qualifier, qualifiers_for, undated_count
from ..logging_setup import get_logger
from ..models import SEVERITY_ORDER

log = get_logger("export")


class ExportError(Exception):
    """An export could not be produced. The message says why."""


@dataclass
class ExportSelection:
    """Which records an export covers, and how it was chosen.

    Recorded verbatim in the integrity statement so the reader of a CSV in five
    years knows what question it answered.
    """

    description: str = "every record in the archive"
    kinds: list[str] = field(default_factory=list)
    query: str | None = None
    source_ids: list[int] = field(default_factory=list)
    period_start: str | None = None
    period_end: str | None = None
    item_ids: list[int] = field(default_factory=list)


@dataclass
class IntegrityStatement:
    """What was uncertain or missing in one exported set."""

    generated_utc: str
    selection: ExportSelection
    exported_count: int
    undated_count: int
    qualifiers: list[Qualifier]
    findings: list[dict]
    archive_path: str

    @property
    def estimated_missing(self) -> int | None:
        losses = [q.estimated_loss for q in self.qualifiers if q.estimated_loss]
        return sum(losses) if losses else None

    @property
    def is_clean(self) -> bool:
        return not self.qualifiers and not self.findings and not self.undated_count

    def as_text(self) -> str:
        """The companion .txt, written for someone who is not a programmer."""
        lines: list[str] = []
        add = lines.append

        add("=" * 72)
        add("WHAT THIS EXPORT DOES AND DOES NOT CONTAIN")
        add("=" * 72)
        add("")
        add("Read this before you rely on the numbers in the file beside it.")
        add("")
        add(f"Made on:          {self.generated_utc} (UTC)")
        add(f"From the archive: {self.archive_path}")
        add(f"Covers:           {self.selection.description}")
        if self.selection.kinds:
            add(f"Kinds of record:  {', '.join(self.selection.kinds)}")
        if self.selection.query:
            add(f"Search used:      {self.selection.query}")
        if self.selection.period_start or self.selection.period_end:
            add(
                f"Period:           {self.selection.period_start or 'the beginning'}"
                f" to {self.selection.period_end or 'the end'}"
            )
        add("")
        add(f"Records exported: {self.exported_count:,}")

        if self.estimated_missing:
            add("")
            add("-" * 72)
            add(f"ABOUT {self.estimated_missing:,} MORE RECORDS COULD NOT BE READ")
            add("-" * 72)
            add("")
            add(
                "This export is not the whole story. The count above is exactly "
                "what Recall could read; it is not a guess, and it has not been "
                "rounded. The records below are ones Recall knows it could not "
                "reach, so the true total is higher."
            )

        if self.undated_count:
            add("")
            add(
                f"{self.undated_count:,} record(s) in the archive have no usable "
                "date at all."
            )
            add(
                "They are NOT included in any date range, and no date has been "
                "invented for them. Look at the Undated list in Recall to see them."
            )

        if self.qualifiers:
            add("")
            add("-" * 72)
            add("WHY THESE NUMBERS ARE NOT COMPLETE")
            add("-" * 72)
            for q in self.qualifiers:
                add("")
                add(f"  * {q.text}")
                if q.estimated_loss:
                    add(f"    About {q.estimated_loss:,} records are affected.")
                add(f"    (Severity: {q.severity}. Code: {q.code}.)")

        if self.findings:
            add("")
            add("-" * 72)
            add("EVERY KNOWN PROBLEM AFFECTING THIS EXPORT")
            add("-" * 72)
            by_severity: dict[str, list[dict]] = {}
            for f in self.findings:
                by_severity.setdefault(f["severity"], []).append(f)

            for severity in sorted(by_severity, key=lambda s: SEVERITY_ORDER.get(s, 9)):
                add("")
                add(f"{severity.upper()} ({len(by_severity[severity])})")
                for f in by_severity[severity]:
                    add("")
                    add(f"  {f['title']}")
                    if f.get("estimated_loss"):
                        add(f"    Records believed unreadable: {f['estimated_loss']:,}")
                    if f.get("affected_count"):
                        add(f"    Records affected: {f['affected_count']:,}")
                    if f.get("user_note"):
                        add(f"    Your note: {f['user_note']}")
                    detail = (f.get("detail") or "").strip()
                    for line in detail.splitlines():
                        add(f"    {line}" if line.strip() else "")

        if self.is_clean:
            add("")
            add("-" * 72)
            add("NO KNOWN PROBLEMS")
            add("-" * 72)
            add("")
            add(
                "Recall found nothing wrong with the records in this export. "
                "Every one of them has a date, and every file they came out of "
                "was read in full."
            )
            add("")
            add(
                "That is a statement about what Recall could check. It cannot "
                "know about a mailbox that was never on this computer."
            )

        add("")
        add("=" * 72)
        add(
            "Recall never invents a date, a timezone, a sender or a total. "
            "Where something is missing, it says so - here."
        )
        add("=" * 72)
        add("")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_utc": self.generated_utc,
            "archive": self.archive_path,
            "selection": {
                "description": self.selection.description,
                "kinds": self.selection.kinds,
                "query": self.selection.query,
                "source_ids": self.selection.source_ids,
                "period_start": self.selection.period_start,
                "period_end": self.selection.period_end,
            },
            "exported_count": self.exported_count,
            "undated_count": self.undated_count,
            "estimated_missing": self.estimated_missing,
            "is_complete": self.is_clean,
            "qualifiers": [q.as_dict() for q in self.qualifiers],
            "findings": self.findings,
        }

    def rows(self) -> list[tuple[str, str]]:
        """Flat (label, value) pairs, for the XLSX Integrity sheet.

        Every row is a fact with a name. No blank spacers and no prose blocks:
        the sheet is a table, so that it reads well *and* parses, and the
        findings have a sheet of their own rather than a paragraph in column B
        where a critical one looked exactly like a note.
        """
        out = [
            ("Made on (UTC)", self.generated_utc),
            ("From the archive", self.archive_path),
            ("Covers", self.selection.description),
            ("Records exported", f"{self.exported_count:,}"),
        ]
        if self.estimated_missing:
            out.append(
                ("Records that could NOT be read", f"about {self.estimated_missing:,}")
            )
            out.append(
                ("Is this export complete?", "NO - see the Problems sheet")
            )
        else:
            out.append(("Is this export complete?", "Nothing is known to be missing"))
        if self.undated_count:
            out.append(
                ("Records with no date (not in any date range)", f"{self.undated_count:,}")
            )
        for q in self.qualifiers:
            out.append(
                (
                    f"Why this count may be short ({q.severity})",
                    q.text
                    + (f" - about {q.estimated_loss:,} records" if q.estimated_loss else ""),
                )
            )
        out.append((
            "Problems listed on the Problems sheet",
            f"{len(self.findings):,}" if self.findings else "none",
        ))
        return out


def build_statement(
    conn,
    selection: ExportSelection,
    exported_count: int,
    archive_path: str | Path,
) -> IntegrityStatement:
    """Gather everything known to be wrong with one exported set."""
    qualifiers = qualifiers_for(
        conn,
        source_ids=selection.source_ids or None,
        period_start=selection.period_start,
        period_end=selection.period_end,
    )

    finding_ids: set[int] = set()
    for q in qualifiers:
        finding_ids.update(q.finding_ids)

    # Findings attached to the exported items themselves - a guessed encoding,
    # an unknown timezone - qualify the records rather than the total, and
    # belong in the statement just as much.
    if selection.item_ids:
        for row in conn.execute(
            f"SELECT id FROM findings WHERE state IN ('open','acknowledged') "
            f"AND item_id {IN_IDS}",
            (ids_param(selection.item_ids),),
        ):
            finding_ids.add(int(row["id"]))

    findings: list[dict] = []
    if finding_ids:
        findings = [
            dict(r)
            for r in conn.execute(
                f"SELECT id, code, severity, title, detail, estimated_loss, "
                f"affected_count, period_start, period_end, state, user_note "
                f"FROM findings WHERE id {IN_IDS} "
                f"ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
                f"WHEN 'medium' THEN 2 ELSE 3 END, id",
                (ids_param(finding_ids),),
            )
        ]

    return IntegrityStatement(
        generated_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        selection=selection,
        exported_count=exported_count,
        undated_count=undated_count(conn),
        qualifiers=qualifiers,
        findings=findings,
        archive_path=str(archive_path),
    )


class Exporter(abc.ABC):
    """One output format.

    Subclasses implement ``_write_data``. ``write`` is deliberately not
    overridable in spirit: it writes the data and then the statement, so no
    export can leave without one.
    """

    #: File extension, with the dot.
    suffix: str = ".txt"
    #: Human name, for messages.
    format_name: str = "text"
    #: True when the statement goes inside the file rather than beside it.
    statement_is_embedded: bool = False

    def __init__(self, conn, settings) -> None:
        self.conn = conn
        self.settings = settings

    @abc.abstractmethod
    def _write_data(self, rows: Iterator[dict], out_path: Path, statement: IntegrityStatement) -> int:
        """Write the records. Returns how many were written."""

    def write(
        self,
        rows: Iterator[dict],
        out_path: str | Path,
        selection: ExportSelection,
    ) -> tuple[Path, Path | None, IntegrityStatement]:
        """Produce the export and its integrity statement.

        Returns (data file, statement file or None when embedded, statement).
        """
        out_path = Path(out_path)
        if out_path.suffix.lower() != self.suffix:
            out_path = out_path.with_suffix(self.suffix)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        from ..scan.onedrive import assert_not_onedrive

        assert_not_onedrive(out_path.parent)

        # The rows are materialised so the statement can count them exactly.
        # An export that says "12,481 records" must have written 12,481 rows.
        materialized = list(rows)
        selection.item_ids = [
            int(r["id"]) for r in materialized if r.get("id") is not None
        ]

        statement = build_statement(
            self.conn, selection, len(materialized), self.settings.db_path
        )
        written = self._write_data(iter(materialized), out_path, statement)

        if written != len(materialized):
            raise ExportError(
                f"The export wrote {written:,} rows but {len(materialized):,} were "
                "selected. Rather than hand you a file whose count cannot be "
                "trusted, Recall has stopped."
            )

        statement_path: Path | None = None
        if not self.statement_is_embedded:
            statement_path = out_path.with_name(out_path.stem + "_integrity.txt")
            statement_path.write_text(statement.as_text(), encoding="utf-8")

        log.info(
            "Exported %d records to %s%s",
            written,
            out_path,
            f" (statement: {statement_path.name})" if statement_path else " (statement inside)",
        )
        return out_path, statement_path, statement


def json_default(value: Any) -> str:
    """json.dumps fallback that never loses a value it cannot type."""
    return str(value)
