"""OneDrive cloud-only files: recognising them, and never opening one by accident.

A OneDrive "Files On-Demand" placeholder looks exactly like a file. It has a
name, a size and a date. Its contents are not on this computer. Reading one
byte of it makes Windows download the whole thing - which on a 20 GB PST over a
metered connection is a real cost, and on a slow link is an overnight wait the
user did not ask for.

Windows marks these files with attribute bits. Spec section 5:

    FILE_ATTRIBUTE_OFFLINE               0x00001000
    FILE_ATTRIBUTE_RECALL_ON_OPEN        0x00040000
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS 0x00400000

Any of them set means the bytes are elsewhere. Recall records the file, marks
it, and stops. Downloading happens only when the user asks for it, having been
shown the total size first - never on its own initiative.

When a download does happen, Recall keeps its own copy under ``workdir/cloud``
rather than relying on the hydrated original. OneDrive puts files back in the
cloud on its own schedule, and a file that was readable this morning is a
placeholder again by Friday; a copy Recall owns stays readable.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, ClassVar

from ..logging_setup import get_logger

log = get_logger("scan.onedrive")

FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000

PLACEHOLDER_MASK = (
    FILE_ATTRIBUTE_OFFLINE
    | FILE_ATTRIBUTE_RECALL_ON_OPEN
    | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)

#: Which bit was set, for the explanation shown to the user.
_BIT_NAMES = {
    FILE_ATTRIBUTE_OFFLINE: "offline",
    FILE_ATTRIBUTE_RECALL_ON_OPEN: "recall-on-open",
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS: "recall-on-data-access",
}


class HydrationRefused(Exception):
    """A download was asked for and Recall declined. The message says why."""


def is_placeholder(stat_result: os.stat_result) -> bool:
    """True when this file's bytes are not on this computer.

    Takes an already-obtained ``os.stat_result`` so the caller controls how many
    times the filesystem is touched. On platforms without
    ``st_file_attributes`` (anything but Windows) the answer is always False,
    because cloud placeholders are a Windows feature.
    """
    attrs = getattr(stat_result, "st_file_attributes", None)
    if attrs is None:
        return False
    return bool(attrs & PLACEHOLDER_MASK)


def placeholder_reason(stat_result: os.stat_result) -> str | None:
    """Which attribute bits made this a placeholder, named for a human."""
    attrs = getattr(stat_result, "st_file_attributes", None)
    if attrs is None:
        return None
    set_bits = [name for bit, name in _BIT_NAMES.items() if attrs & bit]
    if not set_bits:
        return None
    return ", ".join(sorted(set_bits))


def is_onedrive_path(path: Path, onedrive_roots: list[Path] | None = None) -> bool:
    """Is this path inside a OneDrive folder?

    Used for two different rules: files found there are labelled ``onedrive``,
    and Recall refuses to *write* anywhere inside one.
    """
    if onedrive_roots is None:
        from ..config import onedrive_roots as discover

        onedrive_roots = discover()

    try:
        resolved = path.resolve()
    except OSError:
        resolved = path

    for root in onedrive_roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except (ValueError, OSError):
            continue

    # Env vars are not always set (a second account, a service context), so fall
    # back to the folder name, which OneDrive always uses.
    return any(part.lower().startswith("onedrive") for part in resolved.parts)


def assert_not_onedrive(path: Path) -> None:
    """Guard every write. Spec section 14: never write into a OneDrive folder."""
    if is_onedrive_path(path):
        raise HydrationRefused(
            f"Recall will not write to {path} because it is inside OneDrive. "
            "Writing there would upload your whole archive. "
            "Change the [workdir] path in config.toml."
        )


@dataclass(slots=True)
class HydrationPlan:
    """What downloading a set of placeholders would cost, before anything runs."""

    paths: list[Path]
    total_bytes: int
    free_bytes: int
    #: Room wanted on each drive, keyed by drive. A drive that is both the
    #: source and the destination appears once, charged twice.
    needed_by_drive: dict[str, int] = field(default_factory=dict)
    free_by_drive: dict[str, int] = field(default_factory=dict)

    #: A full disk part-way through a download helps nobody.
    HEADROOM_BYTES: ClassVar[int] = 1024**3

    @property
    def total_gb(self) -> float:
        return self.total_bytes / (1024**3)

    @property
    def needed_bytes(self) -> int:
        """The largest single demand any one drive has to meet."""
        if not self.needed_by_drive:
            return self.total_bytes
        return max(self.needed_by_drive.values())

    @property
    def fits_on_disk(self) -> bool:
        # Nothing to download always fits. Without this, an archive with no
        # cloud-only files reports "not enough room" for a download of zero
        # bytes and the button that starts the whole pass is disabled.
        if not self.paths or self.total_bytes == 0:
            return True
        if not self.needed_by_drive:
            return self.free_bytes > self.total_bytes + self.HEADROOM_BYTES
        return all(
            self.free_by_drive.get(drive, 0) > want + self.HEADROOM_BYTES
            for drive, want in self.needed_by_drive.items()
        )

    @property
    def tight_drives(self) -> list[str]:
        """The drives that do not have the room, named so the user can act."""
        return sorted(
            drive
            for drive, want in self.needed_by_drive.items()
            if self.free_by_drive.get(drive, 0) <= want + self.HEADROOM_BYTES
        )

    def describe(self) -> str:
        n = len(self.paths)
        sentence = (
            f"{n} file{'s' if n != 1 else ''} would be downloaded from OneDrive, "
            f"{self.total_gb:,.2f} GB in total. "
        )
        if self.needed_bytes > self.total_bytes:
            sentence += (
                f"That needs about {self.needed_bytes / (1024**3):,.1f} GB of room "
                "while it works, because the OneDrive original is filled in on "
                "this computer as well as copied. "
            )
        return sentence + (
            f"There is {self.free_bytes / (1024**3):,.1f} GB free on the drive "
            "with least room."
        )


def _drive_of(path: Path) -> str:
    """The drive a path sits on, as the key the free-space maths groups by."""
    return (path.anchor or str(path.parent)).lower()


def _free_on(drive: str) -> int:
    try:
        return shutil.disk_usage(drive).free
    except OSError:
        return 0


def plan_hydration(
    paths: list[Path],
    sizes: dict[str, int],
    *,
    dest_dir: Path | None = None,
) -> HydrationPlan:
    """Work out the cost of downloading these placeholders. Downloads nothing.

    Every file is charged to two drives, not one. Recall keeps its own copy, so
    the destination drive pays the file's size - and reading a placeholder to
    make that copy also fills the OneDrive original in, so the source drive pays
    it too. When both are the same drive, as they usually are, that is twice the
    download size at once. Windows offers no way to avoid it, so the honest
    thing is to say so before starting rather than run out of room at file 300.
    """
    total = sum(sizes.get(str(p), 0) for p in paths)

    needed: dict[str, int] = {}
    for p in paths:
        size = sizes.get(str(p), 0)
        needed[_drive_of(p)] = needed.get(_drive_of(p), 0) + size
        if dest_dir is not None:
            d = _drive_of(dest_dir)
            needed[d] = needed.get(d, 0) + size

    # The destination drive is always measured, even with nothing to download,
    # so the screen can still say how much room there is.
    if dest_dir is not None:
        needed.setdefault(_drive_of(dest_dir), 0)
    free = {drive: _free_on(drive) for drive in needed}

    return HydrationPlan(
        paths=list(paths),
        total_bytes=total,
        # The tightest drive, so the single headline figure is the honest one.
        free_bytes=min(free.values()) if free else 0,
        needed_by_drive=needed,
        free_by_drive=free,
    )


def hydrate(path: Path, *, chunk_size: int = 4 * 1024 * 1024) -> int:
    """Pull one placeholder's bytes onto this computer, by reading it.

    There is no Windows API to say "download this file" from Python without
    additional components, so hydration is done the way Explorer does it: read
    the file, which makes the OneDrive filter driver fetch it. The file is
    opened read-only and nothing is written back.

    Returns the number of bytes read. Raises OSError with the real reason when
    the download fails - typically no network, or not enough disk space.
    """
    read = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            read += len(chunk)
    log.info("Downloaded %s from OneDrive (%.1f MB)", path, read / (1024 * 1024))
    return read


@dataclass(slots=True)
class CopyResult:
    """What came of one attempt to bring a cloud-only file down and keep it."""

    src: Path
    dest: Path
    bytes_copied: int = 0
    content_hash: str | None = None
    reused_existing: bool = False
    canceled: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.canceled


def copy_destination(cloud_root: Path, source_id: int, src: Path) -> Path:
    """Where Recall's copy of one source file goes.

    Scoped by row id because file names collide - two mailboxes called
    ``Sent Items.dbx`` in different folders is the ordinary case, not the odd
    one. The real name is kept alongside it so the parser's extension dispatch
    still works and so the folder is legible to someone looking at it.

    Windows still refuses paths past 260 characters, so a name that would take
    it over the line is replaced by the id and the extension alone.
    """
    folder = cloud_root / f"{source_id:06d}"
    candidate = folder / src.name
    if len(str(candidate)) > 250:
        return folder / f"{source_id:06d}{src.suffix}"
    return candidate


def hydrate_to(
    src: Path,
    dest: Path,
    *,
    expected_size: int | None = None,
    chunk_size: int = 4 * 1024 * 1024,
    progress: Callable[[int], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> CopyResult:
    """Bring one placeholder down and keep a copy of it at ``dest``.

    Reading the source is what makes the OneDrive filter driver fetch the bytes;
    writing them to ``dest`` is what means Recall still has them tomorrow, after
    OneDrive has decided to put the original back in the cloud.

    The file is hashed on the way past, because the alternative is reading
    twenty gigabytes twice. It is written to a ``.part`` name and renamed only
    once the size has been checked, so a stop, a crash or a half-finished
    download never leaves behind a file that looks complete - the same rule the
    attachment store follows.

    Never raises for an ordinary failure: the reason comes back on the result,
    because one unreachable file must not end a pass over nine hundred.
    """
    from .fingerprint import new_digest

    result = CopyResult(src=src, dest=dest)

    # Before opening anything: a workdir inside OneDrive would upload every copy
    # straight back, doubling the user's cloud usage. Spec section 14.
    assert_not_onedrive(dest)

    if expected_size is not None and dest.exists():
        try:
            if dest.stat().st_size == expected_size:
                result.reused_existing = True
                return result
        except OSError:
            pass

    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_name(dest.name + ".part")
    # A part-file from a previous stop is never resumed. Appending to it would
    # need the download to restart at exactly the right offset, and getting that
    # wrong gives a corrupt mailbox that reads as a short one - which is the
    # failure this program exists to prevent.
    temporary.unlink(missing_ok=True)

    digest = new_digest()
    copied = 0
    try:
        with open(src, "rb") as reader, open(temporary, "wb") as writer:
            while True:
                if cancel is not None and cancel():
                    result.canceled = True
                    break
                chunk = reader.read(chunk_size)
                if not chunk:
                    break
                writer.write(chunk)
                digest.update(chunk)
                copied += len(chunk)
                if progress is not None:
                    progress(copied)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        result.error = f"{exc.__class__.__name__}: {exc}"
        log.warning("Could not download %s: %s", src, exc)
        return result

    if result.canceled:
        temporary.unlink(missing_ok=True)
        return result

    # A OneDrive download can fail by handing back fewer bytes rather than by
    # raising, and a short copy parses as a half-empty mailbox that nothing
    # flags. Check the size against both what was expected and what the source
    # says now - it can be evicted and changed while this runs.
    try:
        actual_src = os.stat(src).st_size
    except OSError:
        actual_src = expected_size if expected_size is not None else copied

    wanted = expected_size if expected_size is not None else actual_src
    if copied != wanted or copied != actual_src:
        temporary.unlink(missing_ok=True)
        result.error = (
            f"The download stopped short: {copied:,} bytes arrived out of "
            f"{wanted:,} expected. Nothing was kept, so there is no half-file "
            "to clean up. This usually means the connection dropped."
        )
        log.warning("Short download for %s: %d of %d", src, copied, wanted)
        return result

    os.replace(temporary, dest)
    result.bytes_copied = copied
    result.content_hash = digest.hexdigest()
    log.info(
        "Downloaded %s to %s (%.1f MB)", src, dest, copied / (1024 * 1024)
    )
    return result


def copy_utc_now() -> str:
    """Timestamp for ``source_files.local_copy_utc``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def still_placeholder(path: Path) -> bool:
    """After a hydration attempt, did the file actually arrive?

    A download can fail silently - the read succeeds against a partial file, or
    OneDrive evicts it again immediately. This re-reads the attributes so the
    Sources screen reports what is true rather than what was attempted.
    """
    try:
        return is_placeholder(os.stat(path))
    except OSError:
        return True
