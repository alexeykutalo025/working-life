"""Talking to Microsoft Outlook without ever hanging.

``win32com.client.Dispatch("Outlook.Application")`` can block forever. Outlook
shows a profile chooser, a sign-in prompt, or a repair dialog on a hidden
window, and COM simply waits. During development this happened on the first
try: Outlook was launched by an automation call, put up something invisible,
and every later call blocked indefinitely.

An overnight extraction that silently stops on a hidden dialog is exactly the
failure the spec forbids, so every COM call in Recall goes through here:

* it runs on a worker thread with a deadline,
* on timeout it returns a plain-language explanation instead of waiting,
* the stuck thread is a daemon, so it can never keep the program alive.

A hung COM thread cannot be killed from outside - that is a Windows fact, not a
shortcut taken here. What this module guarantees is that Recall notices, says
so, and carries on with the other backend.

It also cleans up after itself. A Dispatch that hangs leaves a real OUTLOOK.EXE
running with no window, and that process then holds the mail profile, so the
next attempt hangs on the same invisible dialog and the advice "open Outlook
and answer whatever it is asking" cannot be followed. Recall therefore ends an
Outlook it started and nobody can see - and only ever that one. See
``end_stranded_outlook``.
"""

from __future__ import annotations

import queue
import subprocess
import threading
import time
from typing import Any, Callable, TypeVar

from .logging_setup import get_logger

log = get_logger("com")

T = TypeVar("T")

#: How long to wait for Outlook to answer before declaring it unavailable.
DEFAULT_TIMEOUT = 45.0

#: How long to wait for the first Dispatch, which is the slow one: Outlook may
#: have to start up completely.
DISPATCH_TIMEOUT = 90.0

HUNG_MESSAGE = (
    "Outlook did not answer within {timeout:.0f} seconds. It is usually waiting "
    "for someone to click something on a window you cannot see - a profile "
    "chooser, a password box, or a repair prompt.\n"
    "What to do: open Outlook yourself, answer whatever it asks, close it, then "
    "run this again. Recall will use its own built-in reader in the meantime."
)

#: Added when Recall had to end the invisible Outlook it started, so the user
#: knows why the advice above will now work.
CLEANED_UP = (
    "\nRecall closed the invisible copy of Outlook it had started, so opening "
    "Outlook yourself will work now."
)

#: Added when the thing in the way was already running before Recall asked.
#: Recall never closes that one - it may be the user's own Outlook, mid-sentence.
ALREADY_RUNNING = (
    "\nOutlook was already running before Recall asked it anything, so Recall "
    "has left it alone. If you cannot see an Outlook window, the hidden one is "
    "waiting for an answer: end OUTLOOK.EXE in Task Manager, or sign out and "
    "back in, then try again."
)


class ComUnavailable(Exception):
    """Outlook cannot be used. The message says why, in plain language."""


class ComTimeout(ComUnavailable):
    """Outlook was asked something and never answered."""


class Heartbeat:
    """Proof that a long COM call is still getting somewhere.

    A fixed timeout cannot serve both cases Recall meets. Reading a 40 GB
    mailbox through Outlook legitimately takes hours; a corrupt file that makes
    Outlook put up a repair dialog never finishes at all. A single number is
    either too short for the first or useless for the second.

    So the worker calls ``beat()`` as it makes progress, and the deadline is
    measured from the last beat rather than from the start. A wedged call is
    caught in minutes; a slow one runs as long as it keeps moving.
    """

    def __init__(self) -> None:
        self._last = time.monotonic()
        self._lock = threading.Lock()
        self.count = 0

    def beat(self) -> None:
        with self._lock:
            self._last = time.monotonic()
            self.count += 1

    @property
    def since_last(self) -> float:
        with self._lock:
            return time.monotonic() - self._last


def run_with_timeout(
    fn: Callable[..., T],
    timeout: float = DEFAULT_TIMEOUT,
    *,
    what: str = "Outlook",
    heartbeat: bool = False,
) -> T:
    """Run ``fn`` on a COM-initialised daemon thread, with a deadline.

    With ``heartbeat=True``, ``fn`` is called with a ``Heartbeat`` and the
    deadline applies to the gap between beats rather than to the whole call.

    Raises ``ComTimeout`` when the deadline passes, or re-raises whatever
    ``fn`` raised. Never blocks indefinitely.
    """
    box: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
    pulse = Heartbeat() if heartbeat else None

    def worker() -> None:
        try:
            import pythoncom  # type: ignore[import-not-found]
        except ImportError as exc:
            box.put((False, ComUnavailable(f"pywin32 is not installed: {exc}")))
            return
        pythoncom.CoInitialize()
        try:
            box.put((True, fn(pulse) if pulse is not None else fn()))
        except BaseException as exc:  # noqa: BLE001 - relayed to the caller
            box.put((False, exc))
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass

    thread = threading.Thread(target=worker, daemon=True, name=f"com-{what}")
    thread.start()

    if pulse is None:
        try:
            ok, payload = box.get(timeout=timeout)
        except queue.Empty:
            log.warning("%s did not respond within %.0f seconds", what, timeout)
            raise ComTimeout(HUNG_MESSAGE.format(timeout=timeout)) from None
    else:
        while True:
            try:
                ok, payload = box.get(timeout=min(timeout, 5.0))
                break
            except queue.Empty:
                if pulse.since_last > timeout:
                    log.warning(
                        "%s stopped making progress: nothing for %.0f seconds "
                        "after %d steps",
                        what, pulse.since_last, pulse.count,
                    )
                    raise ComTimeout(HUNG_MESSAGE.format(timeout=timeout)) from None

    if ok:
        return payload  # type: ignore[return-value]
    raise payload  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Cleaning up an Outlook that got stuck
