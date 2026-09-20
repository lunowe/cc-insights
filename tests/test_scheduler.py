"""Installing the background job, without touching the real one.

Every test here redirects `~/Library/LaunchAgents` into a tmp_path and stubs
`launchctl`, for the obvious reason: this suite runs on a machine with a live
`com.cc-insights` job on it, and a test that unloaded it would silently stop
capturing the history the project exists to capture.

What is worth asserting is narrow but load-bearing:

* the job names an absolute `cci` that exists -- a job pointing at a path
  that moved fails every 15 minutes and reports it to a log nobody reads;
* installing one job removes the other, because two writers on one SQLite
  database is the one self-inflicted failure available here;
* no placeholder survives substitution.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from cc_insights import scheduler

darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="launchd is macOS")


@pytest.fixture
def launchctl_calls() -> list[tuple[str, ...]]:
    """Every launchctl invocation, in order. Kept beside `agents` rather than
    hung off it: `Path` uses __slots__ and will not take an attribute."""
    return []


@pytest.fixture
def agents(tmp_path, monkeypatch, launchctl_calls):
    """A fake LaunchAgents directory and a launchctl that only records."""
    d = tmp_path / "LaunchAgents"
    d.mkdir()
    monkeypatch.setattr(scheduler, "_launch_agents", lambda: d)

    def fake_launchctl(*argv: str) -> int:
        launchctl_calls.append(argv)
        return 0

    monkeypatch.setattr(scheduler, "_launchctl", fake_launchctl)
    monkeypatch.setattr(scheduler, "_launchd_loaded", lambda label: True)
    return d


@pytest.fixture
def fake_cci(tmp_path, monkeypatch):
    exe = tmp_path / "bin" / "cci"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(scheduler, "cci_executable", lambda: exe)
    return exe


# ------------------------------------------------------------ the binary --


def test_it_prefers_the_cci_beside_this_interpreter(tmp_path, monkeypatch):
    """A venv's `cci install` must schedule that venv's cci.

    Taking it off PATH instead would let a different installation shadow it,
    and the job would then write a database this CLI never reads.
    """
    fake_bin = tmp_path / "venv" / "bin"
    fake_bin.mkdir(parents=True)
    exe = fake_bin / "cci"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)

    monkeypatch.setattr(sys, "executable", str(fake_bin / "python"))
    monkeypatch.setattr(scheduler.shutil, "which", lambda _: "/usr/local/bin/cci")

    assert scheduler.cci_executable() == exe.resolve()


def test_it_falls_back_to_path(tmp_path, monkeypatch):
    other = tmp_path / "elsewhere" / "cci"
    other.parent.mkdir(parents=True)
    other.write_text("#!/bin/sh\n")
    other.chmod(0o755)

    monkeypatch.setattr(sys, "executable", str(tmp_path / "nowhere" / "python"))
    monkeypatch.setattr(scheduler.shutil, "which", lambda _: str(other))

    assert scheduler.cci_executable() == other.resolve()


def test_no_cci_anywhere_says_how_to_get_one(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "executable", str(tmp_path / "nowhere" / "python"))
    monkeypatch.setattr(scheduler.shutil, "which", lambda _: None)

    with pytest.raises(scheduler.Unsupported, match="pip install -e"):
        scheduler.cci_executable()


# ------------------------------------------------------------ installing --


@darwin_only
def test_install_writes_a_valid_plist_naming_the_real_cci(agents, fake_cci, tmp_path):
    job, cci = scheduler.install(scheduler.INTERVAL, config_dir=tmp_path)

    assert job is not None and job.exists()
    assert cci == fake_cci

    d = plistlib.loads(job.read_bytes())
    assert d["Label"] == scheduler.LABEL
    assert d["StartInterval"] == 900
    # `init` leads so a migration cannot stop capture; `sync auto` trails so
    # an unreachable server cannot. Both ends of the line are load-bearing.
    assert d["ProgramArguments"][2] == (
        f"{fake_cci} init && {fake_cci} ingest && {fake_cci} derive "
        f"&& {fake_cci} sync auto"
    )
    assert d["StandardOutPath"] == str(tmp_path / "logs" / "ingest.log")


@darwin_only
def test_no_placeholder_survives(agents, fake_cci, tmp_path):
    """An unsubstituted __CCI__ is a job that runs a nonexistent command."""
    job, _ = scheduler.install(scheduler.INTERVAL, config_dir=tmp_path)
    text = job.read_text()
    assert "__CCI__" not in text and "__LOGDIR__" not in text
    assert "__CONFIGDIR__" not in text


@darwin_only
def test_the_log_directory_is_created(agents, fake_cci, tmp_path):
    """launchd does not create it, and a job whose StandardOutPath cannot be
    opened does not run at all."""
    logs = tmp_path / "deep" / "logs"
    scheduler.install(scheduler.INTERVAL, config_dir=tmp_path, log_dir=logs)
    assert logs.is_dir()


@darwin_only
def test_installing_watch_removes_the_interval_job(agents, fake_cci, tmp_path):
    """Two writers on one database is the failure this ordering prevents."""
    scheduler.install(scheduler.INTERVAL, config_dir=tmp_path)
    assert (agents / f"{scheduler.LABEL}.plist").exists()

    scheduler.install(scheduler.WATCH, config_dir=tmp_path)
    assert not (agents / f"{scheduler.LABEL}.plist").exists()
    assert (agents / f"{scheduler.WATCH_LABEL}.plist").exists()


@darwin_only
def test_installing_interval_removes_the_watch_job(agents, fake_cci, tmp_path):
    scheduler.install(scheduler.WATCH, config_dir=tmp_path)
    scheduler.install(scheduler.INTERVAL, config_dir=tmp_path)
    assert not (agents / f"{scheduler.WATCH_LABEL}.plist").exists()
    assert (agents / f"{scheduler.LABEL}.plist").exists()


@darwin_only
def test_the_watch_job_keeps_one_process_alive(agents, fake_cci, tmp_path):
    job, cci = scheduler.install(scheduler.WATCH, config_dir=tmp_path)
    d = plistlib.loads(job.read_bytes())
    assert d["KeepAlive"] is True
    assert "StartInterval" not in d
    assert d["ProgramArguments"][2] == f"{cci} init && exec {cci} watch --quiet"


@darwin_only
def test_reinstalling_unloads_before_loading(agents, fake_cci, tmp_path,
                                             launchctl_calls):
    """Loading over a loaded job leaves launchd with the old definition."""
    scheduler.install(scheduler.INTERVAL, config_dir=tmp_path)
    launchctl_calls.clear()
    scheduler.install(scheduler.INTERVAL, config_dir=tmp_path)

    verbs = [c[0] for c in launchctl_calls]
    assert verbs.index("load") > 0
    assert "unload" in verbs[: verbs.index("load")]


def test_an_unknown_mode_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown job mode"):
        scheduler.install("hourly", config_dir=tmp_path)


# ---------------------------------------------------------- uninstalling --


@darwin_only
def test_uninstall_removes_both_jobs(agents, fake_cci, tmp_path):
    """Whichever is loaded must go, not only the one that was asked about --
    otherwise an upgrade can leave two writers behind."""
    (agents / f"{scheduler.LABEL}.plist").write_text("x")
    (agents / f"{scheduler.WATCH_LABEL}.plist").write_text("x")

    removed = scheduler.uninstall()

    assert set(removed) == {scheduler.LABEL, scheduler.WATCH_LABEL}
    assert not list(agents.glob("*.plist"))


@darwin_only
def test_uninstall_with_nothing_installed_is_not_an_error(agents, fake_cci):
    assert scheduler.uninstall() == []


# ---------------------------------------------------------------- status --


@darwin_only
def test_status_distinguishes_installed_from_loaded(agents, fake_cci, tmp_path,
                                                    monkeypatch):
    """The interesting failure: the file is there and launchd is not running it.

    A job that is installed but not loaded looks fine to anyone who checks by
    listing ~/Library/LaunchAgents, and captures nothing.
    """
    scheduler.install(scheduler.INTERVAL, config_dir=tmp_path)
    monkeypatch.setattr(scheduler, "_launchd_loaded", lambda label: False)

    interval = next(j for j in scheduler.status() if j.mode == scheduler.INTERVAL)
    assert interval.installed and not interval.loaded
    assert not interval.healthy
    assert scheduler.active() is None


@darwin_only
def test_active_names_the_running_job(agents, fake_cci, tmp_path):
    scheduler.install(scheduler.WATCH, config_dir=tmp_path)
    job = scheduler.active()
    assert job is not None and job.mode == scheduler.WATCH


# ------------------------------------------------- unsupported platforms --


def test_an_unsupported_platform_prints_a_usable_cron_line(monkeypatch, fake_cci):
    """"Unsupported" on its own leaves a Linux user with nothing. The cron
    line is a complete substitute for the interval job, so print it."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(scheduler.paths, "LOCAL", scheduler.paths.POSIX)

    assert scheduler.status() == []
    assert not scheduler.supported()

    with pytest.raises(scheduler.Unsupported) as exc:
        scheduler.install(scheduler.INTERVAL, config_dir=Path("/tmp/x"))
    message = str(exc.value)
    assert "crontab" in message
    assert "*/15 * * * *" in message
    assert str(fake_cci) in message


