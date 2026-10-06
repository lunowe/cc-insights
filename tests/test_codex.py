"""Tests for the Codex source adapter.

The load-bearing one is `test_ordinal_alone_would_collide`: Codex's `ordinal`
is file-scoped and restarts at 0 in every thread, so keying events on
(session_id, ordinal) silently drops 8,709 real events on the measured corpus.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cc_insights import ids
from cc_insights.config import DEFAULT_SOURCE_GLOBS, Config
from cc_insights.sources import EventKind, SourceAdapter
from cc_insights.sources.codex import CodexAdapter

ROOT = "01a05385-43c3-77d1-bc25-81e11d29a05c"
THREAD_A = "01a05385-a3f8-7902-b37b-72113cdde335"
THREAD_B = "01a05385-b640-77b1-b7a7-45c22fd88f77"


# --- fixtures -----------------------------------------------------------


def meta_line(thread_id, session_id, ordinal=0, ts="2026-08-30T18:34:22.000Z", **extra):
    payload = {
        "session_id": session_id,
        "id": thread_id,
        "timestamp": ts,
        "cwd": "/Users/x/Coding/Proj",
        "originator": "Codex Desktop",
        "cli_version": "0.98.0",
        "source": "vscode",
        "git": {"commit_hash": "abc", "branch": "main", "repository_url": "u"},
    }
    payload.update(extra)
    return {"timestamp": ts, "ordinal": ordinal, "type": "session_meta", "payload": payload}


def subagent_meta_line(thread_id, session_id, parent, nickname="Confucius", ordinal=0):
    return meta_line(
        thread_id,
        session_id,
        ordinal=ordinal,
        source={
            "subagent": {
                "thread_spawn": {
                    "parent_thread_id": parent,
                    "depth": 1,
                    "agent_path": "/root/audit",
                    "agent_nickname": nickname,
                }
            }
        },
        parent_thread_id=parent,
        agent_nickname=nickname,
        forked_from_id=parent,
    )


def line(ordinal, ts, top_type, payload):
    return {"timestamp": ts, "ordinal": ordinal, "type": top_type, "payload": payload}


def write_jsonl(path: Path, records) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


@pytest.fixture
def simple_file(tmp_path) -> Path:
    return write_jsonl(
        tmp_path / "sessions" / "2026" / "08" / "30" / "rollout-a.jsonl",
        [
            meta_line(THREAD_A, ROOT, ordinal=0),
            line(1, "2026-08-30T18:34:23.100Z", "response_item",
                 {"type": "message", "role": "user", "content": []}),
            line(2, "2026-08-30T18:34:24.000Z", "response_item",
                 {"type": "reasoning", "summary": []}),
            line(3, "2026-08-30T18:34:25.000Z", "response_item",
                 {"type": "function_call", "name": "exec_command", "call_id": "call_1"}),
            line(4, "2026-08-30T18:34:26.000Z", "response_item",
                 {"type": "function_call_output", "call_id": "call_1"}),
        ],
    )


@pytest.fixture
def adapter() -> CodexAdapter:
    return CodexAdapter()


# --- contract -----------------------------------------------------------


def test_conforms_to_source_adapter_protocol(adapter):
    assert isinstance(adapter, SourceAdapter)
    assert adapter.name == "codex"


def test_default_globs_come_from_config(adapter):
    assert adapter._patterns() == DEFAULT_SOURCE_GLOBS["codex"]
    assert DEFAULT_SOURCE_GLOBS["codex"] == [
        "~/.codex/sessions/*/*/*/*.jsonl",
        "~/.codex/archived_sessions/**/*.jsonl",
    ]


def test_emits_no_content(adapter, simple_file):
    """Structural, but assert it at the adapter level too: nothing a RawEvent
    carries may come from message text, tool arguments or tool output."""
    import dataclasses

    from cc_insights.sources import RawEvent

    values = set()
    for event in adapter.parse(simple_file):
        for field in dataclasses.fields(RawEvent):
            values.add(repr(getattr(event, field.name)))
    assert not any("content" in v or "arguments" in v for v in values)


# --- discovery ----------------------------------------------------------


def test_discover_uses_both_globs(tmp_path, simple_file):
    archived = write_jsonl(
        tmp_path / "archived_sessions" / "2026" / "old.jsonl", [meta_line(THREAD_B, ROOT)]
    )
    adapter = CodexAdapter(
        globs=[
            str(tmp_path / "sessions" / "*" / "*" / "*" / "*.jsonl"),
            str(tmp_path / "archived_sessions" / "**" / "*.jsonl"),
        ]
    )
    assert list(adapter.discover()) == sorted([archived, simple_file])


def test_discover_accepts_globs_argument_and_dedupes(tmp_path, simple_file):
    pattern = str(tmp_path / "sessions" / "*" / "*" / "*" / "*.jsonl")
    found = list(CodexAdapter().discover(globs=[pattern, pattern]))
    assert found == [simple_file]


def test_discover_accepts_a_config(tmp_path, simple_file):
    cfg = Config(
        host_id="h",
        hostname="host",
        db_path=tmp_path / "db.sqlite",
        source_globs={"codex": [str(tmp_path / "sessions" / "*" / "*" / "*" / "*.jsonl")]},
        config_dir=tmp_path,
    )
    assert list(CodexAdapter(config=cfg).discover()) == [simple_file]


# --- session_meta is resolved once per file -----------------------------


def test_session_meta_resolved_once_per_file(adapter, simple_file):
    events = list(adapter.parse(simple_file))
    assert len(events) == 5
    assert {e.native_session_id for e in events} == {ROOT}
    assert {e.native_thread_id for e in events} == {THREAD_A}
    assert {e.cwd for e in events} == {"/Users/x/Coding/Proj"}
    assert {e.git_branch for e in events} == {"main"}
    assert {e.cli_version for e in events} == {"0.98.0"}
    assert {e.source for e in events} == {"codex"}


def test_only_the_first_session_meta_is_authoritative(adapter, tmp_path):
    """Three real files carry a SECOND session_meta whose `id` is the ROOT
    session id, not this thread's. Taking the last would re-key the thread."""
    path = write_jsonl(
        tmp_path / "second-meta.jsonl",
        [
            meta_line(THREAD_A, ROOT, ordinal=0),
            line(1, "2026-08-30T18:34:23.000Z", "response_item",
                 {"type": "message", "role": "user"}),
            meta_line(ROOT, ROOT, ordinal=2, ts="2026-08-30T18:34:24.000Z"),
            line(3, "2026-08-30T18:34:25.000Z", "response_item",
                 {"type": "message", "role": "assistant"}),
        ],
    )
    events = list(adapter.parse(path))
    assert [e.native_thread_id for e in events] == [THREAD_A] * 4
    assert [e.native_session_id for e in events] == [ROOT] * 4


