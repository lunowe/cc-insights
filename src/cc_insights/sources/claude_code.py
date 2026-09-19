"""Claude Code source adapter: `~/.claude/projects/**/*.jsonl` -> RawEvent stream.

METADATA ONLY. Nothing here reads prompt text, tool arguments or file contents
into a RawEvent -- the frozen/slots contract in `base.py` makes that impossible
by construction, and this module does not try to work around it.

Four things about the Claude Code log shape drive this implementation, all of
them measured against the live corpus and reproducing
`docs/probes/canonical_metrics.py`, which is the spec:

1. **Sessionize on the `sessionId` field, never the filename.** Resuming a
   session or running it in a worktree writes its events into a *new* file, so
   one session legitimately spans several files. 36 events (`file-history-delta`)
   carry no `sessionId` at all; like the canonical probe we fall back to the
   file STEM -- the stem *is* a session id, whereas the basename (with the
   `.jsonl` suffix) minted 3 phantom sessions. The corpus holds 118 sessions.

2. **The dedup key is the event `uuid`.** Resume replays prior history verbatim
   into the new file: 1,295 (session, uuid) pairs occur twice with identical
   timestamp and type. Keying on anything ingest-assigned overcounts by ~2.6%.
   3,502 events (`queue-operation`, `pr-link`, `file-history-delta`) have no
   `uuid`; those hash their raw line via `ids.content_fallback_id`.

3. **Subagent threads ARE on disk, in nested files** -- an earlier draft of this
   adapter claimed otherwise and missed half the corpus. `projects/*/*.jsonl`
   holds only the root thread (`isSidechain` false, no `agentId`), but each
   session directory also carries
   `<session>/subagents/agent-<id>.jsonl` and, for workflows,
   `<session>/subagents/workflows/<wf-id>/agent-<id>.jsonl`: 387 files,
   63,614 timestamped events, ~52% of all Claude Code activity, with zero
   `uuid` overlap with the main transcripts. Every one of those events carries
   `isSidechain: true`, an `agentId` (the thread) and a `sessionId` (the parent
   session), so a subagent thread is READ off the log, not inferred. We key on
   `agentId` alone: it is present on every subagent event and on no main-
   transcript event, which makes the classification independent of the path.
   `subagents/workflows/<wf-id>/journal.jsonl` (8 files, 97 lines) is workflow
   bookkeeping -- `type`/`key`/`agentId` only, no `timestamp`, `uuid` or
   `sessionId` -- so it is dropped by the no-timestamp rule, exactly as the
   canonical probe drops it. WP5 no longer needs to pair `Agent` tool calls to
   discover threads, though tool pairing is still emitted (see 4).

4. **The subagent's type is on its assistant lines**, as `attributionAgent`
   ("general-purpose", "Explore", "workflow-subagent", ...), never on the
   opening user line. It is constant per `agentId` (checked: 0 of 371 agents
   carry two values), so it is read once from the head of the file rather than
   per event -- which also keeps `agent_name` stable when parsing resumes from
   a byte offset in the middle. 8 four-line transcripts record none; those get
   `agent_name=None`.

ONE EVENT PER MESSAGE LINE (the decision the contract asks us to document).
Applies to both file shapes.
Claude Code writes one JSON object per message and, measured over the whole
corpus, 21,276 of 21,277 assistant lines carry exactly one content block; the
multi-block lines are user turns with images alongside text. Emitting one event
per block would therefore only fragment single messages into synthetic events,
inflate event counts against the ground truth in FINDINGS, and force invented
ids. So: one RawEvent per line, its `kind` taken from the line's content blocks.
The single exception keeps tool pairing lossless -- if one message carries
*more than one* tool block, each extra block gets its own TOOL_USE/TOOL_RESULT
event keyed `"<uuid>#<block index>"` so WP5 can still pair every `Agent` call.
Those extra events carry no token usage, so nothing is double-counted. Exactly
two lines in the corpus hit this path, both subagent assistant turns issuing
two parallel `Bash` calls; they are why this adapter counts 2 events more than
the canonical probe, which counts lines. Everything else matches key for key.
"""

from __future__ import annotations

import glob as _glob
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from cc_insights import ids
from cc_insights.config import DEFAULT_SOURCE_GLOBS, Config
from cc_insights.sources.base import EventKind, RawEvent

SOURCE = "claude_code"

#: Claude Code writes this in place of a model name on locally generated
#: assistant turns. It is not a model and must never reach a model or cost
#: breakdown, so it is normalized to None. See FINDINGS section 4.1.
SYNTHETIC_MODEL = "<synthetic>"