@darwin_only
def test_a_malformed_template_fails_at_install_time(agents, fake_cci, tmp_path,
                                                    monkeypatch):
    """launchd rejects a bad plist with a message that does not name the
    problem, and the job then just never runs. Catch it here instead."""
    if not scheduler.shutil.which("plutil"):
        pytest.skip("needs plutil")
    monkeypatch.setattr(
        scheduler.assets, "job_template", lambda name: "<plist>not xml at all"
    )
    with pytest.raises(RuntimeError, match="invalid"):
        scheduler.install(scheduler.INTERVAL, config_dir=tmp_path)
    assert not list(agents.glob("*.plist")), "an invalid plist must not be left behind"


@darwin_only
def test_the_installed_plist_matches_the_shell_installer(tmp_path, agents, fake_cci):
    """`cci install` and `scripts/install-launchd.sh` must produce the same
    file, or which one you used changes what runs."""
    job, cci = scheduler.install(scheduler.INTERVAL, config_dir=tmp_path)

    from cc_insights import assets
    by_hand = (assets.job_template("com.cc-insights.plist")
               .replace("__CCI__", str(cci))
               .replace("__LOGDIR__", str(tmp_path / "logs"))
               .replace("__CONFIGDIR__", str(tmp_path)))
    assert job.read_text() == by_hand


