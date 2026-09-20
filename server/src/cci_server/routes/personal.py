"""The personal store: batched push, resumable pull.

docs/SERVER_API.md §3. One person's full rows, paths included, readable by
exactly one account -- docs/ACCOUNTS.md §2 is explicit that there is no
admin-can-see-everything mode, because an admin who can read it is a second
person.

Every query in this file filters on `account_id`, and `account_id` comes from
the token and from nowhere else. There is no parameter that can change it.
That is belt; the braces are in migration 002, where the primary keys are
composite on `account_id` and so are the foreign keys -- a cross-account join
is not merely unreachable through this API, it is unrepresentable in the
database.
"""

from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from cci_server import paging, personal_schema, tokens
from cci_server.auth import get_conn, principal
from cci_server.config import MAX_BATCH_ROWS
from cci_server.errors import bad_request, conflict, not_found, too_large

router = APIRouter(prefix="/v1/personal", tags=["personal"])

#: A hard ceiling on any single TEXT value, and it is a privacy control, not a
#: performance one. Every string this schema legitimately carries is an id, a
#: path, a branch, a tool name or a model name -- the longest realistic one is
#: a filesystem path, and PATH_MAX is 1024 on macOS and 4096 on Linux. Prompt
#: text, a response, a tool argument or a file's contents would all be orders
#: of magnitude past this.
#:
#: "No prompt text in the schema" has held for this project because it is
#: enforced where rows are WRITTEN. A server that accepts arbitrary strings
#: into `agent_name` is a server where that stops being true the first time a
#: client has a bug, and nothing downstream would ever notice.
MAX_TEXT = 4096


class PushBody(BaseModel):
    hostId: str
    table: str
    columns: list[str]
    rows: list[list[Any]]


def _table(name: str) -> personal_schema.Table:
    if name in personal_schema.BY_NAME:
        return personal_schema.BY_NAME[name]
    if name in personal_schema.EXCLUDED:
        raise bad_request(
            "table_not_transferred",
            f"{name!r} is deliberately not transferred: "
            f"{personal_schema.EXCLUDED[name]}",
        )
    raise bad_request("unknown_table", f"No table named {name!r} in the personal store.")


def _check_columns(table: personal_schema.Table, columns: list[str]) -> None:
    seen = set(columns)
    if len(seen) != len(columns):
        raise bad_request("duplicate_column", "A column is named twice.")
    missing = [c for c in table.columns if c not in seen]
    if missing:
        # A subset is refused rather than accepted, because the upsert would
        # write NULL over whatever is already stored in the omitted columns.
        # Optional columns are the exception and are not in `table.columns`.
        raise bad_request(
            "missing_column",
            f"{table.name} needs every one of its columns; missing {missing}.",
        )
    unknown = [c for c in columns if c not in table.all_columns]
    if unknown:
        raise bad_request(
            "unknown_column",
            f"{table.name} has no column {unknown}. A value the server does not "
            "recognise is refused, never silently dropped.",
        )


def _clean(value: Any, column: str) -> Any:
    """One cell, checked. Scalars only, and nothing long enough to be prose.

    Rejecting dicts and lists is the metadata-only rule at the wire: a nested
    object is somewhere a transcript could ride along inside a field that
    looks like an id, and PostgreSQL would happily store the JSON encoding of
    it in a TEXT column.
    """
    if value is None or isinstance(value, (int, float)):
        # JSON's `true`/`false` arrive as bool, which is an int in Python but
        # not to PostgreSQL's BIGINT columns. The client sends 0/1 for
        # `attended`, `is_subagent` and `group_pinned`; this makes a client
        # that sends booleans work rather than fail at the driver.
        return int(value) if isinstance(value, bool) else value
    if isinstance(value, str):
        if len(value) > MAX_TEXT:
            raise bad_request(
                "value_too_long",
                f"{column} is {len(value)} characters. Nothing in this schema is "
                f"longer than {MAX_TEXT}; this store holds metadata only.",
            )
        return value
    raise bad_request(
        "invalid_value",
        f"{column} must be a string, a number or null, not {type(value).__name__}.",
    )


@router.get("/tables")
def tables(who: tokens.Principal = Depends(principal)):
    """The transfer rules, as data.

    So a client can check for drift instead of hardcoding a copy that rots.
    `sync.TABLES` and this list have to agree, and the only thing worse than
    them disagreeing is them disagreeing silently.
    """
    return {
        "tables": [
            {
                "table": t.name,
                "key": list(t.key),
                "columns": list(t.columns),
                "optionalColumns": list(t.optional),
                "hostScoped": t.host_scoped,
            }
            for t in personal_schema.TABLES
        ],
        "excluded": [
            {"table": name, "reason": why}
            for name, why in sorted(personal_schema.EXCLUDED.items())
        ],
        "maxBatchRows": MAX_BATCH_ROWS,
    }


