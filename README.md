# CC-Insights

**C**oding-agent **Insights** — a local-first tracker and visualizer for how,
when, and how long coding agents run on this machine.

It reconstructs an accurate timeline of agent activity from the logs the agents
already write, so you can answer: when do I actually use agents and for how
long, how often do I run them in parallel, which projects consume the time, and
how much of it is me driving versus agents running on their own.

## Status — Stage 2 complete, v1 under way

The pipeline works end to end: three source adapters, incremental ingest, span
derivation, cost, a dashboard and a CLI. 511 tests.

```bash
python3 -m venv .venv && .venv/bin/pip install -e . pytest
.venv/bin/cci init
.venv/bin/cci ingest      # ~11s cold, ~0.4s warm
.venv/bin/cci derive
.venv/bin/cci stats
```

`cci serve` opens the dashboard: filter by project, source, thread role and
date range, with the filter state in the URL so a view is shareable. The page
reads the live database through the API it is served from; if it is opened
from somewhere with no API behind it, it falls back to the bundled sample and
says so on the badge rather than passing the sample off as your data.

To keep it current automatically — **this is the point of the project**, since
agent log directories are pruned on a rolling basis and uncaptured history is
lost for good:

```bash
./scripts/install-launchd.sh              # every 15 min; --uninstall to remove
```

To watch it happen instead, `cci watch` follows the logs and keeps the database
current as the agents write it, ~0.15 s per cycle because ingest resumes at
each file's byte offset and only the sessions that moved are re-derived:

```bash
.venv/bin/cci watch                  # follow the logs, print a line per change
.venv/bin/cci watch --serve          # ...and a dashboard that refreshes itself
./scripts/install-launchd.sh --watch # ...or leave it running in the background
```

Install one background job or the other, not both: two writers on one database
is the one way to make this contend with itself. The installer unloads the
other before it loads either, and `--uninstall` removes both.

## What it costs

```bash
.venv/bin/cci price sync   # load rates for the models you actually ran
.venv/bin/cci cost         # the breakdown
```

**$13,272 at published API rates** on this corpus, and the shape of it is the
finding: **cache reads are 58%** of the total, cache writes 26%, output 14%,
fresh input 2%. The cheapest component per token is most of the bill.

That figure is a *list-price equivalent*, not a bill — a subscription charges a
flat monthly fee no matter how many tokens run through it. Rates come from a
committed snapshot of [pydantic/genai-prices](https://github.com/pydantic/genai-prices),
keyed by model **and date**, so a vendor's next price change does not rewrite
last month. Anything it cannot price is reported rather than counted as zero
(138 M tokens here), and a model priced as a near relative is named, because
`claude-fable-5-1` priced as `claude-fable-5` is thousands of dollars of
difference that must not be invisible. `cci price set` overrides any of it.

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
  cli.py        cci init | ingest | derive | cost | price | watch | serve | ...
  model_prices.json   the price catalog snapshot (scripts/sync_prices.py)
frontend/       Vite + React dashboard, built into frontend/dist
migrations/     numbered SQL, applied in order
docs/           FINDINGS.md (ground truth), API.md (frozen contract), ROADMAP.md
scripts/        launchd jobs (interval and watch) + installer,
                fixture and price sync
```

## Roadmap

`docs/ROADMAP.md`. v1 has landed its first three: the opencode adapter, real
cost tracking, and watch mode. Next in v1 is session annotation (tagging a
session client/ticket/billable after the fact). Then Postgres and
multi-machine, then outcome correlation. "Time saved" is explicitly deferred,
and the roadmap explains why.
