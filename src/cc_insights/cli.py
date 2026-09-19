"""`cci` command line entry point.

All rendering lives here; all querying lives in stats.py and derive.py. Export
and dashboard land in Stage 2 and are intentionally absent rather than stubbed --
a command that exists but does nothing is worse than one that does not exist.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from cc_insights import __version__, config as config_mod, db, derive, ingest, stats


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


# ---------------------------------------------------------------- rendering --

BAR = "\u2588"
BAR_BG = "\u2591"


def _bar(fraction: float, width: int = 28) -> str:
    filled = max(0, min(width, round(fraction * width)))
    return BAR * filled + BAR_BG * (width - filled)


def _hours(h: float) -> str:
    return f"{h:,.1f} h"


def _open_db(args: argparse.Namespace):
    """Load config and open the DB, or exit with a useful message."""
    cfg = config_mod.load(args.config_dir, create=False)
    if not cfg.db_path.exists():
        print("no database yet \u2014 run `cci init`", file=sys.stderr)
        raise SystemExit(1)
    return cfg, db.connect(cfg.db_path)


def cmd_ingest(args: argparse.Namespace) -> int:
    cfg, conn = _open_db(args)
    try:
        db.upsert_host(conn, cfg.host_id, cfg.hostname, config_mod.host_os())
        r = ingest.ingest(conn, cfg, sources=args.source)
    finally:
        conn.close()

    print(
        f"ingested {r.files_ingested:,}/{r.files_discovered:,} files "
        f"({r.files_skipped:,} unchanged) in {r.duration_s:.1f}s"
    )
    print(f"  {r.events_inserted:,} new events from {r.events_read:,} read")
    if r.files_failed:
        print(f"  {r.files_failed} file(s) failed:", file=sys.stderr)
        for e in r.errors[:5]:
            print(f"    {e}", file=sys.stderr)
    return 1 if r.files_failed else 0


def cmd_derive(args: argparse.Namespace) -> int:
    cfg, conn = _open_db(args)
    threshold = args.threshold or cfg.idle_threshold_s
    try:
        d = derive.derive(conn, idle_threshold_s=threshold)
    finally:
        conn.close()
    print(
        f"derived {d.spans:,} spans over {d.threads:,} threads "
        f"({d.active_ms / 3_600_000:,.1f} h active, idle threshold {threshold}s)"
    )
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    cfg, conn = _open_db(args)
    try:
        s = stats.summarize(conn)
        conc = {
            src: derive.concurrency_from_db(conn, source=src)
            for src, _ in s.by_source
        }
    finally:
        conn.close()

    if not s.events:
        print("database is empty \u2014 run `cci ingest && cci derive`")
        return 0
    if not s.spans:
        print("events ingested but no spans \u2014 run `cci derive`")
        return 0

    first = datetime.fromtimestamp(s.first_ts / 1000)
    last = datetime.fromtimestamp(s.last_ts / 1000)
    days = max(1, (last - first).days)

    print(f"\nCC-Insights \u00b7 {s.hostname}")
    print(
        f"{first:%Y-%m-%d} \u2192 {last:%Y-%m-%d}  \u00b7  {days} days  \u00b7  "
        f"{s.sessions:,} sessions  \u00b7  {s.threads:,} threads  \u00b7  {s.events:,} events"
    )

    print("\nACTIVE AGENT TIME")
    for src, h in s.by_source:
        share = h / s.total_h if s.total_h else 0
        print(f"  {src:<14} {_hours(h):>9}  {_bar(share)} {share:>4.0%}")
    print(f"  {'total':<14} {_hours(s.total_h):>9}")

    print("\nHOW THAT TIME SPLITS")
    for label, h, note in (
        ("human-initiated", s.human_h, "a person typed the turn that started it"),
        ("autonomous", s.autonomous_h, "a model spawned it (subagent threads)"),
        ("unattended root", s.unattended_root_h, "agent ran on in the main thread"),
    ):
        share = h / s.total_h if s.total_h else 0
        print(f"  {label:<16} {_hours(h):>9} {share:>5.0%}   {note}")

    for src, c in conc.items():
        if c.wall_ms <= 0:
            continue
        wall_h = c.wall_ms / 3_600_000
        active_h = dict(s.by_source).get(src, 0.0)
        ge2 = c.at_least(2) / 3_600_000
        print(f"\nPARALLELISM \u00b7 {src}")
        print(f"  {'wall-clock, 1+ active':<24} {_hours(wall_h):>9}")
        print(f"  {'2+ running at once':<24} {_hours(ge2):>9} {ge2 / wall_h:>5.0%}")
        print(f"  {'peak concurrent threads':<24} {c.peak:>9}")
        print(f"  {'multiplier':<24} {active_h / wall_h:>8.2f}x")
        levels = " ".join(
            f"{lvl}:{ms / 3_600_000:.1f}h" for lvl, ms in sorted(c.time_at_level.items())
        )
        print(f"  {'distribution':<24} {levels}")

    if s.projects:
        print("\nTOP PROJECTS")
        top = s.projects[0][1] or 1
        for name, h in s.projects:
            print(f"  {name[:28]:<28} {_hours(h):>9}  {_bar(h / top, 18)}")

    if s.agents:
        print("\nSUBAGENT TYPES \u00b7 claude_code")
        top = s.agents[0][2] or 1
        for name, n, h in s.agents:
            print(f"  {name[:22]:<22} {_hours(h):>9}  {n:>4} threads  {_bar(h / top, 14)}")

    if s.models:
        print("\nMODELS")
        print("  " + "  ".join(f"{m} ({n:,})" for m, n in s.models[:5]))

    inp, out, cache = s.tokens
    if inp or out:
        print("\nTOKENS")
        print(f"  input {inp / 1e6:,.1f}M  \u00b7  output {out / 1e6:,.1f}M  "
              f"\u00b7  cache read {cache / 1e6:,.1f}M")
    print()
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

    ing = sub.add_parser("ingest", help="read agent logs into the database")
    ing.add_argument("--source", action="append",
                     help="limit to a source (repeatable): claude_code, codex")
    ing.set_defaults(fn=cmd_ingest)

    der = sub.add_parser("derive", help="recompute active spans from ingested events")
    der.add_argument("--threshold", type=int, default=None,
                     help="idle threshold in seconds (default: from config)")
    der.set_defaults(fn=cmd_derive)

    sub.add_parser("stats", help="summarize agent usage").set_defaults(fn=cmd_stats)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
