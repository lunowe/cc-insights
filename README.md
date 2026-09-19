# CC-Insights

**C**oding-agent **Insights** — a local-first tracker and visualizer for how, when, and
how long coding agents run on this machine.

It reconstructs an accurate timeline of agent activity from the logs the agents
already write, so you can answer:

- When do I actually use agents, and for how long?
- How often do I run them in parallel, and how many at once?
- Which projects consume the time?
- How much of that time am I supervising vs. the agent working unattended?

Useful for curiosity, for work time-tracking, and as the evidence base for any
productivity claim you want to make — without inventing numbers.

## Status

Pre-implementation. The premise has been validated against real logs on this
machine; see `docs/FINDINGS.md`. The build is specified in `PROMPT.md`.

## Why this is not just "sum the session durations"

Measured on this machine's Claude Code logs (2026-06-24 → 2026-09-19):

| metric | value |
| --- | --- |
| Sum of session wall-clock spans | 2710.3 h |
| Actual active agent time | **104.1 h** (4% of span) |

Sessions stay open for days. Any tool that reports wall-span is off by 25x.
CC-Insights reconstructs *active* time from inter-event gaps instead. See
`docs/FINDINGS.md` for the gap distribution that justifies the threshold.

## Supported sources (MVP)

| source | log location | verified history |
| --- | --- | --- |
| Claude Code | `~/.claude/projects/*/*.jsonl` | 120 sessions, 65.6 active h |
| Codex | `~/.codex/sessions/YYYY/MM/DD/*.jsonl` | 212 sessions, 49.9 active h |

## Design stance

- **Metadata only.** Prompt and response *text is never stored.* Timestamps,
  session ids, cwd, branch, model, token counts and tool names only. This keeps
  the database small, keeps secrets out of it, and makes the future cloud/
  multi-machine mode safe by construction.
- **Local-first, portable-by-design.** SQLite now; the schema is written so it
  moves to Postgres without a rewrite (see `PROMPT.md` § Schema).
- **Ingest before dashboards.** The agent log directories are a rolling window
  that gets pruned. Capturing history is urgent; visualizing it is not.

## Roadmap

See `docs/ROADMAP.md`.
