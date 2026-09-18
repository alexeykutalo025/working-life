"""Reading source files into the archive.

This is where the spec's reliability requirements are actually met, so they are
worth naming:

* **Resumable.** ``source_files.parse_state`` is committed after every file. A
  crash at hour nine of a twelve-hour run costs the file in progress, nothing
  more, and ``--resume`` starts from the next one.
* **Idempotent.** Items are keyed by ``dedup_key``. Re-running produces the same
  archive, never a second copy - and every place an item was found still gets
  its own ``item_sources`` row, so provenance survives deduplication.
* **Cancelable.** Ctrl-C or the Stop button sets an event; the current
  transaction finishes, state is written, and the process exits cleanly.
* **Unstoppable by one bad file.** Every per-file failure is caught, recorded in
  ``errors`` and in ``findings``, and the loop moves to the next file.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .config import Settings
from .db import log_error, transaction
from .integrity.engine import Finding, record_finding
from .integrity.sources import (
    record_claimed_count,
    record_cross_check,
    record_folder_claims,
    record_sampled,
)
from .logging_setup import get_logger
from .models import Kind, ParsedItem, ParseState, Role, Severity
from .normalize.dedup import dedup_key_for
from .normalize.attachments import BlobStore, store_attachments
from .normalize.people import PeopleResolver
from .parsers.base import load_all_parsers, parser_for

log = get_logger("extract")

ALL_KINDS = frozenset({Kind.MESSAGE, Kind.EVENT, Kind.CONTACT, Kind.TASK, Kind.NOTE})

#: The words the user types on the command line, and what they mean.
KIND_GROUPS = {
    "calendar": frozenset({Kind.EVENT}),
    "contacts": frozenset({Kind.CONTACT}),
    "mail": frozenset({Kind.MESSAGE}),
    "tasks": frozenset({Kind.TASK}),
    "notes": frozenset({Kind.NOTE}),
    "all": ALL_KINDS,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ExtractProgress:
    files_total: int = 0
    files_done: int = 0
    items_written: int = 0
    items_seen: int = 0
    duplicates_collapsed: int = 0
    attachments_written: int = 0
    current_file: str = ""
    current_folder: str = ""
    state: str = "running"
    message: str = ""
    started: float = field(default_factory=time.monotonic)
    failures: list[tuple[str, str]] = field(default_factory=list)
    detail_threads: dict = field(default_factory=dict)

    @property
    def items_per_sec(self) -> float:
        elapsed = time.monotonic() - self.started
        return self.items_seen / elapsed if elapsed > 0.5 else 0.0

    def as_dict(self) -> dict:
        return {
            "files_total": self.files_total,
            "files_done": self.files_done,
            "items_written": self.items_written,
            "items_seen": self.items_seen,
            "duplicates_collapsed": self.duplicates_collapsed,
            "attachments_written": self.attachments_written,
            "current_file": self.current_file,
            "current_folder": self.current_folder,
            "state": self.state,
            "message": self.message,
            "items_per_sec": round(self.items_per_sec, 1),
            "failures": self.failures,
            "threads": self.detail_threads,
        }


def parse_kinds(text: str | None) -> frozenset[str]:
    """"calendar,contacts" to the set of kinds. Unknown words are refused."""
    if not text:
        return ALL_KINDS
    wanted: set[str] = set()
    unknown: list[str] = []
    for word in (w.strip().lower() for w in text.split(",") if w.strip()):
        if word in KIND_GROUPS:
            wanted |= KIND_GROUPS[word]
        elif word in ALL_KINDS:
            wanted.add(word)
        else:
            unknown.append(word)
    if unknown:
        raise ValueError(
            f"Not a kind of record Recall knows: {', '.join(unknown)}. "
            f"Choose from: {', '.join(sorted(KIND_GROUPS))}"
        )
    return frozenset(wanted) if wanted else ALL_KINDS


class Extractor:
    """Runs one extraction over a set of source files."""

    def __init__(
        self,
        settings: Settings,
        conn,
        *,
        cancel: threading.Event | None = None,
        on_progress: Callable[[ExtractProgress], None] | None = None,
    ) -> None:
        self.settings = settings
        self.conn = conn
        self.cancel = cancel or threading.Event()
        self.on_progress = on_progress
        self.progress = ExtractProgress()
        self.people = PeopleResolver(conn, settings)
        self.blobs = BlobStore(settings.blobs_path)
        self._folder_cache: dict[tuple[int, str], int] = {}
        load_all_parsers()

    # -- the run ----------------------------------------------------------

    def run(
        self,
        *,
        kinds: frozenset[str] | None = None,
        source_ids: Iterable[int] | None = None,
        sample: int = 0,
        resume: bool = True,
    ) -> ExtractProgress:
        kinds = kinds or ALL_KINDS
        sources = self._sources_to_read(source_ids, resume)
        self.progress.files_total = len(sources)

        if not sources:
            self.progress.state = "done"
            self.progress.message = (
                "There is nothing left to read. Every file chosen has already "
                "been read, or none was chosen."
            )
            return self.progress

        log.info("Reading %d file(s), kinds: %s", len(sources), ", ".join(sorted(kinds)))

        for row in sources:
            if self.cancel.is_set():
                self.progress.state = "canceled"
                self.progress.message = (
                    f"Stopped after {self.progress.files_done} of "
                    f"{self.progress.files_total} files. Everything read so far is "
                    "saved - run it again to carry on from here."
                )
                return self.progress

            self._read_one(dict(row), kinds, sample)
            self.progress.files_done += 1
            self._report()

        self.progress.state = "done"
        self.progress.message = self._summary()
        self._run_post_checks()
        return self.progress

    def _sources_to_read(self, source_ids, resume: bool) -> list:
        sql = [
            "SELECT * FROM source_files WHERE is_placeholder = 0 AND is_readable = 1"
        ]
        params: list = []
        if source_ids:
            ids = list(source_ids)
            sql.append(f"AND id IN ({','.join('?' * len(ids))})")
            params.extend(ids)
        if resume:
            # A file left in 'parsing' was interrupted; it is read again from
            # the start, because a half-read file is not a read file.
            sql.append("AND parse_state IN ('pending', 'selected', 'parsing', 'failed')")
        sql.append("ORDER BY size_bytes ASC, id ASC")
        return self.conn.execute(" ".join(sql), params).fetchall()

    # -- one file ---------------------------------------------------------

    def _read_one(self, source: dict, kinds: frozenset[str], sample: int) -> None:
        source_id = int(source["id"])
        path = Path(source["path"])
        self.progress.current_file = str(path)
        self.progress.current_folder = ""
        self._report()

        parser_cls = parser_for(path, source["ext"])
        if parser_cls is None:
            self.conn.execute(
                "UPDATE source_files SET parse_state = 'skipped', parse_error = ? WHERE id = ?",
                (f"Recall has no reader for {source['ext']} files.", source_id),
            )
            log.info("No reader for %s, skipping", path)
            return

        if not path.exists():
            self._fail(
                source_id,
                path,
                "The file is no longer where it was found. It may have been moved "
                "or deleted since the last search.",
                "FileNotFoundError",
            )
            return

        self.conn.execute(
            "UPDATE source_files SET parse_state = 'parsing', parse_error = NULL WHERE id = ?",
            (source_id,),
        )

        limit = sample or self.settings.extract.sample_limit
        kwargs: dict = {"sample_limit": limit}
        if parser_cls.name == "pst":
            kwargs["preferred"] = self.settings.extract.pst_backend
            kwargs["cross_check"] = self.settings.extract.cross_check_backends and not limit
            kwargs["com_stall_timeout"] = self.settings.extract.com_stall_timeout_seconds

        record_sampled(self.conn, source_id, limit)

        parser = parser_cls(path, **kwargs)
        written = 0
        seen = 0
        batch: list[ParsedItem] = []
        batch_size = self.settings.extract.batch_size

        try:
            for item in parser.parse(kinds):
                if self.cancel.is_set():
                    break
                seen += 1
                self.progress.items_seen += 1
                if item.folder_path:
                    self.progress.current_folder = item.folder_path
                batch.append(item)

                if len(batch) >= batch_size:
                    written += self._write_batch(batch, source_id)
                    batch = []
                    self._report()

            if batch:
                written += self._write_batch(batch, source_id)

        except Exception as exc:  # noqa: BLE001 - one file never ends the run
            if batch:
                try:
                    written += self._write_batch(batch, source_id)
                except Exception as flush_exc:  # noqa: BLE001
                    log.warning("The last batch from %s could not be saved: %s", path, flush_exc)
            self._fail(source_id, path, str(exc), repr(exc), partial_items=written)
            self._record_outcome(source_id, parser, written)
            return
        finally:
            try:
                parser.close()
            except Exception:  # noqa: BLE001
                pass

        self._record_outcome(source_id, parser, written)
        self._finish_source(source_id, parser, written, canceled=self.cancel.is_set())
        self.progress.items_written += written
        log.info("%s: %d records read, %d new", path.name, seen, written)

    def _write_batch(self, batch: list[ParsedItem], source_id: int) -> int:
        """One transaction per batch. Returns how many new items were created."""
        created = 0
        with transaction(self.conn):
            for item in batch:
                if self._write_item(item, source_id):
                    created += 1
        return created

    def _write_item(self, item: ParsedItem, source_id: int) -> bool:
        """Write one item. Returns True when it was new.

        A duplicate is not written again, but its ``item_sources`` row always
        is: the fact that this message also exists in that file is information
        the archive must not lose.
        """
        try:
            key = dedup_key_for(item)
        except ValueError as exc:
            log_error(
                self.conn,
                "extract",
                f"A record could not be given an identity and was not saved: {exc}",
                detail=f"kind={item.kind} subject={item.subject!r}",
                source_file_id=source_id,
            )
            return False

        folder_id = self._folder_id(source_id, item.folder_path)

        existing = self.conn.execute(
            "SELECT id FROM items WHERE dedup_key = ?", (key,)
        ).fetchone()

        if existing is not None:
            item_id = int(existing["id"])
            self.progress.duplicates_collapsed += 1
            self._link_source(item_id, source_id, folder_id, item.native_id)
            return False

        cur = self.conn.execute(
            """
            INSERT INTO items
                (kind, dedup_key, occurred_utc, occurred_local, tz, end_utc, all_day,
                 subject, body_text, body_html, body_format, location, importance,
                 sensitivity, internet_message_id, in_reply_to, references_json,
                 conversation_topic, ical_uid, recurrence_json, is_recurring_master,
                 recurrence_id, meeting_status, busy_status, contact_json, folder_id,
                 has_attachments, raw_headers, parse_confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?)
            """,
            (
                item.kind,
                key,
                item.occurred.utc,
                item.occurred.local,
                item.occurred.tz,
                item.end.utc if item.end else None,
                int(item.all_day),
                item.subject,
                item.body_text,
                item.body_html,
                item.body_format,
                item.location,
                item.importance,
                item.sensitivity,
                item.internet_message_id,
                item.in_reply_to,
                json.dumps(item.references) if item.references else None,
                item.conversation_topic,
                item.ical_uid,
                json.dumps(item.recurrence, default=str) if item.recurrence else None,
                int(item.is_recurring_master),
                item.recurrence_id,
                item.meeting_status,
                item.busy_status,
                json.dumps(item.contact, default=str) if item.contact else None,
                folder_id,
                int(item.has_attachments),
                item.raw_headers,
                item.parse_confidence,
            ),
        )
        item_id = int(cur.lastrowid)

        self._link_source(item_id, source_id, folder_id, item.native_id)
        self._write_participants(item_id, item)
        self._write_attachments(item_id, item)
        self._write_notes(item_id, source_id, item)

        if item.categories:
            self._write_tags(item_id, item.categories)

        return True

    def _link_source(self, item_id: int, source_id: int, folder_id: int | None, native_id: str | None) -> None:
        """Provenance. One logical item may live in many files; keep all of them."""
        self.conn.execute(
            "INSERT OR IGNORE INTO item_sources(item_id, source_file_id, folder_id, native_id) "
            "VALUES (?, ?, ?, ?)",
            (item_id, source_id, folder_id, native_id or ""),
        )

    def _write_participants(self, item_id: int, item: ParsedItem) -> None:
        when = item.occurred.utc
        for participant in item.participants:
            norm = self.people.normalize(participant.address, participant.display_name)
            if norm is None:
                continue
            identity_id = self.people.identity_id(norm, when)
            person_id = self.people.person_for_identity(identity_id)
            self.conn.execute(
                "INSERT OR IGNORE INTO participations"
                "(item_id, person_id, identity_id, role, response_status) "
                "VALUES (?, ?, ?, ?, ?)",
                (item_id, person_id, identity_id, participant.role, participant.response_status),
            )

    def _write_attachments(self, item_id: int, item: ParsedItem) -> None:
        """Store the bytes once, and pull out text for search."""
        if not item.attachments:
            return
        try:
            stored, failed = store_attachments(
                self.conn,
                self.blobs,
                item_id,
                item.attachments,
                size_cap=self.settings.extract.attachment_text_cap_bytes,
            )
        except Exception as exc:  # noqa: BLE001 - attachments never fail an item
            log.warning("Attachments for item %d could not be saved: %s", item_id, exc)
            log_error(
                self.conn, "extract",
                f"Attachments for one record could not be saved: {exc}",
                detail=repr(exc),
            )
            return
        self.progress.attachments_written += stored
        if failed:
            log.debug("%d attachment(s) on item %d could not be read", failed, item_id)

    def _write_notes(self, item_id: int, source_id: int, item: ParsedItem) -> None:
        """Parser-level observations become findings attached to the item."""
        severities = {
            "no_date": Severity.MEDIUM,
            "implausible_date": Severity.MEDIUM,
            "unknown_timezone": Severity.MEDIUM,
            "low_confidence_text": Severity.MEDIUM,
            "unresolved_recurrence": Severity.MEDIUM,
        }
        titles = {
            "no_date": "A record has no date",
            "implausible_date": "A record has a date that cannot be right",
            "unknown_timezone": "A record's timezone is not recorded",
            "low_confidence_text": "A record's text had to be guessed at",
            "unresolved_recurrence": "A repeating entry's rule could not be read",
        }
        for code, detail in item.notes:
            record_finding(
                self.conn,
                Finding(
                    code=code,
                    severity=severities.get(code, Severity.MEDIUM),
                    title=f"{titles.get(code, code)}: {item.subject or '(no subject)'}",
                    detail=detail,
                    item_id=item_id,
                    # Deliberately not source_file_id. An item-level finding is
                    # about the record, and the record already names its files
                    # through item_sources. Setting both would give the same
                    # fact two different dedup keys, so a later sweep for the
                    # same condition would report it twice.
                    affected_count=1,
                    evidence={
                        "subject": item.subject,
                        "kind": item.kind,
                        "source_file_id": source_id,
                    },
                ),
            )

    def _write_tags(self, item_id: int, categories: list[str]) -> None:
        for name in categories:
            name = name.strip()
            if not name:
                continue
            self.conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (name,))
            row = self.conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
            if row:
                self.conn.execute(
                    "INSERT OR IGNORE INTO item_tags(item_id, tag_id) VALUES (?, ?)",
                    (item_id, int(row["id"])),
                )

    def _folder_id(self, source_id: int, folder_path: str | None) -> int | None:
        if not folder_path:
            return None
        key = (source_id, folder_path)
        cached = self._folder_cache.get(key)
        if cached is not None:
            return cached

        row = self.conn.execute(
            "SELECT id FROM folders WHERE source_file_id = ? AND path = ?", key
        ).fetchone()
        if row is not None:
            folder_id = int(row["id"])
        else:
            parent_id = None
            if "/" in folder_path:
                parent_id = self._folder_id(source_id, folder_path.rsplit("/", 1)[0])
            cur = self.conn.execute(
                "INSERT INTO folders(source_file_id, path, parent_id) VALUES (?, ?, ?)",
                (source_id, folder_path, parent_id),
            )
            folder_id = int(cur.lastrowid)

        self._folder_cache[key] = folder_id
        self.conn.execute(
            "UPDATE folders SET item_count = item_count + 1 WHERE id = ?", (folder_id,)
        )
        return folder_id

    # -- per-file bookkeeping --------------------------------------------

    def _record_outcome(self, source_id: int, parser, written: int) -> None:
        """Store what the file claimed about itself, for estimated_loss later."""
        outcome = parser.outcome

        if outcome.claimed_count is not None:
            record_claimed_count(self.conn, source_id, outcome.claimed_count)
        if outcome.folder_counts:
            record_folder_claims(self.conn, source_id, outcome.folder_counts)

        counts = getattr(parser, "backend_counts", None)
        if counts and any(v is not None for v in counts.values()):
            record_cross_check(self.conn, source_id, counts.get("pypff"), counts.get("com"))

        for code, severity, title, detail, evidence in outcome.findings:
            record_finding(
                self.conn,
                Finding(
                    code=code,
                    severity=severity,
                    title=title,
                    detail=detail,
                    source_file_id=source_id,
                    evidence=evidence,
                ),
            )

    def _finish_source(self, source_id: int, parser, written: int, canceled: bool) -> None:
        """Commit the file's state. This is what makes --resume work."""
        row = self.conn.execute(
            "SELECT MIN(i.occurred_utc) AS first, MAX(i.occurred_utc) AS last, "
            "COUNT(*) AS n FROM items i "
            "JOIN item_sources s ON s.item_id = i.id WHERE s.source_file_id = ?",
            (source_id,),
        ).fetchone()

        state = ParseState.PARSING if canceled else ParseState.DONE
        self.conn.execute(
            "UPDATE source_files SET parse_state = ?, parse_backend = ?, "
            "item_count = ?, first_item_utc = ?, last_item_utc = ?, parse_error = NULL "
            "WHERE id = ?",
            (
                state,
                parser.outcome.backend,
                int(row["n"] or 0),
                row["first"],
                row["last"],
                source_id,
            ),
        )

    def _fail(
        self,
        source_id: int,
        path: Path,
        message: str,
        detail: str,
        partial_items: int = 0,
    ) -> None:
        log.error("%s could not be read: %s", path, message)
        self.progress.failures.append((str(path), message))
        self.conn.execute(
            "UPDATE source_files SET parse_state = 'failed', parse_error = ?, "
            "item_count = ? WHERE id = ?",
            (message, partial_items, source_id),
        )
        log_error(self.conn, "extract", f"{path}: {message}", detail=detail, source_file_id=source_id)

    # -- after the run ----------------------------------------------------

    def _run_post_checks(self) -> None:
        """Integrity checks run at the end of every extraction (spec section 9)."""
        # Threading is a global property - one late message can join two
        # existing threads - so it is rebuilt rather than maintained.
        # The search index is built as part of extraction. Asking the user to
        # run a second command before search works would mean a search that
        # quietly finds nothing, which reads as "it is not in the archive".
        try:
            from .search.indexer import build_index

            with transaction(self.conn):
                build_index(self.conn)
        except Exception as exc:  # noqa: BLE001
            log.warning("The search index could not be built: %s", exc)
            log_error(self.conn, "extract", f"Building the search index failed: {exc}")

        try:
            from .normalize.threads import rebuild_threads

            with transaction(self.conn):
                result = rebuild_threads(self.conn)
            self.progress.detail_threads = result
        except Exception as exc:  # noqa: BLE001
            log.warning("Conversations could not be rebuilt: %s", exc)
            log_error(self.conn, "extract", f"Rebuilding conversations failed: {exc}")

        # People's counts and date spans are derived, so they are recomputed
        # rather than maintained incrementally: an incremental count that drifts
        # is worse than no count, because it looks authoritative.
        try:
            from .normalize.merge import recount_all

            with transaction(self.conn):
                recount_all(self.conn)
        except Exception as exc:  # noqa: BLE001
            log.warning("People's totals could not be recounted: %s", exc)
            log_error(self.conn, "extract", f"Recounting people failed: {exc}")

        try:
            from .integrity.engine import run_all_checks

            run_all_checks(self.conn, self.settings)
        except ImportError as exc:
            # A check family that is not present yet is reported, not hidden.
            log.warning("Not every integrity check could run: %s", exc)
            log_error(self.conn, "integrity", f"Some checks are unavailable: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("Integrity checks after extraction failed")
            log_error(self.conn, "integrity", str(exc), detail=repr(exc))

    def _summary(self) -> str:
        p = self.progress
        parts = [
            f"Read {p.files_done:,} file{'s' if p.files_done != 1 else ''} and added "
            f"{p.items_written:,} record{'s' if p.items_written != 1 else ''} to the archive."
        ]
        if p.duplicates_collapsed:
            parts.append(
                f"{p.duplicates_collapsed:,} record{'s were' if p.duplicates_collapsed != 1 else ' was'} "
                "already in the archive from another file; the archive now records "
                "every place each one was found."
            )
        if p.failures:
            parts.append(
                f"{len(p.failures)} file{'s' if len(p.failures) != 1 else ''} could not "
                "be read - see the Problems screen for what is missing."
            )
        return " ".join(parts)

    def _report(self) -> None:
        if self.on_progress is not None:
            self.on_progress(self.progress)
