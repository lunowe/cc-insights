"""Configuration, stored as TOML at ~/.config/cc-insights/config.toml.

The `host_id` is generated once on first run and must never change: it is part
of every session id, so regenerating it would fork the entire history into a
duplicate set of rows. It is also what makes the multi-machine future a config
concern rather than a migration.
"""

from __future__ import annotations

import os
import platform
import re
import socket
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from cc_insights import paths


def default_config_dir() -> Path:
    """Where the config lives when nothing overrides it.

    `~/.config/cc-insights` everywhere except Windows, which has no XDG
    convention and puts per-user application state in `%APPDATA%`. A Windows
    install that predates this and already has `~/.config/cc-insights` keeps
    working by passing `--config-dir` or setting CC_INSIGHTS_HOME -- the
    host_id lives in that file and must never be regenerated.
    """
    override = os.environ.get("CC_INSIGHTS_HOME")
    if override:
        return Path(override).expanduser()
    if paths.LOCAL == paths.WINDOWS:
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "cc-insights"
    return Path("~/.config/cc-insights").expanduser()


DEFAULT_CONFIG_DIR = default_config_dir()
DEFAULT_IDLE_THRESHOLD_S = 300

# Why 300s: measured on the real corpus, the inter-event gap distribution has a
# wide flat valley between "agent is working" (p99 = 216s) and "user walked
# away". Any threshold in 120-900s gives materially the same totals, so the
# headline numbers are not an artifact of this knob. See docs/FINDINGS.md.

DEFAULT_SOURCE_GLOBS: dict[str, list[str]] = {
    # Two shapes, both real. The second needs recursive glob expansion (`**`):
    # it catches `<session>/subagents/agent-<id>.jsonl` *and* the workflow
    # nesting `<session>/subagents/workflows/<wf-id>/agent-<id>.jsonl`, which
    # together hold 52% of all Claude Code events. See sources/claude_code.py.
    "claude_code": ["~/.claude/projects/*/*.jsonl", "~/.claude/projects/*/*/subagents/**/*.jsonl"],
    "codex": ["~/.codex/sessions/*/*/*/*.jsonl", "~/.codex/archived_sessions/**/*.jsonl"],
    # Not a log file: one SQLite store holding every session. `-wal`/`-shm` are
    # read through the database handle, so only the main file is matched.
    # See sources/opencode.py.
    "opencode": ["~/.local/share/opencode/opencode.db"],
}

# Windows keeps `~` meaningful (`os.path.expanduser` resolves it to
# %USERPROFILE%), so the globs above already find the usual `.claude` and
# `.codex` directories there. These are the *other* places an installer may
# have put them. A pattern that matches nothing costs one failed glob, so
# guessing wide is cheap and guessing narrow loses history permanently.
WINDOWS_EXTRA_GLOBS: dict[str, list[str]] = {
    "claude_code": [
        "%APPDATA%/claude/projects/*/*.jsonl",
        "%APPDATA%/claude/projects/*/*/subagents/**/*.jsonl",
        "%LOCALAPPDATA%/claude/projects/*/*.jsonl",
        "%LOCALAPPDATA%/claude/projects/*/*/subagents/**/*.jsonl",
    ],
    "codex": [
        "%APPDATA%/codex/sessions/*/*/*/*.jsonl",
        "%APPDATA%/codex/archived_sessions/**/*.jsonl",
        "%LOCALAPPDATA%/codex/sessions/*/*/*/*.jsonl",
        "%LOCALAPPDATA%/codex/archived_sessions/**/*.jsonl",
    ],
    # `~/.local/share` is an XDG path, and Windows has no XDG. opencode's
    # store lands under one of the AppData roots there instead.
    "opencode": [
        "%LOCALAPPDATA%/opencode/opencode.db",
        "%APPDATA%/opencode/opencode.db",
    ],
}


def default_source_globs() -> dict[str, list[str]]:
    """The shipped globs for this platform."""
    globs = {name: list(patterns) for name, patterns in DEFAULT_SOURCE_GLOBS.items()}
    if paths.LOCAL == paths.WINDOWS:
        for name, extra in WINDOWS_EXTRA_GLOBS.items():
            here = globs.setdefault(name, [])
            here.extend(p for p in extra if p not in here)
    return globs


