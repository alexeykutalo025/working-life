# BUILD SPEC — Outlook Archive Extractor ("Recall")

You are building a complete, working, offline desktop application in one pass. Read this entire
document before writing any code. Build it in phase order (0 → 3), verify each phase actually runs
before starting the next, and commit to git after each phase.

Do not stop to ask permission between phases. Do not leave `TODO`, `pass`, or stub functions
anywhere in the final code — every function either works or raises a clear, specific exception.

---

## 1. WHAT THIS IS

A local program that finds every Outlook-related data file on a Windows machine (including OneDrive
folders), extracts the mail, calendar, contacts and attachments out of them, de-duplicates and
organizes everything into one searchable archive, and presents it through a local web UI.

**The user is a 72-year-old non-programmer** with roughly 50 years of accumulated business
correspondence. He is building a searchable record of a long career. He will run this himself, on
his own machine, repeatedly, over months. Optimize every decision for: *it works unattended, it
never loses data, it never silently fails, and the screens are legible and plainly worded.*

**Absolute constraints:**

- Runs 100% offline. No cloud services, no telemetry, no API calls, no CDN links at runtime.
  Vendor every front-end asset locally.
- **Source files are read-only. Never modify, move, rename or delete anything the scanner finds.**
  All output goes to a separate working directory.
- Never write anything into a OneDrive-synced folder.
- Every long-running operation is resumable, cancelable, idempotent, and shows honest progress.
- Re-running any step must never create duplicates.
- **Flag problems; never guess past them.** Corrupted files, missing date ranges, duplicate
  accounts and unreadable records are surfaced to the user as named, quantified findings — never
  patched over, inferred around, or silently dropped. This is a headline feature of the program.
  Section 9 specifies it in full and it is not optional.

---

## 2. TECH STACK (pinned — do not substitute)

- **Python 3.11+**
- **FastAPI** + **uvicorn** — local server, binds `127.0.0.1` only, never `0.0.0.0`
- **SQLite** via stdlib `sqlite3`, WAL mode, **FTS5** for full-text search
- **Typer** for the CLI
- **Front end: plain HTML + CSS + vanilla JavaScript.** No React, no npm, no build step, no
  bundler. A single-page app with hash routing and `fetch()`. Charts are hand-rolled inline SVG —
  do not add a charting library.
- **pydantic** v2 for config and API models
- **pytest** for tests

### Parsing libraries

| Format | Primary | Notes |
|---|---|---|
| `.pst` (ANSI + Unicode) | `libpff-python` (pypff) | Both variants. Fast path. |
| `.ost` | pypff, **fallback to Outlook COM** | pypff is unreliable on Outlook 2013+ OST |
| `.msg` | `extract-msg` | Well maintained, pure Python |
| `.eml`, `.mbox` | stdlib `email`, `mailbox` | |
| `.ics` | `icalendar` | iCalendar 2.0 |
| `.vcs` | `vobject` | vCalendar 1.0 — older, different spec |
| `.vcf` | `vobject` | Contacts |
| `.olm` | stdlib `zipfile` + `xml.etree` | Mac Outlook = zip of XML. Easy win. |
| `.dbx` | **implement in-project** | Outlook Express. No maintained Python lib exists. |
| `.mbx` | **implement in-project** | Outlook Express 4 / Eudora |
| `.wab` | best-effort, may skip | Windows Address Book. Log clearly if unsupported. |
| `.pab` | Outlook COM only | Legacy address book. Best-effort. |

Also support: `charset-normalizer` (essential — 1990s files are CP1252, Latin-1, and mojibake),
`python-docx`, `openpyxl`, `pypdf` for attachment text extraction. OCR (`pytesseract`) is optional
and off by default.

### The dual-backend rule for PST/OST

Build `parsers/pst_backend.py` with **two interchangeable backends behind one interface**:

1. `PyPffBackend` — default. Fast, no Outlook required.
2. `OutlookComBackend` — uses `pywin32` COM automation
   (`win32com.client.Dispatch("Outlook.Application")`, `Session.AddStore(path)`, walk
   `Folders`/`Items`, then `RemoveStore`). Slower but authoritative — Outlook itself does the
   parsing, so it handles modern OST, ANSI quirks, and password-prompted stores.

