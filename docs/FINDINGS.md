# Findings — feasibility probe

Measured 2026-09-19 against the real logs on this machine (host: lunowe's Mac,
Darwin 27.0.0). These are **ground truth for the build** — implementers should
not re-derive them, but should reproduce them as an acceptance check.

Probe scripts: `docs/probes/`.

## 1. Corpus

| | Claude Code | Codex |
| --- | --- | --- |
| Log glob | `~/.claude/projects/*/*.jsonl` | `~/.codex/sessions/*/*/*/*.jsonl` |
| Files | 504 | 212 |
| On-disk size | 872 MB | — |
| Sessions (distinct `sessionId`) | 120 | 212 |
| Events with timestamps | 53,915 | — |
| Coverage | 2026-06-24 → 2026-09-19 | 2026-02-08 → 2026-09-19 |
| Active time | 65.6 h | 49.9 h |

Note the 504 files vs 120 sessions: a session's events are spread across
multiple files (resumes, worktrees). **Sessionize on the `sessionId` field, not
on filename.**

## 2. Wall-span is a useless metric

| | |
| --- | --- |
| Sum of session wall-spans | 2710.3 h |
| Sum of active time (300 s idle cap) | 104.1 h |
| Ratio | **4%** |

Sessions are left open for days. Reporting span would overstate usage ~25x.

## 3. Inter-event gap distribution (justifies the idle threshold)

```
p50 = 0.1s   p90 = 10.1s   p95 = 21.7s   p99 = 216s   p99.9 = 6478s   max = 1462982s
```

| threshold | gaps exceeding | share |
| --- | --- | --- |
| 60 s | 1076 | 2.00% |
| 120 s | 724 | 1.35% |
| **300 s** | **464** | **0.86%** |
| 900 s | 195 | 0.36% |
| 1800 s | 96 | 0.18% |

There is a wide flat valley between "agent is working" (p99 = 216 s) and "user
walked away". Any threshold in 120–900 s yields materially the same totals, so
the headline numbers are **not an artifact of the tuning knob**. Default: 300 s,
configurable, and the config value must be recorded alongside any export.

## 4. Concurrency (300 s idle cap, 582 derived active blocks)

| concurrent sessions | wall-clock | share |
| --- | --- | --- |
| 1 | 50.2 h | 88.2% |
| 2 | 5.1 h | 9.0% |
| 3 | 1.3 h | 2.3% |
| 4 | 0.3 h | 0.5% |
| 5 | 0.0 h | 0.0% |

- Total wall-clock with ≥1 agent active: **56.9 h**
- Sum of active session-hours: **65.6 h**
- Parallelism multiplier: **1.15x**
- Peak concurrency: **5 sessions**, 2026-09-09 12:14
- Time with ≥2 active: 6.7 h (12%)

## 5. Gotchas discovered

1. **`isSidechain` is always 0.** All 53,915 Claude Code events have
   `isSidechain: false` in this version, despite **323 `Agent` tool calls**.
   Subagent spans must be derived by pairing `Agent` `tool_use` → matching
   `tool_result` timestamps via `tool_use_id`. Do not rely on the flag.
2. **Logs are a rolling window, not an archive.** `~/.claude/.last-cleanup` was
   touched the day of the probe, and Claude history reaches back only to June
   while Codex reaches February. Ingest must run on a schedule or history is
   permanently lost.
3. **`<synthetic>` appears as a model name** in Claude Code assistant events.
   Filter it out of model/cost breakdowns.
4. **Codex nests its real metadata** under `payload` (`session_meta` line
   carries `cwd`, `originator`, `cli_version`, `model_provider`); Claude Code
   puts `cwd`/`gitBranch`/`version` at the top level of every event. The
   adapters normalize these differences away.
5. **Token usage is per-assistant-event** in `message.usage`, and includes
   `cache_creation_input_tokens` / `cache_read_input_tokens` which dominate the
   raw input count. Cost math must treat them at their own rates.

## 6. Scale implication

53,915 events ≈ 3 months of one tool. A full year of both is well under 1M rows.
**SQLite is not a performance compromise here.** Postgres is a
sharing/multi-machine decision, not a scale one — do not over-engineer for it.
