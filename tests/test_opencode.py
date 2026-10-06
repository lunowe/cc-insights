"""Tests for the opencode source adapter.

The store is a SQLite database rather than a log file, so the fixture is built
here in Python rather than committed as a binary: a reviewer can read what the
adapter is being fed, which is the whole point of a fixture.

Two tests carry the weight. `test_every_event_reports_byte_end_zero` pins the
resume contract this source deliberately opts out of (see the module docstring
in `sources/opencode.py`), and `test_no_message_content_reaches_a_raw_event`
pins the no-content rule against the one source whose rows are full of prompt
text, file contents and tool arguments.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from cc_insights.config import DEFAULT_SOURCE_GLOBS, Config
from cc_insights.sources import EventKind, SourceAdapter
from cc_insights.sources.opencode import OpencodeAdapter

ROOT = "ses_root00000000000000000000000"
CHILD = "ses_child0000000000000000000000"
T0 = 1_771_244_458_000


# --- fixture store ------------------------------------------------------

SCHEMA = """
CREATE TABLE session (
    id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, directory TEXT,
    version TEXT, agent TEXT, time_created INTEGER, time_updated INTEGER
);
CREATE TABLE message (
    id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
    time_updated INTEGER, data TEXT
);
CREATE TABLE part (
    id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
    time_created INTEGER, time_updated INTEGER, data TEXT
);
"""


class Store:
    """A minimal opencode store, written the way opencode writes one."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self._seq = 0

    def session(self, sid, *, parent=None, directory="/Users/x/Coding/Proj",
                version="1.18.31", agent=None, created=T0):
        self.conn.execute(
            "INSERT INTO session (id, project_id, parent_id, directory, version, "
            "agent, time_created, time_updated) VALUES (?,?,?,?,?,?,?,?)",
            (sid, "prj", parent, directory, version, agent, created, created),
        )
        return sid

    def message(self, sid, data, *, mid=None, created=None):
        self._seq += 1
        mid = mid or f"msg_{self._seq:04d}"
        created = created if created is not None else data.get("time", {}).get("created", T0)
        self.conn.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data) "
            "VALUES (?,?,?,?,?)",
            (mid, sid, created, created, json.dumps(data)),
        )
        return mid

    def part(self, sid, mid, data, *, pid=None, created=T0):
        self._seq += 1
        pid = pid or f"prt_{self._seq:04d}"
        self.conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
            "VALUES (?,?,?,?,?,?)",
            (pid, mid, sid, created, created, json.dumps(data)),
        )
        return pid

    def close(self):
        self.conn.commit()
        self.conn.close()
        return self.path


def assistant(created, completed=None, *, model="gpt-5.3-codex", tokens=None, cwd=None):
    data = {
        "role": "assistant",
        "time": {"created": created} | ({"completed": completed} if completed else {}),
        "modelID": model,
        "providerID": "openai",
        "cost": 0,
        "tokens": tokens or {"input": 100, "output": 20, "reasoning": 5,
                             "cache": {"read": 900, "write": 40}},
    }
    if cwd:
        data["path"] = {"cwd": cwd, "root": cwd}
    return data


def tool(name, start, end=None, *, status="completed", call="call_abc"):
    time = {"start": start} | ({"end": end} if end else {})
    return {"type": "tool", "callID": call, "tool": name,
            "state": {"status": status, "input": {"filePath": "/etc/passwd"}, "time": time}}


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """A root session with one turn, plus a subagent session with one turn."""
    s = Store(tmp_path / "opencode.db")
    s.session(ROOT)
    s.session(CHILD, parent=ROOT, agent="general")
    s.message(ROOT, {"role": "user", "time": {"created": T0}}, mid="msg_user")
    s.message(ROOT, assistant(T0 + 1_000, T0 + 9_000), mid="msg_asst")
    s.part(ROOT, "msg_asst", tool("read", T0 + 2_000, T0 + 2_500), pid="prt_tool")
    s.message(CHILD, assistant(T0 + 3_000, T0 + 4_000), mid="msg_sub")
    return s.close()


def parse(path: Path, from_byte: int = 0):
    return list(OpencodeAdapter(globs=[str(path)]).parse(path, from_byte))


# --- contract -----------------------------------------------------------


