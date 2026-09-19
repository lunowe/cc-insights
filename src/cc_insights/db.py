"""SQLite connection handling and the migration runner.

Migrations are numbered .sql files applied in order and recorded in
schema_migrations. A shipped migration is never edited -- add a new one.
Everything here stays inside the SQL subset PostgreSQL also accepts; see the
portability contract at the top of migrations/001_init.sql.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
_MIGRATION_RE = re.compile(r"^(\d+)_.*\.sql$")


def now_ms() -> int:
    return int(time.time() * 1000)


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")   # concurrent reads during ingest
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def discover_migrations(directory: Path | None = None) -> list[tuple[int, Path]]:
    directory = directory or MIGRATIONS_DIR
    found: list[tuple[int, Path]] = []
    for p in sorted(directory.glob("*.sql")):
        m = _MIGRATION_RE.match(p.name)
        if m:
            found.append((int(m.group(1)), p))
    found.sort(key=lambda t: t[0])

    versions = [v for v, _ in found]
    if len(set(versions)) != len(versions):
        raise RuntimeError(f"duplicate migration version in {directory}")
    return found


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if not row:
        return set()
    return {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}


def migrate(conn: sqlite3.Connection, directory: Path | None = None) -> list[int]:
    """Apply pending migrations in order. Returns the versions applied."""
    done = applied_versions(conn)
    newly: list[int] = []
    for version, path in discover_migrations(directory):
        if version in done:
            continue
        # executescript() implicitly COMMITs any open transaction before it
        # runs, so an explicit conn.execute("BEGIN") around it is silently
        # discarded. The transaction has to live inside the script text.
        # SQLite DDL is transactional, so a failure mid-migration rolls back
        # cleanly. version and applied_at are ints, not user input.
        script = (
            "BEGIN;\n"
            f"{path.read_text()}\n"
            "INSERT INTO schema_migrations (version, applied_at) "
            f"VALUES ({int(version)}, {now_ms()});\n"
            "COMMIT;"
        )
        try:
            conn.executescript(script)
        except Exception:
            try:
                conn.executescript("ROLLBACK;")
            except Exception:
                pass
            raise
        newly.append(version)
    return newly


def upsert_host(conn: sqlite3.Connection, host_id: str, hostname: str, os_name: str) -> None:
    ts = now_ms()
    conn.execute(
        """
        INSERT INTO host (host_id, hostname, os, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (host_id) DO UPDATE SET
            hostname  = excluded.hostname,
            os        = excluded.os,
            last_seen = excluded.last_seen
        """,
        (host_id, hostname, os_name, ts, ts),
    )
