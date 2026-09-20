"""`cci` command line entry point.

All rendering lives here; all querying lives in stats.py and derive.py. Export
and dashboard land in Stage 2 and are intentionally absent rather than stubbed --
a command that exists but does nothing is worse than one that does not exist.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from cc_insights import (
    __version__,
    account,
    backfill as backfill_mod,
    config as config_mod,
    cost as cost_mod,
    db,
    derive,
    doctor as doctor_mod,
    grouping,
    ingest,
    paths,
    pricing,
    redact,
    remote,
    scheduler,
    serve,
    stats,
    sync,
    watch as watch_mod,
)


def cmd_init(args: argparse.Namespace) -> int:
    # Whether the file existed has to be checked BEFORE load(), which creates
    # it. Checking after meant `created_config` was always False and the
    # "(created)" marker never appeared on a first run.
    config_dir = (args.config_dir or config_mod.DEFAULT_CONFIG_DIR).expanduser()
    created_config = not (config_dir / "config.toml").exists()

    cfg = config_mod.load(args.config_dir)
    # An absolute db_path inside its own config directory makes a copy of that
    # directory point back at the original database. Configs written before
    # that was fixed are still on disk, so repair them here rather than leaving
    # a trap armed for whoever next copies one.
    repaired = config_mod.make_db_path_portable(cfg.config_dir)

    conn = db.connect(cfg.db_path)
    try:
        applied = db.migrate(conn)
        db.upsert_host(conn, cfg.host_id, cfg.hostname, config_mod.host_os())
    finally:
        conn.close()

    print(f"config  {cfg.path}{'  (created)' if created_config else ''}")
    if repaired is not None:
        print("        db_path made relative — copying this directory is now safe")
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
    """Spans, then costs.

    `derive.py` owns spans and `cost.py` owns costs; they stay separate
    modules because they change for different reasons. They run together here
    because a dashboard that has just re-derived its hours and still shows
    yesterday's money is not a subtlety anyone will forgive.
    """
    cfg, conn = _open_db(args)
    threshold = args.threshold or cfg.idle_threshold_s
    try:
        d = derive.derive(conn, idle_threshold_s=threshold)
        c = None if args.no_cost else cost_mod.derive_costs(conn)
    finally:
        conn.close()
    print(
        f"derived {d.spans:,} spans over {d.threads:,} threads "
        f"({d.active_ms / 3_600_000:,.1f} h active, idle threshold {threshold}s)"
    )
    if c is not None:
        print(f"  priced {c.events:,} events at {_money(c.total, c.currency)} list-price equivalent")
    return 0


# -------------------------------------------------------------------- cost --


def _money(amount: float, currency: str = "USD") -> str:
    symbol = {"USD": "$", "EUR": "\u20ac", "GBP": "\u00a3"}.get(currency, "")
    return f"{symbol}{amount:,.2f}" if symbol else f"{amount:,.2f} {currency}"


#: How each rate component reads in a report.
_COMPONENT_LABEL = {
    "input": "input",
    "output": "output",
    "cache_read": "cache read",
    "cache_write": "cache write 5m",
    "cache_write_1h": "cache write 1h",
}


def _mtok(rate: float | None) -> str:
    return "—" if rate is None else f"{rate:g}"


def _print_price_caveats(conn) -> None:
    """Everything that makes a printed total less than the whole truth.

    This is not decoration. A list-price total is the number most likely to be
    quoted at work, and it is quotable only with its qualifications attached.
    """
    approx = pricing.approximations(conn)
    if approx:
        print("\n  priced as a near relative, which the catalog has not split yet:")
        for model, matched in approx:
            print(f"    {model:<28} priced as {matched}")
        print("    correct one with: cci price set <model> --input ... --output ...")


def cmd_cost(args: argparse.Namespace) -> int:
    cfg, conn = _open_db(args)
    try:
        if args.sync:
            pricing.sync(conn)
        c = cost_mod.derive_costs(conn)
        print(f"\nLIST-PRICE EQUIVALENT \u00b7 {_money(c.total, c.currency)}")
        print("  what this traffic would have cost at published API rates. Not a bill:")
        print("  a subscription charges a flat fee no matter how many tokens run through it.")

        if c.by_component:
            print("\nBY COMPONENT")
            top = max(c.by_component.values()) or 1
            for component in pricing.COMPONENTS:
                nano = c.by_component.get(component, 0)
                if not nano:
                    continue
                share = nano / (c.total_nano or 1)
                print(f"  {_COMPONENT_LABEL.get(component, component):<16} "
                      f"{_money(nano / cost_mod.NANO, c.currency):>12} {share:>5.0%}  "
                      f"{_bar(nano / top, 18)}")

        if c.by_model:
            print("\nBY MODEL")
            top = max(c.by_model.values()) or 1
            ranked = sorted(c.by_model.items(), key=lambda kv: -kv[1])
            for model, nano in ranked[:10]:
                print(f"  {model[:26]:<26} {_money(nano / cost_mod.NANO, c.currency):>12}  "
                      f"{_bar(nano / top, 18)}")

        u = c.unpriced
        if u.tokens:
            print("\nNOT PRICED")
            for model, tokens in sorted(u.by_model.items(), key=lambda kv: -kv[1]):
                print(f"  {model[:26]:<26} {tokens / 1e6:>10,.1f}M tokens  no rate on file")
            if u.unknown_model_tokens:
                print(f"  {'(no model recorded)':<26} {u.unknown_model_tokens / 1e6:>10,.1f}M "
                      f"tokens  {u.unknown_model_events} events")
            for component, tokens in sorted(u.by_component.items(), key=lambda kv: -kv[1]):
                print(f"  {component + ' tokens':<26} {tokens / 1e6:>10,.1f}M tokens  "
                      f"model priced, this component is not")
            print("  add a rate with: cci price set <model> --input ... --output ...")

        if c.attributed:
            print(f"\n  {c.attributed:,} of {c.events:,} priced events took their model from "
                  "an earlier\n  event in the same thread (Codex records usage without one).")
        if c.assumed_5m_tokens:
            print(f"\n  {c.assumed_5m_tokens / 1e6:,.1f}M cache-write tokens have no recorded "
                  "TTL and are priced at\n  the five-minute rate. A one-hour write costs 2x "
                  "base input against 1.25x,\n  so this is a floor, not a guess at the middle. "
                  "`cci backfill` fills what\n  the logs still hold.")
        _print_price_caveats(conn)
        print()
    finally:
        conn.close()
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    """Fill columns a later migration added, from the logs that still exist.

    Not part of `cci ingest`: ingest never rewrites an event row, and that
    rule is what makes a re-run provably a no-op. This is the deliberate
    exception, run by hand, touching only the named columns.
    """
    cfg, conn = _open_db(args)
    try:
        before = {j.column: backfill_mod.coverage(conn, j.column) for j in backfill_mod.JOBS}
        seen: list[Path] = []
        r = backfill_mod.backfill(conn, cfg, sources=args.source, on_file=seen.append)
        print(f"read {r.files_read:,}/{len(seen):,} log file(s), {r.events_seen:,} events")
        print(f"  filled {r.filled:,} row(s); {r.already_set:,} already knew; "
              f"{r.not_in_database:,} not ingested yet")
        for job in backfill_mod.JOBS:
            known, total = backfill_mod.coverage(conn, job.column)
            was = before[job.column][0]
            share = known / total if total else 1.0
            print(f"\n  {job.column}")
            print(f"    {job.why}")
            print(f"    {known / 1e6:,.1f}M of {total / 1e6:,.1f}M cache-write tokens "
                  f"now have a known TTL ({share:.0%}), up from {was / 1e6:,.1f}M")
            if share < 1:
                print("    the rest is in logs that have aged out; those stay unknown "
                      "and are\n    priced at the five-minute rate, which `cci cost` "
                      "says out loud")
        if r.errors:
            print(f"\n  {len(r.errors)} file(s) failed:", file=sys.stderr)
            for e in r.errors[:5]:
                print(f"    {e}", file=sys.stderr)
        print("\n  run `cci cost` to re-price with them")
    finally:
        conn.close()
    return 1 if r.errors else 0


def cmd_price_list(args: argparse.Namespace) -> int:
    cfg, conn = _open_db(args)
    try:
        rows = list(conn.execute(
            """SELECT model, effective_from, input_mtok, output_mtok, cache_read_mtok,
                      cache_write_mtok, currency, origin, matched_id
               FROM model_price ORDER BY model, effective_from"""
        ))
        in_use = set(pricing.models_in_use(conn))
        if not rows:
            print("no prices yet — run `cci price sync`")
            return 0
        print(f"\n{'MODEL':<28} {'FROM':<11} {'INPUT':>8} {'OUTPUT':>8} "
              f"{'CACHE R':>8} {'CACHE W':>8}  ORIGIN")
        print(f"{'':<28} {'':<11} {'per million tokens':>34}")
        for r in rows:
            when = "—" if not r["effective_from"] else datetime.fromtimestamp(
                r["effective_from"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            print(f"{r['model'][:28]:<28} {when:<11} {_mtok(r['input_mtok']):>8} "
                  f"{_mtok(r['output_mtok']):>8} {_mtok(r['cache_read_mtok']):>8} "
                  f"{_mtok(r['cache_write_mtok']):>8}  {r['origin']}")
        missing = sorted(in_use - {r["model"] for r in rows})
        if missing:
            print(f"\n  no rate on file: {', '.join(missing)}")
        _print_price_caveats(conn)
        overridden = [r for r in rows if r["origin"] == pricing.OVERRIDE]
        if overridden:
            print("\n  corrected against the vendor's own pricing page "
                  "(src/cc_insights/price_overrides.json):")
            for r in overridden:
                print(f"    {r['model']}")
        src = pricing.catalog_source()
        if src:
            print(f"\n  catalog {src.get('repo')}@{(src.get('commit') or '?')[:7]} "
                  f"fetched {src.get('fetched_at')}, plus {src.get('overrides', 0)} "
                  "shipped correction(s)")
            print("  refresh it with: python3 scripts/sync_prices.py")
        print()
    finally:
        conn.close()
    return 0


def cmd_price_sync(args: argparse.Namespace) -> int:
    cfg, conn = _open_db(args)
    try:
        r = pricing.sync(conn)
    finally:
        conn.close()
    print(f"priced {len(r.priced)} model(s) from the catalog, {r.rows_written} rate row(s)")
    if r.overridden:
        print(f"  {len(r.overridden)} priced from the shipped corrections instead: "
              f"{', '.join(sorted(r.overridden))}")
    if r.redundant:
        print(f"  the catalog now has entries of its own for {', '.join(sorted(r.redundant))}"
              " \u2014 those overrides can be deleted from price_overrides.json")
    if r.rows_kept_manual:
        print(f"  kept {r.rows_kept_manual} manual rate(s) untouched")
    if r.unpriced:
        print(f"  no rate available for: {', '.join(sorted(r.unpriced))}")
        print("  set one with: cci price set <model> --input ... --output ...")
    print("  run `cci cost` to apply them")
    return 0


def cmd_price_set(args: argparse.Namespace) -> int:
    cfg, conn = _open_db(args)
    try:
        pricing.set_price(
            conn, args.model,
            effective_from=pricing.day_to_ms(args.since),
            input_mtok=args.input, output_mtok=args.output,
            cache_read_mtok=args.cache_read, cache_write_mtok=args.cache_write,
            currency=args.currency, note=args.note,
        )
    finally:
        conn.close()
    since = f" from {args.since}" if args.since else ""
    print(f"set a manual rate for {args.model}{since}; `cci price sync` will not overwrite it")
    print("  run `cci cost` to apply it")
    return 0


def cmd_price_clear(args: argparse.Namespace) -> int:
    cfg, conn = _open_db(args)
    try:
        gone = pricing.clear_price(conn, args.model, pricing.day_to_ms(args.since) if args.since else None)
    finally:
        conn.close()
    print(f"removed {gone} manual rate(s) for {args.model}")
    if gone:
        print("  run `cci price sync` to fall back to the catalog, then `cci cost`")
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
        print("\nSUBAGENT TYPES \u00b7 claude_code, opencode")
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

    if s.cost_nano:
        print("\nLIST-PRICE EQUIVALENT")
        print(f"  {_money(s.cost_nano / 1e9, s.cost_currency)} at published API rates \u2014 "
              "not a bill; a subscription")
        print("  charges a flat fee no matter how many tokens run through it.")
        if s.unpriced_tokens:
            print(f"  excludes {s.unpriced_tokens / 1e6:,.1f}M tokens with no rate on file.")
        print("  full breakdown: cci cost")
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


# ------------------------------------------------------- install and doctor --

#: Symbols, not colour. This output gets pasted into issues and read over
#: SSH, and a red dot that renders as nothing is a check nobody sees failed.
_MARK = {doctor_mod.OK: "ok  ", doctor_mod.WARN: "warn", doctor_mod.FAIL: "FAIL"}


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report whether this installation is actually capturing anything.

    Exit code is the point: 0 clean, 1 if anything failed. That makes it
    usable from the installer and from a cron guard, not just by eye.
    Warnings do not fail -- an unbuilt dashboard is not a broken capture.
    """
    cfg = config_mod.load(args.config_dir, create=False)
    checks = doctor_mod.run(cfg)

    print(f"config  {cfg.path}")
    print(f"db      {cfg.db_path}")
    print()
    width = max(len(c.name) for c in checks)
    for c in checks:
        print(f"  {_MARK[c.level]}  {c.name:<{width}}  {c.detail}")
        if c.fix:
            print(f"        {'':<{width}}  -> {c.fix}")

    level = doctor_mod.worst(checks)
    print()
    if level == doctor_mod.OK:
        print("everything is working.")
    elif level == doctor_mod.WARN:
        print("working, with notes above.")
    else:
        print("something is wrong — the arrows above say what to run.")
    return 1 if level == doctor_mod.FAIL else 0


