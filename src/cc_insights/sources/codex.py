"""Codex source adapter.

Codex writes one JSONL file per THREAD, not per session. The whole shape of
this adapter follows from two measured facts (docs/FINDINGS.md sections 2-4):

1. Only the first ``session_meta`` line of a file carries the ids. It is
   resolved ONCE PER FILE and applied to every event in that file. A per-line
   fallback splits each file into a phantom extra session and inflated an early
   session count from 188 to 403.

   Three real files carry a SECOND ``session_meta`` line whose ``payload.id``
   is the *root session* id rather than the file's own thread id. Only the
   first one is authoritative; taking the last would corrupt those threads.

2. ``ordinal`` is FILE-SCOPED and restarts at 0 in every thread, so the dedup
   key must be ``"<thread_id>:<ordinal>"``. Keying on ``(session_id, ordinal)``
   collides 8,709 times on this corpus with *different timestamps* -- i.e. it
   silently drops 8,709 real events. This is the single most important line in
   this module; see ``tests/test_codex.py::test_ordinal_alone_would_collide``.

METADATA ONLY. Nothing here reads ``payload.content``, ``arguments``, ``input``
or ``output``. ``RawEvent`` is frozen+slots so it could not carry them anyway.

payload.type -> EventKind mapping
---------------------------------
Resolution order: ``payload.type`` first, then the top-level ``type`` for the
records that have no ``payload.type``, then OTHER.

======================================  ==============  =====================
record                                  EventKind       note
======================================  ==============  =====================
message (role=user)                     USER_PROMPT     a human turn
message (role=developer|system)         SYSTEM          injected instructions
message (role=assistant)                ASSISTANT
agent_message                           ASSISTANT       agent-to-agent message
reasoning                               ASSISTANT       model reasoning summary
function_call                           TOOL_USE        name + call_id
custom_tool_call                        TOOL_USE        name + call_id
tool_search_call                        TOOL_USE        call_id
web_search_call                         TOOL_USE        no call_id in payload
function_call_output                    TOOL_RESULT     call_id
custom_tool_call_output                 TOOL_RESULT     call_id
tool_search_output                      TOOL_RESULT     call_id
token_count                             SYSTEM          carries token usage
item_completed                          SYSTEM          see below
task_started / task_complete            SYSTEM
turn_aborted                            SYSTEM
thread_settings_applied                 SYSTEM          carries model
thread_goal_updated                     SYSTEM
session_meta (no payload.type)          SYSTEM
turn_context (no payload.type)          SYSTEM          carries model + cwd
world_state (no payload.type)           SYSTEM
compacted (no payload.type)             SYSTEM
token_usage_record (no payload.type)    SYSTEM          carries token usage
inter_agent_communication_metadata      SYSTEM
anything else                           OTHER
======================================  ==============  =====================

``item_completed`` is the UI-level event stream mirroring the ``response_item``
records (214 of 215 files contain both). It is mapped to SYSTEM rather than to
its ``item.type`` on purpose: mapping it through would double-count every
prompt, message and tool call in the corpus.

Token usage
-----------
Codex records usage in several places that overlap, so reading every usage
block counts the same request two or three times. The rules below follow
ccusage (``rust/adapters/codex/src/parser.rs`` and ``replay.rs``); each one
fixed a measured overcount on the real corpus.

* ``event_msg/token_count`` is the primary record. Its
  ``info.last_token_usage`` counts ONLY when ``info.total_token_usage``
  advanced since the previous ``token_count`` in the file. Codex re-emits the
  same snapshot many times (1,552 repeats locally), and a repeated total means
  nothing new was spent. When ``last_token_usage`` is absent, or the total did
  not advance, the usage is the per-field saturating difference of the totals,
  which is zero for a repeat.
* ``token_usage_record`` repeats a ``token_count`` almost always (2,344 of
  2,351 locally). It counts only for a remote compaction request, i.e. once
  its ``response_id`` is paired with a ``compacted`` line's
  ``compaction_response_id``, and only if no advancing ``token_count`` already
  covered it. Pairing needs both lines; the tokens go on whichever of the two
  comes second, so no event ever has to look ahead.
* A forked or spawned thread (``forked_from_id`` or
  ``source.subagent.thread_spawn.parent_thread_id``) starts with a replay of the
  parent's usage. Leading usage that equals the parent's usage sequence up to
  the fork instant is not counted. When the parent's log is not on this
  machine, a burst of usage at the head of the file written less than a second
  apart is skipped instead. Copied compaction requests are skipped too.

Events whose usage does not count keep their ``native_event_id`` and their
place on the timeline; they just carry no tokens.

All of that state is per file and starts at byte 0, so a resumed parse
(``from_byte > 0``) replays the earlier lines through the same state machine,
decoding only the few lines that can carry usage, and yields events after
``from_byte`` only.

Codex's ``input_tokens`` INCLUDES ``cached_input_tokens`` (verified on 25,181
usage blocks: ``total_tokens == input_tokens + output_tokens`` and
``cached_input_tokens <= input_tokens`` in every non-degenerate case), and it
includes ``cache_write_input_tokens`` too. The contract's ``input_tokens``
means *fresh* input, as it does for Claude Code, so both cache parts are
subtracted out: ``input - cached - cache_write``, never below zero. Otherwise a
cache write would be billed twice, once as input and once as a write.
``reasoning_output_tokens`` is a subset of ``output_tokens`` and is not added.
The field aliases ccusage accepts (``prompt_tokens``, ``completion_tokens``,
``cached_tokens``, ``cache_read_input_tokens``, ``cache_creation_input_tokens``,
``reasoning_tokens``) are read too.
"""