def test_file_without_session_meta_falls_back_to_the_filename(adapter, tmp_path):
    path = write_jsonl(
        tmp_path / "orphan.jsonl",
        [line(0, "2026-08-30T18:34:23.000Z", "response_item", {"type": "message", "role": "user"})],
    )
    (event,) = list(adapter.parse(path))
    assert event.native_thread_id == "orphan"
    assert event.native_session_id == "orphan"
    assert event.native_event_id == "orphan:0"


# --- the ordinal trap ---------------------------------------------------


def test_native_event_id_is_thread_scoped(adapter, simple_file):
    events = list(adapter.parse(simple_file))
    assert [e.native_event_id for e in events] == [f"{THREAD_A}:{i}" for i in range(5)]
    assert [e.ordinal for e in events] == list(range(5))


def test_ordinal_alone_would_collide(adapter, tmp_path):
    """REGRESSION (FINDINGS section 2). Two threads of ONE session with
    overlapping ordinals and different timestamps. Keying on
    (session_id, ordinal) drops half the events; the contract's
    "<thread_id>:<ordinal>" keeps every one."""
    a = write_jsonl(
        tmp_path / "a.jsonl",
        [meta_line(THREAD_A, ROOT, ordinal=0)]
        + [
            line(i, f"2026-08-30T18:35:{20 + i:02d}.000Z", "response_item",
                 {"type": "message", "role": "assistant"})
            for i in range(1, 5)
        ],
    )
    b = write_jsonl(
        tmp_path / "b.jsonl",
        [meta_line(THREAD_B, ROOT, ordinal=0, ts="2026-08-30T18:40:00.000Z")]
        + [
            line(i, f"2026-08-30T18:40:{20 + i:02d}.000Z", "response_item",
                 {"type": "message", "role": "assistant"})
            for i in range(1, 5)
        ],
    )
    events = list(adapter.parse(a)) + list(adapter.parse(b))
    assert len(events) == 10

    # Same session, overlapping ordinals, genuinely disjoint work.
    assert {e.native_session_id for e in events} == {ROOT}
    assert {e.native_thread_id for e in events} == {THREAD_A, THREAD_B}
    assert sorted(e.ordinal for e in events) == sorted(list(range(5)) * 2)
    assert len({e.ts_ms for e in events}) == 10

    # The trap: ordinal alone halves the corpus.
    naive = {(e.native_session_id, e.ordinal) for e in events}
    assert len(naive) == 5

    # The contract's key loses nothing.
    deduped = {(e.native_session_id, e.native_event_id) for e in events}
    assert len(deduped) == 10
    assert (ROOT, f"{THREAD_A}:1") in deduped
    assert (ROOT, f"{THREAD_B}:1") in deduped


def test_missing_ordinal_falls_back_to_content_hash(adapter, tmp_path):
    record = {"timestamp": "2026-08-30T18:34:23.000Z", "type": "response_item",
              "payload": {"type": "message", "role": "user"}}
    path = write_jsonl(tmp_path / "no-ordinal.jsonl", [meta_line(THREAD_A, ROOT), record])
    events = list(adapter.parse(path))
    assert events[1].ordinal is None
    assert events[1].native_event_id == ids.content_fallback_id(json.dumps(record) + "\n")
    assert events[1].native_event_id != events[0].native_event_id


