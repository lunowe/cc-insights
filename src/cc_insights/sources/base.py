"""The source adapter contract.

Adapters do ONE job: turn a log file into a stream of normalized `RawEvent`s.
They do not compute spans, active time, or concurrency (that is derive.py), and
they do not talk to the database (that is ingest.py). Keeping them this thin is
what lets several adapters be written in parallel against a frozen contract.

METADATA ONLY. `RawEvent` is a frozen slots dataclass with a fixed field set,
so an adapter *cannot* smuggle prompt text, tool arguments or file contents
through it. That is deliberate: the no-content rule is enforced by the shape of
the contract rather than by review. Do not add a free-form field here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Iterable, Iterator, Protocol, runtime_checkable


class EventKind(StrEnum):
    """Normalized event kinds. Anything a source cannot map becomes OTHER."""

    USER_PROMPT = "user_prompt"     # a turn the human initiated
    ASSISTANT = "assistant"         # a model response
    TOOL_USE = "tool_use"           # model invoked a tool
    TOOL_RESULT = "tool_result"     # tool returned
    SYSTEM = "system"               # hooks, mode changes, meta
    OTHER = "other"


# `frozen=True, slots=True` is load-bearing: it makes RawEvent immutable and
# closed, so no adapter can attach message content to it.
@dataclass(frozen=True, slots=True)
class RawEvent:
    """One normalized log event. Maps onto the `event` table plus the session
    and thread fields ingest needs to upsert their parent rows."""

    # --- session identity -------------------------------------------------
    source: str                       # 'claude_code' | 'codex' | 'opencode'
    native_session_id: str            # root conversation id

    # --- thread identity --------------------------------------------------
    # Codex threads are explicit (one log file each). Claude Code has only a
    # root thread in the log; its subagent threads are derived later from
    # Agent tool_use/tool_result pairs. Adapters set native_thread_id ==
    # native_session_id for a root thread.
    native_thread_id: str
    parent_native_thread_id: str | None = None
    is_subagent: bool = False
    agent_name: str | None = None

    # --- event ------------------------------------------------------------
    # native_event_id MUST be unique within its session; it is the dedup key.
    # Claude: the event `uuid`.  Codex: "<thread_id>:<ordinal>".
    # Neither available: ids.content_fallback_id(raw_line).
    native_event_id: str = ""
    ts_ms: int = 0                    # epoch milliseconds UTC
    ordinal: int | None = None        # native sequence number, when provided
    kind: EventKind = EventKind.OTHER
    model: str | None = None          # drop '<synthetic>' before emitting
    tool_name: str | None = None
    tool_use_id: str | None = None

    # --- token usage ------------------------------------------------------
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None

    # --- session metadata (may repeat on every event; ingest takes the
    #     value from the latest event, so worktree switches land correctly) --
    cwd: str | None = None
    git_branch: str | None = None
    cli_version: str | None = None

    # --- ingest bookkeeping ----------------------------------------------
    # Byte offset of the END of the line this event came from, so incremental
    # ingest can resume mid-file. Adapters must set this.
    #
    # A source that is not a text file has no honest offset to report and must
    # say so with 0 rather than guess: ingest then never believes it has
    # consumed the file and re-reads it in full every run, which dedup on
    # `native_event_id` makes free. See sources/opencode.py, a SQLite store.
    byte_end: int = 0


@runtime_checkable
class SourceAdapter(Protocol):
    """Implemented once per tool. See sources/claude_code.py, codex.py, opencode.py."""

    name: str

    def discover(self) -> Iterable[Path]:
        """All log files for this source on this machine, in any order."""
        ...

    def parse(self, path: Path, from_byte: int = 0) -> Iterator[RawEvent]:
        """Stream events from `path`, starting at byte offset `from_byte`.

        Must be resumable: starting at the `byte_end` of the last event
        previously ingested yields exactly the events after it.

        Must tolerate a truncated final line (the agent may be mid-write) by
        skipping it without raising -- the next run picks it up once complete.
        """
        ...