@darwin_only
def test_plutil_accepts_what_install_writes(agents, fake_cci, tmp_path):
    if not scheduler.shutil.which("plutil"):
        pytest.skip("needs plutil")
    job, _ = scheduler.install(scheduler.INTERVAL, config_dir=tmp_path)
    assert subprocess.run(["plutil", "-lint", str(job)],
                          capture_output=True).returncode == 0


# ------------------------------------------------ surviving an upgrade --


@darwin_only
@pytest.mark.parametrize("mode", [scheduler.INTERVAL, scheduler.WATCH])
def test_every_job_migrates_before_it_reads(agents, fake_cci, tmp_path, mode):
    """A migration in a new release must not silently stop capture.

    This happened for real. Adding migration 006 put the repo at schema 6
    while the installed database was on 5; every command refuses a database
    older than the code, so the 15-minute job failed every run. The refusal
    went to ingest.err, which nobody reads, and capture was dead for as long
    as it took someone to notice -- while the agent logs that would have
    filled the gap were pruned on a rolling basis.

    `cci init` is idempotent and additive, so leading with it costs one query
    per run and makes the job self-healing across upgrades.
    """
    job, cci = scheduler.install(mode, config_dir=tmp_path)
    d = plistlib.loads(job.read_bytes())
    command = d["ProgramArguments"][2]

    assert command.startswith(f"{cci} init &&"), command
    verb = "watch" if mode == scheduler.WATCH else "ingest"
    assert command.index("init") < command.index(verb)


