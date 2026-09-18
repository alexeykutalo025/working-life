"""Turning what the user typed into an FTS5 query, safely.

Spec, screen 3: quoted phrases, ``AND``/``OR``/``NOT``, and ``NEAR``.

The difficulty is that FTS5's own syntax is unforgiving, and a 72-year-old
typing ``invoice (fitzgerald`` gets a syntax error rather than results. So the
input is tokenised here and a valid query is rebuilt from the tokens:

* a quoted phrase stays a phrase;
* ``AND``, ``OR``, ``NOT`` and ``NEAR`` in capitals are operators, and the
  same words in lower case are just words, because "and" appears in a great
  many sentences;
* an unbalanced quote closes itself;
* a bare word with punctuation in it - ``tim@example.com``, ``4,500`` - is
  quoted, because FTS5 would otherwise read the punctuation as syntax;
* anything left that FTS5 still refuses falls back to a phrase search over the
  whole input, which always works.

The result is that a search never produces an error message. It produces
results, or it produces nothing and says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..logging_setup import get_logger

log = get_logger("search.query")

#: Operators, recognised only in capitals.
_OPERATORS = {"AND", "OR", "NOT", "NEAR"}

#: The columns of items_fts, for a `subject:` style prefix.
COLUMNS = ("subject", "body", "participants", "location", "attachment_names",
           "attachment_text")

_FIELD_ALIASES = {
    # The columns under their own names, for anyone who knows them...
    **{column: column for column in COLUMNS},
    # ...and the words a person would actually type.
    "subject": "subject",
    "title": "subject",
    "body": "body",
    "text": "body",
    "from": "participants",
    "to": "participants",
    "person": "participants",
    "people": "participants",
    "who": "participants",
    "where": "location",
    "location": "location",
    "attachment": "attachment_names",
    "attachments": "attachment_names",
    "file": "attachment_names",
    "filename": "attachment_names",
    "inside": "attachment_text",
    "contents": "attachment_text",
}

#: A bare word that FTS5 will accept without quoting.
_SAFE_WORD = re.compile(r"^[A-Za-z0-9_À-￿]+\*?$")

_TOKEN = re.compile(
    r"""
      "(?P<phrase>[^"]*)"?            # a quoted phrase, closing quote optional
    | (?P<field>[A-Za-z_]+):          # a field prefix, e.g. subject: or from:
    | (?P<word>[^\s"]+)               # anything else up to whitespace
    """,
    re.VERBOSE,
)


@dataclass
class ParsedQuery:
    """What the user typed, and what FTS5 will be asked."""

    raw: str
    fts: str
    terms: list[str] = field(default_factory=list)
    phrases: list[str] = field(default_factory=list)
    used_operators: list[str] = field(default_factory=list)
    fell_back: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.fts.strip()

    def describe(self) -> str:
        """What Recall understood, in words, so a surprising result is explicable."""
        if self.is_empty:
            return "Nothing to search for."
        parts = []
        if self.phrases:
            parts.append(
                "the exact phrase " + ", ".join(f"“{p}”" for p in self.phrases)
            )
        if self.terms:
            parts.append("the words " + ", ".join(self.terms))
        if self.used_operators:
            parts.append("using " + ", ".join(sorted(set(self.used_operators))))
        if self.fell_back:
            return (
                "That search had something in it Recall could not read as a "
                f"search, so it looked for the whole thing as one phrase: "
                f"“{self.raw.strip()}”"
            )
        return "Looking for " + "; ".join(parts) if parts else "Nothing to search for."


def parse(text: str) -> ParsedQuery:
    """Build a valid FTS5 query out of whatever the user typed."""
    raw = (text or "").strip()
    if not raw:
        return ParsedQuery(raw="", fts="")

    pieces: list[str] = []
    terms: list[str] = []
    phrases: list[str] = []
    operators: list[str] = []
    pending_field: str | None = None

    for match in _TOKEN.finditer(raw):
        phrase = match.group("phrase")
        field_name = match.group("field")
        word = match.group("word")

        if field_name is not None:
            column = _FIELD_ALIASES.get(field_name.lower().strip("_"))
            if column:
                pending_field = column
            else:
                # Not a field Recall knows - it is part of the search text,
                # like "re:" in a subject line.
                pieces.append(_quote(field_name + ":"))
                terms.append(field_name)
            continue

        if phrase is not None:
            phrase = phrase.strip()
            if phrase:
                phrases.append(phrase)
                pieces.append(_with_field(pending_field, f'"{_escape(phrase)}"'))
            pending_field = None
            continue

        if word is None:
            continue

        if word in _OPERATORS:
            operators.append(word)
            # NEAR needs its own parenthesised form, handled below.
            pieces.append(word)
            continue

        if word.upper() in _OPERATORS and word not in _OPERATORS:
            # "and" in lower case is a word people write in sentences.
            terms.append(word)
            pieces.append(_with_field(pending_field, _quote(word)))
            pending_field = None
            continue

        terms.append(word)
        pieces.append(_with_field(pending_field, _quote(word)))
        pending_field = None

    fts = _assemble(pieces)
    return ParsedQuery(
        raw=raw, fts=fts, terms=terms, phrases=phrases, used_operators=operators
    )


def _assemble(pieces: list[str]) -> str:
    """Join the tokens into something FTS5 will accept.

    An operator with nothing on one side of it is dropped rather than passed
    on: "invoice AND" is what a half-typed search looks like, and it should
    find invoices, not fail.
    """
    cleaned: list[str] = []
    for piece in pieces:
        if piece in _OPERATORS:
            if not cleaned or cleaned[-1] in _OPERATORS:
                continue          # leading or doubled operator
            cleaned.append(piece)
        else:
            cleaned.append(piece)

    while cleaned and cleaned[-1] in _OPERATORS:
        cleaned.pop()

    if not cleaned:
        return ""

    # NEAR in FTS5 is NEAR(a b, n), not "a NEAR b". Rewrite the infix form
    # people actually type.
    out: list[str] = []
    i = 0
    while i < len(cleaned):
        if (
            cleaned[i] == "NEAR"
            and out
            and i + 1 < len(cleaned)
            and cleaned[i + 1] not in _OPERATORS
        ):
            left = out.pop()
            right = cleaned[i + 1]
            out.append(f"NEAR({left} {right}, 10)")
            i += 2
            continue
        out.append(cleaned[i])
        i += 1

    return " ".join(out)


def _with_field(column: str | None, term: str) -> str:
    return f"{column} : {term}" if column else term


def _quote(word: str) -> str:
    """Quote a word unless FTS5 will take it bare.

    tim@example.com, 4,500 and re: all contain characters FTS5 reads as syntax.
    """
    stripped = word.strip()
    if not stripped:
        return '""'
    if _SAFE_WORD.match(stripped):
        return stripped
    return f'"{_escape(stripped)}"'


def _escape(text: str) -> str:
    return text.replace('"', '""')


def safe_query(conn, text: str) -> ParsedQuery:
    """Parse, and prove FTS5 accepts it. Falls back to a phrase if not.

    A search must never show the user a syntax error. If the rebuilt query is
    still refused, the whole input becomes one phrase, which FTS5 always takes.
    """
    parsed = parse(text)
    if parsed.is_empty:
        return parsed

    try:
        conn.execute(
            "SELECT rowid FROM items_fts WHERE items_fts MATCH ? LIMIT 1",
            (parsed.fts,),
        ).fetchone()
        return parsed
    except Exception as exc:  # noqa: BLE001 - any FTS5 complaint
        log.debug("FTS5 refused %r (%s); falling back to a phrase", parsed.fts, exc)

    fallback = f'"{_escape(parsed.raw)}"'
    try:
        conn.execute(
            "SELECT rowid FROM items_fts WHERE items_fts MATCH ? LIMIT 1", (fallback,)
        ).fetchone()
    except Exception:  # noqa: BLE001 - nothing left to try
        return ParsedQuery(raw=parsed.raw, fts="", fell_back=True)

    return ParsedQuery(
        raw=parsed.raw,
        fts=fallback,
        phrases=[parsed.raw],
        fell_back=True,
    )