Automatic selection: try pypff; on failure, or when the file is `.ost` and pypff yields zero items,
fall back to COM if Outlook is installed. Record which backend produced each item in the database.
If both fail, mark the source `failed` with the exact error and move on — **never abort the run.**

---

## 3. PROJECT LAYOUT

```
recall/
├── README.md
├── requirements.txt
├── config.toml                  # user-editable settings
├── setup.bat                    # creates venv, installs deps, runs doctor
├── start.bat                    # activates venv, launches server, opens browser
├── recall/
│   ├── __init__.py
│   ├── cli.py                   # Typer: scan, extract, index, serve, export, doctor, reset
│   ├── config.py
│   ├── db.py                    # connection, migrations, schema DDL
│   ├── models.py
│   ├── scan/
│   │   ├── walker.py            # filesystem discovery
│   │   ├── onedrive.py          # placeholder detection & hydration
│   │   └── fingerprint.py       # hashing, duplicate file detection
│   ├── parsers/
│   │   ├── base.py              # Parser ABC → yields normalized records
│   │   ├── pst_backend.py       # pypff + Outlook COM
│   │   ├── msg.py  eml.py  ics.py  vcs.py  olm.py  dbx.py  mbx.py  wab.py
│   ├── normalize/
│   │   ├── dedup.py             # dedup key computation
│   │   ├── people.py            # identity resolution & merge suggestions
│   │   ├── threads.py           # conversation reconstruction
│   │   ├── text.py              # encoding repair, HTML→text, signature stripping
│   │   └── attachments.py       # blob store + text extraction
│   ├── search/indexer.py        # builds search_docs + FTS5
│   ├── export/                  # csv, xlsx, markdown, json
│   ├── api/                     # FastAPI routers
│   └── web/                     # static/ (css, js, vendored assets), templates/
├── tests/
│   ├── fixtures/                # synthetic test files, generated
│   └── test_*.py
└── workdir/                     # created at runtime, gitignored
    ├── archive.db
    ├── blobs/                   # content-addressed attachments
    ├── logs/
    └── exports/
```

---

## 4. DATABASE SCHEMA

