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
        print("\n  priced as a near relative (no exact catalog key for the name):")
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
            fallback = src.get("fallback") or {}
            also = (f", {fallback['name']} as fallback" if fallback.get("name") else "")
            print(f"\n  catalog {src.get('repo')}@{(src.get('commit') or '?')[:7]}{also}, "
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
        print(f"  the catalog now agrees with the correction(s) for "
              f"{', '.join(sorted(r.redundant))}"
              " \u2014 move them to `retired` in price_overrides.json")
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
        except (scheduler.Unsupported, RuntimeError) as exc:
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
    elif scheduler.uses_cron():
        print(f"  job      your crontab, the line ending `{scheduler.CRON_TAG}`")
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
        "  (a join link from `cci team invite` carries the server: cci team join <link>)\n"
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

    identity = _sign_in(cfg, url)
    if identity is None:
        return 1
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


def _sign_in(cfg, url: str):
    """Run the device grant against `url` and store the credential.

    Returns the identity, or None after saying why on stderr. Shared by
    `cci login` and by `cci team join <link>`, which signs a fresh machine in
    on the way to redeeming the code.
    """
    client = remote.Client(url)
    try:
        flow = client.device_start(f"cci {__version__} on {cfg.hostname}")
    except remote.RemoteError as exc:
        print(f"cannot start sign-in: {exc}", file=sys.stderr)
        return None

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
        return None
    print()

    account.save(
        account.Credential(identity.token, identity.account_id, url, _now_ms()),
        cfg.config_dir,
    )
    return identity


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
#
# THE SHAPE OF THIS COMMAND SURFACE, and why it is not the HTTP routes.
#
# The wire contract is organised around resources, because that is what a
# wire contract is for. A person at a terminal is not holding a resource
# model; they are holding a question, and the questions are: who is on my
# team, how do I let somebody in, what am I sharing, and how do I stop. So
# the verbs are named for those and the ids are hidden wherever a name will
# do -- `cci team share cc-insights`, not a 64-character hash, because a
# repo id is a sha256 and nobody is going to type one correctly.
#
# Everything that needs a team takes `--team`, and omitting it when you are
# on exactly one team picks that one. That is not a shortcut bolted on: one
# team is the case docs/ACCOUNTS.md §1 describes ("one private instance, for
# one team"), so making it the wordless path is making the common case the
# short one. With several teams it refuses and lists them, rather than
# guessing about an operation that changes who can read somebody's work.


def _print_teams(teams: list[dict], *, stream=sys.stdout) -> None:
    width = max((len(t.get("name") or "") for t in teams), default=4)
    for t in teams:
        print(f"  {(t.get('name') or ''):<{width}}  {t.get('role', ''):<6} "
              f" {t.get('teamId', '')}", file=stream)


def _resolve_team(client, spec: str | None) -> dict:
    """The team a command applies to, or exit saying how to say which.

    Accepts an exact team id or a case-insensitive substring of the name,
    because a person who can see the name on the previous command's output
    should be able to type it. Ambiguity is never resolved by picking one:
    these commands change who can read other people's data, and a `share`
    that landed on the wrong team is not something the user would be told
    about.
    """
    try:
        teams = client.teams()
    except remote.RemoteError as exc:
        print(f"\n{exc}", file=sys.stderr)
        raise SystemExit(1)

    if not teams:
        print("you are not on a team yet.\n"
              "  cci team new <name>     to start one\n"
              "  cci team join <code>    if a colleague sent you a join code",
              file=sys.stderr)
        raise SystemExit(1)

    if spec is None:
        if len(teams) == 1:
            return teams[0]
        print(f"you are on {len(teams)} teams — say which with --team:",
              file=sys.stderr)
        _print_teams(teams, stream=sys.stderr)
        raise SystemExit(1)

    exact = [t for t in teams if t.get("teamId") == spec]
    if exact:
        return exact[0]
    matches = [t for t in teams if spec.casefold() in (t.get("name") or "").casefold()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        print(f"no team of yours matches {spec!r}:", file=sys.stderr)
    else:
        print(f"{spec!r} matches {len(matches)} of your teams:", file=sys.stderr)
    _print_teams(matches or teams, stream=sys.stderr)
    raise SystemExit(1)


def _repo_label(repo: dict) -> str:
    return repo.get("name") or repo.get("repo") or repo.get("repoId", "")


def _resolve_repo(repos: list[dict], spec: str, where: str) -> dict:
    """One repo out of a list, by id, name or remote. Never a guess.

    `where` names the list that was searched, so "not found" distinguishes
    "you cannot see that repo" from "that team is not sharing it" -- two
    different problems with two different next commands.
    """
    exact = [r for r in repos if r.get("repoId") == spec]
    if exact:
        return exact[0]
    needle = spec.casefold()
    matches = [
        r for r in repos
        if needle in (_repo_label(r) or "").casefold()
        or needle in (r.get("remoteUrl") or "").casefold()
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        print(f"no repo matching {spec!r} {where}.", file=sys.stderr)
        if repos:
            print("  cci team repos   to see what there is", file=sys.stderr)
        raise SystemExit(1)
    print(f"{spec!r} matches {len(matches)} repos {where}:", file=sys.stderr)
    for r in matches:
        print(f"    {_repo_label(r):<28}  {r.get('remoteUrl') or ''}", file=sys.stderr)
    raise SystemExit(1)


def _resolve_member(members: list[dict], spec: str) -> dict:
    exact = [m for m in members if m.get("accountId") == spec
             or (m.get("actor") or "").casefold() == spec.casefold()]
    if len(exact) == 1:
        return exact[0]
    matches = [m for m in members
               if spec.casefold() in (m.get("actor") or "").casefold()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        print(f"nobody on this team matches {spec!r}.", file=sys.stderr)
    else:
        print(f"{spec!r} matches {len(matches)} people:", file=sys.stderr)
        for m in matches:
            print(f"    {m.get('actor', '')}", file=sys.stderr)
    raise SystemExit(1)


#: Suffixes for `--expires-in`. Minutes are absent deliberately: a code that
#: lives for minutes is a code the recipient will miss, and the failure is
#: silent -- they get the same 404 as an attacker and no way to tell.
_DURATIONS = {"h": 3_600_000, "d": 86_400_000, "w": 7 * 86_400_000}


def _duration_ms(spec: str) -> int:
    """`72h`, `3d`, `1w` -> milliseconds. Raises `ValueError` for anything else.

    A bare number is rejected rather than assumed. "7" could mean hours or
    days and the two differ by a factor of 24; guessing wrong in the long
    direction leaves a live credential in a chat log for a week.
    """
    spec = spec.strip().lower()
    if len(spec) < 2 or spec[-1] not in _DURATIONS or not spec[:-1].isdigit():
        raise ValueError(
            f"{spec!r} is not a duration. Use a number and a unit: 12h, 3d, 1w."
        )
    n = int(spec[:-1])
    if n < 1:
        raise ValueError("a duration must be at least 1.")
    return n * _DURATIONS[spec[-1]]


def _plural(n: int, one: str, many: str | None = None) -> str:
    """`1 person` / `2 people`. Small, and the reason is that it is read.

    This output is the first thing a person sees of the team feature, and
    "1 people" is the kind of seam that makes a tool feel unfinished — which
    matters here more than usual, because the thing it is asking them to
    trust is a credential.
    """
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def _relative(ms: int | None, *, now: int | None = None) -> str:
    """"in 2 days" / "3 hours ago" / "expired". Coarse on purpose.

    An exact timestamp is the wrong answer to "can I still send this to
    somebody": it makes the reader do arithmetic across a timezone to find
    out. The absolute time is available on the server for anyone who needs
    it; this is the form the decision is actually made in.
    """
    if not ms:
        return "—"
    delta = ms - (now if now is not None else _now_ms())
    past = delta < 0
    delta = abs(delta)
    # Floored, not rounded, and that is the safe direction for a credential:
    # a 72-hour code reads "in 2 days", so nobody promises a colleague more
    # life than it has. Rounding up would do the opposite.
    for unit, size in (("day", 86_400_000), ("hour", 3_600_000), ("minute", 60_000)):
        if delta >= size:
            n = delta // size
            plural = "" if n == 1 else "s"
            return f"{n} {unit}{plural} ago" if past else f"in {n} {unit}{plural}"
    return "just now" if past else "in under a minute"


def _returns_exit_code(fn):
    """Turn a refusal raised from deep in a helper into this command's exit code.

    The helpers above (`_resolve_team`, `_resolve_repo`, `_team_call`, and
    `_account_client` before them) refuse by printing a sentence and raising
    `SystemExit`, because they are several frames down from the command and
    have no `return` that reaches it.

    Elsewhere in this CLI that exception is allowed to propagate out of
    `main`, and a handful of tests depend on it doing so -- so this does NOT
    change `main`. It converts only inside the `cci team` verbs, which gives
    that surface one contract ("a command returns an int") without altering
    the behaviour of any command that already shipped.
    """
    def wrapper(args: argparse.Namespace) -> int:
        try:
            return fn(args)
        except SystemExit as exc:
            if exc.code is None:
                return 0
            return exc.code if isinstance(exc.code, int) else 1

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _team_client(args):
    """Config plus an authenticated client. Every `cci team` verb starts here."""
    cfg = config_mod.load(args.config_dir, create=False)
    client, _credential, url = _account_client(args, cfg)
    return cfg, client, url


def _team_call(fn, *a, **kw):
    """Run one request, or print the server's sentence and exit.

    Wrapped in one place because every verb below does the same thing with a
    failure, and `RemoteError.__str__` is already the whole message a user
    should see -- including, for a 401, the `cci login` that fixes it.
    """
    try:
        return fn(*a, **kw)
    except remote.RemoteError as exc:
        print(f"\n{exc}", file=sys.stderr)
        raise SystemExit(1)


def cmd_team_list(args: argparse.Namespace) -> int:
    """The teams you are on, and what you are on them as."""
    _cfg, client, url = _team_client(args)
    teams = _team_call(client.teams)
    if not teams:
        print("\nyou are not on a team.")
        print("  cci team new <name>     to start one")
        print("  cci team join <code>    if a colleague sent you a join code\n")
        return 0

    print(f"\n{url}")
    print(f"\nYOUR TEAMS  ({len(teams)})")
    width = max(len(t.get("name") or "") for t in teams)
    for t in teams:
        roster = _team_call(client.team_roster, t["teamId"])
        members = _team_call(client.team_members, t["teamId"])
        print(f"  {(t.get('name') or ''):<{width}}  {t.get('role', ''):<6} "
              f" {_plural(len(members), 'person', 'people'):>9}  "
              f"{_plural(len(roster), 'repo')} shared")
    print(f"\n  cci team members   who is on {'it' if len(teams) == 1 else 'one of them'}")
    print("  cci team invite    to let somebody else in\n")
    return 0


def cmd_team_new(args: argparse.Namespace) -> int:
    """Create a team. You are its first admin, and its only member."""
    _cfg, client, _url = _team_client(args)
    team = _team_call(client.create_team, args.name)
    print(f"\ncreated {team['name']}")
    print(f"  id       {team['teamId']}")
    print(f"  you      {team['role']}")
    print("\nIt shares nothing yet — a team is a list of people until a repo is on it.")
    print("  cci team share <repo>   to share one of the repos you can see")
    print("  cci team invite         to mint a join code for a colleague\n")
    return 0


def cmd_team_members(args: argparse.Namespace) -> int:
    """Who is on the team, what they can do, and who let them in."""
    _cfg, client, _url = _team_client(args)
    team = _resolve_team(client, args.team)
    members = _team_call(client.team_members, team["teamId"])

    print(f"\n{team['name']}  ·  {_plural(len(members), 'person', 'people')}")
    width = max(len(m.get("actor") or "") for m in members)
    for m in members:
        # Who invited them, because "how did this person get here" is the
        # question the audit trail exists to answer and the roster is where
        # somebody would think to ask it.
        via = (f"invited by {m['invitedByActor']}" if m.get("invitedByActor")
               else "created the team")
        print(f"  {(m.get('actor') or ''):<{width}}  {m.get('role', ''):<6} "
              f" joined {_relative(m.get('joinedAt')):<16} {via}")
    if team.get("role") == "admin":
        print("\n  cci team invite           to let somebody in")
        print("  cci team remove <who>     to take somebody out")
    print("  cci team leave            to take yourself out\n")
    return 0


def cmd_team_invite(args: argparse.Namespace) -> int:
    """Mint a join code and print it once, saying plainly that it is once.

    THE ONLY PLACE A LIVE JOIN CODE IS EVER PRINTED. It is stored hashed, so
    "copy it now" is a statement of fact rather than a nag -- there is no
    command that can show it again, and a person who closes the terminal has
    to mint another one.

    The code goes on its own line with nothing else on it, so that a
    double-click selects the whole of it and nothing else.
    """
    _cfg, client, url = _team_client(args)
    team = _resolve_team(client, args.team)

    ttl = None
    if args.expires_in:
        try:
            ttl = _duration_ms(args.expires_in)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1

    minted = _team_call(client.create_invite, team["teamId"], role=args.role,
                        expires_in_ms=ttl, max_uses=args.uses, note=args.note)

    seats = minted.get("maxUses", 1)
    link = join_link(url, minted["code"])
    print(f"\njoin link for {team['name']} — COPY IT NOW, IT IS NOT SHOWN AGAIN")
    print(f"\n    {link}\n")
    print(f"  joins as   {minted.get('role', 'member')}")
    print(f"  expires    {_relative(minted.get('expiresAt'))}")
    print(f"  good for   {_plural(seats, 'person', 'people')}")
    if minted.get("note"):
        print(f"  note       {minted['note']}")
    print(f"  id         {minted.get('inviteId', '')}")
    # Said out loud because the mistake this prevents is the expensive one:
    # the code is a bearer secret, and somebody who treats it as a team name
    # will paste it somewhere public.
    print("\nAnyone holding this can join the team and read what it shares. Send it")
    print("the way you would send a password. The link carries the server, so on")
    print("any machine they run")
    print("\n    cci team join <link>\n")
    print("or, with nothing installed yet,")
    print(f"\n    curl -fsSL {INSTALL_SCRIPT_URL} | bash -s -- --join <link>\n")
    print("Opened in a browser, the link shows the same instructions.")
    print("  cci team invites          to see it listed (without the code)")
    print(f"  cci team revoke {minted.get('inviteId', '')}   if it goes astray\n")
    return 0


def cmd_team_invites(args: argparse.Namespace) -> int:
    """Outstanding and spent codes. Never the codes themselves."""
    _cfg, client, _url = _team_client(args)
    team = _resolve_team(client, args.team)
    invites = _team_call(client.team_invites, team["teamId"])

    if not invites:
        print(f"\nno join codes for {team['name']}.")
        print("  cci team invite   to mint one\n")
        return 0

    live = [i for i in invites if i.get("active")]
    print(f"\n{team['name']}  ·  {len(live)} live of "
          f"{_plural(len(invites), 'code')}")
    for i in invites:
        if i.get("revokedAt"):
            state = "revoked"
        elif i.get("uses", 0) >= i.get("maxUses", 1):
            state = "used up"
        elif not i.get("active"):
            state = "expired"
        else:
            state = f"live, {i.get('maxUses', 1) - i.get('uses', 0)} left"
        print(f"\n  {i.get('inviteId', '')}  {state}")
        print(f"    minted by {i.get('createdByActor', '')} "
              f"{_relative(i.get('createdAt'))}"
              + (f" · {i['note']}" if i.get("note") else ""))
        print(f"    joins as {i.get('role', 'member')}, "
              f"expires {_relative(i.get('expiresAt'))}")
        for r in i.get("redemptions") or []:
            print(f"    redeemed by {r.get('actor', '')} "
                  f"{_relative(r.get('redeemedAt'))}")
    # The absence is worth stating, so nobody goes looking for a flag.
    print("\nThe codes are not shown: they are stored hashed and cannot be "
          "recovered.\n  cci team revoke <id>   to kill one\n")
    return 0


def cmd_team_revoke(args: argparse.Namespace) -> int:
    """Kill a join code. Idempotent, so it is safe to run when unsure."""
    _cfg, client, _url = _team_client(args)
    team = _resolve_team(client, args.team)
    _team_call(client.revoke_invite, team["teamId"], args.invite_id)
    print(f"\nrevoked {args.invite_id} on {team['name']}")
    print("  Anybody who has not already used it cannot now.")
    print("  cci team members   to check who did get in\n")
    return 0


#: Where the installer lives, for the line `cci team invite` prints.
INSTALL_SCRIPT_URL = "https://raw.githubusercontent.com/lunowe/cc-insights/master/scripts/install.sh"

#: The path segment between a server's address and a code in a join link.
#: The server answers a browser on it (cci_server/routes/join_page.py).
JOIN_PATH = "/join/"


def join_link(server_url: str, code: str) -> str:
    """`<server>/join/<code>`: a join code that also says which server it is for.

    A bare code was useless on a fresh machine -- nothing on it knew where to
    sign in -- and there is deliberately no default server. The link carries
    that, so one thing sent to a colleague is enough.
    """
    return server_url.rstrip("/") + JOIN_PATH + code


def parse_join(text: str) -> tuple[str | None, str]:
    """(server, code) from a join link, or (None, code) for a bare code."""
    text = text.strip()
    if text.startswith(("https://", "http://")) and JOIN_PATH in text:
        server, _, code = text.rpartition(JOIN_PATH)
        code = code.split("#", 1)[0].split("?", 1)[0].strip("/")
        return server.rstrip("/"), code
    return None, text


def _join_client(args):
    """The client to redeem with, signing this machine in first if the link
    names a server it is not yet signed in to.

    A machine holds one credential, so a link for a different server than the
    one it is signed in to is refused rather than silently switching -- that
    would move this machine's automatic pushes to another server.
    """
    server, code = parse_join(args.code)
    if server is None:
        _cfg, client, _url = _team_client(args)
        return client, code

    cfg = config_mod.load(args.config_dir)
    if args.server and args.server.rstrip("/") != server:
        print(f"the link is for {server}, but --server says {args.server}", file=sys.stderr)
        raise SystemExit(1)
    credential = account.load(cfg.config_dir)
    token = account.token_for(credential)
    current = credential.server_url.rstrip("/") if credential is not None else None
    if token and current is not None and current != server:
        print(f"this machine is signed in to {current}, and the link is for {server}.\n"
              "  cci logout     first, to switch this machine to the other server",
              file=sys.stderr)
        raise SystemExit(1)
    if not token:
        print(f"\nThe link is for {server}. Sign in to it first:")
        identity = _sign_in(cfg, server)
        if identity is None:
            raise SystemExit(1)
        print(f"signed in as {identity.actor} — this host {_claim_host(cfg, identity.account_id)}")
        token = identity.token
    return remote.Client(server, token), code


def cmd_team_join(args: argparse.Namespace) -> int:
    """Redeem a join code. This is the moment you agree to be on the team."""
    client, code = _join_client(args)
    joined = _team_call(client.join_team, code)

    if joined.get("alreadyMember"):
        print(f"\nyou were already on {joined.get('name', '')} — nothing changed.")
        print("  cci team members   to see who else is\n")
        return 0

    print(f"\njoined {joined.get('name', '')} as {joined.get('role', 'member')}")
    roster = _team_call(client.team_roster, joined["teamId"])
    if roster:
        print(f"\n  It shares {_plural(len(roster), 'repo')} with you:")
        for r in roster[:5]:
            print(f"    {_repo_label(r)}")
        if len(roster) > 5:
            print(f"    … and {len(roster) - 5} more")
    else:
        print("\n  It shares no repos yet, so you can see nothing new.")
    # Worth saying, because the natural assumption on joining a team is that
    # your own work is now on it. It is not: publishing is what shares your
    # rows, and a roster is what shares a repo.
    print("\nJoining did not share any of your own work. `cci publish` sends yours.")
    print("  cci team          what you can see now")
    print("  cci team leave    to undo this\n")
    return 0


def cmd_team_leave(args: argparse.Namespace) -> int:
    """Leave a team. The other half of consent."""
    _cfg, client, _url = _team_client(args)
    team = _resolve_team(client, args.team)

    if not args.yes:
        print(f"\nLeaving {team['name']}. You will lose sight of the repos it "
              "shares with you,")
        print("and its members lose whatever your rosters shared with them.")
        answer = input(f"Type the team name to confirm [{team['name']}]: ").strip()
        if answer != team["name"]:
            print("nothing changed.")
            return 1

    account_id = _team_call(client.whoami)["accountId"]
    _team_call(client.remove_member, team["teamId"], account_id)
    print(f"\nleft {team['name']}")
    print("  cci team list   the teams you are still on\n")
    return 0


def cmd_team_remove(args: argparse.Namespace) -> int:
    """Remove somebody from the team. Admin only."""
    _cfg, client, _url = _team_client(args)
    team = _resolve_team(client, args.team)
    members = _team_call(client.team_members, team["teamId"])
    target = _resolve_member(members, args.who)

    _team_call(client.remove_member, team["teamId"], target["accountId"])
    print(f"\nremoved {target.get('actor', '')} from {team['name']}")
    # Named because removal alone is not enough if they still hold a live
    # code: a spent single-use code cannot readmit them, but a multi-use one
    # can, and an admin doing this deliberately should be told.
    print("  cci team invites   check no live code lets them back in\n")
    return 0


def cmd_team_repos(args: argparse.Namespace) -> int:
    """Repos you can see, or -- with `--team` -- the ones one team shares.

    Two questions on one command because they are the same question with a
    different scope, and keeping them apart as two verbs would invite the
    belief that a team's roster is the whole of what you can see. It is not:
    forge access and your own publications reach repos no team shares.
    """
    _cfg, client, _url = _team_client(args)

    if args.team:
        team = _resolve_team(client, args.team)
        repos = _team_call(client.team_roster, team["teamId"])
        heading = f"{team['name']} SHARES"
        empty = ("nothing yet.\n  cci team share <repo>   "
                 "to put one of your repos on it")
    else:
        repos = _team_call(client.team_repos)
        heading = "REPOS YOU CAN SEE"
        empty = ("no repos in scope yet.\n  cci publish   "
                 "to share this machine's repos")

    if not repos:
        print(f"\n{empty}\n")
        return 0

    print(f"\n{heading}  ({len(repos)})")
    width = max(len(_repo_label(r)) for r in repos)
    for r in sorted(repos, key=lambda r: _repo_label(r).casefold()):
        # `via` says WHY it is visible, which is the question a person asks
        # when they are surprised by something in this list. Only the scope
        # listing carries it; a roster is a roster.
        via = ", ".join(r.get("via") or []) or ""
        bits = [f"via {via}"] if via else []
        if not r.get("branchNamesPublished", True):
            bits.append("branch names off")
        tail = ("  [" + " · ".join(bits) + "]") if bits else ""
        print(f"  {_repo_label(r):<{width}}  {r.get('remoteUrl') or ''}{tail}")

    if args.ids:
        print()
        for r in sorted(repos, key=lambda r: _repo_label(r).casefold()):
            print(f"  {r.get('repoId', '')}  {_repo_label(r)}")
    else:
        print("\n  --ids   to print the repo ids as well")
    print()
    return 0


def cmd_team_share(args: argparse.Namespace) -> int:
    """Put a repo on a team's roster.

    The server permits this only for a repo already in the caller's own
    scope, and what the team then gets is what the CALLER had -- every row
    if a forge identity was verified for the repo, their own rows otherwise.
    That is `docs/SERVER_API.md` §4.5 and it is not restated in the output
    as a rule, but the "shares" line below says which of the two happened,
    because an admin who thinks they shared a whole repo and shared one row
    has been misled by their own tool.
    """
    _cfg, client, _url = _team_client(args)
    team = _resolve_team(client, args.team)
    scope = _team_call(client.team_repos)
    repo = _resolve_repo(scope, args.repo, "in what you can see")

    _team_call(client.share_repo, team["teamId"], repo["repoId"],
               branch_names_published=not args.no_branches)

    via = repo.get("via") or []
    full = any(v == "github" for v in via)
    print(f"\nshared {_repo_label(repo)} with {team['name']}")
    print("  they see   " + ("every published row in this repo"
                             if full else "the rows you published, and no others"))
    if not full:
        # The under-sharing is deliberate and correctable, and saying so here
        # saves an admin concluding the feature is broken.
        print("             (you reach this repo as its publisher, not through")
        print("              GitHub, so that is all you had to share)")
    print("  branches   " + ("published" if not args.no_branches else "hidden"))
    print("\n  cci team repos --team   what the team shares now")
    print("  cci team unshare        to take it back off\n")
    return 0


def cmd_team_unshare(args: argparse.Namespace) -> int:
    """Take a repo off a team's roster."""
    _cfg, client, _url = _team_client(args)
    team = _resolve_team(client, args.team)
    roster = _team_call(client.team_roster, team["teamId"])
    if not roster:
        print(f"\n{team['name']} shares nothing.\n")
        return 0
    repo = _resolve_repo(roster, args.repo, f"on {team['name']}'s roster")

    _team_call(client.unshare_repo, team["teamId"], repo["repoId"])
    print(f"\n{team['name']} no longer sees {_repo_label(repo)}")
    # The honest limit, stated rather than left to be discovered. It is the
    # same point docs/REDACTION.md §5 makes about revocation.
    print("  Anything they already read, they have read. This stops the next look.")
    print("  cci team repos --team   what is left\n")
    return 0


def cmd_team_branches(args: argparse.Namespace) -> int:
    """Turn branch-name publication on or off for one repo, for one team.

    docs/ACCOUNTS.md §5 rule 2. The switch takes effect at READ time, which
    is the only way it can work -- its whole purpose is to be flipped after
    the rows were published.
    """
    _cfg, client, _url = _team_client(args)
    team = _resolve_team(client, args.team)
    roster = _team_call(client.team_roster, team["teamId"])
    if not roster:
        print(f"\n{team['name']} shares no repos, so there is no switch to flip.\n")
        return 0
    repo = _resolve_repo(roster, args.repo, f"on {team['name']}'s roster")
    on = args.state == "on"

    _team_call(client.set_branch_names, team["teamId"], repo["repoId"], on)
    print(f"\nbranch names {'shown to' if on else 'hidden from'} {team['name']} "
          f"for {_repo_label(repo)}")
    if on:
        print("  Branch names are free text. `feat/restricted-org-dbs` can say")
        print("  more than its author meant.")
    else:
        # Both halves matter: it is retroactive, and it is per team.
        print("  This applies to names already published, not just new ones.")
        print("  Another team with this repo on its roster is unaffected.")
    print("  cci team sessions   to see it from a reader's side\n")
    return 0


def cmd_team_actors(args: argparse.Namespace) -> int:
    """Per-person totals inside your scope.

    docs/ACCOUNTS.md §7 rules out ranking and "time saved" on purpose: this
    measures agent time, not work, and the answer to "who did the most" is
    wrong in a way that will be quoted anyway. So the rows are ordered by
    name, not by hours, and the caveat is printed rather than implied.
    """
    _cfg, client, _url = _team_client(args)
    body = _team_call(client.team_actors)
    actors = body.get("actors") or []
    if not actors:
        print("\nno published time in your scope.")
        print("  cci publish   to send yours\n")
        return 0

    print(f"\nPEOPLE IN SCOPE  ({len(actors)})")
    width = max(len(a.get("actor") or "") for a in actors)
    for a in sorted(actors, key=lambda a: (a.get("actor") or "").casefold()):
        print(f"  {(a.get('actor') or ''):<{width}} "
              f" {_hours((a.get('activeMs') or 0) / 3_600_000):>9} "
              f"  {_plural(a.get('sessions', 0), 'session'):>12}"
              f"  {_plural(a.get('repos', 0), 'repo'):>9}")
    _print_withheld_block(body)
    print("\n  This is agent time, not work done, and it is only the repos you")
    print("  can see. It does not rank anybody.\n")
    return 0


def cmd_team_daily(args: argparse.Namespace) -> int:
    """Active time per UTC day, inside your scope."""
    _cfg, client, _url = _team_client(args)
    body = _team_call(client.team_daily)
    days = body.get("days") or []
    if not days:
        print("\nno published time in your scope.\n")
        return 0

    peak = max((d.get("activeMs") or 0) for d in days) or 1
    print(f"\nDAILY  ({_plural(len(days), 'day')} with activity, UTC)")
    for d in days[-args.limit:]:
        ms = d.get("activeMs") or 0
        print(f"  {d.get('date', '')}  {_bar(ms / peak, 24)} "
              f"{_hours(ms / 3_600_000):>9}")
    # UTC, and said so: docs/SERVER_API.md §4.8 buckets by UTC deliberately
    # because a team spans timezones, and a renderer that does not label the
    # axis is showing somebody a day that is not theirs.
    print("\n  Days are UTC, not your local calendar — a team spans timezones.")
    _print_withheld_block(body)
    print()
    return 0


def _print_withheld_block(body: dict) -> None:
    """The hours that could not be published. Never silently omitted.

    docs/ACCOUNTS.md §5: a dashboard that quietly drops part of somebody's
    week is not private, it is wrong, and the reader cannot tell which.
    """
    withheld = body.get("withheld") or {}
    rows = withheld.get("byActor") or []
    if not rows:
        return
    span = "this range" if withheld.get("rangeFiltered") else "all time"
    print(f"\nWITHHELD  ({span}, work in no repo at all)")
    for row in rows:
        print(f"  {(row.get('actor') or ''):<16} "
              f"{_hours((row.get('withheldMs') or 0) / 3_600_000)}"
              f"  in {row.get('withheldProjects', 0)} projects")


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

    def team_sub(name, helptext, fn, *, team_flag=True):
        """One `cci team` verb, with the two flags every one of them takes.

        `--team` is added here rather than per-command so that no verb can
        be added without it. A command that silently acted on "the team" when
        the caller is on three is a command that changes who reads somebody's
        work without being asked which team.
        """
        sp = tsub.add_parser(name, help=helptext)
        if team_flag:
            sp.add_argument("--team", default=None, metavar="NAME|ID",
                            help="which team; optional when you are on one")
        sp.add_argument("--server", default=None, metavar="URL")
        # Wrapped here, so a verb cannot be registered without it and one
        # added next month exits with a code rather than a traceback.
        sp.set_defaults(fn=_returns_exit_code(fn))
        return sp

    team_sub("list", "the teams you are on", cmd_team_list, team_flag=False)

    tnew = team_sub("new", "create a team; you become its admin",
                    cmd_team_new, team_flag=False)
    tnew.add_argument("name", help="what to call it")

    team_sub("members", "who is on the team, and who let them in", cmd_team_members)

    tinv = team_sub("invite", "mint a join code — printed once, never again",
                    cmd_team_invite)
    tinv.add_argument("--role", choices=("member", "admin"), default="member",
                      help="what the redeemer becomes (default: member)")
    tinv.add_argument("--expires-in", default=None, metavar="12h|3d|1w",
                      help="how long it stays usable (default: 3d, max 30d)")
    tinv.add_argument("--uses", type=int, default=None, metavar="N",
                      help="how many people may redeem it (default: 1)")
    tinv.add_argument("--note", default=None, metavar="TEXT",
                      help="what it was for, shown in `cci team invites`")

    team_sub("invites", "join codes on this team, without the codes",
             cmd_team_invites)

    trev = team_sub("revoke", "kill a join code", cmd_team_revoke)
    trev.add_argument("invite_id", metavar="INVITE-ID")

    tjoin = team_sub("join", "redeem a join link a colleague sent you (signs in if needed)",
                     cmd_team_join, team_flag=False)
    tjoin.add_argument("code", metavar="LINK|CODE",
                       help="the join link from `cci team invite`, or a bare code")

    tleave = team_sub("leave", "leave a team", cmd_team_leave)
    tleave.add_argument("--yes", action="store_true", help="skip the confirmation")

    trm = team_sub("remove", "remove somebody from the team", cmd_team_remove)
    trm.add_argument("who", metavar="ACTOR|ACCOUNT-ID")

    trepos = team_sub("repos", "repos you can see, or what one team shares",
                      cmd_team_repos)
    trepos.add_argument("--ids", action="store_true",
                        help="print repo ids too, for copying into `share`")

    tshare = team_sub("share", "put a repo you can see on a team's roster",
                      cmd_team_share)
    tshare.add_argument("repo", metavar="NAME|ID")
    tshare.add_argument("--no-branches", action="store_true",
                        help="share it with branch names hidden")

    tunshare = team_sub("unshare", "take a repo off a team's roster",
                        cmd_team_unshare)
    tunshare.add_argument("repo", metavar="NAME|ID")

    tbranch = team_sub("branches", "show or hide branch names for one repo",
                       cmd_team_branches)
    tbranch.add_argument("state", choices=("on", "off"))
    tbranch.add_argument("repo", metavar="NAME|ID")

    tsess = tsub.add_parser("sessions", help="published sessions inside your scope")
    tsess.add_argument("--limit", type=int, default=50)
    tsess.add_argument("--server", default=None, metavar="URL")
    tsess.set_defaults(fn=cmd_team_sessions)

    team_sub("actors", "per-person totals inside your scope",
             cmd_team_actors, team_flag=False)
    tdaily = team_sub("daily", "active time per UTC day inside your scope",
                      cmd_team_daily, team_flag=False)
    tdaily.add_argument("--limit", type=int, default=30, metavar="DAYS")

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
