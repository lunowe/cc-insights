"""Configuration, stored as TOML at ~/.config/cc-insights/config.toml.

The `host_id` is generated once on first run and must never change: it is part
of every session id, so regenerating it would fork the entire history into a
duplicate set of rows. It is also what makes the multi-machine future a config
concern rather than a migration.
"""

from __future__ import annotations

import os
import platform
import socket
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_DIR = Path(os.environ.get("CC_INSIGHTS_HOME", "~/.config/cc-insights")).expanduser()
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
}


@dataclass(slots=True)
class Config:
    host_id: str
    hostname: str
    db_path: Path
    idle_threshold_s: int = DEFAULT_IDLE_THRESHOLD_S
    source_globs: dict[str, list[str]] = field(default_factory=lambda: dict(DEFAULT_SOURCE_GLOBS))
    config_dir: Path = DEFAULT_CONFIG_DIR

    @property
    def path(self) -> Path:
        return self.config_dir / "config.toml"

    def globs_for(self, source: str) -> list[Path]:
        """Expanded glob patterns for one source."""
        return [Path(p).expanduser() for p in self.source_globs.get(source, [])]

    def to_toml(self) -> str:
        lines = [
            "# CC-Insights configuration.",
            "# host_id is generated once and must never change -- it is part of every",
            "# session id. Changing it duplicates your entire history.",
            "",
            f'host_id = "{self.host_id}"',
            f'hostname = "{self.hostname}"',
            f'db_path = "{self.db_path}"',
            "",
            "# Seconds of silence that ends an active span. See docs/FINDINGS.md.",
            f"idle_threshold_s = {self.idle_threshold_s}",
            "",
            "[source_globs]",
        ]
        for name, globs in self.source_globs.items():
            rendered = ", ".join(f'"{g}"' for g in globs)
            lines.append(f"{name} = [{rendered}]")
        return "\n".join(lines) + "\n"

    def save(self) -> Path:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.to_toml())
        return self.path


def load(config_dir: Path | None = None, *, create: bool = True) -> Config:
    """Load config, creating it with a fresh host_id on first run."""
    config_dir = (config_dir or DEFAULT_CONFIG_DIR).expanduser()
    path = config_dir / "config.toml"

    if path.exists():
        raw = tomllib.loads(path.read_text())
        return Config(
            host_id=raw["host_id"],
            hostname=raw.get("hostname", socket.gethostname()),
            db_path=Path(raw["db_path"]).expanduser(),
            idle_threshold_s=int(raw.get("idle_threshold_s", DEFAULT_IDLE_THRESHOLD_S)),
            source_globs={k: list(v) for k, v in (raw.get("source_globs") or {}).items()}
            or dict(DEFAULT_SOURCE_GLOBS),
            config_dir=config_dir,
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


def host_os() -> str:
    return f"{platform.system()} {platform.release()}"
