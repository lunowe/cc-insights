"""Tests for the ingest layer.

Two rules shape this file.

**Never assert against a frozen corpus count.** `docs/FINDINGS.md` §0b: this
machine writes agent logs continuously, including while the test runs. Every
test that touches the real corpus either works on a *snapshot copied into
tmp_path* (frozen by construction) or compares KEY SETS gathered in the same
process against exactly the bytes ingest recorded reading.

**Idempotency is the load-bearing property.** A second run must insert zero
rows and leave every table count identical -- that is what makes `cci ingest`
safe to run on a timer.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Iterator, Sequence

import pytest

from cc_insights import db, ids, ingest
from cc_insights.config import DEFAULT_SOURCE_GLOBS, Config
from cc_insights.sources.base import EventKind, RawEvent
from cc_insights.sources.claude_code import ClaudeCodeAdapter
from cc_insights.sources.codex import CodexAdapter

FIXTURES = Path(__file__).parent / "fixtures"
TABLES = ("project", "session", "thread", "event", "span", "ingest_file")

REAL_CORPUS = os.environ.get("CC_INSIGHTS_REAL_CORPUS") == "1"
_real_only = pytest.mark.skipif(
    not REAL_CORPUS,
    reason="set CC_INSIGHTS_REAL_CORPUS=1 to run against ~/.claude and ~/.codex",
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def make_config(tmp_path: Path, **globs: Sequence[str]) -> Config:
    """A Config pointed at `tmp_path` with explicit per-source glob patterns."""
    return Config(
        host_id="host-under-test",
        hostname="test-host",
        db_path=tmp_path / "test.db",
        source_globs={k: [str(p) for p in v] for k, v in globs.items()},
        config_dir=tmp_path,
    )


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}


def write_jsonl(path: Path, records: Sequence[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def claude_line(
    session: str,
    uuid: str,
    ts: str,
    *,
    agent_id: str | None = None,
    cwd: str = "/Users/demo/Coding/Demo",
    branch: str = "main",
    version: str = "2.0.0",
    attribution: str | None = None,
) -> dict:
    """One Claude Code transcript line. `agent_id` makes it a subagent line."""
    rec: dict = {
        "parentUuid": None,
        "isSidechain": agent_id is not None,
        "userType": "external",
        "cwd": cwd,
        "sessionId": session,
        "version": version,
        "gitBranch": branch,
        "type": "user",
        "message": {"role": "user", "content": "hi"},
        "uuid": uuid,
        "timestamp": ts,
    }
    if agent_id is not None:
        rec["agentId"] = agent_id
    if attribution is not None:
        rec["attributionAgent"] = attribution
        rec["type"] = "assistant"
        rec["message"] = {"role": "assistant", "model": "claude-opus-5", "content": []}
    return rec


def codex_meta(thread: str, session: str, ts: str, *, parent: str | None = None, **extra) -> dict:
    payload: dict = {
        "session_id": session,
        "id": thread,
        "cwd": "/Users/demo/Coding/Demo",
        "cli_version": "0.98.0",
        "source": {"subagent": {"thread_spawn": {}}} if parent else "cli",
    }
    if parent:
        payload["parent_thread_id"] = parent
    payload.update(extra)
    return {"timestamp": ts, "ordinal": 0, "type": "session_meta", "payload": payload}


def codex_line(ts: str, ordinal: int, role: str = "user") -> dict:
    return {
        "timestamp": ts,
        "ordinal": ordinal,
        "type": "response_item",
        "payload": {"type": "message", "role": role, "content": []},
    }


def fresh_db(cfg: Config) -> sqlite3.Connection:
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    return conn


class FakeAdapter:
    """Adapter that yields canned events, and can be told to explode on a file."""

    def __init__(self, name: str, events: dict[Path, list[RawEvent]], boom: Path | None = None):
        self.name = name
        self._events = events
        self._boom = boom

    def discover(self) -> list[Path]:
        return sorted(self._events)

    def parse(self, path: Path, from_byte: int = 0) -> Iterator[RawEvent]:
        for ev in self._events[Path(path)]:
            if ev.byte_end <= from_byte:
                continue
            yield ev
        if self._boom is not None and Path(path) == self._boom:
            raise RuntimeError("malformed log file")


def fake_event(session: str, thread: str, nid: str, ts: int, byte_end: int, **kw) -> RawEvent:
    return RawEvent(
        source="fake",
        native_session_id=session,
        native_thread_id=thread,
        native_event_id=nid,
        ts_ms=ts,
        kind=EventKind.USER_PROMPT,
        cwd="/Users/demo/Coding/Demo",
        byte_end=byte_end,
        **kw,
    )


# --------------------------------------------------------------------------
# the project root rule
# --------------------------------------------------------------------------


def test_project_root_is_the_cwd_lexically_normalized():
    assert ingest.project_root("/Users/demo/Coding/Demo") == "/Users/demo/Coding/Demo"
    assert ingest.project_root("/Users/demo/Coding/Demo/") == "/Users/demo/Coding/Demo"
    assert ingest.project_root("/Users/demo//Coding/./Demo") == "/Users/demo/Coding/Demo"
    assert ingest.project_root("/Users/demo/Coding/Other/../Demo") == "/Users/demo/Coding/Demo"


def test_project_root_never_touches_the_filesystem(tmp_path: Path):
    """The rule must be a pure function of the logged string: `project_id` is a
    hash of it, so a git-root walk would mint a second project the moment the
    checkout moved or was deleted."""
    gone = str(tmp_path / "definitely" / "not" / "here")
    assert ingest.project_root(gone) == gone
    assert ingest.project_name(gone) == "here"


def test_project_root_none_for_missing_cwd():
    assert ingest.project_root(None) is None
    assert ingest.project_root("") is None
    assert ingest.project_root("   ") is None


def test_project_name_is_the_last_segment():
    assert ingest.project_name("/Users/demo/Coding/Demo") == "Demo"
    assert ingest.project_name("/") == "/"


# --------------------------------------------------------------------------
# basic shape
# --------------------------------------------------------------------------


def test_ingest_populates_all_four_tables(tmp_path: Path):
    cc, cx = tmp_path / "cc", tmp_path / "cx"
    for name in ("resume_replay_a.jsonl", "resume_replay_b.jsonl"):
        (cc).mkdir(exist_ok=True)
        shutil.copy(FIXTURES / "claude_code" / name, cc / name)
    for name in ("root_thread.jsonl", "subagent_thread.jsonl"):
        (cx).mkdir(exist_ok=True)
        shutil.copy(FIXTURES / "codex" / name, cx / name)

    cfg = make_config(tmp_path, claude_code=[cc / "*.jsonl"], codex=[cx / "*.jsonl"])
    conn = fresh_db(cfg)
    stats = ingest.ingest(conn, cfg)

    assert stats.files_failed == 0 and stats.errors == []
    assert stats.files_ingested == 4
    n = counts(conn)
    assert n["session"] == 2          # one Claude session (2 files), one Codex session
    assert n["thread"] == 3           # Claude root + Codex root + Codex subagent
    assert n["event"] > 0
    assert n["project"] == 1          # both fixtures live in the same cwd
    assert n["span"] == 0             # WP5 owns span


def test_ids_come_from_the_ids_module(tmp_path: Path):
    cc = tmp_path / "cc"
    write_jsonl(
        cc / "s.jsonl",
        [claude_line("sess-1", "u-1", "2026-09-01T10:00:00.000Z")],
    )
    cfg = make_config(tmp_path, claude_code=[cc / "*.jsonl"])
    conn = fresh_db(cfg)
    ingest.ingest(conn, cfg)

    sid = ids.session_id(cfg.host_id, "claude_code", "sess-1")
    tid = ids.thread_id(sid, "sess-1")
    eid = ids.event_id(sid, "u-1")
    pid = ids.project_id("/Users/demo/Coding/Demo")

    assert conn.execute("SELECT id FROM session").fetchone()[0] == sid
    assert conn.execute("SELECT id FROM thread").fetchone()[0] == tid
    assert conn.execute("SELECT id FROM event").fetchone()[0] == eid
    assert conn.execute("SELECT project_id FROM project").fetchone()[0] == pid
    assert conn.execute("SELECT project_id FROM session").fetchone()[0] == pid


def test_span_and_active_ms_are_left_to_wp5(tmp_path: Path):
    cc = tmp_path / "cc"
    write_jsonl(
        cc / "s.jsonl",
        [
            claude_line("sess-1", "u-1", "2026-09-01T10:00:00.000Z"),
            claude_line("sess-1", "u-2", "2026-09-01T18:00:00.000Z"),  # huge idle gap
        ],
    )
    cfg = make_config(tmp_path, claude_code=[cc / "*.jsonl"])
    conn = fresh_db(cfg)
    ingest.ingest(conn, cfg)

    assert conn.execute("SELECT count(*) FROM span").fetchone()[0] == 0
    assert conn.execute("SELECT active_ms FROM session").fetchone()[0] == 0
    assert conn.execute("SELECT active_ms FROM thread").fetchone()[0] == 0


# --------------------------------------------------------------------------
# aggregates and metadata
# --------------------------------------------------------------------------


def test_aggregates_are_min_max_and_count_of_events(tmp_path: Path):
    cc = tmp_path / "cc"
    write_jsonl(
        cc / "s.jsonl",
        [
            claude_line("sess-1", "u-2", "2026-09-01T10:05:00.000Z"),
            claude_line("sess-1", "u-1", "2026-09-01T10:00:00.000Z"),  # out of order
            claude_line("sess-1", "u-3", "2026-09-01T10:10:00.000Z"),
        ],
    )
    cfg = make_config(tmp_path, claude_code=[cc / "*.jsonl"])
    conn = fresh_db(cfg)
    ingest.ingest(conn, cfg)

    lo, hi, n = tuple(conn.execute("SELECT started_at, ended_at, event_count FROM session").fetchone())
    expected_lo, expected_hi, expected_n = tuple(conn.execute(
        "SELECT MIN(ts), MAX(ts), COUNT(*) FROM event"
    ).fetchone())
    assert (lo, hi, n) == (expected_lo, expected_hi, expected_n) == (expected_lo, expected_hi, 3)
    assert tuple(
        conn.execute("SELECT started_at, ended_at, event_count FROM thread").fetchone()
    ) == (lo, hi, n)


def test_latest_event_wins_for_cwd_branch_and_version(tmp_path: Path):
    """A worktree switch mid-session must land. The file holding the LATER
    events is deliberately the one that sorts FIRST, so this fails if the
    implementation takes 'last file processed' instead of 'highest ts'."""
    cc = tmp_path / "cc"
    write_jsonl(
        cc / "aaa_late.jsonl",
        [claude_line("s", "u-late", "2026-09-01T12:00:00.000Z",
                     cwd="/wt/new", branch="feature", version="2.1.0")],
    )
    write_jsonl(
        cc / "zzz_early.jsonl",
        [claude_line("s", "u-early", "2026-09-01T10:00:00.000Z",
                     cwd="/wt/old", branch="main", version="2.0.0")],
    )
    cfg = make_config(tmp_path, claude_code=[cc / "*.jsonl"])
    conn = fresh_db(cfg)
    ingest.ingest(conn, cfg)

    cwd, branch, version = tuple(conn.execute(
        "SELECT cwd, git_branch, cli_version FROM session"
    ).fetchone())
    assert (cwd, branch, version) == ("/wt/new", "feature", "2.1.0")
    assert [r[0] for r in conn.execute("SELECT root_path FROM project")] == ["/wt/new"]


def test_a_run_that_sees_no_metadata_keeps_what_is_stored(tmp_path: Path):
    cc = tmp_path / "cc"
    path = write_jsonl(cc / "s.jsonl", [claude_line("s", "u-1", "2026-09-01T10:00:00.000Z")])
    cfg = make_config(tmp_path, claude_code=[cc / "*.jsonl"])
    conn = fresh_db(cfg)
    ingest.ingest(conn, cfg)

    # Append an event carrying no cwd/branch/version at all.
    with path.open("a") as fh:
        fh.write(json.dumps({
            "sessionId": "s", "uuid": "u-2", "type": "user",
            "timestamp": "2026-09-01T10:01:00.000Z",
        }) + "\n")
    ingest.ingest(conn, cfg)

    assert tuple(
        conn.execute("SELECT cwd, git_branch, cli_version FROM session").fetchone()
    ) == ("/Users/demo/Coding/Demo", "main", "2.0.0")


def test_one_project_row_per_distinct_root(tmp_path: Path):
    cc = tmp_path / "cc"
    write_jsonl(cc / "a.jsonl", [claude_line("a", "ua", "2026-09-01T10:00:00.000Z", cwd="/p/one")])
    write_jsonl(cc / "b.jsonl", [claude_line("b", "ub", "2026-09-01T10:00:00.000Z", cwd="/p/one/")])
    write_jsonl(cc / "c.jsonl", [claude_line("c", "uc", "2026-09-01T10:00:00.000Z", cwd="/p/two")])
    cfg = make_config(tmp_path, claude_code=[cc / "*.jsonl"])
    conn = fresh_db(cfg)
    ingest.ingest(conn, cfg)

    assert sorted(r[0] for r in conn.execute("SELECT root_path FROM project")) == ["/p/one", "/p/two"]


def test_session_without_any_cwd_gets_a_null_project(tmp_path: Path):
    cc = tmp_path / "cc"
    write_jsonl(cc / "s.jsonl", [{
        "sessionId": "s", "uuid": "u-1", "type": "user",
        "timestamp": "2026-09-01T10:00:00.000Z",
    }])
    cfg = make_config(tmp_path, claude_code=[cc / "*.jsonl"])
    conn = fresh_db(cfg)
    ingest.ingest(conn, cfg)

    assert conn.execute("SELECT project_id FROM session").fetchone()[0] is None
    assert conn.execute("SELECT count(*) FROM project").fetchone()[0] == 0


# --------------------------------------------------------------------------
# threads and parent links
# --------------------------------------------------------------------------


def test_root_thread_has_no_parent_and_is_not_a_subagent(tmp_path: Path):
    cc = tmp_path / "cc"
    write_jsonl(cc / "s.jsonl", [claude_line("s", "u-1", "2026-09-01T10:00:00.000Z")])
    cfg = make_config(tmp_path, claude_code=[cc / "*.jsonl"])
    conn = fresh_db(cfg)
    ingest.ingest(conn, cfg)

    assert tuple(
        conn.execute("SELECT parent_thread_id, is_subagent FROM thread").fetchone()
    ) == (None, 0)


def test_claude_subagent_thread_links_to_its_root_thread(tmp_path: Path):
    """The subagent transcript is a separate file. Its thread must resolve to
    the ROW id of the root thread, not to a native id."""
    cc = tmp_path / "cc"
    # Name the subagent file so it sorts FIRST: the link must not depend on
    # the parent's file being processed earlier.
    write_jsonl(
        cc / "aaa_sub.jsonl",
        [
            claude_line("sess-1", "sa-1", "2026-09-01T10:01:00.000Z",
                        agent_id="agent-xyz", attribution="Explore"),
            claude_line("sess-1", "sa-2", "2026-09-01T10:02:00.000Z", agent_id="agent-xyz"),
        ],
    )
    write_jsonl(cc / "zzz_main.jsonl", [claude_line("sess-1", "m-1", "2026-09-01T10:00:00.000Z")])

    cfg = make_config(tmp_path, claude_code=[cc / "*.jsonl"])
    conn = fresh_db(cfg)
    ingest.ingest(conn, cfg)

    sid = ids.session_id(cfg.host_id, "claude_code", "sess-1")
    root_id = ids.thread_id(sid, "sess-1")
    sub = tuple(conn.execute(
        "SELECT id, parent_thread_id, is_subagent, agent_name, event_count "
        "FROM thread WHERE native_id = 'agent-xyz'"
    ).fetchone())
    assert sub[0] == ids.thread_id(sid, "agent-xyz")
    assert sub[1] == root_id
    assert sub[2] == 1
    assert sub[3] == "Explore"
    assert sub[4] == 2
    assert conn.execute("SELECT count(*) FROM thread WHERE session_id = ?", (sid,)).fetchone()[0] == 2


def test_codex_subagent_links_even_when_the_child_file_sorts_first(tmp_path: Path):
    cx = tmp_path / "cx"
    write_jsonl(
        cx / "aaa_child.jsonl",
        [
            codex_meta("thread-child", "sess-root", "2026-09-01T10:05:00.000Z",
                       parent="thread-root"),
            codex_line("2026-09-01T10:06:00.000Z", 1),
        ],
    )
    write_jsonl(
        cx / "zzz_root.jsonl",
        [
            codex_meta("thread-root", "sess-root", "2026-09-01T10:00:00.000Z"),
            codex_line("2026-09-01T10:01:00.000Z", 1),
        ],
    )
    cfg = make_config(tmp_path, codex=[cx / "*.jsonl"])
    conn = fresh_db(cfg)
    ingest.ingest(conn, cfg)

    sid = ids.session_id(cfg.host_id, "codex", "sess-root")
    assert conn.execute(
        "SELECT parent_thread_id FROM thread WHERE native_id = 'thread-child'"
    ).fetchone()[0] == ids.thread_id(sid, "thread-root")
    assert tuple(conn.execute(
        "SELECT parent_thread_id, is_subagent FROM thread WHERE native_id = 'thread-root'"
    ).fetchone()) == (None, 0)
    # The self-referencing FK really is enforced.
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_subagent_with_an_absent_parent_keeps_a_null_link(tmp_path: Path):
    """Better an honest NULL than an invented parent. Claude's rolling window
    can age out a main transcript while its subagent files survive."""
    cx = tmp_path / "cx"
    write_jsonl(
        cx / "orphan.jsonl",
        [
            codex_meta("thread-child", "sess-root", "2026-09-01T10:05:00.000Z",
                       parent="thread-that-is-gone"),
            codex_line("2026-09-01T10:06:00.000Z", 1),
        ],
    )
    cfg = make_config(tmp_path, codex=[cx / "*.jsonl"])
    conn = fresh_db(cfg)
    ingest.ingest(conn, cfg)

    assert tuple(
        conn.execute("SELECT parent_thread_id, is_subagent FROM thread").fetchone()
    ) == (None, 1)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


# --------------------------------------------------------------------------
# dedup
# --------------------------------------------------------------------------


def test_claude_resume_replay_is_collapsed_by_uuid(tmp_path: Path):
    """`resume_replay_b` replays `resume_replay_a`'s history verbatim."""
    cc = tmp_path / "cc"
    cc.mkdir()
    for name in ("resume_replay_a.jsonl", "resume_replay_b.jsonl"):
        shutil.copy(FIXTURES / "claude_code" / name, cc / name)
    cfg = make_config(tmp_path, claude_code=[cc / "*.jsonl"])
    conn = fresh_db(cfg)
    stats = ingest.ingest(conn, cfg)

    assert stats.events_read > stats.events_inserted   # replay really overlapped
    assert conn.execute("SELECT count(*) FROM event").fetchone()[0] == stats.events_inserted
    assert conn.execute(
        "SELECT count(*) FROM (SELECT session_id, native_event_id FROM event "
        "GROUP BY session_id, native_event_id HAVING count(*) > 1)"
    ).fetchone()[0] == 0


