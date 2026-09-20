"""Derivation engine: active spans, the `attended` flag, and concurrency.

This module reads the `event` and `thread` tables and writes the `span` table.
It never touches a log file and never imports a source adapter, so it works on
whatever ingest has already put in the database.

ACTIVE SPANS
------------
A span is a **maximal run of one thread's events whose consecutive gaps are
<= idle_threshold**. A gap *above* the threshold contributes **zero** time: it
ends the span and the next event starts a new one. The tempting
`sum(min(gap, threshold))` form invents `threshold` seconds of work per idle
gap and inflated an early draft of this project by 59% (docs/FINDINGS.md §0).

Spans are computed **per thread, never per session**. A session groups a root
thread with its subagent threads; merging them into one event stream would
hide exactly the concurrency this tool exists to measure. A consequence worth
stating: `session.active_ms` is the sum of its threads' span durations and can
therefore exceed the session's wall-clock duration. That is the parallelism
signal, not a bug.

Two edge cases, both chosen to match docs/probes/canonical_metrics.py exactly:

* A thread with fewer than two events produces **no span at all** -- one
  timestamp is an instant, not an interval.
* Inside a thread that does have >= 2 events, an isolated event (idle on both
  sides) produces a **zero-duration span**. It contributes 0 ms of active time
  but is a real row: it records that the thread was touched at that instant.

ATTENDED
--------
`span.attended` classifies **the transition into the span** -- that is, whether
a human turn started this run -- and nothing else.

It is judged on the span's **head**: the events from the span's first up to,
but excluding, the first `assistant` / `tool_use` / `tool_result` event. The
head is used rather than the literal first event because neither CLI writes the
human's turn first. Claude Code emits `queue-operation` and `attachment` lines
in the same instant the human presses enter (measured: a median of tens of
milliseconds before the `user` line); Codex opens a session with its meta,
environment and instruction lines before the first human message. Those are the
client's packaging of the human's turn, not work. Keying on the literal first
event scored a 66-hour corpus of human-driven root-thread work as 1.5 hours.
The head ends at the first agent event because that is the point where the run
stops being "the human starting something" and becomes "the model working".

    1     the thread is a ROOT thread and its span's head contains a
          `user_prompt`. A human typed: either they came back after the idle
          gap that ended the previous span (present: reading, thinking,
          typing), or this is the thread's opening prompt.
    0     everything else that is not NULL, i.e. either
          (a) the thread is a SUBAGENT thread -- structurally, never attended;
              see below -- or
          (b) some earlier span exists in this thread and its head holds no
              human turn: the thread resumed mid-agent-turn (a tool returned,
              the model emitted its next message) with no human input.
    NULL  a ROOT thread's FIRST span whose head holds no human turn.
          There is no preceding gap to classify and no human action to point
          at. Genuinely unknowable.

**Subagent threads are never `1`.** A human has no interface to type into a
subagent thread, so a human turn there is definitionally impossible. Their
first line *is* `user`-typed -- it carries the orchestrator's task prompt --
which is exactly the trap this gate exists to close: ungated, those injected
prompts scored as the largest block of "supervised" time in the corpus.

What this DOES NOT claim:

* Not "a human watched this whole span." Attendance is judged once, at the head
  of the span. A span marked 1 may run unattended for an hour afterwards.
* Not "the human was away" when 0. They may have been watching the agent work
  the entire time -- they simply did not start this run. They may also have
  been typing into a *different* thread, which is what concurrency measures.
* **Attendance is only ever asserted for root threads.** A 0 on a subagent span
  is structural, not a measurement: it says "a model spawned this", not "nobody
  was watching". Any dashboard that sums attended vs unattended time must say
  so, or it will read as a claim that the user abandoned work they may well
  have been supervising from the parent thread.
* Not an idle-time measure. The gap before a span is not part of any span, so
  `attended` never adds or removes active time; it only labels it.
* User prompts *after* the head are ignored: a prompt that follows an agent
  event is a mid-run interjection, not the thing that started the run.

RE-RUN SEMANTICS: delete-and-recompute, not upsert.
---------------------------------------------------
`derive()` deletes every span in its scope and recomputes from the events. It
is not an upsert, deliberately: a span id is `ids.span_id(thread_id,
started_at)`, so when a new event lands inside a former idle gap, or the idle
threshold changes, the surviving spans get *different* ids. An upsert would
write the new rows and leave the old ones behind forever, with no key that
ever matches them again -- the table would silently accumulate contradictory
history. Deleting the scope first is the only cheap way to guarantee the
invariant that matters: **the span table is exactly the function of the event
table**. Running derive twice in a row therefore yields identical rows, and
running it after new events land yields the correct rows rather than a merge
of two epochs. `thread.active_ms` and `session.active_ms` are recomputed from
the surviving spans in the same transaction.

CONCURRENCY is computed on demand by `concurrency()` / `concurrency_from_db()`
and is never persisted: it is a property of a *set* of spans (which set depends
on the filter you ask for), not of any one row.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from cc_insights import ids
from cc_insights.config import DEFAULT_IDLE_THRESHOLD_S

# The one string this module shares with the adapters. It is imported from the
# adapter *contract* (sources/base.py holds the normalized EventKind enum), not
# from any adapter, so a rename cannot silently desynchronize `attended` from
# what ingest writes into `event.kind`.
from cc_insights.sources.base import EventKind

__all__ = [
    "Span",
    "DeriveResult",
    "Concurrency",
    "derive",
    "spans_for_thread",
    "concurrency",
    "concurrency_from_db",
    "load_spans",
]

# SQLite's default limit is 999 host parameters per statement; stay well under.
_CHUNK = 400


@dataclass(frozen=True, slots=True)
class Span:
    """One active span, exactly as it is written to the `span` table."""

    id: str
    session_id: str
    thread_id: str
    started_at: int          # epoch ms UTC
    ended_at: int            # epoch ms UTC
    event_count: int
    attended: int | None     # 1 | 0 | None -- see the module docstring

    @property
    def duration_ms(self) -> int:
        return self.ended_at - self.started_at


@dataclass(frozen=True, slots=True)
class DeriveResult:
    """What one `derive()` run produced. Counts describe the scope derived."""

    threads: int             # threads that produced at least one span
    spans: int               # span rows written
    active_ms: int           # total active time across those spans
    sessions: int            # distinct sessions that produced at least one span
    idle_threshold_s: int


@dataclass(frozen=True, slots=True)
class Concurrency:
    """Sweep-line result over a set of spans. Plain data; never persisted."""

    time_at_level: dict[int, int] = field(default_factory=dict)  # level -> ms
    peak: int = 0
    peak_at: int | None = None   # epoch ms at which `peak` was first reached
    wall_ms: int = 0             # wall-clock with >= 1 span active

    def at_least(self, level: int) -> int:
        """Milliseconds with at least `level` spans running at once."""
        return sum(ms for lvl, ms in self.time_at_level.items() if lvl >= level)


# --------------------------------------------------------------------------
# pure span math
# --------------------------------------------------------------------------
def spans_for_thread(
    events: Sequence[tuple[int, str]],
    *,
    thread_id: str,
    session_id: str,
    idle_threshold_s: int = DEFAULT_IDLE_THRESHOLD_S,
    is_subagent: bool = False,
) -> list[Span]:
    """Split one thread's events into maximal active spans.

    `events` is `[(ts_ms, kind), ...]` **sorted by ts_ms**. A thread with fewer
    than two events yields no span. `is_subagent` only affects `attended`: a
    subagent thread has no human interface, so none of its spans can be 1.
    """
    if len(events) < 2:
        return []

    idle_ms = int(idle_threshold_s) * 1000
    out: list[Span] = []
    start = 0
    prev = 0
    for i in range(1, len(events)):
        if events[i][0] - events[prev][0] > idle_ms:
            out.append(_span(events, start, prev, thread_id, session_id, is_subagent))
            start = i
        prev = i
    out.append(_span(events, start, prev, thread_id, session_id, is_subagent))
    return out


def _span(
    events: Sequence[tuple[int, str]],
    start: int,
    end: int,
    thread_id: str,
    session_id: str,
    is_subagent: bool,
) -> Span:
    started_at = events[start][0]
    return Span(
        id=ids.span_id(thread_id, started_at),
        session_id=session_id,
        thread_id=thread_id,
        started_at=started_at,
        ended_at=events[end][0],
        event_count=end - start + 1,
        attended=_attended(
            events, start, end, is_first_span=start == 0, is_subagent=is_subagent
        ),
    )


# The head of a span ends at the first of these: past this point the run is the
# model working, not a human starting something.
_AGENT_KINDS = frozenset({
    str(EventKind.ASSISTANT), str(EventKind.TOOL_USE), str(EventKind.TOOL_RESULT),
})


def _attended(
    events: Sequence[tuple[int, str]],
    start: int,
    end: int,
    *,
    is_first_span: bool,
    is_subagent: bool,
) -> int | None:
    """See ATTENDED in the module docstring. This is the whole rule."""
    if is_subagent:
        # No human interface exists for this thread. Its opening `user_prompt`
        # is the orchestrator's task prompt, so it can never mean attendance.
        return 0
    for i in range(start, end + 1):
        kind = events[i][1]
        if kind == EventKind.USER_PROMPT:
            return 1         # a human turn opened the run
        if kind in _AGENT_KINDS:
            break            # head is over: the model took it from here
    if is_first_span:
        return None          # no preceding gap, no human turn: unknowable
    return 0                 # resumed mid-agent-turn


# --------------------------------------------------------------------------
# derive: event + thread -> span, thread.active_ms, session.active_ms
# --------------------------------------------------------------------------
def derive(
    conn: sqlite3.Connection,
    *,
    idle_threshold_s: int = DEFAULT_IDLE_THRESHOLD_S,
    session_ids: Sequence[str] | None = None,
) -> DeriveResult:
    """Recompute spans and active_ms. Delete-and-recompute; see the docstring.

    With `session_ids`, only those sessions are touched (their spans are
    deleted and rebuilt); every other session's rows are left alone. With
    `session_ids=None` the whole database is recomputed.
    """
    idle_threshold_s = int(idle_threshold_s)
    scope: list[str] | None = list(dict.fromkeys(session_ids)) if session_ids is not None else None

    # The read happens inside the write transaction: ingest may be appending
    # events concurrently (WAL), and computing spans from a snapshot taken
    # before the lock could delete rows that a newer event should have kept.
    owns_txn = not conn.in_transaction
    if owns_txn:
        conn.execute("BEGIN IMMEDIATE")
    try:
        computed = _compute(conn, idle_threshold_s, scope)
        _clear(conn, scope)
        conn.executemany(
            """
            INSERT INTO span
                (id, session_id, thread_id, started_at, ended_at, event_count, attended)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (s.id, s.session_id, s.thread_id, s.started_at, s.ended_at,
                 s.event_count, s.attended)
                for s in computed
            ],
        )
        _rollup(conn, scope)
    except Exception:
        if owns_txn:
            conn.execute("ROLLBACK")
        raise
    if owns_txn:
        conn.execute("COMMIT")

    return DeriveResult(
        threads=len({s.thread_id for s in computed}),
        spans=len(computed),
        active_ms=sum(s.duration_ms for s in computed),
        sessions=len({s.session_id for s in computed}),
        idle_threshold_s=idle_threshold_s,
    )


