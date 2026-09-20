"""`recall doctor` - does this computer have what Recall needs?

Prints a pass/fail table. A FAIL means something will not work at all; a WARN
means something will work in a reduced way, and says exactly what is reduced.
Nothing here guesses: if a library is missing, the row says which file types
become unreadable.
"""

from __future__ import annotations

import importlib
import platform
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import Settings

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"


@dataclass(slots=True)
class Check:
    name: str
    status: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.status != FAIL


# (import name, pypi name, what it is for, is it essential)
_LIBRARIES: list[tuple[str, str, str, bool]] = [
    ("fastapi", "fastapi", "the local web page", True),
    ("uvicorn", "uvicorn", "the local web server", True),
    ("typer", "typer", "these commands", True),
    ("pydantic", "pydantic", "settings and checking", True),
    ("pypff", "libpff-python", "reading .pst and .ost quickly", False),
    ("extract_msg", "extract-msg", "reading .msg files", False),
    ("icalendar", "icalendar", "reading .ics calendar files", False),
    ("vobject", "vobject", "reading .vcs calendars and .vcf contacts", False),
    ("charset_normalizer", "charset-normalizer", "repairing old text encodings", True),
    ("win32com.client", "pywin32", "using Outlook itself as a reader", False),
    ("docx", "python-docx", "searching inside Word attachments", False),
    ("openpyxl", "openpyxl", "searching inside Excel attachments", False),
    ("pypdf", "pypdf", "searching inside PDF attachments", False),
    ("pptx", "python-pptx", "searching inside PowerPoint attachments", False),
    ("pytesseract", "pytesseract", "reading text out of scanned images (optional)", False),
]


def check_python() -> Check:
    v = sys.version_info
    text = f"{v.major}.{v.minor}.{v.micro} at {sys.executable}"
    if (v.major, v.minor) < (3, 11):
        return Check("Python version", FAIL, f"{text} - Recall needs 3.11 or newer")
    return Check("Python version", PASS, text)


def check_platform() -> Check:
    text = f"{platform.system()} {platform.release()}"
    if platform.system() != "Windows":
        return Check(
            "Operating system",
            WARN,
            f"{text} - Outlook files and OneDrive cloud-file detection are "
            "Windows features. Reading .eml/.mbox/.ics/.vcf still works.",
        )
    return Check("Operating system", PASS, text)


def check_sqlite() -> list[Check]:
    checks = [Check("SQLite version", PASS, sqlite3.sqlite_version)]
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE VIRTUAL TABLE t USING fts5(a, "
            "tokenize='porter unicode61 remove_diacritics 2')"
        )
        conn.close()
        checks.append(Check("Full-text search (FTS5)", PASS, "available"))
    except sqlite3.Error as exc:
        checks.append(
            Check(
                "Full-text search (FTS5)",
                FAIL,
                f"not available ({exc}). Searching will not work. "
                "Install a Python build with FTS5 enabled.",
            )
        )
    return checks


def check_libraries() -> list[Check]:
    out: list[Check] = []
    for module, package, purpose, essential in _LIBRARIES:
        try:
            mod = importlib.import_module(module)
            version = getattr(mod, "__version__", "")
            if module == "pypff":
                try:
                    version = mod.get_version()
                except Exception:  # noqa: BLE001 - version is cosmetic
                    version = ""
            detail = f"{version} - {purpose}" if version else purpose
            out.append(Check(f"Library: {package}", PASS, detail))
        except Exception as exc:  # noqa: BLE001 - any import failure counts
            status = FAIL if essential else WARN
            if module == "pytesseract":
                status = WARN
            out.append(
                Check(
                    f"Library: {package}",
                    status,
                    f"not installed - {purpose} will not work. "
                    f"Install with: pip install {package}   ({exc.__class__.__name__})",
                )
            )
    return out


def check_outlook_com() -> Check:
    """Is Microsoft Outlook installed and driveable?

    This is the authoritative reader for .pst/.ost. Without it, modern .ost
    files and password-protected stores may be unreadable.

    The check is on a deadline. Outlook can sit forever behind an invisible
    dialog, and a setup check that never returns is worse than a clear "no".
    """
    from .comguard import ComTimeout, ComUnavailable, is_outlook_registered, outlook_version

    if platform.system() != "Windows":
        return Check(
            "Microsoft Outlook (COM)",
            WARN,
            "not checked - Outlook automation is Windows only",
        )
    try:
        import win32com.client  # noqa: F401
    except ImportError:
        return Check(
            "Microsoft Outlook (COM)",
            WARN,
            "pywin32 is not installed, so Outlook cannot be used as a reader. "
            ".ost files may be unreadable. Install with: pip install pywin32",
        )
    if not is_outlook_registered():
        return Check(
            "Microsoft Outlook (COM)",
            WARN,
            "Outlook is not installed on this computer. The fast built-in reader "
            "will be used for .pst; some .ost files may be unreadable.",
        )
    try:
        version = outlook_version()
        return Check(
            "Microsoft Outlook (COM)", PASS, f"version {version}, usable as a reader"
        )
    except ComTimeout as exc:
        return Check("Microsoft Outlook (COM)", WARN, " ".join(str(exc).split()))
    except ComUnavailable as exc:
        return Check("Microsoft Outlook (COM)", WARN, str(exc))