def cmd_install(args: argparse.Namespace) -> int:
    """Set the tool up to run by itself, in one command.

    This does the whole first-run sequence rather than only the scheduler
    part -- init, a first ingest, then the job -- because every one of those
    was previously a separate thing to remember, and forgetting the last one
    is unrecoverable: agent logs are pruned on a rolling basis, so a gap in
    capture is a permanent gap in history.

    The first ingest runs in the foreground on purpose. It is the slow one
    (~11 s cold on the author's corpus) and running it here means the
    dashboard has data the first time it is opened, instead of looking
    broken until a scheduled run happens to fire.
    """
    mode = scheduler.WATCH if args.watch else scheduler.INTERVAL

    if args.uninstall:
        try:
            removed = scheduler.uninstall()
        except scheduler.Unsupported as exc:
            print(exc, file=sys.stderr)
            return 1
        print("removed " + (", ".join(removed) if removed else "nothing — no job was installed"))
        return 0

    rc = cmd_init(args)
    if rc != 0:
        return rc
    cfg = config_mod.load(args.config_dir, create=False)

    if not args.no_ingest:
        print()
        print("reading your agent logs for the first time...")
        rc = cmd_ingest(args)
        if rc == 0:
            rc = cmd_derive(args)
        if rc != 0:
            # The job is still worth installing: a first ingest can fail on
            # one malformed log and every run after it succeed.
            print("\nfirst ingest did not finish cleanly — installing the job anyway",
                  file=sys.stderr)

    print()
    try:
        # The config dir, not just its logs subdirectory: the job writes it
        # into its own environment so the background run fills the database
        # this CLI reads. Passing only `log_dir` is what let `cci install
        # --config-dir X` log to X while capturing into ~/.config.
        job_file, cci = scheduler.install(mode, config_dir=cfg.config_dir)
    except (scheduler.Unsupported, RuntimeError) as exc:
        print(exc, file=sys.stderr)
        return 1

    label = scheduler.WATCH_LABEL if mode == scheduler.WATCH else scheduler.LABEL
    print(f"installed {label}")
    # `init` leads in both job templates so an upgrade that adds a migration
    # cannot stop capture; print what is actually scheduled, not a tidier
    # version of it. See the comment in the plists.
    runs = f"{cci} init && {cci} watch --quiet" if mode == scheduler.WATCH \
        else f"{cci} init && {cci} ingest && {cci} derive && {cci} sync auto"
    print(f"  runs     {runs}")
    if account.token_for(account.load(cfg.config_dir)):
        print("  pushes   after each run, to your account")
    else:
        # Said out loud, because "install once and a second machine just
        # works" is the promise and sign-in is the step that makes it true.
        print("  pushes   nothing — `cci login` to share with your other machines")
    print("  cadence  " + ("follows the logs, ~2s behind" if mode == scheduler.WATCH
                           else "every 15 minutes, and once now"))
    if job_file is not None:
        print(f"  job      {job_file}")
    print(f"  logs     {cfg.config_dir / 'logs'}")
    print("  remove   cci install --uninstall")
    print()
    print("You are done. It keeps itself current from here — `cci serve` to look,")
    print("`cci doctor` if you ever want to check it is still running.")
    return 0


