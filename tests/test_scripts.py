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

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
PLIST = SCRIPTS / "com.cc-insights.plist"
INSTALL = SCRIPTS / "install-launchd.sh"
INSTALL_TASK = SCRIPTS / "install-task.ps1"


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
    assert d["ProgramArguments"][2] == "/opt/cci ingest && /opt/cci derive"
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