Implement exactly this. It is the heart of the project — one unified `items` table with a `kind`
column, because the entire purpose is a single chronological spine.

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

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
```

Include triggers (or an explicit reindex step) keeping `items_fts` in sync with `search_docs`.
Provide `recall index --rebuild` to regenerate from scratch.

---

## 5. PHASE 0 — INVENTORY (build first, must work standalone)

**Goal: know what exists before parsing anything.** No parsing in this phase. It should complete in
minutes, not hours.

- Recursively walk configured roots (default: all fixed drives + detected OneDrive folders +
  `%LOCALAPPDATA%\Microsoft\Outlook`). Honor an exclusion list (`Windows\`, `Program Files\`,
  `node_modules`, `$Recycle.Bin`, `AppData\Local\Temp`).
- Match by extension **and** magic-byte signature (`!BDN` for PST/OST, `\xD0\xCF\x11\xE0` OLE for
  MSG, `JMF9` family for DBX) so misnamed files are caught.
- Record path, size, timestamps, container type.

**OneDrive placeholder handling — this matters.** On Windows read
`os.stat(path).st_file_attributes` and test these bits:

```python
FILE_ATTRIBUTE_OFFLINE                = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_OPEN         = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS  = 0x00400000
```

If any is set, the file's bytes are **not on disk** — it is a cloud placeholder. Record
`is_placeholder=1`, do **not** hash it, do **not** open it. Reading it would trigger a download.
Hydration happens only when the user explicitly opts in on the Sources screen, one file at a time
or in a batch, with a clear warning of total download size.

- Hash only real local files, streaming, BLAKE2b-256, with a progress callback. Skip hashing files
  over a configurable size cap on the first pass; hash on demand.
- Detect identical-content duplicate files (same hash) and set `duplicate_of`.
- Detect locked files (Outlook holds `.ost`/`.pst` open): catch `PermissionError`, set
  `is_readable=0` and `lock_error`, surface a plain-language message: *"Close Outlook and scan
  again to include this file."*

**Phase 0 output:** a populated `source_files` table and a Sources screen listing everything found,
sorted by size, with type, date range of the file, placeholder flag, duplicate flag, and a checkbox
per row. Plus a one-page summary: *"Found 41 Outlook files totaling 84 GB. 12 are duplicates.
7 are cloud-only. 2 are locked by Outlook."*

---

## 6. PHASE 1 — CALENDAR

Parse only calendar data from selected sources: appointments from PST/OST, plus all `.ics` and
`.vcs` files.

- Extract: subject, start, end, all-day flag, timezone, location, body, organizer, attendees with
  response status, recurrence rule, categories, busy status, meeting status, folder path.
- **Store `occurred_utc` in UTC, always, plus the original timezone string and local time.**
  Never guess a timezone — if absent, record it as unknown rather than assuming.
- Recurrence: store the RRULE in `recurrence_json`. Store the master; expand instances on demand in
  the UI, not into the database.
- Dedup key for events: `sha256(ical_uid + recurrence_id + start_utc)`; when UID is missing, fall
  back to `sha256(normalized_subject + start_utc + normalized_organizer)`.

**Phase 1 acceptance:** a working Timeline screen and a CSV export of all events with columns
`date, start, end, duration_min, subject, location, organizer, attendee_count, attendees, category,
source_file`. This export alone must be usable in Excel without further cleanup.

---

## 7. PHASE 2 — CONTACTS AND PEOPLE

Parse contacts from PST/OST, `.vcf`, `.wab`, `.pab`. Then build the identity layer across
*everything* extracted so far.

**Identity resolution** — auto-merge only on high confidence:

- Same normalized SMTP address → same person. Always.
- Resolve Exchange legacyDN (`/o=.../cn=Recipients/cn=...`) to SMTP where the source provides it.
- Strip `+tags`, lowercase, normalize dots for known providers.
- Propose (never auto-apply) merges when: identical display name across different addresses;
  same surname + same domain; high Jaro-Winkler similarity on display name plus any shared
  correspondent.
- Every proposal appears in a **merge review queue** in the UI with the evidence shown, and requires
  a click to confirm. Merging is reversible — set `merged_into`, never delete a `people` row.

Mark the user's own identities `is_self=1` (configured in `config.toml` as a list of his addresses
across the decades).

**Phase 2 acceptance:** People screen listing every person with first contact, last contact, item
count, and organization. Clicking a person shows their timeline and top co-correspondents.

---

## 8. PHASE 3 — EMAIL AND ATTACHMENTS

The big one. Volume may reach hundreds of thousands of messages.

- Walk every folder in each store, preserving the folder tree in `folders`.
- Extract headers, plain and HTML bodies, RTF where that's all there is (decompress and convert).
- **Encoding repair is mandatory.** Old `.dbx`/`.mbx`/ANSI-PST content is CP1252, Latin-1, or
  already-mangled UTF-8. Use `charset-normalizer` with the declared charset as a hint, and fix
  common mojibake (`â€™` → `'`). Store a `parse_confidence` below 1.0 when encoding was guessed.
- Convert HTML to readable plain text for `body_text` (keep the HTML too).
- **Dedup key for mail:** `sha256(internet_message_id)` when present. Otherwise
  `sha256(normalized_from + sorted_normalized_recipients + occurred_utc_to_minute +
  normalized_subject + body_text[:2000])`. On collision, insert the `item_sources` row and skip the
  duplicate item — **keep every source pointer.**
- **Thread reconstruction:** primary `In-Reply-To` / `References` chains; fallback to normalized
  subject (strip `RE:`, `FW:`, `FWD:`, bracketed list tags) plus participant overlap plus a time
  window.
- **Attachments:** write to a content-addressed blob store (`blobs/ab/cd/<hash>.pdf`) so identical
  attachments across thousands of messages are stored once. Extract text from PDF, DOCX, XLSX,
  PPTX, TXT, and nested MSG for search. Skip extraction over a configurable size cap. Inline images
  are stored but not text-extracted.

**Throughput requirements:** batch inserts in transactions of 1,000 rows. Commit progress to
`source_files.parse_state` after each source so `--resume` works. Target 500+ items/sec through
pypff. The job must survive a crash and pick up where it left off.

---

## 9. DATA INTEGRITY — FLAG, NEVER GUESS