def test_codex_sibling_threads_reuse_ordinals_without_colliding(tmp_path: Path):
    """`ordinal` restarts at 0 in every thread; the key must be thread-scoped.
    Keying on (session, ordinal) drops 8,709 real events (FINDINGS §4)."""
    cx = tmp_path / "cx"
    write_jsonl(
        cx / "t1.jsonl",
        [codex_meta("thread-1", "sess", "2026-09-01T10:00:00.000Z"),
         codex_line("2026-09-01T10:01:00.000Z", 1),
         codex_line("2026-09-01T10:02:00.000Z", 2)],
    )
    write_jsonl(
        cx / "t2.jsonl",
        [codex_meta("thread-2", "sess", "2026-09-01T11:00:00.000Z", parent="thread-1"),
         codex_line("2026-09-01T11:01:00.000Z", 1),
         codex_line("2026-09-01T11:02:00.000Z", 2)],
    )
    cfg = make_config(tmp_path, codex=[cx / "*.jsonl"])
    conn = fresh_db(cfg)
    ingest.ingest(conn, cfg)

    assert conn.execute("SELECT count(*) FROM session").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM thread").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM event").fetchone()[0] == 6


# --------------------------------------------------------------------------
# idempotency
# --------------------------------------------------------------------------


def test_second_run_over_the_fixture_tree_inserts_nothing(tmp_path: Path):
    cc, cx = tmp_path / "cc", tmp_path / "cx"
    cc.mkdir()
    cx.mkdir()
    for f in (FIXTURES / "claude_code").glob("*.jsonl"):
        shutil.copy(f, cc / f.name)
    for f in (FIXTURES / "codex").glob("*.jsonl"):
        shutil.copy(f, cx / f.name)

    cfg = make_config(tmp_path, claude_code=[cc / "*.jsonl"], codex=[cx / "*.jsonl"])
    conn = fresh_db(cfg)
    first = ingest.ingest(conn, cfg)
    before = counts(conn)

    second = ingest.ingest(conn, cfg)
    assert second.events_inserted == 0
    assert second.projects_written == 0
    assert second.files_failed == 0
    assert counts(conn) == before
    assert first.events_inserted > 0


