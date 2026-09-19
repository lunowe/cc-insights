"""Tests for the Claude Code adapter.

Everything here is hermetic: fixtures are hand-written JSONL in tmp_path that
reproduce the shapes measured in the real corpus (see docs/FINDINGS.md). The
corpus-wide acceptance numbers are verified separately against the live logs.
"""

import json
from pathlib import Path

import pytest

from cc_insights import ids
from cc_insights.config import DEFAULT_SOURCE_GLOBS, Config
from cc_insights.sources import EventKind, SourceAdapter
from cc_insights.sources.claude_code import ClaudeCodeAdapter

SID = "11111111-2222-3333-4444-555555555555"


AGENT = "aecee3047f87d3105"


def _line(**kw):
    base = {"timestamp": "2026-09-19T10:00:00.000Z", "sessionId": SID, "cwd": "/repo",
            "gitBranch": "main", "version": "2.0.1", "isSidechain": False}
    return {**base, **kw}


def _sub(doc, agent=AGENT):
    """Turn a main-transcript line into a subagent one, as the real logs do."""
    return {**doc, "isSidechain": True, "agentId": agent}


def _assistant(uuid, *, blocks=None, model="claude-opus-5", usage=None, **kw):
    message = {"role": "assistant", "model": model,
               "content": blocks if blocks is not None else [{"type": "text", "text": "hi"}]}
    if usage is not None:
        message["usage"] = usage
    return _line(type="assistant", uuid=uuid, message=message, **kw)


def _user(uuid, *, blocks=None, **kw):
    content = blocks if blocks is not None else [{"type": "text", "text": "do it"}]
    return _line(type="user", uuid=uuid, message={"role": "user", "content": content}, **kw)


def _write(path: Path, docs) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(d) + "\n" for d in docs), encoding="utf-8")
    return path


@pytest.fixture
def projects(tmp_path: Path) -> Path:
    return tmp_path / "projects"


def _adapter(projects: Path) -> ClaudeCodeAdapter:
    """Both real shapes: the main transcripts and the nested subagent ones."""
    return ClaudeCodeAdapter(globs=[
        str(projects / "*" / "*.jsonl"),
        str(projects / "*" / "*" / "subagents" / "**" / "*.jsonl"),
    ])


def _subagent_file(projects: Path, docs, *, agent=AGENT, workflow: str | None = None) -> Path:
    """Write `<project>/<session>/subagents/[workflows/<wf>/]agent-<id>.jsonl`."""
    directory = projects / "-repo" / SID / "subagents"
    if workflow:
        directory = directory / "workflows" / workflow
    return _write(directory / f"agent-{agent}.jsonl", docs)


# --- discovery ------------------------------------------------------------

def test_conforms_to_source_adapter_protocol():
    adapter = ClaudeCodeAdapter()
    assert isinstance(adapter, SourceAdapter)
    assert adapter.name == "claude_code"


def test_default_globs_come_from_config():
    assert ClaudeCodeAdapter().globs == DEFAULT_SOURCE_GLOBS["claude_code"]


def test_default_globs_cover_both_file_shapes():
    """Main transcripts AND the nested subagent ones -- the latter hold ~52%
    of all events, and the `**` is what reaches the workflow nesting."""
    globs = DEFAULT_SOURCE_GLOBS["claude_code"]
    assert "~/.claude/projects/*/*.jsonl" in globs
    assert any("subagents" in g and "**" in g for g in globs)


def test_config_globs_are_used(tmp_path: Path):
    cfg = Config(host_id="h", hostname="x", db_path=tmp_path / "db",
                 source_globs={"claude_code": [str(tmp_path / "p" / "*" / "*.jsonl")]})
    assert ClaudeCodeAdapter(cfg).globs == [str(tmp_path / "p" / "*" / "*.jsonl")]


