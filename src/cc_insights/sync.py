"""Multi-machine sync: push what this host owns, pull what the others did.

The shape follows from two things that were already true.

**Every id is a content hash.** The same logical row computes the same id on
any machine, so a row pushed twice collapses instead of duplicating and the
whole transfer is an upsert with no coordination, no sequence numbers and no
merge. Re-running a push changes nothing.

**Local stays the source of truth.** PostgreSQL is a meeting point, not a
replacement: each machine ingests its own logs into its own SQLite file,
pushes the rows it owns, and pulls everyone else's back down. The dashboard,
`metrics`, `stats` and `derive` keep reading SQLite and do not know any of this
happened -- which is the reason the read path needs no porting at all.

Ownership is explicit. A host pushes the sessions, threads, events, spans and
probe rows that are *its own*; `project` and `project_group` are shared by
nature and pushed whole. Nothing is ever deleted remotely.

Not everything travels. `EXCLUDED` lists what stays behind and why, and a test
reads the live schema so a new table has to be ruled on rather than quietly
forgotten -- the first version of that test compared this module to a hardcoded
copy of itself and sailed through a merge that added three tables.

SCOPE: this is one person's several machines, and paths travel because it is
all the same disk. Sharing with OTHER PEOPLE is a different pipe with different
rules -- see `redact.py` and docs/REDACTION.md. Do not widen this one to reach
them: a `root_path` here still says `~/Coding/<client-name>`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

from cc_insights import db

BATCH = 500


def connect_remote(url: str):
    """Open the shared database and bring its schema up to date.

    psycopg is an optional dependency: the whole point of this project is that
    it works on one machine with nothing installed, and a driver you only need
    for sync should not be a condition of `pip install`.

    `autocommit=True` puts psycopg in the mode where `conn.transaction()` is an
    explicit block rather than a nested savepoint, which is what `_transaction`
    and the migration runner both assume.
    """
    try:
        import psycopg
    except ModuleNotFoundError as exc:      # pragma: no cover - env-dependent
        raise RuntimeError(
            "sync needs the PostgreSQL driver, which is not installed.\n"
            "    pip install 'cc-insights[postgres]'"
        ) from exc

    conn = psycopg.connect(url, autocommit=True)
    db.migrate(conn, dialect=db.POSTGRES)
    return conn


@dataclass(frozen=True)
class Table:
    """One table's transfer rules.

    `owner_filter` is the WHERE clause that selects the rows a host owns, and
    `None` means the table is shared and travels whole. `set_clause` overrides
    the default "overwrite every non-key column" for the columns where a blind
    overwrite would lose something.
    """

    name: str
    columns: tuple[str, ...]
    key: tuple[str, ...]
    owner_filter: str | None = None
    set_clause: str | None = None
    parent: str | None = None          # self-reference to order rows by

    @property
    def updatable(self) -> tuple[str, ...]:
        return tuple(c for c in self.columns if c not in self.key)


#: Push order is FK order. `project_group` precedes `project` because
#: `project.group_id` points at it, and `thread` precedes `event`/`span`.
TABLES: tuple[Table, ...] = (
    Table(
        "host",
        ("host_id", "hostname", "os", "first_seen", "last_seen", "account_id"),
        ("host_id",),
        # first_seen is the earliest this machine was ever seen; a later push
        # must not move it forward.
        #
        # account_id travels, and only ever forward from NULL. Sync is one
        # person's own machines, so learning which account a host was claimed
        # by is not a disclosure. But a machine that has not run `cci login`
        # pushes NULL, and a blind overwrite would then un-claim every other
        # machine from the account they are signed in to -- the same shape of
        # bug as a push unpinning a project the pusher never heard about.
        set_clause=(
            "hostname = excluded.hostname, os = excluded.os, "
            "first_seen = CASE WHEN host.first_seen < excluded.first_seen "
            "THEN host.first_seen ELSE excluded.first_seen END, "
            "last_seen = CASE WHEN host.last_seen > excluded.last_seen "
            "THEN host.last_seen ELSE excluded.last_seen END, "
            "account_id = coalesce(excluded.account_id, host.account_id)"
        ),
    ),
    Table(
        "project_group",
        ("group_id", "name", "origin", "match_key", "remote_url", "forge", "owner",
         "repo", "web_url", "created_at", "updated_at"),
        ("group_id",),
    ),
    Table(
        "project",
        ("project_id", "root_path", "name", "group_id", "group_pinned"),
        ("project_id",),
        # A pin is a human saying where this project belongs. A machine that
        # has never been told must not silently unpin it, and must not move it
        # out of the group the human chose -- which is exactly the rule
        # `cci group auto` already follows locally.
        set_clause=(
            "root_path = excluded.root_path, name = excluded.name, "
            "group_id = CASE WHEN project.group_pinned = 1 "
            "THEN project.group_id ELSE excluded.group_id END, "
            "group_pinned = CASE WHEN project.group_pinned = 1 "
            "THEN 1 ELSE excluded.group_pinned END"
        ),
    ),
    Table(
        "project_probe",
        ("project_id", "host_id", "git_remote", "git_common_dir", "path_exists",
         "detected_at"),
        ("project_id", "host_id"),
        owner_filter="host_id = ?",
    ),
    Table(
        "session",
        ("id", "native_id", "source", "host_id", "project_id", "cwd", "git_branch",
         "cli_version", "started_at", "ended_at", "event_count", "active_ms"),
        ("id",),
        owner_filter="host_id = ?",
    ),
    Table(
        "thread",
        ("id", "native_id", "session_id", "parent_thread_id", "is_subagent",
         "agent_name", "started_at", "ended_at", "event_count", "active_ms"),
        ("id",),
        owner_filter="session_id IN (SELECT id FROM session WHERE host_id = ?)",
        parent="parent_thread_id",
    ),
    Table(
        "event",
        ("id", "session_id", "thread_id", "native_event_id", "ts", "ordinal", "kind",
         "model", "tool_name", "tool_use_id", "input_tokens", "output_tokens",
         "cache_read_tokens", "cache_write_tokens", "cache_write_1h_tokens"),
        ("id",),
        owner_filter="session_id IN (SELECT id FROM session WHERE host_id = ?)",
    ),
    Table(
        "span",
        ("id", "session_id", "thread_id", "started_at", "ended_at", "event_count",
         "attended"),
        ("id",),
        owner_filter="session_id IN (SELECT id FROM session WHERE host_id = ?)",
    ),
)


#: Tables deliberately NOT synced, and why. `test_sync` reads the live schema
#: and fails when a table is in neither this list nor TABLES, so a migration
#: has to answer the question rather than be quietly forgotten.
EXCLUDED: dict[str, str] = {
    "schema_migrations": "about one database's own schema, not about any work",
    "ingest_file": (
        "how far this machine read each local log file. Bookkeeping about a "
        "disk nobody else can see, whose only content is a full local path."
    ),
    "model_price": (
        "rates are resolved locally from the committed catalog, the shipped "
        "overrides and `cci price set` -- which v1 deliberately keeps as a "
        "layer no sync touches, so that a human's correction is owned by the "
        "human and not by whichever machine pushed last."
    ),
    "event_cost": (
        "derived from events and the local price table, both of which the "
        "other machine already has. Recomputing costs one `cci cost` and "
        "keeps each machine's prices authoritative for its own view; shipping "
        "it would silently impose this machine's rates on every other."
    ),
    "event_unpriced": "derived alongside event_cost, for the same reason",
}


@dataclass
class SyncStats:
    """Rows moved per table. Counts are rows sent, not rows that changed.

    A clean re-run still reports the same numbers: the transfer is an upsert,
    and knowing how much crossed the wire is the useful figure. What actually
    changed is not knowable without reading every row back, which would cost
    more than the sync.
    """

    direction: str = "push"
    rows: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.rows.values())


# --------------------------------------------------------------------------
# statement building
# --------------------------------------------------------------------------


def upsert_sql(table: Table, dialect: str) -> str:
    cols = ", ".join(table.columns)
    marks = ", ".join("?" for _ in table.columns)
    keys = ", ".join(table.key)
    sets = table.set_clause or ", ".join(
        f"{c} = excluded.{c}" for c in table.updatable
    )
    if not sets:   # every column is part of the key; nothing left to update
        action = "DO NOTHING"
    else:
        action = f"DO UPDATE SET {sets}"
    sql = (
        f"INSERT INTO {table.name} ({cols}) VALUES ({marks}) "
        f"ON CONFLICT ({keys}) {action}"
    )
    return db.to_dialect(sql, dialect)


def select_sql(table: Table, *, owned: bool) -> str:
    cols = ", ".join(table.columns)
    sql = f"SELECT {cols} FROM {table.name}"
    if owned and table.owner_filter:
        sql += f" WHERE {table.owner_filter}"
    return sql


def set_fragments(table: Table) -> dict[str, str]:
    """`set_clause` split into one assignment per column.

    The HTTP transport in `remote.py` upserts a *subset* of the columns -- the
    account server does not model `host.account_id`, and an optional column may
    be absent -- and a whole-clause string cannot be narrowed: an assignment
    mentioning `excluded.account_id` is invalid SQL the moment that column is
    not in the INSERT. Splitting here rather than writing a second copy of the
    merge rules keeps one source of truth for the three places a blind
    overwrite would lose something.

    `{}` for a table with no custom clause, which means "overwrite every
    non-key column" and needs no fragments to say so.

    The split counts parentheses rather than splitting on every ", ", and
    that is not defensive coding -- `host.account_id` is
    `coalesce(excluded.account_id, host.account_id)`, whose comma is inside
    the call. A naive split produced a fragment keyed
    `host.account_id)` and silently dropped the real assignment.
    """
    if not table.set_clause:
        return {}
    clause = table.set_clause
    pieces: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(clause):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            pieces.append(clause[start:i])
            start = i + 1
    pieces.append(clause[start:])

    out: dict[str, str] = {}
    for piece in pieces:
        fragment = piece.strip()
        column, _, _rest = fragment.partition(" = ")
        out[column.strip()] = fragment
    return out


def order_by_parent(table: Table, rows: list[tuple],
                    columns: Sequence[str] | None = None) -> list[tuple]:
    """Parents before children, for a table that references itself.

    `thread.parent_thread_id` points at another thread, and a subagent can be
    nested several levels deep (a workflow's agents are children of an agent).
    Inserting a child first fails the foreign key, and relying on the order the
    rows happen to come back in is how that becomes an intermittent failure
    that only shows up on somebody else's machine.

    `columns` says what the rows actually carry, for callers whose projection
    is not `table.columns` -- the HTTP transport sends and receives a subset.
    Defaulting to `table.columns` keeps every existing caller unchanged.
    """
    if table.parent is None:
        return rows
    layout = tuple(columns) if columns is not None else table.columns
    id_at = layout.index(table.key[0])
    parent_at = layout.index(table.parent)

    pending = {r[id_at]: r for r in rows}
    placed: list[tuple] = []
    done: set = set()

    def place(row: tuple) -> None:
        rid = row[id_at]
        if rid in done:
            return
        done.add(rid)            # before recursing: a cycle must not hang
        parent = row[parent_at]
        if parent is not None and parent in pending and parent not in done:
            place(pending[parent])
        placed.append(row)

    for row in rows:
        place(row)
    return placed


def _batched(rows: Sequence[tuple], size: int = BATCH) -> Iterator[Sequence[tuple]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def _chunks(cursor, table: Table, size: int) -> Iterator[list[tuple]]:
    """`table`'s rows in insertable order, `size` at a time.

    `event` is the big one -- 187k rows on the author's corpus and growing --
    so it is streamed off the cursor rather than materialized. `thread` is the
    exception: ordering parents before children means seeing every row first,
    which is affordable precisely because threads are three orders of
    magnitude fewer than events.
    """
    if table.parent is not None:
        rows = order_by_parent(table, [tuple(r) for r in cursor.fetchall()])
        yield from (list(c) for c in _batched(rows, size))
        return
    while True:
        chunk = cursor.fetchmany(size)
        if not chunk:
            return
        yield [tuple(r) for r in chunk]


# --------------------------------------------------------------------------
# transfer
# --------------------------------------------------------------------------


def _transfer(
    src: Any,
    dst: Any,
    *,
    dst_dialect: str,
    host_id: str | None,
    direction: str,
    batch: int = BATCH,
) -> SyncStats:
    stats = SyncStats(direction=direction)
    for table in TABLES:
        owned = host_id is not None
        sql = select_sql(table, owned=owned)
        params = (host_id,) if (owned and table.owner_filter) else ()
        read = src.execute(sql, params)

        statement = upsert_sql(table, dst_dialect)
        # Through a cursor, not the connection: sqlite3 puts executemany on
        # both, psycopg only on the cursor.
        write = dst.cursor()
        sent = 0
        for chunk in _chunks(read, table, batch):
            write.executemany(statement, chunk)
            sent += len(chunk)
        if sent:
            stats.rows[table.name] = sent
    return stats


def push(
    local: sqlite3.Connection,
    remote: Any,
    host_id: str,
    *,
    dialect: str = db.POSTGRES,
    batch: int = BATCH,
) -> SyncStats:
    """Send the rows `host_id` owns to the shared database.

    One transaction: a half-pushed host is a host whose sessions exist without
    their events, and every count on the dashboard would be wrong in a way that
    looks plausible.
    """
    with _transaction(remote, dialect):
        return _transfer(
            local, remote, dst_dialect=dialect, host_id=host_id,
            direction="push", batch=batch,
        )


def pull(
    local: sqlite3.Connection,
    remote: Any,
    *,
    dialect: str = db.POSTGRES,
    batch: int = BATCH,
) -> SyncStats:
    """Bring every host's rows down into the local database.

    Not filtered by host: the point is to see the other machines. This host's
    own rows come back too and land on themselves, because the ids are hashes
    of the same content.
    """
    with _transaction(local, db.SQLITE):
        return _transfer(
            remote, local, dst_dialect=db.SQLITE, host_id=None,
            direction="pull", batch=batch,
        )


class _transaction:
    """One transaction over either engine, without leaking which one it is."""

    def __init__(self, conn: Any, dialect: str) -> None:
        self._conn = conn
        self._dialect = dialect
        self._inner = None

    def __enter__(self):
        if self._dialect == db.POSTGRES:
            self._inner = self._conn.transaction()
            self._inner.__enter__()
        else:
            # `connect()` opens SQLite in autocommit (isolation_level=None), so
            # the BEGIN has to be explicit or every batch commits on its own.
            self._conn.execute("BEGIN")
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._inner is not None:
            return self._inner.__exit__(exc_type, exc, tb)
        self._conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False
