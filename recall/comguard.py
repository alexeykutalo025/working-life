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
"""

from __future__ import annotations

import queue
import threading
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


class ComUnavailable(Exception):
    """Outlook cannot be used. The message says why, in plain language."""


class ComTimeout(ComUnavailable):
    """Outlook was asked something and never answered."""


def run_with_timeout(
    fn: Callable[[], T],
    timeout: float = DEFAULT_TIMEOUT,
    *,
    what: str = "Outlook",
) -> T:
    """Run ``fn`` on a COM-initialised daemon thread, with a deadline.

    Raises ``ComTimeout`` if the deadline passes, or re-raises whatever ``fn``
    raised. Never blocks longer than ``timeout``.
    """
    box: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            import pythoncom  # type: ignore[import-not-found]
        except ImportError as exc:
            box.put((False, ComUnavailable(f"pywin32 is not installed: {exc}")))
            return
        pythoncom.CoInitialize()
        try:
            box.put((True, fn()))
        except BaseException as exc:  # noqa: BLE001 - relayed to the caller
            box.put((False, exc))
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass

    thread = threading.Thread(target=worker, daemon=True, name=f"com-{what}")
    thread.start()
    try:
        ok, payload = box.get(timeout=timeout)
    except queue.Empty:
        log.warning("%s did not respond within %.0f seconds", what, timeout)
        raise ComTimeout(HUNG_MESSAGE.format(timeout=timeout)) from None
    if ok:
        return payload  # type: ignore[return-value]
    raise payload  # type: ignore[misc]


def outlook_version(timeout: float = DISPATCH_TIMEOUT) -> str:
    """Outlook's version string, or raise ``ComUnavailable`` with the reason."""

    def probe() -> str:
        import win32com.client  # type: ignore[import-not-found]

        app = win32com.client.Dispatch("Outlook.Application")
        return str(app.Version)

    try:
        return run_with_timeout(probe, timeout, what="Outlook")
    except ComTimeout:
        raise
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
