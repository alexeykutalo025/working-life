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

from recall.scan import onedrive as onedrive_module
from recall.scan.fingerprint import hash_file
from recall.scan.onedrive import (
    FILE_ATTRIBUTE_OFFLINE,
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    FILE_ATTRIBUTE_RECALL_ON_OPEN,
    HydrationRefused,
    assert_not_onedrive,
    copy_destination,
    hydrate_to,
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


# --- downloading a copy, and keeping it -----------------------------------
#
# These use real files rather than mocked attribute bits. The point of
# hydrate_to is what it does with bytes on disk - what it leaves behind when
# it is interrupted, and what it refuses to keep - and none of that can be
# checked against a placeholder that does not exist.


def test_copy_lands_in_the_workdir_and_matches_the_original(tmp_path: Path):
    src = tmp_path / "mail.pst"
    src.write_bytes(b"x" * 300_000)
    dest = copy_destination(tmp_path / "cloud", 12, src)

    result = hydrate_to(src, dest, expected_size=src.stat().st_size)

    assert result.ok
    assert result.bytes_copied == 300_000
    assert dest.read_bytes() == src.read_bytes()
    assert dest.parent.name == "000012"
    assert dest.name == "mail.pst"


def test_the_copy_is_hashed_on_the_way_past(tmp_path: Path):
    """One read, not two. Hashing afterwards would re-read twenty gigabytes."""
    src = tmp_path / "mail.pst"
    src.write_bytes(b"contents worth hashing" * 500)
    dest = copy_destination(tmp_path / "cloud", 1, src)

    result = hydrate_to(src, dest, expected_size=src.stat().st_size)

    assert result.content_hash == hash_file(src).content_hash


def test_progress_is_reported_while_a_long_file_comes_down(tmp_path: Path):
    src = tmp_path / "big.pst"
    src.write_bytes(b"y" * 3_000_000)
    dest = copy_destination(tmp_path / "cloud", 2, src)

    seen: list[int] = []
    hydrate_to(
        src, dest, expected_size=src.stat().st_size,
        chunk_size=1024 * 1024, progress=seen.append,
    )

    assert len(seen) >= 3, "a 3 MB file in 1 MB chunks must report more than once"
    assert seen == sorted(seen)
    assert seen[-1] == 3_000_000


def test_stopping_midway_leaves_nothing_behind(tmp_path: Path):
    """Not the copy, and not the part-file either.

    A half-copied mailbox that looks complete is worse than no copy at all:
    it parses, it yields some records, and nothing anywhere says the rest of
    them were never there.
    """
    src = tmp_path / "big.pst"
    src.write_bytes(b"z" * 3_000_000)
    dest = copy_destination(tmp_path / "cloud", 3, src)

    calls = {"n": 0}

    def cancel() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    result = hydrate_to(
        src, dest, expected_size=src.stat().st_size,
        chunk_size=1024 * 1024, cancel=cancel,
    )

    assert result.canceled is True
    assert not dest.exists()
    assert list(dest.parent.glob("*.part")) == []


def test_a_short_download_is_an_error_and_is_not_kept(tmp_path: Path):
    """OneDrive can fail by handing back fewer bytes instead of raising."""
    src = tmp_path / "mail.pst"
    src.write_bytes(b"w" * 1000)
    dest = copy_destination(tmp_path / "cloud", 4, src)

    result = hydrate_to(src, dest, expected_size=5000)

    assert result.error is not None
    assert "stopped short" in result.error
    assert not dest.exists()
    assert list(dest.parent.glob("*.part")) == []


def test_a_copy_already_here_is_not_downloaded_again(tmp_path: Path):
    src = tmp_path / "mail.pst"
    src.write_bytes(b"v" * 2000)
    dest = copy_destination(tmp_path / "cloud", 5, src)
    hydrate_to(src, dest, expected_size=2000)

    seen: list[int] = []
    again = hydrate_to(src, dest, expected_size=2000, progress=seen.append)

    assert again.reused_existing is True
    assert again.bytes_copied == 0
    assert seen == [], "nothing should have been read at all"


def test_a_stale_part_file_is_discarded_rather_than_resumed(tmp_path: Path):
    """Appending to a part-file needs the download to restart at exactly the
    right offset. Getting that wrong gives a corrupt mailbox that reads as a
    short one, so it starts again instead."""
    src = tmp_path / "mail.pst"
    src.write_bytes(b"u" * 4000)
    dest = copy_destination(tmp_path / "cloud", 6, src)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.with_name(dest.name + ".part").write_bytes(b"rubbish from last time")

    result = hydrate_to(src, dest, expected_size=4000)

    assert result.ok
    assert dest.read_bytes() == src.read_bytes()


def test_it_refuses_to_copy_into_onedrive(tmp_path: Path, monkeypatch):
    """A workdir inside OneDrive would upload every copy straight back."""
    monkeypatch.setenv("OneDrive", str(tmp_path / "OneDrive"))
    src = tmp_path / "mail.pst"
    src.write_bytes(b"t" * 100)

    with pytest.raises(HydrationRefused):
        hydrate_to(src, tmp_path / "OneDrive" / "cloud" / "mail.pst")


def test_a_very_long_name_falls_back_to_the_id(tmp_path: Path):
    """Windows still refuses paths past 260 characters.

    Saved messages are named after the subject line, so a 200-character file
    name is an ordinary thing to find rather than a contrived one.
    """
    src = tmp_path / ("Re FW " + "a very long subject line " * 9 + ".msg")
    dest = copy_destination(tmp_path / "cloud", 77, src)

    assert len(str(dest)) <= 250
    assert dest.name == "000077.msg"


def test_an_ordinary_name_is_kept(tmp_path: Path):
    """The id is a fallback, not the normal case - the folder stays legible."""
    dest = copy_destination(tmp_path / "cloud", 8, tmp_path / "Sent Items.dbx")
    assert dest.name == "Sent Items.dbx"


# --- what the download costs in disk --------------------------------------


def test_one_drive_pays_twice_because_the_original_fills_in_too(tmp_path: Path):
    """Reading a placeholder hydrates the OneDrive original as well as writing
    the copy. Windows offers no way round it, so the plan says so up front
    rather than running out of room at file 300."""
    paths = [tmp_path / "a.pst"]
    plan = plan_hydration(
        paths, {str(paths[0]): 1000}, dest_dir=tmp_path / "workdir" / "cloud"
    )

    drive = (tmp_path.anchor or str(tmp_path)).lower()
    assert plan.needed_by_drive[drive] == 2000
    assert plan.needed_bytes == 2000
    assert plan.total_bytes == 1000


def test_two_drives_each_pay_once(tmp_path: Path, monkeypatch):
    src = Path("Q:/mail/a.pst")
    dest = Path("R:/workdir/cloud")
    monkeypatch.setattr(
        onedrive_module.shutil, "disk_usage",
        lambda p: SimpleNamespace(total=0, used=0, free=10**12),
    )

    plan = plan_hydration([src], {str(src): 1000}, dest_dir=dest)

    assert plan.needed_by_drive["q:\\"] == 1000
    assert plan.needed_by_drive["r:\\"] == 1000
    assert plan.needed_bytes == 1000


def test_every_drive_is_measured_not_just_the_first(tmp_path: Path, monkeypatch):
    """The free space used to be read off the first path's drive alone, so a
    full second drive looked fine."""
    free = {"q:\\": 10**12, "r:\\": 1}
    monkeypatch.setattr(
        onedrive_module.shutil, "disk_usage",
        lambda p: SimpleNamespace(total=0, used=0, free=free[str(p).lower()]),
    )

    src = Path("Q:/mail/a.pst")
    plan = plan_hydration([src], {str(src): 1000}, dest_dir=Path("R:/cloud"))

    assert plan.fits_on_disk is False
    assert plan.tight_drives == ["r:\\"]


def test_downloading_nothing_always_fits(tmp_path: Path):
    """An archive with no cloud files must not report "not enough room" for a
    download of zero bytes, which would disable the button that starts
    everything."""
    plan = plan_hydration([], {}, dest_dir=tmp_path)
    assert plan.fits_on_disk is True