@_real_only
def test_idempotent_on_a_frozen_snapshot_of_the_real_corpus(tmp_path: Path):
    """Acceptance #2. The live corpus GROWS while it is measured (FINDINGS
    §0b), so idempotency is proven against a snapshot copied into tmp_path,
    which cannot move underneath the test."""
    cc, cx = tmp_path / "cc", tmp_path / "cx"
    cc.mkdir()
    cx.mkdir()

    def smallest(patterns: list[str], limit: int) -> list[Path]:
        import glob as _g
        hits = {p for pat in patterns for p in _g.glob(os.path.expanduser(pat), recursive=True)}
        files = [Path(p) for p in hits if os.path.isfile(p)]
        return sorted(files, key=lambda p: p.stat().st_size)[:limit]

    claude_files = smallest(DEFAULT_SOURCE_GLOBS["claude_code"], 40)
    codex_files = smallest(DEFAULT_SOURCE_GLOBS["codex"], 40)
    if not claude_files or not codex_files:
        pytest.skip("no real corpus on this machine")

    for i, src in enumerate(claude_files):
        shutil.copy(src, cc / f"{i:03d}_{src.name}")
    for i, src in enumerate(codex_files):
        shutil.copy(src, cx / f"{i:03d}_{src.name}")

    cfg = make_config(tmp_path, claude_code=[cc / "*.jsonl"], codex=[cx / "*.jsonl"])
    conn = fresh_db(cfg)
    first = ingest.ingest(conn, cfg)
    assert first.events_inserted > 0
    before = counts(conn)
    rows_before = conn.execute(
        "SELECT id, session_id, thread_id, native_event_id, ts, kind FROM event ORDER BY id"
    ).fetchall()

    second = ingest.ingest(conn, cfg)
    assert second.events_inserted == 0
    assert second.files_failed == 0
    assert counts(conn) == before
    assert conn.execute(
        "SELECT id, session_id, thread_id, native_event_id, ts, kind FROM event ORDER BY id"
    ).fetchall() == rows_before


