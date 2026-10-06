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
import shutil
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


def test_a_platform_with_no_scheduler_prints_the_whole_job(monkeypatch, fake_cci,
                                                           tmp_path):
    """No launchd, no Task Scheduler, no crontab: a container, say. Print the
    job -- the same line `cci install` writes elsewhere, `init` first --
    rather than a bare "unsupported", so whatever schedules jobs there can
    run it. The line this replaced had no `init`, and the first upgrade with
    a migration stopped capture on every machine that pasted it."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(scheduler.paths, "LOCAL", scheduler.paths.POSIX)

    assert scheduler.status() == []
    assert not scheduler.supported()

    with pytest.raises(scheduler.Unsupported) as exc:
        scheduler.install(scheduler.INTERVAL, config_dir=tmp_path / "cfg")
    message = str(exc.value)
    assert "*/15 * * * *" in message
    assert f"{fake_cci} init && {fake_cci} ingest" in message
    assert str(tmp_path / "cfg") in message


# ------------------------------------------------------------ linux, cron --


@pytest.fixture
def crontab(tmp_path, monkeypatch) -> Path:
    """Linux with a fake `crontab` whose table is a file in tmp_path.

    The fake speaks the real one's protocol -- `-l` prints the table or fails
    with "no crontab for <user>", `-` replaces it from stdin -- so the code
    under test runs its real subprocess calls.
    """
    table = tmp_path / "crontab.txt"
    exe = tmp_path / "fakebin" / "crontab"
    exe.parent.mkdir()
    exe.write_text(
        "#!/bin/sh\n"
        f"T='{table}'\n"
        'case "$1" in\n'
        '  -l) [ -f "$T" ] || { echo "no crontab for tester" >&2; exit 1; }; cat "$T" ;;\n'
        '  -)  cat > "$T" ;;\n'
        '  *)  exit 2 ;;\n'
        "esac\n"
    )
    exe.chmod(0o755)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(scheduler.paths, "LOCAL", scheduler.paths.POSIX)
    monkeypatch.setattr(scheduler, "_crontab_bin", lambda: str(exe))
    return table


def _ours(table: Path) -> list[str]:
    return [l for l in table.read_text().splitlines()
            if l.endswith(f" {scheduler.CRON_TAG}")]


@pytest.fixture
def cron_daemon(monkeypatch):
    """A running cron daemon, unless a test says otherwise. The real check
    reads /proc, which says nothing useful about a test machine."""
    monkeypatch.setattr(scheduler, "_cron_daemon_running", lambda: True)


def test_linux_install_writes_one_line_and_keeps_the_rest(crontab, fake_cci, tmp_path):
    """A crontab is the user's file. `crontab -` replaces it wholesale, so
    every line that is not ours must come back byte for byte."""
    theirs = ["# my jobs", "MAILTO=me@example.com", "0 3 * * * /usr/bin/backup --all"]
    crontab.write_text("\n".join(theirs) + "\n")

    job_file, cci = scheduler.install(scheduler.INTERVAL, config_dir=tmp_path / "cfg")

    lines = crontab.read_text().splitlines()
    assert lines[:3] == theirs
    assert len(_ours(crontab)) == 1 and len(lines) == 4
    assert job_file is None and cci == fake_cci
    assert scheduler.supported()


def test_other_lines_survive_byte_for_byte(crontab, fake_cci, tmp_path):
    """Decoding, `\\r` translation and `str.splitlines` each rewrite somebody's
    jobs: a carriage return inside a printf, a form feed, a U+2028, a Latin-1
    comment, a table with no final newline. Install and uninstall must hand
    every one of those back untouched."""
    theirs = (b"# caf\xe9 -- latin-1, not utf-8\r\n"
              b"0 3 * * * printf 'a\rb' > /tmp/x\n"
              b"5 4 * * * echo 'form\x0cfeed' 'sep\xe2\x80\xa8arator'\n"
              b"@reboot /usr/bin/thing")
    crontab.write_bytes(theirs)

    scheduler.install(scheduler.INTERVAL, config_dir=tmp_path / "cfg")
    assert crontab.read_bytes().startswith(theirs + b"\n")

    assert scheduler.uninstall() == [scheduler.LABEL]
    assert crontab.read_bytes() == theirs + b"\n"


def test_linux_install_with_no_crontab_yet(crontab, fake_cci, tmp_path):
    """"no crontab for <user>" is the normal first install, not an error."""
    scheduler.install(scheduler.INTERVAL, config_dir=tmp_path / "cfg")
    assert len(_ours(crontab)) == 1


def test_linux_reinstall_replaces_rather_than_duplicates(crontab, fake_cci, tmp_path):
    """Two lines would be two writers on one database."""
    scheduler.install(scheduler.INTERVAL, config_dir=tmp_path / "a")
    scheduler.install(scheduler.INTERVAL, config_dir=tmp_path / "b")
    [line] = _ours(crontab)
    assert str(tmp_path / "b") in line and str(tmp_path / "a") not in line


def test_linux_install_replaces_the_line_people_were_told_to_paste(crontab, fake_cci,
                                                                   tmp_path):
    """Earlier versions printed a line to paste. Left beside ours it is a
    second writer, so it goes -- and only that exact line, for a program
    called cci, and nothing else that merely looks like it."""
    pasted = f"*/15 * * * * {fake_cci} ingest && {fake_cci} derive"
    similar = f"0 * * * * {fake_cci} ingest && {fake_cci} derive && echo mine"
    other_tool = "*/15 * * * * /usr/local/bin/mytool ingest && /usr/local/bin/mytool derive"
    crontab.write_text(f"{pasted}\n{similar}\n{other_tool}\n")

    scheduler.install(scheduler.INTERVAL, config_dir=tmp_path / "cfg")

    lines = crontab.read_text().splitlines()
    assert pasted not in lines and similar in lines and other_tool in lines
    assert len(_ours(crontab)) == 1


def test_an_unreadable_crontab_is_never_overwritten(crontab, fake_cci, tmp_path,
                                                    monkeypatch):
    """A failed read written back is a deleted crontab. Refuse instead."""
    crontab.write_text("0 3 * * * /usr/bin/backup\n")
    real_run = scheduler.subprocess.run

    def flaky(argv, **kw):
        if argv[-1] == "-l":
            return subprocess.CompletedProcess(argv, 1, b"", b"crontab: permission denied")
        return real_run(argv, **kw)

    monkeypatch.setattr(scheduler.subprocess, "run", flaky)
    with pytest.raises(RuntimeError, match="left untouched"):
        scheduler.install(scheduler.INTERVAL, config_dir=tmp_path / "cfg")
    assert crontab.read_text() == "0 3 * * * /usr/bin/backup\n"


@pytest.mark.parametrize("stderr, empty", [
    (b"no crontab for tester", True),                                  # cronie, vixie
    (b"crontab: can't open 'tester': No such file or directory", True),  # busybox
    (b"crontab: can't open 'tester': Permission denied", False),       # busybox, exists
    (b"crontab: can't open 'tester': I/O error", False),
])
def test_only_a_missing_table_counts_as_empty(crontab, fake_cci, tmp_path, monkeypatch,
                                              stderr, empty):
    """busybox says "can't open" for every failure. Only the missing-file one
    means "no crontab yet"; treating Permission denied as empty would write
    our single line over a table that exists."""
    real_run = scheduler.subprocess.run
    monkeypatch.setattr(scheduler.subprocess, "run", lambda argv, **kw:
                        subprocess.CompletedProcess(argv, 1, b"", stderr)
                        if argv[-1] == "-l" else real_run(argv, **kw))
    if empty:
        assert scheduler._cron_read() == []
    else:
        with pytest.raises(RuntimeError, match="left untouched"):
            scheduler.install(scheduler.INTERVAL, config_dir=tmp_path / "cfg")
        assert not crontab.exists()


def test_a_comment_that_mentions_the_label_is_not_ours(crontab, fake_cci, tmp_path):
    comment = f"# TODO remove {scheduler.CRON_TAG}"
    crontab.write_text(comment + "\n")
    scheduler.install(scheduler.INTERVAL, config_dir=tmp_path / "cfg")
    assert scheduler.uninstall() == [scheduler.LABEL]
    assert crontab.read_text() == comment + "\n"


def test_doctor_names_an_unreadable_crontab(crontab, fake_cci, monkeypatch):
    """"not installed" would send somebody to `cci install`, which cannot
    fix a crontab that will not open. Say what is actually wrong."""
    monkeypatch.setattr(scheduler.subprocess, "run", lambda argv, **kw:
                        subprocess.CompletedProcess(argv, 1, b"", b"crontab: must be suid"))
    assert scheduler.cron_error() == "crontab -l: crontab: must be suid"


def test_linux_uninstall_removes_only_ours(crontab, fake_cci, tmp_path):
    crontab.write_text("0 3 * * * /usr/bin/backup\n")
    scheduler.install(scheduler.INTERVAL, config_dir=tmp_path / "cfg")

    assert scheduler.uninstall() == [scheduler.LABEL]
    assert crontab.read_text() == "0 3 * * * /usr/bin/backup\n"
    assert scheduler.uninstall() == []


def test_linux_refuses_watch(crontab, fake_cci, tmp_path):
    """cron starts things and does not keep them up, same as Task Scheduler."""
    with pytest.raises(scheduler.Unsupported, match="macOS-only"):
        scheduler.install(scheduler.WATCH, config_dir=tmp_path / "cfg")
    assert not crontab.exists()


def test_a_path_cron_would_mangle_is_refused(crontab, tmp_path, monkeypatch):
    """`%` means newline to cronie and nothing to busybox; no escaping is
    right for both, so refuse rather than schedule the wrong path."""
    exe = tmp_path / "100% bin" / "cci"
    monkeypatch.setattr(scheduler, "cci_executable", lambda: exe)
    with pytest.raises(RuntimeError, match="'%'"):
        scheduler.install(scheduler.INTERVAL, config_dir=tmp_path / "cfg")
    assert not crontab.exists()


def test_doctor_sees_the_cron_job(crontab, fake_cci, tmp_path, cron_daemon):
    """What made Linux unfinishable before: doctor could not see a job, so
    it sent people back to `cci install` forever."""
    assert scheduler.active() is None
    scheduler.install(scheduler.INTERVAL, config_dir=tmp_path / "cfg")

    job = scheduler.active()
    assert job is not None and job.mode == scheduler.INTERVAL
    assert "sync auto" in scheduler.installed_command(scheduler.INTERVAL)


def test_a_job_with_no_cron_daemon_is_not_running(crontab, fake_cci, tmp_path,
                                                  monkeypatch):
    """WSL without systemd, a container with the package and no init: the
    line is there and nothing will ever read it."""
    monkeypatch.setattr(scheduler, "_cron_daemon_running", lambda: False)
    scheduler.install(scheduler.INTERVAL, config_dir=tmp_path / "cfg")
    [interval, _] = scheduler.status()
    assert interval.installed and not interval.loaded
    assert scheduler.active() is None


def test_a_paused_or_edited_line_never_crashes_status(crontab, fake_cci, cron_daemon):
    """Commented out to pause it: not running. Edited to `@hourly`, or a
    bare comment that happens to end in the tag: read, never an IndexError."""
    tag = scheduler.CRON_TAG
    crontab.write_text(f"#*/15 * * * * /bin/sh -c 'x' {tag}\n")
    assert scheduler.active() is None

    crontab.write_text(f"just {tag}\n")
    assert scheduler.active() is None

    crontab.write_text(f"@hourly /bin/sh -c 'x' {tag}\n")
    assert scheduler.installed_command(scheduler.INTERVAL) == f"/bin/sh -c 'x' {tag}"


@pytest.mark.parametrize("shell", ["/bin/sh", "bash", "zsh", "dash"])
def test_the_cron_line_runs_the_same_pipeline_as_launchd(crontab, tmp_path,
                                                         monkeypatch, shell):
    """Run the written line the way cron does -- `$SHELL -c <command>`, with
    whatever SHELL= the user's table sets -- against a stub cci that records
    what it was asked and in which environment.

    The path has a space, a quote, a `$` and a `#` in it: the shell-quoting
    cases that would make the line run something other than what it says.
    """
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} not installed")
    home = tmp_path / "it's $HOME #1"
    record = tmp_path / "calls.txt"
    exe = home / "bin" / "cci"
    exe.parent.mkdir(parents=True)
    exe.write_text(f'#!/bin/sh\necho "$* home=$CC_INSIGHTS_HOME" >> \'{record}\'\n'
                   'echo out; echo err >&2\n')
    exe.chmod(0o755)
    monkeypatch.setattr(scheduler, "cci_executable", lambda: exe)
    cfg = home / "cfg"

    scheduler.install(scheduler.INTERVAL, config_dir=cfg)

    [line] = _ours(crontab)
    command = line.split(None, 5)[5]
    subprocess.run([shell, "-c", command], check=True, env={"PATH": "/usr/bin:/bin"})

    assert record.read_text().splitlines() == [
        f"{step} home={cfg}" for step in ("init", "ingest", "derive", "sync auto")
    ]
    assert (cfg / "logs" / "ingest.log").read_text() == "out\n" * 4
    assert (cfg / "logs" / "ingest.err").read_text() == "err\n" * 4


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