**This is a first-class feature of the program, not error handling.**

The archive is a witness, not a storyteller. When the program cannot read a file, cannot date a
record, or cannot tell two people apart, it says so — on screen, in plain language, attached to the
records affected. It never interpolates a missing date, never assumes a timezone, never silently
merges two identities, never draws a chart line through a period it has no data for, and never
reports a total it cannot substantiate.

A known gap, honestly labeled, is worth more than a clean-looking archive that quietly lost 1998.

Implement `recall/integrity/` with one module per check family below. Every check writes rows to
`findings`. Checks run automatically at the end of every scan and every extraction, and on demand
via `recall audit`.

### 9.1 Corrupted, partial, and unreadable sources

| Code | Detect | Severity |
|---|---|---|
| `magic_mismatch` | Extension and header signature disagree | high |
| `partial_parse` | Store header reports N items; fewer than 95% were yielded | **critical** |
| `read_failure` | CRC or block read error mid-store | **critical** |
| `needs_password` | Encrypted or password-protected store | high |
| `orphaned_ost` | `.ost` with no matching Outlook profile | high |
| `zero_or_tiny` | Zero-byte, or implausibly small for its type | medium |
| `empty_tree` | Folder tree parsed but yielded zero items | high |
| `backend_disagreement` | pypff and Outlook COM return materially different counts | high |
| `attachment_unreadable` | Attachment or nested MSG could not be opened | medium |

For every one of these, record: the exact library exception, the **last successfully read folder
path and item offset**, which backend was used, and — most importantly — `estimated_loss`.

`estimated_loss` is the number that matters to the user. Compute it from the store's own header
count, or from folder-level counts, minus what was actually extracted. Report it as:
*"This file reports 12,400 messages. We could read 4,380. About 8,000 messages in the Clients and
Archive folders could not be reached."*

Never "repair" a source file. Never write to it. If a retry with the other backend recovers more,
record both attempts and keep the better result, leaving the finding in place with a note.

### 9.2 Coverage gaps — missing date ranges

After every extraction, rebuild `coverage_months`: for every month from the earliest item to the
latest, per kind, count items and distinct contributing sources. Then classify:

- **`hard_gap`** — a month with zero items inside an otherwise-populated span.
- **`soft_gap`** — a month whose count falls below 15% of the trailing 12-month median.
- **`source_contradiction`** — *the important one.* A source file whose own metadata, folder names,
  or header date range claims coverage of a period, but from which no items in that period were
  extracted. **This is a parse failure, not a life event.** Severity high. Say so plainly:
  *"archive1998.pst appears to cover 1996–2004, but nothing from 2001 came out of it."*
- **`edge_truncation`** — the archive's earliest item is implausibly late relative to the oldest
  source file's own creation date.

**Rendering rule:** the Timeline marks gap months with visible hatching labeled "no data." It must
never interpolate, smooth, average, or connect a line across a gap. An empty month looks empty.

The user can annotate any gap as **explained** — *"I wasn't using email yet"*, *"this was the
Contract Marketing server we lost in the move."* The annotation is stored on the finding and in
`coverage_months.explained_by_user`. An explained gap stops appearing in the health banner but
stays visible on the timeline forever. Explaining a gap never deletes it.

### 9.3 Duplicate and colliding accounts

| Code | Detect | Action |
|---|---|---|
| `under_merged` | Two `people` rows that are probably one person | Propose in merge queue with evidence; never auto-apply |
| `over_merged_risk` | One identity that is probably several humans — role mailboxes (`info@`, `office@`, `admin@`, `sales@`, `noreply@`), generic display names ("Administrator", "Support"), or one SMTP address carrying more than 8 distinct display names | Flag. **Never auto-split. Never let it appear as a "top correspondent" without the warning attached.** |
| `duplicate_account_store` | Two source files that are the same mailbox — matching primary SMTP in store properties, or >90% item overlap by `dedup_key` | Report which file is the superset, with the overlap percentage and the item counts |
| `self_identity_unclaimed` | An address sending more than 2% of all items that is not in the configured `is_self` list | Ask: *"Is tmccarthy@contractmktg.com one of your addresses?"* |
| `ambiguous_legacydn` | An Exchange legacyDN that never resolved to an SMTP address | Keep the raw DN visible; do not fabricate an address |
| `identity_conflict` | Two `is_self` addresses appearing as sender and recipient of the same message | Flag — usually a store from a different account than assumed |