def test_discover_finds_transcripts_and_ignores_others(projects: Path):
    a = _write(projects / "-repo-a" / "s1.jsonl", [_assistant("u1")])
    b = _write(projects / "-repo-b" / "s2.jsonl", [_assistant("u2")])
    (projects / "-repo-a" / "notes.txt").write_text("nope")
    (projects / "-repo-a" / "deep").mkdir()
    (projects / "-repo-a" / "deep" / "s3.jsonl").write_text("{}\n")  # not a transcript dir
    assert list(_adapter(projects).discover()) == sorted([a, b])


def test_discover_finds_both_subagent_shapes(projects: Path):
    """`subagents/agent-<id>.jsonl` and `subagents/workflows/<wf>/agent-<id>.jsonl`."""
    main = _write(projects / "-repo" / f"{SID}.jsonl", [_assistant("u1")])
    flat = _subagent_file(projects, [_sub(_assistant("u2"))])
    nested = _subagent_file(projects, [_sub(_assistant("u3"), agent="b1")],
                            agent="b1", workflow="wf_db5151b5-e69")
    journal = _write(projects / "-repo" / SID / "subagents" / "workflows"
                     / "wf_db5151b5-e69" / "journal.jsonl", [{"type": "started", "agentId": "b1"}])
    assert list(_adapter(projects).discover()) == sorted([main, flat, nested, journal])


def test_discover_deduplicates_overlapping_globs(projects: Path):
    _write(projects / "-repo-a" / "s1.jsonl", [_assistant("u1")])
    pattern = str(projects / "*" / "*.jsonl")
    adapter = ClaudeCodeAdapter(globs=[pattern, pattern])
    assert len(list(adapter.discover())) == 1


# --- identity: session, thread, event -------------------------------------

def test_sessionizes_on_field_not_filename(projects: Path):
    """A resumed session writes into a new file; both files are one session."""
    f1 = _write(projects / "-repo" / "file-one.jsonl", [_assistant("u1")])
    f2 = _write(projects / "-repo" / "file-two.jsonl", [_assistant("u2")])
    adapter = _adapter(projects)
    sessions = {e.native_session_id for f in (f1, f2) for e in adapter.parse(f)}
    assert sessions == {SID}


def test_session_id_falls_back_to_the_file_stem_when_field_absent(projects: Path):
    """file-history-delta events carry no sessionId. The stem IS a session id,
    so falling back to it keeps them in their real session; the basename (with
    `.jsonl`) minted 3 phantom sessions -- 121 instead of the true 118."""
    doc = _line(type="file-history-delta")
    doc.pop("sessionId")
    f = _write(projects / "-repo" / "abc.jsonl", [doc, _assistant("u1", sessionId="abc")])
    events = list(_adapter(projects).parse(f))
    assert [e.native_session_id for e in events] == ["abc", "abc"]


def test_native_event_id_is_the_uuid(projects: Path):
    f = _write(projects / "-repo" / "s.jsonl", [_assistant("uuid-a")])
    (event,) = list(_adapter(projects).parse(f))
    assert event.native_event_id == "uuid-a"


def test_resume_replay_dedups_on_uuid(projects: Path):
    """Identical events replayed into the resumed file collapse to one key."""
    first = _assistant("u1")
    f1 = _write(projects / "-repo" / "a.jsonl", [first, _assistant("u2")])
    f2 = _write(projects / "-repo" / "b.jsonl", [first, _assistant("u3")])
    adapter = _adapter(projects)
    keys = {(e.native_session_id, e.native_event_id)
            for f in (f1, f2) for e in adapter.parse(f)}
    assert len(keys) == 3


def test_uuidless_events_fall_back_to_content_hash(projects: Path):
    docs = [_line(type="queue-operation", operation="add"),
            _line(type="pr-link", prNumber=7),
            _line(type="file-history-delta")]
    f = _write(projects / "-repo" / "s.jsonl", docs)
    events = list(_adapter(projects).parse(f))
    assert [e.native_event_id for e in events] == [
        ids.content_fallback_id(json.dumps(d)) for d in docs
    ]
    assert len({e.native_event_id for e in events}) == 3


