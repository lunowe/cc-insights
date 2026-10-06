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

Two jobs, and on macOS you install exactly one:

* **interval** -- `cci ingest && cci derive` every 15 minutes, then exits.
* **watch** -- one `cci watch` process that follows the logs, seconds behind.

Both at once is two writers on one SQLite database, which is the one way to
make this tool contend with itself. Installing either removes the other
first; that rule is enforced here rather than documented.

Windows and Linux have the interval job only -- a Task Scheduler task, or one
marked line in the user's crontab -- and `--watch` is refused there rather
than half-implemented: neither has a supervisor for a process that is
supposed to stay up, so a registered `cci watch` would stop at its first
crash and report it nowhere. The refusal names the interval job instead. A
platform with none of the three gets the job printed in full by
`_unsupported()`, for whatever schedules jobs there.
"""

from __future__ import annotations

import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from cc_insights import assets, paths

INTERVAL = "interval"
WATCH = "watch"

#: The label, in the one place both schedulers read it from. launchd uses it
#: for the plist name and Task Scheduler for the task name, and the two used
#: to be spelled differently: `install-task.ps1` registered "CC-Insights
#: Ingest" while `_status_task` asked `Get-ScheduledTask` about
#: "com.cc-insights". They could never match, so on Windows a successful
#: install was followed by `cci doctor` reporting "not installed" and
#: `active()` returning None forever -- and `scripts/install.sh` ends with
#: `cci doctor`, so a correct install failed its own final gate.
#: `tests/test_scripts.py` now compares this constant against the .ps1.
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


def _write_atomically(dst: Path, text: str) -> None:
    """Write `text` to `dst` through a temporary file in the same directory.

    `write_text` truncates in place, so a crash, a full disk or a kill
    between the truncate and the write leaves a zero-byte plist where a
    working job used to be. launchd refuses an empty plist at the next
    login without saying so anywhere, and capture stops with no error and no
    log line -- the silent failure this whole module exists to avoid.

    `os.replace` within one directory is atomic, so the job file on disk is
    either the old definition or the new one and never half of either. Same
    bargain `account.save` and `config._atomic_write` already make for files
    that matter less than this one.
    """
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text)
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)


def _render(mode: str, cci: Path, log_dir: Path, config_dir: Path) -> str:
    """The job file's text, with every path escaped for XML.

    A path is allowed to contain the three characters XML is not: a home
    directory called "Tom & Jerry" produced `not well-formed (invalid
    token): line 16, column 28` out of `cci install` and named nothing the
    user could act on. Escaping the substituted values -- and only those,
    the template's own `&amp;&amp;` is already correct -- makes those
    directories work rather than fail more politely.
    """
    rendered = assets.job_template(_TEMPLATES[mode])
    for placeholder, value in (("__CCI__", cci), ("__LOGDIR__", log_dir),
                               ("__CONFIGDIR__", config_dir)):
        rendered = rendered.replace(placeholder, escape(str(value)))
    if any(p in rendered for p in ("__CCI__", "__LOGDIR__", "__CONFIGDIR__")):
        raise RuntimeError("template substitution left a placeholder behind")
    return rendered


def _install_launchd(mode: str, cci: Path, log_dir: Path, config_dir: Path) -> Path:
    rendered = _render(mode, cci, log_dir, config_dir)

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
        # Name the substituted values. What is nearly always wrong is one of
        # these two paths, and an XML parse error on its own sends the reader
        # to the template, which is fine.
        raise RuntimeError(
            f"generated plist is invalid: {exc}\n"
            f"    cci:  {cci}\n"
            f"    logs: {log_dir}"
        ) from exc

    dst = _plist_path(mode)
    dst.parent.mkdir(parents=True, exist_ok=True)
    _write_atomically(dst, rendered)

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


# ------------------------------------------------------------- Linux, cron --
#
# The interval job as one line in the user's crontab. It used to be printed
# for the user to paste, and the printed line was missing `init`, the log
# redirects and CC_INSIGHTS_HOME -- every one of which the plist comments
# explain the need for. Owning the line here means it is the same pipeline
# as the other platforms, `--uninstall` can remove it and `cci doctor` can
# see it.
#
# A crontab is the user's file, not ours, and `crontab -` replaces it
# wholesale. So everything below works on the table as BYTES split only on
# b"\n": decoding would choke on a Latin-1 comment, `text=True` turns `\r`
# into a line break, and `str.splitlines` splits on form feeds and U+2028 --
# each of which would rewrite somebody's existing jobs. Only the line ending
# in CRON_TAG is ever changed, and nothing is written after a failed read,
# because a failed read written back is a deleted crontab.

#: The last word of our line. It is the `$0` handed to `/bin/sh -c`, so it
#: is inert in every shell -- a trailing `# comment` is not, since cron runs
#: the line with the crontab's own SHELL=, which may be fish or tcsh.
#: Matched together with the quote that closes the script before it, so a
#: user's comment that merely ends in the label is never taken for ours.
#: scripts/install.sh matches the same bytes.
CRON_TAG = LABEL
_CRON_TAG_B = f"' {CRON_TAG}".encode()

#: What `crontab -l` says when the user has no table yet: cronie and vixie
#: say "no crontab for <user>"; busybox says "can't open '<user>': No such
#: file or directory" -- and "can't open ... Permission denied" when the table
#: exists and cannot be read, which must NOT count as empty. Read under
#: LC_ALL=C so none of it is translated.
def _says_no_crontab(stderr: bytes) -> bool:
    text = stderr.lower()
    return b"no crontab for" in text or (b"can't open" in text and b"no such file" in text)

#: The line `cci install` used to print for people to paste. Two writers on
#: one database is the failure the job swap exists to prevent, so a pasted
#: copy is replaced along with our own line rather than left running beside
#: it. Matched exactly, and only for a program called `cci`.
_PASTED_HINT = re.compile(
    rb"^\*/15 \* \* \* \* ((?:\S*/)?cci) ingest && \1 derive$"
)

#: Process names of the cron daemons in common use, for `_cron_daemon_running`.
_CRON_DAEMONS = {"cron", "crond", "cronie", "fcron", "dcron"}


def _crontab_bin() -> str | None:
    return shutil.which("crontab")


def uses_cron() -> bool:
    """True where the interval job lives in the user's crontab."""
    return (sys.platform != "darwin" and paths.LOCAL != paths.WINDOWS
            and _crontab_bin() is not None)


