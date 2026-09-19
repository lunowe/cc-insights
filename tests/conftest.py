import sqlite3
from pathlib import Path

import pytest

from cc_insights import db


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = db.connect(tmp_path / "test.db")
    db.migrate(c)
    yield c
    c.close()