def test_identical_uuidless_lines_in_two_files_share_a_key(projects: Path):
    doc = _line(type="queue-operation", operation="add")
    f1 = _write(projects / "-repo" / "a.jsonl", [doc])
    f2 = _write(projects / "-repo" / "b.jsonl", [doc])
    adapter = _adapter(projects)
    (one,) = list(adapter.parse(f1))
    (two,) = list(adapter.parse(f2))
    assert one.native_event_id == two.native_event_id


def test_main_transcript_events_are_all_on_the_root_thread(projects: Path):
    """The main file holds only the root thread -- no `agentId` ever appears in
    it, not even on the `Agent` call that spawned a subagent."""
    f = _write(projects / "-repo" / "s.jsonl", [
        _assistant("u1", blocks=[{"type": "tool_use", "id": "t1", "name": "Agent"}]),
        _user("u2", blocks=[{"type": "tool_result", "tool_use_id": "t1"}]),
    ])
    for event in _adapter(projects).parse(f):
        assert event.native_thread_id == event.native_session_id
        assert event.parent_native_thread_id is None
        assert event.is_subagent is False
        assert event.agent_name is None


# --- subagent threads -----------------------------------------------------

def test_subagent_file_yields_a_subagent_thread(projects: Path):
    """`agentId` is the thread; `sessionId` still names the parent session."""
    f = _subagent_file(projects, [_sub(_user("u1")), _sub(_assistant("u2"))])
    events = list(_adapter(projects).parse(f))
    assert len(events) == 2
    for event in events:
        assert event.is_subagent is True
        assert event.native_session_id == SID
        assert event.native_thread_id == AGENT
        assert event.native_thread_id != event.native_session_id
        # the root thread's id IS the session id, so that is the parent
        assert event.parent_native_thread_id == SID


def test_subagent_thread_is_parented_on_the_root_thread(projects: Path):
    """Parent and child land in one session: main file + subagent file."""
    main = _write(projects / "-repo" / f"{SID}.jsonl", [_assistant("u1")])
    sub = _subagent_file(projects, [_sub(_assistant("u2"))])
    adapter = _adapter(projects)
    events = [e for f in (main, sub) for e in adapter.parse(f)]
    assert {e.native_session_id for e in events} == {SID}
    by_thread = {e.native_thread_id: e for e in events}
    assert set(by_thread) == {SID, AGENT}
    assert by_thread[AGENT].parent_native_thread_id == by_thread[SID].native_thread_id


def test_two_subagents_of_one_session_are_two_threads(projects: Path):
    a = _subagent_file(projects, [_sub(_assistant("u1"), agent="a1")], agent="a1")
    b = _subagent_file(projects, [_sub(_assistant("u2"), agent="b2")], agent="b2",
                       workflow="wf_db5151b5-e69")
    adapter = _adapter(projects)
    events = [e for f in (a, b) for e in adapter.parse(f)]
    assert {(e.native_session_id, e.native_thread_id) for e in events} == {
        (SID, "a1"), (SID, "b2"),
    }


def test_agent_name_comes_from_the_transcript_head(projects: Path):
    """`attributionAgent` is on assistant lines only, so the opening user line
    gets its name from the head of the file rather than from itself."""
    f = _subagent_file(projects, [
        _sub(_user("u1")),
        _sub(_assistant("u2", attributionAgent="Explore")),
        _sub(_assistant("u3", attributionAgent="Explore")),
    ])
    assert [e.agent_name for e in _adapter(projects).parse(f)] == ["Explore"] * 3


def test_agent_name_is_none_when_the_transcript_records_none(projects: Path):
    """8 short transcripts in the corpus never name their agent."""
    f = _subagent_file(projects, [_sub(_user("u1")), _sub(_assistant("u2"))])
    assert {e.agent_name for e in _adapter(projects).parse(f)} == {None}