def _crontab(*argv: str, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    exe = _crontab_bin()
    if exe is None:
        raise RuntimeError("no `crontab` command on PATH")
    try:
        return subprocess.run([exe, *argv], input=stdin, capture_output=True,
                              env={**os.environ, "LC_ALL": "C"})
    except OSError as exc:
        raise RuntimeError(f"could not run crontab: {exc}") from exc


def _cron_line(cci: Path, log_dir: Path, config_dir: Path) -> str:
    """The crontab entry: the plist's pipeline, environment and logs.

    `init` leads for the reason the plist gives; CC_INSIGHTS_HOME is pinned
    because cron, like launchd, does not run your shell; the redirects go to
    the same two files on every platform. The script runs under an explicit
    `/bin/sh -c`, whatever SHELL= the crontab sets.

    A `%` or a newline in a path is refused rather than escaped: cronie turns
    `%` into a newline unless backslashed, busybox passes the backslash
    through, and no single spelling is right for both.
    """
    for path in (cci, log_dir, config_dir):
        if "%" in str(path) or "\n" in str(path):
            raise RuntimeError(
                f"cannot schedule a path containing '%' or a newline: {path}\n"
                "    cron treats both specially. Install cc-insights, or set\n"
                "    CC_INSIGHTS_HOME, somewhere without them."
            )
    q = shlex.quote
    steps = " && ".join(f"{q(str(cci))} {step}"
                        for step in ("init", "ingest", "derive", "sync auto"))
    script = (f"export CC_INSIGHTS_HOME={q(str(config_dir))}; "
              f"{{ {steps}; }} >>{q(str(log_dir / 'ingest.log'))} "
              f"2>>{q(str(log_dir / 'ingest.err'))}")
    return f"*/15 * * * * /bin/sh -c {q(script)} {CRON_TAG}"


def _cron_read() -> list[bytes]:
    """The table as raw lines, without their b"\\n"."""
    proc = _crontab("-l")
    if proc.returncode == 0:
        lines = proc.stdout.split(b"\n")
        return lines[:-1] if lines and lines[-1] == b"" else lines
    if _says_no_crontab(proc.stderr):
        return []
    detail = proc.stderr.decode(errors="replace").strip() or f"exit {proc.returncode}"
    raise RuntimeError(
        f"could not read your crontab, so it was left untouched:\n"
        f"    crontab -l: {detail}"
    )


def _cron_write(lines: list[bytes]) -> None:
    proc = _crontab("-", stdin=b"".join(line + b"\n" for line in lines))
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"crontab refused the new table: {detail}")


def _is_ours(line: bytes) -> bool:
    return line.rstrip().endswith(_CRON_TAG_B) or _PASTED_HINT.match(line.strip()) is not None


