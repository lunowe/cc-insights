"""The `curl | bash` installer, exercised rather than read.

This script runs on a machine that has nothing on it, from a pipe the user
cannot inspect first, before there is any cc-insights to report a bug with.
There is no second chance and no error path back to us, so what it does wrong
it does in silence on someone else's laptop.

Most of what follows sources the script with `CC_INSIGHTS_INSTALL_SH_LIB=1`
and calls its functions. That is the difference between checking that the
right words appear in the file and checking that the thing works: a grep for
`python3.12` passes on a version that picks the 3.9 sitting in front of it.

Nothing here runs the installer to completion. Doing so would run
`cci install`, which writes a launchd job -- and this suite runs on a machine
with a live `com.cc-insights` job that must keep capturing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "install.sh"


def source_and_run(snippet: str, *, cwd: Path | None = None,
                   env: dict[str, str] | None = None,
                   script: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Load the installer's functions, then run `snippet` against them."""
    path = script or SCRIPT
    program = f'export CC_INSIGHTS_INSTALL_SH_LIB=1\n. "{path}"\n{snippet}\n'
    full_env = {**os.environ, "CC_INSIGHTS_INSTALL_SH_LIB": "1", **(env or {})}
    return subprocess.run(
        ["bash", "-c", program],
        capture_output=True, text=True, timeout=60,
        cwd=str(cwd) if cwd else None, env=full_env,
    )


@pytest.fixture
def shim_dir(tmp_path: Path) -> Path:
    """A directory to put fake interpreters in, first on PATH."""
    d = tmp_path / "shims"
    d.mkdir()
    return d


