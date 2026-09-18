"""People, their profiles, and the merge review queue.

The queue is the point. Recall suggests; the user decides; and every suggestion
arrives with the evidence that produced it, including the reasons to doubt it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..logging_setup import get_logger

log = get_logger("api.people")

router = APIRouter(tags=["people"])


def _conn(request: Request):
    return request.app.state.db()


def _settings(request: Request):
    return request.app.state.settings


class MergeRequest(BaseModel):
    keep_id: int
    merge_id: int
    note: str | None = None


class PersonEdit(BaseModel):
    display_name: str | None = None
    org: str | None = None
    role: str | None = None
    notes: str | None = None
    is_self: bool | None = None


@router.get("/people")
def list_people(
    request: Request,
    sort: str = "items",
    order: str = "desc",
    search: str | None = None,
    include_self: bool = True,
    limit: int = 500,
    offset: int = 0,
) -> dict[str, Any]:
    """Everyone in the archive, with a warning on anyone who is really a group."""
    conn = _conn(request)

    columns = {
        "items": "p.item_count",
        "name": "p.display_name",
        "first": "p.first_seen_utc",
        "last": "p.last_seen_utc",
        "org": "p.org",
    }
    sort_sql = columns.get(sort, "p.item_count")
    order_sql = "DESC" if order.lower() == "desc" else "ASC"

    where = ["p.merged_into IS NULL"]
    params: list[Any] = []
    if not include_self:
        where.append("p.is_self = 0")
    if search:
        where.append(
            "(p.display_name LIKE ? OR EXISTS ("
            "  SELECT 1 FROM identities i WHERE i.person_id = p.id AND i.address LIKE ?"
            "))"
        )
        params.extend([f"%{search}%", f"%{search.lower()}%"])

    where_sql = "WHERE " + " AND ".join(where)

    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM people p {where_sql}", params
    ).fetchone()["n"]

    rows = conn.execute(
        f"""
        SELECT p.*,
               (SELECT GROUP_CONCAT(i.address, ' | ') FROM identities i
                 WHERE i.person_id = p.id) AS addresses,
               (SELECT COUNT(*) FROM identities i WHERE i.person_id = p.id) AS address_count,
               (SELECT COUNT(*) FROM people m WHERE m.merged_into = p.id) AS merged_count,
               (SELECT f.id FROM findings f
                 WHERE f.person_id = p.id AND f.code = 'over_merged_risk'
                   AND f.state IN ('open','acknowledged') LIMIT 1) AS over_merged_finding
        FROM people p
        {where_sql}
        ORDER BY {sort_sql} {order_sql}, p.id
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()

    return {
        "total": total,
        "rows": [_person_row(r) for r in rows],
        "offset": offset,
        "limit": limit,
    }


def _person_row(row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "display_name": row["display_name"],
        "org": row["org"],
        "role": row["role"],
        "is_self": bool(row["is_self"]),
        "item_count": int(row["item_count"] or 0),
        "first_seen_utc": row["first_seen_utc"],
        "last_seen_utc": row["last_seen_utc"],
        "addresses": [a for a in (row["addresses"] or "").split(" | ") if a],
        "address_count": int(row["address_count"] or 0),
        "merged_count": int(row["merged_count"] or 0),
        "confirmed_by_user": bool(row["confirmed_by_user"]),
        # Spec 9.3: this must never appear as a top correspondent without the
        # warning attached, so the warning travels with the row.
        "over_merged_risk": row["over_merged_finding"] is not None,
        "over_merged_finding_id": row["over_merged_finding"],
    }


