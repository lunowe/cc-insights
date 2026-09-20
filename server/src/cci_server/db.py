"""The connection pool and the migration runner.

Numbered .sql files applied in order and recorded in `schema_migrations`, the
same convention as `migrations/` in the client and for the same reason: a
shipped migration is never edited, you add a new one.

This runner is deliberately a near-copy of `cc_insights.db.migrate` rather than
an import of it. The server does not depend on the client package -- the client
installs with zero dependencies and that is a tested feature -- and the twenty
lines that would be shared are the twenty lines where a subtle difference
between the two stores would be hidden.

One thing the client's runner does that this one does not: `translate_ddl`,
which rewrites INTEGER to BIGINT on the way to PostgreSQL. These migrations say
BIGINT outright (see the header of 001), so there is nothing to translate and
no rewriter standing between the file and the database.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_MIGRATION_RE = re.compile(r"^(\d+)_.*\.sql$")


def now_ms() -> int:
    return int(time.time() * 1000)


def migrations_dir() -> Path:
    """The packaged copy, falling back to the checkout.

    `assets.py` in the client exists because resolving this by walking up from
    `__file__` works under `pip install -e .` and nowhere else: in a wheel the
    package sits in site-packages and two levels up is `lib/python3.x`. The
    failure was silent -- an empty directory meant "no migrations to apply",
    so `init` created a database with no tables and exited 0. Same trap here,
    same fix, and the same refusal to find none.
    """
    packaged = Path(__file__).resolve().parent / "migrations"
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[2] / "migrations"


def discover_migrations(directory: Path | None = None) -> list[tuple[int, Path]]:
    """The numbered .sql files in order, refusing to find none.

    "No migrations here" is never a legitimate answer for the shipped
    directory: it means the package was built without them, and the caller
    would go on to create an empty database and call it a success.
    """
    explicit = directory is not None
    directory = directory or migrations_dir()
    found: list[tuple[int, Path]] = []
    for p in sorted(directory.glob("*.sql")):
        m = _MIGRATION_RE.match(p.name)
        if m:
            found.append((int(m.group(1)), p))
    found.sort(key=lambda t: t[0])

    versions = [v for v, _ in found]
    if len(set(versions)) != len(versions):
        raise RuntimeError(f"duplicate migration version in {directory}")
    if not found and not explicit:
        raise RuntimeError(
            f"no schema migrations found in {directory}. This is a packaging "
            "bug: the installed package is missing its .sql files, and "
            "continuing would create an empty database."
        )
    return found


_SCHEMA_TABLE_EXISTS = (
    "SELECT 1 FROM information_schema.tables "
    "WHERE table_schema = current_schema() AND table_name = 'schema_migrations'"
)


def applied_versions(conn: psycopg.Connection) -> set[int]:
    if not conn.execute(_SCHEMA_TABLE_EXISTS).fetchone():
        return set()
    # Named, not positional: the pool sets `dict_row`, and a runner that only
    # works under the default tuple factory is a runner that breaks the day
    # somebody changes it.
    return {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}


def migrate(conn: psycopg.Connection, directory: Path | None = None) -> list[int]:
    """Apply pending migrations in order. Returns the versions applied.

    Each migration runs in one transaction. PostgreSQL DDL is transactional, so
    a failure part-way leaves the schema exactly as it was rather than half
    migrated -- which for a store holding two different privacy classes is the
    difference between a retry and an incident.
    """
    done = applied_versions(conn)
    newly: list[int] = []
    for version, path in discover_migrations(directory):
        if version in done:
            continue
        body = path.read_text()
        with conn.transaction():
            conn.execute(body)
            # version is an int off a filename and applied_at is a clock; no
            # user input reaches this string.
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (%s, %s)",
                (version, now_ms()),
            )
        newly.append(version)
    return newly


def open_pool(database_url: str, *, min_size: int = 1, max_size: int = 8) -> ConnectionPool:
    """A pool of connections in AUTOCOMMIT.

    Autocommit, and then explicit `with conn.transaction()` where a transaction
    is wanted, because the alternative -- an implicit transaction opened by the
    first statement -- means a read-only endpoint holds one open until the
    request ends. On a push of 191,475 rows that is a long time to pin a
    snapshot, and it is how a pool of eight runs out.
    """
    return ConnectionPool(
        database_url,
        min_size=min_size,
        max_size=max_size,
        kwargs={"autocommit": True, "row_factory": dict_row},
        open=True,
    )


class Database:
    """Everything the routes need from PostgreSQL, behind one object.

    A class rather than a module global so that a test can stand up a second
    database in the same process, and so `app.state` owns the lifetime instead
    of import order doing it.
    """

    def __init__(self, database_url: str, *, max_size: int = 8) -> None:
        self.url = database_url
        self.pool = open_pool(database_url, max_size=max_size)

    def migrate(self) -> list[int]:
        with self.pool.connection() as conn:
            return migrate(conn)

    def connection(self):
        return self.pool.connection()

    def close(self) -> None:
        self.pool.close()

    def healthy(self) -> bool:
        try:
            with self.pool.connection(timeout=2) as conn:
                conn.execute("SELECT 1")
            return True
        except Exception:
            return False


def chunks(seq: list, size: int) -> Iterator[list]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]
