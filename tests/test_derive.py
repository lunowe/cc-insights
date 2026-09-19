"""Tests for the derivation engine.

The synthetic tests pin the span/attended/concurrency rules. The two corpus
tests at the bottom are the real acceptance gate: they load
docs/probes/canonical_metrics.py -- the spec -- in the *same process*, push its
events through a throwaway database, run derive over it, and assert exact
equality of the span set and the concurrency result. Comparing against a frozen
number would be meaningless: the corpus grows while you measure it
(docs/FINDINGS.md §0b).
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from cc_insights import derive, ids
from cc_insights.sources.base import EventKind

REPO = Path(__file__).resolve().parents[1]
PROBE = REPO / "docs" / "probes" / "canonical_metrics.py"

HOST = "test-host"
T0 = 1_700_000_000_000          # a round epoch-ms base for readable fixtures
MIN = 60_000
IDLE_S = 300
IDLE_MS = IDLE_S * 1000


# --------------------------------------------------------------------------
# fixture helpers -- a test-only loader, deliberately NOT production ingest
# --------------------------------------------------------------------------
def _host(conn: sqlite3.Connection, host_id: str = HOST) -> str:
    conn.execute(
        "INSERT OR IGNORE INTO host (host_id, hostname, os, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?, ?)",
        (host_id, "testbox", "test-os", T0, T0),
    )
    return host_id


def _session(
    conn: sqlite3.Connection,
    native_id: str,
    *,
    source: str = "claude_code",
    host_id: str = HOST,
    started_at: int = T0,
    ended_at: int = T0,
) -> str:
    _host(conn, host_id)
    sid = ids.session_id(host_id, source, native_id)
    conn.execute(
        "INSERT INTO session (id, native_id, source, host_id, started_at, ended_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (sid, native_id, source, host_id, started_at, ended_at),
    )
    return sid


def _thread(
    conn: sqlite3.Connection,
    session_id: str,
    native_id: str,
    events: list[tuple[int, str]],
    *,
    is_subagent: bool = False,
) -> str:
    """Insert a thread plus `events` = [(ts_ms, kind)]. Returns the thread id."""
    tid = ids.thread_id(session_id, native_id)
    ts = [t for t, _ in events]
    conn.execute(
        "INSERT INTO thread (id, native_id, session_id, is_subagent, started_at, ended_at, "
        "                    event_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (tid, native_id, session_id, int(is_subagent),
         min(ts, default=T0), max(ts, default=T0), len(events)),
    )
    conn.executemany(
        "INSERT INTO event (id, session_id, thread_id, native_event_id, ts, ordinal, kind) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (ids.event_id(session_id, f"{native_id}:{i}"), session_id, tid,
             f"{native_id}:{i}", t, i, str(kind))
            for i, (t, kind) in enumerate(events)
        ],
    )
    return tid


def _spans(conn: sqlite3.Connection, thread_id: str | None = None) -> list[derive.Span]:
    rows = derive.load_spans(conn)
    if thread_id is not None:
        rows = [s for s in rows if s.thread_id == thread_id]
    return sorted(rows, key=lambda s: s.started_at)


U = str(EventKind.USER_PROMPT)
A = str(EventKind.ASSISTANT)
TR = str(EventKind.TOOL_RESULT)
SYS = str(EventKind.SYSTEM)
OTHER = str(EventKind.OTHER)


# --------------------------------------------------------------------------
# span boundaries
# --------------------------------------------------------------------------
def test_gap_exactly_at_threshold_stays_one_span(conn):
    s = _session(conn, "s-edge")
    t = _thread(conn, s, "t", [(T0, U), (T0 + IDLE_MS, A)])
    derive.derive(conn, idle_threshold_s=IDLE_S)

    spans = _spans(conn, t)
    assert len(spans) == 1
    assert (spans[0].started_at, spans[0].ended_at) == (T0, T0 + IDLE_MS)
    assert spans[0].event_count == 2


def test_gap_just_under_threshold_stays_one_span(conn):
    s = _session(conn, "s-under")
    t = _thread(conn, s, "t", [(T0, U), (T0 + IDLE_MS - 1, A)])
    derive.derive(conn, idle_threshold_s=IDLE_S)

    spans = _spans(conn, t)
    assert len(spans) == 1
    assert spans[0].duration_ms == IDLE_MS - 1


def test_gap_just_over_threshold_splits_and_the_gap_contributes_zero(conn):
    s = _session(conn, "s-over")
    t = _thread(conn, s, "t", [(T0, U), (T0 + IDLE_MS + 1, A), (T0 + IDLE_MS + 1 + MIN, A)])
    derive.derive(conn, idle_threshold_s=IDLE_S)

    spans = _spans(conn, t)
    assert len(spans) == 2
    assert (spans[0].started_at, spans[0].ended_at) == (T0, T0)
    assert (spans[1].started_at, spans[1].ended_at) == (T0 + IDLE_MS + 1, T0 + IDLE_MS + 1 + MIN)
    # The idle gap contributes nothing: total is the second span only, NOT
    # capped-and-counted (which would have added another IDLE_MS).
    assert sum(sp.duration_ms for sp in spans) == MIN
    row = conn.execute("SELECT active_ms FROM thread WHERE id = ?", (t,)).fetchone()
    assert row["active_ms"] == MIN


def test_single_event_thread_produces_no_span(conn):
    s = _session(conn, "s-single")
    t = _thread(conn, s, "t", [(T0, U)])
    result = derive.derive(conn, idle_threshold_s=IDLE_S)

    assert _spans(conn, t) == []
    assert result.spans == 0
    assert conn.execute("SELECT active_ms FROM thread WHERE id = ?", (t,)).fetchone()[0] == 0
    assert conn.execute("SELECT active_ms FROM session WHERE id = ?", (s,)).fetchone()[0] == 0


def test_isolated_event_inside_a_multi_event_thread_is_a_zero_duration_span(conn):
    """Matches the spec probe: an instant surrounded by idleness is still a row."""
    s = _session(conn, "s-iso")
    t = _thread(conn, s, "t", [(T0, U), (T0 + 10 * IDLE_MS, A), (T0 + 20 * IDLE_MS, A)])
    derive.derive(conn, idle_threshold_s=IDLE_S)

    spans = _spans(conn, t)
    assert len(spans) == 3
    assert all(sp.duration_ms == 0 for sp in spans)
    assert all(sp.event_count == 1 for sp in spans)
    assert conn.execute("SELECT active_ms FROM thread WHERE id = ?", (t,)).fetchone()[0] == 0


def test_spans_are_per_thread_never_merged_per_session(conn):
    """Two threads interleaved in time stay two spans, not one merged stream."""
    s = _session(conn, "s-per-thread")
    a = _thread(conn, s, "a", [(T0, U), (T0 + 4 * MIN, A)])
    b = _thread(conn, s, "b", [(T0 + 2 * MIN, A), (T0 + 6 * MIN, A)])
    derive.derive(conn, idle_threshold_s=IDLE_S)

    assert len(_spans(conn, a)) == 1
    assert len(_spans(conn, b)) == 1
    # Session active time is the SUM of its threads: 4 + 4 minutes of work
    # inside 6 minutes of wall-clock. That excess is the parallelism signal.
    assert conn.execute("SELECT active_ms FROM session WHERE id = ?", (s,)).fetchone()[0] == 8 * MIN


# --------------------------------------------------------------------------
# attended
# --------------------------------------------------------------------------
def test_attended_is_1_when_the_gap_ends_at_a_user_prompt(conn):
    s = _session(conn, "s-att")
    t = _thread(conn, s, "t", [
        (T0, U), (T0 + MIN, A),
        (T0 + 60 * MIN, U), (T0 + 61 * MIN, A),      # human came back and typed
    ])
    derive.derive(conn, idle_threshold_s=IDLE_S)

    spans = _spans(conn, t)
    assert [sp.attended for sp in spans] == [1, 1]


def test_attended_is_0_when_work_resumes_mid_agent_turn(conn):
    s = _session(conn, "s-unatt")
    t = _thread(conn, s, "t", [
        (T0, U), (T0 + MIN, A),
        (T0 + 60 * MIN, TR), (T0 + 61 * MIN, A),     # a tool returned; no human turn
    ])
    derive.derive(conn, idle_threshold_s=IDLE_S)

    spans = _spans(conn, t)
    assert [sp.attended for sp in spans] == [1, 0]


def test_attended_is_null_for_a_first_span_with_no_human_action(conn):
    """A root thread that opens straight into model work: nothing to classify."""
    s = _session(conn, "s-null")
    t = _thread(conn, s, "t", [(T0, A), (T0 + MIN, TR)])
    derive.derive(conn, idle_threshold_s=IDLE_S)

    spans = _spans(conn, t)
    assert [sp.attended for sp in spans] == [None]
    raw = conn.execute("SELECT attended FROM span WHERE thread_id = ?", (t,)).fetchone()
    assert raw["attended"] is None


def test_attended_counts_a_prompt_behind_the_clients_own_bookkeeping(conn):
    """Neither CLI writes the human's turn first.

    Claude Code emits `queue-operation` / `attachment` lines milliseconds
    before the `user` line, and Codex opens with meta and environment lines.
    Keying on the literal first event scored 66 h of human-driven root work as
    1.5 h on the real corpus.
    """
    s = _session(conn, "s-head")
    t = _thread(conn, s, "t", [
        (T0, OTHER), (T0 + 14, OTHER), (T0 + 14, U), (T0 + MIN, A),      # session opens
        (T0 + 60 * MIN, SYS), (T0 + 60 * MIN + 70, U), (T0 + 61 * MIN, A),  # human returns
    ])
    derive.derive(conn, idle_threshold_s=IDLE_S)

    assert [sp.attended for sp in _spans(conn, t)] == [1, 1]


def test_attended_head_ends_at_the_first_agent_event(conn):
    """A prompt after the model has spoken is a mid-run interjection, not a start."""
    s = _session(conn, "s-headend")
    t = _thread(conn, s, "t", [
        (T0, U), (T0 + MIN, A),
        (T0 + 60 * MIN, SYS), (T0 + 60 * MIN + 1, A), (T0 + 61 * MIN, U),
    ])
    derive.derive(conn, idle_threshold_s=IDLE_S)

    assert [sp.attended for sp in _spans(conn, t)] == [1, 0]


def test_user_prompt_after_the_head_does_not_change_attended(conn):
    """The flag classifies the entry into the span, not its contents."""
    s = _session(conn, "s-mid")
    t = _thread(conn, s, "t", [(T0, A), (T0 + MIN, U), (T0 + 2 * MIN, A)])
    derive.derive(conn, idle_threshold_s=IDLE_S)

    assert [sp.attended for sp in _spans(conn, t)] == [None]


def test_subagent_spans_are_never_attended(conn):
    """A Claude subagent transcript opens with a `user`-typed line, but it is
    the orchestrator's task prompt: a human has no interface to type there."""
    s = _session(conn, "s-sub")
    root = _thread(conn, s, "root", [(T0, U), (T0 + MIN, A)])
    sub = _thread(conn, s, "sub", [
        (T0 + MIN, U), (T0 + 2 * MIN, A),                 # injected task prompt
        (T0 + 60 * MIN, U), (T0 + 61 * MIN, A),           # and again after idling
    ], is_subagent=True)
    derive.derive(conn, idle_threshold_s=IDLE_S)

    assert [sp.attended for sp in _spans(conn, sub)] == [0, 0]
    assert [sp.attended for sp in _spans(conn, root)] == [1]