@router.get("/people/merge-queue")
def merge_queue(request: Request) -> dict[str, Any]:
    """Proposed merges, with their evidence. Nothing here has been applied."""
    from ..normalize.merge import suggest_merges

    conn = _conn(request)
    settings = _settings(request)
    proposals = suggest_merges(conn, settings)

    out = []
    for proposal in proposals:
        people = {
            int(r["id"]): _person_row(r)
            for r in conn.execute(
                """
                SELECT p.*,
                       (SELECT GROUP_CONCAT(i.address, ' | ') FROM identities i
                         WHERE i.person_id = p.id) AS addresses,
                       (SELECT COUNT(*) FROM identities i WHERE i.person_id = p.id)
                           AS address_count,
                       (SELECT COUNT(*) FROM people m WHERE m.merged_into = p.id)
                           AS merged_count,
                       NULL AS over_merged_finding
                FROM people p WHERE p.id IN (?, ?)
                """,
                (proposal.person_a, proposal.person_b),
            )
        }
        if len(people) != 2:
            continue
        a = people[proposal.person_a]
        b = people[proposal.person_b]
        # The one with more records is offered as the one to keep, because
        # merging the smaller into the larger loses less if it is later undone.
        keep, merge = (a, b) if a["item_count"] >= b["item_count"] else (b, a)
        out.append({
            **proposal.as_dict(),
            "a": a,
            "b": b,
            "suggested_keep": keep["id"],
            "suggested_merge": merge["id"],
        })

    return {
        "proposals": out,
        "count": len(out),
        "note": (
            "Recall has not merged anyone. Each suggestion below is a guess with "
            "its evidence shown. Confirming one can be undone at any time."
        ),
    }


@router.get("/people/{person_id}")
def get_person(request: Request, person_id: int) -> dict[str, Any]:
    """One person's profile: identities, timeline, co-correspondents."""
    conn = _conn(request)

    row = conn.execute(
        """
        SELECT p.*,
               (SELECT GROUP_CONCAT(i.address, ' | ') FROM identities i
                 WHERE i.person_id = p.id) AS addresses,
               (SELECT COUNT(*) FROM identities i WHERE i.person_id = p.id) AS address_count,
               (SELECT COUNT(*) FROM people m WHERE m.merged_into = p.id) AS merged_count,
               (SELECT f.id FROM findings f
                 WHERE f.person_id = p.id AND f.code = 'over_merged_risk'
                   AND f.state IN ('open','acknowledged') LIMIT 1) AS over_merged_finding
        FROM people p WHERE p.id = ?
        """,
        (person_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No person with id {person_id}.")

    person = _person_row(row)
    person["merged_into"] = row["merged_into"]
    person["notes"] = row["notes"]

    person["identities"] = [
        dict(r)
        for r in conn.execute(
            "SELECT id, address, address_type, raw_display_name, first_seen_utc, "
            "last_seen_utc, use_count FROM identities WHERE person_id = ? "
            "ORDER BY use_count DESC",
            (person_id,),
        )
    ]

    person["merged_people"] = [
        {"id": int(r["id"]), "display_name": r["display_name"]}
        for r in conn.execute(
            "SELECT id, display_name FROM people WHERE merged_into = ?", (person_id,)
        )
    ]

    # Records per year, for the sparkline. Years with nothing are present with a
    # zero rather than absent, so the sparkline cannot close over a gap.
    counts = {
        r["y"]: int(r["n"])
        for r in conn.execute(
            "SELECT substr(i.occurred_utc, 1, 4) AS y, COUNT(DISTINCT i.id) AS n "
            "FROM participations p JOIN items i ON i.id = p.item_id "
            "WHERE p.person_id = ? AND i.occurred_utc IS NOT NULL GROUP BY y",
            (person_id,),
        )
    }
    if counts:
        years = sorted(counts)
        person["per_year"] = [
            {"year": y, "count": counts.get(f"{y:04d}", 0)}
            for y in range(int(years[0]), int(years[-1]) + 1)
        ]
    else:
        person["per_year"] = []

    person["undated_count"] = conn.execute(
        "SELECT COUNT(DISTINCT i.id) AS n FROM participations p "
        "JOIN items i ON i.id = p.item_id "
        "WHERE p.person_id = ? AND i.occurred_utc IS NULL",
        (person_id,),
    ).fetchone()["n"]

    person["top_correspondents"] = [
        {
            "id": int(r["id"]),
            "display_name": r["display_name"],
            "shared": int(r["shared"]),
            "over_merged_risk": r["risk"] is not None,
        }
        for r in conn.execute(
            """
            SELECT other.id, other.display_name, COUNT(DISTINCT a.item_id) AS shared,
                   (SELECT f.id FROM findings f
                     WHERE f.person_id = other.id AND f.code = 'over_merged_risk'
                       AND f.state IN ('open','acknowledged') LIMIT 1) AS risk
            FROM participations a
            JOIN participations b ON b.item_id = a.item_id AND b.person_id != a.person_id
            JOIN people other ON other.id = b.person_id
            WHERE a.person_id = ? AND other.merged_into IS NULL
            GROUP BY other.id, other.display_name
            ORDER BY shared DESC
            LIMIT 15
            """,
            (person_id,),
        )
    ]

    person["recent_items"] = [
        dict(r)
        for r in conn.execute(
            "SELECT i.id, i.kind, i.subject, i.occurred_utc, p.role "
            "FROM participations p JOIN items i ON i.id = p.item_id "
            "WHERE p.person_id = ? "
            "ORDER BY i.occurred_utc IS NULL, i.occurred_utc DESC LIMIT 50",
            (person_id,),
        )
    ]

    person["findings"] = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM findings WHERE person_id = ? "
            "AND state IN ('open','acknowledged') "
            "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
            "WHEN 'medium' THEN 2 ELSE 3 END",
            (person_id,),
        )
    ]

    return person