from __future__ import annotations

import glob as _glob
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Iterator, Sequence

from cc_insights import ids
from cc_insights.config import Config, default_source_globs, expand_glob
from cc_insights.sources.base import EventKind, RawEvent

SOURCE_NAME = "codex"

# --- kind mapping -------------------------------------------------------

_PAYLOAD_KIND: dict[str, EventKind] = {
    "reasoning": EventKind.ASSISTANT,
    "agent_message": EventKind.ASSISTANT,
    "function_call": EventKind.TOOL_USE,
    "custom_tool_call": EventKind.TOOL_USE,
    "tool_search_call": EventKind.TOOL_USE,
    "web_search_call": EventKind.TOOL_USE,
    "function_call_output": EventKind.TOOL_RESULT,
    "custom_tool_call_output": EventKind.TOOL_RESULT,
    "tool_search_output": EventKind.TOOL_RESULT,
    "token_count": EventKind.SYSTEM,
    "item_completed": EventKind.SYSTEM,
    "task_started": EventKind.SYSTEM,
    "task_complete": EventKind.SYSTEM,
    "turn_aborted": EventKind.SYSTEM,
    "thread_settings_applied": EventKind.SYSTEM,
    "thread_goal_updated": EventKind.SYSTEM,
}

_TOP_LEVEL_KIND: dict[str, EventKind] = {
    "session_meta": EventKind.SYSTEM,
    "turn_context": EventKind.SYSTEM,
    "world_state": EventKind.SYSTEM,
    "compacted": EventKind.SYSTEM,
    "token_usage_record": EventKind.SYSTEM,
    "inter_agent_communication_metadata": EventKind.SYSTEM,
}

# `developer` messages are instructions injected into the transcript, not a
# turn the human initiated, so they are SYSTEM rather than USER_PROMPT.
_ROLE_KIND: dict[str, EventKind] = {
    "user": EventKind.USER_PROMPT,
    "assistant": EventKind.ASSISTANT,
    "developer": EventKind.SYSTEM,
    "system": EventKind.SYSTEM,
}

# Payload types whose tool name is implied rather than stated.
_IMPLIED_TOOL_NAME = {
    "web_search_call": "web_search",
    "tool_search_call": "tool_search",
    "tool_search_output": "tool_search",
}


# --- per-file session metadata -----------------------------------------