def expand_glob(pattern: str | os.PathLike[str]) -> str:
    """`~` and `%APPDATA%` / `$HOME` resolved, in that order.

    An undefined variable is left verbatim, so a Windows-only pattern read on a
    Mac stays `%APPDATA%/...` and simply matches nothing.
    """
    return os.path.expanduser(os.path.expandvars(str(pattern)))


@dataclass(slots=True)
class Config:
    host_id: str
    hostname: str
    db_path: Path
    idle_threshold_s: int = DEFAULT_IDLE_THRESHOLD_S
    source_globs: dict[str, list[str]] = field(default_factory=default_source_globs)
    config_dir: Path = DEFAULT_CONFIG_DIR
    #: PostgreSQL URL for `cci sync`. Optional: everything else works without it.
    sync_url: str | None = None
    #: Account server for `cci login`. Unlike `sync_url` this is not a secret --
    #: it is an address, and the credential it authenticates with lives in
    #: `credentials.toml`. It is here so an organisation can ship one config
    #: rather than telling everybody to remember a URL, and `cci login
    #: --server` still wins over it.
    server_url: str | None = None

    @property
    def path(self) -> Path:
        return self.config_dir / "config.toml"

    def globs_for(self, source: str) -> list[Path]:
        """Expanded glob patterns for one source."""
        return [Path(expand_glob(p)) for p in self.source_globs.get(source, [])]

    def to_toml(self) -> str:
        lines = [
            "# CC-Insights configuration.",
            "# host_id is generated once and must never change -- it is part of every",
            "# session id. Changing it duplicates your entire history.",
            "",
            f"host_id = {_toml_str(self.host_id)}",
            f"hostname = {_toml_str(self.hostname)}",
            f"db_path = {_toml_str(self._db_path_for_toml())}",
            "",
            "# Seconds of silence that ends an active span. See docs/FINDINGS.md.",
            f"idle_threshold_s = {self.idle_threshold_s}",
            "",
        ]
        if self.sync_url:
            lines += [
                "# Shared PostgreSQL database for `cci sync`. CC_INSIGHTS_SYNC_URL",
                "# overrides this -- prefer the environment if the URL carries a",
                "# password, since this file is plain text.",
                f"sync_url = {_toml_str(self.sync_url)}",
                "",
            ]
        if self.server_url:
            lines += [
                "# Account server for `cci login`. An address, not a secret --",
                "# the token it authenticates with is in credentials.toml, 0600.",
                f"server_url = {_toml_str(self.server_url)}",
                "",
            ]
        lines.append("[source_globs]")
        for name, globs in self.source_globs.items():
            rendered = ", ".join(_toml_str(g) for g in globs)
            lines.append(f"{name} = [{rendered}]")
        return "\n".join(lines) + "\n"

    def _db_path_for_toml(self) -> str:
        """Relative when the database lives inside the config directory.

        Copying a config directory to experiment on is the obvious safety move,
        and with an absolute path here it silently fails: the copy's config
        still points at the ORIGINAL database, so `--config-dir <copy>` writes
        to the real one. That actually happened and corrupted two rows of a
        committed fixture. A relative path makes a copied directory
        self-contained, which is what anyone copying it assumes.
        """
        try:
            return str(self.db_path.relative_to(self.config_dir))
        except ValueError:
            return str(self.db_path)  # deliberately elsewhere: keep it absolute

    def save(self) -> Path:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.path, self.to_toml())
        return self.path


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file and rename, so a crash cannot truncate the config.

    `host_id` lives in this file and must never change. A half-written
    config.toml does not parse, an unparseable config is a config `load` will
    replace, and a replaced config is a regenerated host_id -- which forks the
    entire history. Cheap insurance against an expensive, silent failure.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


# `db_path = "..."` on its own line, before any [table] header. Anchored so a
# key of the same name inside a table could never be mistaken for it.
_DB_PATH_LINE = re.compile(r"^(?P<key>db_path\s*=\s*).*$", re.MULTILINE)