# --------------------------------------------------------------------------
# incremental resume
# --------------------------------------------------------------------------


def test_appending_a_line_adds_exactly_that_event(tmp_path: Path):
    """Acceptance #3."""
    cc = tmp_path / "cc"
    path = write_jsonl(
        cc / "s.jsonl",
        [
            claude_line("sess-1", "u-1", "2026-09-01T10:00:00.000Z"),
            claude_line("sess-1", "u-2", "2026-09-01T10:01:00.000Z"),
        ],
    )
    cfg = make_config(tmp_path, claude_code=[cc / "*.jsonl"])
    conn = fresh_db(cfg)
    ingest.ingest(conn, cfg)

    read_1, size_1 = tuple(conn.execute(
        "SELECT bytes_read, size_bytes FROM ingest_file"
    ).fetchone())
    assert read_1 == size_1 == path.stat().st_size
    assert conn.execute("SELECT count(*) FROM event").fetchone()[0] == 2

    with path.open("a") as fh:
        fh.write(json.dumps(claude_line("sess-1", "u-3", "2026-09-01T10:02:00.000Z")) + "\n")

    stats = ingest.ingest(conn, cfg)
    assert stats.events_read == 1          # only the new line was parsed
    assert stats.events_inserted == 1
    assert conn.execute("SELECT count(*) FROM event").fetchone()[0] == 3
    assert [r[0] for r in conn.execute("SELECT native_event_id FROM event ORDER BY ts")] == [
        "u-1", "u-2", "u-3",
    ]

    read_2, size_2, lines_2 = tuple(conn.execute(
        "SELECT bytes_read, size_bytes, lines_read FROM ingest_file"
    ).fetchone())
    assert read_2 > read_1
    assert read_2 == size_2 == path.stat().st_size
    assert lines_2 == 3
    # The aggregates followed the new event.
    assert conn.execute("SELECT event_count FROM session").fetchone()[0] == 3
    assert conn.execute("SELECT ended_at FROM session").fetchone()[0] == conn.execute(
        "SELECT MAX(ts) FROM event"
    ).fetchone()[0]


