"""Section 9.1 - corrupted, partial and unreadable sources.

| code                   | detected                                        |
|------------------------|-------------------------------------------------|
| magic_mismatch         | extension and header signature disagree         |
| partial_parse          | store claims N items, fewer than 95% came out   |
| read_failure           | CRC or block read error mid-store               |
| needs_password         | encrypted or password-protected store           |
| orphaned_ost           | .ost with no matching Outlook profile           |
| zero_or_tiny           | zero-byte or implausibly small for its type     |
| empty_tree             | folder tree parsed but yielded zero items       |
| backend_disagreement   | pypff and Outlook return materially different counts |
| attachment_unreadable  | attachment or nested MSG could not be opened    |

The number that matters is ``estimated_loss``: the store's own header count, or
its folder counts, minus what actually came out. It is computed, never
estimated by feel, and it is left NULL when there is no honest basis for it -
"unknown" is a truthful answer and "0" would not be.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..logging_setup import get_logger
from ..models import Severity
from .engine import Finding, record_finding, resolve_absent_findings

log = get_logger("integrity.sources")

#: Below this, a store of that type cannot hold anything. A PST always carries a
#: 512-byte header plus allocation maps; a real one is never under ~256 KB.
_MIN_PLAUSIBLE: dict[str, int] = {
    ".pst": 256 * 1024,
    ".ost": 256 * 1024,
    ".dbx": 2 * 1024,
    ".mbx": 1024,
    # A zip's own end-of-central-directory record is 22 bytes, so a valid .olm
    # holding one short message really can be a few hundred bytes.
    ".olm": 256,
    ".wab": 512,
    ".pab": 512,
    ".msg": 512,
}

_TYPE_NAMES: dict[str, str] = {
    ".pst": "Outlook data file",
    ".ost": "Outlook offline data file",
    ".dbx": "Outlook Express folder",
    ".mbx": "Outlook Express 4 mailbox",
    ".olm": "Mac Outlook archive",
    ".wab": "Windows Address Book",
    ".pab": "Outlook personal address book",
    ".msg": "Outlook message",
    ".eml": "email message",
    ".mbox": "mailbox",
    ".ics": "calendar file",
    ".vcs": "calendar file",
    ".vcf": "contact card",
}


def scan_time_checks(conn, settings, candidates: list) -> int:
    """Checks that need no parsing. Run at the end of every scan."""
    n = 0
    n += _check_magic_mismatch(conn, candidates)
    n += _check_zero_or_tiny(conn, settings)
    n += _check_orphaned_ost(conn)
    n += _check_unreadable(conn)
    return n


def source_checks(conn, settings) -> int:
    """Everything in 9.1, including the checks that need a parse to have run.

    Used by ``recall audit``. Safe before any extraction: the parse-time checks
    simply find nothing to report yet.
    """
    n = 0
    n += _check_zero_or_tiny(conn, settings)
    n += _check_orphaned_ost(conn)
    n += _check_unreadable(conn)
    n += _check_partial_parse(conn, settings)
    n += _check_failed_parses(conn)
    n += _check_empty_tree(conn)
    n += _check_backend_disagreement(conn)
    n += _check_attachment_unreadable(conn)
    return n


# ---------------------------------------------------------------------------
# 9.1 checks
# ---------------------------------------------------------------------------


def _check_magic_mismatch(conn, candidates: list) -> int:
    """The name says one thing, the first bytes say another."""
    n = 0
    present: list[tuple] = []
    for c in candidates:
        if not getattr(c, "magic_mismatch", False):
            continue
        row = conn.execute(
            "SELECT id FROM source_files WHERE path = ?", (c.path,)
        ).fetchone()
        if row is None:
            continue
        sid = int(row["id"])
        present.append(("magic_mismatch", sid, -1, -1, ""))
        record_finding(
            conn,
            Finding(
                code="magic_mismatch",
                severity=Severity.HIGH,
                title=(
                    f"{Path(c.path).name} is not the kind of file its name says "
                    "it is"
                ),
                detail=(
                    f"{c.magic_detail}\n\n"
                    "The file has been kept and will still be examined, but it "
                    "may have been renamed by hand, or it may be damaged at the "
                    "start. Nothing has been changed on disk."
                ),
                source_file_id=sid,
                evidence={
                    "path": c.path,
                    "extension": c.ext,
                    "detected_type": c.detected_type,
                },
            ),
        )
        n += 1
    return n


def _check_zero_or_tiny(conn, settings) -> int:
    """Empty, or too small to hold anything of its declared type.

    "Too small" is only meaningful for the container formats in
    ``_MIN_PLAUSIBLE``, which carry headers and allocation tables and cannot be
    small. A single .vcf contact card is routinely 200 bytes and a one-event
    .ics under 400; flagging those would be crying wolf, so for every other
    format only a genuinely empty file is reported.
    """
    n = 0
    present: list[tuple] = []

    for row in conn.execute(
        "SELECT id, path, ext, size_bytes, is_placeholder FROM source_files "
        "WHERE is_placeholder = 0"
    ):
        size = row["size_bytes"] or 0
        ext = row["ext"]
        limit = _MIN_PLAUSIBLE.get(ext, 1)
        if size >= limit:
            continue

        name = Path(row["path"]).name
        type_name = _TYPE_NAMES.get(ext, f"{ext} file")
        if size == 0:
            title = f"{name} is empty (zero bytes)"
            detail = (
                f"This file contains nothing at all. A {type_name} is never "
                "zero bytes. It is most likely a leftover from a failed copy or "
                "a deleted mailbox.\n\n"
                "Nothing has been changed on disk. If you know this file should "
                "have contained something, the original may still exist in a "
                "backup."
            )
        else:
            title = f"{name} is too small to be a real {type_name}"
            detail = (
                f"The file is {size:,} bytes. A {type_name} needs at least "
                f"about {limit:,} bytes just for its own structure, so this one "
                "cannot hold any messages.\n\n"
                "It may be a truncated copy. Nothing has been changed on disk."
            )

        present.append(("zero_or_tiny", int(row["id"]), -1, -1, ""))
        record_finding(
            conn,
            Finding(
                code="zero_or_tiny",
                severity=Severity.MEDIUM,
                title=title,
                detail=detail,
                source_file_id=int(row["id"]),
                evidence={
                    "path": row["path"],
                    "size_bytes": size,
                    "minimum_plausible_bytes": limit,
                },
            ),
        )
        n += 1

    resolve_absent_findings(conn, "zero_or_tiny", present)
    return n


def _check_orphaned_ost(conn) -> int:
    """An .ost belonging to no Outlook profile on this machine.

    An OST is a cache of a server mailbox. Without its profile there is no
    account behind it, which usually means a mailbox that no longer exists -
    so it may be the only copy of that mail left.

    If the profiles cannot be read at all, nothing is reported. Guessing that a
    file is orphaned because we could not look would be exactly the behaviour
    the spec forbids.
    """
    profiles = _outlook_profile_data_files()
    if profiles is None:
        log.debug("Outlook profiles could not be read; orphaned_ost not evaluated")
        return 0

    n = 0
    present: list[tuple] = []
    for row in conn.execute(
        "SELECT id, path FROM source_files WHERE ext = '.ost'"
    ):
        path_l = str(row["path"]).lower()
        if any(path_l == p or os.path.basename(path_l) == os.path.basename(p) for p in profiles):
            continue

        name = Path(row["path"]).name
        present.append(("orphaned_ost", int(row["id"]), -1, -1, ""))
        record_finding(
            conn,
            Finding(
                code="orphaned_ost",
                severity=Severity.HIGH,
                title=f"{name} belongs to an email account this computer no longer has",
                detail=(
                    "An .ost file is a local copy of a mailbox that lives on a "
                    "mail server. This one does not match any email account set "
                    "up in Outlook on this computer.\n\n"
                    "That usually means the account was removed, or the file was "
                    "copied here from another machine. It matters because if the "
                    "server mailbox is gone, this file may be the only copy of "
                    "that mail left.\n\n"
                    "Microsoft Outlook is the only reliable way to read a "
                    "detached .ost. Recall will try it automatically.\n\n"
                    f"Outlook profiles found on this computer reference: "
                    f"{', '.join(sorted({os.path.basename(p) for p in profiles})) or 'no data files'}"
                ),
                source_file_id=int(row["id"]),
                evidence={
                    "path": row["path"],
                    "profile_data_files": sorted(profiles),
                },
            ),
        )
        n += 1

    resolve_absent_findings(conn, "orphaned_ost", present)
    return n


def _check_unreadable(conn) -> int:
    """Files the scanner could not open - usually Outlook is holding them."""
    n = 0
    present: list[tuple] = []
    for row in conn.execute(
        "SELECT id, path, lock_error, ext FROM source_files "
        "WHERE is_readable = 0 AND is_placeholder = 0"
    ):
        name = Path(row["path"]).name
        err = row["lock_error"] or "the reason was not recorded"
        locked_by_outlook = "PermissionError" in err or "being used by another" in err

        if locked_by_outlook:
            detail = (
                "Another program is holding this file open, and Windows will not "
                "let anything else read it. Outlook does this to the mailbox it "
                "is currently using.\n\n"
                "What to do: close Outlook completely, then run the search "
                "again. This file will be included.\n\n"
                f"Exact message from Windows: {err}"
            )
        else:
            detail = (
                "This file could not be opened. Until it can be, nothing in it "
                "can be added to the archive.\n\n"
                f"Exact message from Windows: {err}"
            )

        present.append(("read_failure", int(row["id"]), -1, -1, ""))
        record_finding(
            conn,
            Finding(
                code="read_failure",
                severity=Severity.CRITICAL,
                title=f"{name} could not be opened, so nothing in it has been read",
                detail=detail,
                source_file_id=int(row["id"]),
                evidence={"path": row["path"], "error": err},
            ),
        )
        n += 1

    resolve_absent_findings(conn, "read_failure", present)
    return n


def _check_partial_parse(conn, settings) -> int:
    """The store said how much it held, and less than that came out."""
    threshold = settings.integrity.partial_parse_threshold
    n = 0
    present: list[tuple] = []

    for row in conn.execute(
        "SELECT id, path, item_count, parse_state, parse_backend, parse_error "
        "FROM source_files WHERE parse_state = 'done'"
    ):
        # A file read with --sample was deliberately stopped early. Reporting
        # that as data loss would turn the "check it works in seconds" feature
        # into a wall of false alarms.
        if was_sampled(conn, int(row["id"])):
            continue

        claimed = _claimed_count(conn, int(row["id"]))
        if claimed is None or claimed <= 0:
            continue
        got = int(row["item_count"] or 0)
        if got >= claimed * threshold:
            continue

        lost = claimed - got
        name = Path(row["path"]).name
        folders = _incomplete_folders(conn, int(row["id"]))
        folder_phrase = ""
        if folders:
            named = ", ".join(folders[:4])
            more = f" and {len(folders) - 4} more" if len(folders) > 4 else ""
            folder_phrase = f" in the {named}{more} folder{'s' if len(folders) != 1 else ''}"

        present.append(("partial_parse", int(row["id"]), -1, -1, ""))
        record_finding(
            conn,
            Finding(
                code="partial_parse",
                severity=Severity.CRITICAL,
                title=(
                    f"{name}: about {lost:,} records could not be read out of "
                    f"{claimed:,}"
                ),
                detail=(
                    f"This file reports {claimed:,} records. Recall could read "
                    f"{got:,}. About {lost:,} records{folder_phrase} could not be "
                    "reached.\n\n"
                    "This is a reading failure, not an empty mailbox. The file "
                    "has not been changed. Trying the other reader "
                    "(Outlook itself, or the built-in one) sometimes recovers "
                    "more - use Retry on this row.\n\n"
                    f"Reader used: {row['parse_backend'] or 'unknown'}"
                    + (f"\nLast error: {row['parse_error']}" if row["parse_error"] else "")
                ),
                source_file_id=int(row["id"]),
                affected_count=got,
                estimated_loss=lost,
                evidence={
                    "path": row["path"],
                    "claimed_count": claimed,
                    "yielded_count": got,
                    "completeness": round(got / claimed, 4),
                    "backend": row["parse_backend"],
                    "incomplete_folders": folders,
                },
            ),
        )
        n += 1

    resolve_absent_findings(conn, "partial_parse", present)
    return n


def _check_failed_parses(conn) -> int:
    """A source that could not be read at all, and why."""
    n = 0
    present_read: list[tuple] = []
    present_pw: list[tuple] = []

    for row in conn.execute(
        "SELECT id, path, parse_error, parse_backend, ext FROM source_files "
        "WHERE parse_state = 'failed'"
    ):
        err = row["parse_error"] or "no error was recorded"
        name = Path(row["path"]).name
        claimed = _claimed_count(conn, int(row["id"]))
        needs_password = any(
            token in err.lower()
            for token in ("password", "encrypt", "crypt", "not supported: compressible")
        )

        if needs_password:
            present_pw.append(("needs_password", int(row["id"]), -1, -1, ""))
            record_finding(
                conn,
                Finding(
                    code="needs_password",
                    severity=Severity.HIGH,
                    title=f"{name} is password-protected and cannot be opened",
                    detail=(
                        "This file was locked with a password when it was made. "
                        "Recall cannot open it without that password, and it "
                        "will not try to break it.\n\n"
                        "What to do: if you remember the password, open the file "
                        "in Outlook once (File, Open, Outlook Data File), let "
                        "Outlook remember it, then use Retry on this row so "
                        "Outlook itself does the reading.\n\n"
                        f"Exact message: {err}"
                    ),
                    source_file_id=int(row["id"]),
                    estimated_loss=claimed,
                    evidence={"path": row["path"], "error": err},
                ),
            )
        else:
            present_read.append(("read_failure", int(row["id"]), -1, -1, ""))
            record_finding(
                conn,
                Finding(
                    code="read_failure",
                    severity=Severity.CRITICAL,
                    title=f"{name} could not be read, so none of it is in the archive",
                    detail=(
                        "Reading this file failed part-way through. Everything "
                        "in it is missing from the archive.\n\n"
                        "The file itself has not been changed or repaired - "
                        "Recall never writes to your files. Trying the other "
                        "reader sometimes gets further; use Retry on this row.\n\n"
                        f"Reader used: {row['parse_backend'] or 'unknown'}\n"
                        f"Exact error: {err}"
                    ),
                    source_file_id=int(row["id"]),
                    estimated_loss=claimed,
                    evidence={
                        "path": row["path"],
                        "error": err,
                        "backend": row["parse_backend"],
                        "claimed_count": claimed,
                    },
                ),
            )
        n += 1

    return n


def _check_empty_tree(conn) -> int:
    """The folder structure read fine and contained nothing.

    A real mailbox with folders and no messages is possible but unusual; far
    more often it means the message bodies could not be reached.
    """
    n = 0
    present: list[tuple] = []
    for row in conn.execute(
        """
        SELECT sf.id, sf.path, sf.parse_backend,
               (SELECT COUNT(*) FROM folders f WHERE f.source_file_id = sf.id) AS n_folders
        FROM source_files sf
        WHERE sf.parse_state = 'done' AND COALESCE(sf.item_count, 0) = 0
        """
    ):
        if int(row["n_folders"] or 0) == 0:
            continue
        name = Path(row["path"]).name
        present.append(("empty_tree", int(row["id"]), -1, -1, ""))
        record_finding(
            conn,
            Finding(
                code="empty_tree",
                severity=Severity.HIGH,
                title=f"{name} has {row['n_folders']} folders but not one message came out",
                detail=(
                    "Recall could see the folder structure inside this file - "
                    f"{row['n_folders']} folders - but could not read a single "
                    "record from any of them.\n\n"
                    "A mailbox with folders and no messages is unusual. This is "
                    "much more likely to be a reading failure than an empty "
                    "mailbox, so it is reported rather than accepted.\n\n"
                    f"Reader used: {row['parse_backend'] or 'unknown'}. "
                    "Use Retry to try the other reader."
                ),
                source_file_id=int(row["id"]),
                affected_count=0,
                evidence={"path": row["path"], "folder_count": int(row["n_folders"])},
            ),
        )
        n += 1

    resolve_absent_findings(conn, "empty_tree", present)
    return n


def _check_backend_disagreement(conn) -> int:
    """pypff and Outlook read the same file and got materially different counts."""
    n = 0
    present: list[tuple] = []
    for row in conn.execute(
        "SELECT id, path, item_count, parse_backend FROM source_files "
        "WHERE parse_state = 'done'"
    ):
        sid = int(row["id"])
        cross = _cross_check_record(conn, sid)
        if not cross:
            continue
        a, b = cross["pypff"], cross["com"]
        if a is None or b is None:
            continue
        bigger = max(a, b)
        if bigger == 0 or abs(a - b) / bigger < 0.05:
            continue

        name = Path(row["path"]).name
        better = "the built-in reader" if a > b else "Microsoft Outlook"
        present.append(("backend_disagreement", sid, -1, -1, ""))
        record_finding(
            conn,
            Finding(
                code="backend_disagreement",
                severity=Severity.HIGH,
                title=f"{name}: the two readers disagree about how much is in it",
                detail=(
                    f"The built-in reader found {a:,} records. Microsoft Outlook "
                    f"found {b:,}. They should agree.\n\n"
                    f"The larger result ({better}, {bigger:,} records) was kept. "
                    "The difference means at least one reader is missing part of "
                    "the file, so the archive may still be short of "
                    f"{abs(a - b):,} records from it.\n\n"
                    "The file has not been changed."
                ),
                source_file_id=sid,
                affected_count=bigger,
                estimated_loss=abs(a - b),
                evidence={
                    "path": row["path"],
                    "pypff_count": a,
                    "com_count": b,
                    "kept_backend": row["parse_backend"],
                },
            ),
        )
        n += 1

    resolve_absent_findings(conn, "backend_disagreement", present)
    return n


def _check_attachment_unreadable(conn) -> int:
    """Attachments whose contents could not be opened, grouped per source file."""
    n = 0
    present: list[tuple] = []
    rows = conn.execute(
        """
        SELECT isrc.source_file_id AS sid, sf.path AS path, COUNT(*) AS n
        FROM attachments a
        JOIN item_sources isrc ON isrc.item_id = a.item_id
        JOIN source_files sf ON sf.id = isrc.source_file_id
        WHERE a.extract_state = 'failed'
        GROUP BY isrc.source_file_id, sf.path
        """
    ).fetchall()

    for row in rows:
        sid = int(row["sid"])
        count = int(row["n"])
        name = Path(row["path"]).name
        present.append(("attachment_unreadable", sid, -1, -1, ""))
        record_finding(
            conn,
            Finding(
                code="attachment_unreadable",
                severity=Severity.MEDIUM,
                title=f"{name}: {count:,} attachment{'s' if count != 1 else ''} could not be opened",
                detail=(
                    f"{count:,} attachment{'s were' if count != 1 else ' was'} "
                    "found but could not be read out of the message.\n\n"
                    "The message text is in the archive; the attachment contents "
                    "are not, and they cannot be searched. Damaged or unusually "
                    "encoded attachments are the usual cause."
                ),
                source_file_id=sid,
                affected_count=count,
                evidence={"path": row["path"], "failed_attachments": count},
            ),
        )
        n += 1

    resolve_absent_findings(conn, "attachment_unreadable", present)
    return n


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _claimed_count(conn, source_file_id: int) -> int | None:
    """What the store itself says it holds, recorded at parse time.

    Returns None when no claim was recorded. None means "we do not know", and
    every caller treats it that way rather than substituting zero.
    """
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?",
        (f"claimed_count:{source_file_id}",),
    ).fetchone()
    if row is None:
        return None
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return None


def record_claimed_count(conn, source_file_id: int, claimed: int) -> None:
    """Remember what a store said about itself, so loss can be computed later."""
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (f"claimed_count:{source_file_id}", str(int(claimed))),
    )


def record_sampled(conn, source_file_id: int, sample_limit: int) -> None:
    """Remember that a file was read only in part, and on purpose.

    Without this, ``recall extract --sample 50`` would report every large
    mailbox as catastrophically incomplete, which is both wrong and the fastest
    possible way to teach the user to ignore the Problems screen.
    """
    key = f"sampled:{source_file_id}"
    if sample_limit:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(int(sample_limit))),
        )
    else:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))


def was_sampled(conn, source_file_id: int) -> bool:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (f"sampled:{source_file_id}",)
    ).fetchone()
    return row is not None


def record_cross_check(
    conn, source_file_id: int, pypff_count: int | None, com_count: int | None
) -> None:
    """Remember what each reader made of the same file."""
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (
            f"cross_check:{source_file_id}",
            json.dumps({"pypff": pypff_count, "com": com_count}),
        ),
    )


def _cross_check_record(conn, source_file_id: int) -> dict | None:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (f"cross_check:{source_file_id}",)
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return None


def record_folder_claims(conn, source_file_id: int, folder_counts: dict) -> None:
    """Per-folder claimed vs yielded, so loss can be named by folder."""
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (
            f"folder_claims:{source_file_id}",
            json.dumps({k: list(v) for k, v in folder_counts.items()}),
        ),
    )


def _incomplete_folders(conn, source_file_id: int) -> list[str]:
    """Folder names whose own counts were not met - the 'which folders' answer."""
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?",
        (f"folder_claims:{source_file_id}",),
    ).fetchone()
    if row is None:
        return []
    try:
        claims = json.loads(row["value"])
    except (TypeError, ValueError):
        return []

    short: list[tuple[str, int]] = []
    for folder, pair in claims.items():
        try:
            claimed, got = int(pair[0]), int(pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        if claimed > got:
            short.append((folder.rsplit("/", 1)[-1], claimed - got))
    short.sort(key=lambda t: t[1], reverse=True)
    return [name for name, _ in short]


def _outlook_profile_data_files() -> set[str] | None:
    """Data files referenced by Outlook profiles in the registry, lowercased.

    Returns None when the registry cannot be read at all - which is different
    from "there are no profiles", and is treated as such by the caller.
    """
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return None

    found: set[str] = set()
    read_anything = False
    roots = [
        r"Software\Microsoft\Office\16.0\Outlook\Profiles",
        r"Software\Microsoft\Office\15.0\Outlook\Profiles",
        r"Software\Microsoft\Windows NT\CurrentVersion\Windows Messaging Subsystem\Profiles",
    ]

    for root in roots:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, root) as key:
                read_anything = True
                _collect_paths(winreg, key, found, depth=0)
        except OSError:
            continue

    if not read_anything:
        return None
    return found


def _collect_paths(winreg, key, found: set[str], depth: int) -> None:
    """Walk a profile subtree looking for anything that names a .ost or .pst."""
    if depth > 4:
        return
    try:
        n_sub, n_val, _ = winreg.QueryInfoKey(key)
    except OSError:
        return

    for i in range(n_val):
        try:
            _, value, _ = winreg.EnumValue(key, i)
        except OSError:
            continue
        text = _as_text(value)
        if not text:
            continue
        lowered = text.lower()
        for ext in (".ost", ".pst"):
            idx = lowered.find(ext)
            if idx == -1:
                continue
            start = max(
                lowered.rfind("\x00", 0, idx) + 1,
                lowered.rfind("\\\\?\\", 0, idx),
                0,
            )
            candidate = text[start : idx + 4].strip("\x00").strip()
            if candidate:
                found.add(candidate.lower())

    for i in range(n_sub):
        try:
            name = winreg.EnumKey(key, i)
            with winreg.OpenKey(key, name) as sub:
                _collect_paths(winreg, sub, found, depth + 1)
        except OSError:
            continue


def _as_text(value) -> str | None:
    """Registry values arrive as str, bytes (often UTF-16) or numbers."""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        for encoding in ("utf-16-le", "mbcs", "latin-1"):
            try:
                return value.decode(encoding, errors="ignore")
            except (LookupError, UnicodeDecodeError):
                continue
    return None
