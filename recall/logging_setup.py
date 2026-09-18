"""Logging. DEBUG to a dated file, INFO to the console.

Spec section 12: structured logging to ``workdir/logs/recall-YYYYMMDD.log`` at
DEBUG, console at INFO. Console lines are written for someone who is not a
programmer: no logger names, no module paths, just the time and the sentence.
"""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_CONFIGURED = False


class _ConsoleFormatter(logging.Formatter):
    """Plain sentences for the person watching the window."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        msg = record.getMessage()
        if record.levelno >= logging.ERROR:
            return f"{stamp}  PROBLEM  {msg}"
        if record.levelno >= logging.WARNING:
            return f"{stamp}  note     {msg}"
        return f"{stamp}  {msg}"


def setup_logging(
    logs_dir: str | Path,
    *,
    file_level: str = "DEBUG",
    console_level: str = "INFO",
    keep_days: int = 90,
    quiet: bool = False,
) -> Path:
    """Install handlers once per process. Returns the log file path."""
    global _CONFIGURED

    logs_dir = Path(logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"recall-{date.today():%Y%m%d}.log"

    root = logging.getLogger()
    if _CONFIGURED:
        return log_path

    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(getattr(logging, file_level.upper(), logging.DEBUG))
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)-28s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(file_handler)

    if not quiet:
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(getattr(logging, console_level.upper(), logging.INFO))
        console.setFormatter(_ConsoleFormatter())
        root.addHandler(console)

    # Uvicorn's access log is noise in a single-user local app.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("multipart").setLevel(logging.WARNING)

    _prune_old_logs(logs_dir, keep_days)
    _CONFIGURED = True
    logging.getLogger("recall").debug("Logging started, writing to %s", log_path)
    return log_path


def _prune_old_logs(logs_dir: Path, keep_days: int) -> None:
    if keep_days <= 0:
        return
    cutoff = datetime.now() - timedelta(days=keep_days)
    for old in logs_dir.glob("recall-*.log"):
        try:
            if datetime.fromtimestamp(old.stat().st_mtime) < cutoff:
                old.unlink()
        except OSError:
            # A log we cannot delete is not worth failing a run over.
            pass


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"recall.{name}")