def test_unchanged_files_are_skipped_without_being_parsed(tmp_path: Path):
    cc = tmp_path / "cc"
    write_jsonl(cc / "s.jsonl", [claude_line("s", "u-1", "2026-09-01T10:00:00.000Z")])
    cfg = make_config(tmp_path, claude_code=[cc / "*.jsonl"])
    conn = fresh_db(cfg)
    ingest.ingest(conn, cfg)

    stats = ingest.ingest(conn, cfg)
    assert stats.files_skipped == 1
    assert stats.files_ingested == 0
    assert stats.events_read == 0


def test_a_shrunken_file_is_treated_as_rotated_and_restarts_at_zero(tmp_path: Path):
    cc = tmp_path / "cc"
    path = write_jsonl(
        cc / "s.jsonl",
        [claude_line("s", f"u-{i}", f"2026-09-01T10:0{i}:00.000Z") for i in range(5)],
    )
    cfg = make_config(tmp_path, claude_code=[cc / "*.jsonl"])
    conn = fresh_db(cfg)
    ingest.ingest(conn, cfg)
    assert conn.execute("SELECT count(*) FROM event").fetchone()[0] == 5

    # Rotate: the path now holds a shorter, different file.
    write_jsonl(path, [claude_line("s", "u-new", "2026-09-01T11:00:00.000Z")])

    stats = ingest.ingest(conn, cfg)
    assert stats.events_read == 1                  # re-read from byte 0
    assert stats.events_inserted == 1
    assert conn.execute("SELECT count(*) FROM event").fetchone()[0] == 6
    assert conn.execute("SELECT bytes_read FROM ingest_file").fetchone()[0] == path.stat().st_size


