"""Tests for watch mode.

The loop is driven with an injected `Watcher` and a bounded `max_cycles`
rather than with sleeps: a test that waits on a real interval is a test that
is flaky on a loaded machine and slow on an idle one.

Two carry the weight. `test_a_failing_cycle_does_not_stop_the_loop` is the
whole reliability claim -- a watcher that exits on the first bad file is a
watcher that is not running when it matters. `test_a_wal_sidecar_is_watched`
covers the one source whose main file can sit unchanged for hours while work
piles up beside it.
"""

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from cc_insights import db, watch
from cc_insights.config import Config

CLAUDE = "claude_code"


def make_config(tmp_path: Path, **globs) -> Config:
    return Config(
        host_id="host-under-test",
        hostname="test-host",
        db_path=tmp_path / "test.db",
        source_globs={k: [str(p) for p in v] for k, v in globs.items()},
        config_dir=tmp_path,
    )


def line(session: str, uuid: str, ts: str) -> str:
    return json.dumps({
        "isSidechain": False, "cwd": "/code/demo", "sessionId": session,
        "version": "2.0.0", "gitBranch": "main", "type": "assistant",
        "message": {"role": "assistant", "model": "claude-opus-5", "content": [],
                    "usage": {"input_tokens": 10, "output_tokens": 5}},
        "uuid": uuid, "timestamp": ts,
    }) + "\n"


@pytest.fixture
def corpus(tmp_path: Path):
    """A one-file Claude corpus, its config, and a migrated database."""
    logs = tmp_path / "logs"
    logs.mkdir()
    path = logs / "s1.jsonl"
    path.write_text(line("s1", "u1", "2026-05-10T09:00:00.000Z")
                    + line("s1", "u2", "2026-05-10T09:01:00.000Z"))
    cfg = make_config(tmp_path, **{CLAUDE: [logs / "*.jsonl"]})
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    yield conn, cfg, logs
    conn.close()


