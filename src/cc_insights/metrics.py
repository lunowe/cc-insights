"""Filter-aware read queries behind the local HTTP API.

One function per endpoint in **docs/API.md**, which is the frozen contract:
`meta`, `summary`, `timeline`, `daily`, `concurrency`, `projects`, `groups`,
`agents`, `heatmap`. Every one returns plain dicts and lists whose keys are the
wire names (camelCase) exactly as the contract spells them, so `serve.py` is a
`json.dumps` away from a response and `scripts/dump_fixtures.py` captures the
same bytes the server would send.

Nothing here derives anything. It reads what ingest and derive already wrote.

WHAT A FILTER DOES
------------------
A filter narrows the set of **spans**, and every number is then computed from
the spans that survive. Sessions and threads are counted as *those reachable
from the surviving spans* -- not as global table counts -- because under a
project or role filter a global count would report rows the numbers beside it
do not describe. A thread whose single event is an instant has no span, so it
is outside every number here; that is the definition working, not a loss.

A project row is one on-disk path; a **group** is the logical project behind
several of them (`docs/GROUPING.md`). `project` and `group` therefore both
select spans by *which project their session belongs to*, and they combine as a
**union**, not an intersection: `?group=G&project=P` is "the spans of group G,
plus the spans of project P". A user who narrows to a group and then ticks one
more stray project expects to see both, and an intersection would answer with
an empty dashboard whenever P is not in G. Every other filter still intersects
with that union.

`ts_from` is inclusive and `ts_to` exclusive, both compared against the span's
**start**. A span is therefore in or out as a whole: no span is ever clipped,
so `activeMs` never counts a fraction of a run that a user cannot see in the
timeline. The day and hour series below still split spans internally, but that
is bucketing, not filtering.

Every value reaches SQLite as a bound parameter. The only thing ever formatted
into a statement is the number of `?` placeholders.

DAY AND HOUR BOUNDARIES
-----------------------
`daily` and `heatmap` **split each span at local midnight / at the top of each
local hour** and credit each piece to the bucket it falls in. Attributing a
span whole to its start bucket silently misplaces overnight and long-running
work, which is exactly the work this tool exists to show. Local means the
machine's timezone: the wire is UTC epoch ms everywhere else, but a calendar
day is a local-time question and the answer would otherwise be wrong by the
UTC offset for every user outside UTC.
"""

from __future__ import annotations

import collections
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Mapping, Sequence

from cc_insights import db, derive
from cc_insights.config import Config

__all__ = [
    "Filters",
    "FilterError",
    "ROLES",
    "TIMELINE_LIMIT",
    "meta",
    "summary",
    "timeline",
    "daily",
    "concurrency",
    "projects",
    "groups",
    "agents",
    "heatmap",
    "ENDPOINTS",
]

Role = Literal["all", "root", "subagent"]
ROLES: tuple[str, ...] = ("all", "root", "subagent")

# The swimlane cap. Above this the widest spans are kept and `truncated` is
# set, so the UI can tell the user rather than silently dropping work.
TIMELINE_LIMIT = 4000


class FilterError(ValueError):
    """A filter value that cannot be parsed. `serve.py` turns this into a 400."""


@dataclass(frozen=True, slots=True)
class Filters:
    """The one filter shape every endpoint accepts. All fields optional.

    An empty list means the same as `None` -- no constraint -- because that is
    what an omitted repeated query parameter parses to. A value that matches
    nothing (an unknown project or group id) is not an error: it yields empty
    results.

    `projects` and `groups` are the two spellings of one question -- which
    project a span's session belongs to -- so they UNION with each other while
    intersecting with everything else. See the module docstring.
    """

    projects: list[str] | None = None
    groups: list[str] | None = None
    sources: list[str] | None = None
    ts_from: int | None = None
    ts_to: int | None = None
    role: Role = "all"

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise FilterError(f"role must be one of {', '.join(ROLES)}; got {self.role!r}")

    @property
    def is_empty(self) -> bool:
        return not (self.projects or self.groups or self.sources) and (
            self.ts_from is None and self.ts_to is None and self.role == "all"
        )

    @classmethod
    def from_query(cls, params: Mapping[str, Sequence[str]]) -> "Filters":
        """Build from `urllib.parse.parse_qs` output. Raises `FilterError`.

        `project`, `group` and `source` are repeatable. `from`/`to` are epoch
        ms; a repeated one takes the last value, matching how a browser treats
        a duplicated form field. Unknown parameters are ignored rather than
        rejected: a cache-buster in the URL is not a malformed filter.
        """
        role = _last(params.get("role")) or "all"
        if role not in ROLES:
            raise FilterError(f"role must be one of {', '.join(ROLES)}; got {role!r}")
        return cls(
            projects=[v for v in params.get("project", []) if v] or None,
            groups=[v for v in params.get("group", []) if v] or None,
            sources=[v for v in params.get("source", []) if v] or None,
            ts_from=_int_param(params, "from"),
            ts_to=_int_param(params, "to"),
            role=role,  # type: ignore[arg-type]
        )