def test_agent_name_survives_a_resumed_parse(projects: Path):
    """The head sniff always reads from byte 0, so resuming past the assistant
    line that carries the name still names the agent."""
    f = _subagent_file(projects, [
        _sub(_user("u1")),
        _sub(_assistant("u2", attributionAgent="general-purpose")),
        _sub(_assistant("u3")),
    ])
    adapter = _adapter(projects)
    full = list(adapter.parse(f))
    tail = list(adapter.parse(f, full[-2].byte_end))
    assert [e.native_event_id for e in tail] == ["u3"]
    assert tail[0].agent_name == "general-purpose"


def test_subagent_events_dedup_by_uuid_within_their_thread(projects: Path):
    """Subagent uuids never collide with the main transcript's, but the key is
    still (session, thread, uuid)."""
    f = _subagent_file(projects, [_sub(_assistant("u1")), _sub(_assistant("u1"))])
    events = list(_adapter(projects).parse(f))
    keys = {(e.native_session_id, e.native_thread_id, e.native_event_id) for e in events}
    assert len(events) == 2 and len(keys) == 1


def test_journal_lines_are_dropped(projects: Path):
    """`subagents/workflows/<wf>/journal.jsonl` is bookkeeping: agentId plus a
    key, no timestamp/uuid/sessionId. No timestamp -> no event, which is also
    what the canonical probe does with it."""
    f = _write(projects / "-repo" / SID / "subagents" / "workflows" / "wf_1" / "journal.jsonl", [
        {"type": "started", "key": "v2:abc", "agentId": "a1"},
        {"type": "result", "key": "v2:abc", "agentId": "a1", "result": {"ok": True}},
    ])
    assert list(_adapter(projects).parse(f)) == []


def test_tool_pairing_still_works_inside_a_subagent_thread(projects: Path):
    f = _subagent_file(projects, [
        _sub(_assistant("u1", blocks=[{"type": "tool_use", "id": "t1", "name": "Bash"}])),
        _sub(_user("u2", blocks=[{"type": "tool_result", "tool_use_id": "t1"}])),
    ])
    events = list(_adapter(projects).parse(f))
    assert [(e.kind, e.tool_name, e.tool_use_id) for e in events] == [
        (EventKind.TOOL_USE, "Bash", "t1"), (EventKind.TOOL_RESULT, None, "t1"),
    ]
    assert all(e.is_subagent for e in events)


def test_parallel_tool_calls_in_a_subagent_turn_keep_their_thread(projects: Path):
    """Two subagent lines in the real corpus issue two Bash calls in one turn."""
    f = _subagent_file(projects, [_sub(_assistant("u1", blocks=[
        {"type": "text", "text": "x"},
        {"type": "tool_use", "id": "t1", "name": "Bash"},
        {"type": "tool_use", "id": "t2", "name": "Bash"},
    ]))])
    events = list(_adapter(projects).parse(f))
    assert [(e.native_event_id, e.tool_use_id) for e in events] == [("u1", "t1"), ("u1#2", "t2")]
    assert all(e.is_subagent and e.native_thread_id == AGENT for e in events)


# --- kind mapping ---------------------------------------------------------

def test_kind_mapping(projects: Path):
    f = _write(projects / "-repo" / "s.jsonl", [
        _user("u1"),
        _assistant("u2"),
        _assistant("u3", blocks=[{"type": "thinking", "thinking": "..."}]),
        _assistant("u4", blocks=[{"type": "tool_use", "id": "t1", "name": "Bash"}]),
        _user("u5", blocks=[{"type": "tool_result", "tool_use_id": "t1"}]),
        _line(type="system", uuid="u6", subtype="stop_hook_summary"),
        _line(type="mode", uuid="u7"),
        _line(type="attachment", uuid="u8", attachment={"type": "environment"}),
        _line(type="queue-operation", operation="add"),
    ])
    assert [e.kind for e in _adapter(projects).parse(f)] == [
        EventKind.USER_PROMPT, EventKind.ASSISTANT, EventKind.ASSISTANT,
        EventKind.TOOL_USE, EventKind.TOOL_RESULT, EventKind.SYSTEM,
        EventKind.SYSTEM, EventKind.OTHER, EventKind.OTHER,
    ]