@router.post("/push")
def push(body: PushBody, who: tokens.Principal = Depends(principal), conn=Depends(get_conn)):
    table = _table(body.table)
    _check_columns(table, body.columns)

    if len(body.rows) > MAX_BATCH_ROWS:
        raise too_large(
            f"{len(body.rows)} rows in one request; the cap is {MAX_BATCH_ROWS}. "
            "Split the batch -- every id is a content hash, so re-sending an "
            "overlapping range costs nothing.",
            maxRows=MAX_BATCH_ROWS,
        )

    width = len(body.columns)
    for i, row in enumerate(body.rows):
        if len(row) != width:
            raise bad_request(
                "malformed_row",
                f"Row {i} has {len(row)} values for {width} columns.",
            )

    _require_host(conn, who.account_id, body.hostId, table, body.columns, body.rows)

    rows = [
        [_clean(v, c) for v, c in zip(row, body.columns)]
        for row in body.rows
    ]
    rows = personal_schema.order_by_parent(table, rows, body.columns)

    sql = personal_schema.upsert_sql(table, body.columns)
    params = [[who.account_id] + row for row in rows]

    try:
        # One transaction. A half-applied batch is sessions without their
        # events, and every count on the dashboard is then wrong in a way that
        # looks plausible.
        with conn.transaction():
            conn.cursor().executemany(sql, params)
    except psycopg.errors.ForeignKeyViolation as exc:
        raise conflict(
            "foreign_key_violation",
            f"A row in {table.name} references something not yet pushed. Push in "
            f"the order given by GET /v1/personal/tables. ({_brief(exc)})",
        ) from exc
    except psycopg.errors.UniqueViolation as exc:
        raise conflict(
            "unique_violation",
            f"A row in {table.name} collides with a different row on a natural "
            f"key. ({_brief(exc)})",
        ) from exc

    return {
        "table": table.name,
        "received": len(body.rows),
        "applied": len(rows),
        "rejected": 0,
        # Structurally always empty here: the primary key is
        # (account_id, <id>), so the same id under two accounts is two
        # legitimate rows and there is nothing to refuse. Kept so a client has
        # one response shape across both bulk endpoints -- the team store,
        # where ids are global, can genuinely populate it.
        "conflicts": [],
    }


def _brief(exc: Exception) -> str:
    """The first line of a driver error, and no more.

    Postgres error detail can quote the offending row, and the offending row
    in this store is a filesystem path. It goes to the client that sent it --
    which is the same account -- but truncating keeps it out of anything that
    logs a response body wholesale.
    """
    return str(exc).strip().splitlines()[0][:200]


def _require_host(conn, account_id: str, host_id: str, table: personal_schema.Table,
                  columns: list[str], rows: list[list]) -> None:
    """The host must belong to this account, or be established by this batch.

    Claiming a host is not a separate call: it is the first push of its `host`
    row. docs/ACCOUNTS.md §4 is firm that `host_id` is not regenerated on
    claim -- it is baked into every session id, so reassigning it forks the
    entire history, and `config.py` has an atomic write guarding exactly that.
    A host is claimed by an account and keeps its identity.
    """
    if table.name == "host":
        at = columns.index("host_id")
        if any(row[at] == host_id for row in rows):
            return
    owned = conn.execute(
        "SELECT 1 FROM host WHERE account_id = %s AND host_id = %s",
        (account_id, host_id),
    ).fetchone()
    if owned is None:
        # 404, not 403: a host id belonging to somebody else must look exactly
        # like a host id that does not exist.
        raise not_found(
            f"Host {host_id!r} is not on this account. Push its `host` row first."
        )


@router.get("/pull")
def pull(
    table: str = Query(...),
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    who: tokens.Principal = Depends(principal),
    conn=Depends(get_conn),
):
    t = _table(table)
    n = paging.clamp(limit)
    after = paging.decode(cursor, width=len(t.key))

    cols = list(t.all_columns)
    order = ", ".join(t.key)
    where = ["account_id = %s"]
    params: list[Any] = [who.account_id]
    if after is not None:
        clause, extra = paging.keyset_where(list(t.key), after, len(params))
        where.append(clause)
        params.extend(extra)

    # Not filtered by host. The point of a pull is to see the OTHER machines,
    # exactly as `sync.pull` does -- this host's own rows come back too and
    # land on themselves, because the ids are hashes of the same content.
    # It is filtered by account on every table, with no parameter that turns
    # that off.
    sql = (
        f"SELECT {', '.join(cols)} FROM {t.name} WHERE {' AND '.join(where)} "
        f"ORDER BY {order} LIMIT %s"
    )
    # One more than asked for, so "is there another page" is known rather than
    # guessed. Guessing produces a trailing empty page, which a resumable
    # client then treats as a failed resume.
    params.append(n + 1)
    fetched = conn.execute(sql, params).fetchall()

    has_more = len(fetched) > n
    page = fetched[:n]
    next_cursor = None
    if has_more and page:
        next_cursor = paging.encode([page[-1][k] for k in t.key])

    return {
        "table": t.name,
        "columns": cols,
        "rows": [[r[c] for c in cols] for r in page],
        "limit": n,
        "nextCursor": next_cursor,
    }


@router.get("/status")
def status(who: tokens.Principal = Depends(principal), conn=Depends(get_conn)):
    hosts = conn.execute(
        """SELECT host_id, hostname, os, first_seen, last_seen
           FROM host WHERE account_id = %s ORDER BY hostname""",
        (who.account_id,),
    ).fetchall()

    counts = []
    total = 0
    for t in personal_schema.TABLES:
        n = conn.execute(
            f"SELECT count(*) AS n FROM {t.name} WHERE account_id = %s", (who.account_id,)
        ).fetchone()["n"]
        counts.append({"table": t.name, "rows": n})
        total += n

    return {
        "hosts": [
            {"hostId": h["host_id"], "hostname": h["hostname"], "os": h["os"],
             "firstSeen": h["first_seen"], "lastSeen": h["last_seen"]}
            for h in hosts
        ],
        "tables": counts,
        "totalRows": total,
    }
