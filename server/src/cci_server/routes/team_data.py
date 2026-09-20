"""The team store: accept a projection, serve scope-aware queries.

docs/SERVER_API.md §4. Three rules from docs/ACCOUNTS.md §5 govern every line
of this file, and getting one wrong is a privacy leak rather than a bug.

**1. Aggregates are computed inside the viewer's scope.** Every query here
begins with `scope.resolve` and every aggregate carries `sp.repo_id = ANY(...)`
from it. There is no rollup table in the schema and there must not be one: a
precomputed "Alice: 40 h this week" that spans a repo Bob cannot see leaks
that repo's existence the moment Bob reads the total. That is why scoping
cannot be a WHERE clause bolted onto an existing query later -- the scope has
to be the first thing the query knows, not the last.

**2. Branch names have a per-repo opt-out.** Enforced by not joining
`published_session_branch` when the caller's scope says the switch is off. The
column does not exist on the session row, so forgetting is not possible.

**3. Withheld time stays counted.** `_withheld_block` is on every aggregate,
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
    """Upsert the repo registry, and record that this account publishes into it.

    `published_repo` is deliberately unowned: two people on the same repo
    compute the same `repo_id` from the same normalized remote, and that is
    what makes a team view of one repo one row rather than two.
    `repo_publisher` is what carries ownership, and it is a set, not a winner.
    """
    now = now_ms()
    with conn.transaction():
        conn.cursor().executemany(
            """INSERT INTO published_repo
                   (repo_id, remote_url, forge, owner, repo, web_url, name,
                    created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (repo_id) DO UPDATE SET
                   remote_url = excluded.remote_url, forge = excluded.forge,
                   owner = excluded.owner, repo = excluded.repo,
                   web_url = excluded.web_url, name = excluded.name,
                   updated_at = excluded.updated_at""",
            [(r.repoId, r.remoteUrl, r.forge, r.owner, r.repo, r.webUrl, r.name,
              now, now) for r in rows],
        )
        conn.cursor().executemany(
            """INSERT INTO repo_publisher
                   (repo_id, account_id, first_published_at, last_published_at)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (repo_id, account_id) DO UPDATE
                   SET last_published_at = excluded.last_published_at""",
            [(r.repoId, who.account_id, now, now) for r in rows],
        )
    return len(rows), 0


def _publish_sessions(conn, who: tokens.Principal,
                      rows: list[SessionRow]) -> tuple[int, int]:
    if any(r.actor != who.actor for r in rows):
        raise _actor_mismatch(who.actor, next(r.actor for r in rows if r.actor != who.actor))

    repo_ids = {r.repoId for r in rows}
    known = {
        x["repo_id"]
        for x in conn.execute(
            "SELECT repo_id FROM published_repo WHERE repo_id = ANY(%s::text[])",
            (sorted(repo_ids),),
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
    """

    repos: list[str]
    actors: list[str]
    sources: list[str]
    roles: list[str]
    frm: int | None
    to: int | None

    def sql(self) -> tuple[str, list[Any]]:
        where = ["sp.repo_id = ANY(%s::text[])"]
        params: list[Any] = [self.repos]
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
        repos=sc.narrow(repo), actors=list(actor or ()), sources=list(source or ()),
        roles=list(role or ()), frm=frm, to=to,
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


@router.get("/repos")
def team_repos(who: tokens.Principal = Depends(principal), conn=Depends(get_conn)):
    sc = scope_mod.resolve(conn, who.account_id)
    rows = conn.execute(
        """SELECT repo_id, remote_url, forge, owner, repo, web_url, name
           FROM published_repo WHERE repo_id = ANY(%s::text[]) ORDER BY name, repo_id""",
        (sorted(sc.repo_ids),),
    ).fetchall()
    return {
        "repos": [
            {"repoId": r["repo_id"], "remoteUrl": r["remote_url"], "forge": r["forge"],
             "owner": r["owner"], "repo": r["repo"], "webUrl": r["web_url"],
             "name": r["name"], "via": list(sc.via.get(r["repo_id"], ())),
             "branchNamesPublished": sc.shows_branch(r["repo_id"])}
            for r in rows
        ]
    }


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

    by_repo = conn.execute(
        f"""SELECT sp.repo_id, r.name,
                   coalesce(sum(sp.ended_at - sp.started_at), 0) AS ms
            {_FROM} JOIN published_repo r ON r.repo_id = sp.repo_id
            WHERE {where} GROUP BY sp.repo_id, r.name ORDER BY ms DESC""",
        params,
    ).fetchall()

    return {
        "scope": {"repos": len(sc.repo_ids), "teams": len(sc.team_ids)},
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
            {"repoId": r["repo_id"], "name": r["name"], "activeMs": r["ms"]}
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
    """
    rows = conn.execute(
        """SELECT w.account_id, a.actor,
                  sum(w.withheld_ms) AS withheld_ms,
                  sum(w.withheld_projects) AS withheld_projects,
                  sum(w.published_ms) AS published_ms,
                  max(w.as_of) AS as_of
           FROM published_withheld w
           JOIN account a ON a.account_id = w.account_id
           WHERE w.account_id = %s
              OR w.account_id IN (SELECT DISTINCT account_id FROM published_span
                                  WHERE repo_id = ANY(%s::text[]))
           GROUP BY w.account_id, a.actor
           ORDER BY a.actor""",
        (who.account_id, sorted(sc.repo_ids)),
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
    _sc, f = _scope_and_filters(conn, who, repo, actor, source, role, frm, to)
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
        ]
    }
