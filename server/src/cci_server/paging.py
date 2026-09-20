"""Keyset pagination, and the cursor that carries it.

Never OFFSET. Two reasons, and the second is the one that bites:

  * `OFFSET 190000` re-reads 190,000 rows to discard them, and the measured
    corpus is 191,475. A resumable pull would spend most of its time
    re-scanning what it already has.
  * OFFSET is wrong under concurrent writes. A row inserted behind the cursor
    shifts every later page by one, so a resumed pull silently skips a row.
    That is a missing session on someone's dashboard with nothing to indicate
    it, which is the failure mode this project treats as worse than an error.

A cursor is therefore the last key returned, base64'd so that it is visibly
opaque and clients do not start parsing it.
"""

from __future__ import annotations

import base64
import json

from cci_server.config import DEFAULT_PAGE, MAX_PAGE
from cci_server.errors import bad_request


def encode(key: list) -> str:
    raw = json.dumps(key, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode(cursor: str | None, *, width: int) -> list | None:
    """The key inside a cursor, or None.

    `width` is how many columns the key has. Checking it matters: a cursor
    from `?table=event` replayed against `?table=span` would otherwise compare
    a one-column key against a one-column key of a different table and quietly
    return the wrong page. Rejecting the shape catches the obvious half of
    that, and the endpoint checks the table name for the rest.
    """
    if not cursor:
        return None
    try:
        pad = "=" * (-len(cursor) % 4)
        key = json.loads(base64.urlsafe_b64decode(cursor + pad))
    except Exception:
        raise bad_request("invalid_cursor", "The cursor is not one this server issued.")
    if not isinstance(key, list) or len(key) != width:
        raise bad_request("invalid_cursor", "The cursor does not match the requested table.")
    return key


def clamp(limit: int | None) -> int:
    """The page size actually used.

    A request above the cap is clamped, not rejected. The client asked for
    more data, not for a different thing, and the response says what it got --
    whereas a 400 for `limit=100000` turns a working client into a broken one
    the day the cap is lowered.
    """
    if limit is None:
        return DEFAULT_PAGE
    if limit < 1:
        raise bad_request("invalid_limit", "limit must be at least 1.")
    return min(limit, MAX_PAGE)


def keyset_where(key_columns: list[str], after: list | None, start: int) -> tuple[str, list]:
    """`WHERE (a, b) > (%s, %s)` -- a row-value comparison, not an OR chain.

    PostgreSQL compares composite values lexicographically, which is exactly
    the ordering `ORDER BY a, b` produces, and it can use the index directly.
    The hand-written equivalent -- `a > x OR (a = x AND b > y)` -- means the
    same thing and is one refactor away from meaning something subtly else.

    `start` exists because these predicates are spliced into queries that
    already have parameters; it is unused in the SQL and kept in the signature
    so a caller must think about ordering.
    """
    if after is None:
        return "", []
    cols = ", ".join(key_columns)
    marks = ", ".join(["%s"] * len(key_columns))
    return f"({cols}) > ({marks})", list(after)


__all__ = ["encode", "decode", "clamp", "keyset_where", "DEFAULT_PAGE", "MAX_PAGE"]
