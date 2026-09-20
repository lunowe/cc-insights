"""Installing the background job, from the installed package.

The point of this project is that it keeps running without being asked: agent
log directories are pruned on a rolling basis, so history nobody captured is
gone permanently. That makes "did the job actually get installed" the single
most important thing the tool can be wrong about -- and until now the answer
lived in `scripts/install-launchd.sh`, which you can only run if you have the
repo. Someone who did `pipx install cc-insights` had a working CLI and no way
to make it automatic.

So the logic moves here, next to the templates it writes, and the shell
script becomes a thin wrapper for the checkout case. One implementation.

Two jobs, and you install exactly one:

* **interval** -- `cci ingest && cci derive` every 15 minutes, then exits.
* **watch** -- one `cci watch` process that follows the logs, seconds behind.

Both at once is two writers on one SQLite database, which is the one way to
make this tool contend with itself. Installing either removes the other
first; that rule is enforced here rather than documented.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from cc_insights import assets, paths

INTERVAL = "interval"
WATCH = "watch"

LABEL = "com.cc-insights"
WATCH_LABEL = "com.cc-insights.watch"

_LABELS = {INTERVAL: LABEL, WATCH: WATCH_LABEL}
_TEMPLATES = {INTERVAL: f"{LABEL}.plist", WATCH: f"{WATCH_LABEL}.plist"}


class Unsupported(RuntimeError):
    """No scheduler integration for this platform. The message says what to do."""


@dataclass(frozen=True)
class JobStatus:
    """What is on the machine right now, for one job."""

    mode: str
    label: str
    installed: bool          # the job file exists
    loaded: bool             # the scheduler has actually picked it up
    path: Path | None = None

    @property
    def healthy(self) -> bool:
        return self.installed and self.loaded


# --------------------------------------------------------------- the binary --


def cci_executable() -> Path:
    """The `cci` this Python would run, as an absolute path.

    A job file records an absolute path, so getting this wrong installs a job
    that fails silently every 15 minutes -- and a job whose only symptom is
    "the numbers stopped moving" is worse than no job at all.

    The console script beside this interpreter is checked first and `PATH`
    second, deliberately. Running `cci install` from a venv means installing
    *that* venv's cci; picking it off `PATH` could pin the job to a different
    installation that happens to shadow it, and the two would then disagree
    about which database is being written.
    """
    candidate = Path(sys.executable).parent / ("cci.exe" if os.name == "nt" else "cci")
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return candidate.resolve()

    found = shutil.which("cci")
    if found:
        return Path(found).resolve()

    raise Unsupported(
        "cannot find the `cci` executable to schedule.\n"
        f"    looked beside this interpreter ({candidate}) and on PATH.\n"
        "    if you are running from a source checkout, `pip install -e .` first."
    )


# ------------------------------------------------------------------- macOS --


def _launch_agents() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _plist_path(mode: str) -> Path:
    return _launch_agents() / f"{_LABELS[mode]}.plist"


def _launchctl(*argv: str) -> int:
    """launchctl, with its noise swallowed.

    `unload` on a job that is not loaded is not an error we care about -- it
    is the normal case on a first install -- so the return code is passed
    back rather than raised on.
    """
    return subprocess.run(
        ["launchctl", *argv], capture_output=True, text=True
    ).returncode


def _launchd_loaded(label: str) -> bool:
    proc = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    if proc.returncode != 0:
        return False
    return any(line.split("\t")[-1] == label for line in proc.stdout.splitlines())


def _install_launchd(mode: str, cci: Path, log_dir: Path) -> Path:
    text = assets.job_template(_TEMPLATES[mode])
    rendered = text.replace("__CCI__", str(cci)).replace("__LOGDIR__", str(log_dir))
    if "__CCI__" in rendered or "__LOGDIR__" in rendered:   # pragma: no cover
        raise RuntimeError("template substitution left a placeholder behind")

    # Parse before writing: a malformed plist is rejected by launchd with a
    # message that does not name the problem, and the job then simply never
    # runs. Checking here turns that into an error at install time.
    #
    # plistlib rather than plutil, and that is not interchangeable. `--` is
    # illegal inside an XML comment; plutil accepts a template containing one
    # and plistlib does not, so a comment written with an em-dash-as-two-
    # hyphens passed the lint and produced a plist launchd could not read.
    # plistlib is also always available, where plutil is macOS-only.
    try:
        plistlib.loads(rendered.encode())
    except Exception as exc:
        raise RuntimeError(f"generated plist is invalid: {exc}") from exc

    dst = _plist_path(mode)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(rendered)

    # The other job goes first. Two writers on one database is the failure
    # this ordering exists to prevent.
    other = WATCH if mode == INTERVAL else INTERVAL
    _uninstall_launchd(other)

    _launchctl("unload", str(dst))     # a reinstall over a loaded job
    if _launchctl("load", str(dst)) != 0:      # pragma: no cover - env-dependent
        raise RuntimeError(f"launchctl refused to load {dst}")
    return dst


def _uninstall_launchd(mode: str) -> bool:
    dst = _plist_path(mode)
    existed = dst.exists()
    _launchctl("unload", str(dst))
    dst.unlink(missing_ok=True)
    return existed


def _status_launchd(mode: str) -> JobStatus:
    dst = _plist_path(mode)
    return JobStatus(
        mode=mode,
        label=_LABELS[mode],
        installed=dst.exists(),
        loaded=_launchd_loaded(_LABELS[mode]),
        path=dst if dst.exists() else None,
    )


# ----------------------------------------------------------------- Windows --


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def _run_task_script(*extra: str) -> None:
    shell = _powershell()
    if shell is None:                                    # pragma: no cover
        raise Unsupported("neither `pwsh` nor `powershell` is on PATH.")
    script = assets.JOBS / "install-task.ps1"
    proc = subprocess.run(
        [shell, "-ExecutionPolicy", "Bypass", "-File", str(script), *extra],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:                             # pragma: no cover
        raise RuntimeError(f"{script.name} failed:\n{proc.stdout}{proc.stderr}")


def _status_task(mode: str) -> JobStatus:               # pragma: no cover - Windows
    shell = _powershell()
    label = _LABELS[mode]
    if shell is None:
        return JobStatus(mode=mode, label=label, installed=False, loaded=False)
    proc = subprocess.run(
        [shell, "-NoProfile", "-Command",
         f"if (Get-ScheduledTask -TaskName '{label}' -EA SilentlyContinue)"
         " { 'yes' } else { 'no' }"],
        capture_output=True, text=True,
    )
    present = proc.stdout.strip() == "yes"
    # Task Scheduler has no "loaded" distinct from "registered".
    return JobStatus(mode=mode, label=label, installed=present, loaded=present)


# ------------------------------------------------------------- the front door --


def supported() -> bool:
    return sys.platform == "darwin" or paths.LOCAL == paths.WINDOWS


def _unsupported() -> Unsupported:
    """Say what to do instead, rather than only what is missing.

    Linux has no integration yet. A cron line is a complete substitute for the
    interval job and takes one command, so printing it is more useful than
    "unsupported platform" and more honest than shipping a systemd unit
    nobody has run.
    """
    try:
        cci = cci_executable()
    except Unsupported:
        cci = Path("cci")
    return Unsupported(
        f"no scheduler integration for {sys.platform} yet (macOS and Windows only).\n"
        "    the interval job is one cron line, and does the same thing:\n\n"
        f"      (crontab -l 2>/dev/null; echo '*/15 * * * * {cci} ingest && {cci} derive') "
        "| crontab -\n\n"
        f"    or keep a watcher up under your service manager:  {cci} watch --quiet"
    )


def install(mode: str = INTERVAL, *, log_dir: Path) -> tuple[Path | None, Path]:
    """Install one job, removing the other. Returns (job file, cci path)."""
    if mode not in _LABELS:
        raise ValueError(f"unknown job mode {mode!r}")
    cci = cci_executable()
    log_dir.mkdir(parents=True, exist_ok=True)

    if sys.platform == "darwin":
        return _install_launchd(mode, cci, log_dir), cci
    if paths.LOCAL == paths.WINDOWS:                     # pragma: no cover
        _run_task_script(*(["-Watch"] if mode == WATCH else []))
        return None, cci
    raise _unsupported()


def uninstall() -> list[str]:
    """Remove both jobs. Returns the labels that were actually there."""
    if sys.platform == "darwin":
        return [_LABELS[m] for m in (INTERVAL, WATCH) if _uninstall_launchd(m)]
    if paths.LOCAL == paths.WINDOWS:                     # pragma: no cover
        _run_task_script("-Uninstall")
        return [LABEL]
    raise _unsupported()


def status() -> list[JobStatus]:
    """Both jobs' state. Empty on a platform with no integration."""
    if sys.platform == "darwin":
        return [_status_launchd(m) for m in (INTERVAL, WATCH)]
    if paths.LOCAL == paths.WINDOWS:                     # pragma: no cover
        return [_status_task(m) for m in (INTERVAL, WATCH)]
    return []


def active() -> JobStatus | None:
    """The one job that is actually running, if any."""
    for job in status():
        if job.healthy:
            return job
    return None