# ------------------------------------------------------------------ privacy --

#: `publication()` wants the actor from auth, which does not exist yet. The
#: report only needs the shape, and a placeholder that cannot be mistaken for a
#: real identity is better here than inventing one.
PLACEHOLDER_ACTOR = "<actor from auth>"


def cmd_privacy(args: argparse.Namespace) -> int:
    """Show what would and would not cross a team boundary. Sends nothing."""
    _, conn = _open_db(args)
    try:
        unclassified = redact.unclassified(conn)
        pub = redact.publication(conn, PLACEHOLDER_ACTOR)
        checked = redact.audit(pub, redact.local_secrets(conn))
        samples = (pub.repos[:3], pub.sessions[:3]) if args.show else ((), ())
    finally:
        conn.close()

    total = pub.total_ms or 1
    print("\nPUBLISHABLE — behind a git remote, so repo access answers who may see it")
    print(f"  {len(pub.repos)} repos · {len(pub.sessions):,} sessions · "
          f"{_hours(pub.published_ms / 3_600_000)} · {pub.published_ms / total:.0%}")

    print("\nWITHHELD — no remote, so nothing in the data can answer 'may they see this?'")
    print(f"  {pub.withheld_projects} projects · "
          f"{_hours(pub.withheld_ms / 3_600_000)} · {pub.withheld_ms / total:.0%}")
    print("  Counted, not dropped: a view that quietly omits your time is not")
    print("  private, it is wrong, and the reader cannot tell the difference.")

    counts: dict[str, int] = {}
    for f in redact.FIELDS:
        counts[f.verdict] = counts.get(f.verdict, 0) + 1
    print(f"\nFIELDS  {counts.get(redact.PUBLIC, 0)} public · "
          f"{counts.get(redact.PRIVATE, 0)} private · "
          f"{counts.get(redact.DERIVED, 0)} re-keyed   "
          f"({len(redact.FIELDS)} columns)")
    if unclassified:
        print("  UNCLASSIFIED COLUMNS — publication is unsafe until these are ruled on:",
              file=sys.stderr)
        for table, column in unclassified:
            print(f"    {table}.{column}", file=sys.stderr)

    if checked.leaks:
        print(f"\nAUDIT   {len(checked.leaks)} LEAK(S) — this must be empty", file=sys.stderr)
        for line in checked.leaks[:10]:
            print(f"    {line}", file=sys.stderr)
    else:
        print("\nAUDIT   clean — no local path, path-derived id, username or")
        print("        hostname reaches the projection")
    if checked.warnings:
        print(f"        {len(checked.warnings)} to glance at (ordinary words collide; "
              f"see docs/REDACTION.md §5):")
        for line in checked.warnings[:5]:
            print(f"          {line}")

    for repo in samples[0]:
        print(f"\n  repo    {repo.repo_id}  {repo.remote_url}")
    for s in samples[1]:
        print(f"  session {s.session_id}  repo={s.repo_id}  {s.source}  "
              f"branch={s.git_branch!r}  {_hours(s.active_ms / 3_600_000)}")

    print("\nNothing was sent. See docs/REDACTION.md.\n")
    return 1 if (checked.leaks or unclassified) else 0


# ------------------------------------------------------------------ account --


def _now_ms() -> int:
    return int(time.time() * 1000)


def _resolve_server(cfg, args, credential) -> str | None:
    return config_mod.server_url_for(
        cfg, getattr(args, "server", None),
        credential.server_url if credential is not None else None,
    )


def _no_server_message(cfg) -> str:
    return (
        "no account server configured. Point this machine at yours:\n"
        "  cci login --server https://your-instance\n"
        f"  (or export CC_INSIGHTS_SERVER, or set server_url in {cfg.path})"
    )