def check_workdir(settings: Settings) -> list[Check]:
    out: list[Check] = []
    workdir = settings.workdir_path
    try:
        settings.ensure_workdir()
        probe = workdir / ".recall-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        out.append(Check("Working folder", PASS, f"{workdir} (writable)"))
    except OSError as exc:
        out.append(
            Check(
                "Working folder",
                FAIL,
                f"{workdir} cannot be written to: {exc}. "
                "Choose another folder in config.toml under [workdir].",
            )
        )
        return out

    if _is_onedrive_path(workdir):
        out.append(
            Check(
                "Working folder location",
                FAIL,
                f"{workdir} is inside OneDrive. Recall must never write into a "
                "synced folder - it would upload your whole archive. "
                "Change [workdir] path in config.toml.",
            )
        )
    else:
        out.append(Check("Working folder location", PASS, "not inside OneDrive"))

    try:
        usage = shutil.disk_usage(workdir)
        free_gb = usage.free / (1024**3)
        detail = f"{free_gb:,.1f} GB free on {workdir.drive or workdir.anchor}"
        if free_gb < 5:
            out.append(
                Check(
                    "Disk space",
                    FAIL,
                    detail + " - not enough room for an archive. Free up space "
                    "or point [workdir] at a bigger drive.",
                )
            )
        elif free_gb < 50:
            out.append(
                Check(
                    "Disk space",
                    WARN,
                    detail + " - tight. A large mailbox and its attachments can "
                    "need tens of gigabytes.",
                )
            )
        else:
            out.append(Check("Disk space", PASS, detail))
    except OSError as exc:
        out.append(Check("Disk space", WARN, f"could not be measured: {exc}"))

    return out


def check_database(settings: Settings) -> Check:
    from . import db as db_module

    try:
        conn = db_module.connect(settings.db_path)
        result = db_module.integrity_check(conn)
        tables = len(db_module.table_names(conn))
        conn.close()
        if result != "ok":
            return Check(
                "Archive database",
                FAIL,
                f"{settings.db_path} reports: {result}. The file is damaged. "
                "Move it aside and scan again.",
            )
        return Check(
            "Archive database", PASS, f"{settings.db_path} ({tables} tables, healthy)"
        )
    except Exception as exc:  # noqa: BLE001
        return Check(
            "Archive database",
            FAIL,
            f"{settings.db_path} could not be opened or created: {exc}",
        )


def check_scan_roots(settings: Settings) -> Check:
    roots = settings.effective_scan_roots()
    existing = [r for r in roots if r.exists()]
    missing = [r for r in roots if not r.exists()]
    detail = ", ".join(str(r) for r in existing) or "none"
    if not existing:
        return Check(
            "Folders to search",
            FAIL,
            "none of the configured folders exist: "
            + (", ".join(str(m) for m in missing) or "(no folders configured)"),
        )
    if missing:
        return Check(
            "Folders to search",
            WARN,
            f"{detail}  |  not found and will be skipped: "
            + ", ".join(str(m) for m in missing),
        )
    return Check("Folders to search", PASS, detail)


def check_temp_space() -> Check:
    try:
        tmp = Path(tempfile.gettempdir())
        usage = shutil.disk_usage(tmp)
        return Check(
            "Temporary folder", PASS, f"{tmp} ({usage.free / (1024**3):,.1f} GB free)"
        )
    except OSError as exc:
        return Check("Temporary folder", WARN, f"could not be measured: {exc}")


def _is_onedrive_path(path: Path) -> bool:
    """True when this path sits inside any OneDrive folder on this machine."""
    from .config import onedrive_roots

    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    for root in onedrive_roots():
        try:
            resolved.relative_to(root.resolve())
            return True
        except (ValueError, OSError):
            continue
    # Fall back to the name, for machines where the env var is not set.
    return any(part.lower().startswith("onedrive") for part in resolved.parts)


def run_all_checks(settings: Settings) -> list[Check]:
    checks: list[Check] = [check_python(), check_platform()]
    checks.extend(check_sqlite())
    checks.extend(check_libraries())
    checks.append(check_outlook_com())
    checks.extend(check_workdir(settings))
    checks.append(check_database(settings))
    checks.append(check_scan_roots(settings))
    checks.append(check_temp_space())
    return checks


def format_table(checks: list[Check]) -> str:
    """The pass/fail table, sized to the terminal."""
    name_w = max((len(c.name) for c in checks), default=20)
    name_w = max(name_w, 20)
    try:
        term_w = max(shutil.get_terminal_size((100, 25)).columns, 72)
    except OSError:
        term_w = 100
    detail_w = max(term_w - name_w - 12, 30)

    lines: list[str] = []
    lines.append(f"{'CHECK'.ljust(name_w)}  {'RESULT':<6}  DETAIL")
    lines.append("-" * min(term_w, name_w + 10 + detail_w))
    for c in checks:
        detail_lines = _wrap(c.detail, detail_w)
        lines.append(f"{c.name.ljust(name_w)}  {c.status:<6}  {detail_lines[0]}")
        pad = " " * (name_w + 10)
        for extra in detail_lines[1:]:
            lines.append(pad + extra)

    n_fail = sum(1 for c in checks if c.status == FAIL)
    n_warn = sum(1 for c in checks if c.status == WARN)
    n_pass = sum(1 for c in checks if c.status == PASS)
    lines.append("")
    lines.append(f"{n_pass} passed, {n_warn} warnings, {n_fail} failed")
    if n_fail:
        lines.append("")
        lines.append("Recall cannot run properly until the FAIL lines above are fixed.")
    elif n_warn:
        lines.append("")
        lines.append(
            "Recall will run. Each WARN line says exactly what will not work."
        )
    else:
        lines.append("")
        lines.append("Everything Recall needs is present.")
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    if not words:
        return [""]
    out: list[str] = []
    line = words[0]
    for word in words[1:]:
        if len(line) + 1 + len(word) <= width:
            line += " " + word
        else:
            out.append(line)
            line = word
    out.append(line)
    return out


def doctor_report(settings: Settings) -> tuple[str, bool]:
    """Returns (printable table, everything_essential_is_present)."""
    checks = run_all_checks(settings)
    return format_table(checks), all(c.ok for c in checks)