# --- threads and subagents ---------------------------------------------


def test_root_thread_has_no_parent_and_is_not_a_subagent(adapter, simple_file):
    for event in adapter.parse(simple_file):
        assert event.parent_native_thread_id is None
        assert event.is_subagent is False
        assert event.agent_name is None


def test_subagent_thread_is_marked(adapter, tmp_path):
    path = write_jsonl(
        tmp_path / "sub.jsonl",
        [
            subagent_meta_line(THREAD_B, ROOT, parent=THREAD_A, nickname="Descartes"),
            line(1, "2026-08-30T18:34:23.000Z", "response_item",
                 {"type": "message", "role": "assistant"}),
        ],
    )
    for event in adapter.parse(path):
        assert event.is_subagent is True
        assert event.parent_native_thread_id == THREAD_A
        assert event.agent_name == "Descartes"
        assert event.native_session_id == ROOT
        assert event.native_thread_id == THREAD_B


def test_string_source_is_never_read_as_a_subagent_mapping(adapter, tmp_path):
    """`source` is a plain string on root threads. A substring test against it
    would be a false positive waiting to happen."""
    path = write_jsonl(
        tmp_path / "str-source.jsonl", [meta_line(THREAD_A, ROOT, source="subagent-ish-cli")]
    )
    (event,) = list(adapter.parse(path))
    assert event.is_subagent is False


# --- kind mapping -------------------------------------------------------


@pytest.mark.parametrize(
    "top_type,payload,expected",
    [
        ("response_item", {"type": "message", "role": "user"}, EventKind.USER_PROMPT),
        ("response_item", {"type": "message", "role": "assistant"}, EventKind.ASSISTANT),
        ("response_item", {"type": "message", "role": "developer"}, EventKind.SYSTEM),
        ("response_item", {"type": "reasoning"}, EventKind.ASSISTANT),
        ("response_item", {"type": "agent_message", "author": "/root/a"}, EventKind.ASSISTANT),
        ("response_item", {"type": "function_call", "name": "exec_command",
                           "call_id": "c1"}, EventKind.TOOL_USE),
        ("response_item", {"type": "custom_tool_call", "name": "apply_patch",
                           "call_id": "c2"}, EventKind.TOOL_USE),
        ("response_item", {"type": "tool_search_call", "call_id": "c3"}, EventKind.TOOL_USE),
        ("response_item", {"type": "web_search_call", "status": "completed"},
         EventKind.TOOL_USE),
        ("response_item", {"type": "function_call_output", "call_id": "c1"},
         EventKind.TOOL_RESULT),
        ("response_item", {"type": "custom_tool_call_output", "call_id": "c2"},
         EventKind.TOOL_RESULT),
        ("response_item", {"type": "tool_search_output", "call_id": "c3"},
         EventKind.TOOL_RESULT),
        ("event_msg", {"type": "token_count", "info": None}, EventKind.SYSTEM),
        ("event_msg", {"type": "item_completed", "item": {"type": "UserMessage"}},
         EventKind.SYSTEM),
        ("event_msg", {"type": "task_started"}, EventKind.SYSTEM),
        ("event_msg", {"type": "task_complete"}, EventKind.SYSTEM),
        ("event_msg", {"type": "turn_aborted"}, EventKind.SYSTEM),
        ("event_msg", {"type": "thread_goal_updated"}, EventKind.SYSTEM),
        ("turn_context", {"model": "gpt-5.3-codex"}, EventKind.SYSTEM),
        ("world_state", {"full": True}, EventKind.SYSTEM),
        ("compacted", {"window_number": 1}, EventKind.SYSTEM),
        ("token_usage_record", {"usage": {}}, EventKind.SYSTEM),
        ("inter_agent_communication_metadata", {"trigger_turn": False}, EventKind.SYSTEM),
        ("response_item", {"type": "something_new_from_the_future"}, EventKind.OTHER),
        ("a_top_level_type_from_the_future", {}, EventKind.OTHER),
    ],
)
def test_kind_mapping(adapter, tmp_path, top_type, payload, expected):
    path = write_jsonl(
        tmp_path / "kinds.jsonl",
        [meta_line(THREAD_A, ROOT), line(1, "2026-08-30T18:34:23.000Z", top_type, payload)],
    )
    events = list(adapter.parse(path))
    assert events[0].kind is EventKind.SYSTEM  # session_meta itself
    assert events[1].kind is expected