def write_shim(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(f"#!/bin/bash\n{body}\n")
    path.chmod(0o755)
    return path


# ------------------------------------------------------------ the basics --


def test_the_script_is_valid_bash():
    """A syntax error here is a user with a half-run installer and no tool."""
    proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_the_script_is_executable():
    """`./scripts/install.sh` is the documented local invocation."""
    assert SCRIPT.stat().st_mode & 0o111


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck is not installed")
def test_shellcheck_is_clean():
    """Unquoted expansions in an installer are someone's home directory.

    Skipped rather than failed when shellcheck is absent, the same bargain
    `test_packaging.py` makes with `build` -- but CI installs it, so this
    runs there.
    """
    proc = subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ----------------------------------------------------------------- stdin --


def test_the_script_never_reads_stdin():
    """Piped from curl, stdin *is* the script -- a read would eat the rest.

    `curl ... | bash` hands bash the script on stdin and bash consumes it
    lazily. Any `read` in here therefore swallows the lines that have not run
    yet, and the user gets a truncated installer that exits zero having done
    part of the job. There is no way to notice that from inside the script,
    so it is checked from outside.
    """
    source = SCRIPT.read_text()
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    for reader in (" read ", "\tread ", "read -r", "read -p", "$(cat)", "`cat`"):
        assert reader not in code, f"{reader!r} reads stdin; a curl|bash script cannot"


def test_the_script_does_not_block_on_an_open_stdin(shim_dir: Path, tmp_path: Path):
    """The same invariant, observed instead of parsed.

    stdin here is a pipe that is open and empty and never written to, which
    is what a `read` would block on forever. The installer is steered into
    its fastest exit (no usable Python) so that finishing at all is the
    signal; a timeout is the failure.
    """
    for name in ("python3.14", "python3.13", "python3.12", "python3.11", "python3", "python"):
        write_shim(shim_dir, name, "exit 1")

    read_fd, write_fd = os.pipe()
    try:
        proc = subprocess.run(
            ["bash", str(SCRIPT)],
            stdin=read_fd, capture_output=True, text=True, timeout=60,
            env={**os.environ, "PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}",
                 "HOME": str(tmp_path)},
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert proc.returncode != 0, "a machine with no usable Python must not report success"


# ---------------------------------------------------------------- python --


def test_no_usable_python_names_a_way_to_get_one(shim_dir: Path, tmp_path: Path):
    """"Python 3.11+ required" is true and leaves the user exactly as stuck.

    The whole promise of a one-line installer is that it works on a machine
    with nothing set up, and the commonest nothing is an old system Python.
    What has to come out of that case is the command for *this* platform.
    """
    for name in ("python3.14", "python3.13", "python3.12", "python3.11", "python3", "python"):
        write_shim(shim_dir, name, "exit 1")

    proc = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, timeout=60,
        env={**os.environ, "PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}",
             "HOME": str(tmp_path)},
    )

    assert proc.returncode != 0
    assert "3.11" in proc.stderr
    hints = ("brew", "apt", "dnf", "pacman", "apk", "pyenv", "python.org", "winget")
    assert any(h in proc.stderr for h in hints), (
        f"the failure names no way to get a Python:\n{proc.stderr}"
    )


def test_a_too_old_python3_does_not_end_the_search(shim_dir: Path):
    """`python3` being 3.9 must not hide the 3.12 installed beside it.

    This is the normal macOS arrangement -- /usr/bin/python3 is the 3.9 Apple
    ships and the usable one answers only to `python3.12`. A search that took
    the first `python3` on PATH and gave up would tell most Mac users to go
    and install a Python they already have, which is the single most likely
    way for this installer to fail on a machine where it should work.
    """
    write_shim(shim_dir, "python3", "exit 1")           # stands in for 3.9
    write_shim(shim_dir, "python3.12", f'exec "{sys.executable}" "$@"')

    proc = source_and_run("find_python", env={"PATH": f"{shim_dir}{os.pathsep}/usr/bin:/bin"})

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(shim_dir / "python3.12")


def test_this_interpreter_is_accepted():
    """The suite runs on a supported Python, so the check must say so.

    A version test that rejects everything would fail closed and look like
    "no Python found" on a machine that has one.
    """
    proc = source_and_run(f'python_is_new_enough "{sys.executable}" && echo yes')
    assert proc.stdout.strip() == "yes"


# -------------------------------------------------------------- the repo --


def test_it_installs_the_checkout_it_is_run_from():
    """PyPI has nothing until the first release; the checkout is the fallback.

    Also the contributor path: a script that always installed the published
    version would quietly test someone else's code.
    """
    proc = source_and_run("find_checkout", cwd=REPO / "tests")
    assert proc.returncode == 0, proc.stderr
    assert Path(proc.stdout.strip()) == REPO


def test_outside_a_checkout_it_falls_back_to_pypi(tmp_path: Path):
    """The `curl | bash` case -- no repo anywhere near.

    `find_checkout` failing is what selects PyPI, so a false positive here
    means the installer tries to build a wheel out of whatever directory the
    user happened to be standing in.
    """
    elsewhere = tmp_path / "not-a-checkout"
    elsewhere.mkdir()
    (elsewhere / "pyproject.toml").write_text('[project]\nname = "something-else"\n')

    copy = tmp_path / "install.sh"
    copy.write_text(SCRIPT.read_text())

    proc = source_and_run("find_checkout || echo pypi", cwd=elsewhere, script=copy)
    assert proc.stdout.strip() == "pypi"


# --------------------------------------------------------- the two flags --


def test_watch_and_uninstall_reach_cci():
    """Both are pass-throughs; a dropped flag silently installs the wrong job.

    `--watch` and the interval job are mutually exclusive -- two writers on
    one SQLite database -- so an ignored `--watch` does not degrade to "the
    other one", it contradicts what the user asked for.
    """
    text = SCRIPT.read_text()
    assert '"$cci_path" install --watch' in text
    assert '"$cci" install --uninstall' in text


def test_the_flags_are_accepted_and_nonsense_is_not(tmp_path: Path):
    """Argument parsing, without getting as far as installing anything."""
    ok = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True,
                        timeout=30)
    assert ok.returncode == 0
    assert "--watch" in ok.stdout and "--uninstall" in ok.stdout

    bad = subprocess.run(["bash", str(SCRIPT), "--definitely-not-a-flag"],
                         capture_output=True, text=True, timeout=30,
                         env={**os.environ, "HOME": str(tmp_path)})
    assert bad.returncode != 0, "an unrecognised flag must not be silently ignored"

    both = subprocess.run(["bash", str(SCRIPT), "--watch", "--uninstall"],
                          capture_output=True, text=True, timeout=30,
                          env={**os.environ, "HOME": str(tmp_path)})
    assert both.returncode != 0, "--watch --uninstall is a contradiction, not a sequence"


