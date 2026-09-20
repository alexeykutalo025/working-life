"""Suggesting that two people are one person, and never deciding it.

Spec section 7 draws the line precisely:

> Propose (never auto-apply) merges when: identical display name across
> different addresses; same surname + same domain; high Jaro-Winkler similarity
> on display name plus any shared correspondent.
>
> Every proposal appears in a merge review queue in the UI with the evidence
> shown, and requires a click to confirm. Merging is reversible - set
> `merged_into`, never delete a `people` row.

So this module computes proposals and the evidence for them. It contains no
function that merges anything on its own, and ``apply_merge`` exists only to be
called from a request the user made.

Jaro-Winkler is implemented here rather than pulled in as a dependency: it is
forty lines, and the spec pins the dependency list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..logging_setup import get_logger

log = get_logger("normalize.merge")


# ---------------------------------------------------------------------------
# Jaro-Winkler
# ---------------------------------------------------------------------------


def jaro(a: str, b: str) -> float:
    """Jaro similarity, 0.0 to 1.0."""
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0

    match_window = max(len(a), len(b)) // 2 - 1
    if match_window < 0:
        match_window = 0

    a_matched = [False] * len(a)
    b_matched = [False] * len(b)
    matches = 0

    for i, ch in enumerate(a):
        start = max(0, i - match_window)
        end = min(i + match_window + 1, len(b))
        for j in range(start, end):
            if b_matched[j] or b[j] != ch:
                continue
            a_matched[i] = True
            b_matched[j] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    transpositions = 0
    k = 0
    for i, matched in enumerate(a_matched):
        if not matched:
            continue
        while not b_matched[k]:
            k += 1
        if a[i] != b[k]:
            transpositions += 1
        k += 1

    transpositions //= 2
    return (
        matches / len(a) + matches / len(b) + (matches - transpositions) / matches
    ) / 3.0


def jaro_winkler(a: str, b: str, scaling: float = 0.1) -> float:
    """Jaro, with a bonus for a shared prefix.

    The prefix bonus is what makes it right for names: "Margaret O'Brien" and
    "Margaret OBrien" share nearly everything and should score high, while
    "Margaret O'Brien" and "Michael O'Brien" share a surname and should not.
    """
    base = jaro(a, b)
    if base < 0.7:
        return base

    prefix = 0
    for x, y in zip(a[:4], b[:4]):
        if x != y:
            break
        prefix += 1
    return base + prefix * scaling * (1 - base)


# ---------------------------------------------------------------------------
# Name handling
# ---------------------------------------------------------------------------

#: An apostrophe or hyphen *inside* a word is part of the name, not a
#: separator. O'Brien, D'Angelo, Ní Bhraonáin and Smith-Jones all lose their
#: surname if these are turned into spaces - and those are exactly the names a
#: forty-year Irish and European correspondence is full of.
_JOINER = re.compile(r"(?<=\w)['’ʼ\-](?=\w)", re.UNICODE)
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")

#: Titles and suffixes that are not part of a name for matching purposes.
_TITLES = {
    "mr", "mrs", "ms", "miss", "dr", "prof", "sir", "rev", "fr", "lord", "lady",
    "jr", "sr", "ii", "iii", "iv", "phd", "md", "esq", "mba",
}


def normalize_name(name: str | None) -> str:
    """A name reduced to what is comparable: lowercase, no punctuation, no titles."""
    if not name:
        return ""
    text = _JOINER.sub("", name)          # O'Brien -> OBrien, before anything else
    text = _PUNCT.sub(" ", text).casefold()
    words = [w for w in _SPACE.split(text) if w and w not in _TITLES]
    return " ".join(words)


def name_parts(name: str | None) -> tuple[str | None, str | None]:
    """(given, surname), handling both "Margaret O'Brien" and "O'Brien, Margaret"."""
    normalized = normalize_name(name)
    if not normalized:
        return None, None

    raw = (name or "").strip()
    if "," in raw:
        before, _, after = raw.partition(",")
        given_after = normalize_name(after)
        # "O'Brien, Margaret" is surname-first. "Margaret O'Brien, PhD" is not -
        # the comma is separating off a suffix, and normalising it leaves
        # nothing, which is how the two are told apart.
        if given_after:
            return given_after or None, normalize_name(before) or None
        normalized = normalize_name(before)

    words = normalized.split()
    if len(words) == 1:
        return None, words[0]
    return " ".join(words[:-1]), words[-1]


def looks_like_an_address(name: str | None) -> bool:
    """Is this "name" really just the email address repeated?"""
    return bool(name and "@" in name)


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------


@dataclass
class MergeProposal:
    """A suggestion that two people rows are one person.

    ``evidence`` is what the user is shown. A proposal with weak evidence is
    still shown - the point is that a human decides - but the evidence is never
    dressed up to look stronger than it is.
    """

    person_a: int
    person_b: int
    confidence: float
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[int, int]:
        return (min(self.person_a, self.person_b), max(self.person_a, self.person_b))

    def as_dict(self) -> dict[str, Any]:
        return {
            "person_a": self.person_a,
            "person_b": self.person_b,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "evidence": self.evidence,
        }


def suggest_merges(conn, settings, *, limit: int = 200) -> list[MergeProposal]:
    """Every merge worth proposing, best evidence first.

    Nothing here writes to the database. The caller shows these to the user and
    applies only what the user confirms.
    """
    people = _people_with_identities(conn)
    if len(people) < 2:
        return []

    threshold = settings.identity.merge_suggest_threshold
    role_locals = set(settings.identity.role_mailbox_locals)

    proposals: dict[tuple[int, int], MergeProposal] = {}

    def offer(proposal: MergeProposal) -> None:
        existing = proposals.get(proposal.key)
        if existing is None or proposal.confidence > existing.confidence:
            proposals[proposal.key] = proposal

    for proposal in _same_display_name(people, role_locals):
        offer(proposal)
    for proposal in _same_surname_and_domain(people, role_locals):
        offer(proposal)
    for proposal in _similar_name_with_shared_correspondent(
        conn, people, threshold, role_locals
    ):
        offer(proposal)

    out = sorted(proposals.values(), key=lambda p: p.confidence, reverse=True)
    return out[:limit]


def _people_with_identities(conn) -> list[dict]:
    """Every unmerged person, with their addresses and how much they appear."""
    rows = conn.execute(
        """
        SELECT p.id, p.display_name, p.org, p.is_self, p.item_count,
               p.first_seen_utc, p.last_seen_utc,
               (SELECT GROUP_CONCAT(i.address, ' ')
                  FROM identities i WHERE i.person_id = p.id) AS addresses,
               (SELECT GROUP_CONCAT(DISTINCT i.raw_display_name)
                  FROM identities i WHERE i.person_id = p.id
                   AND i.raw_display_name IS NOT NULL) AS names
        FROM people p
        WHERE p.merged_into IS NULL
        """
    ).fetchall()

    people: list[dict] = []
    for row in rows:
        addresses = [a for a in (row["addresses"] or "").split(" ") if a]
        smtp = [a for a in addresses if "@" in a]
        people.append({
            "id": int(row["id"]),
            "display_name": row["display_name"],
            "org": row["org"],
            "is_self": bool(row["is_self"]),
            "item_count": int(row["item_count"] or 0),
            "addresses": addresses,
            "smtp": smtp,
            "domains": {a.rsplit("@", 1)[1] for a in smtp},
            "names": [n for n in (row["names"] or "").split(",") if n.strip()],
            "normalized": normalize_name(row["display_name"]),
        })
    return people


def _is_role_mailbox(person: dict, role_locals: set[str]) -> bool:
    """A shared office mailbox is not a person and must never be merged as one."""
    for address in person["smtp"]:
        local = address.split("@", 1)[0]
        if local in role_locals:
            return True
    return False


def _same_display_name(people: list[dict], role_locals: set[str]):
    """Identical display name across different addresses.

    The strongest of the three signals, and still only a proposal: two John
    Murphys at different companies are two people.
    """
    by_name: dict[str, list[dict]] = {}
    for person in people:
        name = person["normalized"]
        if not name or looks_like_an_address(person["display_name"]):
            continue
        if len(name) < 4 or " " not in name:
            # A single short word is not enough. "bob" matching "bob" would
            # merge every Bob in forty years of correspondence.
            continue
        by_name.setdefault(name, []).append(person)

    for name, group in by_name.items():
        if len(group) < 2:
            continue
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                if _is_role_mailbox(a, role_locals) or _is_role_mailbox(b, role_locals):
                    continue
                same_org = bool(a["org"] and a["org"] == b["org"])
                shared_domain = bool(a["domains"] & b["domains"])
                confidence = 0.9 if (same_org or shared_domain) else 0.75
                yield MergeProposal(
                    person_a=a["id"], person_b=b["id"], confidence=confidence,
                    reason="They have exactly the same name",
                    evidence={
                        "rule": "identical_display_name",
                        "name": a["display_name"],
                        "a_addresses": a["addresses"],
                        "b_addresses": b["addresses"],
                        "shared_domain": sorted(a["domains"] & b["domains"]),
                        "same_organisation": same_org,
                        "caution": (
                            None if (same_org or shared_domain) else
                            "Nothing links these two beyond the name. Two people "
                            "can share a name."
                        ),
                    },
                )


def _same_surname_and_domain(people: list[dict], role_locals: set[str]):
    """Same surname at the same company - often one person, sometimes a family."""
    buckets: dict[tuple[str, str], list[dict]] = {}
    for person in people:
        _, surname = name_parts(person["display_name"])
        if not surname or len(surname) < 3:
            continue
        for domain in person["domains"]:
            buckets.setdefault((surname, domain), []).append(person)

    for (surname, domain), group in buckets.items():
        if len(group) < 2:
            continue
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                if _is_role_mailbox(a, role_locals) or _is_role_mailbox(b, role_locals):
                    continue
                given_a, _ = name_parts(a["display_name"])
                given_b, _ = name_parts(b["display_name"])

                # Two different first names at the same company is a family or
                # two colleagues, not one person with two addresses.
                if given_a and given_b and given_a != given_b:
                    if not _initial_match(given_a, given_b):
                        continue
                    confidence = 0.7
                    note = (
                        f"One is written as an initial: {given_a!r} and "
                        f"{given_b!r}. That is often the same person, and "
                        "sometimes a relative."
                    )
                else:
                    confidence = 0.8
                    note = None

                yield MergeProposal(
                    person_a=a["id"], person_b=b["id"], confidence=confidence,
                    reason=f"Same surname at the same organisation ({domain})",
                    evidence={
                        "rule": "surname_and_domain",
                        "surname": surname,
                        "domain": domain,
                        "a_name": a["display_name"],
                        "b_name": b["display_name"],
                        "a_addresses": a["addresses"],
                        "b_addresses": b["addresses"],
                        "caution": note or (
                            "People who share a surname at one company are often "
                            "relatives or colleagues, not one person."
                        ),
                    },
                )


def _initial_match(a: str, b: str) -> bool:
    """Is one given name an initial form of the other? "m" and "margaret"."""
    short, long = sorted((a, b), key=len)
    if not short or not long:
        return False
    return len(short) <= 2 and long.startswith(short[0])


def _similar_name_with_shared_correspondent(
    conn, people: list[dict], threshold: float, role_locals: set[str]
):
    """Similar names, *plus* somebody they both wrote to.

    The similarity alone is not evidence - the spec requires the shared
    correspondent as well, and that is what makes this worth showing.
    """
    correspondents = _correspondents(conn)

    candidates = [
        p for p in people
        if p["normalized"] and " " in p["normalized"]
        and not looks_like_an_address(p["display_name"])
        and not _is_role_mailbox(p, role_locals)
    ]

    for i, a in enumerate(candidates):
        for b in candidates[i + 1 :]:
            if a["normalized"] == b["normalized"]:
                continue   # already covered by the identical-name rule
            score = jaro_winkler(a["normalized"], b["normalized"])
            if score < threshold:
                continue

            shared = correspondents.get(a["id"], set()) & correspondents.get(b["id"], set())
            shared.discard(a["id"])
            shared.discard(b["id"])
            if not shared:
                continue

            names = _names_for(conn, sorted(shared)[:5])
            yield MergeProposal(
                person_a=a["id"], person_b=b["id"],
                confidence=min(0.85, score),
                reason=(
                    f"Their names are nearly the same, and {len(shared)} "
                    f"{'people' if len(shared) != 1 else 'person'} wrote to both"
                ),
                evidence={
                    "rule": "similar_name_shared_correspondent",
                    "a_name": a["display_name"],
                    "b_name": b["display_name"],
                    "similarity": round(score, 3),
                    "a_addresses": a["addresses"],
                    "b_addresses": b["addresses"],
                    "shared_correspondents": names,
                    "shared_count": len(shared),
                    "caution": (
                        "A similar name is weak evidence on its own. What makes "
                        "this worth looking at is the people they both "
                        "corresponded with."
                    ),
                },
            )


def _correspondents(conn) -> dict[int, set[int]]:
    """Who each person shared an item with."""
    out: dict[int, set[int]] = {}
    rows = conn.execute(
        "SELECT a.person_id AS a, b.person_id AS b "
        "FROM participations a JOIN participations b ON a.item_id = b.item_id "
        "WHERE a.person_id IS NOT NULL AND b.person_id IS NOT NULL "
        "AND a.person_id != b.person_id"
    ).fetchall()
    for row in rows:
        out.setdefault(int(row["a"]), set()).add(int(row["b"]))
    return out


def _names_for(conn, person_ids: list[int]) -> list[str]:
    if not person_ids:
        return []
    placeholders = ",".join("?" * len(person_ids))
    return [
        r["display_name"]
        for r in conn.execute(
            f"SELECT display_name FROM people WHERE id IN ({placeholders})", person_ids
        )
    ]


# ---------------------------------------------------------------------------
# Applying and undoing - only ever from a user's click
# ---------------------------------------------------------------------------


class MergeError(Exception):
    """A merge could not be applied. The message says why."""


def apply_merge(conn, keep_id: int, merge_id: int, *, note: str | None = None) -> dict:
    """Merge one person into another. Reversible, and never destructive.

    ``people.merged_into`` is set; the row stays. Participations are repointed
    so queries do not have to walk the merge chain, and the original person_id
    is recoverable because the identities keep their own person_id.
    """
    if keep_id == merge_id:
        raise MergeError("A person cannot be merged into themselves.")

    keep = conn.execute("SELECT * FROM people WHERE id = ?", (keep_id,)).fetchone()
    merge = conn.execute("SELECT * FROM people WHERE id = ?", (merge_id,)).fetchone()
    if keep is None:
        raise MergeError(f"There is no person with id {keep_id}.")
    if merge is None:
        raise MergeError(f"There is no person with id {merge_id}.")
    if merge["merged_into"] is not None:
        raise MergeError(
            f"{merge['display_name']} has already been merged into someone else. "
            "Undo that first if it was wrong."
        )
    if keep["merged_into"] is not None:
        raise MergeError(
            f"{keep['display_name']} has themselves been merged into someone "
            "else, so they cannot be the one kept."
        )

    conn.execute(
        "UPDATE people SET merged_into = ?, confirmed_by_user = 1, "
        "notes = COALESCE(notes || char(10), '') || ? WHERE id = ?",
        (keep_id, note or f"Merged into person {keep_id}", merge_id),
    )
    conn.execute(
        "UPDATE identities SET person_id = ? WHERE person_id = ?", (keep_id, merge_id)
    )
    conn.execute(
        "UPDATE participations SET person_id = ? WHERE person_id = ?", (keep_id, merge_id)
    )
    conn.execute(
        "UPDATE people SET confirmed_by_user = 1, "
        "first_seen_utc = MIN(COALESCE(first_seen_utc, ?), ?), "
        "last_seen_utc = MAX(COALESCE(last_seen_utc, ?), ?), "
        "is_self = MAX(is_self, ?) WHERE id = ?",
        (
            merge["first_seen_utc"], merge["first_seen_utc"] or keep["first_seen_utc"],
            merge["last_seen_utc"], merge["last_seen_utc"] or keep["last_seen_utc"],
            merge["is_self"] or 0, keep_id,
        ),
    )
    _recount(conn, keep_id)

    log.info("Merged person %d into %d", merge_id, keep_id)
    return {
        "kept": keep_id,
        "merged": merge_id,
        "kept_name": keep["display_name"],
        "merged_name": merge["display_name"],
    }


def undo_merge(conn, merged_id: int) -> dict:
    """Separate a person who was merged. Always possible, because nothing was deleted."""
    row = conn.execute("SELECT * FROM people WHERE id = ?", (merged_id,)).fetchone()
    if row is None:
        raise MergeError(f"There is no person with id {merged_id}.")
    if row["merged_into"] is None:
        raise MergeError(f"{row['display_name']} has not been merged into anyone.")

    was = int(row["merged_into"])
    conn.execute(
        "UPDATE people SET merged_into = NULL, confirmed_by_user = 0 WHERE id = ?",
        (merged_id,),
    )
    # The identities remember which person they were created for, so putting
    # them back needs no record of the original assignment beyond this.
    conn.execute(
        "UPDATE identities SET person_id = ? WHERE person_id = ? AND id IN ("
        "  SELECT id FROM identities WHERE person_id = ?"
        ")",
        (merged_id, was, was),
    )
    _recount(conn, was)
    _recount(conn, merged_id)
    log.info("Undid the merge of person %d into %d", merged_id, was)
    return {"separated": merged_id, "from": was}


def _recount(conn, person_id: int) -> None:
    conn.execute(
        "UPDATE people SET item_count = ("
        "  SELECT COUNT(DISTINCT item_id) FROM participations WHERE person_id = ?"
        ") WHERE id = ?",
        (person_id, person_id),
    )


def recount_all(conn) -> None:
    """Refresh every person's item count and first/last dates."""
    conn.execute(
        """
        UPDATE people SET
          item_count = (SELECT COUNT(DISTINCT p.item_id) FROM participations p
                         WHERE p.person_id = people.id),
          first_seen_utc = (SELECT MIN(i.occurred_utc) FROM participations p
                              JOIN items i ON i.id = p.item_id
                             WHERE p.person_id = people.id AND i.occurred_utc IS NOT NULL),
          last_seen_utc = (SELECT MAX(i.occurred_utc) FROM participations p
                             JOIN items i ON i.id = p.item_id
                            WHERE p.person_id = people.id AND i.occurred_utc IS NOT NULL)
        """
    )