### 9.4 Record-level data quality

- **`no_date`** — no usable timestamp. Route these to a visible **"Undated"** bucket in the UI.
  Never assign epoch zero, never assign the parse date, never sort them in as if dated.
- **`implausible_date`** — before 1970 or in the future. Flag, keep the raw value verbatim, do not
  correct it.
- **`unknown_timezone`** — count per source. This directly distorts travel and trip analysis, so
  it must be visible wherever times are shown.
- **`low_confidence_text`** — `parse_confidence < 1.0` because an encoding was guessed. Count per
  source; mark the affected items in the viewer.
- **`orphan_reply`** — a message whose `In-Reply-To` points at something not in the archive. A
  cluster of these is evidence of a missing source file, and should be reported that way.
- **`missing_blob`** — an attachment row whose blob file is absent from disk.
- **`unresolved_recurrence`** — an RRULE that could not be parsed. Show the master, do not invent
  instances.

### 9.5 Severity, state, and the honest-count rule

Severities: `critical` (data certainly lost) · `high` (data probably lost or wrong) · `medium`
(uncertainty recorded) · `info`.

States: `open` → `acknowledged` | `explained` | `resolved` | `wont_fix`, each with a user note and
timestamp. **Nothing auto-resolves.** A finding closes only when a later run finds the condition
genuinely gone, and the row is kept with `resolved_utc` set, never deleted.

**The honest-count rule.** Anywhere the interface shows a count, total, chart, or export, if any
open finding affects that number it is displayed with a marker and an explanation in reach:

> **12,481 messages** ⚠ *about 8,000 more could not be read from 2 files*

Never round a qualified number into a clean one. Never present a partial count as complete. Never
let a chart imply data where there is none.

### 9.6 Where findings surface

- **Home** — a health banner with counts by severity. Always visible, never behind a menu.
- **Problems screen** — the full queue (section 10, screen 7).
- **Timeline** — gaps hatched inline.
- **Sources** — per-file findings on the row, with estimated loss.
- **Item viewer** — a warning strip on any item carrying a finding.
- **People** — the merge queue and `over_merged_risk` warnings.
- **Every export** ships a companion integrity statement: `<name>_integrity.txt` for CSV/Markdown,
  a dedicated `Integrity` sheet for XLSX, a `findings` key for JSON. It names exactly what was
  uncertain or missing in *that exported set*. **An export that silently omits known problems is a
  defect, not a nicety.**
- `recall audit` runs every check and prints a severity-grouped report;
  `recall audit --report health.md` writes it out.

---

## 10. THE USER INTERFACE

Seven screens plus export. Plain language everywhere — the buttons say **"Find Outlook files on this
computer"**, not "Run discovery."

**Readability requirements (non-negotiable):** base font size 17px, body text at least 16px, line
height 1.6, minimum contrast ratio 7:1, generous click targets (44px minimum), no icon-only
controls without a text label, no information conveyed by color alone. Light theme by default with
a dark toggle. Works down to 1280px wide; no horizontal scroll.

1. **Home** — archive at a glance: total items by kind (subject to the honest-count rule in 9.5),
   date coverage span, sources parsed vs. pending, storage used, and three big buttons for the next
   sensible action. Across the top, the **health banner**: open findings by severity, in plain
   language — *"2 files could not be fully read · 3 unexplained gaps · 6 people to confirm"* — each
   linking into the Problems screen. The banner is never hidden, collapsed, or dismissible.
2. **Sources** — the inventory table from Phase 0. Sort, filter, select, hydrate placeholders,
   start extraction, watch live progress (items/sec, elapsed, estimated remaining, current file),
   cancel, resume. Failed sources show the real error plus a plain-language explanation.
3. **Search** — one large search box (FTS5, supporting quoted phrases, `AND`/`OR`/`NOT`, `NEAR`).
   Left sidebar filters: kind, date range, person, folder, has-attachments, source file, tag.
   Results as a list with highlighted snippets. Keyboard: `/` focuses search, `↑`/`↓` moves through
   results, `Enter` opens.
