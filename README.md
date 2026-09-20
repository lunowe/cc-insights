# CC-Insights

**C**oding-agent **Insights** — a local-first tracker and visualizer for how,
when, and how long coding agents run on this machine.

It reconstructs an accurate timeline of agent activity from the logs the agents
already write, so you can answer: when do I actually use agents and for how
long, how often do I run them in parallel, which projects consume the time, and
how much of it is me driving versus agents running on their own.

## Status — Stage 2 complete, v1 and v2 under way

The pipeline works end to end: three source adapters, incremental ingest, span
derivation, cost, a dashboard and a CLI. It runs on Windows, syncs between your
machines through PostgreSQL, and knows what it may and may not share with
anyone else. 935 tests.

## Install

One command, and then it looks after itself:

```bash
pip install -e .      # from a checkout; see Releasing for the wheel
cci install
```

`cci install` does the whole first run — config, database, a first ingest, and
a background job — because every one of those used to be a separate thing to
remember and forgetting the last one is unrecoverable: **agent log directories
are pruned on a rolling basis, so a gap in capture is a permanent gap in
history.** That is the point of the project, not a footnote.

It installs a launchd job on macOS and a Task Scheduler job on Windows; on
anything else it prints the equivalent cron line rather than failing. To
follow the logs live instead of every 15 minutes, `cci install --watch` — one
or the other, never both, since two writers on one SQLite database is the one
way to make this contend with itself. The installer enforces that rather than
documenting it. `cci install --uninstall` removes either, and keeps your data.

To check it is still working, at any point:

```bash
cci doctor
```

It reports the job, how long since the last ingest, the schema, and what to
run for anything that is not right — and exits non-zero on a real failure, so
it works from a script too. It measures freshness from when the tool last
*looked*, not from the newest event: no events for three days is a quiet week,
no ingest for three days is a broken install, and only the second is urgent.

Config and database live in `~/.config/cc-insights`, or
`%APPDATA%\cc-insights` on Windows. `CC_INSIGHTS_HOME` overrides both.

## Looking at it

```bash
cci stats     # the summary, in the terminal
cci serve     # the dashboard
```

`cci serve` opens the dashboard: filter by project, source, thread role and
date range, with the filter state in the URL so a view is shareable. The page
reads the live database through the API it is served from; if it is opened
from somewhere with no API behind it, it falls back to the bundled sample and
says so on the badge rather than passing the sample off as your data.

To watch it happen instead, `cci watch` follows the logs and keeps the database
current as the agents write it, ~0.15 s per cycle because ingest resumes at
each file's byte offset and only the sessions that moved are re-derived:

```bash
cci watch                 # follow the logs, print a line per change
cci watch --serve         # ...and a dashboard that refreshes itself
cci install --watch       # ...or leave it running in the background
```

Install one background job or the other, not both: two writers on one database
is the one way to make this contend with itself. `cci install` unloads the
other before it loads either, and `--uninstall` removes both.

### Several machines

`cci sync` shares one person's machines through a PostgreSQL database. Each
machine ingests its own logs locally, pushes the rows it owns, and pulls the
others' back down — local SQLite stays the source of truth and the dashboard
never changes.

```bash
pip install -e '.[postgres]'
export CC_INSIGHTS_SYNC_URL=postgresql://user@host/cci   # or sync_url in config.toml
cci sync push      # send this machine's rows
cci sync pull      # bring the other machines' rows down
cci sync status    # who has pushed what
```

Re-running either is safe: every id is a content hash, so a row that crosses
twice collapses instead of duplicating. `ingest_file` is deliberately never
sent — it is bookkeeping about local paths with no analytical value.

### Sharing with other people

`sync` moves paths, because it is all your own disk. Sharing beyond one person
goes through a different pipe, and a different set of rules:

```bash
cci privacy        # what would and would not cross a team boundary. Sends nothing.
```

`docs/REDACTION.md` has the design. The short version: publishing `project_id`
publishes `root_path`, because the id *is* `sha256(root_path)` and a colleague
can hash a guess — 555 guesses recovered 20% of this corpus. So published rows
are re-keyed on the repo's remote, the boundary is repo access, and work with
no remote stays local, counted rather than silently dropped. The projection
runs on your laptop; the shared database never receives a path.

Teams and auth follow. The privacy plumbing is in place for them.
## What it costs

```bash
.venv/bin/cci price sync   # load rates for the models you actually ran
.venv/bin/cci cost         # the breakdown
```

**$11,630 at published API rates** on this corpus, and the shape of it is the
finding: **cache reads are 44%** of the total, cache writes 38% (21% of it at
the one-hour rate), output 16%, fresh input 2%. The cheapest component per
token is most of the bill.