def test_adapter_satisfies_the_protocol():
    assert isinstance(OpencodeAdapter(), SourceAdapter)


def test_default_glob_matches_only_the_main_database(tmp_path: Path, store: Path):
    for sidecar in ("opencode.db-wal", "opencode.db-shm"):
        (tmp_path / sidecar).write_bytes(b"")
    found = list(OpencodeAdapter(globs=[str(tmp_path / "opencode.db*")]).discover())
    assert [p.name for p in found] == ["opencode.db", "opencode.db-shm", "opencode.db-wal"]
    # ...which is why the shipped default names the file exactly.
    assert DEFAULT_SOURCE_GLOBS["opencode"] == ["~/.local/share/opencode/opencode.db"]


def test_config_globs_win_over_the_default(tmp_path: Path, store: Path):
    cfg = Config(host_id="h", hostname="H", db_path=tmp_path / "x.db",
                 source_globs={"opencode": [str(store)]}, config_dir=tmp_path)
    assert list(OpencodeAdapter(cfg).discover()) == [store]


# --- identity -----------------------------------------------------------


def test_root_session_is_its_own_thread(store: Path):
    root = [e for e in parse(store) if e.native_thread_id == ROOT]
    assert root and all(e.native_session_id == ROOT for e in root)
    assert not any(e.is_subagent for e in root)
    assert all(e.parent_native_thread_id is None for e in root)


def test_a_child_session_is_a_subagent_thread_of_its_root(store: Path):
    sub = [e for e in parse(store) if e.native_thread_id == CHILD]
    assert sub, "the subagent session produced no events"
    assert all(e.native_session_id == ROOT for e in sub)
    assert all(e.is_subagent and e.parent_native_thread_id == ROOT for e in sub)
    assert all(e.agent_name == "general" for e in sub)


def test_a_child_whose_parent_is_missing_becomes_its_own_root(tmp_path: Path):
    s = Store(tmp_path / "opencode.db")
    s.session(CHILD, parent="ses_deleted", agent="explore")
    s.message(CHILD, assistant(T0))
    events = parse(s.close())
    assert {e.native_session_id for e in events} == {CHILD}
    # Still marked a subagent -- the log says it was one; only the missing
    # parent is dropped, and ingest leaves parent_thread_id NULL for it.
    assert all(e.is_subagent for e in events)


def test_a_parent_cycle_does_not_hang(tmp_path: Path):
    s = Store(tmp_path / "opencode.db")
    s.session("a", parent="b")
    s.session("b", parent="a")
    s.message("a", assistant(T0))
    assert len(parse(s.close())) == 1  # terminates; the root it settles on is arbitrary


def test_session_metadata_lands_on_every_event(store: Path):
    events = parse(store)
    assert {e.cli_version for e in events} == {"1.18.31"}
    assert {e.cwd for e in events} == {"/Users/x/Coding/Proj"}


def test_a_message_cwd_overrides_the_session_directory(tmp_path: Path):
    """A worktree switch shows up on the message, not on the session row."""
    s = Store(tmp_path / "opencode.db")
    s.session(ROOT, directory="/Users/x/Coding/Proj")
    s.message(ROOT, assistant(T0, cwd="/Users/x/Coding/Proj/.worktrees/wt"))
    assert {e.cwd for e in parse(s.close())} == {"/Users/x/Coding/Proj/.worktrees/wt"}


# --- events -------------------------------------------------------------


def test_an_assistant_turn_yields_its_start_and_its_completion(store: Path):
    asst = [e for e in parse(store) if e.native_event_id.startswith("msg_asst")]
    assert [(e.native_event_id, e.ts_ms) for e in asst] == [
        ("msg_asst", T0 + 1_000),
        ("msg_asst:done", T0 + 9_000),
    ]
    assert all(e.kind is EventKind.ASSISTANT for e in asst)


def test_only_the_start_event_carries_token_usage(store: Path):
    """Both halves of a turn priced would double the bill."""
    start, done = [e for e in parse(store) if e.native_event_id.startswith("msg_asst")]
    # output 20 + reasoning 5: the fixture has no `total`, so reasoning is
    # taken as separate, as current opencode writes it.
    assert (start.input_tokens, start.output_tokens) == (100, 25)
    assert (start.cache_read_tokens, start.cache_write_tokens) == (900, 40)
    assert done.input_tokens is done.output_tokens is None
    assert done.cache_read_tokens is done.cache_write_tokens is None


