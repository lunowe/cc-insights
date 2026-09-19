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
