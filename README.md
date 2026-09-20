# CC-Insights

**C**oding-agent **Insights** — a local-first tracker and visualizer for how,
when, and how long coding agents run on this machine.

It reconstructs an accurate timeline of agent activity from the logs the agents
already write, so you can answer: when do I actually use agents and for how
long, how often do I run them in parallel, which projects consume the time, and
how much of it is me driving versus agents running on their own.

## Status — Stage 1 complete

The pipeline works end to end: two source adapters, incremental ingest, span
derivation, and a CLI. 196 tests. Stage 2 (dashboard) is specified in
`PROMPT.md`.

```bash
python3 -m venv .venv && .venv/bin/pip install -e . pytest
.venv/bin/cci init
.venv/bin/cci ingest      # ~11s cold, ~0.04s warm
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
  stats.py      read-only summary queries
  cli.py        cci init | ingest | derive | stats | status | config
migrations/     numbered SQL, applied in order
docs/           FINDINGS.md (ground truth), ROADMAP.md, probes/
scripts/        launchd job + installer
```

## Roadmap

`docs/ROADMAP.md` — more adapters, real cost tracking, Postgres/multi-machine,
outcome correlation. "Time saved" is explicitly deferred and explains why.
