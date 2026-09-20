"""Home: the archive at a glance, and the next sensible thing to do.

Every number here goes through the honest-count envelope, so a total that a
finding affects arrives with its reason attached and the screen cannot render
one without the other.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from ..logging_setup import get_logger

log = get_logger("api.home")

router = APIRouter(tags=["home"])

_KIND_WORDS = {
    "message": "messages",
    "event": "calendar entries",
    "contact": "contacts",
    "task": "tasks",
    "note": "notes",
}


def _conn(request: Request):
    return request.app.state.db()


def _settings(request: Request):
    return request.app.state.settings


@router.get("/home")
def home(request: Request) -> dict[str, Any]:
    from ..integrity.honest import count_items, undated_count
    from ..search.indexer import index_health

    conn = _conn(request)
    settings = _settings(request)

    by_kind = []
    for row in conn.execute(
        "SELECT kind, COUNT(*) AS n FROM items GROUP BY kind ORDER BY n DESC"
    ):
        count = count_items(conn, kind=row["kind"], label=_KIND_WORDS.get(row["kind"], row["kind"]))
        by_kind.append({"kind": row["kind"], **count.as_dict()})

    total = count_items(conn, label="records")

    # The span comes from the census rather than from MIN/MAX over the items,
    # so it uses the same plausibility rule. Taking it raw would let one
    # message dated 1961 turn "seventeen years of correspondence" into
    # "fifty-three" on the headline of the first screen the user sees.
    span = conn.execute(
        "SELECT MIN(month) AS first_month, MAX(month) AS last_month "
        "FROM coverage_months WHERE item_count > 0"
    ).fetchone()

    if span and span["first_month"]:
        first_utc = conn.execute(
            "SELECT MIN(occurred_utc) AS d FROM items WHERE occurred_utc >= ?",
            (span["first_month"] + "-01",),
        ).fetchone()["d"]
        last_utc = conn.execute(
            "SELECT MAX(occurred_utc) AS d FROM items WHERE occurred_utc < ?",
            (_month_after(span["last_month"]),),
        ).fetchone()["d"]
    else:
        first_utc = last_utc = None

    # Dates outside the plausible range are still counted and still findable;
    # they are simply not allowed to define the span.
    implausible = conn.execute(
        "SELECT COUNT(*) AS n FROM findings WHERE code = 'implausible_date' "
        "AND state IN ('open','acknowledged')"
    ).fetchone()["n"]

    sources = conn.execute(
        """
        SELECT COUNT(*) AS found,
               SUM(CASE WHEN parse_state = 'done' THEN 1 ELSE 0 END) AS read_ok,
               SUM(CASE WHEN parse_state = 'pending' THEN 1 ELSE 0 END) AS pending,
               SUM(CASE WHEN parse_state = 'failed' THEN 1 ELSE 0 END) AS failed,
               SUM(CASE WHEN is_placeholder = 1 AND local_copy_path IS NULL
                        THEN 1 ELSE 0 END) AS cloud_only,
               SUM(CASE WHEN local_copy_path IS NOT NULL THEN 1 ELSE 0 END)
                   AS cloud_copied,
               SUM(CASE WHEN duplicate_of IS NOT NULL THEN 1 ELSE 0 END) AS duplicates,
               COALESCE(SUM(size_bytes), 0) AS total_bytes
        FROM source_files
        """
    ).fetchone()

    people = conn.execute(
        "SELECT COUNT(*) AS n FROM people WHERE merged_into IS NULL"
    ).fetchone()["n"]

    attachments = conn.execute(
        "SELECT COUNT(*) AS n, COUNT(DISTINCT content_hash) AS distinct_files, "
        "COALESCE(SUM(size_bytes), 0) AS total_bytes FROM attachments"
    ).fetchone()

    coverage = conn.execute(
        "SELECT COUNT(*) AS months, "
        "SUM(CASE WHEN gap_class IS NOT NULL THEN 1 ELSE 0 END) AS gap_months, "
        "SUM(CASE WHEN explained_by_user = 1 THEN 1 ELSE 0 END) AS explained "
        "FROM (SELECT month, MAX(gap_class) AS gap_class, "
        "      MAX(explained_by_user) AS explained_by_user "
        "      FROM coverage_months GROUP BY month)"
    ).fetchone()

    return {
        "total": total.as_dict(),
        "by_kind": by_kind,
        "undated": undated_count(conn),
        "span": {
            "first": first_utc,
            "last": last_utc,
            "years": (
                int(last_utc[:4]) - int(first_utc[:4]) + 1
                if first_utc and last_utc else 0
            ),
            "implausible_dates": int(implausible or 0),
        },
        "sources": dict(sources),
        "people": int(people or 0),
        "attachments": dict(attachments),
        "coverage": dict(coverage) if coverage else {},
        "storage": {
            "workdir": str(settings.workdir_path),
            "archive_bytes": _file_size(settings.db_path),
            "blobs_bytes": _blob_size(settings),
        },
        "index": index_health(conn),
        "next_steps": _next_steps(conn, sources, total.value),
    }


def _next_steps(conn, sources, item_count: int) -> list[dict[str, str]]:
    """The three big buttons, chosen by what the archive actually needs.

    Ordered by what would help most: finding files, then reading them, then
    dealing with what could not be read.
    """
    steps: list[dict[str, str]] = []

    found = int(sources["found"] or 0)
    pending = int(sources["pending"] or 0)
    failed = int(sources["failed"] or 0)
    cloud = int(sources["cloud_only"] or 0)

    if found == 0:
        steps.append({
            "label": "Find Outlook files on this computer",
            "href": "#/sources",
            "why": "Recall has not looked yet. This reads names and sizes only.",
            "primary": "yes",
        })
        return steps

    if pending or cloud:
        why = "Nothing in them is in the archive until they are read."
        if cloud:
            why += (
                f" {cloud:,} of them are in OneDrive and will be downloaded "
                "first - you will be shown the size before anything starts."
            )
        steps.append({
            "label": "Find every Outlook file and read it into the archive",
            "href": "#/sources?start=1",
            "why": why,
            "primary": "yes",
        })

    if item_count:
        steps.append({
            "label": "Search the archive",
            "href": "#/search",
            "why": f"{item_count:,} records are searchable.",
            "primary": "yes" if not pending else "",
        })

    critical = conn.execute(
        "SELECT COUNT(*) AS n FROM findings WHERE state IN ('open','acknowledged') "
        "AND severity IN ('critical','high')"
    ).fetchone()["n"]
    if critical:
        steps.append({
            "label": f"Look at {critical:,} serious problem(s)",
            "href": "#/problems",
            "why": "Records are missing or wrong, and Recall can say which.",
            "primary": "",
        })

    if failed:
        steps.append({
            "label": f"See why {failed:,} file(s) could not be read",
            "href": "#/problems",
            "why": "Trying the other reader sometimes recovers them.",
            "primary": "",
        })

    merges = conn.execute(
        "SELECT COUNT(*) AS n FROM findings WHERE code = 'under_merged' "
        "AND state IN ('open','acknowledged')"
    ).fetchone()["n"]
    if merges:
        steps.append({
            "label": f"Confirm {merges:,} possible duplicate person/people",
            "href": "#/people",
            "why": "Recall will not merge anybody without you saying so.",
            "primary": "",
        })

    if item_count:
        steps.append({
            "label": "See it all on the timeline",
            "href": "#/timeline",
            "why": "Everything by date, with the gaps marked.",
            "primary": "",
        })

    return steps[:6]


def _file_size(path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _blob_size(settings) -> int:
    from ..normalize.attachments import BlobStore

    try:
        return BlobStore(settings.blobs_path).total_bytes()
    except OSError:
        return 0


def _month_after(month: str) -> str:
    """'2013-02' to '2013-03-01', for an exclusive upper bound."""
    year, _, m = month.partition("-")
    y, n = int(year), int(m or 1)
    if n >= 12:
        return f"{y + 1:04d}-01-01"
    return f"{y:04d}-{n + 1:02d}-01"
