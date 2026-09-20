"""Connection handling and the migration runner, for both engines.

Migrations are numbered .sql files applied in order and recorded in
schema_migrations. A shipped migration is never edited -- add a new one.
Everything here stays inside the SQL subset PostgreSQL also accepts; see the
portability contract at the top of migrations/001_init.sql.

One thing that contract cannot express in the .sql files themselves is the
integer width. SQLite's INTEGER is 64-bit; PostgreSQL's is int4, which tops
out at 2.1e9 -- and epoch-ms is ~1.79e12. So every INTEGER becomes BIGINT on
the way to PostgreSQL. That is a translation, not a schema fork: the files
stay the single source of truth and neither engine gets its own copy to drift.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
_MIGRATION_RE = re.compile(r"^(\d+)_.*\.sql$")

SQLITE = "sqlite"
POSTGRES = "postgres"

# Word-boundary so INTEGER inside a longer identifier is left alone. Comments
# are rewritten too, which is harmless and beats a parser.
_INTEGER_RE = re.compile(r"\bINTEGER\b", re.IGNORECASE)


def translate_ddl(sql: str, dialect: str) -> str:
    """One migration's text, in the dialect that is about to run it.

    Blanket INTEGER -> BIGINT rather than a per-column list. Every INTEGER in
    this schema is an epoch-ms timestamp, a byte count, a row count or a
    0/1 flag; the first two overflow int4 and the rest do not care, so there is
    no column where the wider type is wrong and several where the narrower one
    silently truncates history.
    """
    if dialect == POSTGRES:
        return _INTEGER_RE.sub("BIGINT", sql)
    return sql


def to_dialect(sql: str, dialect: str) -> str:
    """Rewrite `?` placeholders for `dialect`.

    Deliberately naive: it assumes no `?` or `%` inside a string literal, which
    holds for the DDL and the sync statements that use it. It is not a general
    query translator, and the read path (metrics, stats, derive) does not go
    through it -- those still run on SQLite only.
    """
    if dialect == POSTGRES:
        return sql.replace("%", "%%").replace("?", "%s")
    return sql


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


_TABLE_EXISTS = {
    SQLITE: "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'",
    POSTGRES: (
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = 'schema_migrations'"
    ),
}


def applied_versions(conn, dialect: str = SQLITE) -> set[int]:
    if not conn.execute(_TABLE_EXISTS[dialect]).fetchone():
        return set()
    return {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}


def migrate(
    conn,
    directory: Path | None = None,
    *,
    only_through: int | None = None,
    dialect: str = SQLITE,
) -> list[int]:
    """Apply pending migrations in order. Returns the versions applied.

    `only_through` stops after that version, which is how a test stands a
    database up as an older release left it and then migrates it forward for
    real. A migration that moves existing data -- 004 moves the probe cache off
    `project` -- is only proven by running it against data a previous schema
    wrote, and reproducing that by hand would test the reproduction.

    Both engines run each migration in one transaction, so a failure part-way
    leaves the schema exactly as it was. They get there differently, which is
    why the two branches exist rather than one clever one.
    """
    done = applied_versions(conn, dialect)
    newly: list[int] = []
    for version, path in discover_migrations(directory):
        if version in done:
            continue
        if only_through is not None and version > only_through:
            break
        body = translate_ddl(path.read_text(), dialect)
        # version and applied_at are ints, not user input.
        record = (
            "INSERT INTO schema_migrations (version, applied_at) "
            f"VALUES ({int(version)}, {now_ms()});"
        )
        if dialect == POSTGRES:
            _apply_postgres(conn, body, record)
        else:
            _apply_sqlite(conn, body, record)
        newly.append(version)
    return newly


def _apply_sqlite(conn: sqlite3.Connection, body: str, record: str) -> None:
    # executescript() implicitly COMMITs any open transaction before it runs,
    # so an explicit conn.execute("BEGIN") around it is silently discarded. The
    # transaction has to live inside the script text. SQLite DDL is
    # transactional, so a failure mid-migration rolls back cleanly.
    try:
        conn.executescript(f"BEGIN;\n{body}\n{record}\nCOMMIT;")
    except Exception:
        try:
            conn.executescript("ROLLBACK;")
        except Exception:
            pass
        raise


def _apply_postgres(conn, body: str, record: str) -> None:
    # psycopg runs a multi-statement string in one implicit transaction and
    # PostgreSQL DDL is transactional, so this needs no BEGIN of its own -- and
    # must not have one, because an explicit COMMIT inside the string would end
    # the transaction psycopg is managing and leave it confused about state.
    with conn.transaction():
        conn.execute(body)
        conn.execute(record)


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