@darwin_only
def test_the_watch_job_execs_so_keepalive_supervises_watch(agents, fake_cci, tmp_path):
    """Without `exec`, launchd watches a /bin/sh that has already forked, so
    KeepAlive would restart the shell and lose track of the real process."""
    job, cci = scheduler.install(scheduler.WATCH, config_dir=tmp_path)
    d = plistlib.loads(job.read_bytes())
    assert f"exec {cci} watch" in d["ProgramArguments"][2]


# ------------------------------------------------- the database it fills --


@darwin_only
@pytest.mark.parametrize("mode", [scheduler.INTERVAL, scheduler.WATCH])
def test_the_job_captures_into_the_config_dir_it_was_installed_from(
        agents, fake_cci, tmp_path, mode):
    """The worst failure this module had: two databases, no error.

    launchd agents do not read your shell rc, so `CC_INSIGHTS_HOME=/data/cci
    cci install` used to write a plist that logged to /data/cci/logs while
    the job itself ran a bare `cci` that fell back to ~/.config/cc-insights
    and created a second database there. `cci doctor` and `cci serve` read
    /data/cci and said "last ingest: never" for as long as anyone left it,
    while the history piled up somewhere they had no reason to look.
    """
    elsewhere = tmp_path / "data" / "cci"
    job, _ = scheduler.install(mode, config_dir=elsewhere)

    d = plistlib.loads(job.read_bytes())
    assert d["EnvironmentVariables"]["CC_INSIGHTS_HOME"] == str(elsewhere)
    # and the logs are in the same place, because the split between the two
    # is the bug, not either half of it.
    assert d["StandardOutPath"].startswith(str(elsewhere / "logs"))


@darwin_only
def test_an_explicit_log_dir_does_not_move_the_database(agents, fake_cci, tmp_path):
    """Overriding where it logs must not change where it writes."""
    job, _ = scheduler.install(scheduler.INTERVAL, config_dir=tmp_path / "cfg",
                               log_dir=tmp_path / "logs-elsewhere")
    d = plistlib.loads(job.read_bytes())
    assert d["EnvironmentVariables"]["CC_INSIGHTS_HOME"] == str(tmp_path / "cfg")
    assert d["StandardOutPath"] == str(tmp_path / "logs-elsewhere" / "ingest.log")


# ------------------------------------------------------- writing the file --


@darwin_only
def test_a_failed_write_leaves_the_working_job_in_place(agents, fake_cci, tmp_path,
                                                        monkeypatch):
    """The plist must never be observed half-written.

    `write_text` truncates in place: a crash between the truncate and the
    write leaves a zero-byte plist where a working job was, launchd refuses
    it at the next login without saying anything, and capture stops. The
    write therefore goes to a temporary file and lands with one `os.replace`
    -- so anything that fails before that point changes nothing at all.
    """
    job, _ = scheduler.install(scheduler.INTERVAL, config_dir=tmp_path)
    good = job.read_text()

    def explode(src, dst):
        raise OSError("interrupted between the truncate and the write")

    monkeypatch.setattr(scheduler.os, "replace", explode)
    with pytest.raises(OSError):
        scheduler.install(scheduler.INTERVAL, config_dir=tmp_path)

    assert job.read_text() == good, "the old job must survive a failed rewrite"
    assert [p.name for p in agents.iterdir()] == [job.name], \
        "a temporary file was left behind for launchd to trip over"


# ------------------------------------------------- awkward home directories --


