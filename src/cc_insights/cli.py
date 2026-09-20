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

from cc_insights import (
    __version__,
    config as config_mod,
    db,
    derive,
    grouping,
    ingest,
    paths,
    serve,
    stats,
    sync,
)


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

    # `status` diagnoses rather than refuses: it is the command you reach for
    # when something is wrong, so it reports a pending migration instead of
    # becoming another thing that will not run.
    pending = [v for v, _ in db.discover_migrations() if v not in versions]
    if pending:
        print(f"\nschema is out of date (pending: {pending}) — run `cci init` to migrate")
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


def _open_db(args: argparse.Namespace, *, require_current_schema: bool = True):
    """Load config and open the DB, or exit with a useful message.

    A database written before a later migration is a real situation -- `cci`
    upgrades in place while a user's database keeps its history -- and the
    read-only commands cannot migrate it themselves. Failing here with the fix
    beats serving a dashboard whose panels 500 on a missing table.
    """
    cfg = config_mod.load(args.config_dir, create=False)
    if not cfg.db_path.exists():
        print("no database yet \u2014 run `cci init`", file=sys.stderr)
        raise SystemExit(1)
    conn = db.connect(cfg.db_path)
    if require_current_schema:
        applied = db.applied_versions(conn)
        pending = [v for v, _ in db.discover_migrations() if v not in applied]
        if pending:
            conn.close()
            print(
                f"database is on schema {max(applied) if applied else 0}, "
                f"this version needs {max(pending)} \u2014 run `cci init` to migrate "
                f"(it is additive and keeps your history)",
                file=sys.stderr,
            )
            raise SystemExit(1)
    return cfg, conn


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


# ------------------------------------------------------------------- group --

HOME = str(Path.home())


def _tilde(path: str) -> str:
    """`/Users/me/Coding/x` -> `~/Coding/x`. Paths are the bulk of this output.

    A path from another machine has a home directory we cannot know, so it is
    printed in full rather than abbreviated against the wrong one.
    """
    return paths.abbreviate_home(path, HOME)


def _open_grouped_db(args: argparse.Namespace):
    cfg, conn = _open_db(args)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='project_group'"
    ).fetchone()
    if not row:
        conn.close()
        print("database predates grouping — run `cci init` to migrate", file=sys.stderr)
        raise SystemExit(1)
    return cfg, conn


def _print_candidates(err: grouping.AmbiguousMatch) -> None:
    print(f"{err.spec!r} is ambiguous — {len(err.candidates)} matches:", file=sys.stderr)
    for c in err.candidates:
        print(f"    {_tilde(c)}", file=sys.stderr)
    print("  narrow it down, or pass a full path", file=sys.stderr)


def cmd_group_list(args: argparse.Namespace) -> int:
    _, conn = _open_grouped_db(args)
    try:
        views = grouping.list_groups(conn)
        rows = conn.execute("SELECT count(*) FROM project").fetchone()[0]
    finally:
        conn.close()

    if not rows:
        print("no projects yet — run `cci ingest`")
        return 0

    real = [v for v in views if v.group_id]
    loose = len(views) - len(real)
    total_h = sum(v.hours for v in views)
    print(
        f"\nPROJECT GROUPS · {len(real)} groups · {rows} project rows · "
        f"{loose} ungrouped · {_hours(total_h)} active"
    )
    if not real:
        print("  nothing grouped yet — run `cci group auto`")

    spaced = True   # blank lines separate blocks, not every single line
    for v in views:
        tag = v.origin if v.group_id else "ungrouped"
        n = len(v.members)
        paths = "path" if n == 1 else "paths"
        pins = f"  \U0001f4cc{v.pinned_count}" if v.pinned_count else ""
        head = f"  {v.name[:34]:<34} {_hours(v.hours):>9}  {n:>2} {paths:<5} {tag}{pins}"

        if v.group_id is None:
            # An ungrouped project renders as a group of one, so a member line
            # beneath it would only repeat the header. The path goes inline,
            # and a run of them stacks into one readable block.
            m = v.members[0]
            gone = "  (gone)" if m.path_exists == 0 else ""
            if spaced:
                print()
                spaced = False
            print(f"{head}  {_tilde(m.root_path)}{gone}")
            continue

        print()
        spaced = True
        print(head)
        link = v.web_url or v.remote_url
        if link:
            print(f"    → {link}")
        for m in v.members:
            gone = "  (gone)" if m.path_exists == 0 else ""
            pin = " \U0001f4cc" if m.pinned else ""
            print(f"    {_hours(m.hours):>9}  {_tilde(m.root_path)}{gone}{pin}")
    print()
    return 0