def test_tool_name_and_call_id(adapter, tmp_path):
    path = write_jsonl(
        tmp_path / "tools.jsonl",
        [
            meta_line(THREAD_A, ROOT),
            line(1, "2026-08-30T18:34:23.000Z", "response_item",
                 {"type": "function_call", "name": "exec_command", "call_id": "call_1"}),
            line(2, "2026-08-30T18:34:24.000Z", "response_item",
                 {"type": "function_call_output", "call_id": "call_1"}),
            line(3, "2026-08-30T18:34:25.000Z", "response_item",
                 {"type": "custom_tool_call", "name": "apply_patch", "call_id": "call_2"}),
            line(4, "2026-08-30T18:34:26.000Z", "response_item",
                 {"type": "web_search_call", "status": "completed"}),
        ],
    )
    events = list(adapter.parse(path))
    assert (events[1].tool_name, events[1].tool_use_id) == ("exec_command", "call_1")
    assert (events[2].tool_name, events[2].tool_use_id) == (None, "call_1")
    assert (events[3].tool_name, events[3].tool_use_id) == ("apply_patch", "call_2")
    assert (events[4].tool_name, events[4].tool_use_id) == ("web_search", None)


def test_model_is_taken_from_turn_context_and_thread_settings(adapter, tmp_path):
    path = write_jsonl(
        tmp_path / "models.jsonl",
        [
            meta_line(THREAD_A, ROOT),
            line(1, "2026-08-30T18:34:23.000Z", "turn_context",
                 {"model": "gpt-5.3-codex", "cwd": "/Users/x/Coding/Worktree"}),
            line(2, "2026-08-30T18:34:24.000Z", "event_msg",
                 {"type": "thread_settings_applied",
                  "thread_settings": {"model": "gpt-5.6-sol"}}),
            line(3, "2026-08-30T18:34:25.000Z", "response_item",
                 {"type": "message", "role": "assistant"}),
        ],
    )
    events = list(adapter.parse(path))
    assert [e.model for e in events] == [None, "gpt-5.3-codex", "gpt-5.6-sol", None]
    # turn_context also records worktree switches.
    assert events[1].cwd == "/Users/x/Coding/Worktree"
    assert events[3].cwd == "/Users/x/Coding/Proj"


# --- token usage --------------------------------------------------------
#
# Codex records the same request in several overlapping places. Each rule
# below follows ccusage (rust/adapters/codex/src/parser.rs, replay.rs) and
# fixed a measured overcount on the real corpus; see the module docstring.

T_BASE = datetime(2026, 8, 30, 18, 0, 0, tzinfo=timezone.utc)


def at(seconds: float) -> str:
    """An ISO timestamp `seconds` after T_BASE, millisecond precision."""
    moment = T_BASE + timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def usage(inp, out=0, cached=0, write=0, reasoning=0, total=None):
    return {"input_tokens": inp, "cached_input_tokens": cached,
            "cache_write_input_tokens": write, "output_tokens": out,
            "reasoning_output_tokens": reasoning,
            "total_tokens": inp + out if total is None else total}


def token_count(ordinal, seconds, last, total):
    info = {"model_context_window": 258400}
    if last is not None:
        info["last_token_usage"] = last
    if total is not None:
        info["total_token_usage"] = total
    return line(ordinal, at(seconds), "event_msg", {"type": "token_count", "info": info})


def usage_record(ordinal, seconds, response_id, used, thread=None):
    payload = {"response_id": response_id, "usage": used,
               "turn_token_usage": used, "thread_token_usage": thread or used}
    return line(ordinal, at(seconds), "token_usage_record", payload)


def compacted(ordinal, seconds, response_id):
    return line(ordinal, at(seconds), "compacted",
                {"message": "", "compaction_response_id": response_id})


def counted(events):
    """{ordinal: (fresh input, cache read, cache write, output)} for every
    event that carries tokens."""
    return {
        e.ordinal: (e.input_tokens, e.cache_read_tokens, e.cache_write_tokens,
                    e.output_tokens)
        for e in events
        if e.input_tokens is not None
    }


def test_token_count_usage_is_split_into_fresh_and_cached(adapter, tmp_path):
    u = usage(10825, 423, cached=7424, write=12, reasoning=259)
    path = write_jsonl(
        tmp_path / "tokens.jsonl",
        [meta_line(THREAD_A, ROOT), token_count(1, 1, u, u)],
    )
    event = list(adapter.parse(path))[1]
    # Codex's input_tokens INCLUDES the cached part AND the cache write; the
    # contract wants fresh input, so both come out. Reasoning is already
    # inside output and is not added.
    assert event.input_tokens == 10825 - 7424 - 12
    assert event.cache_read_tokens == 7424
    assert event.cache_write_tokens == 12
    assert event.output_tokens == 423


def test_cache_parts_are_clamped_into_the_input(adapter, tmp_path):
    """A cache write larger than the uncached input cannot be billed twice."""
    u = usage(100, 5, cached=80, write=50)
    path = write_jsonl(tmp_path / "t.jsonl", [meta_line(THREAD_A, ROOT), token_count(1, 1, u, u)])
    assert counted(adapter.parse(path)) == {1: (0, 80, 20, 5)}


