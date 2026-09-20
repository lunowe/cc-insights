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

### Windows support — done

Needed before "multi-machine" means anything, since the second box is often
not a Mac.

- **Path handling no longer assumes POSIX separators.** `src/cc_insights/paths.py`
  takes the flavor from the path string, not from `os.name`, and `grouping.py`,
  `ingest.py` and `cli.py` read paths only through it.
- Config dir is `%APPDATA%\cc-insights` on Windows; `CC_INSIGHTS_HOME` still
  overrides. Source globs expand `%APPDATA%`-style variables and ship extra
  Windows candidates alongside the `~`-relative ones.
- `scripts/install-task.ps1` registers the Task Scheduler job: same cadence,
  same commands, same log files as the launchd one. A long-running `cci watch`
  is still the nicer answer and belongs with v1's watch mode.
- Fixed on the way: a Windows `db_path` written into `config.toml` unescaped is
  not valid TOML (`\U`, `\A` are escape sequences), so the file the tool had
  just written was unreadable on the next run — and an unreadable `host_id` is
  a regenerated `host_id`, which forks the whole history.

**What this turned out to be about.** The framing above said "run on Windows".
The real requirement is that *any* machine can reason about *any* other
machine's paths, because after sync the box rendering the dashboard is usually
not the box the path came from. On a Mac, `os.path.basename` of a Windows path
returns the whole string, `os.path.isdir` calls a live remote worktree dead,
and `$HOME` cannot be looked up for a host you are not on. So the flavor lives
in the path, comparison is flavor-aware (Windows folds case, POSIX does not),
and stored paths are never rewritten — `project_id = hash(root_path)`, so
normalizing in place would fork the history rather than fix it. See
docs/GROUPING.md § "Paths belong to a machine, not to this one".

### Postgres + sync

Same migrations, same portable SQL. Each machine ingests locally and pushes
normalized rows; local stays the source of truth and sync is append-only.
Because ids are content hashes, a row pushed twice from two machines collapses
rather than duplicating.

Two prerequisites are done, both of them things that had to be settled before
the first sync writes rather than after:

- **Paths are flavor-aware**, so a row pulled from another host groups
  correctly instead of poisoning the ladder.
- **The probe cache is per-machine** (migration 004). It used to live on
  `project`, but `project_id = hash(root_path)`, so a laptop and a desktop
  that both keep work at `/Users/you/Coding/X` are one project row describing
  two different disks — whichever machine ran `cci group auto` last overwrote
  the other. It now lives in `project_probe (project_id, host_id)`, and the
  readers are explicit: the ladder prefers the local machine's answer and
  falls back to the freshest other one, while "is this path gone" is answered
  across all of them, because live on any machine means not gone.

**Done.** `cci sync push` / `pull` / `status`, over a shared PostgreSQL.

The shape follows from what was already true. Every id is a content hash, so
the transfer is an upsert with no coordination and no merge — a row pushed
twice collapses. Local stays the source of truth: Postgres is a meeting point,
each machine pushes the rows it owns and pulls everyone else's back into its
own SQLite file, and the dashboard, `metrics`, `stats` and `derive` keep
reading SQLite without knowing any of it happened. That is why the read path
needed no porting at all.

- The migrations stayed one source of truth. The only thing the `.sql` files
  cannot express is integer width — SQLite's INTEGER is 64-bit, PostgreSQL's
  is int4 (max 2.1e9), and epoch-ms is ~1.79e12 — so `db.translate_ddl`
  widens every INTEGER to BIGINT on the way out. No second schema to drift.
- **Conflicts have explicit rules, not last-writer-wins.** A pin is a human
  saying where a project belongs, so a push from a machine that never heard
  about it must not unpin it. `host.first_seen` only ever moves backwards.
- **`ingest_file` is never synced**: bookkeeping about how far this machine
  read each local log, whose only content is a full local path. No analytical
  value, pure leakage.
- psycopg is an optional extra (`pip install 'cc-insights[postgres]'`). The
  base install stays dependency-free.

Still one person's several machines. Sharing beyond that is the next section,
and it is blocked on redaction, not on transport.

### Accounts and teams

The goal: several people, each with several machines, sharing insight across a
team's projects.

- **GitHub OAuth is the right front door**, and not only for convenience:
  groups already carry `forge`/`owner`/`repo` from the git remote, so a
  team's scope can be *derived* from repo access rather than hand-maintained.
  If you can see the repo, you can see the agent time spent on it.
- `host_id` becomes a child of an account; an account belongs to teams.
- Needs a tenant column and row-level scoping on every query.

**Redaction is designed and implemented** — `docs/REDACTION.md`,
`src/cc_insights/redact.py`, `cci privacy`. The finding that shaped it:
publishing `project_id` publishes `root_path`, because `project_id` IS
`sha256(root_path)` and a colleague can hash a guess. 555 guesses built from a
username, eight conventional directory names and the repo names in the remotes
recovered 20% of this corpus outright. So hashing is not redaction, salting
would break the cross-machine identity the schema depends on, and published
rows are re-keyed on the normalized git remote instead.

The boundary is repo access, which is the same answer this section already
reached from the auth direction: a row may be published only if it belongs to
a repo, and only to people who can already see that repo. Work with no remote
has nothing to derive permission from and stays local — 8% of active time
here, withheld *and counted*, because a view that quietly omits your hours is
not private, it is wrong. The projection runs on the laptop; the shared
database never receives a path.

What remains for teams is auth and scoping, not privacy plumbing: `actor` is
already a parameter, and `redact.FIELDS` classifies all 110 schema columns
closed-by-default with a test that fails when a migration adds one nobody has
ruled on.

**The problem this section used to open with, kept for the record.** `cwd` is the join key for
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