def test_string_content_user_message_is_a_prompt(projects: Path):
    f = _write(projects / "-repo" / "s.jsonl",
               [_line(type="user", uuid="u1", message={"role": "user", "content": "hello"})])
    (event,) = list(_adapter(projects).parse(f))
    assert event.kind is EventKind.USER_PROMPT


def test_multi_block_user_turn_is_one_event(projects: Path):
    """Images alongside text stay a single user prompt -- one event per message."""
    f = _write(projects / "-repo" / "s.jsonl", [
        _user("u1", blocks=[{"type": "image"}, {"type": "text", "text": "look"}]),
    ])
    events = list(_adapter(projects).parse(f))
    assert len(events) == 1 and events[0].kind is EventKind.USER_PROMPT


# --- tool pairing ---------------------------------------------------------

def test_tool_use_carries_name_and_id(projects: Path):
    f = _write(projects / "-repo" / "s.jsonl",
               [_assistant("u1", blocks=[{"type": "tool_use", "id": "toolu_1", "name": "Agent"}])])
    (event,) = list(_adapter(projects).parse(f))
    assert (event.tool_name, event.tool_use_id) == ("Agent", "toolu_1")


def test_tool_result_carries_the_id_it_answers(projects: Path):
    f = _write(projects / "-repo" / "s.jsonl",
               [_user("u1", blocks=[{"type": "tool_result", "tool_use_id": "toolu_1"}])])
    (event,) = list(_adapter(projects).parse(f))
    assert event.tool_use_id == "toolu_1"
    assert event.tool_name is None


def test_extra_tool_blocks_get_unique_ids_and_no_usage(projects: Path):
    """One event per message, except that additional tool blocks each get their
    own event so no Agent call can go unpaired."""
    f = _write(projects / "-repo" / "s.jsonl", [_assistant(
        "u1",
        blocks=[{"type": "text", "text": "x"},
                {"type": "tool_use", "id": "t1", "name": "Bash"},
                {"type": "tool_use", "id": "t2", "name": "Agent"}],
        usage={"input_tokens": 5, "output_tokens": 6,
               "cache_read_input_tokens": 7, "cache_creation_input_tokens": 8},
    )])
    events = list(_adapter(projects).parse(f))
    assert [(e.native_event_id, e.tool_use_id) for e in events] == [("u1", "t1"), ("u1#2", "t2")]
    assert all(e.kind is EventKind.TOOL_USE for e in events)
    assert events[0].input_tokens == 5
    assert events[1].input_tokens is None and events[1].output_tokens is None
    assert events[1].cache_read_tokens is None and events[1].cache_write_tokens is None


# --- payload fields -------------------------------------------------------

def test_token_usage_is_mapped(projects: Path):
    f = _write(projects / "-repo" / "s.jsonl", [_assistant("u1", usage={
        "input_tokens": 12, "output_tokens": 34,
        "cache_read_input_tokens": 56, "cache_creation_input_tokens": 78,
        "service_tier": "standard",
    })])
    (event,) = list(_adapter(projects).parse(f))
    assert (event.input_tokens, event.output_tokens,
            event.cache_read_tokens, event.cache_write_tokens) == (12, 34, 56, 78)


def test_missing_usage_is_none_not_zero(projects: Path):
    f = _write(projects / "-repo" / "s.jsonl", [_user("u1")])
    (event,) = list(_adapter(projects).parse(f))
    assert event.input_tokens is None and event.cache_write_tokens is None