def _account_client(args, cfg, *, need_token: bool = True):
    """An authenticated client, or exit with the line that fixes it."""
    credential = account.load(cfg.config_dir)
    url = _resolve_server(cfg, args, credential)
    if not url:
        print(_no_server_message(cfg), file=sys.stderr)
        raise SystemExit(1)
    token = account.token_for(credential)
    if need_token and not token:
        print("not signed in.\n  cci login", file=sys.stderr)
        raise SystemExit(1)
    return remote.Client(url, token), credential, url


def _claim_host(cfg, account_id: str) -> str:
    """Record on the local host row which account owns this machine.

    `host_id` is NOT replaced and must never be. It is baked into every
    session id, so reassigning it forks the entire history into a duplicate
    set of rows -- docs/ACCOUNTS.md §4 says so and the atomic write in
    `config.py` guards the same property. A host is *claimed by* an account
    and keeps its identity.
    """
    if not cfg.db_path.exists():
        return "will be claimed when `cci init` creates the database"
    conn = db.connect(cfg.db_path)
    try:
        db.upsert_host(conn, cfg.host_id, cfg.hostname, config_mod.host_os())
        conn.execute("UPDATE host SET account_id = ? WHERE host_id = ?",
                     (account_id, cfg.host_id))
    except sqlite3.OperationalError:
        # A database older than migration 006 has no account_id column. Not
        # fatal: the credential is stored and everything else works, and
        # `init` leads every job line precisely so this repairs itself.
        return "run `cci init` to finish claiming this host"
    finally:
        conn.close()
    return f"claimed ({cfg.host_id})"


def cmd_login(args: argparse.Namespace) -> int:
    """Sign in with the device grant, then claim this machine for the account.

    A redirect flow is not an option here and docs/ACCOUNTS.md §6 says why: a
    CLI cannot reliably receive a browser callback, and the device grant works
    identically over SSH -- which is the case that matters, because the second
    machine is usually not the one in front of you.
    """
    cfg = config_mod.load(args.config_dir)
    credential = account.load(cfg.config_dir)
    url = _resolve_server(cfg, args, credential)
    if not url:
        print(_no_server_message(cfg), file=sys.stderr)
        return 1

    client = remote.Client(url)
    try:
        flow = client.device_start(f"cci {__version__} on {cfg.hostname}")
    except remote.RemoteError as exc:
        print(f"cannot start sign-in: {exc}", file=sys.stderr)
        return 1

    # Both codes, and the user code is the one a person types. Printing the
    # device code instead would have somebody paste a bearer-equivalent
    # secret into a web form.
    print()
    print(f"  Open this page   {flow.verification_uri}")
    print(f"  Enter this code  {flow.user_code}")
    print()
    print(f"  or go straight to {flow.verification_uri_complete}")
    print()
    print("waiting for approval", end="", flush=True)

    def waiting(seconds: int, why: str) -> None:
        if why == remote.SLOW_DOWN:
            print(f"[server asked us to slow to {seconds}s]", end="", flush=True)
        else:
            print(".", end="", flush=True)

    try:
        identity = client.device_await(flow, on_wait=waiting)
    except remote.RemoteError as exc:
        print(f"\n\n{exc}", file=sys.stderr)
        return 1
    print()

    account.save(
        account.Credential(identity.token, identity.account_id, url, _now_ms()),
        cfg.config_dir,
    )
    claimed = _claim_host(cfg, identity.account_id)

    print()
    print(f"signed in as {identity.actor}")
    print(f"  account     {identity.account_id}")
    print(f"  server      {url}")
    print(f"  credential  {cfg.config_dir / 'credentials.toml'}  (0600, never config.toml)")
    print(f"  this host   {cfg.hostname} — {claimed}")

    # Teams are shown because publishing is scoped by them, and somebody who
    # is in none should learn that here rather than from an empty `cci team`.
    try:
        teams = remote.Client(url, identity.token).whoami().get("teams") or []
    except remote.RemoteError:
        teams = []
    if teams:
        print("  teams       " + ", ".join(f"{t['name']} ({t['role']})" for t in teams))

    print()
    print("  cci sync push   send this machine's history to your account")
    print("  cci sync pull   bring your other machines' history down")
    print("  cci publish     share the redacted projection with your team")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    """Clear the stored credential, and revoke it if the server can be reached."""
    cfg = config_mod.load(args.config_dir, create=False)
    credential = account.load(cfg.config_dir)
    if credential is None:
        print("not signed in — nothing to clear")
        if os.environ.get("CC_INSIGHTS_TOKEN"):
            # Not ours to remove, and reporting a sign-out that did not happen
            # would be worse than saying nothing.
            print("  CC_INSIGHTS_TOKEN is set in this shell and still applies")
        return 0

    try:
        remote.Client(credential.server_url, credential.token).logout()
        outcome = "revoked on the server"
        reachable = True
    except remote.RemoteError as exc:
        outcome = f"not revoked — {exc.code or 'the server could not be reached'}"
        reachable = False

    account.clear(cfg.config_dir)
    print("signed out.")
    print(f"  credential  removed from {cfg.config_dir / 'credentials.toml'}")
    print(f"  token       {outcome}")
    if not reachable:
        print("  it stays valid until revoked from a machine that can reach the server")
    return 0


# ------------------------------------------------------------------ publish --


def _print_withholding(pub: redact.Publication, audit: redact.Audit) -> None:
    """What is about to cross the boundary, and what is not, before it moves.

    Printed BEFORE the first request, never after. This is the one command
    that sends somebody's data to a place other people read, and a summary
    that arrives after the send is a receipt, not a decision.
    """
    total = pub.total_ms or 1
    print("\nSENDING — behind a git remote, so repo access answers who may see it")
    print(f"  {len(pub.repos)} repos · {len(pub.sessions):,} sessions · "
          f"{len(pub.spans):,} spans · {_hours(pub.published_ms / 3_600_000)} "
          f"· {pub.published_ms / total:.0%}")
    print("  repo id, remote URL, branch, source, timings and counts. No paths,")
    print("  no hostnames, no project ids, no prompt or response text, no events.")

    print("\nWITHHELD — no remote, so nothing in the data can answer "
          "'may they see this?'")
    print(f"  {pub.withheld_projects} projects · "
          f"{_hours(pub.withheld_ms / 3_600_000)} · {pub.withheld_ms / total:.0%}")
    print("  The hours are sent as a total; the work they describe is not.")
    print("  Counted, not dropped: a view that quietly omits your time is not")
    print("  private, it is wrong, and the reader cannot tell the difference.")

    if audit.warnings:
        print(f"\n  {len(audit.warnings)} thing(s) to glance at "
              "(ordinary words collide; see docs/REDACTION.md §5):")
        for line in audit.warnings[:5]:
            print(f"    {line}")


