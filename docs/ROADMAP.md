# Roadmap

## v0 — MVP (specified in `PROMPT.md`)

Local SQLite, Claude Code + Codex adapters, incremental scheduled ingest,
derivation engine, static HTML dashboard, CSV export.

## v1 — Breadth

- **More adapters.** Cursor, Gemini CLI, Aider, GitHub Copilot CLI, opencode.
  The adapter protocol exists for this; each is one file.
- **Real cost tracking.** Token counts are already ingested. Add a pricing table
  keyed by model + date so historical rates stay correct as prices change.
  Cache-read vs cache-write vs input must be priced separately (FINDINGS §5.5).
- **Watch mode.** Replace the 15-minute launchd poll with FSEvents for a live
  dashboard.
- **Session annotation.** Let a session be tagged (client, ticket, billable)
  after the fact — the missing piece for real invoicing.

## v2 — Multi-machine / shared

The schema already carries `host_id` on every row, so this is a backend swap
plus a sync path, not a migration.

- **Postgres backend.** Same migrations, same portable SQL (see PROMPT § Schema
  rules). Swap the connection layer.
- **Push sync.** Each machine ingests locally and pushes normalized rows to a
  shared instance. Local stays the source of truth; sync is append-only.
- **Multi-user / team mode.** Once hosts aggregate, per-person and per-team
  views follow. Requires auth, a tenant column, and a privacy decision about
  what colleagues can see of each other.
- **Plug-in deployment.** A hosted instance others point their machines at.
  *The metadata-only rule is what makes this viable* — if content had ever been
  stored, this version could not ship.

## v3 — Outcomes

Correlate agent time with what came out of it:

- Commits, diff size, files touched per session (join on `cwd` + time window
  against git history).
- Review-cycle counts, test runs, revert rate.
- Trends in the attended vs. unattended ratio — the clearest available signal
  for "am I actually delegating more over time".
- Flow analysis: session length distribution vs. time of day, interruption
  frequency.

## Explicitly deferred

- **"Time saved."** The logs measure agent time, not the human counterfactual.
  Any figure would be agent-hours x an invented multiplier, and it is precisely
  the number most likely to be quoted at work — which is why it must not be
  presented as measured. If it ships at all, it ships as: leading indicators
  (diff size, commits, tool-call volume) plus a multiplier the user sets
  themselves, displayed as an assumption, not a measurement.
- **Keystroke / screen-level tracking.** Out of scope. Log-derived only.
- **Storing prompt or response text.** Never. See PROMPT § Non-negotiables.
