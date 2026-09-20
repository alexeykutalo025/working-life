"""SQLite connection, schema and migrations.

The schema is section 4 of the build spec, reproduced exactly, plus three
additions that the spec's own requirements imply but its DDL does not express:

  1. ``ux_findings_key`` - the spec puts ``UNIQUE(code, source_file_id, item_id,
     person_id, period_start)`` on ``findings``, intending re-runs to be
     idempotent. SQLite treats NULLs as distinct in a UNIQUE constraint, and
     nearly every finding has NULLs in those columns, so that constraint alone
     would let every ``recall audit`` add another copy of the same finding. The
     specified constraint is kept verbatim and a COALESCE-based unique index is
     added beside it so the intent actually holds.

  2. FTS5 sync triggers on ``search_docs``. The spec asks for "triggers (or an
     explicit reindex step)". Both are provided.

  3. ``schema_migrations`` - so a database built by an older version can be
     recognised and upgraded rather than silently misread.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 2

# ---------------------------------------------------------------------------
# Schema, verbatim from build spec section 4.
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE scan_runs (
  id INTEGER PRIMARY KEY,
  started_utc TEXT NOT NULL, finished_utc TEXT,
  roots_json TEXT NOT NULL,
  files_seen INTEGER DEFAULT 0, candidates_found INTEGER DEFAULT 0,
  state TEXT DEFAULT 'running'          -- running|done|canceled|failed
);

CREATE TABLE source_files (
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  container TEXT,                        -- local|onedrive|external|network
  ext TEXT NOT NULL,
  size_bytes INTEGER,
  mtime_utc TEXT, ctime_utc TEXT,
  content_hash TEXT,                     -- blake2b-256, NULL if not hydrated
  is_placeholder INTEGER DEFAULT 0,      -- OneDrive cloud-only file
  is_readable INTEGER, lock_error TEXT,
  duplicate_of INTEGER REFERENCES source_files(id),
  scan_run_id INTEGER REFERENCES scan_runs(id),
  parse_state TEXT DEFAULT 'pending',    -- pending|selected|parsing|done|failed|skipped
  parse_backend TEXT, parse_error TEXT,
  item_count INTEGER DEFAULT 0,
  first_item_utc TEXT, last_item_utc TEXT,
  user_note TEXT
);

CREATE TABLE folders (
  id INTEGER PRIMARY KEY,
  source_file_id INTEGER REFERENCES source_files(id),
  path TEXT NOT NULL,                    -- e.g. "Top of Outlook Data File/Inbox/Clients"
  parent_id INTEGER REFERENCES folders(id),
  item_count INTEGER DEFAULT 0
);

CREATE TABLE items (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,                    -- message|event|contact|task|note
  dedup_key TEXT NOT NULL UNIQUE,
  occurred_utc TEXT,                     -- sent date / event start / created
  occurred_local TEXT, tz TEXT,
  end_utc TEXT, all_day INTEGER DEFAULT 0,
  subject TEXT, body_text TEXT, body_html TEXT, body_format TEXT,
  location TEXT, importance TEXT, sensitivity TEXT,
  internet_message_id TEXT, in_reply_to TEXT, references_json TEXT,
  thread_id INTEGER, conversation_topic TEXT,
  ical_uid TEXT, recurrence_json TEXT,
  is_recurring_master INTEGER DEFAULT 0, recurrence_id TEXT,
  meeting_status TEXT, busy_status TEXT,
  contact_json TEXT,                     -- full contact card for kind='contact'
  folder_id INTEGER REFERENCES folders(id),
  has_attachments INTEGER DEFAULT 0,
  raw_headers TEXT,
  parse_confidence REAL DEFAULT 1.0,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_items_occurred ON items(occurred_utc);
CREATE INDEX idx_items_kind_occurred ON items(kind, occurred_utc);
CREATE INDEX idx_items_thread ON items(thread_id);
CREATE INDEX idx_items_msgid ON items(internet_message_id);
CREATE INDEX idx_items_uid ON items(ical_uid);

-- provenance: one logical item may exist in many source files. Never lose this.
CREATE TABLE item_sources (
  item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  source_file_id INTEGER NOT NULL REFERENCES source_files(id),
  folder_id INTEGER REFERENCES folders(id),
  native_id TEXT,
  PRIMARY KEY (item_id, source_file_id, native_id)
);

CREATE TABLE people (
  id INTEGER PRIMARY KEY,
  display_name TEXT NOT NULL,
  merged_into INTEGER REFERENCES people(id),
  org TEXT, role TEXT, notes TEXT,
  is_self INTEGER DEFAULT 0,
  first_seen_utc TEXT, last_seen_utc TEXT, item_count INTEGER DEFAULT 0,
  confirmed_by_user INTEGER DEFAULT 0
);

CREATE TABLE identities (
  id INTEGER PRIMARY KEY,
  person_id INTEGER REFERENCES people(id),
  address TEXT NOT NULL,                 -- normalized lowercase SMTP, or Exchange legacyDN
  address_type TEXT NOT NULL,            -- smtp|ex|phone|none
  raw_display_name TEXT,
  first_seen_utc TEXT, last_seen_utc TEXT, use_count INTEGER DEFAULT 0,
  UNIQUE(address, address_type)
);

CREATE TABLE participations (
  item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  person_id INTEGER REFERENCES people(id),
  identity_id INTEGER NOT NULL REFERENCES identities(id),
  role TEXT NOT NULL,                    -- from|to|cc|bcc|organizer|attendee|optional|resource
  response_status TEXT,
  PRIMARY KEY (item_id, identity_id, role)
);
CREATE INDEX idx_part_person ON participations(person_id);

CREATE TABLE attachments (
  id INTEGER PRIMARY KEY,
  item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
  filename TEXT, mime_type TEXT, size_bytes INTEGER,
  content_hash TEXT,                     -- blob at blobs/<h0:2>/<h2:4>/<hash>.<ext>
  is_inline INTEGER DEFAULT 0, content_id TEXT,
  extracted_text TEXT,
  extract_state TEXT DEFAULT 'pending'   -- pending|done|unsupported|failed
);
CREATE INDEX idx_att_hash ON attachments(content_hash);

CREATE TABLE threads (
  id INTEGER PRIMARY KEY,
  subject_normalized TEXT, first_utc TEXT, last_utc TEXT, message_count INTEGER DEFAULT 0
);

CREATE TABLE eras (                      -- user-defined life/career periods
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL, start_utc TEXT, end_utc TEXT, color TEXT, notes TEXT
);

CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, color TEXT);
CREATE TABLE item_tags (
  item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
  tag_id INTEGER REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY (item_id, tag_id)
);

-- raw technical exceptions as they happen during a run
CREATE TABLE errors (
  id INTEGER PRIMARY KEY, occurred_utc TEXT DEFAULT (datetime('now')),
  stage TEXT, source_file_id INTEGER, message TEXT, detail TEXT,
  acknowledged INTEGER DEFAULT 0
);

-- curated integrity findings surfaced to the user. See section 9.
CREATE TABLE findings (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL,                    -- see section 9 for the full vocabulary
  severity TEXT NOT NULL,                -- critical|high|medium|info
  title TEXT NOT NULL,                   -- one plain-language line
  detail TEXT,                           -- what we know, including the raw exception
  source_file_id INTEGER REFERENCES source_files(id),
  item_id INTEGER REFERENCES items(id),
  person_id INTEGER REFERENCES people(id),
  period_start TEXT, period_end TEXT,    -- for coverage gaps
  affected_count INTEGER,                -- records known affected
  estimated_loss INTEGER,                -- records we believe we could NOT read
  evidence_json TEXT,                    -- structured backing for the claim
  state TEXT DEFAULT 'open',             -- open|acknowledged|explained|resolved|wont_fix
  user_note TEXT,
  first_seen_utc TEXT DEFAULT (datetime('now')),
  last_seen_utc TEXT,
  resolved_utc TEXT,
  UNIQUE(code, source_file_id, item_id, person_id, period_start)
);
CREATE INDEX idx_findings_state ON findings(state, severity);

-- monthly coverage census, rebuilt after every extraction
CREATE TABLE coverage_months (
  month TEXT NOT NULL,                   -- 'YYYY-MM'
  kind TEXT NOT NULL,
  item_count INTEGER DEFAULT 0,
  source_count INTEGER DEFAULT 0,
  gap_class TEXT,                        -- NULL|hard_gap|soft_gap|source_contradiction
  explained_by_user INTEGER DEFAULT 0,
  PRIMARY KEY (month, kind)
);

CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);

-- search: a materialized doc per item, because FTS5 external-content
-- cannot span joins (participants and attachment text live elsewhere).
CREATE TABLE search_docs (
  item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
  subject TEXT, body TEXT, participants TEXT, location TEXT,
  attachment_names TEXT, attachment_text TEXT
);
CREATE VIRTUAL TABLE items_fts USING fts5(
  subject, body, participants, location, attachment_names, attachment_text,
  content='search_docs', content_rowid='item_id',
  tokenize='porter unicode61 remove_diacritics 2'
);
"""

