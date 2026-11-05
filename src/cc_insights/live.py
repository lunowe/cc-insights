"""The tick between whatever writes the database and the server.

One counter, one condition variable, and no idea what either side does. It
lives in its own module so that `serve.py` -- a read-only server -- does not
have to import the entire write pipeline (`watch` -> `ingest` -> every source
adapter, three times the import cost) just to know when something changed.

Two things can feed it. `cci watch --serve` publishes each pipeline cycle it
runs itself. A plain `cci serve` runs no pipeline, so `DatabaseFeed` watches
the database file instead and publishes whenever *another process* commits to
it -- the 15-minute job, `cci install --watch`, a `cci sync pull`. Either way
the page sees the same stream; `feed` says which one is behind it.

The generation number *is* the message. A listener that missed three ticks
does not want three refreshes, it wants the current state, so the payload is
always whatever was true at the last tick and never a queue of what it missed.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)

#: Seconds a listening dashboard waits before being sent a keep-alive. Under
#: the 60 s that proxies and browsers use to decide a stream is dead.
HEARTBEAT_S = 20.0

#: Seconds between two looks at the database in `DatabaseFeed`. One
#: `PRAGMA data_version` is a read of a counter in shared memory, so this is
#: about latency and nothing else; the page coalesces refetches to one per
#: five seconds anyway.
DB_POLL_S = 1.0

#: Who is publishing. `watch`: a pipeline in this process, whose ticks carry
#: the cycle's numbers. `database`: commits by some other process, whose ticks
#: carry only when they were seen.
Feed = Literal["watch", "database"]


def _now_ms() -> int:
    return int(time.time() * 1000)


class LiveState:
    """Publish/subscribe for "the database moved", across two threads."""

    def __init__(self, feed: Feed = "watch") -> None:
        self.feed: Feed = feed
        self._cv = threading.Condition()
        self._generation = 0
        self._payload: dict[str, Any] = {}
        self._started = _now_ms()

    @property
    def generation(self) -> int:
        with self._cv:
            return self._generation

    def publish(self, payload: dict[str, Any]) -> int:
        with self._cv:
            self._generation += 1
            self._payload = payload
            self._cv.notify_all()
            return self._generation

    def snapshot(self) -> tuple[int, dict[str, Any]]:
        with self._cv:
            return self._generation, dict(self._payload)

    def wait_after(self, generation: int, timeout: float) -> tuple[int, dict[str, Any]] | None:
        """Block until the generation passes `generation`, or time out.

        Returns None on timeout, which the caller turns into a keep-alive
        rather than a reconnect.
        """
        deadline = time.monotonic() + timeout
        with self._cv:
            while self._generation <= generation:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cv.wait(remaining)
            return self._generation, dict(self._payload)

    def status(self) -> dict[str, Any]:
        gen, payload = self.snapshot()
        return {"watching": True, "feed": self.feed, "generation": gen,
                "since": self._started, "last": payload}


class DatabaseFeed:
    """Publish to `live` whenever another connection commits to `db_path`.

    `PRAGMA data_version` changes on a connection exactly when *some other*
    connection has committed since it last asked -- across processes, and
    with WAL on a read-only handle too. That is precisely "the database
    moved", for the cost of one counter read per poll and without knowing
    anything about the schema or who the writer is.

    Any commit counts, including an ingest that found nothing new but still
    stamped `ingest_file.last_ingest`. That costs the page one refetch per
    background run, which is cheaper and more honest than guessing which
    tables matter to which chart.

    The connection is opened read-only and held for the life of the feed,
    because the counter is per connection: a fresh handle per poll would
    never see it move. A poll that fails -- the file briefly missing, a lock
    held across a checkpoint -- drops the handle and starts over on the next
    one rather than ending the feed.
    """

    def __init__(self, db_path: Path, live: LiveState, *,
                 interval_s: float = DB_POLL_S) -> None:
        self.db_path = Path(db_path)
        self.live = live
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._conn: sqlite3.Connection | None = None
        self._version = 0
        self._seen = False

    def start(self) -> None:
        """Take the baseline here, then poll in the background.

        The first look happens in the caller's thread, so a server that calls
        this before it accepts requests has its baseline before any page can
        load. Taken lazily in the thread instead, a commit landing between
        the page's fetch and that first look would be folded into the
        baseline and never announced. If the first look fails, the first
        one that succeeds later is treated as a change, for the same reason.
        """
        self.poll()
        self._seen = True
        self._thread = threading.Thread(target=self._run, name="cci-db-feed", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def poll(self) -> bool:
        """Look once. True if the database moved since the last look.

        The very first look only records where the counter stands: the page
        fetched the current state when it loaded. A *re*connect after an
        error counts as a change, because the counter of a new handle says
        nothing about what the old one missed while it was down, and a
        spurious refetch is cheap where a missed one is the bug.
        """
        fresh = self._conn is None
        try:
            if fresh:
                self._conn = self._open()
            version = self._conn.execute("PRAGMA data_version").fetchone()[0]
        except sqlite3.Error as exc:
            log.warning("database feed: %r", exc)
            self._close()
            return False
        moved = self._seen if fresh else version != self._version
        self._version, self._seen = version, True
        if moved:
            self.live.publish({"at": _now_ms()})
        return moved

    def _open(self) -> sqlite3.Connection:
        # as_uri() percent-encodes, so a path with a space or a '?' in it is
        # not misread as the start of the URI query.
        uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, isolation_level=None,
                               check_same_thread=False)
        conn.execute("PRAGMA query_only = ON")
        return conn

    def _close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def _run(self) -> None:
        try:
            while not self._stop.wait(self.interval_s):
                self.poll()
        finally:
            self._close()


__all__ = ["DB_POLL_S", "HEARTBEAT_S", "DatabaseFeed", "Feed", "LiveState"]
