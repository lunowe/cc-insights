"""The team store: accept a projection, serve scope-aware queries.

docs/SERVER_API.md §4. Three rules from docs/ACCOUNTS.md §5 govern every line
of this file, and getting one wrong is a privacy leak rather than a bug.

**1. Aggregates are computed inside the viewer's scope.** Every query here
begins with `scope.resolve` and every aggregate carries the scope predicate
from it -- which is TWO terms, not one: the repos the caller has verified
access to, OR an exact (repo, row-owner) pair. `_visible_rows` builds it and
nothing here writes it by hand. There is no rollup table in the schema and
there must not be one: a precomputed "Alice: 40 h this week" that spans a
repo Bob cannot see leaks that repo's existence the moment Bob reads the
total. That is why scoping cannot be a WHERE clause bolted onto an existing
query later -- the scope has to be the first thing the query knows, not the
last.

**1b. Publishing is not a grant.** A caller who registers a `repo_id` gets to
read back their own rows and nothing else, because the id is a hash of a
public remote and anyone who can guess the remote can compute it. This file
used to take the assertion as proof; `scope.py`'s docstring is the long
version of why that was a critical disclosure.

**2. Branch names have a per-repo opt-out.** Enforced by not joining
`published_session_branch` when the caller's scope says the switch is off. The
column does not exist on the session row, so forgetting is not possible. The
switch applies on every path to the repo, not only the team one.

**3. Withheld time stays counted.** `_withheld_block` is on every aggregate
-- `/summary`, `/actors` AND `/daily`, which shipped without one --
and §4.4 of the contract explains at length what it may and may not contain --
notably that `publishedMs` is served to its own author only, and that there is
no "work you cannot see" figure, not even as a boolean.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, ValidationError

from cci_server import paging, scope as scope_mod, tokens
from cci_server.auth import get_conn, principal
from cci_server.config import MAX_BATCH_ROWS
from cci_server.db import now_ms
from cci_server.errors import bad_request, conflict, too_large

router = APIRouter(prefix="/v1/team", tags=["team"])

#: `redact.HUMAN`, `AUTONOMOUS`, `UNATTENDED`. The three buckets that partition
#: active time, travelling as a label so the partition survives aggregation.
#: A fourth value would make every breakdown quietly stop adding up, which is
#: why it is rejected here and CHECKed in migration 003 as well.
THREAD_ROLES = ("human", "autonomous", "unattended_root")

_DAY_MS = 86_400_000


# --------------------------------------------------------------------------
# publish
# --------------------------------------------------------------------------


class RepoRow(BaseModel):
    repoId: str
    remoteUrl: str
    forge: str | None = None
    owner: str | None = None
    repo: str | None = None
    webUrl: str | None = None
    name: str


class SessionRow(BaseModel):
    sessionId: str
    repoId: str
    actor: str
    source: str
    gitBranch: str | None = None
    startedAt: int
    endedAt: int
    activeMs: int = 0
    eventCount: int = 0


class SpanRow(BaseModel):
    spanId: str
    sessionId: str
    threadRole: str
    startedAt: int
    endedAt: int
    eventCount: int = 0


class WithheldRow(BaseModel):
    hostId: str
    withheldMs: int = 0
    withheldProjects: int = 0
    publishedMs: int = 0


class PublishBody(BaseModel):
    kind: str
    actor: str | None = None
    rows: list[dict] = Field(default_factory=list)


_ROW_MODELS = {
    "repos": RepoRow,
    "sessions": SessionRow,
    "spans": SpanRow,
    "withheld": WithheldRow,
}


@router.post("/publish")
def publish(body: PublishBody, who: tokens.Principal = Depends(principal),
            conn=Depends(get_conn)):
    model = _ROW_MODELS.get(body.kind)
    if model is None:
        raise bad_request(
            "unknown_kind",
            f"kind must be one of {sorted(_ROW_MODELS)}, not {body.kind!r}.",
        )
    if len(body.rows) > MAX_BATCH_ROWS:
        raise too_large(
            f"{len(body.rows)} rows in one request; the cap is {MAX_BATCH_ROWS}.",
            maxRows=MAX_BATCH_ROWS,
        )

    # `actor` is CHECKED, not trusted, and not silently overwritten. The
    # server could stamp its own and move on; that would be worse, because a
    # client whose `cci privacy` output says one thing while the store says
    # another has stopped describing what it sent.
    if body.actor is not None and body.actor != who.actor:
        raise _actor_mismatch(who.actor, body.actor)

    try:
        rows = [model(**r) for r in body.rows]
    except (ValidationError, TypeError) as exc:
        raise bad_request("malformed_row", f"A row does not match `{body.kind}`: {exc}")

    handler = {
        "repos": _publish_repos,
        "sessions": _publish_sessions,
        "spans": _publish_spans,
        "withheld": _publish_withheld,
    }[body.kind]
    applied, rejected = handler(conn, who, rows)
    return {"kind": body.kind, "received": len(rows),
            "applied": applied, "rejected": rejected}


def _actor_mismatch(mine: str, theirs: str):
    from cci_server.errors import ApiError

    return ApiError(
        403, "actor_mismatch",
        f"This account publishes as {mine!r}; the batch claims {theirs!r}. "
        "A projection published under somebody else's name would make the "
        "team view's central claim -- that it says who did the work -- false.",
    )


def _publish_repos(conn, who: tokens.Principal, rows: list[RepoRow]) -> tuple[int, int]:
    """Register repos, record what this account asserted about each, grant nothing.

    THIS CALL IS NOT A GRANT, and `scope.py`'s docstring is the long version
    of why: `repo_id` is a hash of a public remote, so anyone who can guess
    the remote can compute the id, and a row written here used to put the
    caller in scope for the repo. It no longer does. What a publisher gets is
    the ability to read back their OWN rows, and that comes from the rows
    themselves, not from this table.

    `published_repo` is still deliberately unowned: two people on one repo
    compute the same id from the same normalized remote, and that is what
    makes a team view of one repo one row rather than two. But unowned is not
    unprotected, and the old blind upsert had two defects:

    **A stranger could rewrite it.** `ON CONFLICT (repo_id) DO UPDATE` let any
    caller replace `name`, `remote_url` and `web_url` of a repo they have no
    access to, and every real member then saw the replacement. So the shared
    row is refreshed only by a caller with VERIFIED access to that repo.
    Creating it is always allowed -- creating a row that did not exist
    overwrites nothing -- which is what keeps a legitimate first publisher
    working on an instance with no GitHub app.

    **Reading it back was an existence oracle.** Publish a guessed id with a
    deliberately wrong name, read `GET /v1/team/repos`, and a name that came
    back different answers "does anybody here work on acme/skunkworks" --
    exactly the question the 409 in `_publish_sessions` used to answer.
    Refusing the write does not fix that; it makes the difference observable
    somewhere else. So each publisher's own assertion is kept beside their
    `repo_publisher` row and served back to them, and a caller who reaches a
    repo only as its publisher sees what they sent and nothing else.
    """
    now = now_ms()
    may_refresh = scope_mod.verified(conn, who.account_id, [r.repoId for r in rows])

    with conn.transaction():
        conn.cursor().executemany(
            """INSERT INTO published_repo
                   (repo_id, remote_url, forge, owner, repo, web_url, name,
                    created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (repo_id) DO NOTHING""",
            [(r.repoId, r.remoteUrl, r.forge, r.owner, r.repo, r.webUrl, r.name,
              now, now) for r in rows],
        )
        refreshable = [r for r in rows if r.repoId in may_refresh]
        if refreshable:
            conn.cursor().executemany(
                """UPDATE published_repo SET
                       remote_url = %s, forge = %s, owner = %s, repo = %s,
                       web_url = %s, name = %s, updated_at = %s
                   WHERE repo_id = %s""",
                [(r.remoteUrl, r.forge, r.owner, r.repo, r.webUrl, r.name, now,
                  r.repoId) for r in refreshable],
            )
        # Provenance, and the caller's own copy of what they said. Both, and
        # the second is the one that makes the first safe to keep: without it
        # a publisher with no other access would have to be shown somebody
        # else's version of the row, which is the oracle above.
        conn.cursor().executemany(
            """INSERT INTO repo_publisher
                   (repo_id, account_id, first_published_at, last_published_at,
                    remote_url, forge, owner, repo, web_url, name)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (repo_id, account_id) DO UPDATE SET
                   last_published_at = excluded.last_published_at,
                   remote_url = excluded.remote_url, forge = excluded.forge,
                   owner = excluded.owner, repo = excluded.repo,
                   web_url = excluded.web_url, name = excluded.name""",
            [(r.repoId, who.account_id, now, now, r.remoteUrl, r.forge, r.owner,
              r.repo, r.webUrl, r.name) for r in rows],
        )
    return len(rows), 0


def _publish_sessions(conn, who: tokens.Principal,
                      rows: list[SessionRow]) -> tuple[int, int]:
    if any(r.actor != who.actor for r in rows):
        raise _actor_mismatch(who.actor, next(r.actor for r in rows if r.actor != who.actor))

    # THE ORDER CHECK IS SCOPED TO THIS CALLER, and that is a disclosure fix
    # rather than a tidy-up. Asking `published_repo` GLOBALLY made this
    # endpoint an oracle: a 200 rather than a 409 for a guessed `repo_id`
    # answered "does anybody here work on acme/skunkworks", with the missing
    # ids echoed back in the message to make it easy. docs/REDACTION.md §0's
    # defence -- that the remote is public on the far side -- holds for a
    # public repo and is simply untrue for a private one.
    #
    # Asking `repo_publisher` instead answers only from rows this caller
    # wrote, so the response depends on nothing but what they have done
    # themselves. It is also the same rule the contract already states:
    # publish `repos` before `sessions`.
    repo_ids = {r.repoId for r in rows}
    known = {
        x["repo_id"]
        for x in conn.execute(
            """SELECT repo_id FROM repo_publisher
               WHERE account_id = %s AND repo_id = ANY(%s::text[])""",
            (who.account_id, sorted(repo_ids)),
        )
    }
    if missing := sorted(repo_ids - known):
        raise conflict(
            "foreign_key_violation",
            f"Publish the repos first: {missing[:5]} not in the repo registry.",
        )

    # An id already published by SOMEBODY ELSE is refused, not merged. Session
    # ids hash a log UUID and are unguessable, so this should never fire -- it
    # is here because "should never" is precisely how the confirmation attack
    # in docs/REDACTION.md §0 survived into a design.
    foreign = {
        x["session_id"]
        for x in conn.execute(
            """SELECT session_id FROM published_session
               WHERE session_id = ANY(%s::text[]) AND account_id <> %s""",
            (sorted(r.sessionId for r in rows), who.account_id),
        )
    }
    mine = [r for r in rows if r.sessionId not in foreign]

    now = now_ms()
    with conn.transaction():
        conn.cursor().executemany(
            """INSERT INTO published_session
                   (session_id, repo_id, account_id, actor, source,
                    started_at, ended_at, active_ms, event_count, published_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (session_id) DO UPDATE SET
                   repo_id = excluded.repo_id, actor = excluded.actor,
                   source = excluded.source, started_at = excluded.started_at,
                   ended_at = excluded.ended_at, active_ms = excluded.active_ms,
                   event_count = excluded.event_count,
                   published_at = excluded.published_at""",
            [(r.sessionId, r.repoId, who.account_id, who.actor, r.source,
              r.startedAt, r.endedAt, r.activeMs, r.eventCount, now) for r in mine],
        )

        # The branch name goes into its own table -- see migration 003. A row
        # published WITHOUT one deletes any name stored before, so a client
        # that stops sending branches actually stops publishing them rather
        # than freezing the last value it sent.
        with_branch = [r for r in mine if r.gitBranch]
        without = [r.sessionId for r in mine if not r.gitBranch]
        if with_branch:
            conn.cursor().executemany(
                """INSERT INTO published_session_branch (session_id, repo_id, git_branch)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (session_id) DO UPDATE
                       SET git_branch = excluded.git_branch, repo_id = excluded.repo_id""",
                [(r.sessionId, r.repoId, r.gitBranch) for r in with_branch],
            )
        if without:
            conn.execute(
                "DELETE FROM published_session_branch WHERE session_id = ANY(%s::text[])",
                (without,),
            )
    return len(mine), len(rows) - len(mine)


def _publish_spans(conn, who: tokens.Principal, rows: list[SpanRow]) -> tuple[int, int]:
    bad = {r.threadRole for r in rows} - set(THREAD_ROLES)
    if bad:
        raise bad_request(
            "invalid_thread_role",
            f"threadRole must be one of {list(THREAD_ROLES)}; got {sorted(bad)}. "
            "The three buckets partition active time, and a fourth value would "
            "make every breakdown stop adding up.",
        )

    session_ids = sorted({r.sessionId for r in rows})
    parents = {
        x["session_id"]: x
        for x in conn.execute(
            """SELECT session_id, repo_id, account_id FROM published_session
               WHERE session_id = ANY(%s::text[])""",
            (session_ids,),
        )
    }
    if missing := [s for s in session_ids if s not in parents]:
        raise conflict(
            "foreign_key_violation",
            f"Publish the sessions first: {missing[:5]} not in the store.",
        )

    # repo_id and account_id are DERIVED from the parent session here, never
    # taken from the request. That is what makes the denormalized columns on
    # `published_span` safe to aggregate against: they cannot disagree with
    # the session, and a span cannot be attached to somebody else's session.
    mine = [r for r in rows if parents[r.sessionId]["account_id"] == who.account_id]

    now = now_ms()
    with conn.transaction():
        conn.cursor().executemany(
            """INSERT INTO published_span
                   (span_id, session_id, repo_id, account_id, thread_role,
                    started_at, ended_at, event_count, published_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (span_id) DO UPDATE SET
                   session_id = excluded.session_id, repo_id = excluded.repo_id,
                   thread_role = excluded.thread_role,
                   started_at = excluded.started_at, ended_at = excluded.ended_at,
                   event_count = excluded.event_count,
                   published_at = excluded.published_at
               WHERE published_span.account_id = excluded.account_id""",
            [(r.spanId, r.sessionId, parents[r.sessionId]["repo_id"], who.account_id,
              r.threadRole, r.startedAt, r.endedAt, r.eventCount, now) for r in mine],
        )
    return len(mine), len(rows) - len(mine)


def _publish_withheld(conn, who: tokens.Principal,
                      rows: list[WithheldRow]) -> tuple[int, int]:
    """The hours that never left a laptop, per host.

    docs/ACCOUNTS.md §5: withheld time stays counted. A dashboard that quietly
    drops 8% of somebody's week is not private, it is wrong, and the person
    reading it cannot tell the difference.
    """
    now = now_ms()
    with conn.transaction():
        conn.cursor().executemany(
            """INSERT INTO published_withheld
                   (account_id, host_id, withheld_ms, withheld_projects,
                    published_ms, as_of)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (account_id, host_id) DO UPDATE SET
                   withheld_ms = excluded.withheld_ms,
                   withheld_projects = excluded.withheld_projects,
                   published_ms = excluded.published_ms,
                   as_of = excluded.as_of""",
            [(who.account_id, r.hostId, r.withheldMs, r.withheldProjects,
              r.publishedMs, now) for r in rows],
        )
    return len(rows), 0


# --------------------------------------------------------------------------
# reading, always inside the scope
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Filters:
    """The `WHERE` of every read, with the scope already applied.

    `repos` is not the caller's request -- it is `Scope.narrow` of it, so an
    id outside the scope has already been dropped. Nothing downstream can
    widen it, because nothing downstream sees what was asked for.

    `visible` is the second half, and it is the half that stopped the
    two-call bypass: being able to ask about a repo is not the same as being
    able to read everybody's rows in it. See `_visible_rows`.
    """

    repos: list[str]
    visible: tuple[str, list[Any]]
    actors: list[str]
    sources: list[str]
    roles: list[str]
    frm: int | None
    to: int | None

    def sql(self) -> tuple[str, list[Any]]:
        visible_sql, visible_params = self.visible
        where = ["sp.repo_id = ANY(%s::text[])", visible_sql]
        params: list[Any] = [self.repos, *visible_params]
        if self.actors:
            where.append("s.actor = ANY(%s::text[])")
            params.append(self.actors)
        if self.sources:
            where.append("s.source = ANY(%s::text[])")
            params.append(self.sources)
        if self.roles:
            where.append("sp.thread_role = ANY(%s::text[])")
            params.append(self.roles)
        if self.frm is not None:
            where.append("sp.started_at >= %s")
            params.append(self.frm)
        if self.to is not None:
            where.append("sp.started_at < %s")
            params.append(self.to)
        return " AND ".join(where), params


def _filters(sc: scope_mod.Scope, repo: list[str] | None, actor: list[str] | None,
             source: list[str] | None, role: list[str] | None,
             frm: int | None, to: int | None) -> Filters:
    if role:
        bad = set(role) - set(THREAD_ROLES)
        if bad:
            raise bad_request("invalid_role", f"role must be one of {list(THREAD_ROLES)}.")
    return Filters(
        repos=sc.narrow(repo), visible=_visible_rows("sp", sc),
        actors=list(actor or ()), sources=list(source or ()),
        roles=list(role or ()), frm=frm, to=to,
    )


def _visible_rows(alias: str, sc: scope_mod.Scope) -> tuple[str, list[Any]]:
    """"May this caller read THIS row", for any table carrying repo and owner.

    Two terms, and the split is the whole of the fix in `scope.py`:

      * `repo_id` in the caller's verified set -- every row in the repo, which
        is what "access to the repository" means in docs/REDACTION.md §1;
      * otherwise an exact (repo_id, account_id) pair -- the caller's own
        rows, or the rows of an admin who put the repo on one of their
        rosters. Publishing lets you read back what you sent; it has never
        been a reason to read what somebody else sent.

    Written as a predicate on the aggregated table rather than as a filter
    applied afterwards, for the reason migration 003 denormalizes `repo_id`
    and `account_id` onto `published_span` in the first place: an aggregate
    that forgets a join is a bug, and an aggregate that forgets the scope is
    a disclosure, and these two must not be confusable.
    """
    repos, accounts = sc.row_pairs()
    return (
        f"({alias}.repo_id = ANY(%s::text[]) OR ({alias}.repo_id, {alias}.account_id) "
        f"IN (SELECT r, a FROM unnest(%s::text[], %s::text[]) AS pair(r, a)))",
        [sorted(sc.repo_ids), repos, accounts],
    )


#: The FROM every read shares. `published_span` carries `repo_id` so the scope
#: predicate sits on the table being aggregated rather than two joins away --
#: an aggregate that drops a join is a bug, an aggregate that drops the scope
#: is a disclosure, and these two cannot be confused.
_FROM = ("FROM published_span sp "
         "JOIN published_session s ON s.session_id = sp.session_id")


def _scope_and_filters(conn, who, repo, actor, source, role, frm, to):
    sc = scope_mod.resolve(conn, who.account_id)
    return sc, _filters(sc, repo, actor, source, role, frm, to)


#: The columns of a repo as a caller is shown it. Held once because they are
#: read from two different rows depending on how the caller reaches the repo,
#: and a list that existed twice would eventually differ.
_REPO_FIELDS = ("remote_url", "forge", "owner", "repo", "web_url", "name")


@router.get("/repos")
def team_repos(who: tokens.Principal = Depends(principal), conn=Depends(get_conn)):
    """Every repo in the caller's scope, with why it is there.

    A repo is listed because the caller has VERIFIED access to it or because
    they have published rows into it. Not because they claimed it: a listing
    that appeared on a bare `POST /v1/team/publish {"kind":"repos"}` would
    confirm a guessed remote, which is the two-call bypass this endpoint was
    the last visible step of.

    Which row the metadata comes from depends on the same distinction.
    Somebody with verified access sees the shared registry row, because they
    are entitled to the team's shared view of the repo. Somebody who reaches
    it only as its publisher sees WHAT THEY THEMSELVES SENT -- otherwise the
    response would differ depending on whether a stranger's row was already
    there, and that difference is an answer to "does anybody here work on
    acme/skunkworks". Falling back to the shared row covers publishers from
    before migration 004, whose assertion was never recorded.
    """
    sc = scope_mod.resolve(conn, who.account_id)
    rows = conn.execute(
        """SELECT r.repo_id, r.remote_url, r.forge, r.owner, r.repo, r.web_url, r.name,
                  p.remote_url AS mine_remote_url, p.forge AS mine_forge,
                  p.owner AS mine_owner, p.repo AS mine_repo,
                  p.web_url AS mine_web_url, p.name AS mine_name
           FROM published_repo r
           LEFT JOIN repo_publisher p
                  ON p.repo_id = r.repo_id AND p.account_id = %s
           WHERE r.repo_id = ANY(%s::text[])""",
        (who.account_id, sorted(sc.visible_repo_ids)),
    ).fetchall()

    out = []
    for r in rows:
        shared = r["repo_id"] in sc.repo_ids or r["mine_name"] is None
        fields = {f: r[f] if shared else r[f"mine_{f}"] for f in _REPO_FIELDS}
        out.append(
            {"repoId": r["repo_id"], "remoteUrl": fields["remote_url"],
             "forge": fields["forge"], "owner": fields["owner"],
             "repo": fields["repo"], "webUrl": fields["web_url"],
             "name": fields["name"], "via": list(sc.via.get(r["repo_id"], ())),
             "branchNamesPublished": sc.shows_branch(r["repo_id"])}
        )
    # Ordered on the name the caller is actually shown, not on the stored one,
    # so the ORDER BY cannot leak the difference the SELECT just hid.
    out.sort(key=lambda x: (x["name"] or "", x["repoId"]))
    return {"repos": out}


@router.get("/summary")
def summary(
    repo: list[str] | None = Query(default=None),
    actor: list[str] | None = Query(default=None),
    source: list[str] | None = Query(default=None),
    role: list[str] | None = Query(default=None),
    frm: int | None = Query(default=None, alias="from"),
    to: int | None = Query(default=None),
    who: tokens.Principal = Depends(principal),
    conn=Depends(get_conn),
):
    """The scope-aware aggregate, computed now, inside the caller's scope.

    Nothing about this is precomputed, and docs/ACCOUNTS.md §5 rule 1 is the
    reason rather than a lack of time. A nightly rollup keyed on anything but
    (viewer scope, period) is a privacy defect: the first time Bob reads a
    total that includes a repo he cannot see, that repo's existence has
    leaked, and no amount of care in the renderer takes it back.
    """
    sc, f = _scope_and_filters(conn, who, repo, actor, source, role, frm, to)
    where, params = f.sql()

    totals = conn.execute(
        f"""SELECT coalesce(sum(sp.ended_at - sp.started_at), 0) AS active_ms,
                   count(*) AS spans, count(DISTINCT sp.session_id) AS sessions
            {_FROM} WHERE {where}""",
        params,
    ).fetchone()

    by_role = {r["thread_role"]: r["ms"] for r in conn.execute(
        f"""SELECT sp.thread_role,
                   coalesce(sum(sp.ended_at - sp.started_at), 0) AS ms
            {_FROM} WHERE {where} GROUP BY sp.thread_role""",
        params,
    )}

    by_source = conn.execute(
        f"""SELECT s.source, coalesce(sum(sp.ended_at - sp.started_at), 0) AS ms
            {_FROM} WHERE {where} GROUP BY s.source ORDER BY ms DESC""",
        params,
    ).fetchall()

    # Same two-source name as `GET /v1/team/repos`, for the same reason: a
    # caller who reaches the repo only as its publisher must be shown the
    # name they sent, or this aggregate becomes the oracle that endpoint is
    # careful not to be.
    by_repo = conn.execute(
        f"""SELECT sp.repo_id, r.name, p.name AS mine_name,
                   coalesce(sum(sp.ended_at - sp.started_at), 0) AS ms
            {_FROM} JOIN published_repo r ON r.repo_id = sp.repo_id
            LEFT JOIN repo_publisher p
                   ON p.repo_id = sp.repo_id AND p.account_id = %s
            WHERE {where} GROUP BY sp.repo_id, r.name, p.name ORDER BY ms DESC""",
        [who.account_id] + params,
    ).fetchall()

    return {
        "scope": {"repos": len(sc.visible_repo_ids), "teams": len(sc.team_ids)},
        "activeMs": totals["active_ms"],
        "sessions": totals["sessions"],
        "spans": totals["spans"],
        # Partitions activeMs exactly -- which is the property `thread_role`
        # travels as a label in order to preserve.
        "byRole": {
            "human": by_role.get("human", 0),
            "autonomous": by_role.get("autonomous", 0),
            "unattendedRoot": by_role.get("unattended_root", 0),
        },
        "bySource": [{"source": r["source"], "activeMs": r["ms"]} for r in by_source],
        "byRepo": [
            {"repoId": r["repo_id"],
             "name": (r["name"] if r["repo_id"] in sc.repo_ids or r["mine_name"] is None
                      else r["mine_name"]),
             "activeMs": r["ms"]}
            for r in by_repo
        ],
        "withheld": _withheld_block(conn, who, sc),
        "generatedAt": now_ms(),
    }


def _withheld_block(conn, who: tokens.Principal, sc: scope_mod.Scope) -> dict:
    """Time that could not be published at all, per actor. Read §4.4 first.

    Three decisions live in this function and all three are disclosure
    decisions, not formatting ones.

    **Who appears.** Actors with at least one span inside the caller's scope,
    ignoring any time filter. Restricting to the range would mean somebody
    whose visible week was empty silently loses their withheld figure --
    rule 3's exact failure -- and not restricting to the scope at all would
    announce the existence of people the caller shares nothing with.

    **What is not here.** No `publishedMs` and no `totalMs` for anyone but the
    caller. Those are corpus-wide across every repo that account published,
    including repos this caller cannot see, so serving them would disclose the
    MAGNITUDE of invisible work. And there is deliberately no "out of scope"
    figure -- not as a number and not as a boolean, because a boolean saying
    "there is more" still answers "does a repo I cannot see exist".

    **Why `withheldMs` itself is safe to show.** It names no repo and cannot:
    the definition of withheld work is that it belongs to no repo, which is
    exactly why `redact` could not publish it. What it discloses is that a
    person has some unpublishable time, and rule 3 says a viewer must not be
    left to guess about that.

    Two mechanical notes, both of which were wrong before.

    `withheldMs` SUMS over hosts and `withheldProjects` takes the MAXIMUM,
    because they are not the same kind of quantity. Time on two laptops is
    two disjoint stretches of somebody's week and adds up. Projects are not
    disjoint: `project_id = sha256(root_path)`, so a checkout at the same
    path on two machines is the same project counted twice -- docs/ACCOUNTS.md
    §4 raises exactly that collision for `/home/ci/work`. There is no exact
    answer available here and there must not be: de-duplicating would need
    the project ids, and a `project_id` IS a path (docs/REDACTION.md §0),
    which is the one thing this store never receives. The maximum is the
    largest number any single machine reported, so it never claims more
    distinct projects than somebody demonstrably has.

    Membership uses the ROW-level scope predicate, not the repo list. In a
    repo the caller reaches only as its publisher, other people's rows exist
    and are not readable -- and an actor named here purely because they share
    an unreadable repo would announce their existence, which is the thing the
    paragraph above is at pains to avoid.
    """
    visible_sql, visible_params = _visible_rows("sp", sc)
    rows = conn.execute(
        f"""SELECT w.account_id, a.actor,
                   sum(w.withheld_ms) AS withheld_ms,
                   max(w.withheld_projects) AS withheld_projects,
                   sum(w.published_ms) AS published_ms,
                   max(w.as_of) AS as_of
            FROM published_withheld w
            JOIN account a ON a.account_id = w.account_id
            WHERE w.account_id = %s
               OR w.account_id IN (SELECT DISTINCT sp.account_id
                                   FROM published_span sp WHERE {visible_sql})
            GROUP BY w.account_id, a.actor
            ORDER BY a.actor""",
        [who.account_id, *visible_params],
    ).fetchall()

    out = []
    for r in rows:
        entry = {
            "actor": r["actor"],
            "withheldMs": int(r["withheld_ms"]),
            "withheldProjects": int(r["withheld_projects"]),
            "asOf": r["as_of"],
        }
        if r["account_id"] == who.account_id:
            entry["publishedMs"] = int(r["published_ms"])
        out.append(entry)

    return {
        "byActor": out,
        "totalMs": sum(e["withheldMs"] for e in out),
        # Said in the payload so a renderer can label it "all time" even when
        # the rest of the page says "this week", without having to know why.
        "scope": "corpus",
        "rangeFiltered": False,
    }


@router.get("/sessions")
def sessions(
    repo: list[str] | None = Query(default=None),
    actor: list[str] | None = Query(default=None),
    source: list[str] | None = Query(default=None),
    role: list[str] | None = Query(default=None),
    frm: int | None = Query(default=None, alias="from"),
    to: int | None = Query(default=None),
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    who: tokens.Principal = Depends(principal),
    conn=Depends(get_conn),
):
    sc, f = _scope_and_filters(conn, who, repo, actor, source, role, frm, to)
    where, params = f.sql()
    n = paging.clamp(limit)
    after = paging.decode(cursor, width=1)
    if after is not None:
        where += " AND s.session_id > %s"
        params = params + [after[0]]

    rows = conn.execute(
        f"""SELECT DISTINCT s.session_id, s.repo_id, s.actor, s.source,
                   s.started_at, s.ended_at, s.active_ms, s.event_count
            {_FROM} WHERE {where}
            ORDER BY s.session_id LIMIT %s""",
        params + [n + 1],
    ).fetchall()

    has_more = len(rows) > n
    page = rows[:n]

    # THE BRANCH-NAME JOIN, and it only happens for repos whose switch is on.
    # Written as a second query rather than a LEFT JOIN with a CASE, because a
    # CASE is a mask somebody can delete and still have working code, while a
    # missing lookup here returns null -- which is the same value a session
    # with no branch carries, so no observer can tell suppressed from absent.
    showable = [r["session_id"] for r in page if sc.shows_branch(r["repo_id"])]
    branches: dict[str, str] = {}
    if showable:
        branches = {
            b["session_id"]: b["git_branch"]
            for b in conn.execute(
                """SELECT session_id, git_branch FROM published_session_branch
                   WHERE session_id = ANY(%s::text[])""",
                (showable,),
            )
        }

    return {
        "sessions": [
            {"sessionId": r["session_id"], "repoId": r["repo_id"], "actor": r["actor"],
             "source": r["source"], "gitBranch": branches.get(r["session_id"]),
             "startedAt": r["started_at"], "endedAt": r["ended_at"],
             "activeMs": r["active_ms"], "eventCount": r["event_count"]}
            for r in page
        ],
        "limit": n,
        "nextCursor": paging.encode([page[-1]["session_id"]]) if has_more and page else None,
    }


@router.get("/actors")
def actors(
    repo: list[str] | None = Query(default=None),
    actor: list[str] | None = Query(default=None),
    source: list[str] | None = Query(default=None),
    role: list[str] | None = Query(default=None),
    frm: int | None = Query(default=None, alias="from"),
    to: int | None = Query(default=None),
    who: tokens.Principal = Depends(principal),
    conn=Depends(get_conn),
):
    """One row per person with visible time. In-scope only, computed now.

    docs/ACCOUNTS.md §7 rules out what could obviously be built on top of
    this: no ranking, no "time saved", no per-person cost. The data can answer
    "who worked the most hours", the answer is wrong -- it measures agent time,
    not work -- and it will be quoted anyway. So the endpoint reports and does
    not order by time.
    """
    sc, f = _scope_and_filters(conn, who, repo, actor, source, role, frm, to)
    where, params = f.sql()
    rows = conn.execute(
        f"""SELECT s.actor, s.account_id,
                   coalesce(sum(sp.ended_at - sp.started_at), 0) AS ms,
                   count(DISTINCT sp.session_id) AS sessions,
                   count(DISTINCT sp.repo_id) AS repos
            {_FROM} WHERE {where}
            GROUP BY s.actor, s.account_id ORDER BY s.actor""",
        params,
    ).fetchall()
    return {
        "actors": [
            {"actor": r["actor"], "accountId": r["account_id"], "activeMs": r["ms"],
             "sessions": r["sessions"], "repos": r["repos"]}
            for r in rows
        ],
        "withheld": _withheld_block(conn, who, sc),
    }


@router.get("/daily")
def daily(
    repo: list[str] | None = Query(default=None),
    actor: list[str] | None = Query(default=None),
    source: list[str] | None = Query(default=None),
    role: list[str] | None = Query(default=None),
    frm: int | None = Query(default=None, alias="from"),
    to: int | None = Query(default=None),
    who: tokens.Principal = Depends(principal),
    conn=Depends(get_conn),
):
    """Span time per UTC calendar day.

    UTC, unlike `docs/API.md`'s `/api/daily`, which buckets by the LOCAL day.
    That endpoint has exactly one reader and a local day to agree on; a team
    spans timezones and has neither. The divergence is deliberate and the
    contract tells renderers to label the axis.

    The bucket is integer division by 86_400_000, not a date function. That
    keeps the aggregation in the database -- returning every span to bucket
    them in Python would pull 1430 rows on the author's corpus and far more on
    a team's -- while honouring the portability contract's "no
    strftime/julianday", which exists so that one expression does not mean two
    things on two engines. Epoch-ms floor-division IS the UTC day number, on
    any engine, with no calendar involved.
    """
    sc, f = _scope_and_filters(conn, who, repo, actor, source, role, frm, to)
    where, params = f.sql()
    rows = conn.execute(
        f"""SELECT (sp.started_at / {_DAY_MS}) AS day_no,
                   coalesce(sum(sp.ended_at - sp.started_at), 0) AS ms
            {_FROM} WHERE {where} GROUP BY day_no ORDER BY day_no""",
        params,
    ).fetchall()
    return {
        "days": [
            {
                "date": datetime.fromtimestamp(
                    int(r["day_no"]) * _DAY_MS / 1000, timezone.utc
                ).strftime("%Y-%m-%d"),
                "activeMs": int(r["ms"]),
            }
            for r in rows
        ],
        # THE WITHHELD BLOCK IS NOT OPTIONAL HERE, and this endpoint is the
        # one that most needs it. A "hours this week" chart is exactly the
        # renderer docs/ACCOUNTS.md §5 rule 3 is about: it reads `days`, adds
        # them up, and shows a person's week with the unpublishable part
        # silently missing. The reader cannot tell a quiet week from a week
        # that mostly happened in a repo with no remote.
        #
        # It is the same corpus figure the other aggregates carry, with the
        # same `rangeFiltered: false` beside it -- so a renderer drawing a
        # daily axis can label it "all time" without having to know why there
        # is no day to hang it on.
        "withheld": _withheld_block(conn, who, sc),
    }