# ---------------------------------------------------------------------------
# Additions. Each one is here because the spec's requirements need it.
# ---------------------------------------------------------------------------

ADDITIONS_SQL = """
-- A cloud-only file cannot be read where it sits without downloading it, so
-- Recall keeps its own copy under workdir/cloud and reads that instead. The
-- original path stays in source_files.path, because that is the name the user
-- knows the file by; these three columns say where the copy is and when it
-- arrived. Stated as ALTER TABLE rather than inline in SCHEMA_SQL so that a
-- fresh archive and an upgraded one run the same statements - see _MIGRATIONS.
ALTER TABLE source_files ADD COLUMN local_copy_path TEXT;
ALTER TABLE source_files ADD COLUMN local_copy_bytes INTEGER;
ALTER TABLE source_files ADD COLUMN local_copy_utc TEXT;
CREATE INDEX idx_source_files_copy ON source_files(local_copy_path);

-- The spec's UNIQUE(code, source_file_id, item_id, person_id, period_start) does
-- not dedupe rows containing NULLs, and most findings contain NULLs there. This
-- index makes "run the audit twice, get one finding" actually true.
CREATE UNIQUE INDEX ux_findings_key ON findings(
  code,
  COALESCE(source_file_id, -1),
  COALESCE(item_id, -1),
  COALESCE(person_id, -1),
  COALESCE(period_start, '')
);

CREATE INDEX idx_findings_code ON findings(code);
CREATE INDEX idx_findings_source ON findings(source_file_id);
CREATE INDEX idx_findings_item ON findings(item_id);
CREATE INDEX idx_findings_person ON findings(person_id);
CREATE INDEX idx_findings_period ON findings(period_start, period_end);

CREATE INDEX idx_source_files_state ON source_files(parse_state);
CREATE INDEX idx_source_files_hash ON source_files(content_hash);
CREATE INDEX idx_source_files_ext ON source_files(ext);
CREATE INDEX idx_folders_source ON folders(source_file_id);
CREATE INDEX idx_item_sources_source ON item_sources(source_file_id);
CREATE INDEX idx_identities_person ON identities(person_id);
CREATE INDEX idx_people_merged ON people(merged_into);
CREATE INDEX idx_att_item ON attachments(item_id);
CREATE INDEX idx_errors_stage ON errors(stage, occurred_utc);

-- Keep items_fts in lock-step with search_docs. FTS5 external-content tables
-- need the 'delete' command issued with the OLD values before a row changes.
CREATE TRIGGER search_docs_ai AFTER INSERT ON search_docs BEGIN
  INSERT INTO items_fts(rowid, subject, body, participants, location,
                        attachment_names, attachment_text)
  VALUES (new.item_id, new.subject, new.body, new.participants, new.location,
          new.attachment_names, new.attachment_text);
END;

CREATE TRIGGER search_docs_ad AFTER DELETE ON search_docs BEGIN
  INSERT INTO items_fts(items_fts, rowid, subject, body, participants, location,
                        attachment_names, attachment_text)
  VALUES ('delete', old.item_id, old.subject, old.body, old.participants,
          old.location, old.attachment_names, old.attachment_text);
END;

CREATE TRIGGER search_docs_au AFTER UPDATE ON search_docs BEGIN
  INSERT INTO items_fts(items_fts, rowid, subject, body, participants, location,
                        attachment_names, attachment_text)
  VALUES ('delete', old.item_id, old.subject, old.body, old.participants,
          old.location, old.attachment_names, old.attachment_text);
  INSERT INTO items_fts(rowid, subject, body, participants, location,
                        attachment_names, attachment_text)
  VALUES (new.item_id, new.subject, new.body, new.participants, new.location,
          new.attachment_names, new.attachment_text);
END;

CREATE TABLE schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_utc TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class DatabaseError(Exception):
    """Something is wrong with the archive database itself."""


_local = threading.local()


def connect(db_path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open the archive, applying the pragmas the spec requires.

    Creates the file and the schema when it does not exist yet.
    """
    db_path = Path(db_path)
    if not read_only:
        db_path.parent.mkdir(parents=True, exist_ok=True)

    if read_only:
        uri = f"file:{db_path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    else:
        conn = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None)

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.create_function("recall_version", 0, lambda: SCHEMA_VERSION)

    if not read_only:
        migrate(conn)
    return conn