def cmd_group_auto(args: argparse.Namespace) -> int:
    cfg, conn = _open_grouped_db(args)
    try:
        # The probe describes THIS machine's disk, so it is filed under this
        # machine's host_id rather than whichever host the database happens to
        # list first. Once a database holds several, that is the difference
        # between a cache and a lie.
        r = grouping.detect(
            conn,
            host_id=cfg.host_id,
            probe_fs=not args.no_probe,
            dry_run=args.dry_run,
        )
        if args.dry_run:
            hours = grouping.project_hours(conn)
            names = {
                row["project_id"]: row["root_path"]
                for row in conn.execute("SELECT project_id, root_path FROM project")
            }
    finally:
        conn.close()

    if args.dry_run:
        print("\nPLAN · nothing was written\n")
        for plan in sorted(r.plans, key=lambda p: -sum(hours.get(m, 0) for m in p.members)):
            h = sum(hours.get(m, 0.0) for m in plan.members)
            n = len(plan.members)
            print(f"  {plan.name[:34]:<34} {_hours(h):>9}  "
                  f"{n:>2} {'path ' if n == 1 else 'paths'}  {plan.origin}")
            for pid in sorted(plan.members, key=lambda m: -hours.get(m, 0.0)):
                print(f"    {_hours(hours.get(pid, 0.0)):>9}  {_tilde(names[pid])}")
        print()

    if args.dry_run:
        print(
            f"would create {r.groups_created} group(s) and place {r.projects_grouped} "
            f"project(s) in {len(r.plans)} group(s)"
        )
        print(
            f"  {r.projects_moved} would move · {r.pinned_skipped} pinned (skipped) · "
            f"{r.ungrouped} would stay ungrouped · "
            f"{r.groups_deleted} empty group(s) would be removed"
        )
    else:
        print(
            f"created {r.groups_created} group(s), grouped {r.projects_grouped} "
            f"project(s) into {len(r.plans)} group(s)"
        )
        print(
            f"  {r.projects_moved} moved · {r.pinned_skipped} pinned (skipped) · "
            f"{r.ungrouped} left ungrouped · {r.groups_deleted} empty group(s) removed"
        )
    if r.by_rule:
        print("  by rule: " + ", ".join(f"{k}={v}" for k, v in sorted(r.by_rule.items())))
    for failure in r.probe_failures[:5]:
        print(f"  probe failed: {failure}", file=sys.stderr)
    return 0


def cmd_group_new(args: argparse.Namespace) -> int:
    _, conn = _open_grouped_db(args)
    try:
        grouping.create_group(conn, args.name)
    except grouping.GroupError as e:
        print(str(e), file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"created manual group {args.name!r}")
    return 0


def cmd_group_set(args: argparse.Namespace) -> int:
    _, conn = _open_grouped_db(args)
    try:
        try:
            picked = [grouping.resolve_project(conn, spec) for spec in args.project]
            group_id, created = grouping.group_for_name(conn, args.to)
        except grouping.AmbiguousMatch as e:
            _print_candidates(e)
            return 2
        except grouping.GroupError as e:
            print(str(e), file=sys.stderr)
            return 1
        grouping.pin_projects(conn, [p["project_id"] for p in picked], group_id)
    finally:
        conn.close()

    if created:
        print(f"created manual group {args.to!r}")
    for p in picked:
        print(f"pinned {_tilde(p['root_path'])} → {args.to}")
    return 0


def cmd_group_unset(args: argparse.Namespace) -> int:
    _, conn = _open_grouped_db(args)
    try:
        try:
            picked = [grouping.resolve_project(conn, spec) for spec in args.project]
        except grouping.AmbiguousMatch as e:
            _print_candidates(e)
            return 2
        except grouping.GroupError as e:
            print(str(e), file=sys.stderr)
            return 1
        grouping.unpin_projects(conn, [p["project_id"] for p in picked])
    finally:
        conn.close()

    for p in picked:
        print(f"unpinned {_tilde(p['root_path'])}")
    print("run `cci group auto` to regroup them automatically")
    return 0