def test_usage_field_aliases_are_accepted(adapter, tmp_path):
    aliased = {"prompt_tokens": 1000, "completion_tokens": 40, "cached_tokens": 600,
               "cache_creation_input_tokens": 100, "reasoning_tokens": 7}
    path = write_jsonl(
        tmp_path / "t.jsonl", [meta_line(THREAD_A, ROOT), token_count(1, 1, aliased, aliased)]
    )
    assert counted(adapter.parse(path)) == {1: (300, 600, 100, 40)}


# D2: repeated token_count snapshots.


def test_a_repeated_total_counts_nothing(adapter, tmp_path):
    """Codex re-emits the same token_count snapshot; only an advancing
    cumulative total means a new request was billed."""
    a = usage(100, 10, cached=60)
    path = write_jsonl(
        tmp_path / "t.jsonl",
        [meta_line(THREAD_A, ROOT),
         token_count(1, 1, a, a),
         token_count(2, 2, a, a),
         token_count(3, 3, a, a)],
    )
    events = list(adapter.parse(path))
    assert counted(events) == {1: (40, 60, 0, 10)}
    # The repeats stay on the timeline under their own ids, just without tokens.
    assert [e.native_event_id for e in events[2:]] == [f"{THREAD_A}:2", f"{THREAD_A}:3"]


def test_an_advancing_total_counts_last_token_usage(adapter, tmp_path):
    a, b = usage(100, 10), usage(250, 30)
    path = write_jsonl(
        tmp_path / "t.jsonl",
        [meta_line(THREAD_A, ROOT),
         token_count(1, 1, a, a),
         token_count(2, 2, a, a),
         token_count(3, 3, b, usage(350, 40))],
    )
    assert counted(adapter.parse(path)) == {1: (100, 0, 0, 10), 3: (250, 0, 0, 30)}


def test_missing_last_usage_falls_back_to_the_total_delta(adapter, tmp_path):
    path = write_jsonl(
        tmp_path / "t.jsonl",
        [meta_line(THREAD_A, ROOT),
         token_count(1, 1, None, usage(100, 10, cached=50)),
         token_count(2, 2, None, usage(300, 25, cached=200)),
         # A field that went backwards saturates at zero instead of going negative.
         token_count(3, 3, None, usage(400, 20, cached=200))],
    )
    assert counted(adapter.parse(path)) == {
        1: (50, 50, 0, 10),
        2: (50, 150, 0, 15),
        3: (100, 0, 0, 0),
    }


def test_last_usage_without_a_total_still_counts(adapter, tmp_path):
    a = usage(100, 10)
    path = write_jsonl(
        tmp_path / "t.jsonl",
        [meta_line(THREAD_A, ROOT), token_count(1, 1, a, None), token_count(2, 2, a, None)],
    )
    # No total to compare against, so each one is taken as advancing.
    assert counted(adapter.parse(path)) == {1: (100, 0, 0, 10), 2: (100, 0, 0, 10)}


# D3: token_usage_record and compaction.


def test_usage_record_repeated_by_a_token_count_counts_once(adapter, tmp_path):
    """The record and the advancing token_count describe the same request."""
    a = usage(100, 10)
    path = write_jsonl(
        tmp_path / "t.jsonl",
        [meta_line(THREAD_A, ROOT),
         usage_record(1, 1, "resp_1", a),
         token_count(2, 1.1, a, a)],
    )
    assert counted(adapter.parse(path)) == {2: (100, 0, 0, 10)}


def test_an_unpaired_usage_record_counts_nothing(adapter, tmp_path):
    path = write_jsonl(
        tmp_path / "t.jsonl",
        [meta_line(THREAD_A, ROOT), usage_record(1, 1, "resp_1", usage(100, 7, cached=40))],
    )
    events = list(adapter.parse(path))
    assert counted(events) == {}
    assert events[1].kind is EventKind.SYSTEM


def test_a_remote_compaction_counts_once_on_the_compacted_line(adapter, tmp_path):
    """Remote compaction writes no advancing token_count, so the paired
    record is the only place its usage exists."""
    a, c = usage(1000, 10), usage(500, 77, cached=100)
    path = write_jsonl(
        tmp_path / "t.jsonl",
        [meta_line(THREAD_A, ROOT),
         token_count(1, 1, a, a),
         usage_record(2, 5, "resp_c", c, thread=usage(1500, 87)),
         compacted(3, 5.1, "resp_c"),
         token_count(4, 5.2, usage(0, 0, total=24910), a),  # not advancing
         compacted(5, 6, "resp_c")],  # a second marker does not count it again
    )
    assert counted(adapter.parse(path)) == {1: (1000, 0, 0, 10), 3: (400, 100, 0, 77)}


def test_a_compaction_marker_before_its_record_counts_on_the_record(adapter, tmp_path):
    c = usage(500, 77)
    path = write_jsonl(
        tmp_path / "t.jsonl",
        [meta_line(THREAD_A, ROOT), compacted(1, 1, "resp_c"), usage_record(2, 2, "resp_c", c)],
    )
    assert counted(adapter.parse(path)) == {2: (500, 0, 0, 77)}