def test_synthetic_is_not_a_model(projects: Path):
    f = _write(projects / "-repo" / "s.jsonl",
               [_assistant("u1", model="<synthetic>"), _assistant("u2", model="claude-opus-5")])
    assert [e.model for e in _adapter(projects).parse(f)] == [None, "claude-opus-5"]


def test_session_metadata_is_carried(projects: Path):
    f = _write(projects / "-repo" / "s.jsonl",
               [_assistant("u1", cwd="/work/tree", gitBranch="feat/x", version="2.1.0")])
    (event,) = list(_adapter(projects).parse(f))
    assert (event.cwd, event.git_branch, event.cli_version) == ("/work/tree", "feat/x", "2.1.0")


def test_timestamp_becomes_epoch_milliseconds_utc(projects: Path):
    f = _write(projects / "-repo" / "s.jsonl", [
        _assistant("u1", timestamp="2026-09-19T10:00:00.000Z"),
        _assistant("u2", timestamp="2026-09-19T10:00:00.123Z"),
    ])
    a, b = list(_adapter(projects).parse(f))
    # calendar.timegm(time.strptime("2026-09-19T10:00:00", "%Y-%m-%dT%H:%M:%S")) * 1000
    assert a.ts_ms == 1789812000000
    assert b.ts_ms - a.ts_ms == 123
    assert a.ordinal is None


def test_source_is_claude_code(projects: Path):
    f = _write(projects / "-repo" / "s.jsonl", [_assistant("u1")])
    assert next(iter(_adapter(projects).parse(f))).source == "claude_code"


# --- robustness -----------------------------------------------------------

def test_events_without_timestamp_are_skipped(projects: Path):
    doc = _assistant("u1")
    doc.pop("timestamp")
    f = _write(projects / "-repo" / "s.jsonl", [doc, _assistant("u2")])
    assert [e.native_event_id for e in _adapter(projects).parse(f)] == ["u2"]


def test_truncated_final_line_is_skipped_without_raising(projects: Path):
    f = _write(projects / "-repo" / "s.jsonl", [_assistant("u1"), _assistant("u2")])
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_assistant("u3"))[:40])  # mid-write, no newline
    events = list(_adapter(projects).parse(f))
    assert [e.native_event_id for e in events] == ["u1", "u2"]
    # the partial line is left behind, so the next run re-reads it
    assert events[-1].byte_end < f.stat().st_size


def test_completed_line_is_picked_up_after_truncation(projects: Path):
    f = _write(projects / "-repo" / "s.jsonl", [_assistant("u1")])
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_assistant("u2"))[:40])
    resume = list(_adapter(projects).parse(f))[-1].byte_end
    with f.open("w", encoding="utf-8") as fh:  # writer finishes the line
        fh.write("".join(json.dumps(d) + "\n" for d in (_assistant("u1"), _assistant("u2"))))
    assert [e.native_event_id for e in _adapter(projects).parse(f, resume)] == ["u2"]


def test_blank_and_corrupt_lines_are_skipped(projects: Path):
    f = projects / "-repo" / "s.jsonl"
    f.parent.mkdir(parents=True)
    f.write_text("\n".join([json.dumps(_assistant("u1")), "", "{not json", "[1,2,3]",
                            json.dumps(_assistant("u2"))]) + "\n", encoding="utf-8")
    assert [e.native_event_id for e in _adapter(projects).parse(f)] == ["u1", "u2"]


def test_empty_file_yields_nothing(projects: Path):
    f = _write(projects / "-repo" / "s.jsonl", [])
    assert list(_adapter(projects).parse(f)) == []


# --- resumability ---------------------------------------------------------