def _install_cron(cci: Path, log_dir: Path, config_dir: Path) -> None:
    line = os.fsencode(_cron_line(cci, log_dir, config_dir))
    kept = [existing for existing in _cron_read() if not _is_ours(existing)]
    _cron_write([*kept, line])
    # Read back rather than trust the exit code: a cron that accepted the
    # table and then dropped the line is the silent failure again.
    if line not in _cron_read():
        raise RuntimeError("the crontab was written but the cc-insights line is not in it")


def _uninstall_cron() -> bool:
    lines = _cron_read()
    kept = [line for line in lines if not _is_ours(line)]
    if len(kept) == len(lines):
        return False
    _cron_write(kept)
    return True


def _command_of(line: bytes) -> str | None:
    """The command half of an active crontab line, or None.

    A line commented out to pause the job is not a running job, and an
    `@daily`-style schedule has one field where `*/15 * * * *` has five.
    """
    text = os.fsdecode(line).strip()
    if not text or text.startswith("#"):
        return None
    fields = text.split(None, 1 if text.startswith("@") else 5)
    return fields[-1] if len(fields) == (2 if text.startswith("@") else 6) else None


def _cron_command() -> str | None:
    """The command our crontab line runs, or None when it is absent."""
    try:
        lines = _cron_read()
    except RuntimeError:
        return None
    for line in lines:
        if line.rstrip().endswith(_CRON_TAG_B) and (command := _command_of(line)):
            return command
    return None


def _cron_daemon_running() -> bool:
    """Whether a cron daemon is up. True when it cannot be told.

    A line in the table does nothing without one, and the places that have
    `crontab` and no daemon -- WSL without systemd, a container with the
    package but no init -- are exactly where nobody would notice.
    """
    proc_dir = Path("/proc")
    try:
        # hidepid hides other users' processes -- root's cron among them --
        # and with it PID 1, so an unreadable PID 1 means "cannot tell".
        (proc_dir / "1" / "comm").read_text()
    except OSError:
        return True
    # systemd-cron runs crontabs from timers, with no daemon to find.
    if any(Path(d, "cron.target").exists()
           for d in ("/etc/systemd/system", "/usr/lib/systemd/system", "/lib/systemd/system")):
        return True
    for pid in proc_dir.glob("[0-9]*"):
        try:
            name = (pid / "comm").read_text().strip()
            if name in _CRON_DAEMONS:
                return True
            # `busybox crond`, started as busybox rather than via its symlink.
            if name == "busybox" and b"crond" in (pid / "cmdline").read_bytes():
                return True
        except OSError:
            continue
    return False


def cron_error() -> str | None:
    """Why the crontab cannot be read, or None when it can (or is not used).

    Without this, an unreadable table reads as "no job", and doctor would
    send somebody to `cci install` for a problem `cci install` cannot fix.
    """
    if not uses_cron():
        return None
    try:
        _cron_read()
    except RuntimeError as exc:
        return str(exc).splitlines()[-1].strip()
    return None


# ------------------------------------------------------------- the front door --


def supported() -> bool:
    return sys.platform == "darwin" or paths.LOCAL == paths.WINDOWS or uses_cron()


def _unsupported(config_dir: Path | None = None) -> Unsupported:
    """Say what to do instead, rather than only what is missing.

    Reached on a platform that is neither macOS nor Windows and has no
    `crontab` command: a container, a minimal image, a systemd-only distro.
    The line is printed whole -- the same one `cci install` would have
    written -- so whatever schedules jobs there can run it.
    """
    try:
        cci = cci_executable()
    except Unsupported:
        cci = Path("cci")
    if config_dir is None:
        from cc_insights.config import default_config_dir
        config_dir = default_config_dir()
    line = _cron_line(cci, config_dir / "logs", config_dir)
    return Unsupported(
        f"no scheduler found on {sys.platform}: no launchd, Task Scheduler or `crontab`.\n"
        "    the interval job is this one line; run it every 15 minutes with\n"
        "    whatever schedules jobs here (install cron and re-run `cci install`\n"
        "    to have it written for you):\n\n"
        f"      {line}\n\n"
        f"    or keep a watcher up under your service manager:  {cci} watch --quiet"
    )


