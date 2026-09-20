"""Fill columns added after a database was built, from the logs still on disk.

Ingest writes `event` rows with ``ON CONFLICT DO NOTHING``, because a conflict
on `native_event_id` means *the same logical event*, not a newer version of
one. That is right, and it has one consequence: a column added by a later
migration stays NULL on every row already stored, and no amount of re-running
`cci ingest` will fill it.

The usual advice for that is "rebuild the database". Here that advice is
wrong, and the reason is the whole point of this project: agent log
directories are pruned on a rolling basis, so a rebuild silently drops every
session whose log has since aged out. A database is allowed to hold history
its source no longer does.

So this is the third option. It re-reads the logs that *do* still exist and
fills the new column on rows that already have their id, touching nothing
else -- no inserts, no deletes, no other column. What has aged out keeps its
NULL, which means "unknown", and `cci cost` reports how much of the total
rests on that rather than absorbing it.

Today it fills exactly one column, `event.cache_write_1h_tokens` (migration
005). It is written as a table of jobs rather than a one-off script because
the next column added after a database exists will want the same thing, and
because a one-off script is a thing nobody can find in six months.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from cc_insights import ids, ingest
from cc_insights.config import Config
from cc_insights.sources.base import RawEvent

log = logging.getLogger(__name__)

#: Rows per UPDATE batch.
_CHUNK = 5_000


@dataclass(frozen=True, slots=True)
class Job:
    """One column, and how to get its value out of a parsed event."""

    column: str
    #: The RawEvent attribute holding it.
    attribute: str
    why: str

    def value(self, event: RawEvent) -> object | None:
        return getattr(event, self.attribute, None)


#: Every column a backfill knows how to fill. Add a row when a migration adds
#: a column that existing events could have carried all along.
JOBS: tuple[Job, ...] = (
    Job(
        column="cache_write_1h_tokens",
        attribute="cache_write_1h_tokens",
        why="a one-hour cache write costs 2x base input against 1.25x for "
            "five minutes; without the split every write is priced short",
    ),
)


@dataclass(slots=True)
class BackfillStats:
    files_read: int = 0
    events_seen: int = 0
    #: Rows that gained a value they did not have.
    filled: int = 0
    #: Rows whose column was already set -- a second run fills nothing.
    already_set: int = 0
    #: Parsed events with no row in the database. Normal: a log can be newer
    #: than the last ingest, and `cci ingest` is what should pick those up.
    not_in_database: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "filesRead": self.files_read,
            "eventsSeen": self.events_seen,
            "filled": self.filled,
            "alreadySet": self.already_set,
            "notInDatabase": self.not_in_database,
            "errors": len(self.errors),
        }


def coverage(conn: sqlite3.Connection, column: str) -> tuple[int, int]:
    """(tokens whose value is known, tokens total) for a cache-write column.

    Reported in tokens rather than rows because that is the unit the money is
    in: one busy event can carry more cache writes than a thousand quiet ones.
    """
    known = conn.execute(
        f"SELECT coalesce(sum(cache_write_tokens), 0) FROM event"
        f" WHERE {column} IS NOT NULL AND cache_write_tokens > 0"
    ).fetchone()[0] or 0
    total = conn.execute(
        "SELECT coalesce(sum(cache_write_tokens), 0) FROM event"
        " WHERE cache_write_tokens > 0"
    ).fetchone()[0] or 0
    return known, total


def backfill(
    conn: sqlite3.Connection,
    config: Config,
    *,
    jobs: Sequence[Job] = JOBS,
    sources: Sequence[str] | None = None,
    on_file: Callable[[Path], None] | None = None,
) -> BackfillStats:
    """Re-read every discoverable log and fill `jobs`' columns where NULL.

    Ids are recomputed rather than looked up: `event_id` is a hash of its
    session and its native id, so a parsed event knows which row it is
    without a query. That is the same property that makes ingest idempotent.

    One transaction per file, like ingest, so a log that fails mid-read rolls
    back to a consistent state and the run continues.
    """
    stats = BackfillStats()
    columns = [j.column for j in jobs]
    if not columns:
        return stats
    assignments = ", ".join(f"{c} = coalesce(?, {c})" for c in columns)
    guard = " OR ".join(f"{c} IS NULL" for c in columns)
    sql = f"UPDATE event SET {assignments} WHERE id = ? AND ({guard})"

    for adapter in ingest.build_adapters(config, sources):
        try:
            discovered = sorted({str(p): Path(p) for p in adapter.discover()}.values())
        except Exception as exc:
            log.warning("discover failed for %s: %r", adapter.name, exc)
            stats.errors.append((f"<discover:{adapter.name}>", repr(exc)))
            continue

        for path in discovered:
            if on_file is not None:
                on_file(path)
            try:
                _one_file(conn, config, adapter, path, jobs, sql, stats)
            except Exception as exc:
                log.warning("backfill failed for %s: %r", path, exc)
                stats.errors.append((str(path), repr(exc)))
    return stats


def _one_file(conn, config: Config, adapter, path: Path, jobs, sql: str,
              stats: BackfillStats) -> None:
    batch: list[tuple] = []
    conn.execute("BEGIN")
    try:
        for event in adapter.parse(path, 0):
            stats.events_seen += 1
            values = [job.value(event) for job in jobs]
            if all(v is None for v in values):
                continue
            session = ids.session_id(config.host_id, adapter.name, event.native_session_id)
            batch.append((*values, ids.event_id(session, event.native_event_id)))
            if len(batch) >= _CHUNK:
                _apply(conn, sql, batch, stats)
                batch = []
        _apply(conn, sql, batch, stats)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    stats.files_read += 1


def _apply(conn: sqlite3.Connection, sql: str, batch: Sequence[tuple],
           stats: BackfillStats) -> None:
    """Run one batch, and tell apart "filled it" from "nothing to fill".

    `total_changes` counts rows the UPDATE actually touched. A row already
    holding a value is excluded by the WHERE, and a row that is not there at
    all matches nothing, so the two are distinguished by checking existence
    only for the shortfall -- which is almost always zero and costs nothing.
    """
    if not batch:
        return
    before = conn.total_changes
    conn.executemany(sql, batch)
    changed = conn.total_changes - before
    stats.filled += changed

    shortfall = len(batch) - changed
    if shortfall <= 0:
        return
    ids_in_batch = [row[-1] for row in batch]
    present = 0
    for start in range(0, len(ids_in_batch), 400):
        chunk = ids_in_batch[start:start + 400]
        marks = ",".join("?" * len(chunk))
        present += conn.execute(
            f"SELECT count(*) FROM event WHERE id IN ({marks})", chunk
        ).fetchone()[0]
    stats.already_set += max(0, present - changed)
    stats.not_in_database += max(0, len(batch) - present)


__all__ = ["JOBS", "BackfillStats", "Job", "backfill", "coverage"]