def _output_for(tmp_path: Path, tokens: dict) -> int | None:
    s = Store(tmp_path / "opencode.db")
    s.session(ROOT)
    s.message(ROOT, assistant(T0, tokens=tokens), mid="msg_1")
    (event,) = parse(s.close())
    return event.output_tokens


def test_separate_reasoning_is_billed_as_output(tmp_path: Path):
    """opencode 1.18: `total` counts reasoning on top of output, so reasoning
    is separate and is billed at the output rate (as ccusage does)."""
    tokens = {"total": 12593, "input": 120, "output": 422, "reasoning": 546,
              "cache": {"read": 11505, "write": 0}}
    assert _output_for(tmp_path, tokens) == 422 + 546


def test_reasoning_inside_output_is_not_added_again(tmp_path: Path):
    """opencode 1.2: `total` leaves reasoning out because it is already inside
    output. Adding it would bill those tokens twice."""
    tokens = {"total": 12219, "input": 11799, "output": 420, "reasoning": 338,
              "cache": {"read": 0, "write": 0}}
    assert _output_for(tmp_path, tokens) == 420


def test_reasoning_without_a_total_is_taken_as_separate(tmp_path: Path):
    tokens = {"input": 10, "output": 3, "reasoning": 7, "cache": {"read": 0, "write": 0}}
    assert _output_for(tmp_path, tokens) == 10


def test_no_reasoning_leaves_output_alone(tmp_path: Path):
    tokens = {"total": 33, "input": 10, "output": 3, "reasoning": 0,
              "cache": {"read": 20, "write": 0}}
    assert _output_for(tmp_path, tokens) == 3


def test_a_completion_equal_to_the_start_emits_one_event(tmp_path: Path):
    s = Store(tmp_path / "opencode.db")
    s.session(ROOT)
    s.message(ROOT, assistant(T0, T0), mid="msg_1")
    assert [e.native_event_id for e in parse(s.close())] == ["msg_1"]


def test_a_tool_call_yields_a_use_and_a_result(store: Path):
    use, result = [e for e in parse(store) if e.native_event_id.startswith("prt_tool")]
    assert (use.kind, use.ts_ms, use.tool_name) == (EventKind.TOOL_USE, T0 + 2_000, "read")
    assert (result.kind, result.ts_ms) == (EventKind.TOOL_RESULT, T0 + 2_500)
    assert use.tool_use_id == result.tool_use_id == "call_abc"


def test_a_running_tool_call_yields_no_result(tmp_path: Path):
    """Its end time does not exist yet; inventing one extends the span to now."""
    s = Store(tmp_path / "opencode.db")
    s.session(ROOT)
    mid = s.message(ROOT, assistant(T0))
    s.part(ROOT, mid, tool("bash", T0 + 100, status="running"), pid="prt_run")
    kinds = [e.kind for e in parse(s.close()) if e.native_event_id.startswith("prt_run")]
    assert kinds == [EventKind.TOOL_USE]


def test_an_errored_tool_call_still_yields_its_result(tmp_path: Path):
    s = Store(tmp_path / "opencode.db")
    s.session(ROOT)
    mid = s.message(ROOT, assistant(T0))
    s.part(ROOT, mid, tool("bash", T0 + 100, T0 + 200, status="error"), pid="prt_err")
    kinds = [e.kind for e in parse(s.close()) if e.native_event_id.startswith("prt_err")]
    assert kinds == [EventKind.TOOL_USE, EventKind.TOOL_RESULT]


def test_non_tool_parts_are_dropped(tmp_path: Path):
    """Reasoning, text and step bookkeeping sit inside a bracketed window."""
    s = Store(tmp_path / "opencode.db")
    s.session(ROOT)
    mid = s.message(ROOT, assistant(T0, T0 + 500), mid="msg_1")
    s.part(ROOT, mid, {"type": "reasoning", "text": "secret", "time": {"start": T0}})
    s.part(ROOT, mid, {"type": "text", "text": "secret"})
    s.part(ROOT, mid, {"type": "step-finish", "tokens": {"input": 9}})
    assert [e.native_event_id for e in parse(s.close())] == ["msg_1", "msg_1:done"]


