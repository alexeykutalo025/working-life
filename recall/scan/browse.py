"""Walking the folder tree so the user can point at one folder.

The drive chooser searches whole drives, which is right when you do not know
where anything is. Someone who *does* know - "it is all in D:\\Archive\\Mail" -
should not have to search 300 GB to reach one folder, and should not have to
open a command prompt either.

It lists folders and files both, because somebody who knows their mail is in
``Outlook.pst`` should be able to point at exactly that and nothing else. A
file Recall has no reader for is still listed, marked as unreadable rather than
hidden: leaving it out would have the user hunting for a file that is sitting
right in front of them.

Two rules carry over from the rest of Recall:

* A folder that cannot be read is an answer, not a crash. It comes back with
  ``readable`` false and a sentence saying why, exactly as an unreadable file
  does on the Sources screen.
* Nothing here opens, changes or even stats a file. Only directories are
  touched, and only to learn their names.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings, fixed_drives
from ..db import get_setting, set_setting
from ..logging_setup import get_logger

log = get_logger("scan.browse")

#: Where the recently-chosen folders live. The ``settings`` table is a
#: key-value store that already exists, so remembering these needs no schema
#: change - which matters, because ``migrate()`` has no upgrade steps yet and a
#: new table would never reach an archive that already exists.
RECENT_KEY = "recent_scan_folders"

#: Enough to be useful, few enough to stay a list rather than a history.
RECENT_LIMIT = 8

UNREADABLE = (
    "Windows will not let Recall look inside this folder. That is usually a "
    "permission set by Windows itself. You can still pick a folder inside it "
    "if you know the way, or choose somewhere else."
)

GONE = "That folder is not on this computer any more."

NOT_A_FOLDER = "That is a file, not a folder. Pick the folder it sits in."


@dataclass
class Entry:
    """One folder or file offered to the user."""

    name: str
    path: str
    excluded_by_default: bool = False
    #: Last changed, for the Details view. None when Windows will not say -
    #: which is an answer, and better than showing a date that is not real.
    modified: str | None = None
    #: "folder" or "file".
    kind: str = "folder"
    #: Bytes, for a file. Folders do not have a size worth showing: working one
    #: out means walking the whole thing, which is the expensive operation this
    #: chooser exists to help the user avoid.
    size: int | None = None
    #: Lower-case extension, for a file.
    ext: str | None = None
    #: Can Recall actually read this kind of file? A file it cannot read is
    #: still listed - hiding it would leave the user hunting for something that
    #: is in front of them - but it cannot be chosen, and it says why.
    readable_kind: bool = True


@dataclass
class Listing:
    """What is inside one folder, or the list of drives at the top."""

    path: str | None                      # None at the top, where drives live
    label: str
    parent: str | None
    readable: bool = True
    note: str | None = None
    entries: list[Entry] = field(default_factory=list)
    crumbs: list[Entry] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "label": self.label,
            "parent": self.parent,
            "readable": self.readable,
            "note": self.note,
            "entries": [vars(e) for e in self.entries],
            "crumbs": [vars(e) for e in self.crumbs],
        }


def _is_hidden(entry: os.DirEntry) -> bool:
    """Hidden and system folders, which are noise in a chooser.

    Windows marks these with file attributes rather than a leading dot, and a
    chooser full of ``$RECYCLE.BIN`` and ``System Volume Information`` is
    harder to use, not more honest - nothing is being hidden from a person who
    types the path.
    """
    if entry.name.startswith("."):
        return True
    try:
        attrs = entry.stat(follow_symlinks=False).st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attrs & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM))


def drive_listing() -> Listing:
    """The top of the tree: the drives on this computer."""
    entries = []
    for drive in fixed_drives():
        letter = str(drive).rstrip("\\/")
        label = f"Drive {letter}"
        try:
            usage = shutil.disk_usage(drive)
            free_gb = usage.free / 1024**3
            label = f"Drive {letter}  ({free_gb:,.0f} GB free)"
        except OSError:
            pass
        entries.append(Entry(name=label, path=str(drive)))

    return Listing(
        path=None,
        label="This computer",
        parent=None,
        entries=entries,
        note=None if entries else "No drives were found on this computer.",
    )


def _crumbs(path: Path) -> list[Entry]:
    """The path broken into clickable pieces, drive first."""
    out: list[Entry] = []
    parts = list(path.parts)
    for i, part in enumerate(parts):
        here = Path(*parts[: i + 1])
        out.append(Entry(name=part.rstrip("\\/") or str(here), path=str(here)))
    return out


def list_folder(raw: str | None, settings: Settings) -> Listing:
    """What is inside ``raw``, or the drive list when it is empty.

    Never raises for an ordinary filesystem problem. A missing folder, a file
    given where a folder was expected, and a folder Windows refuses to open are
    all answers the user needs to read, so each comes back as a Listing that
    says so.
    """
    if not raw or not raw.strip():
        return drive_listing()

    try:
        path = Path(os.path.expandvars(raw.strip())).expanduser()
    except (OSError, ValueError):
        return Listing(path=raw, label=raw, parent=None, readable=False, note=GONE)

    parent = str(path.parent) if path.parent != path else None

    if not path.exists():
        return Listing(path=str(path), label=str(path), parent=parent,
                       readable=False, note=GONE)
    if not path.is_dir():
        return Listing(path=str(path), label=str(path), parent=str(path.parent),
                       readable=False, note=NOT_A_FOLDER)

    exclude = settings.scan.exclude_dirs
    known = set(settings.scan.extensions)
    entries: list[Entry] = []
    readable = True
    note = None

    try:
        with os.scandir(path) as scan:
            for entry in scan:
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if _is_hidden(entry):
                    continue

                if is_dir:
                    entries.append(
                        Entry(
                            name=entry.name,
                            path=entry.path,
                            excluded_by_default=_normally_skipped(entry.name, exclude),
                            modified=_modified(entry),
                            kind="folder",
                        )
                    )
                    continue

                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue

                ext = os.path.splitext(entry.name)[1].lower()
                entries.append(
                    Entry(
                        name=entry.name,
                        path=entry.path,
                        modified=_modified(entry),
                        kind="file",
                        size=_size(entry),
                        ext=ext or None,
                        readable_kind=ext in known,
                    )
                )
    except PermissionError:
        readable = False
        note = UNREADABLE
    except OSError as exc:
        readable = False
        note = f"Recall could not read this folder ({exc.__class__.__name__})."

    # Folders first, then files, each alphabetically - the order Explorer uses,
    # and the order somebody scanning a list expects.
    entries.sort(key=lambda e: (e.kind != "folder", e.name.lower()))

    if readable and not entries:
        note = "There is nothing inside this folder. You can still search it."

    return Listing(
        path=str(path),
        label=path.name or str(path),
        parent=parent,
        readable=readable,
        note=note,
        entries=entries,
        crumbs=_crumbs(path),
    )


def _size(entry: os.DirEntry) -> int | None:
    try:
        return int(entry.stat(follow_symlinks=False).st_size)
    except OSError:
        return None


def _modified(entry: os.DirEntry) -> str | None:
    """When this folder last changed, or None if Windows will not say.

    One extra stat per folder. On Windows ``scandir`` has usually cached it
    already, so listing a folder of a few hundred entries stays instant.
    """
    try:
        when = entry.stat(follow_symlinks=False).st_mtime
    except OSError:
        return None
    try:
        return datetime.fromtimestamp(when, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def _normally_skipped(name: str, exclude_dirs: list[str]) -> bool:
    """Would a whole-drive search skip a folder with this name?

    Used only to label the chooser. Choosing such a folder deliberately does
    search it - see ``chosen`` in walker.walk_roots - and saying so up front is
    better than letting the user wonder why Windows returned nothing.
    """
    lowered = name.lower()
    return any(
        lowered == part.lower().strip("\\/").split("\\")[-1]
        for part in exclude_dirs
    )


# ---------------------------------------------------------------------------
# Folders the user has chosen before
# ---------------------------------------------------------------------------


def recent_folders(conn) -> list[str]:
    """What was picked before, newest first, keeping only what still exists.

    Files as well as folders: the chooser lets somebody pick one .pst
    directly, and that is just as worth offering again.
    """
    raw = get_setting(conn, RECENT_KEY)
    if not raw:
        return []
    try:
        stored = json.loads(raw)
    except (TypeError, ValueError):
        log.warning("the remembered folder list was unreadable; starting again")
        return []
    if not isinstance(stored, list):
        return []

    out: list[str] = []
    for item in stored:
        if isinstance(item, str) and Path(item).exists() and item not in out:
            out.append(item)
    return out[:RECENT_LIMIT]


def remember_folders(conn, paths: list[str]) -> list[str]:
    """Record what the user chose, newest first, without duplicates.

    Drives are not remembered: they are always on the chooser anyway, and a
    "recent" list that fills up with C:\\ is no use to anybody.
    """
    keep = [p for p in paths if not _is_drive_root(p)]
    if not keep:
        return recent_folders(conn)

    merged: list[str] = []
    for path in keep + recent_folders(conn):
        if path not in merged:
            merged.append(path)

    merged = merged[:RECENT_LIMIT]
    set_setting(conn, RECENT_KEY, json.dumps(merged))
    return merged


def _is_drive_root(raw: str) -> bool:
    path = Path(raw)
    return path.parent == path
