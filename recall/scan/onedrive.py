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
it, and stops. Downloading happens only when the user asks for it on the
Sources screen, having been shown the total size first.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

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

    @property
    def total_gb(self) -> float:
        return self.total_bytes / (1024**3)

    @property
    def fits_on_disk(self) -> bool:
        # Leave a gigabyte of headroom; a full disk mid-download helps nobody.
        return self.free_bytes > self.total_bytes + (1024**3)

    def describe(self) -> str:
        n = len(self.paths)
        return (
            f"{n} file{'s' if n != 1 else ''} would be downloaded from OneDrive, "
            f"{self.total_gb:,.2f} GB in total. "
            f"There is {self.free_bytes / (1024**3):,.1f} GB free on this drive."
        )


def plan_hydration(paths: list[Path], sizes: dict[str, int]) -> HydrationPlan:
    """Work out the cost of downloading these placeholders. Downloads nothing."""
    total = sum(sizes.get(str(p), 0) for p in paths)
    free = 0
    if paths:
        try:
            free = shutil.disk_usage(paths[0].anchor or paths[0].parent).free
        except OSError:
            free = 0
    return HydrationPlan(paths=list(paths), total_bytes=total, free_bytes=free)


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