@darwin_only
def test_a_home_directory_xml_would_choke_on_still_installs(agents, tmp_path,
                                                            monkeypatch):
    """`/Users/Tom & Jerry/...` is a legal path and an illegal XML token.

    Unescaped, it failed with `not well-formed (invalid token): line 16,
    column 28`, which names neither the home directory nor anything the user
    could do about it -- and they cannot rename their account.
    """
    home = tmp_path / "Tom & Jerry <lab>"
    exe = home / ".local" / "bin" / "cci"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(scheduler, "cci_executable", lambda: exe)

    job, _ = scheduler.install(scheduler.INTERVAL, config_dir=home / ".config")

    d = plistlib.loads(job.read_bytes())
    # Escaped on the way in, and therefore unescaped on the way out: what
    # launchd runs has to be the real path, not `&amp;`.
    assert str(exe) in d["ProgramArguments"][2]
    assert d["EnvironmentVariables"]["CC_INSIGHTS_HOME"] == str(home / ".config")
    assert d["StandardOutPath"] == str(home / ".config" / "logs" / "ingest.log")


# --------------------------------------------------------------- windows --


@pytest.fixture
def windows(monkeypatch) -> list[list[str]]:
    """Pretend to be Windows, and record what the .ps1 would be invoked with."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(scheduler.paths, "LOCAL", scheduler.paths.WINDOWS)
    monkeypatch.setattr(scheduler, "_powershell", lambda: "pwsh")

    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)
    return calls


def test_windows_install_passes_only_flags_the_script_declares(windows, fake_cci,
                                                               tmp_path):
    """A flag the .ps1 does not declare is a terminating error, not a warning.

    `-Watch` was passed to a `param()` block that had never heard of it.
    PowerShell rejects an unknown parameter outright, so `cci install
    --watch` exited 1 on every Windows machine -- and nothing in the test
    suite compared the two sides.
    """
    scheduler.install(scheduler.INTERVAL, config_dir=tmp_path / "cfg")
    scheduler.uninstall()

    passed = {arg for call in windows for arg in call if arg.startswith("-")
              and arg not in {"-ExecutionPolicy", "-File", "-NoProfile", "-Command"}}
    declared = _declared_parameters()
    assert passed, "the test learned nothing: no flags reached the script"
    assert passed <= declared, f"install-task.ps1 does not declare {passed - declared}"


def _declared_parameters() -> set[str]:
    """The `param()` names install-task.ps1 will accept, as flags."""
    import re
    text = (scheduler.assets.JOBS / "install-task.ps1").read_text()
    block = text.split("param(", 1)[1].split(")", 1)[0]
    return {f"-{name}" for name in re.findall(r"\$(\w+)", block)}


def test_windows_install_names_the_config_dir(windows, fake_cci, tmp_path):
    """Same split as the launchd job: a scheduled task inherits the account's
    environment, not the shell's, so the directory has to be passed."""
    scheduler.install(scheduler.INTERVAL, config_dir=tmp_path / "cfg")
    argv = windows[0]
    assert "-ConfigDir" in argv
    assert argv[argv.index("-ConfigDir") + 1] == str(tmp_path / "cfg")


def test_windows_refuses_watch_and_says_what_to_run_instead(windows, fake_cci,
                                                            tmp_path):
    """Task Scheduler cannot keep a process up, so there is no watch job.

    Refusing is the honest answer; the previous one was a PowerShell binding
    error surfaced as `RuntimeError` and an exit code of 1.
    """
    with pytest.raises(scheduler.Unsupported) as exc:
        scheduler.install(scheduler.WATCH, config_dir=tmp_path / "cfg")

    message = str(exc.value)
    assert "cci install" in message and "macOS" in message
    assert windows == [], "nothing may be registered when the mode is refused"


def test_windows_uninstall_reports_only_what_was_there(windows, fake_cci,
                                                       monkeypatch):
    """"removed com.cc-insights" on a machine that had no task is the one
    answer that stops somebody looking for the job still running."""
    monkeypatch.setattr(scheduler, "_status_task", lambda mode: scheduler.JobStatus(
        mode=mode, label=scheduler._LABELS[mode], installed=False, loaded=False))
    assert scheduler.uninstall() == []

    monkeypatch.setattr(scheduler, "_status_task", lambda mode: scheduler.JobStatus(
        mode=mode, label=scheduler._LABELS[mode], installed=True, loaded=True))
    assert scheduler.uninstall() == [scheduler.LABEL]
