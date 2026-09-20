"""Watch mode: keep the database current while the agents are still running.

The launchd job runs the pipeline every 15 minutes, which is right for not
losing history and wrong for watching work happen. This module closes that gap:
a loop that notices a log file grow, ingests just the new bytes, re-derives
just the sessions that moved, and publishes a tick that an open dashboard can
listen for.

**It polls. The roadmap said FSEvents; this is the considered answer, not a
shortcut.** Measured on the real corpus, expanding every source glob and
stat-ing all 737 files costs 50 ms, so a two-second cycle spends 2.5% of one
core. Against that, an FSEvents stream means either a dependency (`watchdog`,
`pyobjc`) in a tool whose Python side is deliberately dependency-free, or
about 150 lines of `ctypes` against CoreServices plus a CFRunLoop thread that
only runs on macOS and cannot be tested in CI. Polling is also the only one of
the three that already works on the Windows box v2 assumes. `Watcher` is a
protocol with one implementation, so an FSEvents watcher can be dropped in
later without touching the loop.

**A cycle is incremental on both ends.** Ingest resumes at each file's stored
byte offset; derive and pricing are then scoped to `IngestStats.session_ids`.
On this corpus that is the difference between 2.5 s and about 0.1 s per cycle,
which is what makes a two-second interval sane at all. A full re-derive still
happens exactly where it must -- when a price changes, `cci cost` rebuilds
everything.

**The `-wal` sidecar is watched too.** A SQLite source (opencode) commits to
`<db>-wal` and only folds it back on checkpoint, so the main file's size and
mtime can sit unchanged for hours while sessions accumulate. Watching only
what `discover()` returns would make opencode invisible to this loop for as
long as a user keeps it open, which is precisely when they are watching.

**Nothing in a cycle may kill the loop.** A malformed log, a database locked
by another process, a glob that suddenly matches a directory: each is logged,
counted, and the loop sleeps and tries again. A watcher that exits on the
first bad file is a watcher that is not running when it matters.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

from cc_insights import cost as cost_mod, db, derive, ingest, pricing
from cc_insights.config import Config
from cc_insights.live import HEARTBEAT_S, LiveState

log = logging.getLogger(__name__)

#: Seconds between scans. Two is comfortably under the pace at which a human
#: notices staleness, and 50 ms of stat per scan is affordable at that rate.
DEFAULT_INTERVAL_S = 2.0

#: (size, mtime_ns). Both, because a file can be rewritten to the same length
#: and an editor can preserve mtime; together they miss almost nothing, and
#: what they miss the next append catches.
Signature = tuple[int, int]


class Watcher(Protocol):
    """Anything that can say which files changed since it last looked."""

    def poll(self) -> set[str]:
        """Paths that appeared or grew since the previous call."""
        ...


def _signature(path: str) -> Signature | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


class PollingWatcher:
    """Stat every file the adapters discover, and diff against last time.

    Discovery goes through the adapters rather than through the globs
    directly, so this watches exactly the set ingest will read -- one source
    of truth for "what counts as a log file", and a new adapter is watched the
    day it lands.
    """

    def __init__(self, config: Config, sources: Sequence[str] | None = None) -> None:
        self.config = config
        self.sources = list(sources) if sources else None
        self._seen: dict[str, Signature] = {}
        #: Set after the first poll. The first one reports everything, which
        #: the loop uses to decide whether a startup pass has work to do.
        self.primed = False

    def paths(self) -> list[str]:
        """Every watchable path, log files plus their `-wal` sidecars."""
        found: list[str] = []
        for adapter in ingest.build_adapters(self.config, self.sources):
            try:
                discovered = list(adapter.discover())
            except Exception as exc:  # a broken glob must not stop the others
                log.warning("discover failed for %s: %r", adapter.name, exc)
                continue
            for p in discovered:
                sp = str(p)
                found.append(sp)
                wal = f"{sp}-wal"
                if os.path.exists(wal):
                    found.append(wal)
        return found

    def poll(self) -> set[str]:
        """Paths that appeared or changed since the last call.

        A file that *disappeared* is not reported: Claude prunes its log
        directories on a rolling basis and there is nothing to read from a
        path that is gone. What was already ingested stays in the database,
        which is the entire point of ingesting it.
        """
        current: dict[str, Signature] = {}
        for path in self.paths():
            sig = _signature(path)
            if sig is not None:
                current[path] = sig

        changed = {p for p, sig in current.items() if self._seen.get(p) != sig}
        self._seen = current
        self.primed = True
        return changed


@dataclass(slots=True)
class Cycle:
    """What one pass of the pipeline did, and where that left the corpus.

    The distinction matters more than it looks. `events_inserted` and
    `sessions` are this cycle's delta; `spans`, `active_ms` and `cost_total`
    are the WHOLE database afterwards. Reporting the scoped derive's own
    numbers instead -- which the first draft did -- printed "32 spans, 2.9 h"
    every few seconds on a corpus of 1,430 spans and 193 hours, which reads
    like the history just vanished.
    """

    at: int = 0
    changed_files: int = 0
    events_inserted: int = 0
    sessions: int = 0
    #: Corpus-wide, after this cycle.
    spans: int = 0
    active_ms: int = 0
    cost_total: float = 0.0
    currency: str = "USD"
    duration_s: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def did_work(self) -> bool:
        return self.events_inserted > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "changedFiles": self.changed_files,
            "eventsInserted": self.events_inserted,
            "sessions": self.sessions,
            "spans": self.spans,
            "activeMs": self.active_ms,
            "cost": round(self.cost_total, 6),
            "currency": self.currency,
            "durationS": round(self.duration_s, 3),
            "errors": self.errors,
        }


def run_cycle(
    conn: sqlite3.Connection,
    config: Config,
    *,
    changed_files: int = 0,
    sources: Sequence[str] | None = None,
    full: bool = False,
) -> Cycle:
    """Ingest, derive and price once. Never raises for a bad log file.

    `full=True` re-derives and re-prices the whole database, which is what a
    first pass after a schema or price change needs. Otherwise both are scoped
    to the sessions this ingest actually wrote to.
    """
    started = time.perf_counter()
    cycle = Cycle(at=db.now_ms(), changed_files=changed_files)

    stats = ingest.ingest(conn, config, sources=sources)
    cycle.events_inserted = stats.events_inserted
    cycle.sessions = stats.sessions_touched
    cycle.errors = [f"{path}: {err}" for path, err in stats.errors[:5]]

    scope = None if full else sorted(stats.session_ids)
    if full or scope:
        derive.derive(conn, idle_threshold_s=config.idle_threshold_s, session_ids=scope)
        cost_mod.derive_costs(conn, session_ids=scope)
    cycle.spans, cycle.active_ms, cycle.cost_total, cycle.currency = totals(conn)

    cycle.duration_s = time.perf_counter() - started
    return cycle


def totals(conn: sqlite3.Connection) -> tuple[int, int, float, str]:
    """(spans, active ms, cost, currency) for the whole database.

    Read after every cycle rather than accumulated, and read from the rolled-up
    `session.active_ms` rather than by re-summing spans: 319 rows instead of
    1,430, and it is the same number the dashboard shows, which is the point
    of printing it.
    """
    spans = conn.execute("SELECT count(*) FROM span").fetchone()[0] or 0
    active = conn.execute("SELECT coalesce(sum(active_ms), 0) FROM session").fetchone()[0] or 0
    nano = conn.execute(
        "SELECT coalesce(sum(input_nano + output_nano + cache_read_nano"
        " + cache_write_nano), 0) FROM event_cost"
    ).fetchone()[0] or 0
    currency = pricing.currency_in_use(conn)
    return spans, active, nano / cost_mod.NANO, currency


def watch(
    conn: sqlite3.Connection,
    config: Config,
    *,
    interval_s: float = DEFAULT_INTERVAL_S,
    sources: Sequence[str] | None = None,
    live: LiveState | None = None,
    on_cycle: Callable[[Cycle], None] | None = None,
    stop: threading.Event | None = None,
    watcher: Watcher | None = None,
    max_cycles: int | None = None,
) -> int:
    """Poll for log activity and keep the database current. Blocks.

    Returns the number of cycles that did work. `stop` (any `threading.Event`)
    ends the loop at the next boundary; `max_cycles` bounds it, which is how
    the tests drive it without a clock.

    The first pass always runs, before any polling: a dashboard opened next to
    a watcher must be current immediately, not current in two seconds. It is a
    `full` pass, because whatever happened while nothing was watching is not
    described by any file's mtime.
    """
    watcher = watcher or PollingWatcher(config, sources)
    worked = 0
    cycles = 0

    _scan(watcher)  # prime: everything looks new the first time
    cycle = run_cycle(conn, config, sources=sources, full=True)
    _report(cycle, live, on_cycle)
    worked += bool(cycle.did_work)
    cycles += 1

    while max_cycles is None or cycles < max_cycles:
        # Waiting on the stop event rather than sleeping is what makes Ctrl-C
        # land within a cycle instead of within an interval.
        if stop is not None:
            if stop.wait(interval_s):
                break
        else:
            time.sleep(interval_s)

        changed = _scan(watcher)
        cycles += 1
        if not changed:
            continue

        try:
            cycle = run_cycle(conn, config, changed_files=len(changed), sources=sources)
        except Exception as exc:
            # A database locked by another writer, a disk full, a bug: report
            # it and keep watching. Exiting here is how a watcher is quietly
            # not running when someone needs it.
            log.warning("watch cycle failed: %r", exc)
            cycle = Cycle(at=db.now_ms(), changed_files=len(changed), errors=[repr(exc)])
        _report(cycle, live, on_cycle)
        worked += bool(cycle.did_work)

    return worked


def _scan(watcher: Watcher) -> set[str]:
    """Poll, treating a failed scan as "nothing changed".

    A filesystem that goes away mid-scan -- an unmounted volume, a directory
    swapped under us -- is a reason to try again in two seconds, not a reason
    to stop watching.
    """
    try:
        return watcher.poll()
    except Exception as exc:
        log.warning("watch scan failed: %r", exc)
        return set()


def _report(
    cycle: Cycle, live: LiveState | None, on_cycle: Callable[[Cycle], None] | None
) -> None:
    if on_cycle is not None:
        on_cycle(cycle)
    # Only a cycle that changed the data is published: a listening dashboard
    # refetches on every tick, and ticking when nothing moved would make it
    # refetch forever.
    if live is not None and (cycle.did_work or cycle.errors):
        live.publish(cycle.as_dict())


#: Re-exported so `from cc_insights.watch import LiveState` keeps working:
#: the tick is part of watch mode's surface even though it is defined in
#: `live.py`, which `serve.py` imports without dragging in the pipeline.
__all__ = [
    "DEFAULT_INTERVAL_S",
    "HEARTBEAT_S",
    "Cycle",
    "LiveState",
    "PollingWatcher",
    "Signature",
    "Watcher",
    "run_cycle",
    "watch",
]
