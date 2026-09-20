"""The scheduler templates are the only artifacts that touch the user's
machine, so they are checked rather than eyeballed.

The PowerShell job can only be checked as text here: there is no `pwsh` on the
machines this suite runs on, so what follows asserts parity with the launchd
job (same commands, same cadence, same log files, an uninstall path) and not
that the script executes. Executing it is a manual step on Windows -- see
README."""

import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

from cc_insights import assets

# The templates are package data, not repo scripts: `cci install` has to
# write a launchd job on a machine that only ever ran `pipx install`, so they
# live beside the code and ship in the wheel. `assets.JOBS` is the one place
# that knows where -- reading them from `scripts/` here would let the package
# and the test drift apart in exactly the way that hid the missing dashboard.
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
PLIST = assets.JOBS / "com.cc-insights.plist"
INSTALL = SCRIPTS / "install-launchd.sh"
INSTALL_TASK = assets.JOBS / "install-task.ps1"


def test_template_carries_both_placeholders():
    text = PLIST.read_text()
    assert "__CCI__" in text and "__LOGDIR__" in text


def test_substituted_plist_is_valid_and_correct(tmp_path):
    out = tmp_path / "out.plist"
    out.write_text(
        PLIST.read_text().replace("__CCI__", "/opt/cci").replace("__LOGDIR__", "/var/log/cci")
    )
    with out.open("rb") as fh:
        d = plistlib.load(fh)

    assert d["Label"] == "com.cc-insights"
    assert d["StartInterval"] == 900
    assert d["RunAtLoad"] is True
    assert d["ProgramArguments"][:2] == ["/bin/sh", "-c"]
    # The && must survive as a shell operator, not stay XML-escaped.
    # `init` leads so an upgrade that adds a migration cannot stop the job.
    assert d["ProgramArguments"][2] == (
        "/opt/cci init && /opt/cci ingest && /opt/cci derive"
    )
    assert d["StandardOutPath"] == "/var/log/cci/ingest.log"
    assert d["StandardErrorPath"] == "/var/log/cci/ingest.err"
    assert "__CCI__" not in str(d) and "__LOGDIR__" not in str(d)


@pytest.mark.skipif(not shutil.which("plutil"), reason="macOS only")
def test_plutil_accepts_the_template():
    assert subprocess.run(["plutil", "-lint", str(PLIST)], capture_output=True).returncode == 0


def test_install_script_is_valid_bash_and_has_an_uninstall_path():
    assert subprocess.run(["bash", "-n", str(INSTALL)], capture_output=True).returncode == 0
    assert "--uninstall" in INSTALL.read_text()
    assert INSTALL.stat().st_mode & 0o111, "install script must be executable"


# ------------------------------------------------------- windows: the task --


def test_the_windows_task_matches_the_launchd_job():
    """Two schedulers, one contract. A divergence here is a silent data gap."""
    ps = INSTALL_TASK.read_text()
    plist = PLIST.read_text()

    assert "-Uninstall" in ps, "there must be a way back off the machine"
    assert "Unregister-ScheduledTask" in ps

    # Same cadence as StartInterval=900, spelled in minutes.
    assert "$IntervalMinutes = 15" in ps
    assert "<integer>900</integer>" in plist

    # Same two commands, in the same order, short-circuiting the same way: a
    # failed ingest must not be followed by a derive over half-written rows.
    assert ps.index("ingest") < ps.index("derive")
    assert "&&" in ps

    # Same two log files, so `cci`'s config directory is the one place to look.
    assert "'ingest.log'" in ps and "'ingest.err'" in ps
    assert "ingest.log" in plist and "ingest.err" in plist


def test_the_windows_task_respects_the_config_dir_override():
    """Logging somewhere the CLI never looks is worse than not logging."""
    ps = INSTALL_TASK.read_text()
    assert "CC_INSIGHTS_HOME" in ps
    assert "'cc-insights'" in ps and "APPDATA" in ps


def test_the_windows_task_never_asks_for_elevation():
    """It reads one user's logs; it has no business running as anyone else."""
    ps = INSTALL_TASK.read_text()
    assert "RunLevel Highest" not in ps
    assert "-LogonType Interactive" in ps
# --------------------------------------------------------------------------
# the watch job
# --------------------------------------------------------------------------
WATCH_PLIST = assets.JOBS / "com.cc-insights.watch.plist"


def test_the_watch_template_carries_both_placeholders():
    text = WATCH_PLIST.read_text()
    assert "__CCI__" in text and "__LOGDIR__" in text


def test_the_substituted_watch_plist_keeps_one_process_alive(tmp_path):
    out = tmp_path / "watch.plist"
    out.write_text(
        WATCH_PLIST.read_text().replace("__CCI__", "/opt/cci").replace("__LOGDIR__", "/var/log/cci")
    )
    with out.open("rb") as fh:
        d = plistlib.load(fh)

    assert d["Label"] == "com.cc-insights.watch"
    # A long-running job, not an interval one: KeepAlive instead of
    # StartInterval, or launchd would start a second copy every 15 minutes.
    assert d["KeepAlive"] is True
    assert "StartInterval" not in d
    assert d["ThrottleInterval"] == 30
    assert d["ProgramArguments"][:2] == ["/bin/sh", "-c"]
    # `exec` so KeepAlive supervises watch itself rather than the shell.
    assert d["ProgramArguments"][2] == "/opt/cci init && exec /opt/cci watch --quiet"
    assert d["StandardOutPath"] == "/var/log/cci/watch.log"
    assert "__CCI__" not in str(d) and "__LOGDIR__" not in str(d)


def test_the_two_jobs_have_different_labels_and_logs():
    """Installing one must be able to unload the other by label; sharing
    either would make two writers on one database indistinguishable."""
    interval = plistlib.loads(PLIST.read_text().replace("__CCI__", "x")
                              .replace("__LOGDIR__", "/l").encode())
    watch = plistlib.loads(WATCH_PLIST.read_text().replace("__CCI__", "x")
                           .replace("__LOGDIR__", "/l").encode())
    assert interval["Label"] != watch["Label"]
    assert interval["StandardOutPath"] != watch["StandardOutPath"]


def test_the_installer_forwards_every_mode_to_cci_install():
    """The script is a wrapper now; the logic lives in `cci install`.

    It had to move: someone who ran `pipx install cc-insights` has no
    checkout to run a script from, and two implementations of "which job is
    loaded" would eventually disagree about the one thing that must not be
    wrong. What is left here is finding `cci` when PATH does not have it,
    which in a checkout it usually does not.

    "--uninstall removes BOTH jobs" is still enforced, one layer down --
    see test_scheduler.py::test_uninstall_removes_both_jobs.
    """
    text = INSTALL.read_text()
    for mode, forwarded in (
        ("--uninstall", '"$CCI" install --uninstall'),
        ("--watch", '"$CCI" install --watch'),
    ):
        assert mode in text and forwarded in text, f"{mode} is not forwarded"

    # The bare case must not silently become a no-op.
    assert '"")          exec "$CCI" install ;;' in text

    # And it must still find a venv's cci, which is the only reason it exists.
    assert ".venv/bin/cci" in text