def test_a_local_compaction_is_covered_by_its_token_count(adapter, tmp_path):
    """Local compaction: record, advancing token_count, then the marker. The
    token_count is the count; the marker must not add the record again. The
    match on the cumulative thread usage covers a token_count whose
    last_token_usage differs from the record's."""
    a = usage(1000, 10)
    thread = usage(1600, 30)
    path = write_jsonl(
        tmp_path / "t.jsonl",
        [meta_line(THREAD_A, ROOT),
         token_count(1, 1, a, a),
         usage_record(2, 5, "resp_c", usage(600, 20), thread=thread),
         token_count(3, 5.1, usage(601, 20), thread),
         compacted(4, 5.2, "resp_c")],
    )
    assert counted(adapter.parse(path)) == {1: (1000, 0, 0, 10), 3: (601, 0, 0, 20)}


# D4: forked and spawned threads replay their parent's usage.

PARENT = "01a05385-1111-7000-8000-000000000001"
CHILD = "01a05385-2222-7000-8000-000000000002"


def fork_meta(thread_id, parent, seconds, *, spawned=False):
    extra = (
        {"source": {"subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1}}},
         "parent_thread_id": parent}
        if spawned else {"forked_from_id": parent}
    )
    return meta_line(thread_id, ROOT, ts=at(seconds), **extra)


@pytest.fixture
def codex_home(tmp_path):
    """A ~/.codex lookalike and an adapter that discovers inside it."""
    home = tmp_path / "codex"
    adapter = CodexAdapter(globs=[str(home / "sessions" / "**" / "*.jsonl"),
                                  str(home / "archived_sessions" / "**" / "*.jsonl")])
    return home, adapter


A, B, C = usage(1000, 10, cached=500), usage(2000, 20, cached=1500), usage(3000, 30, cached=2500)
TA, TB, TC = usage(1000, 10, cached=500), usage(3000, 30, cached=2000), usage(6000, 60, cached=4500)


def write_parent(home, name=f"rollout-2026-08-30T18-00-00-{PARENT}.jsonl", folder="sessions"):
    return write_jsonl(
        home / folder / "2026" / "08" / "30" / name,
        [meta_line(PARENT, ROOT, ts=at(0)),
         token_count(1, 10, A, TA),
         token_count(2, 20, B, TB),
         token_count(3, 40, C, TC)],  # after the fork at 30 s: never replayed
    )


def write_child(home, *, spawned=False, own=None):
    own = own or usage(500, 5)
    return write_jsonl(
        home / "sessions" / "2026" / "08" / "30" / f"rollout-2026-08-30T18-00-30-{CHILD}.jsonl",
        [fork_meta(CHILD, PARENT, 30, spawned=spawned),
         # The replay: the parent's usage, rewritten to the fork instant.
         token_count(1, 30.01, A, TA),
         token_count(2, 30.02, B, TB),
         # The child's own first turn, after a real pause.
         token_count(3, 45, own, usage(3000 + own["input_tokens"], 30 + own["output_tokens"]))],
    )


@pytest.mark.parametrize("spawned", [False, True], ids=["forked_from_id", "thread_spawn"])
def test_a_forks_replayed_prefix_is_not_counted(codex_home, spawned):
    home, adapter = codex_home
    write_parent(home)
    child = write_child(home, spawned=spawned)
    events = list(adapter.parse(child))
    assert counted(events) == {3: (500, 0, 0, 5)}
    # The replayed events keep their ids and timeline slots.
    assert [e.ordinal for e in events] == [0, 1, 2, 3]


def test_parent_usage_after_the_fork_does_not_mask_the_child(codex_home):
    """The parent's third request happened after the fork; a child request
    with the same counts is the child's own and counts."""
    home, adapter = codex_home
    write_parent(home)
    child = write_child(home, own=C)
    assert counted(adapter.parse(child)) == {3: (500, 2500, 0, 30)}


def test_the_parent_is_found_in_archived_sessions_under_any_name(codex_home):
    home, adapter = codex_home
    write_parent(home, name="renamed.jsonl", folder="archived_sessions")
    child = write_child(home)
    assert counted(adapter.parse(child)) == {3: (500, 0, 0, 5)}


def test_a_missing_parent_skips_the_burst_at_the_head(codex_home):
    """Without the parent log, the replay is recognised as a burst of usage
    written under a second apart at the head of the file."""
    home, adapter = codex_home
    child = write_child(home)  # no parent written
    assert counted(adapter.parse(child)) == {3: (500, 0, 0, 5)}


def test_a_missing_parent_without_a_burst_counts_everything(codex_home):
    home, adapter = codex_home
    child = write_jsonl(
        home / "sessions" / "c.jsonl",
        [fork_meta(CHILD, PARENT, 30),
         token_count(1, 31, A, TA),
         token_count(2, 40, B, TB)],  # 9 s later: the child's own turns
    )
    assert counted(adapter.parse(child)) == {1: (500, 500, 0, 10), 2: (500, 1500, 0, 20)}


def test_a_root_thread_never_skips_its_head(codex_home):
    """A thread that is not a fork may well start with two fast requests."""
    home, adapter = codex_home
    root = write_jsonl(
        home / "sessions" / "r.jsonl",
        [meta_line(THREAD_A, ROOT, ts=at(0)),
         token_count(1, 1, A, TA),
         token_count(2, 1.2, B, TB)],
    )
    assert counted(adapter.parse(root)) == {1: (500, 500, 0, 10), 2: (500, 1500, 0, 20)}


def test_a_copied_compaction_is_not_counted_in_the_fork(codex_home):
    home, adapter = codex_home
    compaction, own_compaction = usage(700, 70), usage(800, 80)
    write_jsonl(
        home / "sessions" / f"rollout-{PARENT}.jsonl",
        [meta_line(PARENT, ROOT, ts=at(0)),
         token_count(1, 10, A, TA),
         usage_record(2, 15, "resp_parent", compaction),
         compacted(3, 15.1, "resp_parent")],
    )
    child = write_jsonl(
        home / "sessions" / f"rollout-{CHILD}.jsonl",
        [fork_meta(CHILD, PARENT, 30),
         token_count(1, 30.01, A, TA),
         usage_record(2, 30.02, "resp_parent", compaction),
         compacted(3, 30.03, "resp_parent"),
         usage_record(4, 50, "resp_child", own_compaction),
         compacted(5, 50.1, "resp_child")],
    )
    assert counted(adapter.parse(child)) == {5: (800, 0, 0, 80)}


# --- usage state survives resuming --------------------------------------


@pytest.fixture
def stateful_file(codex_home):
    """A fork whose every usage rule depends on lines before any cut point:
    the replayed prefix, a repeated total, and a compaction whose record and
    marker are many lines apart."""
    home, adapter = codex_home
    write_parent(home)
    a = usage(500, 5)
    path = write_jsonl(
        home / "sessions" / "2026" / "08" / "30" / f"rollout-{CHILD}.jsonl",
        [fork_meta(CHILD, PARENT, 30),
         token_count(1, 30.01, A, TA),
         token_count(2, 30.02, B, TB),
         token_count(3, 45, a, usage(3500, 35)),
         token_count(4, 46, a, usage(3500, 35)),  # repeat
         usage_record(5, 50, "resp_c", usage(900, 9)),
         line(6, at(50.05), "response_item", {"type": "reasoning"}),
         compacted(7, 50.1, "resp_c"),
         token_count(8, 51, a, usage(3500, 35)),  # repeat
         token_count(9, 60, usage(100, 1), usage(3600, 36))],
    )
    return adapter, path


def test_resuming_at_every_line_matches_one_pass(stateful_file):
    adapter, path = stateful_file
    full = list(adapter.parse(path))
    assert counted(full) == {3: (500, 0, 0, 5), 7: (900, 0, 0, 9), 9: (100, 0, 0, 1)}
    for cut in range(len(full)):
        resumed = list(adapter.parse(path, from_byte=full[cut].byte_end))
        assert resumed == full[cut + 1:], f"cut after line {cut}"


def test_a_file_parsed_in_two_halves_matches_one_pass(stateful_file):
    """The live case: the first half is ingested, the rest is appended later.
    The cut falls between the compaction's record and its marker."""
    adapter, path = stateful_file
    data = path.read_bytes()
    full = list(adapter.parse(path))
    lines = data.splitlines(keepends=True)

    path.write_bytes(b"".join(lines[:6]))
    first = list(adapter.parse(path))
    path.write_bytes(data)
    second = list(adapter.parse(path, from_byte=first[-1].byte_end))

    assert [e.ordinal for e in first] == [0, 1, 2, 3, 4, 5]
    assert first + second == full


def test_events_without_usage_report_none(adapter, tmp_path):
    path = write_jsonl(
        tmp_path / "nousage.jsonl",
        [
            meta_line(THREAD_A, ROOT),
            line(1, "2026-08-30T18:34:23.000Z", "event_msg",
                 {"type": "token_count", "info": None}),
        ],
    )
    for event in adapter.parse(path):
        assert event.input_tokens is None
        assert event.output_tokens is None
        assert event.cache_read_tokens is None
        assert event.cache_write_tokens is None


# --- timestamps ---------------------------------------------------------


def test_timestamps_are_epoch_milliseconds_utc(adapter, simple_file):
    events = list(adapter.parse(simple_file))
    assert events[0].ts_ms == 1788114862000  # 2026-08-30T18:34:22.000Z
    assert events[1].ts_ms == 1788114863100  # sub-second precision preserved
    assert all(isinstance(e.ts_ms, int) for e in events)
    assert all(e.ts_ms > 1_000_000_000_000 for e in events)  # ms, not seconds


def test_lines_without_a_timestamp_are_skipped(adapter, tmp_path):
    path = write_jsonl(
        tmp_path / "nots.jsonl",
        [
            meta_line(THREAD_A, ROOT),
            {"ordinal": 1, "type": "response_item", "payload": {"type": "reasoning"}},
            line(2, "2026-08-30T18:34:24.000Z", "response_item", {"type": "reasoning"}),
        ],
    )
    events = list(adapter.parse(path))
    assert [e.ordinal for e in events] == [0, 2]


def test_unparseable_timestamp_is_skipped(adapter, tmp_path):
    path = write_jsonl(
        tmp_path / "badts.jsonl",
        [meta_line(THREAD_A, ROOT), line(1, "not a timestamp", "response_item", {})],
    )
    assert [e.ordinal for e in list(adapter.parse(path))] == [0]


# --- robustness ---------------------------------------------------------


def test_truncated_final_line_is_skipped_without_raising(adapter, tmp_path):
    path = write_jsonl(
        tmp_path / "trunc.jsonl",
        [
            meta_line(THREAD_A, ROOT),
            line(1, "2026-08-30T18:34:23.000Z", "response_item", {"type": "reasoning"}),
        ],
    )
    complete = path.read_bytes()
    path.write_bytes(complete + b'{"timestamp": "2026-08-30T18:34:24.000Z", "ordi')

    events = list(adapter.parse(path))
    assert [e.ordinal for e in events] == [0, 1]
    # The half-written bytes are NOT accounted for, so the next run re-reads
    # them once the writer finishes the line.
    assert events[-1].byte_end == len(complete)


def test_malformed_line_in_the_middle_is_skipped(adapter, tmp_path):
    path = write_jsonl(
        tmp_path / "mid.jsonl",
        [meta_line(THREAD_A, ROOT), line(2, "2026-08-30T18:34:24.000Z", "response_item", {})],
    )
    body = path.read_bytes().split(b"\n")
    path.write_bytes(body[0] + b"\n" + b"{not json at all}\n" + body[1] + b"\n")
    assert [e.ordinal for e in adapter.parse(path)] == [0, 2]


def test_empty_file_yields_nothing(adapter, tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_bytes(b"")
    assert list(adapter.parse(path)) == []


# --- resumability -------------------------------------------------------


def test_byte_end_marks_the_end_of_each_line(adapter, simple_file):
    raw = simple_file.read_bytes().split(b"\n")[:-1]
    expected, running = [], 0
    for chunk in raw:
        running += len(chunk) + 1
        expected.append(running)
    assert [e.byte_end for e in adapter.parse(simple_file)] == expected
    assert expected[-1] == simple_file.stat().st_size


@pytest.mark.parametrize("cut", [0, 1, 2, 3, 4])
def test_parse_resumes_from_byte_end(adapter, simple_file, cut):
    """Starting at the byte_end of event `cut` yields exactly the events after
    it -- no gap, no repeat -- and they are identical to the full-parse ones."""
    full = list(adapter.parse(simple_file))
    resumed = list(adapter.parse(simple_file, from_byte=full[cut].byte_end))
    assert resumed == full[cut + 1:]


def test_resume_still_reads_session_meta_from_the_top_of_the_file(adapter, simple_file):
    """`from_byte` skips past the session_meta line, but the ids still have to
    come from it -- it is the only line that carries them."""
    full = list(adapter.parse(simple_file))
    resumed = list(adapter.parse(simple_file, from_byte=full[0].byte_end))
    assert resumed
    assert {e.native_session_id for e in resumed} == {ROOT}
    assert {e.native_thread_id for e in resumed} == {THREAD_A}
    assert {e.cli_version for e in resumed} == {"0.98.0"}


def test_resume_at_eof_yields_nothing(adapter, simple_file):
    size = simple_file.stat().st_size
    assert list(adapter.parse(simple_file, from_byte=size)) == []
    assert list(adapter.parse(simple_file, from_byte=size + 10_000)) == []


def test_incremental_parse_reconstructs_the_whole_file(adapter, simple_file):
    full = list(adapter.parse(simple_file))
    collected, offset = [], 0
    while True:
        batch = list(adapter.parse(simple_file, from_byte=offset))
        if not batch:
            break
        collected.append(batch[0])
        offset = batch[0].byte_end
    assert collected == full


def test_resume_picks_up_appended_lines(adapter, tmp_path):
    """The live case: a file grows between runs."""
    path = write_jsonl(
        tmp_path / "growing.jsonl",
        [meta_line(THREAD_A, ROOT),
         line(1, "2026-08-30T18:34:23.000Z", "response_item", {"type": "reasoning"})],
    )
    first = list(adapter.parse(path))
    with open(path, "ab") as fh:
        fh.write(
            json.dumps(line(2, "2026-08-30T18:34:24.000Z", "response_item",
                            {"type": "reasoning"})).encode() + b"\n"
        )
    second = list(adapter.parse(path, from_byte=first[-1].byte_end))
    assert [e.ordinal for e in second] == [2]
    assert second[0].native_event_id == f"{THREAD_A}:2"
    assert second[0].native_thread_id == THREAD_A