def _compute(
    conn: sqlite3.Connection,
    idle_threshold_s: int,
    scope: list[str] | None,
) -> list[Span]:
    """All spans for the scope, computed from `event` joined to `thread`.

    The thread row is authoritative for `session_id` and `is_subagent`:
    `thread.id` is derived from its session, so a thread can never straddle two
    sessions, and the subagent flag is a property of the thread, not of any one
    event.
    """
    sql = (
        "SELECT e.thread_id AS thread_id, t.session_id AS session_id, "
        "       t.is_subagent AS is_subagent, e.ts AS ts, e.kind AS kind "
        "FROM event e JOIN thread t ON t.id = e.thread_id "
    )
    # e.id breaks ts ties deterministically: it cannot change a span boundary
    # (those depend only on the sorted timestamps), but it keeps `attended`
    # and event_count stable across runs and engines when two events of one
    # thread share a millisecond.
    order = " ORDER BY e.thread_id, e.ts, e.id"

    rows: list[sqlite3.Row] = []
    if scope is None:
        rows = list(conn.execute(sql + order))
    else:
        for chunk in _chunks(scope):
            marks = ",".join("?" * len(chunk))
            rows += list(conn.execute(f"{sql} WHERE t.session_id IN ({marks}){order}", chunk))

    out: list[Span] = []
    current: list[tuple[int, str]] = []
    cur_thread: str | None = None
    cur_session: str | None = None
    cur_sub = False
    for r in rows:
        tid = r["thread_id"]
        if tid != cur_thread:
            if cur_thread is not None:
                out += spans_for_thread(
                    current, thread_id=cur_thread, session_id=cur_session,
                    idle_threshold_s=idle_threshold_s, is_subagent=cur_sub,
                )
            cur_thread, cur_session, current = tid, r["session_id"], []
            cur_sub = bool(r["is_subagent"])
        current.append((r["ts"], r["kind"]))
    if cur_thread is not None:
        out += spans_for_thread(
            current, thread_id=cur_thread, session_id=cur_session,
            idle_threshold_s=idle_threshold_s, is_subagent=cur_sub,
        )
    return out


