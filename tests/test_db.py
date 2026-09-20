"""Migration runner and schema guarantees."""

import sqlite3

import pytest

from cc_insights import db

EXPECTED_TABLES = {
    "schema_migrations", "host", "project", "session",
    "thread", "event", "span", "ingest_file",
}


def test_migrate_creates_all_tables(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert EXPECTED_TABLES <= names


def test_migrate_is_idempotent(conn, tmp_path):
    assert db.migrate(conn) == []  # already applied by the fixture
    # Version-agnostic on purpose: a new migration must not break this test.
    assert sorted(db.applied_versions(conn)) == [v for v, _ in db.discover_migrations()]


def test_migrate_records_versions(conn):
    rows = list(conn.execute("SELECT version, applied_at FROM schema_migrations ORDER BY version"))
    assert [r["version"] for r in rows] == [v for v, _ in db.discover_migrations()]
    assert rows, "at least one migration must exist"
    assert all(r["applied_at"] > 1_600_000_000_000 for r in rows)  # epoch MS, not seconds


def test_foreign_keys_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO session (id, native_id, source, host_id, started_at, ended_at)"
            " VALUES ('s','n','codex','nonexistent-host',0,0)"
        )


def test_present_day_epoch_ms_round_trips(conn):
    """Regression: PostgreSQL INTEGER is int4 (max 2.1e9) but epoch-ms is ~1.79e12.
    Every timestamp column must be BIGINT on Postgres. Guard the value range here
    so the Postgres migration is written against a test that already fails on int4."""
    ts = 1_789_000_000_000
    assert ts > 2_147_483_647, "test value must exceed int4 to be meaningful"
    db.upsert_host(conn, "h", "mac", "darwin")
    conn.execute(
        "INSERT INTO session (id, native_id, source, host_id, started_at, ended_at)"
        " VALUES ('s','n','codex','h',?,?)", (ts, ts + 1000),
    )
    got = conn.execute("SELECT started_at, ended_at FROM session").fetchone()
    assert got["started_at"] == ts and got["ended_at"] == ts + 1000


def test_upsert_host_is_idempotent(conn):
    db.upsert_host(conn, "h", "mac", "darwin")
    db.upsert_host(conn, "h", "mac-renamed", "darwin")
    rows = list(conn.execute("SELECT hostname FROM host"))
    assert len(rows) == 1 and rows[0]["hostname"] == "mac-renamed"


def test_migration_sql_stays_postgres_portable():
    """The schema must not drift into SQLite-only syntax."""
    import re
    raw = "\n".join(p.read_text() for _, p in db.discover_migrations())
    sql = re.sub(r"--[^\n]*", "", raw).lower()  # strip comments: they discuss these very terms
    for banned in ("autoincrement", "strftime", "julianday", "without rowid",
                   "insert or replace", "pragma", "datetime("):
        assert banned not in sql, f"non-portable SQL in migrations: {banned}"