def cmd_publish(args: argparse.Namespace) -> int:
    """Send `redact.publication()` to the team store, after saying what it is."""
    cfg, conn = _open_db(args)
    try:
        client, _credential, url = _account_client(args, cfg)

        try:
            who = client.whoami()
        except remote.RemoteError as exc:
            print(f"\n{exc}", file=sys.stderr)
            return 1
        actor, account_id = who["actor"], who["accountId"]

        # The actor comes from the server, never from a flag or the local
        # username: the store checks it and a mismatch is a 403, and a client
        # that guessed would fail after printing a report that described a
        # send that could not happen.
        pub = redact.publication(conn, actor, host_id=cfg.host_id)
        try:
            audit = remote.check_publishable(conn, pub)
        except remote.Unsafe as exc:
            print(f"\nREFUSING TO PUBLISH\n  {exc}", file=sys.stderr)
            return 1

        print(f"\npublishing as {actor} to {url}")
        _print_withholding(pub, audit)

        if args.dry_run:
            print("\n--dry-run: nothing was sent.\n")
            return 0

        first_time = not remote.has_published(cfg.config_dir, url, account_id)
        if first_time and not args.yes:
            print("\nThis is the first publish from this machine. Other people will")
            print("be able to read the rows above, for the repos you share with them.")
            answer = input("Type 'publish' to continue: ").strip()
            if answer != "publish":
                print("nothing was sent.")
                return 1

        try:
            results = remote.publish(conn, client, pub, host_id=cfg.host_id,
                                     on_part=lambda kind, n: print(f"  {kind:<10} {n:>8,}"))
        except remote.RemoteError as exc:
            print(f"\n{exc}", file=sys.stderr)
            return 1

        sent = sum(r.applied for r in results.values())
        rejected = sum(r.rejected for r in results.values())
        remote.record_publish(cfg.config_dir, url, account_id,
                              confirmed=True, rows=sent)
        print(f"\npublished {sent:,} rows" + (f", {rejected:,} rejected" if rejected else ""))
        print("  cci team   to see it from the reader's side\n")
        return 0
    finally:
        conn.close()


# --------------------------------------------------------------------- team --


def cmd_team(args: argparse.Namespace) -> int:
    """Repos in scope and the scope-aware aggregate.

    Everything here is computed inside the caller's scope at request time.
    There is no rollup, and docs/ACCOUNTS.md §5 is why: a precomputed total
    that spans a repo the reader cannot see leaks that repo's existence the
    moment they read it.
    """
    cfg = config_mod.load(args.config_dir, create=False)
    client, _credential, url = _account_client(args, cfg)
    try:
        repos = client.team_repos()
        summary = client.team_summary()
    except remote.RemoteError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1

    print(f"\n{url}")
    if not repos:
        print("\nno repos in scope yet.")
        print("  cci publish   to share this machine's repos")
        print("  a teammate adding you to a team roster also puts theirs here\n")
        return 0

    print(f"\nREPOS IN SCOPE  ({len(repos)})")
    width = max(len(r.get("name") or "") for r in repos)
    for repo in sorted(repos, key=lambda r: (r.get("name") or "").casefold()):
        via = ", ".join(repo.get("via") or []) or "unknown"
        branches = "" if repo.get("branchNamesPublished", True) else "  [branch names off]"
        print(f"  {(repo.get('name') or ''):<{width}}  {repo.get('remoteUrl', '')}"
              f"  via {via}{branches}")

    scope = summary.get("scope") or {}
    print(f"\nSUMMARY  {scope.get('repos', 0)} repos · {scope.get('teams', 0)} teams")
    print(f"  active    {_hours(summary.get('activeMs', 0) / 3_600_000)}"
          "   in repos you can see")
    print(f"  sessions  {summary.get('sessions', 0):,}")
    print(f"  spans     {summary.get('spans', 0):,}")

    by_role = summary.get("byRole") or {}
    if by_role:
        parts = [f"{name} {_hours((by_role.get(key) or 0) / 3_600_000)}"
                 for name, key in (("human", "human"), ("autonomous", "autonomous"),
                                   ("unattended", "unattendedRoot"))]
        print("  by role   " + " · ".join(parts))

    for row in summary.get("bySource") or []:
        print(f"    {row.get('source', ''):<14} "
              f"{_hours((row.get('activeMs') or 0) / 3_600_000)}")

    withheld = summary.get("withheld") or {}
    if withheld.get("byActor"):
        # Labelled "all time" deliberately, even when the rest of the page is
        # a range: `redact` has no time dimension for withheld work, because
        # the work it describes has no repo to hang a query on. The payload
        # says `rangeFiltered: false` so a renderer can say this without
        # having to know the reason.
        span = "all time" if not withheld.get("rangeFiltered") else "this range"
        print(f"\nWITHHELD  ({span}, not narrowed by any filter)")
        for row in withheld["byActor"]:
            own = "  ← you" if row.get("publishedMs") is not None else ""
            print(f"  {row.get('actor', ''):<16} "
                  f"{_hours((row.get('withheldMs') or 0) / 3_600_000)}"
                  f"  in {row.get('withheldProjects', 0)} projects{own}")
        print("  This is work in no repo at all. It is counted here and nowhere")
        print("  else, and it does not add up with `active` — the difference is")
        print("  work in repos you cannot see, which is deliberately not reported.")
    print()
    return 0


