import sqlite3
from pathlib import Path

import pytest

from cc_insights import db, scheduler


@pytest.fixture(autouse=True)
def _no_real_crontab(monkeypatch):
    """No test reaches the developer's or the CI runner's real crontab.

    `crontab -` replaces the whole table, so a test that wrote through the
    real binary would delete somebody's jobs. Tests that exercise the cron
    backend opt in with the `crontab` fixture in test_scheduler.py, which
    points this at a fake backed by a file in tmp_path.
    """
    monkeypatch.setattr(scheduler, "_crontab_bin", lambda: None)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = db.connect(tmp_path / "test.db")
    db.migrate(c)
    yield c
    c.close()