# ------------------------------------------------------------ the ending --


def test_it_finishes_by_running_doctor():
    """Without it the script's last word is "installed", which is a guess.

    `cci install` can succeed and still leave a job that is written but not
    loaded, or a first ingest that read nothing. `cci doctor` is the only
    thing that distinguishes those from a working install, and it has to run
    *after* the install or it reports on the previous state.
    """
    text = SCRIPT.read_text()
    assert '"$cci_path" doctor' in text
    assert text.index('"$cci_path" install') < text.index('"$cci_path" doctor')


def test_a_failing_doctor_fails_the_install():
    """Exiting 0 over a red report is how a broken install looks fine.

    `cci doctor` already exits non-zero on a real failure specifically so it
    can be used from a script. This is that script.
    """
    text = SCRIPT.read_text()
    assert '"$cci_path" doctor || rc=$?' in text
    assert 'return "$rc"' in text


def test_the_uninstall_removes_the_job_before_the_program():
    """The other order leaves a launchd job pointing at a deleted binary.

    That job keeps firing every 15 minutes forever, fails every time, and
    reports it to a log file nobody has any reason to open.
    """
    text = SCRIPT.read_text()
    assert text.index('"$cci" install --uninstall') < text.index('rm -rf "$INSTALL_DIR"')


# ----------------------------------------------------------- environment --


def test_it_says_which_shell_rc_to_edit(tmp_path: Path):
    """Naming the wrong file costs a reopened terminal and no working `cci`.

    On macOS, Terminal starts bash as a login shell: it reads .bash_profile
    and never .bashrc. Telling a Mac user to edit .bashrc produces a line
    they add, a terminal they restart, and the same missing command.
    """
    home = str(tmp_path)
    expected_bash = ".bash_profile" if sys.platform == "darwin" else ".bashrc"
    for shell, expected in (
        ("/bin/zsh", ".zshrc"),
        ("/bin/bash", expected_bash),
        ("/usr/bin/fish", "config.fish"),
        ("/usr/bin/nonsense", ".profile"),
    ):
        proc = source_and_run("rc_file_for_shell", env={"SHELL": shell, "HOME": home})
        assert proc.stdout.strip().startswith(home)
        assert proc.stdout.strip().endswith(expected), f"{shell} -> {proc.stdout!r}"


def test_the_path_line_is_one_a_fish_user_can_paste(tmp_path: Path):
    """fish has no `export`, so the bash line is a syntax error there.

    A line that errors when pasted is worse than no advice: the user has now
    edited their config and their shell complains on every start.
    """
    fish = source_and_run('path_line_for_shell "/x/bin"', env={"SHELL": "/usr/bin/fish"})
    assert fish.stdout.strip() == "fish_add_path /x/bin"

    posix = source_and_run('path_line_for_shell "/x/bin"', env={"SHELL": "/bin/zsh"})
    assert posix.stdout.strip() == 'export PATH="/x/bin:$PATH"', (
        "$PATH must survive into the rc file unexpanded"
    )


@pytest.mark.skipif(not shutil.which("dash"), reason="needs a non-bash sh to pipe into")
def test_piping_into_sh_says_to_use_bash(tmp_path: Path):
    """`| sh` is the commonest way to run a `curl | bash` line wrongly.

    The script needs bash (`set -o pipefail`, arrays, `[[`). Without a guard
    above that `set`, dash dies on line one with "Illegal option -o
    pipefail", which names neither the problem nor the fix.
    """
    proc = subprocess.run(
        ["dash"], stdin=SCRIPT.open("rb"), capture_output=True, text=True, timeout=30,
        env={**os.environ, "HOME": str(tmp_path)},
    )
    assert proc.returncode != 0
    assert "bash" in proc.stderr.lower()
    assert "pipefail" not in proc.stderr, (
        "the shell guard must come before `set -o pipefail`, or dash never reaches it"
    )