def test_subagent_first_span_is_zero_not_unknown(conn):
    """Structurally knowable: a model spawned it. That is not 'unknowable'."""
    s = _session(conn, "s-sub2")
    t = _thread(conn, s, "sub", [(T0, A), (T0 + MIN, TR)], is_subagent=True)
    derive.derive(conn, idle_threshold_s=IDLE_S)

    assert [sp.attended for sp in _spans(conn, t)] == [0]


def test_no_span_of_a_subagent_thread_is_ever_attended_whatever_its_kinds(conn):
    s = _session(conn, "s-sub3")
    kinds = [U, A, TR, SYS, OTHER, str(EventKind.TOOL_USE)]
    events = [(T0 + i * MIN, k) for i, k in enumerate(kinds)]
    t = _thread(conn, s, "sub", events, is_subagent=True)
    derive.derive(conn, idle_threshold_s=IDLE_S)

    assert {sp.attended for sp in _spans(conn, t)} == {0}


# --------------------------------------------------------------------------
# concurrency
# --------------------------------------------------------------------------
def test_two_threads_of_one_session_overlap_gives_concurrency_2(conn):
    s = _session(conn, "s-conc")
    _thread(conn, s, "a", [(T0, U), (T0 + 4 * MIN, A)])
    _thread(conn, s, "b", [(T0 + 2 * MIN, A), (T0 + 6 * MIN, A)])
    derive.derive(conn, idle_threshold_s=IDLE_S)

    c = derive.concurrency_from_db(conn)
    assert c.peak == 2
    assert c.peak_at == T0 + 2 * MIN
    assert c.time_at_level == {1: 4 * MIN, 2: 2 * MIN}
    assert c.wall_ms == 6 * MIN
    assert c.at_least(2) == 2 * MIN
    # 8 min of active time inside 6 min of wall-clock = 1.33x parallelism.
    total = conn.execute("SELECT SUM(ended_at - started_at) FROM span").fetchone()[0]
    assert total == 8 * MIN


