"""The personal store's transfer rules: a mirror of `cc_insights.sync`.

`sync.py` already moves these tables between one person's machines, straight
against PostgreSQL. This server is the same transfer with an HTTP hop in the
middle -- docs/ACCOUNTS.md §3 chose that over row-level security because an
RLS policy missing from one table is a silent total leak, and because the
roadmap already needs rules a database cannot express.

So the ownership rules, the conflict clauses and the excluded tables here are
`sync.TABLES` and `sync.EXCLUDED`, unchanged. The transport moved; the
semantics did not, and a difference between the two would show up as data that
is correct over one path and wrong over the other.

It is a copy rather than an import because this package does not depend on
`cc_insights` -- whose zero-dependency install is a tested feature -- and a
copy that nobody checks is exactly how `sync`'s own first test failed: it
compared the module to a hardcoded copy of itself and sailed through a merge
that added three tables. `test_mirrors_sync_tables` therefore imports the real
`sync` when the client source is on the path and asserts, table by table and
column by column, that the two have not drifted. It SKIPS rather than fails
when the client is not importable, which is honest about what it checked.

SCOPE, repeated from `sync.py` because it is the thing that must not be
widened: this is one person's several machines, and paths travel because it is
all the same disk. Sharing with OTHER PEOPLE is `team_schema.py`, a different
pipe with different rules.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Table:
    """One table's transfer rules.

    `key` is the natural key as the client knows it. The actual primary key in
    this store is `("account_id",) + key` -- see the header of migration 002
    for why a bare `project_id` is not enough.

    `host_scoped` is `sync.Table.owner_filter` in the one form it ever takes:
    "rows belonging to this host". The filter itself lives on the client, which
    is the side that knows which host it is; the server records the fact so it
    can reject a push that claims a host the account does not own.

    `optional` are columns that exist in this store and in the client's schema
    but are NOT in `sync.TABLES`. There is exactly one today --
    `event.cache_write_1h_tokens`, which migration 005 added and the sync list
    was never extended for -- and the category exists so that the drift test
    can report it rather than either side silently deciding.

    `set_clause` overrides the default "overwrite every non-key column sent"
    for the columns where a blind overwrite would lose something.
    """

    name: str
    columns: tuple[str, ...]
    key: tuple[str, ...]
    host_scoped: bool = False
    set_clause: str | None = None
    optional: tuple[str, ...] = ()
    parent: str | None = None

    @property
    def all_columns(self) -> tuple[str, ...]:
        return self.columns + self.optional

    @property
    def conflict_target(self) -> tuple[str, ...]:
        return ("account_id",) + self.key


#: Push order is FK order. `project_group` precedes `project` because
#: `project.group_id` points at it, and `thread` precedes `event`/`span`.
TABLES: tuple[Table, ...] = (
    Table(
        "host",
        ("host_id", "hostname", "os", "first_seen", "last_seen"),
        ("host_id",),
        # first_seen is the earliest this machine was ever seen; a later push
        # must not move it forward.
        set_clause=(
            "hostname = excluded.hostname, os = excluded.os, "
            "first_seen = CASE WHEN host.first_seen < excluded.first_seen "
            "THEN host.first_seen ELSE excluded.first_seen END, "
            "last_seen = CASE WHEN host.last_seen > excluded.last_seen "
            "THEN host.last_seen ELSE excluded.last_seen END"
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
        # out of the group the human chose.
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
        host_scoped=True,
    ),
    Table(
        "session",
        ("id", "native_id", "source", "host_id", "project_id", "cwd", "git_branch",
         "cli_version", "started_at", "ended_at", "event_count", "active_ms"),
        ("id",),
        host_scoped=True,
    ),
    Table(
        "thread",
        ("id", "native_id", "session_id", "parent_thread_id", "is_subagent",
         "agent_name", "started_at", "ended_at", "event_count", "active_ms"),
        ("id",),
        host_scoped=True,
        parent="parent_thread_id",
    ),
    Table(
        "event",
        ("id", "session_id", "thread_id", "native_event_id", "ts", "ordinal", "kind",
         "model", "tool_name", "tool_use_id", "input_tokens", "output_tokens",
         "cache_read_tokens", "cache_write_tokens"),
        ("id",),
        host_scoped=True,
        # Migration 005 split cache writes by TTL -- 41% of the author's
        # cache-write tokens bought an hour and cost 2x base input rather than
        # 1.25x. `sync.TABLES` has never listed the column, so a client on the
        # current release does not send it. Accepting it as optional means the
        # store is ready the day sync is fixed, and means a client that already
        # sends it is not rejected. Omitting it leaves whatever is stored
        # alone, rather than writing NULL over a value a newer client sent.
        optional=("cache_write_1h_tokens",),
    ),
    Table(
        "span",
        ("id", "session_id", "thread_id", "started_at", "ended_at", "event_count",
         "attended"),
        ("id",),
        host_scoped=True,
    ),
)

BY_NAME: dict[str, Table] = {t.name: t for t in TABLES}


#: Tables deliberately NOT transferred, and why. Verbatim from
#: `sync.EXCLUDED`, including the reasons, because the reasons are the part
#: that stops someone adding one back.
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


def upsert_sql(table: Table, columns: list[str]) -> str:
    """The INSERT for one batch, over exactly the columns the client sent.

    Built from `columns` rather than from `table.all_columns` so that an
    omitted optional column is left alone instead of being overwritten with
    NULL. A client on an older release must be able to push without erasing a
    value a newer one wrote.

    `account_id` is prepended here and is never accepted from the request
    body. That is the single point where a row is bound to its owner, and it
    reads from the token -- there is no code path that lets a caller name a
    different account.
    """
    cols = ["account_id"] + list(columns)
    placeholders = ", ".join(["%s"] * len(cols))
    target = ", ".join(table.conflict_target)

    if table.set_clause:
        sets = table.set_clause
    else:
        updatable = [c for c in columns if c not in table.key]
        sets = ", ".join(f"{c} = excluded.{c}" for c in updatable)

    action = f"DO UPDATE SET {sets}" if sets else "DO NOTHING"
    return (
        f"INSERT INTO {table.name} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({target}) {action}"
    )


def order_by_parent(table: Table, rows: list[list], columns: list[str]) -> list[list]:
    """Parents before children, for a table that references itself.

    A safety net rather than a requirement: migration 002 defers the
    self-reference on `thread.parent_thread_id` to commit, so within one batch
    the order does not matter. It is here because the deferral only covers one
    transaction, and because relying on the order rows happen to arrive in is
    how a foreign-key failure becomes intermittent and reproduces only on
    somebody else's machine.
    """
    if table.parent is None or table.parent not in columns:
        return rows
    id_at = columns.index(table.key[0])
    parent_at = columns.index(table.parent)

    pending = {r[id_at]: r for r in rows}
    placed: list[list] = []
    done: set = set()

    def place(row: list) -> None:
        rid = row[id_at]
        if rid in done:
            return
        done.add(rid)  # before recursing: a cycle must not hang
        parent = row[parent_at]
        if parent is not None and parent in pending and parent not in done:
            place(pending[parent])
        placed.append(row)

    for row in rows:
        place(row)
    return placed