def _clear(conn: sqlite3.Connection, scope: list[str] | None) -> None:
    """Drop the spans in scope and zero the active_ms they fed."""
    if scope is None:
        conn.execute("DELETE FROM span")
        conn.execute("UPDATE thread SET active_ms = 0")
        conn.execute("UPDATE session SET active_ms = 0")
        return
    for chunk in _chunks(scope):
        marks = ",".join("?" * len(chunk))
        conn.execute(f"DELETE FROM span WHERE session_id IN ({marks})", chunk)
        conn.execute(f"UPDATE thread SET active_ms = 0 WHERE session_id IN ({marks})", chunk)
        conn.execute(f"UPDATE session SET active_ms = 0 WHERE id IN ({marks})", chunk)


def _rollup(conn: sqlite3.Connection, scope: list[str] | None) -> None:
    """thread.active_ms / session.active_ms := the sum of their spans.

    Correlated-subquery UPDATE: the portable form, valid on SQLite and
    PostgreSQL alike (see the portability contract in 001_init.sql).
    """
    thread_sql = (
        "UPDATE thread SET active_ms = COALESCE("
        "  (SELECT SUM(s.ended_at - s.started_at) FROM span s WHERE s.thread_id = thread.id), 0)"
    )
    session_sql = (
        "UPDATE session SET active_ms = COALESCE("
        "  (SELECT SUM(s.ended_at - s.started_at) FROM span s WHERE s.session_id = session.id), 0)"
    )
    if scope is None:
        conn.execute(thread_sql)
        conn.execute(session_sql)
        return
    for chunk in _chunks(scope):
        marks = ",".join("?" * len(chunk))
        conn.execute(f"{thread_sql} WHERE thread.session_id IN ({marks})", chunk)
        conn.execute(f"{session_sql} WHERE session.id IN ({marks})", chunk)