#: Upgrade steps for an archive that already exists, keyed by the version they
#: produce. A fresh archive never runs these - it gets the same statements as
#: part of ADDITIONS_SQL - so the two paths cannot drift apart. Every entry is
#: append-only: once a version has shipped, its statements are frozen.
_MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: (
        "ALTER TABLE source_files ADD COLUMN local_copy_path TEXT",
        "ALTER TABLE source_files ADD COLUMN local_copy_bytes INTEGER",
        "ALTER TABLE source_files ADD COLUMN local_copy_utc TEXT",
        "CREATE INDEX IF NOT EXISTS idx_source_files_copy "
        "ON source_files(local_copy_path)",
    ),
}


def migrate(conn: sqlite3.Connection) -> int:
    """Bring an archive up to SCHEMA_VERSION. Returns the version in force."""
    have_tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )
    }

    if "items" not in have_tables:
        conn.executescript("BEGIN;" + SCHEMA_SQL + ADDITIONS_SQL + "COMMIT;")
        conn.execute(
            "INSERT INTO schema_migrations(version) VALUES (?)", (SCHEMA_VERSION,)
        )
        return SCHEMA_VERSION

    if "schema_migrations" not in have_tables:
        raise DatabaseError(
            "This archive was made by a version of Recall that is too old to "
            "upgrade automatically. Move workdir/archive.db aside and scan again."
        )

    row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    current = row["v"] if row and row["v"] is not None else 0
    if current > SCHEMA_VERSION:
        raise DatabaseError(
            f"This archive was made by a newer version of Recall "
            f"(schema {current}, this program understands {SCHEMA_VERSION}). "
            "Update Recall, or use a different working folder."
        )

    for version in sorted(v for v in _MIGRATIONS if v > current):
        # SQLite runs DDL inside a transaction, so a half-applied upgrade is not
        # a state this can end in: either every statement lands and the version
        # is recorded, or the archive is exactly as it was.
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _MIGRATIONS[version]:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
            )
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        current = version

    return current


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit transaction that rolls back on any exception.

    Used around every batch write so a crash mid-extraction leaves the archive
    consistent and `--resume` can pick up from the last completed source.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    else:
        conn.execute("COMMIT")