def _corpus(projects: Path) -> Path:
    docs = [
        _user("u1"),
        _assistant("u2", usage={"input_tokens": 1, "output_tokens": 2,
                                "cache_read_input_tokens": 3, "cache_creation_input_tokens": 4}),
        _assistant("u3", blocks=[{"type": "tool_use", "id": "t1", "name": "Bash"}]),
        _user("u4", blocks=[{"type": "tool_result", "tool_use_id": "t1"}]),
        _line(type="queue-operation", operation="add"),
        _line(type="system", uuid="u6", subtype="stop_hook_summary"),
    ]
    return _write(projects / "-repo" / "s.jsonl", docs)


def test_byte_end_is_the_end_of_the_line(projects: Path):
    f = _corpus(projects)
    events = list(_adapter(projects).parse(f))
    raw = f.read_bytes()
    assert events[-1].byte_end == len(raw)
    assert [e.byte_end for e in events] == sorted({e.byte_end for e in events})
    for n, event in enumerate(events, start=1):
        assert raw[event.byte_end - 1:event.byte_end] == b"\n"
        assert raw[:event.byte_end].count(b"\n") == n  # n complete lines consumed


def test_resuming_from_byte_end_yields_exactly_the_events_after_it(projects: Path):
    f = _corpus(projects)
    adapter = _adapter(projects)
    full = list(adapter.parse(f))
    assert len(full) == 6
    for n, event in enumerate(full):
        assert list(adapter.parse(f, event.byte_end)) == full[n + 1:], f"resume after #{n}"


def test_resume_after_append_yields_only_new_events(projects: Path):
    f = _corpus(projects)
    adapter = _adapter(projects)
    before = list(adapter.parse(f))
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_assistant("u7")) + "\n")
    after = list(adapter.parse(f, before[-1].byte_end))
    assert [e.native_event_id for e in after] == ["u7"]
    assert after[0].byte_end == f.stat().st_size


def test_resume_from_zero_is_a_full_parse(projects: Path):
    f = _corpus(projects)
    adapter = _adapter(projects)
    assert list(adapter.parse(f, 0)) == list(adapter.parse(f))


def test_resume_from_end_of_file_yields_nothing(projects: Path):
    f = _corpus(projects)
    assert list(_adapter(projects).parse(f, f.stat().st_size)) == []


def _subagent_corpus(projects: Path, *, workflow: str | None = None) -> Path:
    docs = [
        _sub(_user("u1")),
        _sub(_assistant("u2", attributionAgent="general-purpose",
                        usage={"input_tokens": 1, "output_tokens": 2,
                               "cache_read_input_tokens": 3, "cache_creation_input_tokens": 4})),
        _sub(_assistant("u3", blocks=[{"type": "tool_use", "id": "t1", "name": "Bash"}])),
        _sub(_user("u4", blocks=[{"type": "tool_result", "tool_use_id": "t1"}])),
        _sub(_line(type="attachment", uuid="u5", attachment={"type": "deferred_tools_delta"})),
    ]
    return _subagent_file(projects, docs, workflow=workflow)


@pytest.mark.parametrize("workflow", [None, "wf_db5151b5-e69"])
def test_subagent_files_are_resumable(projects: Path, workflow):
    """Both nested shapes resume from any byte_end, identically."""
    f = _subagent_corpus(projects, workflow=workflow)
    adapter = _adapter(projects)
    full = list(adapter.parse(f))
    assert len(full) == 5
    assert all(e.is_subagent and e.native_thread_id == AGENT for e in full)
    for n, event in enumerate(full):
        assert list(adapter.parse(f, event.byte_end)) == full[n + 1:], f"resume after #{n}"
    assert full[-1].byte_end == f.stat().st_size


def test_resume_after_append_to_a_subagent_file(projects: Path):
    f = _subagent_corpus(projects)
    adapter = _adapter(projects)
    before = list(adapter.parse(f))
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_sub(_assistant("u6"))) + "\n")
    after = list(adapter.parse(f, before[-1].byte_end))
    assert [e.native_event_id for e in after] == ["u6"]
    assert after[0].is_subagent and after[0].agent_name == "general-purpose"
    assert after[0].byte_end == f.stat().st_size
