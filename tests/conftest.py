"""Shared fixtures.

Every test gets its own workdir under tmp_path, so no test can see another
test's archive and none of them can touch the real one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from recall import db as db_module  # noqa: E402
from recall.config import Settings, WorkdirSettings  # noqa: E402


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(workdir=WorkdirSettings(path=str(tmp_path / "workdir")))
    s.source_path = tmp_path / "config.toml"
    s.ensure_workdir()
    # Never drive Outlook from a unit test. A deliberately-corrupt .pst makes
    # Outlook put up a repair dialog, and the guard then waits out its stall
    # timeout on every such fixture - minutes per test, for behaviour that is
    # tested separately behind the needs_outlook mark.
    s.extract.pst_backend = "pypff"
    s.extract.cross_check_backends = False
    return s


@pytest.fixture
def conn(settings: Settings):
    c = db_module.connect(settings.db_path)
    yield c
    c.close()


@pytest.fixture
def fixtures_dir(tmp_path: Path) -> Path:
    """A freshly generated set of synthetic source files."""
    from tests.fixtures.generate import generate_all

    out = tmp_path / "fixtures"
    generate_all(out)
    return out


def has_real_fixtures() -> bool:
    return (Path(__file__).parent / "fixtures" / "real").is_dir()


def has_outlook_com() -> bool:
    """Is Outlook usable for automation, decided cheaply and once.

    Deliberately does NOT call Dispatch at collection time. Starting Outlook can
    block forever behind a hidden dialog, which would hang the whole test run
    before a single test executed - this happened during development.
    """
    from recall.comguard import is_outlook_registered

    return is_outlook_registered()


needs_real_files = pytest.mark.skipif(
    not has_real_fixtures(),
    reason="no tests/fixtures/real/ directory - drop real PST/OST files there to enable",
)

needs_outlook = pytest.mark.skipif(
    not has_outlook_com(), reason="Microsoft Outlook is not installed on this machine"
)
