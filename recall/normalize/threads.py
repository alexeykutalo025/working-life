"""Putting conversations back together.

Spec section 8:

> primary In-Reply-To / References chains; fallback to normalized subject
> (strip RE:, FW:, FWD:, bracketed list tags) plus participant overlap plus a
> time window.

The two halves are kept strictly apart, because they are not equally reliable.
A References chain is a fact the mail client recorded. A subject match is an
inference, and a wrong one merges two unrelated conversations that happened to
be called "Invoice" - which in a forty-year archive is not a rare event.

So the subject fallback requires all three of: the same normalised subject, at
least one participant in common, and a gap short enough to be a reply. Any one
of those alone is not enough, and the chain evidence always wins.

Threading is rebuilt from scratch each time rather than maintained
incrementally. It is a global property - one late-arriving message can join two
existing threads - and an incrementally maintained thread table would drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..logging_setup import get_logger
from .text import normalize_subject

log = get_logger("normalize.threads")

#: How far apart two messages can be and still be the same conversation when
#: only the subject and participants link them. Long, because a thread about a
#: contract really can go quiet for six months and resume.
SUBJECT_WINDOW = timedelta(days=180)

#: A subject too generic to link anything on its own.
_WEAK_SUBJECTS = frozenset({
    "", "hello", "hi", "hey", "thanks", "thank you", "fyi", "test", "meeting",
    "invoice", "update", "question", "quick question", "info", "information",
    "follow up", "reminder", "no subject", "(no subject)", "read receipt",
    "out of office", "автоответ", "delivery status notification",
})


@dataclass
class _Node:
    item_id: int
    message_id: str | None
    in_reply_to: str | None
    references: list[str]
    subject_normalized: str
    occurred_utc: str | None
    participants: set[str] = field(default_factory=set)


class _Union:
    """Union-find. Threading is a partition problem, so this is the right shape."""

    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:       # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Lower id wins, so the result does not depend on insertion order.
            self.parent[max(ra, rb)] = min(ra, rb)


def rebuild_threads(conn) -> dict[str, int]:
    """Recompute every thread. Returns counts for the caller to report."""
    nodes = _load(conn)
    if not nodes:
        conn.execute("DELETE FROM threads")
        conn.execute("UPDATE items SET thread_id = NULL")
        return {"items": 0, "threads": 0, "by_chain": 0, "by_subject": 0}

    union = _Union()
    by_message_id: dict[str, int] = {}
    for node in nodes.values():
        if node.message_id:
            by_message_id.setdefault(node.message_id, node.item_id)

    # --- the reliable half: chains the mail client recorded ---------------
    by_chain = 0
    for node in nodes.values():
        targets = []
        if node.in_reply_to:
            targets.append(node.in_reply_to)
        targets.extend(node.references)

        for target in targets:
            other = by_message_id.get(target)
            if other is not None and other != node.item_id:
                union.union(node.item_id, other)
                by_chain += 1

    # --- the inference: subject plus people plus time ---------------------
    by_subject = 0
    buckets: dict[str, list[_Node]] = {}
    for node in nodes.values():
        subject = node.subject_normalized
        if not subject or subject in _WEAK_SUBJECTS or len(subject) < 6:
            continue
        buckets.setdefault(subject, []).append(node)

    for subject, group in buckets.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda n: n.occurred_utc or "")

        for i, node in enumerate(group):
            for other in group[i + 1 :]:
                if union.find(node.item_id) == union.find(other.item_id):
                    continue
                if not (node.participants & other.participants):
                    continue         # same subject, no-one in common: not linked
                if not _within_window(node.occurred_utc, other.occurred_utc):
                    break            # sorted, so everything after is further away
                union.union(node.item_id, other.item_id)
                by_subject += 1

    # --- write -----------------------------------------------------------
    groups: dict[int, list[int]] = {}
    for item_id in nodes:
        groups.setdefault(union.find(item_id), []).append(item_id)

    conn.execute("UPDATE items SET thread_id = NULL")
    conn.execute("DELETE FROM threads")

    for root, members in groups.items():
        dated = [nodes[m].occurred_utc for m in members if nodes[m].occurred_utc]
        subject = _best_subject(nodes, members)

        cur = conn.execute(
            "INSERT INTO threads(subject_normalized, first_utc, last_utc, message_count) "
            "VALUES (?, ?, ?, ?)",
            (subject, min(dated) if dated else None, max(dated) if dated else None,
             len(members)),
        )
        thread_id = int(cur.lastrowid)
        conn.executemany(
            "UPDATE items SET thread_id = ? WHERE id = ?",
            [(thread_id, m) for m in members],
        )

    log.info(
        "Rebuilt %d thread(s) from %d items (%d links from chains, %d from subject)",
        len(groups), len(nodes), by_chain, by_subject,
    )
    return {
        "items": len(nodes),
        "threads": len(groups),
        "by_chain": by_chain,
        "by_subject": by_subject,
    }


def _load(conn) -> dict[int, _Node]:
    import json

    nodes: dict[int, _Node] = {}
    for row in conn.execute(
        "SELECT id, internet_message_id, in_reply_to, references_json, subject, "
        "occurred_utc FROM items WHERE kind = 'message'"
    ):
        references: list[str] = []
        if row["references_json"]:
            try:
                value = json.loads(row["references_json"])
                if isinstance(value, list):
                    references = [str(v).strip().strip("<>") for v in value if v]
            except (TypeError, ValueError):
                references = []

        nodes[int(row["id"])] = _Node(
            item_id=int(row["id"]),
            message_id=(row["internet_message_id"] or "").strip().strip("<>") or None,
            in_reply_to=(row["in_reply_to"] or "").strip().strip("<>") or None,
            references=references,
            subject_normalized=normalize_subject(row["subject"]),
            occurred_utc=row["occurred_utc"],
        )

    for row in conn.execute(
        "SELECT p.item_id, i.address FROM participations p "
        "JOIN identities i ON i.id = p.identity_id "
        "WHERE i.address_type = 'smtp'"
    ):
        node = nodes.get(int(row["item_id"]))
        if node is not None and row["address"]:
            node.participants.add(row["address"].casefold())

    return nodes


def _within_window(a: str | None, b: str | None) -> bool:
    """Are these two close enough in time to be one conversation?

    A record with no date is not linked by this rule at all. Guessing that an
    undated message belongs to a thread because its subject matches would be
    putting it in a conversation it may have nothing to do with.
    """
    if not a or not b:
        return False
    try:
        da = datetime.strptime(a, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        db = datetime.strptime(b, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return abs(da - db) <= SUBJECT_WINDOW


def _best_subject(nodes: dict[int, _Node], members: list[int]) -> str:
    """The thread's subject: the earliest dated member's, which is the original."""
    dated = [
        nodes[m] for m in members
        if nodes[m].occurred_utc and nodes[m].subject_normalized
    ]
    if dated:
        return min(dated, key=lambda n: n.occurred_utc or "").subject_normalized
    for m in members:
        if nodes[m].subject_normalized:
            return nodes[m].subject_normalized
    return ""


def orphan_replies(conn) -> list[dict]:
    """Messages replying to something that is not in the archive.

    Spec 9.4: "A cluster of these is evidence of a missing source file, and
    should be reported that way." So they are returned grouped by the source
    they came from, not one by one.
    """
    rows = conn.execute(
        """
        SELECT i.id, i.subject, i.in_reply_to, i.occurred_utc,
               (SELECT sf.path FROM item_sources s
                  JOIN source_files sf ON sf.id = s.source_file_id
                 WHERE s.item_id = i.id LIMIT 1) AS source_path,
               (SELECT s.source_file_id FROM item_sources s
                 WHERE s.item_id = i.id LIMIT 1) AS source_id
        FROM items i
        WHERE i.kind = 'message'
          AND i.in_reply_to IS NOT NULL AND i.in_reply_to != ''
          AND NOT EXISTS (
            SELECT 1 FROM items o
             WHERE o.internet_message_id = i.in_reply_to
          )
        """
    ).fetchall()
    return [dict(r) for r in rows]