def test_a_truncated_final_line_is_picked_up_once_it_completes(tmp_path: Path):
    cx = tmp_path / "cx"
    path = write_jsonl(
        cx / "t.jsonl",
        [codex_meta("thread-1", "sess", "2026-09-01T10:00:00.000Z"),
         codex_line("2026-09-01T10:01:00.000Z", 1)],
    )
    half = json.dumps(codex_line("2026-09-01T10:02:00.000Z", 2))
    with path.open("a") as fh:
        fh.write(half[: len(half) // 2])          # mid-write

    cfg = make_config(tmp_path, codex=[cx / "*.jsonl"])
    conn = fresh_db(cfg)
    ingest.ingest(conn, cfg)
    assert conn.execute("SELECT count(*) FROM event").fetchone()[0] == 2

    with path.open("a") as fh:                     # the writer finishes the line
        fh.write(half[len(half) // 2:] + "\n")
    stats = ingest.ingest(conn, cfg)
    assert stats.events_inserted == 1
    assert conn.execute("SELECT count(*) FROM event").fetchone()[0] == 3


# --------------------------------------------------------------------------
# failure isolation
# --------------------------------------------------------------------------


def test_one_exploding_file_does_not_abort_the_run(tmp_path: Path):
    good_a, bad, good_b = tmp_path / "a.log", tmp_path / "b.log", tmp_path / "c.log"
    for p in (good_a, bad, good_b):
        p.write_text("x")
    adapter = FakeAdapter(
        "fake",
        {
            good_a: [fake_event("s-a", "s-a", "e-a", 1_000, 1)],
            bad: [fake_event("s-bad", "s-bad", "e-bad", 2_000, 1)],
            good_b: [fake_event("s-b", "s-b", "e-b", 3_000, 1)],
        },
        boom=bad,
    )
    cfg = make_config(tmp_path)
    conn = fresh_db(cfg)
    stats = ingest.ingest(conn, cfg, adapters=[adapter])

    assert stats.files_failed == 1
    assert stats.files_ingested == 2
    assert len(stats.errors) == 1 and str(bad) in stats.errors[0][0]
    assert sorted(r[0] for r in conn.execute("SELECT native_id FROM session")) == ["s-a", "s-b"]


def test_a_failed_file_leaves_no_partial_rows(tmp_path: Path):
    """Its transaction rolls back, and the run must not keep believing the
    rows it wrote are there -- otherwise a later file reusing that session
    would trip the foreign key."""
    bad, good = tmp_path / "a.log", tmp_path / "b.log"
    for p in (bad, good):
        p.write_text("x")
    shared = "s-shared"
    adapter = FakeAdapter(
        "fake",
        {
            bad: [fake_event(shared, shared, "e-1", 1_000, 1)],
            good: [fake_event(shared, shared, "e-2", 2_000, 1)],
        },
        boom=bad,
    )
    cfg = make_config(tmp_path)
    conn = fresh_db(cfg)
    stats = ingest.ingest(conn, cfg, adapters=[adapter])

    assert stats.files_failed == 1
    assert conn.execute("SELECT count(*) FROM ingest_file").fetchone()[0] == 1   # only `good`
    assert [r[0] for r in conn.execute("SELECT native_event_id FROM event")] == ["e-2"]
    assert conn.execute("SELECT event_count FROM session").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_a_rolled_back_only_session_leaves_no_orphan_project(tmp_path: Path):
    bad = tmp_path / "a.log"
    bad.write_text("x")
    adapter = FakeAdapter(
        "fake",
        {bad: [fake_event("s-gone", "s-gone", "e-1", 1_000, 1)]},
        boom=bad,
    )
    cfg = make_config(tmp_path)
    conn = fresh_db(cfg)
    ingest.ingest(conn, cfg, adapters=[adapter])

    assert counts(conn)["session"] == 0
    assert counts(conn)["project"] == 0
    assert counts(conn)["event"] == 0


def test_a_broken_glob_for_one_source_does_not_stop_the_other(tmp_path: Path):
    class BrokenAdapter:
        name = "broken"

        def discover(self):
            raise OSError("permission denied")

        def parse(self, path, from_byte: int = 0):
            return iter(())

    good = tmp_path / "a.log"
    good.write_text("x")
    ok = FakeAdapter("fake", {good: [fake_event("s-a", "s-a", "e-a", 1_000, 1)]})

    cfg = make_config(tmp_path)
    conn = fresh_db(cfg)
    stats = ingest.ingest(conn, cfg, adapters=[BrokenAdapter(), ok])

    assert stats.files_failed == 1
    assert conn.execute("SELECT count(*) FROM event").fetchone()[0] == 1


# --------------------------------------------------------------------------
# adapter selection
# --------------------------------------------------------------------------


def test_build_adapters_defaults_to_every_known_source(tmp_path: Path):
    cfg = make_config(tmp_path)
    built = ingest.build_adapters(cfg)
    assert sorted(a.name for a in built) == ["claude_code", "codex", "opencode"]


def test_build_adapters_can_select_one_source(tmp_path: Path):
    cfg = make_config(tmp_path)
    assert [a.name for a in ingest.build_adapters(cfg, ["codex"])] == ["codex"]


def test_build_adapters_rejects_an_unknown_source(tmp_path: Path):
    with pytest.raises(ValueError, match="unknown source"):
        ingest.build_adapters(make_config(tmp_path), ["nope"])


def test_the_host_row_is_created_so_the_session_fk_holds(tmp_path: Path):
    cc = tmp_path / "cc"
    write_jsonl(cc / "s.jsonl", [claude_line("s", "u-1", "2026-09-01T10:00:00.000Z")])
    cfg = make_config(tmp_path, claude_code=[cc / "*.jsonl"])
    conn = fresh_db(cfg)
    ingest.ingest(conn, cfg)

    assert conn.execute("SELECT host_id FROM host").fetchone()[0] == cfg.host_id
    assert conn.execute("SELECT host_id FROM session").fetchone()[0] == cfg.host_id
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


# --------------------------------------------------------------------------
# real corpus reconciliation (acceptance #4)
# --------------------------------------------------------------------------


@_real_only
def test_full_corpus_reconciles_with_the_adapters_in_one_process(tmp_path: Path):
    """Acceptance #4.

    Set equality, never counts: the corpus grows between two runs minutes apart
    (FINDINGS §0b). The adapter side is re-derived from exactly the files and
    exactly the byte ranges `ingest_file` records having consumed, so the two
    sides describe the same instant even though the logs keep growing.
    """
    cfg = Config(
        host_id="reconcile-host",
        hostname="test-host",
        db_path=tmp_path / "real.db",
        source_globs=dict(DEFAULT_SOURCE_GLOBS),
        config_dir=tmp_path,
    )
    conn = fresh_db(cfg)
    stats = ingest.ingest(conn, cfg)
    assert stats.files_failed == 0, stats.errors
    assert stats.events_inserted > 0

    adapters = {"claude_code": ClaudeCodeAdapter(cfg), "codex": CodexAdapter(cfg)}
    from_adapters: dict[str, set[tuple[str, str]]] = {"claude_code": set(), "codex": set()}
    for path, source, bytes_read in conn.execute(
        "SELECT path, source, bytes_read FROM ingest_file"
    ):
        for ev in adapters[source].parse(Path(path), 0):
            if ev.byte_end <= bytes_read:
                from_adapters[source].add((ev.native_session_id, ev.native_thread_id))

    from_db: dict[str, set[tuple[str, str]]] = {"claude_code": set(), "codex": set()}
    for source, native_session, native_thread in conn.execute(
        "SELECT s.source, s.native_id, t.native_id FROM thread t "
        "JOIN session s ON s.id = t.session_id"
    ):
        from_db[source].add((native_session, native_thread))

    for source in ("claude_code", "codex"):
        assert from_db[source] == from_adapters[source], (
            f"{source}: {len(from_db[source] - from_adapters[source])} only in db, "
            f"{len(from_adapters[source] - from_db[source])} only from adapters"
        )
        assert from_db[source], f"{source} yielded nothing"

    # Nothing invented, nothing orphaned, and WP5's columns untouched.
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("SELECT count(*) FROM span").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM thread WHERE active_ms <> 0").fetchone()[0] == 0


# --------------------------------------------------- one response, billed once --
#
# Claude Code writes one line per content block of a response, and each line
# repeats the response's whole `usage`. Summing per line counted input and
# cache ~2x. These run the real adapter through the real ingest, because the
# thing that can go wrong is the interaction: a response cut in half by an
# ingest run that happens while it is still streaming.


def _response_line(uuid: str, ts: str, msg: str, req: str | None, out: int,
                   block: str = "text", **usage) -> dict:
    content = [{"type": "tool_use", "id": f"tu-{uuid}", "name": "Bash", "input": {}}] \
        if block == "tool_use" else [{"type": block, block: "x"}]
    rec = {
        "type": "assistant", "uuid": uuid, "timestamp": ts, "sessionId": "s1",
        "cwd": "/Users/demo/Coding/Demo", "version": "2.1.0",
        "message": {
            "id": msg, "role": "assistant", "model": "claude-opus-5-5", "content": content,
            "usage": {"input_tokens": usage.get("inp", 10), "output_tokens": out,
                      "cache_read_input_tokens": usage.get("cr", 1000),
                      "cache_creation_input_tokens": usage.get("cw", 200),
                      "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                         "ephemeral_1h_input_tokens": usage.get("cw", 200)}},
        },
    }
    if req is not None:
        rec["requestId"] = req
    return rec


def _tool_result(uuid: str, ts: str) -> dict:
    rec = claude_line("s1", uuid, ts)
    rec["message"] = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x"}]}
    return rec


def _totals(conn: sqlite3.Connection) -> tuple[int, ...]:
    return tuple(conn.execute(
        "SELECT coalesce(sum(input_tokens),0), coalesce(sum(output_tokens),0), "
        "coalesce(sum(cache_read_tokens),0), coalesce(sum(cache_write_tokens),0), "
        "coalesce(sum(cache_write_1h_tokens),0), count(*) FROM event").fetchone())


# Two responses. The first streams thinking, text, a tool call (with its
# result in between, as Claude Code writes it) and a second tool call; output
# grows as it streams. The second response has a single line.
_STREAMED = [
    _response_line("a1", "2026-10-01T10:00:00Z", "msg_1", "req_1", 5, "thinking"),
    _response_line("a2", "2026-10-01T10:00:01Z", "msg_1", "req_1", 40, "text"),
    _response_line("a3", "2026-10-01T10:00:02Z", "msg_1", "req_1", 90, "tool_use"),
    _tool_result("u1", "2026-10-01T10:00:03Z"),
    _response_line("a4", "2026-10-01T10:00:04Z", "msg_1", "req_1", 120, "tool_use"),
    _tool_result("u2", "2026-10-01T10:00:05Z"),
    _response_line("b1", "2026-10-01T10:00:06Z", "msg_2", "req_2", 7, "text",
                   inp=3, cr=2000, cw=0),
]
# What the API billed: each response once, at its final output count.
_BILLED = (10 + 3, 120 + 7, 1000 + 2000, 200 + 0, 200 + 0)


def test_a_streamed_response_is_billed_once(tmp_path: Path):
    log = write_jsonl(tmp_path / "claude" / "p" / "s1.jsonl", _STREAMED)
    cfg = make_config(tmp_path, claude_code=[str(tmp_path / "claude" / "*" / "*.jsonl")])
    conn = fresh_db(cfg)
    ingest.ingest(conn, cfg, sources=["claude_code"])

    *tokens, events = _totals(conn)
    assert tuple(tokens) == _BILLED
    assert events == len(_STREAMED), "every line is still an event on the timeline"
    assert log.exists()


@pytest.mark.parametrize("cut", range(1, len(_STREAMED)))
def test_a_response_cut_by_an_ingest_run_is_still_billed_once(tmp_path: Path, cut: int):
    """The file is ingested with only its first `cut` lines on disk, then
    again once the rest has been written -- for every possible cut, including
    in the middle of the streaming response. The totals must equal a single
    pass over the finished file."""
    log = tmp_path / "claude" / "p" / "s1.jsonl"
    cfg = make_config(tmp_path, claude_code=[str(tmp_path / "claude" / "*" / "*.jsonl")])
    conn = fresh_db(cfg)

    write_jsonl(log, _STREAMED[:cut])
    ingest.ingest(conn, cfg, sources=["claude_code"])
    with log.open("a") as handle:
        handle.write("".join(json.dumps(r) + "\n" for r in _STREAMED[cut:]))
    os.utime(log, (1, 1))   # a different mtime, as a real append would leave
    ingest.ingest(conn, cfg, sources=["claude_code"])

    *tokens, events = _totals(conn)
    assert tuple(tokens) == _BILLED
    assert events == len(_STREAMED)


def test_an_unchanged_file_with_a_held_back_response_is_skipped(tmp_path: Path):
    """Holding the last response back leaves the offset short of the end of
    the file. That must not turn every run into a re-read of every file."""
    write_jsonl(tmp_path / "claude" / "p" / "s1.jsonl", _STREAMED)
    cfg = make_config(tmp_path, claude_code=[str(tmp_path / "claude" / "*" / "*.jsonl")])
    conn = fresh_db(cfg)
    ingest.ingest(conn, cfg, sources=["claude_code"])
    again = ingest.ingest(conn, cfg, sources=["claude_code"])
    assert again.files_skipped == 1 and again.events_inserted == 0
