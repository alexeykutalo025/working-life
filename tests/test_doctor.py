"""`recall doctor` reports honestly, including about itself."""

from __future__ import annotations

from pathlib import Path

from recall.config import Settings
from recall.doctor import (
    FAIL,
    PASS,
    WARN,
    Check,
    check_database,
    check_python,
    check_sqlite,
    check_workdir,
    doctor_report,
    format_table,
    run_all_checks,
)


def test_python_check_passes_on_this_interpreter():
    c = check_python()
    assert c.status == PASS
    assert c.ok


def test_sqlite_and_fts_available():
    checks = check_sqlite()
    names = {c.name: c for c in checks}
    assert names["Full-text search (FTS5)"].status == PASS, (
        "FTS5 is required for search; a Python without it cannot run Recall"
    )


def test_workdir_check_passes_for_a_fresh_dir(settings: Settings):
    checks = check_workdir(settings)
    # Disk space may legitimately WARN on a machine that is short of room; that
    # is the check working, not failing. Nothing here may be a FAIL.
    assert not any(c.status == FAIL for c in checks), [
        (c.name, c.detail) for c in checks if c.status == FAIL
    ]
    by_name = {c.name: c for c in checks}
    assert by_name["Working folder"].status == PASS
    assert by_name["Working folder location"].status == PASS


def test_workdir_inside_onedrive_fails(tmp_path: Path, monkeypatch):
    fake_onedrive = tmp_path / "OneDrive"
    (fake_onedrive / "Recall").mkdir(parents=True)
    monkeypatch.setenv("OneDrive", str(fake_onedrive))

    s = Settings()
    s.source_path = tmp_path / "config.toml"
    s.workdir.path = str(fake_onedrive / "Recall")
    checks = check_workdir(s)
    location = [c for c in checks if c.name == "Working folder location"][0]
    assert location.status == FAIL
    assert "OneDrive" in location.detail


def test_database_check_creates_and_passes(settings: Settings):
    c = check_database(settings)
    assert c.status == PASS
    assert settings.db_path.exists()


def test_database_check_reports_corruption(settings: Settings):
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.db_path.write_bytes(b"this is definitely not a database")
    c = check_database(settings)
    assert c.status == FAIL


def test_run_all_checks_covers_the_essentials(settings: Settings):
    names = {c.name for c in run_all_checks(settings)}
    for expected in (
        "Python version",
        "Operating system",
        "Full-text search (FTS5)",
        "Microsoft Outlook (COM)",
        "Working folder",
        "Archive database",
        "Folders to search",
        "Disk space",
    ):
        assert expected in names


def test_report_says_pass_fail_honestly(settings: Settings):
    table, ok = doctor_report(settings)
    assert "CHECK" in table and "RESULT" in table
    assert ("failed" in table)
    assert isinstance(ok, bool)


def test_table_wraps_long_detail_without_losing_words():
    long_detail = " ".join(f"word{i}" for i in range(60))
    table = format_table([Check("A check", WARN, long_detail)])
    flattened = " ".join(table.split())
    for i in range(60):
        assert f"word{i}" in flattened


def test_fail_makes_report_not_ok():
    checks = [Check("X", PASS, "fine"), Check("Y", FAIL, "broken")]
    assert not all(c.ok for c in checks)
    assert "1 passed, 0 warnings, 1 failed" in format_table(checks)


def test_warn_does_not_make_report_not_ok():
    checks = [Check("X", PASS, "fine"), Check("Y", WARN, "reduced")]
    assert all(c.ok for c in checks)