def test_a_user_turn_is_a_user_prompt(store: Path):
    user = [e for e in parse(store) if e.kind is EventKind.USER_PROMPT]
    assert [(e.native_event_id, e.ts_ms) for e in user] == [("msg_user", T0)]
    assert user[0].model is None


def test_a_message_without_a_timestamp_is_skipped(tmp_path: Path):
    s = Store(tmp_path / "opencode.db")
    s.session(ROOT)
    s.message(ROOT, {"role": "assistant", "modelID": "m"}, mid="msg_1", created=T0)
    s.message(ROOT, assistant(T0), mid="msg_2")
    assert {e.native_event_id for e in parse(s.close())} == {"msg_2"}


def test_native_event_ids_are_unique_within_a_session(store: Path):
    events = parse(store)
    keys = {(e.native_session_id, e.native_event_id) for e in events}
    assert len(keys) == len(events)


def test_the_model_is_stored_bare(store: Path):
    """`gpt-5.3-codex`, not `openai/gpt-5.3-codex`: one model, one row, whether
    it ran through opencode or the Codex CLI."""
    assert {e.model for e in parse(store) if e.model} == {"gpt-5.3-codex"}


# --- resume and robustness ----------------------------------------------


def test_every_event_reports_byte_end_zero(store: Path):
    """There is no honest byte offset into a B-tree, and a WAL database's size
    and mtime go stale. 0 keeps ingest re-reading the store every run."""
    assert {e.byte_end for e in parse(store)} == {0}


def test_from_byte_is_ignored_so_a_resumed_run_sees_everything(store: Path):
    assert parse(store, from_byte=10_000) == parse(store)


def test_a_database_that_is_not_an_opencode_store_raises(tmp_path: Path):
    """Raising lets ingest count one bad file and carry on; returning nothing
    would look like an empty store and hide the misconfiguration."""
    other = tmp_path / "opencode.db"
    sqlite3.connect(other).executescript("CREATE TABLE unrelated (x TEXT)")
    with pytest.raises(ValueError, match="not an opencode store"):
        parse(other)


def test_an_unreadable_row_is_skipped_not_fatal(tmp_path: Path):
    s = Store(tmp_path / "opencode.db")
    s.session(ROOT)
    s.message(ROOT, assistant(T0), mid="msg_ok")
    s.conn.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) "
        "VALUES ('msg_bad', ?, ?, ?, '{not json')", (ROOT, T0, T0),
    )
    assert {e.native_event_id for e in parse(s.close())} == {"msg_ok"}


def test_the_store_is_opened_read_only(store: Path):
    before = store.stat().st_mtime_ns
    parse(store)
    assert store.stat().st_mtime_ns == before


# --- the no-content rule -------------------------------------------------


def test_no_message_content_reaches_a_raw_event(tmp_path: Path):
    """opencode's rows carry prompt text, file contents and tool arguments.
    None of it may appear in any field of any RawEvent."""
    secrets = ["SECRET_PROMPT", "SECRET_FILE_BODY", "SECRET_TOOL_ARG", "SECRET_TITLE"]
    s = Store(tmp_path / "opencode.db")
    s.session(ROOT)
    mid = s.message(ROOT, {
        "role": "user",
        "time": {"created": T0},
        "summary": {"title": "SECRET_TITLE",
                    "diffs": [{"file": "a.py", "before": "SECRET_FILE_BODY"}]},
        "text": "SECRET_PROMPT",
    })
    s.message(ROOT, assistant(T0 + 1, T0 + 2))
    s.part(ROOT, mid, {"type": "tool", "callID": "c", "tool": "bash",
                       "state": {"status": "completed",
                                 "input": {"command": "SECRET_TOOL_ARG"},
                                 "output": "SECRET_FILE_BODY",
                                 "time": {"start": T0 + 3, "end": T0 + 4}}})
    s.part(ROOT, mid, {"type": "text", "text": "SECRET_PROMPT"})

    events = parse(s.close())
    assert events
    rendered = "\n".join(repr(e) for e in events)
    for secret in secrets:
        assert secret not in rendered, f"{secret} leaked into a RawEvent"
