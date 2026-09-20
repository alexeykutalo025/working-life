"""Section 9.3 - duplicate and colliding accounts.

| code                     | detect                                  | action |
|--------------------------|-----------------------------------------|--------|
| under_merged             | two people rows that are probably one   | propose, never apply |
| over_merged_risk         | one identity that is probably several   | flag, never split |
| duplicate_account_store  | two files that are the same mailbox     | name the superset |
| self_identity_unclaimed  | a big sender not in the is_self list    | ask |
| ambiguous_legacydn       | an Exchange DN that never resolved      | keep the raw DN |
| identity_conflict        | two is_self addresses on one message    | flag |

The rule running through all six: **never auto-merge, never auto-split**. The
program's job is to notice and to show the evidence. The decision is the user's,
because only he knows whether the Bob Jenkins who wrote in 1997 is the Bob
Jenkins who wrote in 2011.
"""

from __future__ import annotations

from ..logging_setup import get_logger
from ..models import Severity
from .engine import Finding, record_finding, resolve_absent_findings

log = get_logger("integrity.accounts")


def account_checks(conn, settings) -> int:
    """Everything in 9.3. Returns how many findings were recorded."""
    n = 0
    n += _check_under_merged(conn, settings)
    n += _check_over_merged_risk(conn, settings)
    n += _check_duplicate_account_store(conn, settings)
    n += _check_self_identity_unclaimed(conn, settings)
    n += _check_ambiguous_legacydn(conn, settings)
    n += _check_identity_conflict(conn, settings)
    return n


# ---------------------------------------------------------------------------
# under_merged
# ---------------------------------------------------------------------------


def pair_key(a: int, b: int) -> str:
    """Two person ids as one stable key, whichever order they arrive in."""
    return f"{min(a, b)}:{max(a, b)}"


def _check_under_merged(conn, settings) -> int:
    """Two people who are probably one. Proposed, with the evidence attached."""
    from ..normalize.merge import suggest_merges

    proposals = suggest_merges(conn, settings)
    present: list[tuple] = []
    n = 0

    for proposal in proposals:
        names = conn.execute(
            "SELECT id, display_name FROM people WHERE id IN (?, ?)",
            (proposal.person_a, proposal.person_b),
        ).fetchall()
        if len(names) != 2:
            continue
        by_id = {int(r["id"]): r["display_name"] for r in names}
        a_name = by_id.get(proposal.person_a) or "(unnamed)"
        b_name = by_id.get(proposal.person_b) or "(unnamed)"

        # A finding is identified by (code, file, item, person, period), so
        # hanging one off a single person id gave *one row per person* rather
        # than one per pair: every later suggestion involving the same person
        # overwrote the previous one, and 200 proposals collapsed into six
        # findings. Worse, turning one down then silently turned down every
        # other pair that person appeared in.
        #
        # The pair itself is the thing being decided, so the pair is the key.
        # person_id stays the lower of the two, because the finding still has
        # to point somewhere for "jump to the person it is about".
        anchor = min(proposal.person_a, proposal.person_b)
        pair = pair_key(proposal.person_a, proposal.person_b)
        present.append(("under_merged", -1, -1, anchor, pair))

        caution = proposal.evidence.get("caution")
        record_finding(
            conn,
            Finding(
                code="under_merged",
                severity=Severity.INFO,
                title=f"{a_name} and {b_name} may be the same person",
                detail=(
                    f"{proposal.reason}.\n\n"
                    f"{a_name}: {', '.join(proposal.evidence.get('a_addresses') or []) or 'no address'}\n"
                    f"{b_name}: {', '.join(proposal.evidence.get('b_addresses') or []) or 'no address'}\n\n"
                    + (f"{caution}\n\n" if caution else "")
                    + "Recall has NOT merged them. Go to the People screen to "
                    "look at the evidence and decide. Merging can be undone at "
                    "any time - nothing is ever deleted."
                ),
                person_id=anchor,
                period_start=pair,
                affected_count=2,
                evidence=proposal.as_dict(),
            ),
        )
        n += 1

    resolve_absent_findings(conn, "under_merged", present)
    return n