def _last(values: Sequence[str] | None) -> str | None:
    return values[-1] if values else None


def _int_param(params: Mapping[str, Sequence[str]], name: str) -> int | None:
    raw = _last(params.get(name))
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        raise FilterError(f"{name} must be an integer epoch-ms timestamp; got {raw!r}") from None


# --------------------------------------------------------------------------
# SQL assembly
# --------------------------------------------------------------------------

# Every filtered query hangs off this. Both joins are on foreign keys that are
# always present, so neither can drop a span row; they are always in scope so
# one WHERE builder serves every endpoint.
_FROM = (
    " FROM span sp"
    " JOIN thread t ON t.id = sp.thread_id"
    " JOIN session s ON s.id = sp.session_id"
)


def _marks(n: int) -> str:
    return ",".join("?" * n)


def _where(f: Filters, *extra: str) -> tuple[str, list[Any]]:
    """The WHERE clause for `f` plus any endpoint-specific conditions.

    Returns `("" | " WHERE ...", params)`. Values only ever leave here as bound
    parameters; the sole thing interpolated is a run of `?`. `extra` conditions
    are literal SQL written in this module and must carry no placeholder, so
    that `params` always lines up with the filter clauses alone.

    `project` and `group` are OR-ed into a single clause -- the union the
    contract promises -- and that clause then AND-s with the rest. The group
    arm is a subquery rather than a join so that every endpoint keeps the same
    `_FROM`, including the two that already join `project` themselves.
    """
    clauses: list[str] = list(extra)
    params: list[Any] = []

    identity: list[str] = []
    if f.projects:
        identity.append(f"s.project_id IN ({_marks(len(f.projects))})")
        params += list(f.projects)
    if f.groups:
        identity.append(
            "s.project_id IN (SELECT project_id FROM project"
            f" WHERE group_id IN ({_marks(len(f.groups))}))")
        params += list(f.groups)
    if identity:
        clauses.append("(" + " OR ".join(identity) + ")")

    if f.sources:
        clauses.append(f"s.source IN ({_marks(len(f.sources))})")
        params += list(f.sources)
    if f.ts_from is not None:
        clauses.append("sp.started_at >= ?")     # inclusive
        params.append(f.ts_from)
    if f.ts_to is not None:
        clauses.append("sp.started_at < ?")      # exclusive
        params.append(f.ts_to)
    if f.role == "root":
        clauses.append("t.is_subagent = 0")
    elif f.role == "subagent":
        clauses.append("t.is_subagent = 1")

    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _rows(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, tuple(params))]


