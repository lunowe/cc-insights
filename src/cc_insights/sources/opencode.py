"""opencode source adapter: `~/.local/share/opencode/opencode.db` -> RawEvent stream.

METADATA ONLY, and here that rule needs teeth rather than trust. Unlike the two
JSONL sources, opencode's store is *full* of content: `message.data` for a user
turn embeds `summary.diffs` with the complete before-text of every file touched,
and `part.data` holds prompts, tool arguments, command output and reasoning
traces. This adapter reads five scalar paths out of that JSON (`role`, `time`,
`modelID`, `tokens`, `path.cwd`) and nothing else. The frozen/slots `RawEvent`
means a slip cannot reach the database, but the slip must not happen here
either: never widen the `_message_event` / `_tool_events` field lists.

Four things about opencode's shape drive this implementation.

1. **The source is a SQLite database, not a log file.** opencode 1.18 keeps
   everything in one `opencode.db` (WAL) with `session` / `message` / `part`
   tables, each row's payload a JSON blob in a `data` column. Older releases
   wrote `storage/**.json`; that layout is gone and is not supported here.
   The database is opened `mode=ro` so a bug in this module cannot write to
   the user's live opencode state.

2. **`byte_end` is 0 on every event, deliberately.** The contract's incremental
   resume is a byte offset into a text file, and there is no honest byte offset
   into a B-tree. Worse, a WAL database is written to `opencode.db-wal` and
   only folded back on checkpoint, so `opencode.db`'s size and mtime can sit
   unchanged for hours while sessions accumulate -- any watermark keyed on them
   would silently skip real work. Reporting 0 makes `ingest._plan_file` see a
   file it has not accounted for, so the database is re-read in full on every
   run and dedup on `native_event_id` collapses what was already stored. That
   is affordable because the store is small (a heavy user's is a few thousand
   rows, parsed in well under a second) and it is *correct*, which the
   alternatives are not. One wart follows honestly from it: `ingest_file`'s
   `lines_read` counts runs rather than rows for this source.

3. **A subagent is a session, not a thread.** The `task` tool spawns a child
   `session` row carrying `parent_id`; `agent` names its type ("general",
   "explore", ...). So a root session's `id` is the `native_session_id` for
   itself *and* for its children, while each row's own id is the
   `native_thread_id`. That is the same shape Codex has, reached by a different
   route, and it is why one timeline can render all three sources.

4. **Two rows carry two timestamps each, and both are real.** A tool part
   records `state.time.start` and `state.time.end`; an assistant message
   records `time.created` and `time.completed`. Emitting only the first of each
   would end a session at the moment its last turn *began* and lose the
   generation itself -- on this corpus that is minutes per session. So those
   rows yield a start event and an end event, keyed `<row id>` and
   `<row id>:done`. Only the start event carries token usage, so nothing is
   counted twice. `reasoning`, `text`, `step-start` and `step-finish` parts add
   no timestamp outside a window already bracketed this way and are dropped.

**Model names are stored bare** (`gpt-5.3-codex`), not provider-qualified, so a
model reads the same whether it ran through opencode or the Codex CLI. The
free-tier routes name themselves (`muse-spark-1.3-contributor-free`), which is
what keeps them distinct where it matters -- pricing.

**opencode's own `cost` column is ignored.** It is 0 for every message in the
measured corpus (subscription and free-tier routes report no per-call price),
and a cost that is sometimes the provider's and sometimes ours would be
impossible to read. Cost is derived from token counts and one pricing table for
every source. See `pricing.py`.
"""

from __future__ import annotations

import glob as _glob
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from cc_insights.config import Config, default_source_globs, expand_glob
from cc_insights.sources.base import EventKind, RawEvent

log = logging.getLogger(__name__)

SOURCE = "opencode"

#: A tool call that has not returned yet yields its TOOL_USE but no
#: TOOL_RESULT: the end timestamp does not exist yet, and inventing one would
#: extend a span to now. The next run picks up the result once it lands.
_UNFINISHED_TOOL_STATES = frozenset({"pending", "running"})

#: Guards a `parent_id` cycle. opencode has no depth limit and a corrupted row
#: must not spin forever.
_MAX_PARENT_DEPTH = 32


