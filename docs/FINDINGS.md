# Findings — measured ground truth

Measured against the real logs on this machine (Darwin 27.0.0), regenerated
2026-09-19 by `docs/probes/canonical_metrics.py`. That script is the **spec for
the time math** — WP4 must reproduce it.

These numbers are ground truth for acceptance tests. If an implementation
disagrees with them, the implementation is wrong. Re-derive only by re-running
the canonical script.

## 0. Definition — what "active time" means

> **Active time** is the sum of active *span* durations, where a span is a
> maximal run of events whose consecutive gaps are ≤ `idle_threshold`.
> A gap **above** the threshold contributes **zero**.

The tempting alternative, `sum(min(gap, threshold))`, silently invents
`threshold` seconds of work for every idle gap. On this corpus that inflated
Claude Code's total from 65.8 h to 104.5 h — **+59%**. An early draft of this
document shipped that wrong number. Do not reintroduce it.

## 1. Corpus

| | Claude Code | Codex |
| --- | --- | --- |
| Log glob | `~/.claude/projects/*/*.jsonl` | `~/.codex/sessions/*/*/*/*.jsonl` (+ `archived_sessions`) |
| Files | 504 | 215 |
| Sessions | 121 | 188 |
| Events (deduped) | 52,919 | 54,296 |
| Coverage | 2026-06-24 → 2026-09-19 | 2026-02-08 → 2026-09-19 |
| **Active time** | **65.8 h** | **36.8 h** |
| Sum of wall-spans | 2710.7 h | 741.7 h |
| Active as % of wall-span | **2.4%** | 5.0% |
| Wall-clock with ≥1 active | 57.0 h | 35.1 h |
| Active spans | 584 | 284 |
| Parallelism multiplier | 1.15x | 1.05x |
| Peak concurrency | 5 @ 2026-09-09 12:14 | 4 @ 2026-08-27 13:30 |
| Time ≥2 concurrent | 6.9 h (12%) | 1.6 h (5%) |

Concurrency distribution, Claude Code (share of the 57.0 h):

| concurrent | 1 | 2 | 3 | 4 | 5 |
| --- | --- | --- | --- | --- | --- |
| hours | 50.1 | 5.3 | 1.3 | 0.3 | 0.0 |
| share | 88.0% | 9.2% | 2.3% | 0.5% | 0.0% |

**Wall-span is useless**: reporting it would overstate Claude Code usage by 41x.

> These figures are **session-level**. WP4 derives spans **per thread**, which
> will reveal extra intra-session concurrency in the 7 Codex sessions that have
> multiple threads. Expect Codex's multiplier to rise; Claude Code's is
> unaffected until derived subagent threads land.

## 2. Dedup is mandatory, and the key differs per source

`native_event_id` must be unique **within its session**. Getting this wrong
corrupts every downstream number.

**Claude Code — key is the event `uuid`.**
1,295 (session, uuid) pairs appear more than once; 1,294 of them are in
*different files* with **identical timestamp and type**. Session resume replays
prior history into the new file. Deduping on `uuid` collapses these correctly.
Ingest-assigned ordinals would **overcount by ~2.6%**.
`uuid` is absent on 3,502 events, all of type `queue-operation`, `pr-link` or
`file-history-delta` — fall back to a hash of the raw line.

**Codex — key is `"<thread_id>:<ordinal>"`, never `ordinal` alone.**
`ordinal` is *file-scoped* and restarts at 0 in every thread. Keying on
`(session_id, ordinal)` collides 8,709 times with **different timestamps** —
i.e. it would have silently **dropped 8,709 real events**.

## 3. Codex records subagent threads explicitly — Claude Code does not

The 215 Codex files are **215 distinct threads**, not 215 sessions. Each file's
`session_meta.payload` carries:

- `id` — this thread's own id (unique per file, 1:1 with the file)
- `session_id` — the **root** session id, shared by all its threads
- `parent_thread_id`, `forked_from_id`, `subagent_history_start_ordinal`
- `source = {"subagent": {"thread_spawn": {...}}}` on subagent threads

Totals: **188 root sessions, 215 threads, 27 marked subagent**, 7 roots have
more than one thread, max **9 threads under one root**. Threads under one root
hold *disjoint* events (verified: 0 content overlap between two threads of the
same session) — they are genuinely parallel work, not replay.

Claude Code has no equivalent: **`isSidechain` is `false` on all 54,227
events** despite **323 `Agent` tool calls**. Its subagent threads must be
*derived* by pairing `Agent` `tool_use` → `tool_result` on `tool_use_id`.

This is why the schema models **thread** as the universal unit: it is the only
way one timeline renders both sources. See `migrations/001_init.sql`.

## 4. Other gotchas

1. **`<synthetic>` appears as a model name** in Claude Code assistant events.
   Exclude it from model and cost breakdowns.
2. **Codex nests everything under `payload`**; Claude Code puts `cwd`,
   `gitBranch` and `version` at the top level of every event.
3. **Only Codex's `session_meta` line carries the session id.** Resolve it once
   per file and apply to every event in that file — a per-line fallback splits
   each file into a phantom extra session (this inflated an early count from
   188 to 403).
4. **Token usage** lives in `message.usage` per assistant event and is
   dominated by `cache_creation_input_tokens` / `cache_read_input_tokens`.
   Cost math must price those separately from fresh input.
5. **A session's events span multiple files** (resume, worktrees). Sessionize on
   the id *field*, never the filename.
6. **PostgreSQL `INTEGER` is int4** (max 2.1e9) while epoch-ms is ~1.79e12.
   Every timestamp column must be `BIGINT` on Postgres or it overflows on the
   first insert. `tests/test_db.py` guards the value range.
7. **Logs are a rolling window.** `~/.claude/.last-cleanup` is touched
   regularly, and Claude history reaches back only to June while Codex reaches
   February. Unrecorded history is lost permanently — this is why ingest is
   urgent and dashboards are not.

## 5. Scale

~107k events for the combined corpus (3 months of one tool, 7 of the other).
A full year of both stays well under 1M rows. **SQLite is not a performance
compromise.** Postgres is a sharing decision, not a scale one.