# ---------------------------------------------------------------------------
# over_merged_risk
# ---------------------------------------------------------------------------


def _check_over_merged_risk(conn, settings) -> int:
    """One address that is probably several humans.

    Three signals, from the spec: a role mailbox local part, a generic display
    name, or one address carrying more than the configured number of display
    names. Never split automatically, and never allowed to appear as a top
    correspondent without the warning attached.
    """
    role_locals = set(settings.identity.role_mailbox_locals)
    max_names = settings.identity.max_display_names_per_address
    generic = {
        "administrator", "admin", "support", "helpdesk", "help desk", "reception",
        "office", "accounts", "sales", "info", "enquiries", "webmaster",
        "postmaster", "mailer-daemon", "system", "noreply", "no-reply",
        "unknown", "undisclosed recipients", "recipients",
    }

    present: list[tuple] = []
    n = 0

    rows = conn.execute(
        """
        SELECT i.id AS identity_id, i.address, i.address_type, i.person_id,
               p.display_name, p.item_count,
               (SELECT COUNT(DISTINCT pt.item_id) FROM participations pt
                 WHERE pt.identity_id = i.id) AS uses
        FROM identities i
        LEFT JOIN people p ON p.id = i.person_id
        WHERE i.address_type = 'smtp'
        """
    ).fetchall()

    for row in rows:
        address = row["address"] or ""
        local = address.split("@", 1)[0] if "@" in address else address
        person_id = row["person_id"]
        if person_id is None:
            continue

        names = [
            r["name"]
            for r in conn.execute(
                "SELECT DISTINCT raw_display_name AS name FROM identities "
                "WHERE address = ? AND raw_display_name IS NOT NULL "
                "UNION "
                "SELECT DISTINCT pe.display_name FROM participations pt "
                "JOIN identities idn ON idn.id = pt.identity_id "
                "JOIN people pe ON pe.id = pt.person_id "
                "WHERE idn.address = ? AND pe.display_name IS NOT NULL",
                (address, address),
            )
            if r["name"]
        ]
        distinct_names = {n_.strip().casefold() for n_ in names if n_.strip()}

        reasons: list[str] = []
        if local in role_locals:
            reasons.append(
                f"{address} is a shared office address, not one person's"
            )
        if (row["display_name"] or "").strip().casefold() in generic:
            reasons.append(
                f"the name on it, {row['display_name']!r}, is a job rather than a person"
            )
        if len(distinct_names) > max_names:
            reasons.append(
                f"{len(distinct_names)} different names have been used with this "
                "one address"
            )

        if not reasons:
            continue

        present.append(("over_merged_risk", -1, -1, int(person_id), ""))
        record_finding(
            conn,
            Finding(
                code="over_merged_risk",
                severity=Severity.HIGH,
                title=f"{address} is probably more than one person",
                detail=(
                    "Recall has put everything from this address under one "
                    "person, because that is all it can safely do. But "
                    + _join_reasons(reasons)
                    + ".\n\n"
                    "What that means: every count, chart and 'top correspondent' "
                    "involving this address is really about a group of people, "
                    "not an individual.\n\n"
                    "Recall will NOT split it up. There is no way to tell from "
                    "the mail which message was written by which person, and "
                    "guessing would put words in somebody's mouth.\n\n"
                    + (
                        "Names seen on this address: "
                        + ", ".join(sorted(distinct_names)[:15])
                        + ("..." if len(distinct_names) > 15 else "")
                        if distinct_names else ""
                    )
                ),
                person_id=int(person_id),
                affected_count=int(row["uses"] or 0),
                evidence={
                    "address": address,
                    "distinct_display_names": sorted(distinct_names),
                    "name_count": len(distinct_names),
                    "reasons": reasons,
                    "is_role_mailbox": local in role_locals,
                    "items": int(row["uses"] or 0),
                },
            ),
        )
        n += 1

    resolve_absent_findings(conn, "over_merged_risk", present)
    return n


def _join_reasons(reasons: list[str]) -> str:
    if len(reasons) == 1:
        return reasons[0]
    return ", and ".join([", ".join(reasons[:-1]), reasons[-1]])


