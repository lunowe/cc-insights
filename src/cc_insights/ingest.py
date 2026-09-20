"""Ingest: drive the source adapters and upsert into the relational tables.

This module is the ONLY writer of `project`, `session`, `thread` and `event`.
Adapters know nothing about SQL; ingest knows nothing about log formats. WP5
owns `span` and `active_ms` and this module never touches either.

METADATA ONLY. Everything written here comes out of a `RawEvent`, which is a
frozen/slots dataclass with no free-form field, so prompt text, tool arguments
and file contents cannot reach the database through this path.

Decisions this module makes, and why
====================================

**Ids are never invented.** Every id comes from `ids.py`:
`session_id(host_id, source, native_id)`, `thread_id(session_id, native_id)`,
`event_id(session_id, native_event_id)`, `project_id(root_path)`. They are
hashes of natural keys, which is what makes a re-run a no-op rather than a
duplicate.

**Conflict semantics.**

* `event` -> ``ON CONFLICT DO NOTHING``. `native_event_id` is the dedup key and
  a conflict on it means *the same logical event*, not a newer version of it:
  Claude Code replays prior history verbatim into a resumed session's new file
  (1,295 (session, uuid) pairs, identical timestamp and type -- FINDINGS §4),
  and Codex never replays at all. So there is nothing to update; `DO UPDATE`
  would rewrite byte-identical values on every run. `DO NOTHING` is also what
  makes idempotency *observable*: the number of rows SQLite reports as changed
  by the event insert is exactly the number of genuinely new events, which is
  what `IngestStats.events_inserted` reports and what the idempotency test
  asserts is 0 on a second run. Consequence, stated plainly: if an adapter's
  field mapping is later corrected, existing event rows are NOT rewritten --
  rebuild the database instead (a full cold ingest is a couple of minutes).
* `project` -> ``ON CONFLICT DO NOTHING``. `project_id` is `hash(root_path)`
  and `name` is derived from `root_path`, so the row is a pure function of its
  own key. There is never anything to update.
* `session`, `thread` -> ``ON CONFLICT ... DO UPDATE``. These rows carry
  aggregates (`started_at`, `ended_at`, `event_count`) and mutable metadata
  (`cwd`, `git_branch`, `cli_version`) that genuinely change as more of the log
  is read, so they must be refreshed.
* `ingest_file` -> ``ON CONFLICT ... DO UPDATE``: it is pure bookkeeping.

**Aggregates are recomputed from the `event` table, not accumulated.** After
every run, `started_at` / `ended_at` / `event_count` for each touched session
and thread are re-derived with `MIN(ts)` / `MAX(ts)` / `COUNT(*)`. This is the
only formulation that is correct for a *resumed* ingest (where the run sees
just the tail of a session), for a session whose events are spread over several
files, and after dedup collapses replayed events. `active_ms` stays at its
default 0 -- WP5 owns it.

**`cwd` / `git_branch` / `cli_version` take the latest event that carries
one.** "Latest" means highest `ts`, tracked across the whole run, because files
are discovered in path order, which is not time order: a session resumed into a
second file can be parsed before the first. A run that sees no value for a
field leaves the stored one alone (`COALESCE`), so a resumed ingest does not
blank out metadata it simply did not re-observe.

**The `project` root rule: the session's latest `cwd`, lexically normalized,
as-is.** `root_path = os.path.normpath(cwd)` (collapses `//`, `.` and `..` and
strips a trailing slash); `name = basename(root_path)`. No filesystem access,
no git-root walk, no symlink resolution, no `~` expansion. The rule has to be a
pure function of the logged string because `project_id` is a hash of it: a
git-root walk would return a different answer once the repo is moved, deleted
or ingested on another machine, and would therefore mint a second project row
for history already recorded. A session with no `cwd` anywhere gets
`project_id = NULL`, which the schema allows.

**Thread parent links are resolved at the END of the run.** `parent_thread_id`
is a self-referencing FK and `PRAGMA foreign_keys = ON`, so a child cannot be
inserted before its parent. A Claude subagent transcript and its main
transcript are different files, and a Codex subagent thread's parent is another
file again -- neither ordering is guaranteed by `discover()`. So every thread
is inserted with `parent_thread_id = NULL` and the links are applied in one
final pass, guarded by an `EXISTS` on the parent row. A subagent whose parent
thread is not in the corpus at all (the main transcript aged out of Claude's
rolling window) keeps `parent_thread_id = NULL` rather than acquiring an
invented parent. Known limitation: if the parent file only appears in a *later*
run and the child file gets no new bytes in that run, the link stays NULL until
the child file next grows; a full re-ingest into a fresh database always
resolves everything resolvable.

**Incremental resume.** `ingest_file` stores `bytes_read` (the `byte_end` of
the last event taken from the file), `size_bytes` and `mtime_ms`. A file whose
size and mtime are unchanged and whose `bytes_read` already covers it is
skipped without being opened. A file that SHRANK was rotated or rewritten, so
it restarts from byte 0 -- dedup on `native_event_id` makes that harmless.
Otherwise parsing resumes at `bytes_read`.

`bytes_read` is only ever advanced to a `byte_end` an adapter actually
reported, never to the file size, because ingest does not read the file itself
and must not claim bytes it cannot account for. A file whose tail is lines the
adapters drop (no `timestamp`: `journal.jsonl`, some `file-history-delta`
records) therefore keeps `bytes_read < size_bytes` and is re-opened on every
run -- but only its short unaccounted tail is re-read, which on the real corpus
costs about 40 ms across ~120 such files.

**One bad file cannot abort a run.** Each file is processed inside its own
transaction; any exception rolls that file back, is logged and recorded in
`IngestStats.errors`, and the run continues with the next file.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence, TypeVar

from cc_insights import db, ids
from cc_insights.config import Config
from cc_insights.sources.base import RawEvent, SourceAdapter
from cc_insights.sources.claude_code import ClaudeCodeAdapter
from cc_insights.sources.codex import CodexAdapter
from cc_insights.sources.opencode import OpencodeAdapter

log = logging.getLogger(__name__)

#: Events buffered before a write. Bounds memory on the 55 MB transcripts in
#: the real corpus without making the insert path chatty.
EVENT_CHUNK = 5_000

#: Ids per `IN (...)` list when re-deriving aggregates.
_ID_CHUNK = 400

#: source name -> adapter class. Adding a source means adding a line here.
ADAPTERS: dict[str, type] = {
    ClaudeCodeAdapter.name: ClaudeCodeAdapter,
    CodexAdapter.name: CodexAdapter,
    OpencodeAdapter.name: OpencodeAdapter,
}

T = TypeVar("T")


# --------------------------------------------------------------------------
# project root rule
# --------------------------------------------------------------------------


def project_root(cwd: str | None) -> str | None:
    """The `project.root_path` for a session whose latest `cwd` is `cwd`.

    Lexical normalization only -- see the module docstring for why this must
    not touch the filesystem. Returns None when there is no usable cwd.
    """
    if not isinstance(cwd, str):
        return None
    text = cwd.strip()
    if not text:
        return None
    root = os.path.normpath(text)
    return root or None


def project_name(root_path: str) -> str:
    """Display name for a project root: its last path segment."""
    return os.path.basename(root_path.rstrip("/")) or root_path


# --------------------------------------------------------------------------
# stats
# --------------------------------------------------------------------------


@dataclass(slots=True)
class IngestStats:
    """What one `ingest()` call did. Counts are rows, not bytes."""

    files_discovered: int = 0
    files_ingested: int = 0      # opened and parsed
    files_skipped: int = 0       # unchanged since the last run
    files_failed: int = 0
    events_read: int = 0         # yielded by the adapters
    events_inserted: int = 0     # genuinely new rows; 0 on a clean re-run
    sessions_touched: int = 0
    threads_touched: int = 0
    #: The sessions this run actually wrote to, so a caller can re-derive just
    #: those instead of the corpus. `cci watch` lives on this: a scoped derive
    #: and re-price is two hundredths of a second where the full pass is two.
    session_ids: set[str] = field(default_factory=set)
    projects_written: int = 0
    bytes_read: int = 0          # newly consumed this run
    duration_s: float = 0.0
    errors: list[tuple[str, str]] = field(default_factory=list)  # (path, repr)

    def as_dict(self) -> dict[str, object]:
        return {
            "files_discovered": self.files_discovered,
            "files_ingested": self.files_ingested,
            "files_skipped": self.files_skipped,
            "files_failed": self.files_failed,
            "events_read": self.events_read,
            "events_inserted": self.events_inserted,
            "sessions_touched": self.sessions_touched,
            "threads_touched": self.threads_touched,
            "projects_written": self.projects_written,
            "bytes_read": self.bytes_read,
            "duration_s": round(self.duration_s, 3),
            "errors": len(self.errors),
        }


# --------------------------------------------------------------------------
# run-scoped accumulators
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Latest:
    """The value carried by the highest-`ts` event that carried one."""

    ts: int = -1
    value: str | None = None

    def offer(self, ts: int, value: str | None) -> None:
        if value is not None and ts >= self.ts:
            self.ts = ts
            self.value = value


@dataclass(slots=True)
class _SessionAcc:
    id: str
    native_id: str
    source: str
    started_at: int
    ended_at: int
    cwd: _Latest = field(default_factory=_Latest)
    git_branch: _Latest = field(default_factory=_Latest)
    cli_version: _Latest = field(default_factory=_Latest)


@dataclass(slots=True)
class _ThreadAcc:
    id: str
    native_id: str
    session_id: str
    started_at: int
    ended_at: int
    is_subagent: bool = False
    #: native id of the parent thread, resolved to a row id after the run.
    parent_native_id: str | None = None
    agent_name: _Latest = field(default_factory=_Latest)


# --------------------------------------------------------------------------
# SQL
# --------------------------------------------------------------------------

_INSERT_PROJECT = """
INSERT INTO project (project_id, root_path, name)
VALUES (:project_id, :root_path, :name)
ON CONFLICT DO NOTHING
"""

# Placeholder aggregates: the final pass re-derives them from `event`.
_INSERT_SESSION = """
INSERT INTO session (
    id, native_id, source, host_id, project_id,
    cwd, git_branch, cli_version,
    started_at, ended_at, event_count, active_ms
) VALUES (
    :id, :native_id, :source, :host_id, NULL,
    NULL, NULL, NULL,
    :started_at, :ended_at, 0, 0
)
ON CONFLICT DO NOTHING
"""

_INSERT_THREAD = """
INSERT INTO thread (
    id, native_id, session_id, parent_thread_id, is_subagent, agent_name,
    started_at, ended_at, event_count, active_ms
) VALUES (
    :id, :native_id, :session_id, NULL, :is_subagent, NULL,
    :started_at, :ended_at, 0, 0
)
ON CONFLICT DO NOTHING
"""

_INSERT_EVENT = """
INSERT INTO event (
    id, session_id, thread_id, native_event_id, ts, ordinal, kind,
    model, tool_name, tool_use_id,
    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT DO NOTHING
"""

_UPDATE_SESSION = """
UPDATE session SET
    project_id  = COALESCE(:project_id, project_id),
    cwd         = COALESCE(:cwd, cwd),
    git_branch  = COALESCE(:git_branch, git_branch),
    cli_version = COALESCE(:cli_version, cli_version),
    started_at  = :started_at,
    ended_at    = :ended_at,
    event_count = :event_count
WHERE id = :id
"""

_UPDATE_THREAD = """
UPDATE thread SET
    agent_name  = COALESCE(:agent_name, agent_name),
    is_subagent = :is_subagent,
    started_at  = :started_at,
    ended_at    = :ended_at,
    event_count = :event_count
WHERE id = :id
"""

# Guarded so a missing parent leaves NULL instead of breaking the self-FK, and
# so a thread can never become its own parent.
_LINK_PARENT = """
UPDATE thread SET parent_thread_id = :parent_id
WHERE id = :id
  AND :parent_id <> id
  AND EXISTS (SELECT 1 FROM thread WHERE id = :parent_id)
"""

_UPSERT_INGEST_FILE = """
INSERT INTO ingest_file (
    host_id, path, source, size_bytes, mtime_ms, bytes_read, lines_read, last_ingest
) VALUES (
    :host_id, :path, :source, :size_bytes, :mtime_ms, :bytes_read, :lines_read, :last_ingest
)
ON CONFLICT (host_id, path) DO UPDATE SET
    source      = excluded.source,
    size_bytes  = excluded.size_bytes,
    mtime_ms    = excluded.mtime_ms,
    bytes_read  = excluded.bytes_read,
    lines_read  = excluded.lines_read,
    last_ingest = excluded.last_ingest
"""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _chunked(items: Iterable[T], size: int) -> Iterator[list[T]]:
    buf: list[T] = []
    for item in items:
        buf.append(item)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


def build_adapters(
    config: Config, sources: Sequence[str] | None = None
) -> list[SourceAdapter]:
    """One adapter instance per requested source, wired to `config`'s globs."""
    names = list(sources) if sources is not None else list(ADAPTERS)
    unknown = [n for n in names if n not in ADAPTERS]
    if unknown:
        raise ValueError(f"unknown source(s): {unknown}; known: {sorted(ADAPTERS)}")
    return [ADAPTERS[name](config) for name in names]


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------


class _Run:
    """One `ingest()` call. Holds the run-scoped accumulators."""

    def __init__(self, conn: sqlite3.Connection, config: Config) -> None:
        self.conn = conn
        self.config = config
        self.host_id = config.host_id
        self.stats = IngestStats()
        self.sessions: dict[str, _SessionAcc] = {}
        self.threads: dict[str, _ThreadAcc] = {}
        self._ensured_sessions: set[str] = set()
        self._ensured_threads: set[str] = set()
        # Ids this file introduced, so a rolled-back file does not leave the
        # run believing rows exist that were never committed.
        self._file_sessions: set[str] = set()
        self._file_threads: set[str] = set()

    # -- per-file transaction bookkeeping --------------------------------

    def begin_file(self) -> None:
        self._file_sessions = set()
        self._file_threads = set()

    def rollback_file(self) -> None:
        """Forget everything the (rolled back) file introduced."""
        for sid in self._file_sessions:
            self.sessions.pop(sid, None)
            self._ensured_sessions.discard(sid)
        for tid in self._file_threads:
            self.threads.pop(tid, None)
            self._ensured_threads.discard(tid)
        self._file_sessions = set()
        self._file_threads = set()

    # -- accumulate ------------------------------------------------------

    def _absorb(self, ev: RawEvent) -> tuple[str, str]:
        """Fold one event into the session/thread accumulators.

        Returns `(session_row_id, thread_row_id)`.
        """
        sid = ids.session_id(self.host_id, ev.source, ev.native_session_id)
        sess = self.sessions.get(sid)
        if sess is None:
            sess = self.sessions[sid] = _SessionAcc(
                id=sid,
                native_id=ev.native_session_id,
                source=ev.source,
                started_at=ev.ts_ms,
                ended_at=ev.ts_ms,
            )
            self._file_sessions.add(sid)
        else:
            if ev.ts_ms < sess.started_at:
                sess.started_at = ev.ts_ms
            if ev.ts_ms > sess.ended_at:
                sess.ended_at = ev.ts_ms
        sess.cwd.offer(ev.ts_ms, ev.cwd)
        sess.git_branch.offer(ev.ts_ms, ev.git_branch)
        sess.cli_version.offer(ev.ts_ms, ev.cli_version)

        tid = ids.thread_id(sid, ev.native_thread_id)
        thread = self.threads.get(tid)
        if thread is None:
            thread = self.threads[tid] = _ThreadAcc(
                id=tid,
                native_id=ev.native_thread_id,
                session_id=sid,
                started_at=ev.ts_ms,
                ended_at=ev.ts_ms,
            )
            self._file_threads.add(tid)
        else:
            if ev.ts_ms < thread.started_at:
                thread.started_at = ev.ts_ms
            if ev.ts_ms > thread.ended_at:
                thread.ended_at = ev.ts_ms
        # Sticky: a thread that is a subagent anywhere is a subagent. The
        # adapters derive this per event from a per-file fact, so it never
        # flips, but ORing it means a partial read cannot demote a thread.
        thread.is_subagent = thread.is_subagent or ev.is_subagent
        if ev.parent_native_thread_id is not None:
            thread.parent_native_id = ev.parent_native_thread_id
        thread.agent_name.offer(ev.ts_ms, ev.agent_name)

        return sid, tid

    # -- write -----------------------------------------------------------

    def _ensure_parents(self, session_ids: set[str], thread_ids: set[str]) -> None:
        """Insert the `session` / `thread` rows an event batch needs.

        Rows go in with placeholder aggregates and no metadata; `finalize()`
        fills both in. Parent links are deliberately left NULL here -- see the
        module docstring.
        """
        new_sessions = [s for s in session_ids if s not in self._ensured_sessions]
        if new_sessions:
            self.conn.executemany(
                _INSERT_SESSION,
                [
                    {
                        "id": acc.id,
                        "native_id": acc.native_id,
                        "source": acc.source,
                        "host_id": self.host_id,
                        "started_at": acc.started_at,
                        "ended_at": acc.ended_at,
                    }
                    for acc in (self.sessions[s] for s in new_sessions)
                ],
            )
            self._ensured_sessions.update(new_sessions)

        new_threads = [t for t in thread_ids if t not in self._ensured_threads]
        if new_threads:
            self.conn.executemany(
                _INSERT_THREAD,
                [
                    {
                        "id": acc.id,
                        "native_id": acc.native_id,
                        "session_id": acc.session_id,
                        "is_subagent": 1 if acc.is_subagent else 0,
                        "started_at": acc.started_at,
                        "ended_at": acc.ended_at,
                    }
                    for acc in (self.threads[t] for t in new_threads)
                ],
            )
            self._ensured_threads.update(new_threads)

    def write_batch(self, events: Sequence[RawEvent]) -> int:
        """Upsert one batch of events. Returns the number of NEW event rows."""
        if not events:
            return 0
        rows: list[tuple] = []
        session_ids: set[str] = set()
        thread_ids: set[str] = set()
        for ev in events:
            sid, tid = self._absorb(ev)
            session_ids.add(sid)
            thread_ids.add(tid)
            rows.append(
                (
                    ids.event_id(sid, ev.native_event_id),
                    sid,
                    tid,
                    ev.native_event_id,
                    ev.ts_ms,
                    ev.ordinal,
                    str(ev.kind),
                    ev.model,
                    ev.tool_name,
                    ev.tool_use_id,
                    ev.input_tokens,
                    ev.output_tokens,
                    ev.cache_read_tokens,
                    ev.cache_write_tokens,
                )
            )

        self._ensure_parents(session_ids, thread_ids)
        # `total_changes` counts rows actually written; ON CONFLICT DO NOTHING
        # contributes nothing, so the delta IS the number of new events.
        before = self.conn.total_changes
        self.conn.executemany(_INSERT_EVENT, rows)
        return self.conn.total_changes - before

    # -- finalize --------------------------------------------------------

    def _aggregates(self, table_column: str, row_ids: Sequence[str]) -> dict[str, tuple[int, int, int]]:
        """`{id: (min_ts, max_ts, count)}` straight from the `event` table."""
        out: dict[str, tuple[int, int, int]] = {}
        for chunk in _chunked(row_ids, _ID_CHUNK):
            placeholders = ",".join("?" * len(chunk))
            sql = (
                f"SELECT {table_column}, MIN(ts), MAX(ts), COUNT(*) FROM event "
                f"WHERE {table_column} IN ({placeholders}) GROUP BY {table_column}"
            )
            for key, lo, hi, n in self.conn.execute(sql, list(chunk)):
                out[key] = (lo, hi, n)
        return out

    def finalize(self) -> None:
        """Write project rows, re-derive aggregates, resolve parent links."""
        if not self.sessions and not self.threads:
            return

        # 1. project rows for the cwds the sessions actually ended up with.
        projects: dict[str, str] = {}   # project_id -> root_path
        session_project: dict[str, str | None] = {}
        for sid, acc in self.sessions.items():
            root = project_root(acc.cwd.value)
            if root is None:
                session_project[sid] = None
                continue
            pid = ids.project_id(root)
            projects[pid] = root
            session_project[sid] = pid
        if projects:
            before = self.conn.total_changes
            self.conn.executemany(
                _INSERT_PROJECT,
                [
                    {"project_id": pid, "root_path": root, "name": project_name(root)}
                    for pid, root in sorted(projects.items())
                ],
            )
            self.stats.projects_written += self.conn.total_changes - before

        # 2. aggregates, re-derived rather than accumulated.
        sess_agg = self._aggregates("session_id", list(self.sessions))
        thread_agg = self._aggregates("thread_id", list(self.threads))

        self.conn.executemany(
            _UPDATE_SESSION,
            [
                {
                    "id": acc.id,
                    "project_id": session_project.get(acc.id),
                    "cwd": acc.cwd.value,
                    "git_branch": acc.git_branch.value,
                    "cli_version": acc.cli_version.value,
                    "started_at": sess_agg.get(acc.id, (acc.started_at, acc.ended_at, 0))[0],
                    "ended_at": sess_agg.get(acc.id, (acc.started_at, acc.ended_at, 0))[1],
                    "event_count": sess_agg.get(acc.id, (0, 0, 0))[2],
                }
                for acc in self.sessions.values()
            ],
        )
        self.conn.executemany(
            _UPDATE_THREAD,
            [
                {
                    "id": acc.id,
                    "agent_name": acc.agent_name.value,
                    "is_subagent": 1 if acc.is_subagent else 0,
                    "started_at": thread_agg.get(acc.id, (acc.started_at, acc.ended_at, 0))[0],
                    "ended_at": thread_agg.get(acc.id, (acc.started_at, acc.ended_at, 0))[1],
                    "event_count": thread_agg.get(acc.id, (0, 0, 0))[2],
                }
                for acc in self.threads.values()
            ],
        )

        # 3. parent links, now that every thread row of this run exists.
        links = [
            {"id": acc.id, "parent_id": ids.thread_id(acc.session_id, acc.parent_native_id)}
            for acc in self.threads.values()
            if acc.parent_native_id is not None
        ]
        if links:
            self.conn.executemany(_LINK_PARENT, links)

        self.stats.sessions_touched = len(self.sessions)
        self.stats.session_ids.update(self.sessions)
        self.stats.threads_touched = len(self.threads)


# --------------------------------------------------------------------------
# per-file plumbing
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FilePlan:
    """What to do with one discovered file."""

    from_byte: int
    prior_lines: int
    skip: bool
    rotated: bool


def _plan_file(
    conn: sqlite3.Connection, host_id: str, path: str, size_bytes: int, mtime_ms: int
) -> _FilePlan:
    row = conn.execute(
        "SELECT size_bytes, mtime_ms, bytes_read, lines_read FROM ingest_file "
        "WHERE host_id = ? AND path = ?",
        (host_id, path),
    ).fetchone()
    if row is None:
        return _FilePlan(from_byte=0, prior_lines=0, skip=False, rotated=False)

    prior_size, prior_mtime, bytes_read, lines_read = (row[0], row[1], row[2], row[3])
    # Shrunk => rotated or rewritten. Restart; dedup makes that harmless.
    if size_bytes < prior_size or size_bytes < bytes_read:
        return _FilePlan(from_byte=0, prior_lines=0, skip=False, rotated=True)
    if size_bytes == prior_size and mtime_ms == prior_mtime and bytes_read >= size_bytes:
        return _FilePlan(from_byte=bytes_read, prior_lines=lines_read, skip=True, rotated=False)
    return _FilePlan(from_byte=bytes_read, prior_lines=lines_read, skip=False, rotated=False)


def _ingest_one_file(run: _Run, adapter: SourceAdapter, path: Path) -> None:
    """Parse and upsert one log file inside its own transaction.

    Raises on failure; the caller rolls back and keeps going.
    """
    spath = str(path)
    stat = os.stat(spath)
    size_bytes = stat.st_size
    mtime_ms = int(stat.st_mtime * 1000)

    plan = _plan_file(run.conn, run.host_id, spath, size_bytes, mtime_ms)
    if plan.skip:
        run.stats.files_skipped += 1
        return
    if plan.rotated:
        log.info("%s shrank (%d bytes); re-reading from 0", spath, size_bytes)

    run.begin_file()
    run.conn.execute("BEGIN")
    try:
        bytes_read = plan.from_byte
        lines_read = plan.prior_lines
        last_byte_end = -1
        read = 0
        inserted = 0
        for batch in _chunked(adapter.parse(path, plan.from_byte), EVENT_CHUNK):
            for ev in batch:
                # One log line can yield several events (a Claude message with
                # two tool blocks), and they share a byte_end.
                if ev.byte_end != last_byte_end:
                    last_byte_end = ev.byte_end
                    lines_read += 1
                if ev.byte_end > bytes_read:
                    bytes_read = ev.byte_end
            read += len(batch)
            inserted += run.write_batch(batch)

        run.conn.execute(
            _UPSERT_INGEST_FILE,
            {
                "host_id": run.host_id,
                "path": spath,
                "source": adapter.name,
                "size_bytes": size_bytes,
                "mtime_ms": mtime_ms,
                "bytes_read": bytes_read,
                "lines_read": lines_read,
                "last_ingest": db.now_ms(),
            },
        )
        run.conn.execute("COMMIT")
    except BaseException:
        run.conn.execute("ROLLBACK")
        run.rollback_file()
        raise

    run.stats.files_ingested += 1
    run.stats.events_read += read
    run.stats.events_inserted += inserted
    run.stats.bytes_read += max(bytes_read - plan.from_byte, 0)


# --------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------


def ingest(
    conn: sqlite3.Connection,
    config: Config,
    *,
    adapters: Sequence[SourceAdapter] | None = None,
    sources: Sequence[str] | None = None,
) -> IngestStats:
    """Ingest every log file the adapters discover into `conn`.

    `adapters` (tests inject fakes) wins over `sources`, which selects from
    `ADAPTERS` by name. Safe to call repeatedly: a second run over an unchanged
    corpus inserts nothing.

    Never raises for a bad log file -- the failure is logged, counted in
    `IngestStats.files_failed` and recorded in `IngestStats.errors`.
    """
    started = time.perf_counter()
    used = list(adapters) if adapters is not None else build_adapters(config, sources)

    db.upsert_host(conn, config.host_id, config.hostname, _host_os())

    run = _Run(conn, config)
    for adapter in used:
        try:
            discovered = sorted({str(p): Path(p) for p in adapter.discover()}.values())
        except Exception as exc:  # a broken glob must not kill the other source
            log.warning("discover failed for source %s: %r", adapter.name, exc)
            run.stats.errors.append((f"<discover:{adapter.name}>", repr(exc)))
            run.stats.files_failed += 1
            continue

        run.stats.files_discovered += len(discovered)
        for path in discovered:
            try:
                _ingest_one_file(run, adapter, path)
            except Exception as exc:
                run.stats.files_failed += 1
                run.stats.errors.append((str(path), repr(exc)))
                log.warning("ingest failed for %s: %r", path, exc)

    try:
        conn.execute("BEGIN")
        run.finalize()
        conn.execute("COMMIT")
    except Exception as exc:
        conn.execute("ROLLBACK")
        log.error("finalize failed: %r", exc)
        raise

    run.stats.duration_s = time.perf_counter() - started
    return run.stats


def _host_os() -> str:
    # Imported lazily so `config.host_os()` stays the single definition.
    from cc_insights.config import host_os

    return host_os()


__all__ = [
    "ADAPTERS",
    "EVENT_CHUNK",
    "IngestStats",
    "build_adapters",
    "ingest",
    "project_name",
    "project_root",
]