4. **Timeline** — hand-drawn inline SVG. Year bars across the full span; click a year to expand to
   months, a month to days. Stacked by kind. User-defined eras shaded behind the bars. Clicking any
   bar filters Search to that period.
5. **People** — sortable list; profile page per person showing every identity, first and last
   contact, item count over time as a sparkline, top co-correspondents, and their full item
   timeline. The merge review queue lives here.
6. **Item viewer** — full message or event, participants as clickable person links, attachment list
   with download and inline preview, the thread it belongs to, raw headers behind a disclosure, and
   a **provenance panel**: *"This item was found in 4 files: …"* Any finding touching this item
   shows as a warning strip above the content — guessed encoding, unknown timezone, missing date.
7. **Problems** — the integrity queue from section 9, grouped by severity and then by class
   (unreadable files · coverage gaps · duplicate accounts · record quality). Each row states in one
   plain sentence what is wrong, what it affects, and how many records are involved, with the raw
   technical detail behind a disclosure for when you send it to someone for help. Per-row actions:
   acknowledge, explain (free-text note — required for gaps), retry with the other backend, mark
   won't-fix, jump to the affected source/person/period. A **coverage map** at the top of the
   screen: one row per year, one cell per month, shaded by volume, hatched where there is no data.
   That map is the single most important picture in the application — it is the honest answer to
   *"what do I actually have?"*

**Export** — from any search result set or filter: CSV, XLSX, Markdown (one file per item or one
combined chronological document), and JSON. Attachments optionally copied out to a dated folder.

---

## 11. CLI

```
recall doctor            # environment check: Python, each library, Outlook COM availability,
                         # disk space, write permissions. Prints a pass/fail table.
recall scan [--roots ...] [--full-hash]
recall extract [--kinds calendar,contacts,mail] [--sources ID,ID] [--sample 50] [--resume]
recall index [--rebuild]
recall people suggest | merge
recall audit [--checks corrupt,gaps,accounts,quality] [--report PATH]
                         # runs every integrity check, prints a severity-grouped report,
                         # exits non-zero if any critical finding is open
recall findings list [--severity ...] [--state open] | explain <id> "<note>" | retry <id>
recall export <format> [--query ...] [--out PATH]
recall serve [--port 8765] [--open]
recall reset --items | --all       # requires typed confirmation
```

`--sample N` parses only the first N items per source. **This is important:** it lets the user
validate the pipeline against real files in seconds before committing to an overnight run.

---

## 12. RELIABILITY REQUIREMENTS

- Structured logging to `workdir/logs/recall-YYYYMMDD.log` at DEBUG, console at INFO.
- **Every caught exception writes a row to the `errors` table.** Silent failure is the worst
  possible outcome — a missing decade must be visible, not invisible.
- One corrupt source file never halts a run. Log it, mark it failed, continue.
- Graceful `Ctrl-C`: finish the current transaction, mark state, exit cleanly.
- A crash mid-extraction followed by `--resume` must produce exactly the same final database as an
  uninterrupted run. Test this.
- Nightly-safe: the whole extraction can run for 12 hours unattended without user input.

---

## 13. TESTING

You will not have real PST files. Handle it this way:

- **Write a fixture generator** (`tests/fixtures/generate.py`) producing synthetic `.eml`, `.mbox`,
  `.ics`, `.vcs`, `.vcf`, `.olm` files, including deliberately nasty cases: CP1252 bytes, mojibake,
  missing timezones, missing Message-ID, recurring events, 200-recipient threads, the same message
  duplicated across three "sources", attachments with identical content and different names,
  filenames with Unicode, and an empty/zero-byte file.
- Unit tests for every parser, the dedup key functions, encoding repair, identity resolution,
  thread reconstruction, and the OneDrive attribute check (mocked `st_file_attributes`).
- Integration test: full pipeline over the fixture set → assert exact item counts, exact duplicate
  collapse, correct people merge, working FTS query, correct CSV export.
- For PST/OST/DBX/MBX: unit-test the structural readers against hand-built byte fixtures, and gate
  real-file integration tests behind `tests/fixtures/real/` existing (skip if absent).