def make_db_path_portable(config_dir: Path) -> Path | None:
    """Rewrite an absolute `db_path` that points inside its own config directory.

    `_db_path_for_toml` has written a relative path since the fix that
    introduced it, but that fix never rewrote the configs already on disk. On
    one of those, copying the config directory to experiment on STILL silently
    writes to the original database -- which is the exact trap the relative
    path exists to close, and it has now caused damage twice: two corrupted
    rows in a committed fixture, and a migration applied to a live database
    during v2.

    Only the one line is touched, so comments, formatting and any key this
    version does not model survive byte-for-byte. The result must parse and
    must resolve to the same file, or nothing is written -- a config that
    cannot be read is worse than the bug being fixed.

    Returns the config path if it changed, None if there was nothing to do.
    """
    path = config_dir / "config.toml"
    if not path.exists():
        return None

    before = path.read_text()
    try:
        raw = tomllib.loads(before)
    except tomllib.TOMLDecodeError:
        return None                       # not ours to repair
    if "db_path" not in raw:
        return None

    cfg = load(config_dir, create=False)
    want = cfg._db_path_for_toml()
    if raw["db_path"] == want:
        return None                       # already portable, or deliberately absolute

    after, count = _DB_PATH_LINE.subn(lambda m: m.group("key") + _toml_str(want), before, count=1)
    if count != 1:
        return None

    # Belt and braces: the rewrite must still parse, and must still name the
    # same database. Anything else and the original stays exactly as it was.
    try:
        checked = tomllib.loads(after)
    except tomllib.TOMLDecodeError:
        return None
    if _resolve_db_path(checked["db_path"], config_dir) != cfg.db_path:
        return None

    _atomic_write(path, after)
    return path


def _toml_str(value: str) -> str:
    """A TOML basic string. The escaping is not cosmetic.

    A Windows `db_path` is `C:\\Users\\you\\AppData\\...`, and in a TOML basic
    string `\\U` and `\\A` are escape sequences -- one is an invalid unicode
    escape and the other is simply not a valid escape, so an unescaped Windows
    path makes the config file this function just wrote unparseable on the
    next run.
    """
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _merge_source_globs(raw: object) -> dict[str, list[str]]:
    """Stored globs, with the defaults filling in for any source not mentioned.

    A config written before a source adapter existed has no entry for it, and
    without this merge that user's new adapter would quietly discover nothing
    for as long as the file sits on disk -- the failure mode is silence, which
    is the worst kind. The file still wins wherever it says something, so a
    customized glob is never overridden. To *exclude* a source, narrow the run
    (`cci ingest --source ...`) rather than deleting its table entry.
    """
    merged = default_source_globs()
    if isinstance(raw, dict):
        merged.update({k: list(v) for k, v in raw.items()})
    return merged


def _resolve_db_path(raw: str, config_dir: Path) -> Path:
    """A relative db_path belongs to the directory its config was read from."""
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (config_dir / p)


def load(config_dir: Path | None = None, *, create: bool = True) -> Config:
    """Load config, creating it with a fresh host_id on first run."""
    config_dir = (config_dir or DEFAULT_CONFIG_DIR).expanduser()
    path = config_dir / "config.toml"

    if path.exists():
        raw = tomllib.loads(path.read_text())
        return Config(
            host_id=raw["host_id"],
            hostname=raw.get("hostname", socket.gethostname()),
            db_path=_resolve_db_path(raw["db_path"], config_dir),
            idle_threshold_s=int(raw.get("idle_threshold_s", DEFAULT_IDLE_THRESHOLD_S)),
            source_globs=_merge_source_globs(raw.get("source_globs")),
            config_dir=config_dir,
            sync_url=raw.get("sync_url"),
            server_url=raw.get("server_url"),
        )

    cfg = Config(
        host_id=str(uuid.uuid4()),
        hostname=socket.gethostname(),
        db_path=config_dir / "cc-insights.db",
        config_dir=config_dir,
    )
    if create:
        cfg.save()
    return cfg


def sync_url_for(cfg: "Config", override: str | None = None) -> str | None:
    """Where `cci sync` talks to: the flag, then the environment, then config.

    The environment sits above the config file on purpose. A PostgreSQL URL
    usually carries a password, and config.toml is plain text that people copy
    around -- `_db_path_for_toml` exists because someone already did.
    """
    return override or os.environ.get("CC_INSIGHTS_SYNC_URL") or cfg.sync_url


def server_url_for(cfg: "Config", override: str | None = None,
                   stored: str | None = None) -> str | None:
    """Which account server to talk to: flag, environment, config, credential.

    `stored` is the URL inside the saved credential and comes LAST on purpose.
    A token is only valid at the server that issued it, so the credential is
    the authoritative answer for "where is this token good" -- but it must not
    override somebody deliberately pointing a command at a different instance,
    which is how they would ever sign in to one.
    """
    return (override or os.environ.get("CC_INSIGHTS_SERVER")
            or cfg.server_url or stored)


def host_os() -> str:
    return f"{platform.system()} {platform.release()}"
