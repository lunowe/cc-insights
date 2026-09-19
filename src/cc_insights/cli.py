"""`cci` command line entry point.

Stage 0 ships `init`, `status` and `config`. Ingest, derive, stats, export and
dashboard land in later work packages and are intentionally absent rather than
stubbed -- a command that exists but does nothing is worse than one that does
not exist.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cc_insights import __version__, config as config_mod, db


def cmd_init(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config_dir)
    created_config = not cfg.path.exists()
    if created_config:
        cfg.save()

    conn = db.connect(cfg.db_path)
    try:
        applied = db.migrate(conn)
        db.upsert_host(conn, cfg.host_id, cfg.hostname, config_mod.host_os())
    finally:
        conn.close()

    print(f"config  {cfg.path}{'  (created)' if created_config else ''}")
    print(f"db      {cfg.db_path}")
    print(f"host_id {cfg.host_id}")
    print(
        f"schema  migrations applied: {applied}"
        if applied
        else "schema  already up to date"
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config_dir, create=False)
    if not cfg.db_path.exists():
        print("no database yet — run `cci init`")
        return 1

    conn = db.connect(cfg.db_path)
    try:
        versions = sorted(db.applied_versions(conn))
        counts = {
            t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("session", "thread", "event", "span", "ingest_file")
        }
    finally:
        conn.close()

    print(f"db               {cfg.db_path}")
    print(f"schema versions  {versions}")
    print(f"idle threshold   {cfg.idle_threshold_s}s")
    for table, n in counts.items():
        print(f"{table:<16} {n:,}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config_dir, create=False)
    print(cfg.path)
    print()
    print(cfg.to_toml(), end="")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cci", description="CC-Insights — coding-agent usage tracking")
    p.add_argument("--version", action="version", version=f"cc-insights {__version__}")
    p.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        help="override the config directory (default: ~/.config/cc-insights)",
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create the config and database").set_defaults(fn=cmd_init)
    sub.add_parser("status", help="show database contents").set_defaults(fn=cmd_status)
    sub.add_parser("config", help="print the resolved configuration").set_defaults(fn=cmd_config)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
