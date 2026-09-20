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
Codex DOES record usage, in two places, both mapped here:
  * ``event_msg/token_count`` -> ``payload.info.last_token_usage`` (the delta
    for the last request; ``total_token_usage`` is cumulative and ignored so
    the numbers stay additive).
  * ``token_usage_record`` -> ``payload.usage`` (``turn_token_usage`` and
    ``thread_token_usage`` are cumulative and likewise ignored).

Codex's ``input_tokens`` INCLUDES ``cached_input_tokens`` (verified on 25,181
usage blocks: ``total_tokens == input_tokens + output_tokens`` and
``cached_input_tokens <= input_tokens`` in every non-degenerate case). The
contract's ``input_tokens`` means *fresh* input, as it does for Claude Code, so
the cached part is subtracted out and reported as ``cache_read_tokens``.
"""

from __future__ import annotations

import glob as _glob
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Iterator, Sequence

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


def _s(value: Any) -> str | None:
    """A non-empty string, or None."""
    return value if isinstance(value, str) and value else None


def _i(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _meta_from_payload(payload: Any, fallback_id: str) -> FileMeta:
    if not isinstance(payload, dict):
        payload = {}
    thread = _s(payload.get("id")) or fallback_id
    session = _s(payload.get("session_id")) or thread
    source = payload.get("source")
    # `source` is a plain string ("vscode", "cli") on root threads and a dict
    # {"subagent": {"thread_spawn": {...}}} on subagent threads. The membership
    # test is a KEY test, so it must not be run against a string.
    is_subagent = isinstance(source, dict) and "subagent" in source
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


def _usage(block: Any) -> tuple[int | None, int | None, int | None, int | None]:
    """-> (fresh input, output, cache read, cache write).

    Codex's ``input_tokens`` includes ``cached_input_tokens``; the contract
    wants them separate, so the cached part is subtracted out.
    """
    if not isinstance(block, dict):
        return None, None, None, None
    total_in = _i(block.get("input_tokens"))
    cached = _i(block.get("cached_input_tokens"))
    write = _i(block.get("cache_write_input_tokens"))
    out = _i(block.get("output_tokens"))
    if total_in is None:
        fresh = None
    elif cached is None:
        fresh = total_in
    else:
        fresh = max(total_in - cached, 0)
    return fresh, out, cached, write


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
                return _meta_from_payload(record.get("payload"), fallback)
        # No session_meta at all (never on the real corpus): key off the file
        # name so the thread is still self-consistent and stable.
        return FileMeta(native_thread_id=fallback, native_session_id=fallback)

    # -- parsing ---------------------------------------------------------

    def parse(self, path: Path | str, from_byte: int = 0) -> Iterator[RawEvent]:
        """Stream events from `path`, starting at byte offset `from_byte`.

        Resumable: `from_byte` is a `byte_end` from a previous run. The
        ``session_meta`` at the top of the file is read first regardless, since
        it is the only line carrying the session and thread ids.
        """
        path = Path(path)
        with open(path, "rb") as fh:
            meta = self._read_file_meta(fh, path)
            for raw, byte_end in _iter_lines(fh, max(from_byte, 0)):
                event = self._event(raw, byte_end, meta)
                if event is not None:
                    yield event

    @staticmethod
    def _event(raw: bytes, byte_end: int, meta: FileMeta) -> RawEvent | None:
        record = _loads(raw)
        if record is None:
            return None
        ts_ms = _ts_ms(record.get("timestamp"))
        if ts_ms is None:
            return None  # no timestamp -> nothing downstream can place it

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

        if payload_type == "token_count":
            info = payload.get("info")
            usage = info.get("last_token_usage") if isinstance(info, dict) else None
        elif top_type == "token_usage_record":
            usage = payload.get("usage")
        else:
            usage = None
        input_tokens, output_tokens, cache_read, cache_write = _usage(usage)

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