# ---------------------------------------------------------------------------
# duplicate_account_store
# ---------------------------------------------------------------------------


def _check_duplicate_account_store(conn, settings) -> int:
    """Two files that are the same mailbox, with which is the superset.

    Overlap is measured on dedup_key through item_sources, so it is a real
    count of shared records rather than a guess from file size.
    """
    threshold = settings.integrity.duplicate_store_overlap
    present: list[tuple] = []
    n = 0

    # A single saved .eml that also appears in a mailbox is a duplicate record,
    # not "the same mailbox saved twice", and calling it one would put noise at
    # the top of the Problems screen. A store has to be substantial before this
    # claim means anything.
    min_items = 10

    sources = conn.execute(
        "SELECT sf.id, sf.path, sf.item_count FROM source_files sf "
        "WHERE sf.parse_state = 'done' AND sf.item_count >= ? "
        "ORDER BY sf.item_count DESC",
        (min_items,),
    ).fetchall()

    if len(sources) < 2:
        resolve_absent_findings(conn, "duplicate_account_store", present)
        return 0

    for i, a in enumerate(sources):
        for b in sources[i + 1 :]:
            a_count, b_count = int(a["item_count"]), int(b["item_count"])
            smaller = min(a_count, b_count)
            if smaller == 0:
                continue

            shared = conn.execute(
                "SELECT COUNT(*) AS n FROM ("
                "  SELECT item_id FROM item_sources WHERE source_file_id = ?"
                "  INTERSECT "
                "  SELECT item_id FROM item_sources WHERE source_file_id = ?"
                ")",
                (int(a["id"]), int(b["id"])),
            ).fetchone()["n"]

            overlap = shared / smaller if smaller else 0.0
            if overlap < threshold:
                continue

            # a is the larger by the ORDER BY, so it is the superset.
            superset, subset = (a, b) if a_count >= b_count else (b, a)
            superset_count = max(a_count, b_count)
            unique_to_subset = smaller - shared

            anchor = int(subset["id"])
            present.append(("duplicate_account_store", anchor, -1, -1, ""))

            from pathlib import Path

            record_finding(
                conn,
                Finding(
                    code="duplicate_account_store",
                    severity=Severity.MEDIUM,
                    title=(
                        f"{Path(subset['path']).name} is almost entirely inside "
                        f"{Path(superset['path']).name}"
                    ),
                    detail=(
                        f"These two files are the same mailbox saved twice.\n\n"
                        f"{Path(superset['path']).name} holds {superset_count:,} records.\n"
                        f"{Path(subset['path']).name} holds {smaller:,} records, "
                        f"{shared:,} of which ({overlap:.0%}) are also in the "
                        "larger file.\n\n"
                        + (
                            f"{unique_to_subset:,} record(s) exist only in the "
                            "smaller file, so it is NOT safe to think of it as "
                            "redundant.\n\n"
                            if unique_to_subset else
                            "Every record in the smaller file is also in the "
                            "larger one.\n\n"
                        )
                        + "Nothing has been deleted, and nothing will be. Both "
                        "files stay in the list, and the archive already holds "
                        "each record once with a note of every file it came from."
                        f"\n\nLarger:  {superset['path']}"
                        f"\nSmaller: {subset['path']}"
                    ),
                    source_file_id=anchor,
                    affected_count=shared,
                    evidence={
                        "superset_id": int(superset["id"]),
                        "superset_path": superset["path"],
                        "superset_count": superset_count,
                        "subset_id": int(subset["id"]),
                        "subset_path": subset["path"],
                        "subset_count": smaller,
                        "shared": shared,
                        "overlap": round(overlap, 4),
                        "unique_to_subset": unique_to_subset,
                    },
                ),
            )
            n += 1

    resolve_absent_findings(conn, "duplicate_account_store", present)
    return n


# ---------------------------------------------------------------------------
# self_identity_unclaimed
# ---------------------------------------------------------------------------