#: Line `type`s that are meta rather than conversation. Everything not listed
#: here and not a user/assistant message falls through to OTHER.
_SYSTEM_TYPES = frozenset({"system", "mode", "permission-mode", "hook", "hook-result"})

_TOOL_BLOCK_KINDS = {"tool_use": EventKind.TOOL_USE, "tool_result": EventKind.TOOL_RESULT}

#: Field naming the subagent's configured type. Only assistant lines carry it,
#: so it is sniffed from the head of the transcript -- see `_agent_name`.
AGENT_NAME_FIELD = "attributionAgent"

#: Bounds on that head sniff. Measured over the corpus, the field first appears
#: on line 4-11 of every subagent transcript that records it at all; the budget
#: is generous so a future format change degrades to `agent_name=None` rather
#: than to an unbounded read.
_HEAD_LINES = 64
_HEAD_BYTES = 4 * 1024 * 1024


def _to_ms(raw: Any) -> int | None:
    """ISO-8601 (`...Z`) -> epoch milliseconds UTC. None when unparseable."""
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:  # Claude Code always writes Z, but do not assume local
        dt = dt.replace(tzinfo=timezone.utc)
    return round(dt.timestamp() * 1000)


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


class ClaudeCodeAdapter:
    """SourceAdapter for Claude Code JSONL transcripts."""

    name = SOURCE

    def __init__(
        self,
        config: Config | None = None,
        *,
        globs: Sequence[str | Path] | None = None,
    ) -> None:
        """`globs` (tests point it at a fixture tree) wins over `config`, which
        wins over the built-in defaults -- the main transcripts *and* the
        nested `subagents/**` ones."""
        if globs is not None:
            patterns = [str(g) for g in globs]
        elif config is not None:
            patterns = [str(p) for p in config.source_globs.get(SOURCE, [])]
        else:
            patterns = list(DEFAULT_SOURCE_GLOBS[SOURCE])
        self.globs: list[str] = patterns
        #: realpath -> subagent type, for transcripts where the sniff found one.
        #: Only hits are cached: a miss is one cheap head read per parse call,
        #: and re-reading it means a transcript that grows an `attributionAgent`
        #: after its first few lines is still picked up on the next run.
        self._agent_names: dict[str, str] = {}

    # -- discovery ---------------------------------------------------------
    def discover(self) -> Iterable[Path]:
        """Every Claude Code transcript on this machine, sorted for determinism."""
        found: dict[str, Path] = {}
        for pattern in self.globs:
            expanded = os.path.expanduser(str(pattern))
            for hit in _glob.glob(expanded, recursive=True):
                if os.path.isfile(hit):
                    found.setdefault(os.path.realpath(hit), Path(hit))
        return sorted(found.values())

    # -- parsing -----------------------------------------------------------
    def parse(self, path: Path, from_byte: int = 0) -> Iterator[RawEvent]:
        """Stream events from `path` starting at `from_byte`.

        `from_byte` is expected to be the `byte_end` of a previously yielded
        event, i.e. a line boundary; parsing then resumes with exactly the
        events after it. A trailing line without its newline is a write in
        progress: it is parsed only if it is already valid JSON, and otherwise
        skipped silently so the next run picks it up once complete.

        Works the same for a main transcript and for a `subagents/**` one; the
        events themselves say which they are.
        """
        path = Path(path)
        agent_name: str | None = None
        sniffed = False
        with path.open("rb") as handle:
            if from_byte:
                handle.seek(from_byte)
            offset = handle.tell()
            for chunk in handle:
                offset += len(chunk)
                text = chunk.decode("utf-8", "replace").rstrip("\r\n")
                if not text.strip():
                    continue
                try:
                    doc = json.loads(text)
                except (ValueError, RecursionError):
                    continue  # corrupt or half-written line: skip, never raise
                if not isinstance(doc, dict):
                    continue
                # Sniffed lazily (never for a main transcript) and always from
                # byte 0, so a resumed parse names the agent the same way.
                if not sniffed and _str_or_none(doc.get("agentId")):
                    agent_name, sniffed = self._agent_name(path), True
                yield from self._events(doc, text, path, offset, agent_name)

    def _agent_name(self, path: Path) -> str | None:
        """The subagent type recorded in this transcript's head, or None.

        Reads at most `_HEAD_LINES` lines / `_HEAD_BYTES` bytes from the start
        of the file, independently of any resume offset.
        """
        key = os.path.realpath(path)
        cached = self._agent_names.get(key)
        if cached is not None:
            return cached
        try:
            with open(key, "rb") as handle:
                read = 0
                for seen, chunk in enumerate(handle):
                    if seen >= _HEAD_LINES or read >= _HEAD_BYTES:
                        break
                    read += len(chunk)
                    try:
                        doc = json.loads(chunk.decode("utf-8", "replace"))
                    except (ValueError, RecursionError):
                        continue
                    if not isinstance(doc, dict):
                        continue
                    name = _str_or_none(doc.get(AGENT_NAME_FIELD))
                    if name:
                        self._agent_names[key] = name
                        return name
        except OSError:
            pass
        return None

    # -- mapping -----------------------------------------------------------
    def _events(
        self,
        doc: dict[str, Any],
        raw_line: str,
        path: Path,
        byte_end: int,
        agent_name: str | None = None,
    ) -> Iterator[RawEvent]:
        ts_ms = _to_ms(doc.get("timestamp"))
        if ts_ms is None:
            return  # no timestamp -> no place on a timeline; drops journal.jsonl

        # Session identity comes from the FIELD. The file stem (a session id in
        # its own right) is only a last resort for the handful of events that
        # carry no sessionId.
        session = _str_or_none(doc.get("sessionId")) or path.stem
        # `agentId` is present on every subagent event and on no main-transcript
        # event, so it alone decides root thread vs. subagent thread.
        agent_id = _str_or_none(doc.get("agentId"))
        native_event_id = _str_or_none(doc.get("uuid")) or ids.content_fallback_id(raw_line)

        line_type = doc.get("type")
        message = doc.get("message") if isinstance(doc.get("message"), dict) else None
        blocks = message.get("content") if message else None
        tool_blocks = (
            [
                (i, b)
                for i, b in enumerate(blocks)
                if isinstance(b, dict) and b.get("type") in _TOOL_BLOCK_KINDS
            ]
            if isinstance(blocks, list)
            else []
        )

        if tool_blocks:
            kind = _TOOL_BLOCK_KINDS[tool_blocks[0][1]["type"]]
        elif line_type == "assistant":
            kind = EventKind.ASSISTANT
        elif line_type == "user":
            kind = EventKind.USER_PROMPT
        elif line_type in _SYSTEM_TYPES:
            kind = EventKind.SYSTEM
        else:
            kind = EventKind.OTHER

        model = _str_or_none(message.get("model")) if message else None
        if model == SYNTHETIC_MODEL:
            model = None

        usage = message.get("usage") if message else None
        if not isinstance(usage, dict):
            usage = {}

        common = dict(
            source=SOURCE,
            native_session_id=session,
            # Root thread: thread == session, no parent. Subagent thread: the
            # `agentId`, parented on the root thread (whose id IS the session
            # id), with the type sniffed from the transcript head.
            native_thread_id=agent_id or session,
            parent_native_thread_id=session if agent_id else None,
            is_subagent=agent_id is not None,
            agent_name=agent_name if agent_id else None,
            ts_ms=ts_ms,
            ordinal=None,  # Claude Code provides no native sequence number
            model=model,
            cwd=_str_or_none(doc.get("cwd")),
            git_branch=_str_or_none(doc.get("gitBranch")),
            cli_version=_str_or_none(doc.get("version")),
            byte_end=byte_end,
        )

        tool_name, tool_use_id = (
            _tool_fields(tool_blocks[0][1]) if tool_blocks else (None, None)
        )
        yield RawEvent(
            native_event_id=native_event_id,
            kind=kind,
            tool_name=tool_name,
            tool_use_id=tool_use_id,
            input_tokens=_int_or_none(usage.get("input_tokens")),
            output_tokens=_int_or_none(usage.get("output_tokens")),
            cache_read_tokens=_int_or_none(usage.get("cache_read_input_tokens")),
            cache_write_tokens=_int_or_none(usage.get("cache_creation_input_tokens")),
            **common,
        )

        # Extra tool blocks on the same message (never seen in the corpus, but
        # losing one would lose a subagent for WP5). Suffixed id keeps the
        # dedup key unique; no usage, so tokens are never counted twice.
        for index, block in tool_blocks[1:]:
            name, use_id = _tool_fields(block)
            yield RawEvent(
                native_event_id=f"{native_event_id}#{index}",
                kind=_TOOL_BLOCK_KINDS[block["type"]],
                tool_name=name,
                tool_use_id=use_id,
                **common,
            )


def _tool_fields(block: dict[str, Any]) -> tuple[str | None, str | None]:
    """(tool_name, tool_use_id) for one content block.

    `tool_use` carries its own `id` and `name`; `tool_result` points back at the
    call through `tool_use_id`. Both sides are needed so WP5 can pair them.
    """
    if block.get("type") == "tool_use":
        return _str_or_none(block.get("name")), _str_or_none(block.get("id"))
    return None, _str_or_none(block.get("tool_use_id"))