def cmd_team_sessions(args: argparse.Namespace) -> int:
    """Published sessions inside the caller's scope, newest first."""
    cfg = config_mod.load(args.config_dir, create=False)
    client, _credential, _url = _account_client(args, cfg)
    try:
        body = client.team_sessions(limit=args.limit)
    except remote.RemoteError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1

    rows = body.get("sessions") or []
    if not rows:
        print("no sessions in scope.")
        return 0
    print(f"\n{'actor':<14} {'source':<12} {'branch':<22} {'active':>9}  started")
    for s in rows:
        when = datetime.fromtimestamp((s.get("startedAt") or 0) / 1000).strftime(
            "%Y-%m-%d %H:%M")
        # `gitBranch` is null both when a session never had a branch and when
        # a team switched branch names off. Indistinguishable on purpose --
        # §4.5 -- so there is one rendering and no observer can tell them apart.
        branch = s.get("gitBranch") or "—"
        print(f"{(s.get('actor') or ''):<14} {(s.get('source') or ''):<12} "
              f"{branch[:22]:<22} "
              f"{_hours((s.get('activeMs') or 0) / 3_600_000):>9}  {when}")
    if body.get("nextCursor"):
        print(f"\n  more available — raise --limit (showing {len(rows)})")
    print()
    return 0


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
        shared = sync.connect_remote(url)
    except Exception as exc:
        conn.close()
        print(f"cannot reach the shared database: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    return cfg, conn, shared


def _print_sync(stats: sync.SyncStats) -> None:
    if not stats.rows:
        print(f"{stats.direction}: nothing to send")
        return
    for table, n in stats.rows.items():
        print(f"  {table:<15} {n:>8,}")
    print(f"  {'total':<15} {stats.total:>8,} rows")


#: The two transports, and how a command decides which one it is talking to.
DIRECT = "direct"
ACCOUNT = "account"


def _backend(args: argparse.Namespace, cfg) -> str:
    """Direct PostgreSQL or the account server, from what is configured.

    An explicitly configured `sync_url` wins over being signed in, and the
    order is not arbitrary. docs/ACCOUNTS.md §3 keeps direct Postgres "a
    supported mode, not dead code" for anyone running their own database,
    and setting that URL is a deliberate act; signing in is also needed for
    `cci publish` and for team reads, so it does not on its own say anything
    about where sync should go. `--direct` and `--account` override, and
    every command prints which one it used.
    """
    if getattr(args, "url", None) or getattr(args, "direct", False):
        return DIRECT
    if getattr(args, "account", False):
        return ACCOUNT
    if config_mod.sync_url_for(cfg):
        return DIRECT
    if account.token_for(account.load(cfg.config_dir)):
        return ACCOUNT
    return "none"


def _no_backend_message(cfg) -> str:
    """Every way in, named. This is the failure a first-time user hits."""
    return (
        "nothing to sync with. Pick one:\n"
        "  cci login --server https://your-instance        your account (recommended)\n"
        "  cci sync push --url postgresql://user@host/db   a database you run\n"
        "  export CC_INSIGHTS_SYNC_URL=postgresql://user@host/db\n"
        f"  sync_url = \"postgresql://...\"   in {cfg.path}"
    )


def _print_transfer(stats: remote.TransferStats) -> None:
    for table, n in stats.rows.items():
        resumed = stats.resumed.get(table)
        note = f"   (resumed after {resumed:,})" if resumed else ""
        print(f"  {table:<15} {n:>8,}{note}")
    if stats.skipped:
        # Named rather than counted: "unchanged" is the answer to "why did my
        # push do nothing", and a number does not answer it.
        print(f"  {'unchanged':<15} {'—':>8}   {', '.join(stats.skipped)}")
    if stats.total:
        print(f"  {'total':<15} {stats.total:>8,} rows in {stats.requests} requests")
    else:
        print(f"  {stats.direction}: everything is already up to date")


def cmd_sync_push(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config_dir, create=False)
    backend = _backend(args, cfg)
    if backend == "none":
        # SystemExit, matching `_open_sync`: one convention for "there is
        # nothing configured to talk to", whichever backend was being sought.
        print(_no_backend_message(cfg), file=sys.stderr)
        raise SystemExit(1)

    if backend == ACCOUNT:
        _, conn = _open_db(args)
        client, _credential, url = _account_client(args, cfg)
        print(f"push → your account at {url}")
        try:
            _print_transfer(remote.push(conn, client, cfg.host_id,
                                        config_dir=cfg.config_dir, force=args.full))
        except remote.RemoteError as exc:
            print(f"\n{exc}", file=sys.stderr)
            return 1
        finally:
            conn.close()
        return 0

    cfg, conn, shared = _open_sync(args)
    print("push → the shared PostgreSQL database (direct)")
    try:
        _print_sync(sync.push(conn, shared, cfg.host_id))
    finally:
        conn.close()
        shared.close()
    return 0


def cmd_sync_pull(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config_dir, create=False)
    backend = _backend(args, cfg)
    if backend == "none":
        # SystemExit, matching `_open_sync`: one convention for "there is
        # nothing configured to talk to", whichever backend was being sought.
        print(_no_backend_message(cfg), file=sys.stderr)
        raise SystemExit(1)

    if backend == ACCOUNT:
        _, conn = _open_db(args)
        client, _credential, url = _account_client(args, cfg)
        print(f"pull ← your account at {url}")
        try:
            _print_transfer(remote.pull(conn, client, config_dir=cfg.config_dir,
                                        force=args.full))
        except remote.RemoteError as exc:
            print(f"\n{exc}", file=sys.stderr)
            return 1
        finally:
            conn.close()
    else:
        _, conn, shared = _open_sync(args)
        print("pull ← the shared PostgreSQL database (direct)")
        try:
            _print_sync(sync.pull(conn, shared))
        finally:
            conn.close()
            shared.close()
    print("\nrun `cci derive` if you want spans recomputed over the pulled events")
    return 0


def cmd_sync_auto(args: argparse.Namespace) -> int:
    """Push after the scheduled ingest, when signed in. Never fails the job.

    This is the last step of `cci init && cci ingest && cci derive && cci sync
    auto`, and the ordering is the whole safety argument. Capture has already
    happened by the time this runs, so a server that is unreachable cannot
    cost a single event.

    **It returns 0 even when it could not connect.** That is deliberate. A
    non-zero exit here makes launchd record the run as failed and, worse,
    hides a real ingest failure behind a network one -- and the failure this
    project is most exposed to is the silent kind, so the loud signal has to
    stay attached to the thing that actually loses history.
    """
    try:
        cfg = config_mod.load(args.config_dir, create=False)
    except Exception as exc:                            # pragma: no cover
        print(f"auto-push skipped: unreadable config ({exc})", file=sys.stderr)
        return 0

    credential = account.load(cfg.config_dir)
    token = account.token_for(credential)
    if not token:
        if args.verbose:
            print("auto-push: not signed in — nothing to do")
        return 0
    url = _resolve_server(cfg, args, credential)
    if not url or not cfg.db_path.exists():
        return 0

    try:
        conn = db.connect(cfg.db_path)
    except sqlite3.Error as exc:                        # pragma: no cover
        print(f"auto-push skipped: {exc}", file=sys.stderr)
        return 0
    try:
        stats = remote.push(conn, remote.Client(url, token), cfg.host_id,
                            config_dir=cfg.config_dir)
    except Exception as exc:
        # Bare `Exception` on purpose, and it is the narrow case where that is
        # right: this runs unattended every 15 minutes, and there is no
        # failure here worth stopping a capture pipeline over. The message
        # still lands in ingest.err, and `cci doctor` reports a stale push.
        print(f"auto-push skipped: {exc}", file=sys.stderr)
        return 0
    finally:
        conn.close()

    if stats.total and args.verbose:
        print(f"auto-push: {stats.total:,} rows in {stats.requests} requests")
    return 0


def cmd_sync_status(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config_dir, create=False)
    if _backend(args, cfg) == ACCOUNT:
        client, _credential, url = _account_client(args, cfg)
        try:
            body = client.personal_status()
        except remote.RemoteError as exc:
            print(f"\n{exc}", file=sys.stderr)
            return 1
        print(f"\nyour account at {url}")
        if not body.get("hosts"):
            print("nothing pushed yet — run `cci sync push`")
            return 0
        print(f"\n{'hostname':<20} {'os':<16}  first seen")
        for host in body["hosts"]:
            mine = "  ← this machine" if host["hostId"] == cfg.host_id else ""
            when = datetime.fromtimestamp((host.get("firstSeen") or 0) / 1000)
            print(f"{(host.get('hostname') or '')[:20]:<20} "
                  f"{(host.get('os') or '')[:16]:<16}  "
                  f"{when:%Y-%m-%d}{mine}")
        print()
        for row in body.get("tables") or []:
            print(f"  {row['table']:<15} {row['rows']:>8,}")
        print(f"  {'total':<15} {body.get('totalRows', 0):>8,} rows\n")
        return 0

    cfg, conn, shared = _open_sync(args)
    try:
        rows = shared.execute(
            """SELECT h.host_id, h.hostname, h.os,
                      count(DISTINCT s.id) AS sessions,
                      coalesce(sum(s.active_ms), 0) AS active_ms
               FROM host h LEFT JOIN session s ON s.host_id = h.host_id
               GROUP BY h.host_id, h.hostname, h.os
               ORDER BY active_ms DESC"""
        ).fetchall()
    finally:
        conn.close()
        shared.close()

    if not rows:
        print("the shared database is empty — run `cci sync push`")
        return 0
    print("the shared PostgreSQL database (direct)\n")
    print(f"{'hostname':<20} {'os':<16} {'sessions':>9} {'active':>10}")
    for host_id, hostname, os_name, sessions, active_ms in rows:
        mine = "  ← this machine" if host_id == cfg.host_id else ""
        print(
            f"{hostname[:20]:<20} {(os_name or '')[:16]:<16} "
            f"{sessions:>9,} {_hours(active_ms / 3_600_000):>10}{mine}"
        )
    print()
    return 0
# ------------------------------------------------------------------- watch --


def _cycle_line(cycle: watch_mod.Cycle) -> str:
    when = datetime.fromtimestamp(cycle.at / 1000).strftime("%H:%M:%S")
    if cycle.errors:
        return f"{when}  {len(cycle.errors)} error(s): {cycle.errors[0]}"
    # The delta, then where it left the corpus -- in that order, because the
    # second is the number someone glancing at a terminal is looking for.
    return (
        f"{when}  +{cycle.events_inserted:,} events \u00b7 {cycle.sessions} session(s)"
        f"  \u2192  {cycle.active_ms / 3_600_000:,.1f} h total"
        f", {_money(cycle.cost_total, cycle.currency)}"
        f"  ({cycle.duration_s:.2f}s)"
    )


#: How often `cci watch` pushes, at most. The loop ticks every two seconds
#: and pushing at that rate would be a request storm buying nothing -- a
#: teammate's view being five minutes behind is not a problem anybody has.
#: Faster than the 15-minute job on purpose, because watch mode is for people
#: who want to see work as it happens.
WATCH_PUSH_EVERY_S = 300


def _auto_pusher(cfg, args):
    """A throttled push for the watch loop, or None when not signed in.

    Opens its own read connection per push rather than borrowing the loop's.
    That handle belongs to the loop thread and is busy writing; WAL takes a
    second reader without either one blocking, and push only ever reads.
    """
    credential = account.load(cfg.config_dir)
    token = account.token_for(credential)
    url = _resolve_server(cfg, args, credential)
    if not token or not url:
        return None

    last = {"at": 0.0}

    def maybe_push(cycle: watch_mod.Cycle) -> None:
        if not cycle.did_work:
            return
        now = time.monotonic()
        if last["at"] and now - last["at"] < WATCH_PUSH_EVERY_S:
            return
        last["at"] = now
        conn = None
        try:
            conn = db.connect(cfg.db_path)
            remote.push(conn, remote.Client(url, token), cfg.host_id,
                        config_dir=cfg.config_dir)
        except Exception as exc:
            # Never kill the watcher. `watch.py` makes the same call for a bad
            # log file and gives the reason: a watcher that exits on the first
            # error is a watcher that is not running when it matters, and the
            # logs it would have read are pruned on a rolling basis.
            print(f"auto-push skipped: {exc}", file=sys.stderr, flush=True)
        finally:
            if conn is not None:
                conn.close()

    return maybe_push


def cmd_watch(args: argparse.Namespace) -> int:
    """Follow the logs and keep the database current until Ctrl-C.

    With `--serve` the dashboard runs alongside in this process and refreshes
    itself as the logs grow; the loop feeds it through `/api/live`. The server
    owns the main thread there because it is the thing that must answer
    promptly, and the loop is the background work.

    When signed in it also pushes, at most every `WATCH_PUSH_EVERY_S`. That
    is the watch-mode counterpart of `cci sync auto` in the interval job, and
    it is here rather than in `watch.py` because pushing is not part of
    keeping the local database current -- the loop must stay something that
    works with no account at all.
    """
    cfg, conn = _open_db(args)
    interval = args.interval
    stop = threading.Event()
    quiet = args.quiet
    pusher = _auto_pusher(cfg, args)

    def report(cycle: watch_mod.Cycle) -> None:
        # A cycle that changed nothing prints nothing: a watcher that scrolls
        # while you are not working is a watcher you stop reading.
        if not quiet and (cycle.did_work or cycle.errors):
            print(_cycle_line(cycle), flush=True)
        if pusher is not None:
            pusher(cycle)

    if not args.serve:
        print(f"watching {cfg.db_path} every {interval:g}s — Ctrl-C to stop", flush=True)
        try:
            watch_mod.watch(conn, cfg, interval_s=interval, sources=args.source,
                            on_cycle=report, stop=stop)
        except KeyboardInterrupt:
            print()
        finally:
            conn.close()
        return 0

    # The handle `_open_db` returned belongs to this thread and sqlite3
    # refuses to let another one use it. It has already done its job -- it is
    # how we know the database exists and is migrated -- so it is closed here
    # and the loop opens its own inside the worker. The server, meanwhile,
    # opens a read-only handle per request thread. WAL is what lets one writer
    # and several readers overlap without blocking.
    conn.close()
    live = watch_mod.LiveState()
    worker: threading.Thread | None = None

    def loop() -> None:
        writer = db.connect(cfg.db_path)
        try:
            watch_mod.watch(writer, cfg, interval_s=interval, sources=args.source,
                            live=live, on_cycle=report, stop=stop)
        finally:
            writer.close()

    def start_loop(_server) -> None:
        nonlocal worker
        worker = threading.Thread(target=loop, name="cci-watch", daemon=True)
        worker.start()

    try:
        return serve.run(cfg, port=args.port, open_browser=not args.no_open,
                         live=live, on_ready=start_loop)
    finally:
        stop.set()
        if worker is not None:
            worker.join(timeout=5)


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
                     help="limit to a source (repeatable): claude_code, codex, opencode")
    ing.set_defaults(fn=cmd_ingest)

    der = sub.add_parser("derive", help="recompute active spans from ingested events")
    der.add_argument("--threshold", type=int, default=None,
                     help="idle threshold in seconds (default: from config)")
    der.add_argument("--no-cost", action="store_true",
                     help="skip the cost pass (spans only)")
    der.set_defaults(fn=cmd_derive)

    sub.add_parser("stats", help="summarize agent usage").set_defaults(fn=cmd_stats)

    cst = sub.add_parser("cost", help="what this traffic would cost at published API rates")
    cst.add_argument("--sync", action="store_true",
                     help="refresh rates from the bundled catalog first")
    cst.set_defaults(fn=cmd_cost)

    bkf = sub.add_parser(
        "backfill",
        help="fill columns a later migration added, from logs still on disk")
    bkf.add_argument("--source", action="append",
                     help="limit to a source (repeatable): claude_code, codex, opencode")
    bkf.set_defaults(fn=cmd_backfill)

    prc = sub.add_parser("price", help="inspect and override the model price table")
    psub = prc.add_subparsers(dest="price_command", required=True)
    psub.add_parser("list", help="show every rate on file").set_defaults(fn=cmd_price_list)
    psub.add_parser("sync", help="load rates for models in use from the bundled catalog"
                    ).set_defaults(fn=cmd_price_sync)

    pset = psub.add_parser("set", help="write a manual rate; sync never overwrites one")
    pset.add_argument("model")
    pset.add_argument("--input", type=float, metavar="PER_MTOK")
    pset.add_argument("--output", type=float, metavar="PER_MTOK")
    pset.add_argument("--cache-read", type=float, metavar="PER_MTOK")
    pset.add_argument("--cache-write", type=float, metavar="PER_MTOK")
    pset.add_argument("--since", metavar="YYYY-MM-DD",
                      help="the day this rate took effect (default: always)")
    pset.add_argument("--currency", default="USD")
    pset.add_argument("--note")
    pset.set_defaults(fn=cmd_price_set)

    pclr = psub.add_parser("clear", help="drop manual rates and fall back to the catalog")
    pclr.add_argument("model")
    pclr.add_argument("--since", metavar="YYYY-MM-DD")
    pclr.set_defaults(fn=cmd_price_clear)

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

    wat = sub.add_parser("watch", help="follow the logs and keep the database current")
    wat.add_argument("--interval", type=float, default=watch_mod.DEFAULT_INTERVAL_S,
                     help=f"seconds between scans (default: {watch_mod.DEFAULT_INTERVAL_S:g})")
    wat.add_argument("--source", action="append",
                     help="limit to a source (repeatable): claude_code, codex, opencode")
    wat.add_argument("--serve", action="store_true",
                     help="also serve the dashboard, refreshing it as the logs grow")
    wat.add_argument("--port", type=int, default=serve.DEFAULT_PORT,
                     help=f"port for --serve (default: {serve.DEFAULT_PORT})")
    wat.add_argument("--no-open", action="store_true",
                     help="with --serve, do not open a browser window")
    wat.add_argument("--quiet", action="store_true", help="print nothing per cycle")
    wat.add_argument("--server", default=None, metavar="URL",
                     help="account server to push to (default: the saved credential)")
    wat.set_defaults(fn=cmd_watch)

    srv = sub.add_parser("serve", help="serve the dashboard and JSON API on localhost")
    srv.add_argument("--port", type=int, default=serve.DEFAULT_PORT,
                     help=f"port to listen on (default: {serve.DEFAULT_PORT})")
    srv.add_argument("--no-open", action="store_true",
                     help="do not open a browser window")
    srv.set_defaults(fn=cmd_serve)
    ins = sub.add_parser(
        "install",
        help="set up everything and keep it running in the background",
        description="Init, a first ingest, and a background job. The one "
                    "command a new machine needs.",
    )
    ins.add_argument("--watch", action="store_true",
                     help="follow the logs live instead of every 15 minutes")
    ins.add_argument("--no-ingest", action="store_true",
                     help="skip the first ingest; install the job only")
    ins.add_argument("--uninstall", action="store_true",
                     help="remove the background job (both kinds). Keeps your data.")
    # cmd_install delegates to cmd_ingest and cmd_derive, which read these.
    ins.set_defaults(fn=cmd_install, source=None, threshold=None, no_cost=False)

    sub.add_parser(
        "doctor", help="check that this installation is capturing anything"
    ).set_defaults(fn=cmd_doctor)

    priv = sub.add_parser(
        "privacy", help="show what would and would not cross a team boundary"
    )
    priv.add_argument(
        "--show", action="store_true", help="also print a few sample published rows"
    )
    priv.set_defaults(fn=cmd_privacy)

    log = sub.add_parser(
        "login",
        help="sign in to your account server and claim this machine",
        description="Device-code sign-in. Prints a code, you approve it in a "
                    "browser, and this machine is claimed for the account.",
    )
    log.add_argument("--server", default=None, metavar="URL",
                     help="account server (default: $CC_INSIGHTS_SERVER, then "
                          "server_url in config, then the saved credential)")
    log.set_defaults(fn=cmd_login)

    sub.add_parser(
        "logout", help="clear the stored credential and revoke it"
    ).set_defaults(fn=cmd_logout, server=None)

    pub = sub.add_parser(
        "publish",
        help="share the redacted projection with your team",
        description="Sends redact.publication() and nothing else: no paths, "
                    "no hostnames, no project ids, no events. Prints what is "
                    "withheld before anything moves.",
    )
    pub.add_argument("--yes", action="store_true",
                     help="skip the first-use confirmation")
    pub.add_argument("--dry-run", action="store_true",
                     help="print the report and send nothing")
    pub.add_argument("--server", default=None, metavar="URL")
    pub.set_defaults(fn=cmd_publish)

    tm = sub.add_parser("team", help="what your team can see, and what you share")
    tm.add_argument("--server", default=None, metavar="URL")
    tm.set_defaults(fn=cmd_team)
    # Not `required=True`: `cci team` on its own is the useful default -- the
    # repos in scope and the summary -- and making somebody pick a subcommand
    # to see the obvious thing is a worse first run.
    tsub = tm.add_subparsers(dest="team_command", required=False)
    tsess = tsub.add_parser("sessions", help="published sessions inside your scope")
    tsess.add_argument("--limit", type=int, default=50)
    tsess.add_argument("--server", default=None, metavar="URL")
    tsess.set_defaults(fn=cmd_team_sessions)

    syn = sub.add_parser("sync", help="share this machine's data with your others")
    ssub = syn.add_subparsers(dest="sync_command", required=True)
    for name, helptext, fn in (
        ("push", "send this host's rows to your account or shared database", cmd_sync_push),
        ("pull", "bring every host's rows down into the local database", cmd_sync_pull),
        ("status", "show what the other side holds, per host", cmd_sync_status),
    ):
        sp = ssub.add_parser(name, help=helptext)
        sp.add_argument(
            "--url",
            default=None,
            help="PostgreSQL URL, and forces the direct backend "
                 "(default: $CC_INSIGHTS_SYNC_URL, then sync_url in config)",
        )
        sp.add_argument("--server", default=None, metavar="URL",
                        help="account server URL")
        backend = sp.add_mutually_exclusive_group()
        backend.add_argument("--account", action="store_true",
                             help="force the account server")
        backend.add_argument("--direct", action="store_true",
                             help="force direct PostgreSQL")
        sp.add_argument("--full", action="store_true",
                        help="re-send everything, ignoring what is already there")
        sp.set_defaults(fn=fn)

    auto = ssub.add_parser(
        "auto",
        help="push if signed in; do nothing otherwise. For the background job.",
        description="What the scheduled job runs after ingest and derive. "
                    "Exits 0 even when the server is unreachable, so a network "
                    "failure can never be mistaken for a capture failure.",
    )
    auto.add_argument("--verbose", action="store_true",
                      help="say what happened even when nothing did")
    auto.add_argument("--server", default=None, metavar="URL")
    auto.set_defaults(fn=cmd_sync_auto)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