def _check_self_identity_unclaimed(conn, settings) -> int:
    """An address sending a lot of mail that is not in the is_self list."""
    share = settings.integrity.self_identity_share
    known = {a.casefold() for a in settings.identity.me}

    total = conn.execute(
        "SELECT COUNT(*) AS n FROM participations WHERE role = 'from'"
    ).fetchone()["n"]
    if not total:
        resolve_absent_findings(conn, "self_identity_unclaimed", [])
        return 0

    present: list[tuple] = []
    n = 0

    rows = conn.execute(
        """
        SELECT i.address, i.person_id, COUNT(*) AS sent, p.display_name, p.is_self
        FROM participations pt
        JOIN identities i ON i.id = pt.identity_id
        LEFT JOIN people p ON p.id = pt.person_id
        WHERE pt.role = 'from' AND i.address_type = 'smtp'
        GROUP BY i.address, i.person_id
        HAVING sent > 0
        ORDER BY sent DESC
        """
    ).fetchall()

    for row in rows:
        address = (row["address"] or "").casefold()
        if not address or address in known or row["is_self"]:
            continue
        sent = int(row["sent"])
        if sent / total < share:
            continue
        if row["person_id"] is None:
            continue

        person_id = int(row["person_id"])
        present.append(("self_identity_unclaimed", -1, -1, person_id, ""))
        record_finding(
            conn,
            Finding(
                code="self_identity_unclaimed",
                severity=Severity.MEDIUM,
                title=f"Is {row['address']} one of your addresses?",
                detail=(
                    f"{sent:,} messages in the archive were sent from "
                    f"{row['address']} - that is {sent / total:.0%} of everything "
                    "sent by anybody.\n\n"
                    "An address that sends that much is usually your own. It is "
                    "not in the list of your addresses in config.toml, so Recall "
                    "is treating it as somebody else's - which means you appear "
                    "in your own list of correspondents, and 'who did I write to "
                    "most' is wrong.\n\n"
                    "If it is yours, add it to config.toml under [identity], in "
                    "the `me` list:\n"
                    f'    me = ["{row["address"]}"]\n\n'
                    "If it is not yours, mark this as won't-fix and it will stop "
                    "asking."
                ),
                person_id=person_id,
                affected_count=sent,
                evidence={
                    "address": row["address"],
                    "display_name": row["display_name"],
                    "sent": sent,
                    "total_sent": int(total),
                    "share": round(sent / total, 4),
                    "configured_self_addresses": sorted(known),
                },
            ),
        )
        n += 1

    resolve_absent_findings(conn, "self_identity_unclaimed", present)
    return n


# ---------------------------------------------------------------------------
# ambiguous_legacydn
# ---------------------------------------------------------------------------


def _check_ambiguous_legacydn(conn, settings) -> int:
    """An Exchange legacyDN that never resolved to an email address.

    The raw DN stays visible. Recall does not fabricate an address from it,
    which is exactly what the spec forbids.
    """
    present: list[tuple] = []
    n = 0

    rows = conn.execute(
        """
        SELECT i.id, i.address, i.person_id, i.raw_display_name,
               (SELECT COUNT(*) FROM participations pt WHERE pt.identity_id = i.id) AS uses
        FROM identities i
        WHERE i.address_type = 'ex'
        """
    ).fetchall()

    for row in rows:
        person_id = row["person_id"]
        if person_id is None:
            continue

        # Did this person ever turn up with a real address too?
        has_smtp = conn.execute(
            "SELECT 1 FROM identities WHERE person_id = ? AND address_type = 'smtp' LIMIT 1",
            (person_id,),
        ).fetchone()
        if has_smtp:
            continue

        uses = int(row["uses"] or 0)
        if uses == 0:
            continue

        present.append(("ambiguous_legacydn", -1, -1, int(person_id), ""))
        record_finding(
            conn,
            Finding(
                code="ambiguous_legacydn",
                severity=Severity.MEDIUM,
                title=(
                    "An internal Exchange address never resolved to an email address"
                    + (f": {row['raw_display_name']}" if row["raw_display_name"] else "")
                ),
                detail=(
                    "Inside a company mail server, people are identified by an "
                    "internal name rather than an email address. When the mail "
                    "was archived, that internal name was kept but the email "
                    "address it stood for was not.\n\n"
                    f"The internal name, exactly as stored:\n    {row['address']}\n\n"
                    f"It appears on {uses:,} record(s).\n\n"
                    "Recall has NOT invented an email address for it. Searching "
                    "for this person by their email address will not find these "
                    "records; searching by the name shown will.\n\n"
                    "If you know whose address this is, open their entry on the "
                    "People screen and merge this one into it."
                ),
                person_id=int(person_id),
                affected_count=uses,
                evidence={
                    "legacy_dn": row["address"],
                    "display_name": row["raw_display_name"],
                    "items": uses,
                },
            ),
        )
        n += 1

    resolve_absent_findings(conn, "ambiguous_legacydn", present)
    return n


