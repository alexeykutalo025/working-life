"""Building the search index.

``search_docs`` is one materialised row per item, because FTS5 external-content
tables cannot span joins and the things worth searching for are spread across
four tables: the subject and body on ``items``, the people on
``participations``, the filenames and extracted text on ``attachments``.

The index is built incrementally where possible. ``recall index --rebuild``
starts from scratch, which is the right answer after any change to how text is
extracted - an index half-built by two different versions of the program would
find some things and not others, with nothing to say which.
"""

from __future__ import annotations

import re
from typing import Iterator

from ..logging_setup import get_logger

log = get_logger("search.indexer")

#: How much body text goes into the index per item. Enough to find anything a
#: person would search for; short enough that a 90 MB mail merge does not take
#: the index with it.
MAX_BODY_CHARS = 200_000

#: Attachment text per item, across all of them.
MAX_ATTACHMENT_CHARS = 200_000


def build_index(conn, *, rebuild: bool = False, progress=None) -> dict[str, int]:
    """Fill ``search_docs`` and, through its triggers, ``items_fts``.

    Returns counts the caller can report.
    """
    if rebuild:
        log.info("Rebuilding the search index from scratch")
        conn.execute("DELETE FROM search_docs")
        conn.execute("INSERT INTO items_fts(items_fts) VALUES ('delete-all')")

    todo = [
        int(r["id"])
        for r in conn.execute(
            "SELECT i.id FROM items i "
            "WHERE NOT EXISTS (SELECT 1 FROM search_docs d WHERE d.item_id = i.id) "
            "ORDER BY i.id"
        )
    ]

    total = len(todo)
    written = 0

    for batch_start in range(0, total, 500):
        batch = todo[batch_start : batch_start + 500]
        rows = list(_documents(conn, batch))
        conn.executemany(
            "INSERT INTO search_docs(item_id, subject, body, participants, "
            "location, attachment_names, attachment_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        written += len(rows)
        if progress is not None:
            progress(written, total)

    # An index built by triggers can drift if a trigger was ever missing;
    # optimize also merges the b-tree, which matters at a few hundred thousand
    # rows.
    conn.execute("INSERT INTO items_fts(items_fts) VALUES ('optimize')")

    log.info("Search index: %d document(s) written", written)
    return {"written": written, "total_items": _count(conn, "items"),
            "indexed": _count(conn, "search_docs")}


def reindex_items(conn, item_ids: list[int]) -> int:
    """Refresh specific items, after their attachments or people changed."""
    if not item_ids:
        return 0
    placeholders = ",".join("?" * len(item_ids))
    conn.execute(f"DELETE FROM search_docs WHERE item_id IN ({placeholders})", item_ids)
    rows = list(_documents(conn, item_ids))
    conn.executemany(
        "INSERT INTO search_docs(item_id, subject, body, participants, location, "
        "attachment_names, attachment_text) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def _documents(conn, item_ids: list[int]) -> Iterator[tuple]:
    """One searchable document per item."""
    if not item_ids:
        return
    placeholders = ",".join("?" * len(item_ids))

    participants: dict[int, list[str]] = {}
    for row in conn.execute(
        f"SELECT p.item_id, i.address, i.raw_display_name, pe.display_name "
        f"FROM participations p "
        f"JOIN identities i ON i.id = p.identity_id "
        f"LEFT JOIN people pe ON pe.id = p.person_id "
        f"WHERE p.item_id IN ({placeholders})",
        item_ids,
    ):
        words = participants.setdefault(int(row["item_id"]), [])
        for value in (row["address"], row["raw_display_name"], row["display_name"]):
            if value and value not in words:
                words.append(value)

    attachments: dict[int, tuple[list[str], list[str]]] = {}
    for row in conn.execute(
        f"SELECT item_id, filename, extracted_text FROM attachments "
        f"WHERE item_id IN ({placeholders})",
        item_ids,
    ):
        names, texts = attachments.setdefault(int(row["item_id"]), ([], []))
        if row["filename"]:
            names.append(row["filename"])
        if row["extracted_text"]:
            texts.append(row["extracted_text"])

    for row in conn.execute(
        f"SELECT id, kind, subject, body_text, body_html, location, "
        f"conversation_topic, contact_json FROM items WHERE id IN ({placeholders})",
        item_ids,
    ):
        item_id = int(row["id"])
        names, texts = attachments.get(item_id, ([], []))

        body = row["body_text"] or ""
        if not body and row["body_html"]:
            from ..normalize.text import html_to_text

            body = html_to_text(row["body_html"])

        # A contact's card is its body for search purposes: an organisation or
        # a phone number is exactly what somebody searches a contact by.
        if row["kind"] == "contact" and row["contact_json"]:
            body = (body + "\n" + _contact_words(row["contact_json"])).strip()

        subject = " ".join(
            x for x in (row["subject"], row["conversation_topic"]) if x
        )

        yield (
            item_id,
            _tidy(subject),
            _tidy(body)[:MAX_BODY_CHARS],
            _tidy(" ".join(participants.get(item_id, []))),
            _tidy(row["location"] or ""),
            _tidy(" ".join(names)),
            _tidy(" ".join(texts))[:MAX_ATTACHMENT_CHARS],
        )


def _contact_words(contact_json: str) -> str:
    """Everything on a contact card, flattened into searchable words."""
    import json

    try:
        card = json.loads(contact_json)
    except (TypeError, ValueError):
        return ""
    if not isinstance(card, dict):
        return ""

    words: list[str] = []

    def walk(value):
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
        elif value not in (None, "", True, False):
            words.append(str(value))

    walk(card)
    return " ".join(words)


def _tidy(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _count(conn, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])


def index_health(conn) -> dict:
    """Is the index complete? Reported rather than assumed.

    A search over a partly-built index quietly misses things, which is the same
    failure as a missing decade: an answer that looks complete and is not.
    """
    items = _count(conn, "items")
    indexed = _count(conn, "search_docs")
    missing = items - indexed

    fts = int(
        conn.execute("SELECT COUNT(*) AS n FROM items_fts").fetchone()["n"]
    )

    return {
        "items": items,
        "indexed": indexed,
        "missing": max(0, missing),
        "fts_rows": fts,
        "complete": missing <= 0 and fts >= indexed,
        "note": (
            ""
            if missing <= 0
            else f"{missing:,} record(s) are not in the search index yet, so a "
                 "search will not find them. Run:  recall index"
        ),
    }