# ---------------------------------------------------------------------------
#
# Two conditions must BOTH hold before Recall ends an Outlook process:
#
#   1. it was not running before Recall asked Outlook anything, and
#   2. it has no window on screen.
#
# The first means Recall started it. The second means nobody is looking at it.
# Together they make it impossible to close the Outlook someone is writing in,
# which is the only outcome here that would be unforgivable.

_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW, so tasklist never flashes a console


def outlook_processes() -> set[int]:
    """The process ids of every Outlook running right now.

    Uses ``tasklist``, which is part of Windows, rather than adding a
    dependency for one query. Any failure returns an empty set, which makes the
    caller treat every Outlook as pre-existing and therefore untouchable - the
    safe direction to be wrong in.
    """
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq OUTLOOK.EXE", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("could not list Outlook processes: %s", exc)
        return set()

    pids: set[int] = set()
    for line in result.stdout.splitlines():
        fields = [f.strip('"') for f in line.split('","')]
        if len(fields) > 1 and fields[0].strip('"').upper() == "OUTLOOK.EXE":
            try:
                pids.add(int(fields[1]))
            except ValueError:
                continue
    return pids


def has_visible_window(pid: int) -> bool:
    """Does this process own a window a person could be looking at?

    If the answer cannot be determined, it is True: an unknown window counts as
    a visible one, so an undecidable case never ends a process.
    """
    try:
        import win32gui  # type: ignore[import-not-found]
        import win32process  # type: ignore[import-not-found]
    except ImportError:
        return True

    seen = False

    def visit(handle: int, _: Any) -> bool:
        nonlocal seen
        try:
            if win32gui.IsWindowVisible(handle) and win32gui.GetWindowText(handle):
                if win32process.GetWindowThreadProcessId(handle)[1] == pid:
                    seen = True
                    return False
        except Exception:  # noqa: BLE001 - a window can vanish mid-enumeration
            pass
        return True

    try:
        win32gui.EnumWindows(visit, None)
    except Exception as exc:  # noqa: BLE001 - EnumWindows stops by raising
        log.debug("window enumeration ended early: %s", exc)
    return seen


def end_stranded_outlook(started_before: set[int]) -> list[int]:
    """End the invisible Outlook that Recall started, and nothing else.

    ``started_before`` is the set of Outlook processes that existed before
    Recall asked Outlook anything. Anything in that set is somebody else's and
    is left strictly alone, running or hung.

    Returns the ids actually ended, for the message shown to the user.
    """
    ended: list[int] = []
    for pid in outlook_processes() - started_before:
        if has_visible_window(pid):
            log.info("leaving Outlook %d alone: it has a window on screen", pid)
            continue
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True, timeout=10, creationflags=_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("could not end stranded Outlook %d: %s", pid, exc)
            continue
        log.info("ended the invisible Outlook %d that Recall had started", pid)
        ended.append(pid)
    return ended


def _cleanup_note(started_before: set[int]) -> str:
    """The sentence to add to a timeout message, after tidying up."""
    if end_stranded_outlook(started_before):
        return CLEANED_UP
    return ALREADY_RUNNING if started_before else ""


def outlook_version(timeout: float = DISPATCH_TIMEOUT) -> str:
    """Outlook's version string, or raise ``ComUnavailable`` with the reason.

    Asking Outlook its version starts Outlook. If it was not running before,
    this puts it back the way it found it - otherwise every health check leaves
    another invisible Outlook resident, and the next one to hang cannot be
    cleared automatically because by then it looks pre-existing.
    """
    before = outlook_processes()

    def probe() -> str:
        import win32com.client  # type: ignore[import-not-found]

        app = win32com.client.Dispatch("Outlook.Application")
        version = str(app.Version)
        if not before:
            # Nothing of the user's to interrupt: ask it to go away politely.
            try:
                app.Quit()
            except Exception as exc:  # noqa: BLE001 - it may refuse; the sweep follows
                log.debug("Outlook declined to quit: %s", exc)
        return version

    try:
        version = run_with_timeout(probe, timeout, what="Outlook")
    except ComTimeout as exc:
        raise ComTimeout(str(exc) + _cleanup_note(before)) from None
    except ImportError as exc:
        raise ComUnavailable(
            "pywin32 is not installed, so Outlook cannot be used as a reader. "
            "Install it with:  pip install pywin32"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - Outlook absent or refusing
        raise ComUnavailable(
            f"Outlook could not be started ({exc.__class__.__name__}: {exc}). "
            "It is probably not installed on this computer."
        ) from exc

    # Quit is asynchronous and Outlook does not always take the hint, so sweep
    # up anything it left. Only ever a windowless instance that we started.
    end_stranded_outlook(before)
    return version


def is_outlook_registered() -> bool:
    """Is Outlook registered for automation on this machine?

    This reads the registry only - instant, and it never starts Outlook. Use it
    to decide whether trying the COM backend is worth the wait at all.
    """
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return False
    for hive in (winreg.HKEY_CLASSES_ROOT,):
        try:
            with winreg.OpenKey(hive, r"Outlook.Application\CLSID"):
                return True
        except OSError:
            continue
    return False
