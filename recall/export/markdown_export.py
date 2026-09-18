"""Markdown: one chronological document, meant to be read rather than queried.

This is the export for a person who wants to sit and read a decade, so it is
laid out as prose with headings by year and month, not as a table. The
integrity statement goes in a companion ``_integrity.txt`` as the spec
requires, and a short summary of it also opens the document - a reader working
through forty pages should be told on page one that 2001 is missing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from .base import Exporter, IntegrityStatement

_MONTHS = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


class MarkdownExporter(Exporter):
    suffix = ".md"
    format_name = "Markdown document"

    def __init__(self, conn, settings, columns: list[str]) -> None:
        super().__init__(conn, settings)
        self.columns = columns

    def _write_data(
        self, rows: Iterator[dict], out_path: Path, statement: IntegrityStatement
    ) -> int:
        written = 0
        current_year: str | None = None
        current_month: str | None = None

        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(f"# {statement.selection.description.capitalize()}\n\n")
            fh.write(f"*Made by Recall on {statement.generated_utc} UTC.*\n\n")
            fh.write(self._opening_note(statement))
            fh.write("\n---\n\n")

            for row in rows:
                date = str(row.get("date") or "")
                year = date[:4] if date else None
                month = date[5:7] if len(date) >= 7 else None

                if year != current_year:
                    fh.write(f"\n## {year or 'No date'}\n\n")
                    current_year = year
                    current_month = None
                if month != current_month:
                    if month:
                        fh.write(f"\n### {_MONTHS[int(month)]} {year}\n\n")
                    current_month = month

                fh.write(self._entry(row))
                written += 1

            fh.write("\n---\n\n")
            fh.write(
                f"*{written:,} record(s) in this document. "
                "What is missing or uncertain is set out in full in "
                f"`{out_path.stem}_integrity.txt`, beside this file.*\n"
            )

        return written

    def _opening_note(self, statement: IntegrityStatement) -> str:
        """The warning a reader needs on page one, not on page forty."""
        if statement.is_clean:
            return (
                "> **Nothing is known to be missing from this document.**\n"
                "> Every record here has a date, and every file they came out of "
                "was read in full.\n"
            )

        lines = ["> **This document is not complete, and here is why.**\n>\n"]
        if statement.estimated_missing:
            lines.append(
                f"> About **{statement.estimated_missing:,} more records** exist "
                "that Recall could not read. The count below is exactly what was "
                "read; it has not been rounded or estimated upward.\n>\n"
            )
        for q in statement.qualifiers:
            loss = f" (about {q.estimated_loss:,} records)" if q.estimated_loss else ""
            lines.append(f"> - {q.text}{loss}\n")
        if statement.undated_count:
            lines.append(
                f"> - {statement.undated_count:,} record(s) have no date at all "
                "and appear under \"No date\" at the end.\n"
            )
        lines.append(
            f">\n> The full detail is in `_integrity.txt`, beside this file.\n"
        )
        return "".join(lines)

    def _entry(self, row: dict) -> str:
        """One record, laid out to be read."""
        out: list[str] = []

        subject = row.get("subject") or row.get("display_name") or "(no subject)"
        date = row.get("date") or ""
        start = row.get("start") or row.get("time") or ""

        heading = f"**{_escape(subject)}**"
        when = " ".join(x for x in (date, start) if x)
        out.append(f"{heading}  \n")
        if when:
            tz = row.get("timezone")
            suffix = ""
            if tz == "unknown":
                suffix = " *(timezone not recorded - the exact time is uncertain)*"
            out.append(f"{when}{suffix}  \n")

        for label, key in (
            ("Where", "location"),
            ("From", "from_name"),
            ("To", "to"),
            ("Cc", "cc"),
            ("Organiser", "organizer"),
            ("Attendees", "attendees"),
            ("Company", "organization"),
            ("Email", "emails"),
            ("Category", "category"),
            ("Folder", "folder"),
        ):
            value = row.get(key)
            if value:
                out.append(f"{label}: {_escape(str(value))}  \n")

        if row.get("recurring") == "yes":
            rule = row.get("recurrence_rule") or "rule not recorded"
            out.append(f"Repeats: {_escape(rule)}  \n")

        if row.get("attachment_names"):
            out.append(f"Attachments: {_escape(row['attachment_names'])}  \n")

        body = (row.get("notes") or row.get("body_preview") or "").strip()
        if body:
            out.append("\n")
            for line in body.splitlines():
                out.append(f"> {_escape(line)}\n" if line.strip() else ">\n")

        quality = row.get("data_quality")
        if quality:
            out.append(f"\n*⚠ {_escape(quality)}*  \n")

        source = row.get("source_file")
        if source:
            out.append(f"\n<small>Found in: {_escape(source)}</small>\n")

        out.append("\n")
        return "".join(out)


def _escape(text: str) -> str:
    """Stop archive content from being read as Markdown formatting.

    A 1997 signature of dashes should not silently turn the line above it into
    a heading, and an asterisked list in a message should keep its asterisks.
    """
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace("*", "\\*")
        .replace("_", "\\_")
        .replace("`", "\\`")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
