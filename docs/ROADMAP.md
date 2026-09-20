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

## v2 — Multi-machine, then teams

The schema already carries `host_id` on every row and every id is a
content hash, so the same logical row computes the same id on any machine.
That was deliberate: this is a backend swap plus a sync path, not a migration.

### Windows support

Needed before "multi-machine" means anything, since the second box is often
not a Mac.

- Log locations differ (`%APPDATA%`, `%USERPROFILE%`); the source globs are
  already config, so this is adapter-local.
- No launchd. Task Scheduler, or a long-running `cci watch`.
- Path handling must stop assuming POSIX separators — `grouping.py`'s
  worktree shapes and ancestor logic are the exposed spots.

### Postgres + sync

Same migrations, same portable SQL. Each machine ingests locally and pushes
normalized rows; local stays the source of truth and sync is append-only.
Because ids are content hashes, a row pushed twice from two machines collapses
rather than duplicating.

### Accounts and teams

The goal: several people, each with several machines, sharing insight across a
team's projects.

- **GitHub OAuth is the right front door**, and not only for convenience:
  groups already carry `forge`/`owner`/`repo` from the git remote, so a
  team's scope can be *derived* from repo access rather than hand-maintained.
  If you can see the repo, you can see the agent time spent on it.
- `host_id` becomes a child of an account; an account belongs to teams.
- Needs a tenant column and row-level scoping on every query.

**The unsolved problem is not auth, it is paths.** `cwd` is the join key for
grouping, and it leaks local detail: usernames, client names, unreleased
project names. `~/Coding/atlas-chat/.claude/worktrees/tenant-restricted`
tells a colleague more than its owner may intend. Sharing beyond one person
needs a redaction layer — publish the group, withhold the path — and that has
to be designed before any data leaves a laptop, not bolted on after.
Metadata-only already rules out the worst of it: no prompt or response text
has ever been stored, which is the only reason this version is viable at all.

## v3 — Outcomes and agentic access

### Outcome correlation

Correlate agent time with what came out of it. The groundwork is in place:
every group carries `forge`/`owner`/`repo` and a local `git_common_dir`, so a
repo identity and a checkout on disk are already known.

- Commits, diff size, files touched per span (join on the repo and the time
  window).
- Review cycles, test runs, revert rate.
- Trends in human-initiated vs autonomous time — the clearest available signal
  for "am I delegating more over time".

### Agentic access to the data

Let an agent answer questions and build views that were never coded.

- **An MCP server over the existing metrics layer.** `metrics.py` is already a
  filter-aware, one-function-per-question API with no rendering in it; exposing
  it as tools is a thin wrapper, not a rewrite. "How much agent time did team X
  spend on project Y last quarter" becomes a tool call.
- **Charts on the fly.** Needs a small declarative chart spec the agent emits
  and the frontend renders, rather than the agent writing components. The
  discipline that makes this safe already exists: the three summary buckets
  partition exactly, durations are ms, active time can exceed elapsed time.
  Those invariants have to be enforced by the renderer, or a generated chart
  will eventually assert something false and look authoritative doing it.
- Natural pairing with teams: the interesting questions are cross-person, and
  they are exactly the ones nobody will build a fixed dashboard for.

## Explicitly deferred

- **"Time saved."** The logs measure agent time, not the human counterfactual.
  Any figure would be agent-hours x an invented multiplier, and it is precisely
  the number most likely to be quoted at work — which is why it must not be
  presented as measured. If it ships at all, it ships as: leading indicators
  (diff size, commits, tool-call volume) plus a multiplier the user sets
  themselves, displayed as an assumption, not a measurement.
- **Keystroke / screen-level tracking.** Out of scope. Log-derived only.
- **Storing prompt or response text.** Never. See PROMPT § Non-negotiables.
