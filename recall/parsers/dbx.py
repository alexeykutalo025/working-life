"""Outlook Express message databases (.dbx): found and listed, not read.

Recall used to parse DBX with a from-scratch reader, because no maintained
Python library reads the format and Microsoft never documented it. That reader
has been removed.

It was not merely incomplete. Its model of the message attribute table was
wrong: it treated a message's sender-name attribute as the pointer to the
message body, which meant every message carrying a sender name - every real
message - was dropped. Worse, it reported that loss as "the file's index is
damaged part-way through", blaming a user's irreplaceable mail for a defect in
the reader. Outlook Express files really were notorious for corruption, so the
false explanation was plausible enough to be believed. Its tests passed because
the fixtures were built from the same wrong assumptions as the reader.

So this parser does not pretend. A .dbx is still found by the scanner, still
recorded with its size, location and signature, and still shown in the sources
list. It is reported as a file Recall will not read, and it yields nothing -
which is worse than reading it correctly, and far better than inventing an
archive out of a format we do not actually understand.

Reinstating a reader means validating the format against real .dbx files, not
against fixtures written from the reader's own assumptions.
"""

from __future__ import annotations

from typing import Iterator

from ..logging_setup import get_logger
from ..models import Kind, ParsedItem
from .base import Parser, register

log = get_logger("parsers.dbx")


@register
class DbxParser(Parser):
    """Identifies an Outlook Express folder file and declines to read it."""

    extensions = frozenset({".dbx"})
    produces = frozenset({Kind.MESSAGE})
    name = "dbx"

    def parse(self, kinds: frozenset[str] | None = None) -> Iterator[ParsedItem]:
        if not self._wants(kinds, Kind.MESSAGE):
            return

        try:
            size = self.path.stat().st_size
        except OSError as exc:
            self.outcome.error = f"could not be opened: {exc}"
            self.outcome.error_detail = repr(exc)
            return

        log.info("%s is a DBX file; Recall does not read this format", self.path.name)

        self.outcome.error = (
            "Outlook Express (.dbx) files are not read by Recall. The file has "
            "been found and recorded, but nothing has been taken from it."
        )
        self.outcome.add_finding(
            "read_failure",
            "high",
            f"{self.path.name} is an Outlook Express folder that Recall does not read",
            "This is an Outlook Express 5 or 6 message database. Recall can find "
            "these files but does not read what is inside them, so any messages "
            "it holds are not in your archive.\n\n"
            "Recall had a reader for this format and it was withdrawn: it was "
            "getting messages wrong, and reporting the ones it lost as damage to "
            "your file. A reader that quietly misreads your mail is worse than no "
            "reader, so there is no reader.\n\n"
            "What to do: if this file matters, the messages can usually be "
            "recovered by importing it into Outlook Express or Windows Mail on an "
            "older machine and exporting to Outlook, which produces a .pst that "
            "Recall reads properly.\n\n"
            "The file has not been changed - Recall never writes to your files.",
            {"path": str(self.path), "size_bytes": size},
        )
        return
        yield  # pragma: no cover - makes this a generator