@dataclass(frozen=True, slots=True)
class FileMeta:
    """Everything the first ``session_meta`` line of a file establishes.

    Resolved once per file and applied to every event in it.
    """

    native_thread_id: str
    native_session_id: str
    parent_native_thread_id: str | None = None
    is_subagent: bool = False
    agent_name: str | None = None
    cli_version: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    #: The thread whose history this one replayed at its head: the fork source
    #: (``forked_from_id``) or the spawning thread
    #: (``source.subagent.thread_spawn.parent_thread_id``). None for a thread
    #: that started empty.
    replayed_from_id: str | None = None
    #: Timestamp of the ``session_meta`` record: the fork instant, which bounds
    #: how much of the parent's usage the replay can contain.
    started_ms: int | None = None


def _s(value: Any) -> str | None:
    """A non-empty string, or None."""
    return value if isinstance(value, str) and value else None


def _i(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _spawn_parent(source: Any) -> str | None:
    """``source.subagent.thread_spawn.parent_thread_id``, if that path exists."""
    if not isinstance(source, dict):
        return None
    subagent = source.get("subagent")
    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    return _s(spawn.get("parent_thread_id")) if isinstance(spawn, dict) else None


def _meta_from_payload(
    payload: Any, fallback_id: str, started_ms: int | None = None
) -> FileMeta:
    if not isinstance(payload, dict):
        payload = {}
    thread = _s(payload.get("id")) or fallback_id
    session = _s(payload.get("session_id")) or thread
    source = payload.get("source")
    # `source` is a plain string ("vscode", "cli") on root threads and a dict
    # {"subagent": {"thread_spawn": {...}}} on subagent threads. The membership
    # test is a KEY test, so it must not be run against a string.
    is_subagent = isinstance(source, dict) and "subagent" in source
    replayed_from = _s(payload.get("forked_from_id")) or _spawn_parent(source)
    git = payload.get("git")
    return FileMeta(
        native_thread_id=thread,
        native_session_id=session,
        parent_native_thread_id=_s(payload.get("parent_thread_id")),
        is_subagent=is_subagent,
        agent_name=_s(payload.get("agent_nickname")),
        cli_version=_s(payload.get("cli_version")),
        cwd=_s(payload.get("cwd")),
        git_branch=_s(git.get("branch")) if isinstance(git, dict) else None,
        replayed_from_id=replayed_from if replayed_from != thread else None,
        started_ms=started_ms,
    )


# --- line-level helpers -------------------------------------------------


def _iter_lines(fh: BinaryIO, start: int) -> Iterator[tuple[bytes, int]]:
    """Yield ``(line_bytes, byte_end)`` for every COMPLETE line from `start`.

    `byte_end` is the offset just past the line's terminator, which is what
    `RawEvent.byte_end` stores and what a later `from_byte` resumes at.

    A final line with no newline terminator is a half-written record (the agent
    may be mid-write); it is dropped and its bytes are not accounted for, so
    the next run picks it up once it is complete. Every one of the 215 real
    files ends with a newline, so this only ever fires on a live file.
    """
    fh.seek(start)
    pos = start
    for raw in fh:
        if not raw.endswith(b"\n"):
            return
        pos += len(raw)
        yield raw, pos


def _loads(raw: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _ts_ms(raw: Any) -> int | None:
    """ISO-8601 (``...Z`` on this corpus) -> epoch milliseconds UTC."""
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return round(dt.timestamp() * 1000)


# --- token usage --------------------------------------------------------

#: One usage block, normalized:
#: ``(input incl. cache, cached, cache write, output, reasoning, total)``.
#: A plain tuple so equality (replay matching, compaction pairing) is exact.
Usage = tuple[int, int, int, int, int, int]

# Field aliases, in ccusage's precedence order (codex/src/types.rs:272-350).
_INPUT_KEYS = ("input_tokens", "prompt_tokens", "input")
_CACHED_KEYS = ("cached_input_tokens", "cache_read_input_tokens", "cached_tokens")
_CACHE_WRITE_KEYS = ("cache_write_input_tokens", "cache_creation_input_tokens")
_OUTPUT_KEYS = ("output_tokens", "completion_tokens", "output")
_REASONING_KEYS = ("reasoning_output_tokens", "reasoning_tokens")

#: Longest pause inside the burst a fork writes when it replays its parent's
#: history. ccusage measured bursts of 10-40 ms followed by a 6-15 s pause
#: before the child's own first turn (codex/src/parser.rs:116-127).
_REPLAY_BURST_PAUSE_MS = 1_000

# Byte markers for the lines that can change usage state. Anything else is
# skipped undecoded while a resumed parse replays the lines before `from_byte`.
_USAGE_MARKERS = (b"token_count", b"token_usage_record", b"compacted")


def _first_count(block: dict[str, Any], keys: tuple[str, ...]) -> int:
    """The first alias present as a non-negative number, else 0."""
    for key in keys:
        value = block.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value != value or value < 0:  # NaN or negative: not a count
            continue
        return int(value)
    return 0


def _normalize(
    total_in: int, cached: int, write: int, out: int, reasoning: int, total: int
) -> Usage:
    """Clamp the cache parts into the input they are part of."""
    cached = min(cached, total_in)
    write = min(write, max(total_in - cached, 0))
    return (total_in, cached, write, out, reasoning, total)


def _raw_usage(block: Any) -> Usage | None:
    """A Codex usage block -> :data:`Usage`, or None if it is not a block.

    Missing fields count as 0, as in ccusage. A missing or zero
    ``total_tokens`` is derived as ``input + output``; reasoning is already
    inside output, so it is not added.
    """
    if not isinstance(block, dict):
        return None
    total_in = _first_count(block, _INPUT_KEYS)
    out = _first_count(block, _OUTPUT_KEYS)
    total = _first_count(block, ("total_tokens",)) or total_in + out
    return _normalize(
        total_in,
        _first_count(block, _CACHED_KEYS),
        _first_count(block, _CACHE_WRITE_KEYS),
        out,
        _first_count(block, _REASONING_KEYS),
        total,
    )


def _subtract(current: Usage, previous: Usage | None) -> Usage:
    """Per-field saturating ``current - previous``."""
    if previous is None:
        return current
    return _normalize(*(max(c - p, 0) for c, p in zip(current, previous)))


def _response_id(value: Any) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None


def _token_fields(
    usage: Usage | None,
) -> tuple[int | None, int | None, int | None, int | None]:
    """-> (fresh input, output, cache read, cache write) for a RawEvent.

    Codex's input includes both cache parts; the contract wants fresh input,
    so both are subtracted out.
    """
    if usage is None:
        return None, None, None, None
    total_in, cached, write, out, _reasoning, _total = usage
    return max(total_in - cached - write, 0), out, cached, write


class _UsageLedger:
    """Per-file state deciding which usage records count, in file order.

    A port of ccusage's ``visit_codex_session_entry``
    (codex/src/parser.rs:342-555); see the module docstring for the rules.
    :meth:`feed` takes every decoded record and returns the usage that record
    makes countable, with the ``response_id`` when it is a compaction request.
    """

    __slots__ = (
        "previous_total", "compacted", "pending", "settled", "latest_record",
        "record_ts",
    )

    def __init__(self) -> None:
        self.previous_total: Usage | None = None
        #: response ids named by a `compacted` line
        self.compacted: set[str] = set()
        #: response id -> (usage, thread_token_usage), awaiting its pairing
        self.pending: dict[str, tuple[Usage, Usage | None]] = {}
        #: response ids already counted, or covered by a token_count
        self.settled: set[str] = set()
        self.latest_record: str | None = None
        #: response id -> timestamp of its token_usage_record
        self.record_ts: dict[str, int] = {}

    def compactions(self) -> dict[str, int]:
        """Compaction requests this file recorded, by response id."""
        return {rid: ts for rid, ts in self.record_ts.items() if rid in self.compacted}

    def feed(
        self, record: dict[str, Any], ts_ms: int | None
    ) -> tuple[Usage, str | None] | None:
        top_type = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            payload = None

        if top_type == "compacted":
            rid = _response_id(payload.get("compaction_response_id")) if payload else None
            if rid is None:
                return None
            self.compacted.add(rid)
            pending = self.pending.pop(rid, None)
            if pending is not None and rid not in self.settled:
                self.settled.add(rid)
                return pending[0], rid
            return None

        if top_type == "token_usage_record":
            rid = _response_id(payload.get("response_id")) if payload else None
            if rid is None or rid in self.settled or ts_ms is None:
                return None
            usage = _raw_usage(payload.get("usage"))
            if usage is None or not any(usage):
                return None
            self.record_ts.setdefault(rid, ts_ms)
            self.latest_record = rid
            if rid in self.compacted:
                self.settled.add(rid)
                return usage, rid
            self.pending.setdefault(
                rid, (usage, _raw_usage(payload.get("thread_token_usage")))
            )
            return None

        if payload is None or payload.get("type") != "token_count" or ts_ms is None:
            return None
        info = payload.get("info")
        if not isinstance(info, dict):
            info = {}
        total = _raw_usage(info.get("total_token_usage"))
        advanced = total is None or total != self.previous_total
        last = _raw_usage(info.get("last_token_usage"))
        if last is not None and advanced:
            usage: Usage | None = last
        elif total is not None:
            usage = _subtract(total, self.previous_total)
        else:
            usage = None
        if total is not None:
            self.previous_total = total
        if usage is None or not any(usage[:5]):
            return None

        # A local compaction writes its token_usage_record, then an advancing
        # token_count for the same request: that token_count is the count, and
        # the record must not be paired later. Only the latest record can be
        # covered, so an unrelated request with equal counts cannot consume it.
        if advanced and self.latest_record is not None:
            pending = self.pending.get(self.latest_record)
            if pending is not None and (
                pending[0] == usage
                or (pending[1] is not None and pending[1] == total)
            ):
                del self.pending[self.latest_record]
                self.settled.add(self.latest_record)
        return usage, None


class _ReplayFilter:
    """Recognizes the parent's usage a forked thread replayed at its head.

    A port of ccusage's ``CodexReplayState`` (codex/src/parser.rs:96-267).
    `prefix` is None for a thread that is not a fork, and empty for a fork
    whose parent log is unavailable. `burst_start` is called at most once, when
    the parent cannot anchor the replay.
    """

    __slots__ = ("_prefix", "_index", "_state", "_previous", "_burst_start")

    def __init__(
        self, prefix: Sequence[Usage] | None, burst_start: Callable[[], int | None]
    ) -> None:
        self._prefix = prefix or ()
        self._index = 0
        self._state = "done" if prefix is None else "matching"
        self._previous = 0
        self._burst_start = burst_start

    def replayed(self, usage: Usage, ts_ms: int | None) -> bool:
        while True:
            if self._state == "matching":
                if self._index < len(self._prefix) and self._prefix[self._index] == usage:
                    self._index += 1
                    return True
                # The parent stream cannot anchor this replay. Only a thread
                # that matched nothing falls back to the rewritten burst.
                start = self._burst_start() if self._index == 0 else None
                if start is None:
                    self._state = "done"
                else:
                    self._state, self._previous = "burst", start
            elif self._state == "burst":
                if ts_ms is not None and 0 <= ts_ms - self._previous <= _REPLAY_BURST_PAUSE_MS:
                    self._previous = ts_ms
                    return True
                self._state = "done"
            else:
                return False


def _rewritten_burst_start(path: Path) -> int | None:
    """Start of a burst of usage at the head of `path`, if it opens with one.

    A thread whose first two ``token_count`` events are under a second apart
    replayed history it did not spend; one that pauses between them was
    recording its own turns from the start (codex/src/parser.rs:129-177).
    """
    first: int | None = None
    try:
        fh = open(path, "rb")
    except OSError:
        return None
    with fh:
        for raw, _end in _iter_lines(fh, 0):
            if b"token_count" not in raw:
                continue
            record = _loads(raw)
            payload = record.get("payload") if record else None
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            if not isinstance(info, dict) or not (
                isinstance(info.get("last_token_usage"), dict)
                or isinstance(info.get("total_token_usage"), dict)
            ):
                continue
            ts_ms = _ts_ms(record.get("timestamp"))
            if ts_ms is None:
                continue
            if first is None:
                first = ts_ms
                continue
            return first if 0 <= ts_ms - first <= _REPLAY_BURST_PAUSE_MS else None
    return None


@dataclass(frozen=True, slots=True)
class _ParentUsage:
    """A parent thread's countable usage, as a fork would have replayed it."""

    #: (timestamp, usage) of every non-compaction usage event, in file order.
    events: tuple[tuple[int | None, Usage], ...]
    #: Compaction requests the parent recorded, by response id.
    compactions: dict[str, int]


def _read_parent_usage(path: Path) -> _ParentUsage:
    """Run `path` through a fresh ledger, without any replay filtering of its
    own (ccusage reads the parent stream unfiltered too, replay.rs:400-428)."""
    ledger = _UsageLedger()
    events: list[tuple[int | None, Usage]] = []
    with open(path, "rb") as fh:
        for raw, _end in _iter_lines(fh, 0):
            if not any(marker in raw for marker in _USAGE_MARKERS):
                continue
            record = _loads(raw)
            if record is None:
                continue
            ts_ms = _ts_ms(record.get("timestamp"))
            hit = ledger.feed(record, ts_ms)
            if hit is not None and hit[1] is None:
                events.append((ts_ms, hit[0]))
    return _ParentUsage(events=tuple(events), compactions=ledger.compactions())


def _kind(top_type: str | None, payload_type: str | None, payload: dict[str, Any]) -> EventKind:
    if payload_type == "message":
        return _ROLE_KIND.get(_s(payload.get("role")) or "", EventKind.OTHER)
    if payload_type is not None:
        kind = _PAYLOAD_KIND.get(payload_type)
        if kind is not None:
            return kind
        # A payload.type we have never seen: unrecognized, by contract OTHER.
        return EventKind.OTHER
    return _TOP_LEVEL_KIND.get(top_type or "", EventKind.OTHER)


# --- the adapter --------------------------------------------------------


class CodexAdapter:
    """Turns ``~/.codex`` rollout files into normalized :class:`RawEvent`s."""

    name = SOURCE_NAME

    def __init__(
        self,
        config: Config | None = None,
        globs: Sequence[str | os.PathLike[str]] | None = None,
    ) -> None:
        self._config = config
        self._globs = [str(g) for g in globs] if globs is not None else None
        # Per-instance caches for fork replay. One instance serves one ingest
        # run, so neither outlives the files it describes for long; the parent
        # usage is keyed on size and mtime anyway because a parent can grow.
        self._thread_ids: dict[Path, str] = {}
        self._parent_usage: dict[tuple[Path, int, int], _ParentUsage] = {}

    # -- discovery -------------------------------------------------------

    def _patterns(
        self, globs: Sequence[str | os.PathLike[str]] | None = None
    ) -> list[str]:
        if globs is not None:
            return [str(g) for g in globs]
        if self._globs is not None:
            return list(self._globs)
        if self._config is not None:
            return [str(p) for p in self._config.globs_for(SOURCE_NAME)]
        return list(default_source_globs()[SOURCE_NAME])

    def discover(
        self, globs: Sequence[str | os.PathLike[str]] | None = None
    ) -> Iterable[Path]:
        """All Codex log files on this machine, deduped and sorted.

        The optional `globs` (or the constructor's `config`/`globs`) lets tests
        point at a fixture directory instead of ``~/.codex``.
        """
        found: dict[str, Path] = {}
        for pattern in self._patterns(globs):
            expanded = expand_glob(pattern)
            for hit in _glob.glob(expanded, recursive=True):
                path = Path(hit)
                if path.is_file():
                    found[str(path)] = path
        return sorted(found.values())

    # -- metadata --------------------------------------------------------

    def read_file_meta(self, path: Path | str) -> FileMeta:
        """The ids for `path`, from its FIRST ``session_meta`` line."""
        path = Path(path)
        with open(path, "rb") as fh:
            return self._read_file_meta(fh, path)

    @staticmethod
    def _read_file_meta(fh: BinaryIO, path: Path) -> FileMeta:
        fallback = path.stem
        for raw, _end in _iter_lines(fh, 0):
            record = _loads(raw)
            if record is None:
                continue
            if record.get("type") == "session_meta":
                # FIRST only: three real files carry a second session_meta
                # whose `id` is the root session id, not this thread's.
                return _meta_from_payload(
                    record.get("payload"), fallback, _ts_ms(record.get("timestamp"))
                )
        # No session_meta at all (never on the real corpus): key off the file
        # name so the thread is still self-consistent and stable.
        return FileMeta(native_thread_id=fallback, native_session_id=fallback)

    # -- fork replay -----------------------------------------------------

    def _thread_id_of(self, path: Path) -> str | None:
        key = path.resolve()
        if key not in self._thread_ids:
            try:
                self._thread_ids[key] = self.read_file_meta(path).native_thread_id
            except OSError:
                return None
        return self._thread_ids[key]

    def _find_thread_file(self, thread_id: str, exclude: Path) -> Path | None:
        """The log of `thread_id` among the discovered files, if it is here.

        Rollout files are named ``rollout-<time>-<thread id>.jsonl``, so the
        name is tried first and only confirmed by reading its ``session_meta``.
        Every other file is checked only if that fails (a renamed file, or a
        parent that is genuinely missing). An active copy wins over an
        archived one, as in ccusage (paths.rs:92-108).
        """
        excluded = exclude.resolve()
        candidates = [
            p for p in self.discover() if p.resolve() != excluded
        ]
        candidates.sort(key=lambda p: ("archived_sessions" in p.parts, str(p)))
        suffix = f"{thread_id}.jsonl"
        named = [p for p in candidates if p.name.endswith(suffix)]
        for path in named + [p for p in candidates if not p.name.endswith(suffix)]:
            if self._thread_id_of(path) == thread_id:
                return path
        return None

    def _read_parent(self, path: Path) -> _ParentUsage | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        key = (path.resolve(), stat.st_size, stat.st_mtime_ns)
        if key not in self._parent_usage:
            try:
                self._parent_usage[key] = _read_parent_usage(path)
            except OSError:
                return None
        return self._parent_usage[key]

    def _replay_context(
        self, path: Path, meta: FileMeta
    ) -> tuple[list[Usage] | None, frozenset[str]]:
        """-> (the parent usage a fork replayed, the compactions it copied).

        ``(None, {})`` for a thread that is not a fork. ``([], {})`` for a fork
        whose parent log is not here, which makes the replay filter fall back
        to skipping the burst at the head of the file
        (codex/src/replay.rs:153-205).
        """
        if meta.replayed_from_id is None:
            return None, frozenset()
        parent_path = self._find_thread_file(meta.replayed_from_id, path)
        parent = self._read_parent(parent_path) if parent_path is not None else None
        if parent is None:
            return [], frozenset()
        forked_at = meta.started_ms
        # Usage the parent recorded after the fork was never replayed.
        cut = len(parent.events)
        if forked_at is not None:
            for index, (ts_ms, _usage) in enumerate(parent.events):
                if ts_ms is not None and ts_ms > forked_at:
                    cut = index
                    break
        prefix = [usage for _ts, usage in parent.events[:cut]]
        copied = frozenset(
            rid for rid, ts_ms in parent.compactions.items()
            if forked_at is None or ts_ms <= forked_at
        )
        return prefix, copied

    # -- parsing ---------------------------------------------------------

    def parse(self, path: Path | str, from_byte: int = 0) -> Iterator[RawEvent]:
        """Stream events from `path`, starting at byte offset `from_byte`.

        Resumable: `from_byte` is a `byte_end` from a previous run. The
        ``session_meta`` at the top of the file is read first regardless, since
        it is the only line carrying the session and thread ids. The lines
        before `from_byte` are replayed through the usage state machine (only
        the ones that can carry usage are decoded), so whether an event's usage
        counts never depends on where a previous run stopped.
        """
        path = Path(path)
        start = max(from_byte, 0)
        with open(path, "rb") as fh:
            meta = self._read_file_meta(fh, path)
            prefix, copied_compactions = self._replay_context(path, meta)
            ledger = _UsageLedger()
            replay = _ReplayFilter(prefix, lambda: _rewritten_burst_start(path))

            def counted(record: dict[str, Any], ts_ms: int | None) -> Usage | None:
                hit = ledger.feed(record, ts_ms)
                if hit is None:
                    return None
                usage, response_id = hit
                if response_id is not None:
                    # A compaction request is identified by its response id,
                    # so it never consumes the replayed prefix; a copy of the
                    # parent's is dropped by id instead.
                    return None if response_id in copied_compactions else usage
                return None if replay.replayed(usage, ts_ms) else usage

            for raw, byte_end in _iter_lines(fh, 0):
                if byte_end <= start:
                    if any(marker in raw for marker in _USAGE_MARKERS):
                        record = _loads(raw)
                        if record is not None:
                            counted(record, _ts_ms(record.get("timestamp")))
                    continue
                record = _loads(raw)
                if record is None:
                    continue
                ts_ms = _ts_ms(record.get("timestamp"))
                usage = counted(record, ts_ms)
                if ts_ms is None:
                    continue  # no timestamp -> nothing downstream can place it
                yield self._event(record, raw, ts_ms, byte_end, meta, usage)

    @staticmethod
    def _event(
        record: dict[str, Any],
        raw: bytes,
        ts_ms: int,
        byte_end: int,
        meta: FileMeta,
        usage: Usage | None,
    ) -> RawEvent:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        top_type = _s(record.get("type"))
        payload_type = _s(payload.get("type"))

        ordinal = _i(record.get("ordinal"))
        if ordinal is None:
            native_event_id = ids.content_fallback_id(raw.decode("utf-8", "replace"))
        else:
            # NEVER the bare ordinal: it is file-scoped and restarts at 0 in
            # every thread of a session.
            native_event_id = f"{meta.native_thread_id}:{ordinal}"

        kind = _kind(top_type, payload_type, payload)

        tool_name = _IMPLIED_TOOL_NAME.get(payload_type or "") or _s(payload.get("name"))
        if kind not in (EventKind.TOOL_USE, EventKind.TOOL_RESULT):
            tool_name = None
        tool_use_id = _s(payload.get("call_id"))

        model = None
        cwd = meta.cwd
        if top_type == "turn_context":
            model = _s(payload.get("model"))
            cwd = _s(payload.get("cwd")) or cwd
        elif payload_type == "thread_settings_applied":
            settings = payload.get("thread_settings")
            if isinstance(settings, dict):
                model = _s(settings.get("model"))
                cwd = _s(settings.get("cwd")) or cwd
        if model == "<synthetic>":
            model = None

        input_tokens, output_tokens, cache_read, cache_write = _token_fields(usage)

        return RawEvent(
            source=SOURCE_NAME,
            native_session_id=meta.native_session_id,
            native_thread_id=meta.native_thread_id,
            parent_native_thread_id=meta.parent_native_thread_id,
            is_subagent=meta.is_subagent,
            agent_name=meta.agent_name,
            native_event_id=native_event_id,
            ts_ms=ts_ms,
            ordinal=ordinal,
            kind=kind,
            model=model,
            tool_name=tool_name,
            tool_use_id=tool_use_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            cwd=cwd,
            git_branch=meta.git_branch,
            cli_version=meta.cli_version,
            byte_end=byte_end,
        )


__all__ = ["CodexAdapter", "FileMeta", "SOURCE_NAME"]