def test_concurrency_touching_spans_are_not_concurrent(conn):
    c = derive.concurrency([(0, 100), (100, 200)])
    assert c.peak == 1
    assert c.time_at_level == {1: 200}


def test_concurrency_of_nothing(conn):
    c = derive.concurrency([])
    assert (c.peak, c.peak_at, c.wall_ms, c.time_at_level) == (0, None, 0, {})


def test_concurrency_can_be_filtered_by_source(conn):
    s1 = _session(conn, "s-cc", source="claude_code")
    s2 = _session(conn, "s-cx", source="codex")
    _thread(conn, s1, "a", [(T0, U), (T0 + 4 * MIN, A)])
    _thread(conn, s2, "b", [(T0, U), (T0 + 4 * MIN, A)])
    derive.derive(conn, idle_threshold_s=IDLE_S)

    assert derive.concurrency_from_db(conn).peak == 2
    assert derive.concurrency_from_db(conn, source="claude_code").peak == 1
    assert derive.concurrency_from_db(conn, source="codex").peak == 1


def test_concurrency_is_not_persisted(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(span)")}
    assert "concurrency" not in cols and "level" not in cols


# --------------------------------------------------------------------------
# re-runnability
# --------------------------------------------------------------------------
def _snapshot(conn: sqlite3.Connection):
    return (
        [tuple(r) for r in conn.execute(
            "SELECT id, session_id, thread_id, started_at, ended_at, event_count, attended "
            "FROM span ORDER BY id")],
        [tuple(r) for r in conn.execute("SELECT id, active_ms FROM thread ORDER BY id")],
        [tuple(r) for r in conn.execute("SELECT id, active_ms FROM session ORDER BY id")],
    )


def test_derive_is_rerunnable_without_duplicating_rows(conn):
    s = _session(conn, "s-rerun")
    _thread(conn, s, "a", [(T0, U), (T0 + MIN, A), (T0 + 60 * MIN, U), (T0 + 61 * MIN, A)])
    _thread(conn, s, "b", [(T0 + MIN, A), (T0 + 2 * MIN, A)])

    first = derive.derive(conn, idle_threshold_s=IDLE_S)
    snap = _snapshot(conn)
    for _ in range(3):
        again = derive.derive(conn, idle_threshold_s=IDLE_S)
        assert again == first
        assert _snapshot(conn) == snap

    assert conn.execute("SELECT COUNT(*) FROM span").fetchone()[0] == 3


def test_rerun_after_new_events_replaces_rows_rather_than_merging(conn):
    s = _session(conn, "s-grow")
    t = _thread(conn, s, "a", [(T0, U), (T0 + 60 * MIN, U)])
    derive.derive(conn, idle_threshold_s=IDLE_S)
    assert len(_spans(conn, t)) == 2

    # An event lands inside the former idle gap, bridging the two spans.
    conn.execute(
        "INSERT INTO event (id, session_id, thread_id, native_event_id, ts, kind) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (ids.event_id(s, "a:new"), s, t, "a:new", T0 + 30 * MIN, A),
    )
    derive.derive(conn, idle_threshold_s=IDLE_S)

    spans = _spans(conn, t)
    assert len(spans) == 3, "old span rows must be deleted, not left behind"
    assert conn.execute("SELECT COUNT(*) FROM span").fetchone()[0] == 3


def test_rerun_with_a_different_threshold_leaves_no_stale_rows(conn):
    s = _session(conn, "s-thresh")
    t = _thread(conn, s, "a", [(T0, U), (T0 + 2 * MIN, A)])
    derive.derive(conn, idle_threshold_s=IDLE_S)
    assert len(_spans(conn, t)) == 1

    derive.derive(conn, idle_threshold_s=60)       # now the 2-minute gap is idle
    assert conn.execute("SELECT COUNT(*) FROM span").fetchone()[0] == 2
    assert conn.execute("SELECT active_ms FROM thread WHERE id = ?", (t,)).fetchone()[0] == 0


def test_scoped_derive_touches_only_its_sessions(conn):
    s1 = _session(conn, "s-one")
    s2 = _session(conn, "s-two")
    _thread(conn, s1, "a", [(T0, U), (T0 + MIN, A)])
    _thread(conn, s2, "b", [(T0, U), (T0 + 2 * MIN, A)])
    derive.derive(conn, idle_threshold_s=IDLE_S)
    before = _snapshot(conn)

    result = derive.derive(conn, idle_threshold_s=IDLE_S, session_ids=[s1])
    assert result.sessions == 1 and result.spans == 1
    assert _snapshot(conn) == before
    untouched = conn.execute("SELECT active_ms FROM session WHERE id = ?", (s2,)).fetchone()
    assert untouched[0] == 2 * MIN


def test_scoped_derive_chunks_its_id_lists(conn, monkeypatch):
    """The IN (...) clauses are chunked to stay under SQLite's parameter limit."""
    monkeypatch.setattr(derive, "_CHUNK", 2)
    sessions = []
    for i in range(5):
        s = _session(conn, f"s-chunk-{i}")
        _thread(conn, s, f"t{i}", [(T0, U), (T0 + MIN, A)])
        sessions.append(s)

    result = derive.derive(conn, idle_threshold_s=IDLE_S, session_ids=sessions)
    assert (result.sessions, result.spans, result.active_ms) == (5, 5, 5 * MIN)

    again = derive.derive(conn, idle_threshold_s=IDLE_S, session_ids=sessions)
    assert again == result
    assert conn.execute("SELECT COUNT(*) FROM span").fetchone()[0] == 5
    assert len(derive.load_spans(conn, session_ids=sessions)) == 5


def test_derive_over_an_empty_database(conn):
    result = derive.derive(conn, idle_threshold_s=IDLE_S)
    assert (result.spans, result.threads, result.active_ms, result.sessions) == (0, 0, 0, 0)


# --------------------------------------------------------------------------
# acceptance: exact agreement with the spec probe, in ONE process
# --------------------------------------------------------------------------
def _probe():
    spec = importlib.util.spec_from_file_location("canonical_metrics", PROBE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_probe_events(conn: sqlite3.Connection, ev: dict, source: str) -> None:
    """Test-only loader: {(session, thread): {event_id: ts}} -> the schema.

    This is a fixture, not ingest. It fabricates only what the schema demands
    (host/session/thread rows); the events keep the probe's own ids and
    timestamps, converted to epoch ms. `kind` is unknowable from the probe's
    output, so every event is OTHER -- this fixture verifies the span and
    concurrency math, not `attended`.
    """
    _host(conn)
    bounds: dict[str, list[int]] = {}
    threads: list[tuple] = []
    events: list[tuple] = []

    for (native_sid, native_tid), by_id in ev.items():
        sid = ids.session_id(HOST, source, native_sid)
        tid = ids.thread_id(sid, native_tid)
        ts = sorted(round(t * 1000) for t in by_id.values())
        lo, hi = ts[0], ts[-1]
        b = bounds.setdefault(sid, [lo, hi])
        b[0], b[1] = min(b[0], lo), max(b[1], hi)
        threads.append((tid, native_tid, sid, int(native_tid != native_sid), lo, hi, len(ts)))
        events += [
            (ids.event_id(sid, nid), sid, tid, nid, round(t * 1000), str(EventKind.OTHER))
            for nid, t in by_id.items()
        ]

    conn.execute("BEGIN")
    conn.executemany(
        "INSERT INTO session (id, native_id, source, host_id, started_at, ended_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(sid, sid, source, HOST, lo, hi) for sid, (lo, hi) in bounds.items()],
    )
    conn.executemany(
        "INSERT INTO thread (id, native_id, session_id, is_subagent, started_at, ended_at, "
        "event_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
        threads,
    )
    # Plain INSERT, not INSERT OR IGNORE: a native-id collision inside one
    # session would mean the probe and the schema disagree about dedup, and
    # that must fail loudly rather than quietly drop events.
    conn.executemany(
        "INSERT INTO event (id, session_id, thread_id, native_event_id, ts, kind) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        events,
    )
    conn.execute("COMMIT")


def _assert_matches_probe(conn: sqlite3.Connection, cm, ev: dict, source: str) -> None:
    _load_probe_events(conn, ev, source)
    result = derive.derive(conn, idle_threshold_s=cm.IDLE)

    expected_spans = [
        (round(a * 1000), round(b * 1000), ids.thread_id(ids.session_id(HOST, source, sid), tid))
        for a, b, sid, tid in cm.spans(ev)
    ]
    got = derive.load_spans(conn)
    got_spans = [(s.started_at, s.ended_at, s.thread_id) for s in got]

    assert len(got_spans) == len(expected_spans)
    assert set(got_spans) == set(expected_spans)
    assert sorted(got_spans) == sorted(expected_spans)

    # active time: identical to the probe, to the millisecond
    expected_active = sum(b - a for a, b, _ in expected_spans)
    assert result.active_ms == expected_active
    assert sum(s.duration_ms for s in got) == expected_active
    assert conn.execute("SELECT SUM(active_ms) FROM thread").fetchone()[0] == expected_active
    assert conn.execute("SELECT SUM(active_ms) FROM session").fetchone()[0] == expected_active

    # concurrency: the probe's own sweep-line, fed the same ms intervals
    at, peak, peak_at, wall = cm.concurrency([(a, b) for a, b, _ in expected_spans])
    mine = derive.concurrency_from_db(conn)
    assert mine.time_at_level == dict(at)
    assert mine.peak == peak
    assert mine.peak_at == peak_at
    assert mine.wall_ms == wall

    # ... and the float-seconds run the probe's own report() would print
    _, f_peak, f_peak_at, f_wall = cm.concurrency(cm.spans(ev))
    assert f_peak == peak
    assert round(f_peak_at * 1000) == peak_at

    print(
        f"\n=== {source} (probe agreement, one process) ===\n"
        f"threads            : {len(ev)}\n"
        f"events             : {sum(len(m) for m in ev.values()):,}\n"
        f"spans              : {len(got_spans)} (probe {len(expected_spans)})\n"
        f"ACTIVE TIME        : {result.active_ms / 3_600_000:.2f} h "
        f"(probe {sum(b - a for a, b, *_ in cm.spans(ev)) / 3600:.2f} h)\n"
        f"wall-clock >=1     : {mine.wall_ms / 3_600_000:.2f} h (probe {f_wall / 3600:.2f} h)\n"
        f"multiplier         : {result.active_ms / mine.wall_ms:.2f}x\n"
        f"peak concurrency   : {mine.peak} at ms {mine.peak_at}\n"
        f">=2 concurrent     : {mine.at_least(2) / 3_600_000:.2f} h\n"
        + "".join(
            f"   {k:>2} concurrent  : {v / 3_600_000:6.2f} h\n"
            for k, v in sorted(mine.time_at_level.items())
        )
    )


@pytest.mark.parametrize("source", ["claude_code", "codex"])
def test_matches_canonical_probe_exactly(conn, source):
    if not PROBE.exists():
        pytest.skip("spec probe not present")
    cm = _probe()
    ev = cm.load_claude() if source == "claude_code" else cm.load_codex()
    if not ev:
        pytest.skip(f"no {source} logs on this machine")
    _assert_matches_probe(conn, cm, ev, source)
