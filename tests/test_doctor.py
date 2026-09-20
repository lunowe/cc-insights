"""`cci doctor` -- the command whose job is to notice a silent failure.

The bar here is higher than "it prints something". Doctor exists because the
way this tool breaks is by quietly not capturing, and a health check that
reports "ok" while capture is dead is strictly worse than no health check:
it converts an unnoticed problem into a *confirmed* non-problem, and the
agent logs that would have closed the gap are pruned in the meantime.

So the tests that matter are the ones where something is wrong and doctor has
to say so -- no job, a job installed but not loaded, a stale ingest, a
pending migration. The happy path is one test.
"""

from __future__ import annotations

import time

import pytest

from cc_insights import config as config_mod, db, doctor, scheduler

LEVELS = {doctor.OK, doctor.WARN, doctor.FAIL}


@pytest.fixture
def cfg(tmp_path):
    """A real config and a migrated database in a tmp directory."""
    c = config_mod.load(tmp_path, create=True)
    conn = db.connect(c.db_path)
    db.migrate(conn)
    conn.close()
    return c


@pytest.fixture(autouse=True)
def no_real_scheduler(monkeypatch):
    """Never consult the live launchd. Default: a healthy interval job.

    Without this the suite's verdict would depend on whether the developer
    running it happens to have the job installed, which is exactly the kind
    of result that is worth nothing.
    """
    healthy = scheduler.JobStatus(
        mode=scheduler.INTERVAL, label=scheduler.LABEL, installed=True, loaded=True
    )
    monkeypatch.setattr(scheduler, "supported", lambda: True)
    monkeypatch.setattr(scheduler, "status", lambda: [healthy])
    monkeypatch.setattr(scheduler, "active", lambda: healthy)


def check(checks, name) -> doctor.Check:
    return next(c for c in checks if c.name == name)