@router.post("/people/merge")
def merge(request: Request, body: MergeRequest) -> dict[str, Any]:
    """Apply a merge the user confirmed. Always undoable."""
    from ..normalize.merge import MergeError, apply_merge

    conn = _conn(request)
    try:
        result = apply_merge(conn, body.keep_id, body.merge_id, note=body.note)
    except MergeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _refresh_account_findings(conn, request)
    return {**result, "undoable": True}


@router.post("/people/{person_id}/unmerge")
def unmerge(request: Request, person_id: int) -> dict[str, Any]:
    """Separate someone who was merged. Possible because nothing was deleted."""
    from ..normalize.merge import MergeError, undo_merge

    conn = _conn(request)
    try:
        result = undo_merge(conn, person_id)
    except MergeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _refresh_account_findings(conn, request)
    return result


@router.post("/people/{person_id}")
def edit_person(request: Request, person_id: int, body: PersonEdit) -> dict[str, Any]:
    """Correct a person's details. The user's word beats anything inferred."""
    conn = _conn(request)
    row = conn.execute("SELECT id FROM people WHERE id = ?", (person_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No person with id {person_id}.")

    fields = []
    params: list[Any] = []
    for column, value in (
        ("display_name", body.display_name),
        ("org", body.org),
        ("role", body.role),
        ("notes", body.notes),
    ):
        if value is not None:
            fields.append(f"{column} = ?")
            params.append(value)
    if body.is_self is not None:
        fields.append("is_self = ?")
        params.append(1 if body.is_self else 0)

    if not fields:
        return {"saved": False, "reason": "nothing to change"}

    fields.append("confirmed_by_user = 1")
    conn.execute(
        f"UPDATE people SET {', '.join(fields)} WHERE id = ?", (*params, person_id)
    )
    return {"saved": True}


def _refresh_account_findings(conn, request) -> None:
    """Re-run the people checks so the queue reflects what just happened."""
    try:
        from ..integrity.accounts import account_checks

        account_checks(conn, _settings(request))
    except Exception as exc:  # noqa: BLE001 - a stale queue is not worth a 500
        log.warning("The people checks could not be re-run after a merge: %s", exc)