def _watch_is_macos_only() -> Unsupported:
    """Refuse `--watch` off macOS, and say what to install instead.

    Task Scheduler and cron start things; they do not supervise them. There
    is no KeepAlive, so a `cci watch` registered as a task stops for good the
    first time it exits -- and the symptom of that is the numbers quietly
    ceasing to move, which is precisely the failure the watch job is
    supposed to remove. Registering one anyway would be shipping a feature
    that fails silently on somebody else's machine.
    """
    return Unsupported(
        "`--watch` is macOS-only; this platform has the 15-minute job.\n"
        "    Task Scheduler and cron cannot keep a process alive -- a watcher\n"
        "    that exited would never be restarted, and nothing would say so.\n\n"
        "      cci install            the interval job: same history, up to\n"
        "                             15 minutes behind instead of seconds\n"
        "      cci watch              or keep one in a terminal yourself"
    )


def install(mode: str = INTERVAL, *, config_dir: Path,
            log_dir: Path | None = None) -> tuple[Path | None, Path]:
    """Install one job, removing the other. Returns (job file, cci path).

    `config_dir` is not optional and is not cosmetic: it is written into the
    job so the background run fills the same database this CLI reads. It
    used to be inferred at run time from the job's own environment, which a
    launchd agent does not have -- see the EnvironmentVariables comment in
    the plist. `log_dir` defaults to it for the same reason: the two halves
    were passed separately once, and a split between "where it logs" and
    "where it writes" is exactly the bug.
    """
    if mode not in _LABELS:
        raise ValueError(f"unknown job mode {mode!r}")
    cci = cci_executable()
    log_dir = log_dir if log_dir is not None else config_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    if sys.platform == "darwin":
        return _install_launchd(mode, cci, log_dir, config_dir), cci
    if paths.LOCAL == paths.WINDOWS:                     # pragma: no cover
        if mode == WATCH:
            raise _watch_is_macos_only()
        _run_task_script("-ConfigDir", str(config_dir))
        return None, cci
    if uses_cron():
        if mode == WATCH:
            raise _watch_is_macos_only()
        _install_cron(cci, log_dir, config_dir)
        return None, cci
    raise _unsupported(config_dir)


def uninstall() -> list[str]:
    """Remove both jobs. Returns the labels that were actually there."""
    if sys.platform == "darwin":
        return [_LABELS[m] for m in (INTERVAL, WATCH) if _uninstall_launchd(m)]
    if paths.LOCAL == paths.WINDOWS:                     # pragma: no cover
        # Ask first. Returning [LABEL] unconditionally made `cci install
        # --uninstall` print "removed com.cc-insights" on a machine that had
        # no task at all, which is the one answer that stops somebody looking
        # for the job that is still running.
        present = _status_task(INTERVAL).installed
        _run_task_script("-Uninstall")
        return [LABEL] if present else []
    if uses_cron():
        return [LABEL] if _uninstall_cron() else []
    raise _unsupported()


def status() -> list[JobStatus]:
    """Both jobs' state. Empty on a platform with no integration."""
    if sys.platform == "darwin":
        return [_status_launchd(m) for m in (INTERVAL, WATCH)]
    if paths.LOCAL == paths.WINDOWS:                     # pragma: no cover
        return [_status_task(m) for m in (INTERVAL, WATCH)]
    if uses_cron():
        # "loaded" is: a cron daemon is running to read the table. No watch job.
        present = _cron_command() is not None
        loaded = present and _cron_daemon_running()
        return [JobStatus(mode=INTERVAL, label=LABEL, installed=present, loaded=loaded),
                JobStatus(mode=WATCH, label=WATCH_LABEL, installed=False, loaded=False)]
    return []


def active() -> JobStatus | None:
    """The one job that is actually running, if any."""
    for job in status():
        if job.healthy:
            return job
    return None


def installed_command(mode: str) -> str | None:
    """The command line the job on this machine actually runs, or None.

    Read back from the job file rather than regenerated from the template,
    because the question worth asking is what is scheduled *right now*. A job
    written by an older version keeps running that older version's line until
    somebody reinstalls -- so an upgrade that adds a step to the pipeline is
    invisible until a check like this one looks. That is the same class of
    silent drift `init`-leads-the-line exists to survive.

    None when the file is absent, unreadable, or on a platform whose
    scheduler does not store the command somewhere this can parse. None means
    "cannot tell", never "no", and callers must not report it as a fault.
    """
    if uses_cron():
        return _cron_command() if mode == INTERVAL else None
    if sys.platform != "darwin":
        return None
    path = _plist_path(mode)
    try:
        with path.open("rb") as stream:
            body = plistlib.load(stream)
    except (OSError, ValueError):
        return None
    argv = body.get("ProgramArguments")
    if not isinstance(argv, list) or not argv:
        return None
    return str(argv[-1])