- **Deliberate-damage tests for the integrity engine.** Generate fixtures that are broken on
  purpose and assert the right finding appears with the right severity and a correct
  `estimated_loss`: a truncated store, a file whose extension lies about its contents, a mailbox
  with every February 2003 message removed (must produce `hard_gap`), a source whose folder names
  claim 2001 while containing nothing from 2001 (must produce `source_contradiction`), one SMTP
  address carrying twelve different display names (must produce `over_merged_risk`), two stores
  where one is a strict subset of the other (must produce `duplicate_account_store` naming the
  superset), messages with no date and with a 1961 date, and an attachment row whose blob has been
  deleted. **Assert equally that clean fixtures produce zero findings** — a system that cries wolf
  is as useless as one that stays silent.
- Assert the honest-count rule: a query whose range overlaps a `partial_parse` source must return
  its total flagged, never bare.
- Every test must pass before you declare a phase complete. Run them.

---

## 14. DO NOT

- Do not modify, move, rename, or delete any file found by the scanner.
- Do not write into OneDrive folders.
- Do not download OneDrive placeholders without explicit user opt-in.
- Do not make any network request at runtime. No CDN `<script src>` — vendor it.
- Do not add npm, a bundler, or a JS framework.
- Do not assume a timezone when one is missing.
- Do not drop data you cannot parse — store the raw bytes or raw headers and flag low confidence.
- Do not leave stubs, `TODO`s, mock data, or "in a real implementation this would…" comments.
- Do not bind the server to anything but `127.0.0.1`.
- **Do not guess past a problem.** Specifically:
  - Do not interpolate, infer, or back-fill a missing date, timezone, sender, or recipient.
  - Do not auto-merge two people, or auto-split one, without the evidence bar in section 9.3.
  - Do not smooth, average, or draw a line across a coverage gap in any chart.
  - Do not present a count as complete when an open finding affects it.
  - Do not auto-resolve, auto-dismiss, hide, or downgrade a finding.
  - Do not delete a finding, ever — resolve it and keep the row.
  - Do not let an export leave without its integrity statement.
  - Do not attempt to repair a corrupt source file in place.

---

## 15. DELIVERABLES — DEFINITION OF DONE

1. `setup.bat` creates the venv and installs everything on a clean Windows machine.
2. `recall doctor` prints a clean pass/fail table.
3. `start.bat` launches the server and opens the browser to a working Home screen.
4. Phase 0 scan completes on a real drive and populates the Sources screen.
5. `recall extract --sample 50` works against real files.
6. All seven screens function with real extracted data.
7. Search returns correct results with snippets. Timeline drills down. Merge queue works.
8. CSV and XLSX exports open cleanly in Excel, each with its integrity statement attached.
9. `recall audit` runs every check and produces a readable health report.
10. The Problems screen shows real findings with real estimated-loss numbers; the coverage map
    renders the full span with gaps visibly hatched and explainable.
11. Full test suite passes, including the deliberate-damage integrity tests.
12. `README.md` written **for a non-programmer**: what to install, what to click, what each screen
    does, **how to read the Problems screen and what each finding means**, what to do when
    something fails, and where the data lives.

---

## 16. BUILD ORDER

Work in this sequence and verify each step runs before moving on:

1. Scaffold, config, `db.py` with full schema, `doctor`, `setup.bat` — verify the DB creates.
2. Phase 0 scanner + Sources API + Sources screen — verify against a real folder.
3. Parser framework + `.ics`/`.vcs` + PST calendar extraction — verify Phase 1 acceptance.
4. Timeline screen + calendar CSV export.
5. Contacts parsers + identity resolution + People screen + merge queue.
6. Mail extraction + threading + attachments + blob store.
7. **Integrity engine** (section 9) + coverage census + Problems screen + coverage map + health
   banner + `recall audit`. Build this before search — the integrity picture is what tells the
   user whether the archive is trustworthy, and it should exist before the archive looks polished.
8. Search indexer + Search screen + Item viewer.
9. Exports with integrity statements, Home dashboard, README, final test pass.

Commit after each numbered step with a clear message. When you finish, print a summary of what was
built, what was tested, what is best-effort (`.wab`, `.pab`, OCR), and the exact commands the user
should run first.

Begin now. Build all of it.