#: Match a column against a list of ids, as ``WHERE id {IN_IDS}``, with
#: ``ids_param(ids)`` as the matching parameter.
#:
#: The obvious way to write that is ``id IN (?,?,?,...)`` with one parameter
#: per id, and it works on a test archive of twenty records. SQLite takes at
#: most SQLITE_LIMIT_VARIABLE_NUMBER host parameters in one statement - 32,766
#: on a current build, 999 on an older one - so somewhere between a test
#: archive and a real one it stops working. It stopped on the client's: a
#: search matching 42,767 records answered "too many SQL variables" where the
#: table should have been, and the download beside it would have done the same.
#:
#: json_each turns the whole list into one parameter, so there is no limit left
#: to reach. It has been part of SQLite itself since 3.38 and needs nothing
#: installed. Ids are integers out of this same database, so nothing user-typed
#: goes anywhere near the statement.
IN_IDS = "IN (SELECT value FROM json_each(?))"


def ids_param(ids) -> str:
    """The parameter that goes with :data:`IN_IDS`."""
    return json.dumps([int(i) for i in ids])


#: A source row whose bytes Recall can actually get at right now, as a WHERE
#: clause fragment.
#:
#: Three columns decide it and they are easy to get wrong separately:
#: ``is_placeholder`` says the bytes are in the cloud, ``is_readable`` says
#: Windows would not open the file, and ``local_copy_path`` says Recall already
#: took its own copy and neither of the other two matters any more. Written once
#: here because a query that forgets the third one silently stops reading every
#: file that was downloaded - and a file nothing tries to read is a file the
#: archive is short of without ever saying so.
REACHABLE_SQL = (
    "(local_copy_path IS NOT NULL OR (is_placeholder = 0 AND is_readable = 1))"
)

#: Still in the cloud, with no copy here. What the user means by "cloud-only".
CLOUD_ONLY_SQL = "(is_placeholder = 1 AND local_copy_path IS NULL)"


def read_path(row) -> Path:
    """Where Recall opens this file.

    The copy when there is one, the original otherwise. The original path is
    still what the user is shown - they recognise their own OneDrive folder, not
    a workdir full of numbered directories.
    """
    return Path(row["local_copy_path"] or row["path"])


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def log_error(
    conn: sqlite3.Connection,
    stage: str,
    message: str,
    *,
    detail: str | None = None,
    source_file_id: int | None = None,
) -> int:
    """Record a caught exception.

    Spec section 12: every caught exception writes a row here. Silent failure is
    the worst possible outcome. This never raises - if the archive itself is
    unwritable there is nowhere left to complain to, and the caller's own error
    is the more useful one.
    """
    try:
        cur = conn.execute(
            "INSERT INTO errors(stage, source_file_id, message, detail) "
            "VALUES (?, ?, ?, ?)",
            (stage, source_file_id, message, detail),
        )
        return int(cur.lastrowid or 0)
    except sqlite3.Error:
        return 0


def table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def index_names(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        if r[0] is not None
    }


def trigger_names(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
    }


def integrity_check(conn: sqlite3.Connection) -> str:
    """SQLite's own structural check. 'ok' means the file is not corrupt."""
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return row[0] if row else "unknown"
