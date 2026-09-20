"""Long jobs that the web page can watch, cancel and resume.

One job runs at a time. That is a deliberate limit: two extractions writing to
one SQLite file would fight, and a 72-year-old watching a progress bar should
never have to wonder which of three bars is his.

Every job reports honest progress - what it is doing now, how much is done, how
much is left, and how fast. When it cannot estimate a remaining time it says
so rather than inventing one.
"""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from ..logging_setup import get_logger

log = get_logger("api.jobs")


@dataclass
class JobStatus:
    kind: str = ""                 # scan | extract | hydrate | index | audit | read_all
    state: str = "idle"            # idle|running|done|canceled|failed
    started_utc: str | None = None
    finished_utc: str | None = None
    message: str = ""
    current: str = ""              # what is being worked on right now
    done: int = 0
    total: int | None = None       # None = not knowable yet
    rate_per_sec: float | None = None
    #: Which step of a multi-step job this is, and how many there are. Left
    #: empty by the single-step jobs, which have nothing to say about it.
    phase: str = ""
    phase_index: int = 0
    phase_count: int = 0
    #: What `done` and `total` are counting. Downloading counts bytes, because
    #: a job that is one twenty-gigabyte file would otherwise sit at "0 of 1"
    #: for forty minutes and look as though it had hung.
    unit: str = "files"
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def elapsed_sec(self) -> float:
        if not self.started_utc:
            return 0.0
        start = datetime.strptime(self.started_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        end = (
            datetime.strptime(self.finished_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            if self.finished_utc
            else datetime.now(timezone.utc)
        )
        return max(0.0, (end - start).total_seconds())

    @property
    def remaining_sec(self) -> float | None:
        """None means "cannot be estimated" - and the screen says exactly that."""
        if self.total is None or not self.rate_per_sec or self.done >= self.total:
            return None
        return (self.total - self.done) / self.rate_per_sec

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "state": self.state,
            "started_utc": self.started_utc,
            "finished_utc": self.finished_utc,
            "message": self.message,
            "current": self.current,
            "done": self.done,
            "total": self.total,
            "rate_per_sec": round(self.rate_per_sec, 1) if self.rate_per_sec else None,
            "phase": self.phase,
            "phase_index": self.phase_index,
            "phase_count": self.phase_count,
            "unit": self.unit,
            "elapsed_sec": round(self.elapsed_sec, 1),
            "remaining_sec": (
                round(self.remaining_sec) if self.remaining_sec is not None else None
            ),
            "error": self.error,
            "detail": self.detail,
        }


class JobBusy(Exception):
    """Something is already running. The message names it."""


class JobManager:
    """Holds the one running job, its progress, and its cancel switch."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self.status = JobStatus()
        self._started_monotonic = 0.0

    # -- state ------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel

    def request_cancel(self) -> bool:
        if not self.running:
            return False
        self._cancel.set()
        # A download has nothing part-written worth keeping: the half-file is
        # deleted, because a truncated mailbox that looks whole is worse than
        # no mailbox. Anything that does keep part-written work says so itself.
        self.status.message = (
            "Stopping cleanly - finishing the step in hand..."
            if self.status.phase == "downloading"
            else "Stopping cleanly - finishing what is part-written..."
        )
        log.info("Cancel requested for %s job", self.status.kind)
        return True

    # -- running ----------------------------------------------------------

    def start(
        self,
        kind: str,
        target: Callable[["JobManager"], None],
        *,
        message: str = "",
    ) -> JobStatus:
        with self._lock:
            if self.running:
                # Named by what it is doing, not by the internal kind. "A
                # hydrate is already running" is not a sentence anyone outside
                # this file should have to read.
                raise JobBusy(
                    "Something is already running. "
                    "Wait for it to finish, or stop it first."
                )
            self._cancel = threading.Event()
            self.status = JobStatus(
                kind=kind,
                state="running",
                started_utc=_utc_now(),
                message=message or "Starting...",
            )
            self._started_monotonic = time.monotonic()

            def wrapper() -> None:
                try:
                    target(self)
                    if self._cancel.is_set():
                        self.status.state = "canceled"
                        if not self.status.message or self.status.message.startswith(
                            "Stopping"
                        ):
                            self.status.message = (
                                "Stopped. Everything done so far was saved - "
                                "start it again to carry on."
                            )
                    elif self.status.state == "running":
                        self.status.state = "done"
                except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                    self.status.state = "failed"
                    self.status.error = f"{exc.__class__.__name__}: {exc}"
                    self.status.message = (
                        "This stopped with a problem. Nothing already saved was "
                        "lost. The exact message is below."
                    )
                    self.status.detail["traceback"] = traceback.format_exc()
                    log.exception("%s job failed", kind)
                finally:
                    self.status.finished_utc = _utc_now()

            self._thread = threading.Thread(target=wrapper, daemon=True, name=f"job-{kind}")
            self._thread.start()
            return self.status

    # -- progress reporting, called from inside the job -------------------

    def progress(
        self,
        *,
        done: int | None = None,
        total: int | None = None,
        current: str | None = None,
        message: str | None = None,
    ) -> None:
        if done is not None:
            self.status.done = done
        if total is not None:
            self.status.total = total
        if current is not None:
            self.status.current = current
        if message is not None:
            self.status.message = message
        elapsed = time.monotonic() - self._started_monotonic
        if elapsed > 0.5 and self.status.done:
            self.status.rate_per_sec = self.status.done / elapsed

    def begin_phase(
        self,
        name: str,
        *,
        index: int,
        count: int,
        total: int | None = None,
        unit: str = "files",
        message: str = "",
    ) -> None:
        """Start a step of a multi-step job. The bar starts again from nothing.

        The clock behind the rate restarts too, and that is the point of this
        existing rather than callers just setting fields. ``rate_per_sec`` is
        ``done / (now - started)``, so a reading step that follows a two-hour
        download would be divided by those two hours and report a rate near
        zero and a time remaining measured in days.
        """
        self.status.phase = name
        self.status.phase_index = index
        self.status.phase_count = count
        self.status.unit = unit
        self.status.done = 0
        self.status.total = total
        self.status.current = ""
        self.status.rate_per_sec = None
        if message:
            self.status.message = message
        self._started_monotonic = time.monotonic()

    def set_detail(self, **detail: Any) -> None:
        """Attach facts to the running job without touching its progress."""
        self.status.detail.update(detail)

    def finish(self, message: str, **detail: Any) -> None:
        self.status.message = message
        self.status.detail.update(detail)

    def join(self, timeout: float | None = None) -> None:
        """Wait for the current job. Used by the CLI and by tests."""
        if self._thread is not None:
            self._thread.join(timeout)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


#: The single manager the web app and CLI share.
JOBS = JobManager()