def set_last_ingest(cfg, ms: int) -> None:
    conn = db.connect(cfg.db_path)
    conn.execute(
        "INSERT INTO ingest_file (host_id, path, source, size_bytes, mtime_ms,"
        " bytes_read, lines_read, last_ingest) VALUES (?,?,?,?,?,?,?,?)",
        (cfg.host_id, "/tmp/a.jsonl", "claude_code", 1, ms, 1, 1, ms),
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------------- capture --


def test_a_missing_job_is_a_failure_not_a_note(cfg, monkeypatch):
    """The whole point of the tool is that it runs unattended. No job is the
    most serious thing doctor can find, and the loss is unrecoverable."""
    monkeypatch.setattr(scheduler, "status", lambda: [])
    monkeypatch.setattr(scheduler, "active", lambda: None)

    c = check(doctor.run(cfg), "background job")
    assert c.level == doctor.FAIL
    assert "pruned" in c.detail
    assert c.fix == "cci install"


def test_installed_but_not_loaded_is_caught(cfg, monkeypatch):
    """The nastiest state: the plist is in ~/Library/LaunchAgents, so anyone
    checking by eye sees it, and launchd is not running it."""
    stopped = scheduler.JobStatus(
        mode=scheduler.INTERVAL, label=scheduler.LABEL, installed=True, loaded=False
    )
    monkeypatch.setattr(scheduler, "status", lambda: [stopped])
    monkeypatch.setattr(scheduler, "active", lambda: None)

    c = check(doctor.run(cfg), "background job")
    assert c.level == doctor.FAIL
    assert "not loaded" in c.detail


def test_a_running_job_is_named_with_its_cadence(cfg):
    c = check(doctor.run(cfg), "background job")
    assert c.level == doctor.OK
    assert scheduler.LABEL in c.detail
    assert "15 minutes" in c.detail


def test_a_watch_job_is_described_as_live(cfg, monkeypatch):
    job = scheduler.JobStatus(
        mode=scheduler.WATCH, label=scheduler.WATCH_LABEL, installed=True, loaded=True
    )
    monkeypatch.setattr(scheduler, "status", lambda: [job])
    monkeypatch.setattr(scheduler, "active", lambda: job)

    assert "live" in check(doctor.run(cfg), "background job").detail


def test_an_unsupported_platform_warns_rather_than_fails(cfg, monkeypatch):
    """No integration is not the same as a broken install, and a Linux user
    running the cron line should not be told their setup has failed."""
    monkeypatch.setattr(scheduler, "supported", lambda: False)
    assert check(doctor.run(cfg), "background job").level == doctor.WARN


# ------------------------------------------------------------- freshness --


def test_never_ingested_with_logs_waiting_is_a_failure(cfg, monkeypatch):
    """Logs are on disk and none have been read: something is actually wrong.

    The count is stubbed rather than measured, because the real globs point
    at ~/.claude and this suite runs on a machine that has one. A test whose
    verdict depends on the developer's home directory proves nothing.
    """
    monkeypatch.setattr(doctor, "source_files", lambda cfg: 744)
    c = check(doctor.run(cfg), "last ingest")
    assert c.level == doctor.FAIL
    assert "never" in c.detail and "744" in c.detail
    assert c.fix == "cci ingest"


def test_nothing_to_read_is_not_a_broken_install(cfg, monkeypatch):
    """The first-run case, and it must not be red.

    A fresh machine whose agents have not written anything yet ran ingest
    successfully and found zero files. `ingest_file` gets no row either way,
    so freshness alone reports "never" and the very first `cci doctor` after
    `curl | sh` says the install FAILED when nothing is wrong. Found by
    running the installer in a sandboxed HOME with no agent logs in it.
    """
    monkeypatch.setattr(doctor, "source_files", lambda cfg: 0)
    checks = doctor.run(cfg)

    assert not any(c.name == "last ingest" for c in checks)
    c = check(checks, "agent logs")
    assert c.level == doctor.WARN
    assert "nothing to capture yet" in c.detail
    # And the whole report must not be a failure on that account.
    assert doctor.worst([x for x in checks if x.name != "background job"]) == doctor.WARN


def test_an_unanswerable_glob_does_not_crash_or_invent_a_count(cfg, monkeypatch):
    monkeypatch.setattr(doctor, "source_files", lambda cfg: -1)
    c = check(doctor.run(cfg), "last ingest")
    assert c.level == doctor.FAIL
    assert c.detail == "never"          # no fabricated "-1 log files are waiting"


def test_source_files_counts_what_the_globs_actually_match(tmp_path, monkeypatch):
    """The real function, against a directory this test controls."""
    logs = tmp_path / "logs"
    logs.mkdir()
    for i in range(3):
        (logs / f"s{i}.jsonl").write_text("")

    c = config_mod.load(tmp_path / "cfg", create=True)
    c.source_globs = {"claude_code": [str(logs / "*.jsonl")]}
    assert doctor.source_files(c) == 3

    c.source_globs = {"claude_code": [str(tmp_path / "nowhere" / "*.jsonl")]}
    assert doctor.source_files(c) == 0


def test_a_recent_ingest_is_ok(cfg):
    set_last_ingest(cfg, int(time.time() * 1000) - 60_000)
    c = check(doctor.run(cfg), "last ingest")
    assert c.level == doctor.OK
    assert "ago" in c.detail


def test_a_few_missed_runs_warn(cfg):
    """Four missed 15-minute runs: past coincidence, short of panic."""
    set_last_ingest(cfg, int((time.time() - 2 * 3600) * 1000))
    assert check(doctor.run(cfg), "last ingest").level == doctor.WARN


def test_a_day_without_an_ingest_fails(cfg):
    """Long enough that agent logs may already have aged out underneath it."""
    set_last_ingest(cfg, int((time.time() - 3 * 86_400) * 1000))
    c = check(doctor.run(cfg), "last ingest")
    assert c.level == doctor.FAIL
    assert "d ago" in c.detail


def test_freshness_measures_the_last_look_not_the_last_event(cfg):
    # (an ingest_file row exists below, so the source_files branch is not taken)
    """A quiet week and a broken install look identical if you measure the
    newest event. They are not the same thing, and only one is urgent."""
    set_last_ingest(cfg, int(time.time() * 1000))
    checks = doctor.run(cfg)
    assert check(checks, "last ingest").level == doctor.OK
    # No events at all, and capture is still healthy.
    assert check(checks, "data").level == doctor.WARN


# ---------------------------------------------------------------- schema --


def test_a_missing_database_says_run_init(tmp_path):
    c = config_mod.load(tmp_path, create=True)
    checks = doctor.run(c)
    assert check(checks, "database").level == doctor.FAIL
    assert check(checks, "database").fix == "cci init"


def test_a_pending_migration_is_a_failure(tmp_path, monkeypatch):
    """An out-of-date schema is the reason the next command will fail, and
    saying so here saves reading a traceback about a missing column."""
    c = config_mod.load(tmp_path, create=True)
    conn = db.connect(c.db_path)
    db.migrate(conn, only_through=1)
    conn.close()

    checks = doctor.run(c)
    assert check(checks, "schema").level == doctor.FAIL
    assert "pending" in check(checks, "schema").detail


def test_an_up_to_date_schema_names_its_version(cfg):
    c = check(doctor.run(cfg), "schema")
    assert c.level == doctor.OK
    assert "through" in c.detail


# ------------------------------------------------------------------ misc --


def test_a_sync_url_without_the_driver_fails(cfg, monkeypatch):
    """Configured-but-broken is worth flagging: `cci sync` would be the only
    way to find out, and nobody runs it until they need it."""
    monkeypatch.setenv("CC_INSIGHTS_SYNC_URL", "postgresql://x@y/z")
    monkeypatch.setitem(__import__("sys").modules, "psycopg", None)

    import builtins
    real = builtins.__import__

    def no_psycopg(name, *a, **k):
        if name == "psycopg":
            raise ModuleNotFoundError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_psycopg)
    assert check(doctor.run(cfg), "sync").level == doctor.FAIL