def _chunks(values: Sequence[str], size: int | None = None) -> Iterable[Sequence[str]]:
    size = size or _CHUNK   # read at call time so the limit stays configurable
    for i in range(0, len(values), size):
        yield values[i : i + size]


# --------------------------------------------------------------------------
# concurrency
# --------------------------------------------------------------------------
def concurrency(intervals: Iterable[tuple[int, int]]) -> Concurrency:
    """Sweep-line over `(started_at, ended_at)` pairs -> time at each level.

    Ends are processed before starts at the same instant, so two spans that
    merely touch (one ends exactly when the next begins) are never counted as
    concurrent. Zero-duration spans therefore contribute no time at any level,
    which is the honest answer: an instant has no width.

    Returns milliseconds per level; `wall_ms` is the time with >= 1 active.
    """
    points: list[tuple[int, int]] = []
    for start, end in intervals:
        points.append((start, 1))
        points.append((end, -1))
    points.sort()

    at: Counter[int] = Counter()
    cur = 0
    last: int | None = None
    peak = 0
    peak_at: int | None = None
    for t, delta in points:
        if last is not None and cur > 0:
            at[cur] += t - last
        cur += delta
        last = t
        if cur > peak:
            peak, peak_at = cur, t

    return Concurrency(
        time_at_level={k: at[k] for k in sorted(at)},
        peak=peak,
        peak_at=peak_at,
        wall_ms=sum(at.values()),
    )


def concurrency_from_db(
    conn: sqlite3.Connection,
    *,
    session_ids: Sequence[str] | None = None,
    source: str | None = None,
) -> Concurrency:
    """Concurrency over the persisted spans, optionally filtered.

    `source` ('claude_code' | 'codex' | 'opencode') filters through the owning
    session, so
    the two tools can be measured separately -- mixing them would report a
    concurrency no single tool ever reached.
    """
    return concurrency(
        (s.started_at, s.ended_at) for s in load_spans(conn, session_ids=session_ids, source=source)
    )


def load_spans(
    conn: sqlite3.Connection,
    *,
    session_ids: Sequence[str] | None = None,
    source: str | None = None,
) -> list[Span]:
    """Read span rows back, ordered by start time."""
    sql = (
        "SELECT sp.id, sp.session_id, sp.thread_id, sp.started_at, sp.ended_at, "
        "       sp.event_count, sp.attended "
        "FROM span sp JOIN session se ON se.id = sp.session_id"
    )
    where: list[str] = []
    params: list[str] = []
    if source is not None:
        where.append("se.source = ?")
        params.append(source)

    rows: list[sqlite3.Row] = []
    if session_ids is None:
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = list(conn.execute(sql + clause + " ORDER BY sp.started_at, sp.id", params))
    else:
        for chunk in _chunks(list(dict.fromkeys(session_ids))):
            marks = ",".join("?" * len(chunk))
            clause = " WHERE " + " AND ".join([*where, f"sp.session_id IN ({marks})"])
            rows += list(conn.execute(sql + clause, [*params, *chunk]))
        rows.sort(key=lambda r: (r["started_at"], r["id"]))

    return [
        Span(
            id=r["id"],
            session_id=r["session_id"],
            thread_id=r["thread_id"],
            started_at=r["started_at"],
            ended_at=r["ended_at"],
            event_count=r["event_count"],
            attended=r["attended"],
        )
        for r in rows
    ]