def _loads(raw: Any) -> dict[str, Any]:
    """Parse a `data` column. A row we cannot read is skipped, never fatal."""
    if not isinstance(raw, (str, bytes)):
        return {}
    try:
        doc = json.loads(raw)
    except (ValueError, RecursionError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class _Session:
    """The session columns this adapter needs, plus its resolved root."""

    __slots__ = ("id", "parent_id", "root_id", "cwd", "cli_version", "agent")

    def __init__(self, row: sqlite3.Row) -> None:
        self.id: str = row["id"]
        self.parent_id: str | None = _str_or_none(row["parent_id"])
        self.root_id: str = self.id
        self.cwd: str | None = _str_or_none(row["directory"])
        self.cli_version: str | None = _str_or_none(row["version"])
        self.agent: str | None = _str_or_none(row["agent"])

    @property
    def is_subagent(self) -> bool:
        return self.parent_id is not None


class OpencodeAdapter:
    """SourceAdapter for the opencode SQLite store."""

    name = SOURCE

    def __init__(
        self,
        config: Config | None = None,
        *,
        globs: Sequence[str | Path] | None = None,
    ) -> None:
        """`globs` (tests point it at a fixture database) wins over `config`,
        which wins over the built-in default."""
        if globs is not None:
            patterns = [str(g) for g in globs]
        elif config is not None:
            patterns = [str(p) for p in config.source_globs.get(SOURCE, [])]
        else:
            patterns = list(default_source_globs()[SOURCE])
        self.globs: list[str] = patterns

    # -- discovery ---------------------------------------------------------
    def discover(self) -> Iterable[Path]:
        """Every opencode store on this machine, sorted for determinism.

        The `-wal` and `-shm` sidecars are deliberately not matched: they are
        read through the database handle, not separately.
        """
        found: dict[str, Path] = {}
        for pattern in self.globs:
            expanded = expand_glob(pattern)
            for hit in _glob.glob(expanded, recursive=True):
                if os.path.isfile(hit):
                    found.setdefault(os.path.realpath(hit), Path(hit))
        return sorted(found.values())

    # -- parsing -----------------------------------------------------------
    def parse(self, path: Path, from_byte: int = 0) -> Iterator[RawEvent]:
        """Stream every event in the store.

        `from_byte` is accepted for the contract and ignored -- see point 2 in
        the module docstring. Every event reports `byte_end = 0`, so ingest
        never believes it has consumed this file and always re-reads it.

        Rows are streamed, not loaded: a store with a million parts costs
        constant memory here. Messages come first, then tool parts; ingest does
        not require events in time order, and both passes are ordered by
        session so the output is deterministic.
        """
        conn = _open_readonly(Path(path))
        try:
            sessions = _load_sessions(conn)
            if not sessions:
                return
            yield from _message_events(conn, sessions)
            yield from _tool_part_events(conn, sessions)
        finally:
            conn.close()


# --------------------------------------------------------------------------
# reading the store
# --------------------------------------------------------------------------


def _open_readonly(path: Path) -> sqlite3.Connection:
    """A read-only handle on the store, WAL content included.

    `mode=ro` still needs to map the `-shm` file to see committed WAL frames.
    Where that is impossible (a read-only directory, another user's store) the
    fallback is `immutable=1`, which reads the main database alone: everything
    since the last checkpoint is invisible, which is a loss worth naming in the
    log rather than an error worth aborting the whole ingest run for.
    """
    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.OperationalError:
        conn = sqlite3.connect(f"{uri}&immutable=1", uri=True, timeout=5.0)
        log.warning(
            "%s: opened immutable; sessions written since the last WAL "
            "checkpoint are not visible to this run",
            path,
        )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _load_sessions(conn: sqlite3.Connection) -> dict[str, _Session]:
    """Every session, each with its root resolved by walking `parent_id`.

    A child whose parent is missing (the parent was deleted, or this is a
    partial store) is its own root: that renders as a root thread of one rather
    than attaching the events to a session that does not exist.
    """
    try:
        rows = conn.execute(
            "SELECT id, parent_id, directory, version, agent FROM session"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise ValueError(f"not an opencode store: {exc}") from exc

    sessions = {row["id"]: _Session(row) for row in rows}
    for session in sessions.values():
        seen = {session.id}
        current = session
        for _ in range(_MAX_PARENT_DEPTH):
            parent = sessions.get(current.parent_id) if current.parent_id else None
            if parent is None or parent.id in seen:
                break
            seen.add(parent.id)
            current = parent
        session.root_id = current.id
    return sessions


def _message_events(
    conn: sqlite3.Connection, sessions: dict[str, _Session]
) -> Iterator[RawEvent]:
    """USER_PROMPT / ASSISTANT events, one or two per `message` row."""
    cursor = conn.execute(
        "SELECT id, session_id, data FROM message ORDER BY session_id, time_created"
    )
    for row in cursor:
        session = sessions.get(row["session_id"])
        if session is None:
            continue  # orphaned by a deleted session; nothing to attribute it to
        data = _loads(row["data"])
        role = _str_or_none(data.get("role"))
        time = _dict(data.get("time"))
        created = _int_or_none(time.get("created"))
        if created is None:
            continue  # no timestamp, no place on a timeline

        if role == "user":
            yield _event(
                session,
                native_event_id=row["id"],
                ts_ms=created,
                kind=EventKind.USER_PROMPT,
            )
            continue

        tokens = _dict(data.get("tokens"))
        cache = _dict(tokens.get("cache"))
        yield _event(
            session,
            native_event_id=row["id"],
            ts_ms=created,
            kind=EventKind.ASSISTANT if role == "assistant" else EventKind.OTHER,
            model=_str_or_none(data.get("modelID")),
            cwd=_str_or_none(_dict(data.get("path")).get("cwd")),
            # opencode reports `input` net of cache, like Claude Code and
            # unlike Codex, so the contract's fields map straight across.
            # `reasoning` is a subset of `output` and is not added again.
            input_tokens=_int_or_none(tokens.get("input")),
            output_tokens=_int_or_none(tokens.get("output")),
            cache_read_tokens=_int_or_none(cache.get("read")),
            cache_write_tokens=_int_or_none(cache.get("write")),
        )

        completed = _int_or_none(time.get("completed"))
        if completed is not None and completed > created:
            yield _event(
                session,
                native_event_id=f"{row['id']}:done",
                ts_ms=completed,
                kind=EventKind.ASSISTANT if role == "assistant" else EventKind.OTHER,
                model=_str_or_none(data.get("modelID")),
                cwd=_str_or_none(_dict(data.get("path")).get("cwd")),
            )


def _tool_part_events(
    conn: sqlite3.Connection, sessions: dict[str, _Session]
) -> Iterator[RawEvent]:
    """TOOL_USE / TOOL_RESULT events, from `part` rows of type `tool`.

    Filtering on `json_extract` in SQL rather than in Python keeps the 70% of
    parts that are reasoning and step bookkeeping out of this process entirely.
    """
    cursor = conn.execute(
        "SELECT id, session_id, data FROM part "
        "WHERE json_extract(data, '$.type') = 'tool' "
        "ORDER BY session_id, time_created"
    )
    for row in cursor:
        session = sessions.get(row["session_id"])
        if session is None:
            continue
        data = _loads(row["data"])
        state = _dict(data.get("state"))
        time = _dict(state.get("time"))
        start = _int_or_none(time.get("start"))
        if start is None:
            continue

        tool_name = _str_or_none(data.get("tool"))
        call_id = _str_or_none(data.get("callID"))
        yield _event(
            session,
            native_event_id=row["id"],
            ts_ms=start,
            kind=EventKind.TOOL_USE,
            tool_name=tool_name,
            tool_use_id=call_id,
        )

        end = _int_or_none(time.get("end"))
        if end is None or _str_or_none(state.get("status")) in _UNFINISHED_TOOL_STATES:
            continue
        yield _event(
            session,
            native_event_id=f"{row['id']}:done",
            ts_ms=max(end, start),
            kind=EventKind.TOOL_RESULT,
            tool_name=tool_name,
            tool_use_id=call_id,
        )


def _event(session: _Session, **fields: Any) -> RawEvent:
    """A RawEvent with this session's identity filled in.

    `cwd` defaults to the session's directory; a message that names its own
    (worktree switches show up here) overrides it.
    """
    fields.setdefault("cwd", None)
    return RawEvent(
        source=SOURCE,
        native_session_id=session.root_id,
        native_thread_id=session.id,
        parent_native_thread_id=session.parent_id,
        is_subagent=session.is_subagent,
        agent_name=session.agent if session.is_subagent else None,
        cli_version=session.cli_version,
        byte_end=0,
        **{**fields, "cwd": fields["cwd"] or session.cwd},
    )


__all__ = ["SOURCE", "OpencodeAdapter"]