# ---------------------------------------------------------------------------
# identity_conflict
# ---------------------------------------------------------------------------


def _check_identity_conflict(conn, settings) -> int:
    """Two of the user's own addresses as sender and recipient of one message.

    Usually means a store belongs to a different account than assumed - which
    matters, because it changes whose archive this is.
    """
    known = [a.casefold() for a in settings.identity.me]
    if len(known) < 2:
        resolve_absent_findings(conn, "identity_conflict", [])
        return 0

    placeholders = ",".join("?" * len(known))
    rows = conn.execute(
        f"""
        SELECT i.id AS item_id, i.subject, i.occurred_utc,
               sender.address AS from_address, recipient.address AS to_address,
               (SELECT sf.path FROM item_sources isrc
                  JOIN source_files sf ON sf.id = isrc.source_file_id
                 WHERE isrc.item_id = i.id LIMIT 1) AS source_path
        FROM items i
        JOIN participations pf ON pf.item_id = i.id AND pf.role = 'from'
        JOIN identities sender ON sender.id = pf.identity_id
        JOIN participations pt ON pt.item_id = i.id AND pt.role IN ('to', 'cc')
        JOIN identities recipient ON recipient.id = pt.identity_id
        WHERE LOWER(sender.address) IN ({placeholders})
          AND LOWER(recipient.address) IN ({placeholders})
          AND LOWER(sender.address) != LOWER(recipient.address)
        LIMIT 200
        """,
        (*known, *known),
    ).fetchall()

    if not rows:
        resolve_absent_findings(conn, "identity_conflict", [])
        return 0

    # Grouped by source file: this is a statement about a store, not a message.
    by_source: dict[str | None, list] = {}
    for row in rows:
        by_source.setdefault(row["source_path"], []).append(row)

    present: list[tuple] = []
    n = 0

    for path, group in by_source.items():
        source_row = conn.execute(
            "SELECT id FROM source_files WHERE path = ?", (path,)
        ).fetchone() if path else None
        source_id = int(source_row["id"]) if source_row else None

        pairs = sorted({(r["from_address"], r["to_address"]) for r in group})
        present.append(
            ("identity_conflict", source_id if source_id is not None else -1, -1, -1, "")
        )

        from pathlib import Path

        record_finding(
            conn,
            Finding(
                code="identity_conflict",
                severity=Severity.HIGH,
                title=(
                    f"{Path(path).name if path else 'A file'} contains mail you "
                    "sent to yourself at another of your addresses"
                ),
                detail=(
                    f"{len(group):,} message(s) have one of your own addresses as "
                    "the sender and a different one of your addresses as a "
                    "recipient.\n\n"
                    "That can be perfectly ordinary - people do send themselves "
                    "notes. But it is also what you see when a mailbox belongs to "
                    "a different account than you assumed, which would change "
                    "whose archive this is.\n\n"
                    "Pairs seen:\n"
                    + "\n".join(f"    {a} to {b}" for a, b in pairs[:10])
                    + ("\n    ..." if len(pairs) > 10 else "")
                    + "\n\nIf this is expected, mark it as explained and it will "
                    "stop appearing."
                ),
                source_file_id=source_id,
                affected_count=len(group),
                evidence={
                    "source_path": path,
                    "pairs": [list(p) for p in pairs],
                    "message_count": len(group),
                    "configured_self_addresses": known,
                },
            ),
        )
        n += 1

    resolve_absent_findings(conn, "identity_conflict", present)
    return n
