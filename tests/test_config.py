"""config.toml is forgiving about what is missing, strict about what is wrong."""

from __future__ import annotations

from pathlib import Path

import pytest

from recall.config import (
    ConfigError,
    Settings,
    default_scan_roots,
    load_settings,
)


def test_missing_file_gives_usable_defaults(tmp_path: Path):
    s = load_settings(tmp_path / "nope.toml")
    assert s.server.host == "127.0.0.1"
    assert s.server.port == 8765
    assert s.extract.batch_size == 1000
    assert s.identity.me == []


def test_shipped_config_loads():
    """The config.toml in the repo must always parse. It is the user's file."""
    repo_config = Path(__file__).resolve().parent.parent / "config.toml"
    s = load_settings(repo_config)
    assert s.source_path == repo_config
    assert s.scan.extensions  # non-empty
    assert ".pst" in s.scan.extensions


def test_broken_toml_names_the_file(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text('[server]\nport = "not a number\n', encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_settings(p)
    assert "config.toml" in str(exc.value)


def test_unknown_key_is_rejected(tmp_path: Path):
    """A typo should be reported, not silently ignored."""
    p = tmp_path / "config.toml"
    p.write_text("[server]\nprot = 8765\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(p)


def test_non_loopback_host_is_refused(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text('[server]\nhost = "0.0.0.0"\n', encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_settings(p)
    assert "127.0.0.1" in str(exc.value)


def test_localhost_is_normalized(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text('[server]\nhost = "localhost"\n', encoding="utf-8")
    assert load_settings(p).server.host == "127.0.0.1"


def test_extensions_are_normalized(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text('[scan]\nextensions = ["PST", ".Ost", " .msg "]\n', encoding="utf-8")
    assert load_settings(p).scan.extensions == [".pst", ".ost", ".msg"]


def test_me_addresses_are_lowercased(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text('[identity]\nme = ["Tim@Example.COM", "  ", "b@c.d"]\n', encoding="utf-8")
    assert load_settings(p).identity.me == ["tim@example.com", "b@c.d"]


def test_bad_threshold_is_rejected(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text("[identity]\nmerge_suggest_threshold = 4.2\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(p)


def test_workdir_is_relative_to_config_file(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text('[workdir]\npath = "store"\n', encoding="utf-8")
    s = load_settings(p)
    assert s.workdir_path == (tmp_path / "store").resolve()


def test_ensure_workdir_creates_the_tree(tmp_path: Path):
    s = Settings()
    s.source_path = tmp_path / "config.toml"
    s.workdir.path = "wd"
    s.ensure_workdir()
    assert s.workdir_path.is_dir()
    assert s.blobs_path.is_dir()
    assert s.logs_path.is_dir()
    assert s.exports_path.is_dir()


def test_effective_roots_uses_explicit_list(tmp_path: Path):
    s = Settings()
    s.scan.roots = [str(tmp_path)]
    assert s.effective_scan_roots() == [tmp_path]


def test_effective_roots_falls_back_to_defaults():
    s = Settings()
    assert s.effective_scan_roots() == default_scan_roots()


def test_default_roots_are_not_empty():
    """Spec section 5: fixed drives plus OneDrive plus Outlook's own folder."""
    roots = default_scan_roots()
    assert roots, "a machine always has at least one drive"
    assert len({str(r).lower() for r in roots}) == len(roots), "no duplicate roots"