That figure is a *list-price equivalent*, not a bill — a subscription charges a
flat monthly fee no matter how many tokens run through it. Rates come from a
committed snapshot of [pydantic/genai-prices](https://github.com/pydantic/genai-prices),
keyed by model **and date**, so a vendor's next price change does not rewrite
last month. Anything it cannot price is reported rather than counted as zero
(138 M tokens here).

Cache writes are priced at two rates, because they have two lifetimes: 1.25x
base input for a five-minute write and 2x for a one-hour one. 41% of this
corpus takes the expensive one — Claude Code gives the main conversation an
hour on a subscription within plan usage, and five minutes to subagents — and
the logs record which, so `cci cost` does not have to guess. A database built
before that column existed fills it with `cci backfill`, from the logs that
are still on disk.

Three layers, most specific first: the catalog, then
`src/cc_insights/price_overrides.json` — corrections checked against the
vendor's own pricing page and shipped with the code — then `cci price set`,
which a human owns and no sync touches. The middle layer exists because the
catalog's errors are not small and a fix kept in one laptop's database is
lost on the next machine: it priced **Claude Fable 5.1 as Fable 5**, whose
cache reads cost four times as much ($1.00 against $0.25 per MTok), which was
**$2,752 — 21% of the total** — and it carried a Sonnet 5 price rise that
never happened.

## What it found on this machine

185 hours of active agent time across 223 days, from 306 sessions and 714
threads:

Projects are the logical unit: a repo's worktrees and subdirectories fold into
one. `atlas-chat` reads **111.6 h across 13 paths**, seven of which no longer
exist on disk — flat, it looked like 54.5 h.

| | Claude Code | Codex |
| --- | --- | --- |
| Active time | 143.8 h | 41.5 h |
| Threads | 499 | 215 |
| Wall-clock with ≥1 active | 83.6 h | 36.1 h |
| **Parallelism multiplier** | **1.72x** | 1.15x |
| Peak concurrent threads | **7** | 4 |
| Time with ≥2 running | 34.1 h (41%) | 3.3 h (9%) |

Split by who started the work:

| | hours | |
| --- | --- | --- |
| human-initiated | 98.4 h | 53% — a person typed the turn that started it |
| autonomous | 81.4 h | 44% — a model spawned it (subagent threads) |
| unattended root | 5.5 h | 3% — agent ran on in the main thread |

## Three things that make the numbers trustworthy

**Wall-span is not usage.** Summed session spans come to 2,710 h against 143 h
of real activity — 2.4%. Sessions sit open for days. Active time is the sum of
*span* durations, where a span breaks on any gap over the idle threshold, and a
gap above the threshold contributes **zero** rather than a capped value.

**The idle threshold is not a tuning knob.** The measured gap distribution has a
wide flat valley between "agent is working" (p99 = 216 s) and "user left". Any
threshold between 120 s and 900 s gives materially the same totals.

**Every number is reproduced from one reference implementation.**
`docs/probes/canonical_metrics.py` is the spec; the derivation engine is tested
for exact agreement with it, compared in a single process rather than against a
frozen constant — the corpus grows while you measure it. That test exists
because three published "measured" numbers turned out to be bugs, each caught by
an implementer who refused an acceptance number they could not reproduce. See
`docs/FINDINGS.md` §5.

## Design stance

- **Metadata only.** Prompt and response text is *never stored* — only
  timestamps, ids, cwd, branch, model, token counts, tool names. `RawEvent` is a
  frozen slots dataclass so an adapter structurally cannot smuggle content
  through it. This is what makes the future multi-machine mode safe.
- **Portable by design.** SQLite now; `host_id` on every row and epoch-ms
  timestamps throughout, so Postgres is a backend swap, not a migration.
- **Ingest before dashboards.** Logs are a rolling window. Capturing history is
  urgent; visualizing it is not.

## Layout

```
src/cc_insights/
  sources/      adapters: claude_code.py, codex.py, opencode.py (contract in base.py)
  ingest.py     adapters -> DB, incremental and idempotent
  derive.py     active spans, attendance, concurrency
  pricing.py    the dated rate table, from a committed price catalog
  cost.py       event -> money, and what it could not price
  watch.py      follow the logs; the tick behind a live dashboard
  metrics.py    filter-aware queries, one per API endpoint
  serve.py      the read-only localhost server
  stats.py      read-only summary queries
  cli.py        cci install | doctor | ingest | derive | cost | price |
                watch | serve | sync | privacy | ...
  scheduler.py  installing the background job, from the installed package
  doctor.py     is this installation actually capturing anything
  paths.py      path reasoning that takes the OS from the path, not the host
  sync.py       push/pull between local SQLite and a shared PostgreSQL
  redact.py     what may cross a team boundary, and in what shape
  assets.py     where the shipped dashboard, migrations and job templates are
  jobs/         launchd + Task Scheduler templates, package data so `cci`
                can install a job on a machine with no checkout
  model_prices.json   the price catalog snapshot (scripts/sync_prices.py)
frontend/       Vite + React dashboard, built into frontend/dist -- the wheel
                force-includes it as cc_insights/web
migrations/     numbered SQL, applied in order; also force-included
docs/           FINDINGS.md (ground truth), API.md (frozen contract),
                REDACTION.md (what may be shared), ROADMAP.md, probes/
scripts/        launchd jobs (interval and watch) + installer (macOS),
                Task Scheduler job (Windows), fixture and price sync
```

## Roadmap

`docs/ROADMAP.md`. v1 has landed the opencode adapter, real cost tracking and
watch mode; session annotation (tagging a session client/ticket/billable after
the fact) is what remains. v2 is done bar teams: Windows, a per-machine probe
cache, PostgreSQL sync, and the redaction layer that had to be designed before
any data left a laptop. Teams need auth and scoping, not privacy plumbing.
Then outcome correlation. "Time saved" is explicitly deferred, and the roadmap
explains why.