def test_no_sync_url_is_fine(cfg, monkeypatch):
    monkeypatch.delenv("CC_INSIGHTS_SYNC_URL", raising=False)
    c = check(doctor.run(cfg), "sync")
    assert c.level == doctor.OK
    assert "this machine only" in c.detail


def test_the_sync_check_never_prints_the_url(cfg, monkeypatch):
    """It usually carries a password -- config.py says so, and doctor output
    is the kind of thing that gets pasted into an issue."""
    monkeypatch.setenv("CC_INSIGHTS_SYNC_URL", "postgresql://user:hunter2@host/cci")
    c = check(doctor.run(cfg), "sync")
    assert "hunter2" not in c.detail and "host" not in c.detail


def test_worst_is_the_most_severe_level_present():
    mk = lambda lvl: doctor.Check("x", lvl, "")
    assert doctor.worst([mk(doctor.OK), mk(doctor.WARN)]) == doctor.WARN
    assert doctor.worst([mk(doctor.WARN), mk(doctor.FAIL)]) == doctor.FAIL
    assert doctor.worst([mk(doctor.OK)]) == doctor.OK


def test_every_check_has_a_known_level_and_a_fix_when_not_ok(cfg):
    """An unactionable failure tells someone their install is broken and
    leaves them there. Every non-ok check must name the next command."""
    for c in doctor.run(cfg):
        assert c.level in LEVELS
        if c.level != doctor.OK:
            assert c.fix, f"{c.name} is {c.level} with no suggested fix"


def test_doctor_never_raises_on_a_broken_install(tmp_path):
    """It is the command you reach for when things are wrong, so it has to
    survive states every other command refuses to run in."""
    c = config_mod.load(tmp_path, create=True)
    c.db_path.write_text("this is not a database")
    doctor.run(c)                    # must not raise


# ------------------------------------------------------------------- CLI --


def test_the_cli_exits_nonzero_only_on_failure(cfg, monkeypatch, capsys):
    """The exit code is what makes doctor usable from a script."""
    from cc_insights import cli

    set_last_ingest(cfg, int(time.time() * 1000))
    args = cli.build_parser().parse_args(["--config-dir", str(cfg.config_dir), "doctor"])

    assert args.fn(args) == 0           # warnings present, but nothing failed
    assert "working" in capsys.readouterr().out

    monkeypatch.setattr(scheduler, "status", lambda: [])
    monkeypatch.setattr(scheduler, "active", lambda: None)
    assert args.fn(args) == 1
    assert "something is wrong" in capsys.readouterr().out
