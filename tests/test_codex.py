"""Tests for the Codex source adapter.

The load-bearing one is `test_ordinal_alone_would_collide`: Codex's `ordinal`
is file-scoped and restarts at 0 in every thread, so keying events on
(session_id, ordinal) silently drops 8,709 real events on the measured corpus.
"""

import json
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


def test_token_count_usage_is_split_into_fresh_and_cached(adapter, tmp_path):
    usage = {"input_tokens": 10825, "cached_input_tokens": 7424,
             "cache_write_input_tokens": 12, "output_tokens": 423,
             "reasoning_output_tokens": 259, "total_tokens": 11248}
    path = write_jsonl(
        tmp_path / "tokens.jsonl",
        [
            meta_line(THREAD_A, ROOT),
            line(1, "2026-08-30T18:34:23.000Z", "event_msg",
                 {"type": "token_count",
                  "info": {"last_token_usage": usage,
                           "total_token_usage": {"input_tokens": 99999,
                                                 "cached_input_tokens": 0,
                                                 "output_tokens": 99999},
                           "model_context_window": 258400}}),
        ],
    )
    event = list(adapter.parse(path))[1]
    # Codex's input_tokens INCLUDES the cached part; the contract wants them
    # separate, and the cumulative total_token_usage must not leak in.
    assert event.input_tokens == 10825 - 7424
    assert event.cache_read_tokens == 7424
    assert event.cache_write_tokens == 12
    assert event.output_tokens == 423


def test_token_usage_record_usage_is_mapped(adapter, tmp_path):
    path = write_jsonl(
        tmp_path / "tur.jsonl",
        [
            meta_line(THREAD_A, ROOT),
            line(1, "2026-08-30T18:34:23.000Z", "token_usage_record",
                 {"usage": {"input_tokens": 100, "cached_input_tokens": 40,
                            "cache_write_input_tokens": 0, "output_tokens": 7},
                  "turn_token_usage": {"input_tokens": 9999},
                  "thread_token_usage": {"input_tokens": 9999}}),
        ],
    )
    event = list(adapter.parse(path))[1]
    assert (event.input_tokens, event.cache_read_tokens, event.cache_write_tokens,
            event.output_tokens) == (60, 40, 0, 7)


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
