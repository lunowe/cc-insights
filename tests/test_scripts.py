"""The launchd template is the only artifact that touches the user's machine,
so it is checked rather than eyeballed."""

import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
PLIST = SCRIPTS / "com.cc-insights.plist"
INSTALL = SCRIPTS / "install-launchd.sh"


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


# --------------------------------------------------------------------------
# the watch job
# --------------------------------------------------------------------------
WATCH_PLIST = SCRIPTS / "com.cc-insights.watch.plist"


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
    assert d["ProgramArguments"] == ["/opt/cci", "watch", "--quiet"]
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


def test_the_installer_knows_both_jobs_and_removes_both():
    text = INSTALL.read_text()
    assert "--watch" in text and "com.cc-insights.watch" in text
    # --uninstall must take out whichever is loaded, not just the one it was
    # asked about, or an upgrade leaves two writers behind.
    branch = text.split('== "--uninstall" ]]', 1)[1].split("exit 0", 1)[0]
    assert 'unload "$LABEL"' in branch and 'unload "$WATCH_LABEL"' in branch
