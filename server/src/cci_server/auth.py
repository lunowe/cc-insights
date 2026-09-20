"""Request-scoped dependencies: a connection, and who is calling.

Every authenticated route takes `principal` and gets a `Principal` or a 401.
There is no route that reads a caller's identity from anything but this -- not
from a query parameter, not from a body field -- which is what makes
"account A cannot read account B's rows" a property of one function rather
than of every handler remembering.
"""

from __future__ import annotations

from typing import Iterator

from fastapi import Depends, Header, Request

from cci_server import tokens
from cci_server.db import Database
from cci_server.errors import unauthenticated


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_conn(db: Database = Depends(get_db)) -> Iterator:
    """One connection for the whole request.

    Per request rather than per query, so that a handler which reads the scope
    and then aggregates inside it sees one consistent snapshot. A publish
    landing between those two statements would otherwise produce an aggregate
    over repos the scope did not include -- a small, rare, and exactly wrong
    violation of docs/ACCOUNTS.md §5 rule 1.
    """
    with db.connection() as conn:
        yield conn


def principal(
    conn=Depends(get_conn),
    authorization: str | None = Header(default=None),
) -> tokens.Principal:
    plain = tokens.parse_header(authorization)
    if plain is None:
        raise unauthenticated()
    who = tokens.resolve(conn, plain)
    if who is None:
        # Unknown, revoked and expired are one answer on purpose. The client's
        # correct response to all three is `cci login`, and telling them apart
        # would tell somebody holding a stolen token whether it was ever real.
        raise unauthenticated("The token is unknown, expired or revoked.")
    tokens.touch(conn, who.token_id)
    return who