def cmd_group_rename(args: argparse.Namespace) -> int:
    _, conn = _open_grouped_db(args)
    try:
        try:
            g = grouping.resolve_group(conn, args.group)
            grouping.rename_group(conn, g["group_id"], args.new_name)
        except grouping.AmbiguousMatch as e:
            _print_candidates(e)
            return 2
        except grouping.GroupError as e:
            print(str(e), file=sys.stderr)
            return 1
    finally:
        conn.close()
    print(f"renamed {g['name']!r} → {args.new_name!r}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Hand the database to the local HTTP server and block until Ctrl-C.

    `_open_db` is used only to reuse its "no database yet" message; the handle
    is closed immediately, because the server opens its own **read-only**
    connections, one per worker thread.
    """
    cfg, conn = _open_db(args)
    conn.close()
    return serve.run(cfg, port=args.port, open_browser=not args.no_open)


# --------------------------------------------------------------------- sync --


def _open_sync(args: argparse.Namespace):
    """Local database, shared database, and this machine's id -- or exit."""
    cfg, conn = _open_db(args)
    url = config_mod.sync_url_for(cfg, args.url)
    if not url:
        conn.close()
        print(
            "no shared database configured. Set one of:\n"
            "  cci sync push --url postgresql://user@host/db\n"
            "  export CC_INSIGHTS_SYNC_URL=postgresql://user@host/db\n"
            f"  sync_url = \"postgresql://...\"   in {cfg.path}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    try:
        remote = sync.connect_remote(url)
    except Exception as exc:
        conn.close()
        print(f"cannot reach the shared database: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    return cfg, conn, remote


def _print_sync(stats: sync.SyncStats) -> None:
    if not stats.rows:
        print(f"{stats.direction}: nothing to send")
        return
    for table, n in stats.rows.items():
        print(f"  {table:<15} {n:>8,}")
    print(f"  {'total':<15} {stats.total:>8,} rows")


def cmd_sync_push(args: argparse.Namespace) -> int:
    cfg, conn, remote = _open_sync(args)
    try:
        _print_sync(sync.push(conn, remote, cfg.host_id))
    finally:
        conn.close()
        remote.close()
    return 0


def cmd_sync_pull(args: argparse.Namespace) -> int:
    _, conn, remote = _open_sync(args)
    try:
        _print_sync(sync.pull(conn, remote))
    finally:
        conn.close()
        remote.close()
    print("\nrun `cci derive` if you want spans recomputed over the pulled events")
    return 0


def cmd_sync_status(args: argparse.Namespace) -> int:
    cfg, conn, remote = _open_sync(args)
    try:
        rows = remote.execute(
            """SELECT h.host_id, h.hostname, h.os,
                      count(DISTINCT s.id) AS sessions,
                      coalesce(sum(s.active_ms), 0) AS active_ms
               FROM host h LEFT JOIN session s ON s.host_id = h.host_id
               GROUP BY h.host_id, h.hostname, h.os
               ORDER BY active_ms DESC"""
        ).fetchall()
    finally:
        conn.close()
        remote.close()

    if not rows:
        print("the shared database is empty — run `cci sync push`")
        return 0
    print(f"{'hostname':<20} {'os':<16} {'sessions':>9} {'active':>10}")
    for host_id, hostname, os_name, sessions, active_ms in rows:
        mine = "  ← this machine" if host_id == cfg.host_id else ""
        print(
            f"{hostname[:20]:<20} {(os_name or '')[:16]:<16} "
            f"{sessions:>9,} {_hours(active_ms / 3_600_000):>10}{mine}"
        )
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

    grp = sub.add_parser("group", help="collapse a project's many paths into one group")
    gsub = grp.add_subparsers(dest="group_command", required=True)

    gsub.add_parser("list", help="show groups, their paths and their hours").set_defaults(
        fn=cmd_group_list
    )

    auto = gsub.add_parser("auto", help="detect groups from git remotes and paths")
    auto.add_argument("--dry-run", action="store_true",
                      help="print the plan and write nothing")
    auto.add_argument("--no-probe", action="store_true",
                      help="use only cached git metadata; do not touch the filesystem")
    auto.set_defaults(fn=cmd_group_auto)

    gnew = gsub.add_parser("new", help="create an empty manual group")
    gnew.add_argument("name")
    gnew.set_defaults(fn=cmd_group_new)

    gset = gsub.add_parser("set", help="pin projects into a group (never moved by `auto`)")
    gset.add_argument("project", nargs="+",
                      help="name or path substring, case-insensitive")
    gset.add_argument("--to", required=True, metavar="GROUP",
                      help="group name; created as a manual group if it is new")
    gset.set_defaults(fn=cmd_group_set)

    gunset = gsub.add_parser("unset", help="unpin projects, returning them to detection")
    gunset.add_argument("project", nargs="+")
    gunset.set_defaults(fn=cmd_group_unset)

    gren = gsub.add_parser("rename", help="rename a group")
    gren.add_argument("group")
    gren.add_argument("new_name", metavar="new-name")
    gren.set_defaults(fn=cmd_group_rename)

    srv = sub.add_parser("serve", help="serve the dashboard and JSON API on localhost")
    srv.add_argument("--port", type=int, default=serve.DEFAULT_PORT,
                     help=f"port to listen on (default: {serve.DEFAULT_PORT})")
    srv.add_argument("--no-open", action="store_true",
                     help="do not open a browser window")
    srv.set_defaults(fn=cmd_serve)
    syn = sub.add_parser("sync", help="share this machine's data with your others")
    ssub = syn.add_subparsers(dest="sync_command", required=True)
    for name, helptext, fn in (
        ("push", "send this host's rows to the shared database", cmd_sync_push),
        ("pull", "bring every host's rows down into the local database", cmd_sync_pull),
        ("status", "show what the shared database holds, per host", cmd_sync_status),
    ):
        sp = ssub.add_parser(name, help=helptext)
        sp.add_argument(
            "--url",
            default=None,
            help="PostgreSQL URL (default: $CC_INSIGHTS_SYNC_URL, then sync_url in config)",
        )
        sp.set_defaults(fn=fn)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