def _one(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> dict:
    row = conn.execute(sql, tuple(params)).fetchone()
    return dict(row) if row else {}


def _scalar(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> int:
    row = conn.execute(sql, tuple(params)).fetchone()
    return (row[0] or 0) if row else 0


# --------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------
def meta(conn: sqlite3.Connection, cfg: Config) -> dict:
    """GET /api/meta -- everything the filter controls need. Never filtered.

    It is deliberately unfiltered: these are the *choices*, and a filter that
    narrowed its own set of options could not be widened again from the UI.
    `projects` and `groups` here are LEFT JOINs so a project or group with no
    derived spans is still offered, with `activeMs` 0.
    """
    host = _one(conn, "SELECT hostname FROM host ORDER BY last_seen DESC LIMIT 1")
    bounds = _one(conn, "SELECT min(ts) AS lo, max(ts) AS hi FROM event")
    return {
        "hostname": host.get("hostname", "unknown"),
        "firstTs": bounds.get("lo"),
        "lastTs": bounds.get("hi"),
        "sources": [
            r["source"] for r in _rows(conn, "SELECT DISTINCT source FROM session ORDER BY 1")
        ],
        "projects": _rows(conn, """
            SELECT p.project_id AS projectId, p.name AS name, p.root_path AS rootPath,
                   coalesce(sum(sp.ended_at - sp.started_at), 0) AS activeMs
            FROM project p
            LEFT JOIN session s ON s.project_id = p.project_id
            LEFT JOIN span sp ON sp.session_id = s.id
            GROUP BY 1, 2, 3 ORDER BY activeMs DESC"""),
        # The group filter control, populated in the same call as the project
        # one. A group with no time yet is still a choice, so this is a roster
        # like `projects` above -- `/api/groups` is the ranking.
        "groups": _rows(conn, """
            SELECT g.group_id AS groupId, g.name AS name,
                   coalesce(sum(sp.ended_at - sp.started_at), 0) AS activeMs
            FROM project_group g
            LEFT JOIN project p ON p.group_id = g.group_id
            LEFT JOIN session s ON s.project_id = p.project_id
            LEFT JOIN span sp ON sp.session_id = s.id
            GROUP BY 1, 2 ORDER BY activeMs DESC, g.name"""),
        "agents": _rows(conn, """
            SELECT DISTINCT t.agent_name AS agentName, s.source AS source
            FROM thread t JOIN session s ON s.id = t.session_id
            WHERE t.agent_name IS NOT NULL ORDER BY 1"""),
        "models": [
            r["model"] for r in _rows(
                conn, "SELECT DISTINCT model FROM event WHERE model IS NOT NULL ORDER BY 1")
        ],
        "idleThresholdS": cfg.idle_threshold_s,
        "generatedAt": db.now_ms(),
    }


def summary(conn: sqlite3.Connection, f: Filters = Filters()) -> dict:
    """GET /api/summary -- the headline numbers over the filtered spans.

    `sessions`, `threads` and `events` count what the surviving spans reach.
    `events` is the sum of `span.event_count`: inside a thread with two or more
    events every event belongs to exactly one span, so this is the number of
    events the reported time was measured from.

    The three `*Ms` buckets partition `activeMs` exactly, by construction:
    every span's thread is root or subagent, and every root span's `attended`
    is 1 or is not.
    """
    where, params = _where(f)

    def bucket(extra: str) -> int:
        w, p = _where(f, extra)
        return _scalar(
            conn, f"SELECT coalesce(sum(sp.ended_at - sp.started_at), 0){_FROM}{w}", p)

    totals = _one(conn, f"""
        SELECT count(*) AS spans,
               count(DISTINCT sp.session_id) AS sessions,
               count(DISTINCT sp.thread_id) AS threads,
               coalesce(sum(sp.event_count), 0) AS events,
               coalesce(sum(sp.ended_at - sp.started_at), 0) AS activeMs
        {_FROM}{where}""", params)

    # Tokens belong to the events inside the surviving spans. An event is in a
    # span when it is that span's thread's and its timestamp lies in the span's
    # closed interval -- which is exactly how derive built the span, so no
    # event can match two spans of one thread (consecutive spans are separated
    # by a gap wider than the idle threshold).
    tokens = _one(conn, f"""
        SELECT coalesce(sum(e.input_tokens), 0) AS input,
               coalesce(sum(e.output_tokens), 0) AS output,
               coalesce(sum(e.cache_read_tokens), 0) AS cacheRead,
               coalesce(sum(e.cache_write_tokens), 0) AS cacheWrite
        {_FROM}
        JOIN event e ON e.thread_id = sp.thread_id
                    AND e.ts >= sp.started_at AND e.ts <= sp.ended_at
        {where}""", params)

    return {
        "sessions": totals["sessions"],
        "threads": totals["threads"],
        "events": totals["events"],
        "spans": totals["spans"],
        "activeMs": totals["activeMs"],
        "bySource": _rows(conn, f"""
            SELECT s.source AS source,
                   coalesce(sum(sp.ended_at - sp.started_at), 0) AS activeMs
            {_FROM}{where} GROUP BY 1 ORDER BY 2 DESC""", params),
        "humanInitiatedMs": bucket("t.is_subagent = 0 AND sp.attended = 1"),
        "autonomousMs": bucket("t.is_subagent = 1"),
        "unattendedRootMs": bucket(
            "t.is_subagent = 0 AND (sp.attended = 0 OR sp.attended IS NULL)"),
        "tokens": tokens,
    }


def timeline(conn: sqlite3.Connection, f: Filters = Filters()) -> dict:
    """GET /api/timeline -- one row per surviving span, for the swimlane.

    Over `TIMELINE_LIMIT` rows the **widest** spans are kept, not the first or
    the newest: a truncated swimlane should still show the work that dominates
    the range, and `truncated` tells the UI to say so.
    """
    where, params = _where(f)
    spans = _rows(conn, f"""
        SELECT sp.id AS spanId, sp.thread_id AS threadId, sp.session_id AS sessionId,
               s.project_id AS projectId, p.name AS projectName, s.source AS source,
               t.agent_name AS agentName, t.is_subagent AS isSubagent,
               t.parent_thread_id AS parentThreadId,
               sp.attended AS attended, sp.started_at AS start, sp.ended_at AS end
        FROM span sp
        JOIN thread t ON t.id = sp.thread_id
        JOIN session s ON s.id = sp.session_id
        LEFT JOIN project p ON p.project_id = s.project_id
        {where}
        ORDER BY sp.started_at""", params)
    for r in spans:
        r["isSubagent"] = bool(r["isSubagent"])

    truncated = len(spans) > TIMELINE_LIMIT
    if truncated:
        spans = sorted(spans, key=lambda r: r["end"] - r["start"], reverse=True)[:TIMELINE_LIMIT]
        spans.sort(key=lambda r: r["start"])
    return {"spans": spans, "truncated": truncated, "limit": TIMELINE_LIMIT}


# ---------------------------------------------------- day / hour bucketing --
class _Day:
    """Local calendar day."""

    @staticmethod
    def next_boundary(dt: datetime) -> datetime:
        return dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

    @staticmethod
    def label(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%d")


class _Hour:
    """Local (weekday, hour); weekday 0 = Monday, per the contract."""

    @staticmethod
    def next_boundary(dt: datetime) -> datetime:
        return dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    @staticmethod
    def label(dt: datetime) -> tuple[int, int]:
        return (dt.weekday(), dt.hour)


def _slice_by(conn: sqlite3.Connection, f: Filters, key) -> tuple[
        collections.Counter, dict[Any, collections.Counter],
        dict[Any, list[tuple[int, int]]]]:
    """Split every surviving span at local `key` boundaries.

    Returns the summed time per bucket, the same split by source, and the
    **clipped pieces** per bucket, which is what a union needs: summing tells
    you how much work happened, and only the pieces can tell you over how much
    of the clock it happened.

    Walking boundary to boundary rather than assuming fixed-width buckets is
    what makes this correct across a DST change, where a local day is 23 or 25
    hours long and an hour can repeat.
    """
    where, params = _where(f)
    acc: collections.Counter = collections.Counter()
    per_src: dict[Any, collections.Counter] = collections.defaultdict(collections.Counter)
    pieces: dict[Any, list[tuple[int, int]]] = collections.defaultdict(list)

    sql = f"SELECT sp.started_at AS a, sp.ended_at AS b, s.source AS source{_FROM}{where}"
    for row in conn.execute(sql, tuple(params)):
        a, b, src = row["a"], row["b"], row["source"]
        cur = a
        while cur < b:
            dt = datetime.fromtimestamp(cur / 1000)
            nxt = min(b, int(key.next_boundary(dt).timestamp() * 1000))
            if nxt <= cur:      # defensive: a boundary must advance
                break
            acc[key.label(dt)] += nxt - cur
            per_src[key.label(dt)][src] += nxt - cur
            pieces[key.label(dt)].append((cur, nxt))
            cur = nxt
        if a == b:
            # A zero-duration span adds no time but is a real touch of the
            # corpus; registering the bucket keeps it inside the reported range.
            acc[key.label(datetime.fromtimestamp(a / 1000))] += 0
    return acc, per_src, pieces


def _union_ms(intervals: list[tuple[int, int]]) -> int:
    """Length of the union of intervals -- overlapping work counted once."""
    total = 0
    cur_a = cur_b = None
    for a, b in sorted(intervals):
        if cur_b is None or a > cur_b:
            if cur_b is not None:
                total += cur_b - cur_a
            cur_a, cur_b = a, b
        else:
            cur_b = max(cur_b, b)
    if cur_b is not None:
        total += cur_b - cur_a
    return total


def daily(conn: sqlite3.Connection, f: Filters = Filters()) -> dict:
    """GET /api/daily -- one row per local day from first to last, gaps filled.

    `activeMs` sums the day's span time; `wallMs` is the **union** of the day's
    spans, clipped to the day, so work done in parallel is counted once.
    `activeMs > wallMs` exactly when agents ran alongside each other that day,
    and the ratio is the day's parallelism. Reporting the sum for both -- which
    this did until the definition was fixed -- flattens every such day to 1.0
    and hides the one thing the corpus is most interesting for.

    Gaps are emitted as zeros rather than omitted so a chart shows the shape of
    a quiet week instead of silently closing it up.
    """
    acc, per_src, pieces = _slice_by(conn, f, _Day)
    if not acc:
        return {"days": []}
    wall = {k: _union_ms(v) for k, v in pieces.items()}
    lo = datetime.strptime(min(acc), "%Y-%m-%d").date()
    hi = datetime.strptime(max(acc), "%Y-%m-%d").date()

    days, d = [], lo
    while d <= hi:
        k = d.strftime("%Y-%m-%d")
        days.append({"date": k, "activeMs": acc.get(k, 0), "wallMs": wall.get(k, 0),
                     "bySource": dict(per_src.get(k, {}))})
        d += timedelta(days=1)
    return {"days": days}


def heatmap(conn: sqlite3.Connection, f: Filters = Filters()) -> dict:
    """GET /api/heatmap -- local weekday x hour totals. Weekday 0 = Monday."""
    acc, _, _ = _slice_by(conn, f, _Hour)
    return {"cells": [{"weekday": w, "hour": h, "activeMs": ms}
                      for (w, h), ms in sorted(acc.items())]}


def concurrency(conn: sqlite3.Connection, f: Filters = Filters()) -> dict:
    """GET /api/concurrency -- sweep-line over the surviving spans.

    Reuses `derive.concurrency`, the same tested sweep the CLI reports, so the
    dashboard and `cci stats` can never drift apart. Ends are processed before
    starts at the same instant there, so two spans that merely touch are not
    counted as concurrent.
    """
    where, params = _where(f)
    intervals = [
        (r["a"], r["b"])
        for r in conn.execute(
            f"SELECT sp.started_at AS a, sp.ended_at AS b{_FROM}{where}"
            " ORDER BY sp.started_at", tuple(params))
    ]
    c = derive.concurrency(intervals)
    active = sum(b - a for a, b in intervals)
    return {
        "timeAtLevel": {str(lvl): ms for lvl, ms in sorted(c.time_at_level.items())},
        "peak": c.peak,
        "peakAt": c.peak_at,
        "wallMs": c.wall_ms,
        "activeMs": active,
        "multiplier": round(active / c.wall_ms, 4) if c.wall_ms else 0,
    }


def projects(conn: sqlite3.Connection, f: Filters = Filters()) -> dict:
    """GET /api/projects -- per-project totals over the surviving spans.

    Inner joins throughout: a project with no span in range is absent rather
    than present with zeros, because this table is a ranking, not a roster.
    `meta.projects` is the roster.

    `groupId`/`groupName` are a LEFT JOIN: an ungrouped project is legal and
    reports both as null. `groupPinned` says a human placed this project in
    that group, which is the difference between a row detection may move and
    one it must not.
    """
    where, params = _where(f)
    rows = _rows(conn, f"""
        SELECT p.project_id AS projectId, p.name AS name, p.root_path AS rootPath,
               p.group_id AS groupId, g.name AS groupName,
               p.group_pinned AS groupPinned,
               p.path_exists AS pathExists,
               coalesce(sum(sp.ended_at - sp.started_at), 0) AS activeMs,
               count(DISTINCT s.id) AS sessions, count(DISTINCT sp.thread_id) AS threads,
               min(sp.started_at) AS firstTs, max(sp.ended_at) AS lastTs
        {_FROM}
        JOIN project p ON p.project_id = s.project_id
        LEFT JOIN project_group g ON g.group_id = p.group_id
        {where}
        GROUP BY 1, 2, 3, 4, 5, 6 ORDER BY activeMs DESC""", params)
    for r in rows:
        r["groupPinned"] = bool(r["groupPinned"])
        # Tri-state on purpose: None means detection has not probed this path
        # yet, which is not the same as "the directory is gone".
        r["pathExists"] = None if r["pathExists"] is None else bool(r["pathExists"])
    return {"projects": rows}


def groups(conn: sqlite3.Connection, f: Filters = Filters()) -> dict:
    """GET /api/groups -- per-group totals over the surviving spans.

    A group is the logical project (`docs/GROUPING.md`): on the author's
    corpus 45 project rows are really ~13 groups, and the largest of them
    reads barely half its true time until they are added up here.

    Like `projects`, this is a ranking and not a roster: a group none of the
    surviving spans reaches is absent rather than present with zeros.
    `meta.groups` is the roster.

    `ungrouped` is the exact complement -- every surviving span whose project
    has no group, including the rare span whose session carries no project at
    all -- so that

        sum(groups[].activeMs) + ungrouped.activeMs == summary.activeMs

    holds under every filter. Before `cci group auto` has ever run, `groups`
    is empty and `ungrouped` holds the whole corpus; that is the normal
    starting state, not an error.

    Counts are filter-aware like everywhere else: `projects` and
    `pinnedProjects` count the group's members that the surviving spans
    reach, not its membership on paper.
    """
    where, params = _where(f)
    rows = _rows(conn, f"""
        SELECT g.group_id AS groupId, g.name AS name, g.origin AS origin,
               g.forge AS forge, g.owner AS owner, g.repo AS repo, g.web_url AS webUrl,
               coalesce(sum(sp.ended_at - sp.started_at), 0) AS activeMs,
               count(DISTINCT s.id) AS sessions,
               count(DISTINCT sp.thread_id) AS threads,
               count(DISTINCT p.project_id) AS projects,
               count(DISTINCT CASE WHEN p.group_pinned = 1 THEN p.project_id END)
                   AS pinnedProjects,
               min(sp.started_at) AS firstTs, max(sp.ended_at) AS lastTs
        {_FROM}
        JOIN project p ON p.project_id = s.project_id
        JOIN project_group g ON g.group_id = p.group_id
        {where}
        GROUP BY 1, 2, 3, 4, 5, 6, 7 ORDER BY activeMs DESC, g.name""", params)

    # The same FROM with the join inverted, so the two halves partition the
    # filtered spans by construction rather than by two definitions agreeing.
    un_where, un_params = _where(f, "g.group_id IS NULL")
    ungrouped = _one(conn, f"""
        SELECT count(DISTINCT s.project_id) AS projects,
               coalesce(sum(sp.ended_at - sp.started_at), 0) AS activeMs
        {_FROM}
        LEFT JOIN project p ON p.project_id = s.project_id
        LEFT JOIN project_group g ON g.group_id = p.group_id
        {un_where}""", un_params)
    return {"groups": rows, "ungrouped": ungrouped}


def agents(conn: sqlite3.Connection, f: Filters = Filters()) -> dict:
    """GET /api/agents -- named agents and the time they hold.

    Claude Code records an agent *type* (`general-purpose`, `Explore`); Codex
    records a random per-thread nickname. Both are returned as recorded and
    with their source, so the frontend can group Codex under one row instead of
    the backend guessing which names are types.
    """
    where, params = _where(f, "t.agent_name IS NOT NULL")
    return {"agents": _rows(conn, f"""
        SELECT t.agent_name AS agentName, s.source AS source,
               count(DISTINCT t.id) AS threads,
               coalesce(sum(sp.ended_at - sp.started_at), 0) AS activeMs
        {_FROM}{where} GROUP BY 1, 2 ORDER BY activeMs DESC""", params)}


# The nine endpoint names of the contract, in the order docs/API.md lists
# them. serve.py routes on this and the tests iterate it, so adding an endpoint
# to the contract is one edit here plus its function.
_FILTERED = {
    "summary": summary,
    "timeline": timeline,
    "daily": daily,
    "concurrency": concurrency,
    "projects": projects,
    "groups": groups,
    "agents": agents,
    "heatmap": heatmap,
}
ENDPOINTS: tuple[str, ...] = ("meta", *_FILTERED)


def endpoint(name: str, conn: sqlite3.Connection, f: Filters, cfg: Config) -> dict:
    """Dispatch one endpoint by its contract name. `KeyError` if unknown.

    `meta` is the odd one out: it ignores filters and needs the config, because
    `idleThresholdS` has to travel with every duration this API exports.
    """
    if name == "meta":
        return meta(conn, cfg)
    return _FILTERED[name](conn, f)
