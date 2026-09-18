"""Settings, loaded from config.toml and validated with pydantic v2.

The config file is written for a non-programmer, so this module is deliberately
forgiving about what is *missing* (every field has a default) and deliberately
unforgiving about what is *wrong* (a bad value raises with the key name and the
file path, not a stack trace about dict keys).
"""

from __future__ import annotations

import os
import string
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Where config.toml lives when the user has not said otherwise: the project root,
# which is the parent of this package directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"


class ConfigError(Exception):
    """config.toml exists but cannot be used. The message names the problem."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkdirSettings(_Section):
    path: str = "workdir"


class ScanSettings(_Section):
    roots: list[str] = Field(default_factory=list)
    exclude_dirs: list[str] = Field(
        default_factory=lambda: [
            "Windows",
            "Program Files",
            "Program Files (x86)",
            "ProgramData",
            "$Recycle.Bin",
            "System Volume Information",
            "node_modules",
            "AppData\\Local\\Temp",
            "AppData\\Local\\Packages",
            ".git",
        ]
    )
    extensions: list[str] = Field(
        default_factory=lambda: [
            ".pst", ".ost", ".msg", ".eml", ".mbox", ".mbx", ".dbx",
            ".ics", ".vcs", ".vcf", ".olm", ".wab", ".pab",
        ]
    )
    hash_size_cap_mb: int = 2048
    min_plausible_bytes: int = 1024
    follow_symlinks: bool = False

    @field_validator("extensions")
    @classmethod
    def _lower_dotted(cls, v: list[str]) -> list[str]:
        out = []
        for ext in v:
            ext = ext.strip().lower()
            if not ext:
                continue
            if not ext.startswith("."):
                ext = "." + ext
            out.append(ext)
        return out

    @property
    def hash_size_cap_bytes(self) -> int:
        """0 means no cap."""
        return self.hash_size_cap_mb * 1024 * 1024


class OneDriveSettings(_Section):
    auto_hydrate: bool = False
    max_hydrate_batch_gb: float = 5.0


class ExtractSettings(_Section):
    batch_size: int = 1000
    sample_limit: int = 0
    pst_backend: Literal["auto", "pypff", "com"] = "auto"
    cross_check_backends: bool = True
    attachment_text_cap_mb: int = 50
    ocr_enabled: bool = False

    @field_validator("batch_size")
    @classmethod
    def _positive_batch(cls, v: int) -> int:
        if v < 1:
            raise ValueError("batch_size must be at least 1")
        return v

    @property
    def attachment_text_cap_bytes(self) -> int:
        return self.attachment_text_cap_mb * 1024 * 1024


class IdentitySettings(_Section):
    me: list[str] = Field(default_factory=list)
    dot_insensitive_domains: list[str] = Field(
        default_factory=lambda: ["gmail.com", "googlemail.com"]
    )
    merge_suggest_threshold: float = 0.92
    role_mailbox_locals: list[str] = Field(
        default_factory=lambda: [
            "info", "office", "admin", "sales", "support", "help", "noreply",
            "no-reply", "donotreply", "postmaster", "mailer-daemon", "contact",
            "enquiries", "inquiries", "accounts", "billing", "hr", "jobs",
            "careers", "newsletter", "marketing", "webmaster", "abuse", "security",
        ]
    )
    max_display_names_per_address: int = 8

    @field_validator("me")
    @classmethod
    def _normalize_me(cls, v: list[str]) -> list[str]:
        return [a.strip().lower() for a in v if a.strip()]

    @field_validator("dot_insensitive_domains", "role_mailbox_locals")
    @classmethod
    def _lower_all(cls, v: list[str]) -> list[str]:
        return [x.strip().lower() for x in v if x.strip()]

    @field_validator("merge_suggest_threshold")
    @classmethod
    def _unit_interval(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("merge_suggest_threshold must be between 0.0 and 1.0")
        return v


class IntegritySettings(_Section):
    soft_gap_ratio: float = 0.15
    partial_parse_threshold: float = 0.95
    self_identity_share: float = 0.02
    duplicate_store_overlap: float = 0.90
    implausible_before_year: int = 1970

    @field_validator(
        "soft_gap_ratio",
        "partial_parse_threshold",
        "self_identity_share",
        "duplicate_store_overlap",
    )
    @classmethod
    def _unit_interval(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("must be a fraction between 0.0 and 1.0")
        return v


class ServerSettings(_Section):
    host: str = "127.0.0.1"
    port: int = 8765
    open_browser: bool = True

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        # Hard rule from the spec: this server is never reachable off this machine.
        if v not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(
                f"server.host must be 127.0.0.1 (got {v!r}). Recall refuses to "
                "listen on an address other people could reach."
            )
        return "127.0.0.1" if v == "localhost" else v

    @field_validator("port")
    @classmethod
    def _valid_port(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError("server.port must be between 1 and 65535")
        return v


class LoggingSettings(_Section):
    file_level: str = "DEBUG"
    console_level: str = "INFO"
    keep_days: int = 90

    @field_validator("file_level", "console_level")
    @classmethod
    def _known_level(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError(
                "must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL"
            )
        return v


class Settings(BaseModel):
    """The whole config, plus the paths derived from it."""

    model_config = ConfigDict(extra="forbid")

    workdir: WorkdirSettings = Field(default_factory=WorkdirSettings)
    scan: ScanSettings = Field(default_factory=ScanSettings)
    onedrive: OneDriveSettings = Field(default_factory=OneDriveSettings)
    extract: ExtractSettings = Field(default_factory=ExtractSettings)
    identity: IdentitySettings = Field(default_factory=IdentitySettings)
    integrity: IntegritySettings = Field(default_factory=IntegritySettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    # Not from the file: remembered so error messages can name it.
    source_path: Path | None = Field(default=None, exclude=True)

    # ---- derived paths ----------------------------------------------------

    @property
    def workdir_path(self) -> Path:
        p = Path(os.path.expandvars(self.workdir.path)).expanduser()
        if not p.is_absolute():
            base = self.source_path.parent if self.source_path else PROJECT_ROOT
            p = base / p
        return p.resolve()

    @property
    def db_path(self) -> Path:
        return self.workdir_path / "archive.db"

    @property
    def blobs_path(self) -> Path:
        return self.workdir_path / "blobs"

    @property
    def logs_path(self) -> Path:
        return self.workdir_path / "logs"

    @property
    def exports_path(self) -> Path:
        return self.workdir_path / "exports"

    def ensure_workdir(self) -> Path:
        """Create the working directory tree. Safe to call repeatedly."""
        for p in (
            self.workdir_path,
            self.blobs_path,
            self.logs_path,
            self.exports_path,
        ):
            p.mkdir(parents=True, exist_ok=True)
        return self.workdir_path

    # ---- scan roots -------------------------------------------------------

    def effective_scan_roots(self) -> list[Path]:
        """The folders a scan will actually walk.

        An explicit list in config.toml wins. Otherwise the spec default:
        every fixed drive, plus OneDrive, plus Outlook's own data folder.
        """
        if self.scan.roots:
            out: list[Path] = []
            for r in self.scan.roots:
                p = Path(os.path.expandvars(r)).expanduser()
                out.append(p)
            return _dedupe_paths(out)
        return default_scan_roots()


def default_scan_roots() -> list[Path]:
    """Fixed drives + OneDrive folders + %LOCALAPPDATA%\\Microsoft\\Outlook."""
    roots: list[Path] = list(fixed_drives())
    for od in onedrive_roots():
        roots.append(od)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        outlook = Path(local) / "Microsoft" / "Outlook"
        if outlook.is_dir():
            roots.append(outlook)
    return _dedupe_paths(roots)


def fixed_drives() -> list[Path]:
    """Drive roots that are local fixed disks (not CD, not network, not removable).

    Falls back to "every drive letter that exists" when the Windows API is not
    available, which is the case on non-Windows CI.
    """
    drives: list[Path] = []
    try:  # pragma: no cover - platform specific
        import ctypes

        DRIVE_FIXED = 3
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            if ctypes.windll.kernel32.GetDriveTypeW(root) == DRIVE_FIXED:
                drives.append(Path(root))
        return drives
    except (ImportError, AttributeError, OSError):
        return [
            Path(f"{letter}:\\")
            for letter in string.ascii_uppercase
            if Path(f"{letter}:\\").exists()
        ]


def onedrive_roots() -> list[Path]:
    """Every OneDrive folder this machine knows about."""
    found: list[Path] = []
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        val = os.environ.get(var)
        if val:
            p = Path(val)
            if p.is_dir():
                found.append(p)
    # Business tenants land as %USERPROFILE%\OneDrive - Contoso
    profile = os.environ.get("USERPROFILE")
    if profile:
        try:
            for child in Path(profile).iterdir():
                if child.is_dir() and child.name.lower().startswith("onedrive"):
                    found.append(child)
        except OSError:
            pass
    return _dedupe_paths(found)


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        key = str(p).rstrip("\\/").lower() or str(p).lower()
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def load_settings(path: str | Path | None = None) -> Settings:
    """Read config.toml. A missing file is fine — every default is usable."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        s = Settings()
        s.source_path = cfg_path
        return s

    try:
        with open(cfg_path, "rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"{cfg_path} could not be read: {exc}\n"
            "A setting is probably missing a quote or a comma. "
            "Undo your last edit and try again."
        ) from exc
    except OSError as exc:
        raise ConfigError(f"{cfg_path} could not be opened: {exc}") from exc

    try:
        s = Settings.model_validate(raw)
    except Exception as exc:
        raise ConfigError(
            f"A setting in {cfg_path} is not valid:\n{exc}"
        ) from exc
    s.source_path = cfg_path
    return s