class FakeWatcher:
    """Replays a scripted sequence of poll results, then reports nothing.

    The loop primes the watcher once before its first cycle, so scripts here
    start with the result of that priming call.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def poll(self) -> set[str]:
        self.calls += 1
        return self.script.pop(0) if self.script else set()


# --- the poller ---------------------------------------------------------


def test_the_first_poll_reports_everything(corpus):
    _, cfg, logs = corpus
    w = watch.PollingWatcher(cfg)
    assert w.poll() == {str(logs / "s1.jsonl")}


def test_a_quiet_second_poll_reports_nothing(corpus):
    _, cfg, _ = corpus
    w = watch.PollingWatcher(cfg)
    w.poll()
    assert w.poll() == set()


def test_an_append_is_reported(corpus):
    _, cfg, logs = corpus
    w = watch.PollingWatcher(cfg)
    w.poll()
    with (logs / "s1.jsonl").open("a") as fh:
        fh.write(line("s1", "u3", "2026-05-10T09:02:00.000Z"))
    assert w.poll() == {str(logs / "s1.jsonl")}


def test_a_new_file_is_reported(corpus):
    _, cfg, logs = corpus
    w = watch.PollingWatcher(cfg)
    w.poll()
    (logs / "s2.jsonl").write_text(line("s2", "u9", "2026-05-10T10:00:00.000Z"))
    assert w.poll() == {str(logs / "s2.jsonl")}


def test_a_deleted_file_is_not_reported(corpus):
    """Claude prunes its log directories; there is nothing to read from a path
    that is gone, and what was ingested stays ingested."""
    _, cfg, logs = corpus
    w = watch.PollingWatcher(cfg)
    w.poll()
    (logs / "s1.jsonl").unlink()
    assert w.poll() == set()


def test_a_wal_sidecar_is_watched(tmp_path: Path):
    """A SQLite source commits to `<db>-wal` and folds it back on checkpoint,
    so the main file can sit unchanged for hours while sessions accumulate."""
    store = tmp_path / "opencode.db"
    sqlite3.connect(store).close()
    wal = tmp_path / "opencode.db-wal"
    wal.write_bytes(b"")
    cfg = make_config(tmp_path, opencode=[store])

    w = watch.PollingWatcher(cfg)
    assert str(wal) in w.paths()
    w.poll()
    wal.write_bytes(b"new frames")
    assert w.poll() == {str(wal)}


def test_a_source_that_cannot_be_discovered_does_not_break_the_scan(corpus, monkeypatch):
    _, cfg, logs = corpus

    class Broken:
        name = "broken"

        def __init__(self, *_a, **_k):
            pass

        def discover(self):
            raise OSError("nope")

    monkeypatch.setitem(watch.ingest.ADAPTERS, "broken", Broken)
    cfg.source_globs["broken"] = ["/nowhere/*"]
    assert watch.PollingWatcher(cfg).poll() == {str(logs / "s1.jsonl")}


def test_the_poller_watches_exactly_what_ingest_reads(corpus):
    """One definition of "a log file", not two that can drift apart."""
    conn, cfg, logs = corpus
    (logs / "not-a-log.txt").write_text("hello")
    assert watch.PollingWatcher(cfg).poll() == {str(logs / "s1.jsonl")}


# --- one cycle ----------------------------------------------------------


def test_a_cycle_ingests_derives_and_prices(corpus):
    conn, cfg, _ = corpus
    cycle = watch.run_cycle(conn, cfg, full=True)
    assert cycle.events_inserted == 2
    assert cycle.sessions == 1
    assert cycle.spans == 1
    assert cycle.active_ms == 60_000
    assert cycle.did_work


def test_a_second_cycle_over_unchanged_logs_does_nothing(corpus):
    conn, cfg, _ = corpus
    watch.run_cycle(conn, cfg, full=True)
    again = watch.run_cycle(conn, cfg)
    assert again.events_inserted == 0 and not again.did_work


def test_a_cycle_reports_corpus_totals_not_its_own_scope(corpus):
    """A scoped derive touches a handful of sessions; printing its span count
    every two seconds would read as if the history had vanished."""
    conn, cfg, logs = corpus
    watch.run_cycle(conn, cfg, full=True)
    (logs / "s2.jsonl").write_text(line("s2", "u9", "2026-05-11T10:00:00.000Z")
                                   + line("s2", "u10", "2026-05-11T10:01:00.000Z"))
    cycle = watch.run_cycle(conn, cfg)
    assert cycle.sessions == 1            # only s2 moved
    assert cycle.spans == 2               # ...but both sessions' spans are reported
    assert cycle.active_ms == 120_000


def test_a_scoped_cycle_leaves_other_sessions_untouched(corpus):
    conn, cfg, logs = corpus
    watch.run_cycle(conn, cfg, full=True)
    before = conn.execute("SELECT id FROM span").fetchone()[0]
    (logs / "s2.jsonl").write_text(line("s2", "u9", "2026-05-11T10:00:00.000Z")
                                   + line("s2", "u10", "2026-05-11T10:01:00.000Z"))
    watch.run_cycle(conn, cfg)
    assert conn.execute("SELECT count(*) FROM span WHERE id = ?", (before,)).fetchone()[0] == 1


def test_a_bad_log_file_is_counted_not_raised(corpus):
    conn, cfg, logs = corpus
    (logs / "broken.jsonl").write_text("{not json\n")
    cycle = watch.run_cycle(conn, cfg, full=True)
    # A truncated line is skipped by the adapter, so this is not even an
    # error -- the point is that the cycle completed and returned.
    assert cycle.events_inserted == 2 and cycle.errors == []


def test_totals_are_the_whole_database(corpus):
    conn, cfg, _ = corpus
    watch.run_cycle(conn, cfg, full=True)
    spans, active, cost, currency = watch.totals(conn)
    assert spans == 1 and active == 60_000
    assert cost == 0.0 and currency == "USD"   # no prices loaded in this corpus


# --- the loop -----------------------------------------------------------


def test_the_first_pass_runs_before_any_polling(corpus):
    """A dashboard opened next to a watcher must be current immediately."""
    conn, cfg, _ = corpus
    seen: list[watch.Cycle] = []
    watch.watch(conn, cfg, interval_s=0, watcher=FakeWatcher([]), max_cycles=1,
                on_cycle=seen.append)
    assert len(seen) == 1 and seen[0].events_inserted == 2


def test_a_cycle_runs_only_when_something_changed(corpus):
    conn, cfg, _ = corpus
    seen: list[watch.Cycle] = []
    fake = FakeWatcher([set(), {"/x"}, set()])
    watch.watch(conn, cfg, interval_s=0, watcher=fake, max_cycles=4, on_cycle=seen.append)
    assert len(seen) == 2          # the startup pass, plus the one change
    assert seen[1].changed_files == 1


def test_the_stop_event_ends_the_loop(corpus):
    conn, cfg, _ = corpus
    stop = threading.Event()
    stop.set()
    watch.watch(conn, cfg, interval_s=60, watcher=FakeWatcher([]), stop=stop)
    # Returned without waiting a minute: the startup pass ran, then it stopped.


def test_stopping_is_prompt_not_interval_bound(corpus):
    _, cfg, _ = corpus
    stop = threading.Event()
    done = threading.Event()

    def run():
        # sqlite3 handles belong to the thread that made them, which is why
        # `cci watch --serve` opens the writer inside its worker too.
        own = db.connect(cfg.db_path)
        try:
            watch.watch(own, cfg, interval_s=30, watcher=FakeWatcher([]), stop=stop)
        finally:
            own.close()
        done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(0.2)
    stop.set()
    assert done.wait(5), "the loop slept through its stop event"


def test_a_failing_cycle_does_not_stop_the_loop(corpus, monkeypatch):
    """A locked database, a full disk, a bug: log it and keep watching."""
    conn, cfg, _ = corpus
    calls = {"n": 0}
    real = watch.run_cycle

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **kw)

    monkeypatch.setattr(watch, "run_cycle", flaky)
    seen: list[watch.Cycle] = []
    fake = FakeWatcher([set(), {"/a"}, {"/b"}])
    watch.watch(conn, cfg, interval_s=0, watcher=fake, max_cycles=3, on_cycle=seen.append)

    assert calls["n"] == 3
    assert seen[1].errors and "locked" in seen[1].errors[0]
    assert seen[2].errors == []      # recovered


def test_a_scan_failure_does_not_stop_the_loop(corpus):
    conn, cfg, _ = corpus

    class Exploding:
        def __init__(self):
            self.calls = 0

        def poll(self):
            self.calls += 1
            raise OSError("filesystem went away")

    w = Exploding()
    watch.watch(conn, cfg, interval_s=0, watcher=w, max_cycles=3)
    assert w.calls >= 2


# --- the live tick ------------------------------------------------------


def test_publish_bumps_the_generation():
    live = watch.LiveState()
    assert live.generation == 0
    assert live.publish({"a": 1}) == 1
    assert live.snapshot() == (1, {"a": 1})


def test_wait_after_returns_none_on_timeout():
    live = watch.LiveState()
    started = time.monotonic()
    assert live.wait_after(0, timeout=0.05) is None
    assert time.monotonic() - started < 2


def test_wait_after_returns_at_once_when_already_ahead():
    live = watch.LiveState()
    live.publish({"a": 1})
    assert live.wait_after(0, timeout=30) == (1, {"a": 1})


def test_a_waiter_wakes_when_a_cycle_publishes():
    live = watch.LiveState()
    got: list = []

    def wait():
        got.append(live.wait_after(0, timeout=5))

    t = threading.Thread(target=wait, daemon=True)
    t.start()
    time.sleep(0.1)
    live.publish({"eventsInserted": 3})
    t.join(timeout=5)
    assert got == [(1, {"eventsInserted": 3})]


def test_only_a_cycle_that_changed_something_ticks(corpus):
    """A listener refetches on every tick; ticking on a quiet cycle would
    make an open dashboard refetch forever."""
    conn, cfg, _ = corpus
    live = watch.LiveState()
    fake = FakeWatcher([set(), {"/x"}])   # a change, but no new events behind it
    watch.watch(conn, cfg, interval_s=0, watcher=fake, max_cycles=2, live=live)
    assert live.generation == 1        # the startup pass only

    snapshot = live.snapshot()[1]
    assert snapshot["eventsInserted"] == 2


def test_the_payload_carries_what_a_dashboard_needs(corpus):
    conn, cfg, _ = corpus
    live = watch.LiveState()
    watch.watch(conn, cfg, interval_s=0, watcher=FakeWatcher([]), max_cycles=1, live=live)
    payload = live.snapshot()[1]
    assert set(payload) >= {"at", "eventsInserted", "spans", "activeMs", "cost", "currency"}


def test_status_reports_that_a_watcher_is_running():
    live = watch.LiveState()
    live.publish({"eventsInserted": 1})
    status = live.status()
    assert status["watching"] is True and status["generation"] == 1
