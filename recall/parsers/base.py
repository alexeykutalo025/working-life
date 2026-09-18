"""What every parser is, and what every parser promises.

A parser takes one file and yields ``ParsedItem`` objects. It knows nothing
about the database, dedup, people or search. That separation is what lets a
format as awkward as Outlook Express DBX sit beside stdlib ``mailbox`` without
either one leaking into the rest of the program.

Three promises every parser keeps:

1. **It never writes to the file it is reading.** Files are opened 'rb'.
2. **It never raises past its caller.** A file that cannot be read produces a
   ``ParseOutcome`` with the exact exception and whatever was salvaged. One
   corrupt file must not end a twelve-hour run.
3. **It reports what it thinks it missed.** ``ParseOutcome.claimed_count`` is
   the store's own idea of how much it holds. The difference between that and
   what came out is the ``estimated_loss`` the user actually cares about.
"""

from __future__ import annotations

import abc
from pathlib import Path
from typing import Iterator

from ..logging_setup import get_logger
from ..models import Kind, ParsedItem, ParseOutcome

log = get_logger("parsers")


class ParserError(Exception):
    """A file could not be parsed. The message is shown to the user as-is."""


class Parser(abc.ABC):
    """One file format."""

    #: Extensions this parser handles, lowercase with the dot.
    extensions: frozenset[str] = frozenset()

    #: Which kinds this parser can produce. Used to skip whole files when the
    #: user asked for calendar only, so a phase-1 run does not read 80 GB of mail.
    produces: frozenset[str] = frozenset()

    #: Short name recorded in ``source_files.parse_backend``.
    name: str = "unknown"

    def __init__(self, path: str | Path, *, sample_limit: int = 0) -> None:
        self.path = Path(path)
        self.sample_limit = sample_limit
        self.outcome = ParseOutcome(source_path=str(self.path), backend=self.name)

    @abc.abstractmethod
    def parse(self, kinds: frozenset[str] | None = None) -> Iterator[ParsedItem]:
        """Yield every record in the file.

        ``kinds`` restricts what is produced. Implementations must honour it
        cheaply where the format allows - skipping a PST's mail folders when
        only calendar was asked for is the difference between minutes and hours.
        """
        raise NotImplementedError

    def close(self) -> None:
        """Release any handle. Always called, even after a failure."""

    def __enter__(self) -> "Parser":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- helpers for subclasses ------------------------------------------

    def _wants(self, kinds: frozenset[str] | None, kind: str) -> bool:
        return kinds is None or kind in kinds

    def _limit_reached(self, produced: int) -> bool:
        return bool(self.sample_limit) and produced >= self.sample_limit


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: list[type[Parser]] = []

#: Every parser module, imported by ``load_all_parsers``. A format is added here
#: when its reader exists, so the log never warns about a reader that was never
#: meant to be present.
PARSER_MODULES: tuple[str, ...] = (
    "ics", "vcs", "vcf", "wab", "eml", "msg", "olm", "dbx", "mbx", "pst_backend",
)


def register(parser_cls: type[Parser]) -> type[Parser]:
    """Decorator. Adds a parser to the table used by ``parser_for``."""
    _REGISTRY.append(parser_cls)
    return parser_cls


def parser_for(path: str | Path, ext: str | None = None) -> type[Parser] | None:
    """Which parser handles this file, or None when nothing does.

    None is a real answer, reported to the user as "Recall cannot read this
    kind of file", rather than being passed to something that will misread it.
    """
    ext = (ext or Path(path).suffix).lower()
    for parser_cls in _REGISTRY:
        if ext in parser_cls.extensions:
            return parser_cls
    return None


def supported_extensions() -> frozenset[str]:
    out: set[str] = set()
    for parser_cls in _REGISTRY:
        out |= set(parser_cls.extensions)
    return frozenset(out)


def parsers_producing(kind: str) -> list[type[Parser]]:
    return [p for p in _REGISTRY if kind in p.produces]


def load_all_parsers() -> None:
    """Import every parser module so the registry is populated.

    Import failures are reported rather than swallowed: a missing library means
    a whole file format is unreadable, and the user needs to know which.
    """
    from importlib import import_module

    for module in PARSER_MODULES:
        try:
            import_module(f".{module}", package=__package__)
        except ImportError as exc:
            log.warning(
                "The %s reader is not available (%s). Files of that type will be "
                "listed but not read.", module, exc,
            )


__all__ = [
    "Kind",
    "Parser",
    "ParserError",
    "ParseOutcome",
    "ParsedItem",
    "load_all_parsers",
    "parser_for",
    "parsers_producing",
    "register",
    "supported_extensions",
]
