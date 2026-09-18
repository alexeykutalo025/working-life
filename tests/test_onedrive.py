"""Cloud-only files are recognised and never opened by accident.

The attribute bits are mocked, because creating a real OneDrive placeholder in
a test is not possible - and because the whole point of the check is that it
must be right without a real one to try it on.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from recall.scan.onedrive import (
    FILE_ATTRIBUTE_OFFLINE,
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    FILE_ATTRIBUTE_RECALL_ON_OPEN,
    HydrationRefused,
    assert_not_onedrive,
    is_onedrive_path,
    is_placeholder,
    placeholder_reason,
    plan_hydration,
)

FILE_ATTRIBUTE_NORMAL = 0x80
FILE_ATTRIBUTE_ARCHIVE = 0x20


def fake_stat(attrs):
    return SimpleNamespace(st_file_attributes=attrs, st_size=1234)


@pytest.mark.parametrize(
    "attrs",
    [
        FILE_ATTRIBUTE_OFFLINE,
        FILE_ATTRIBUTE_RECALL_ON_OPEN,
        FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
        FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_ARCHIVE,
        FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | FILE_ATTRIBUTE_NORMAL,
        FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN,
    ],
)
def test_any_cloud_bit_means_placeholder(attrs):
    assert is_placeholder(fake_stat(attrs)) is True


@pytest.mark.parametrize(
    "attrs",
    [0, FILE_ATTRIBUTE_NORMAL, FILE_ATTRIBUTE_ARCHIVE, 0x2000, 0x00000800],
)
def test_ordinary_files_are_not_placeholders(attrs):
    assert is_placeholder(fake_stat(attrs)) is False


def test_exact_bit_values_match_the_spec():
    """These three constants are the whole check. A typo here downloads 84 GB."""
    assert FILE_ATTRIBUTE_OFFLINE == 0x00001000
    assert FILE_ATTRIBUTE_RECALL_ON_OPEN == 0x00040000
    assert FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS == 0x00400000


def test_no_attribute_support_means_not_a_placeholder():
    """On a platform without st_file_attributes, cloud files do not exist."""
    assert is_placeholder(SimpleNamespace(st_size=10)) is False


def test_placeholder_reason_names_the_bits():
    reason = placeholder_reason(
        fake_stat(FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN)
    )
    assert "offline" in reason
    assert "recall-on-open" in reason


def test_placeholder_reason_is_none_for_ordinary_files():
    assert placeholder_reason(fake_stat(FILE_ATTRIBUTE_NORMAL)) is None


def test_real_local_file_is_not_a_placeholder(tmp_path: Path):
    """The check against a genuine file on this disk, not a mock."""
    p = tmp_path / "real.pst"
    p.write_bytes(b"!BDN" + b"\x00" * 100)
    assert is_placeholder(os.stat(p)) is False


# --- path rules -----------------------------------------------------------


def test_onedrive_path_detected_by_root(tmp_path: Path, monkeypatch):
    od = tmp_path / "OneDrive"
    inner = od / "Documents"
    inner.mkdir(parents=True)
    monkeypatch.setenv("OneDrive", str(od))
    assert is_onedrive_path(inner / "mail.pst") is True


def test_onedrive_path_detected_by_name_when_env_missing(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OneDrive", raising=False)
    monkeypatch.delenv("OneDriveConsumer", raising=False)
    monkeypatch.delenv("OneDriveCommercial", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    p = tmp_path / "OneDrive - Contract Marketing" / "old.pst"
    p.parent.mkdir(parents=True)
    assert is_onedrive_path(p) is True


def test_ordinary_path_is_not_onedrive(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OneDrive", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert is_onedrive_path(tmp_path / "Documents" / "mail.pst") is False


def test_writing_into_onedrive_is_refused(tmp_path: Path, monkeypatch):
    """Spec section 14. Recall must never write into a synced folder."""
    od = tmp_path / "OneDrive"
    od.mkdir()
    monkeypatch.setenv("OneDrive", str(od))
    with pytest.raises(HydrationRefused, match="OneDrive"):
        assert_not_onedrive(od / "workdir")


def test_writing_outside_onedrive_is_allowed(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OneDrive", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert_not_onedrive(tmp_path / "workdir")  # must not raise


# --- hydration planning ---------------------------------------------------


def test_plan_totals_the_sizes(tmp_path: Path):
    paths = [tmp_path / "a.pst", tmp_path / "b.pst"]
    sizes = {str(paths[0]): 1000, str(paths[1]): 2000}
    plan = plan_hydration(paths, sizes)
    assert plan.total_bytes == 3000
    assert "2 files would be downloaded" in plan.describe()


def test_plan_of_nothing_costs_nothing():
    plan = plan_hydration([], {})
    assert plan.total_bytes == 0
    assert plan.paths == []


def test_plan_refuses_when_it_would_not_fit(tmp_path: Path):
    paths = [tmp_path / "huge.pst"]
    plan = plan_hydration(paths, {str(paths[0]): 10**15})
    assert plan.fits_on_disk is False
