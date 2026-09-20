# Roadmap

## v0 — MVP (specified in `PROMPT.md`)

Local SQLite, Claude Code + Codex adapters, incremental scheduled ingest,
derivation engine, static HTML dashboard, CSV export.

## v1 — Breadth

- **More adapters.** ✅ **opencode** (`sources/opencode.py`) — and it cost the
  adapter contract an assumption: its store is one SQLite database, not log
  files, so there is no byte offset to resume from and `byte_end = 0` means
  "re-read me every run". Still to come: Cursor, Gemini CLI, Aider, GitHub
  Copilot CLI. Each is one file.
- **Real cost tracking.** ✅ `pricing.py` + `cost.py` + migration 003. Rates are
  keyed (model, effective_from) from a committed snapshot of
  pydantic/genai-prices, so history does not move when a vendor reprices.
  Input, output, cache read and cache write are priced separately, which
  turned out to matter more than expected: cache reads are 58% of the total.
  What is left, and it is not small:
  - **Context-window tiers are flattened to the base rate.** Some models charge
    more above a threshold; the logs record tokens per request, not context
    length, so the tier cannot be chosen honestly. This under-states long-context
    traffic on tiered models.
  - **The catalog prices some models as a near relative** (`claude-fable-5-1`
    as `claude-fable-5`, whose cache reads cost 4x). ✅ Resolved with a third
    layer: `price_overrides.json`, checked against the vendor's own pricing
    page and shipped with the code, beating the catalog and losing to
    `cci price set`. It corrected two models and moved the corpus total by
    $2,744. Anything still priced as a relative is reported, never hidden.
  - **1-hour cache writes are priced as 5-minute ones.** 41% of cache-write
    tokens on this corpus are 1-hour writes, charged at 2x base input rather
    than 1.25x; the logs carry the split and the adapter does not read it.
    Understates the total by ~$1,085. Needs a column on `event`, an adapter
    change and a re-ingest — and a re-ingest cannot recover the split for
    sessions whose logs have aged out. See FINDINGS §6b.
- **Watch mode.** ✅ `watch.py` + `cci watch [--serve]` + `/api/live`.
  **It polls rather than using FSEvents, deliberately** — 50 ms to stat the
  whole corpus against a dependency or 150 lines of untestable `ctypes`, and
  polling is the only option that already works on the Windows box v2 assumes.
  `Watcher` is a protocol so FSEvents can still be dropped in. A cycle is
  ~0.15 s because ingest resumes at each byte offset and derive and pricing are
  scoped to the sessions that moved.
- **Session annotation.** Let a session be tagged (client, ticket, billable)
  after the fact — the missing piece for real invoicing. **Next up.** Note that
  the price table now sets the pattern for "a mutable layer over immutable
  history": `origin = 'manual'` is never overwritten by detection, the same
  rule `project_group` follows.

## v2 — Multi-machine, then teams

**Status: multi-machine is done; teams are not started, but nothing about
privacy blocks them any more.**

The schema already carried `host_id` on every row and every id is a content
hash, so the same logical row computes the same id on any machine. That was
deliberate, and it held: this was a backend swap plus a sync path, not a
migration.

### Windows support — done

- **Path handling no longer assumes POSIX separators.** `paths.py` takes the
  flavor from the path *string*, not from `os.name`; `grouping.py`,
  `ingest.py` and `cli.py` read paths only through it.
- Config dir is `%APPDATA%\cc-insights`; `CC_INSIGHTS_HOME` still overrides.
  Globs expand `%APPDATA%`-style variables and ship extra Windows candidates
  alongside the `~`-relative ones, for all three adapters.
- `scripts/install-task.ps1` registers the Task Scheduler job at parity with
  the launchd one. **Not execution-verified** — see the gaps below.
- Fixed on the way: a Windows `db_path` written into `config.toml` unescaped
  is not valid TOML (`\U` and `\A` are escape sequences), so the file the tool
  had just written was unreadable on the next run — and an unreadable
  `host_id` is a regenerated `host_id`, which forks the whole history.

**What this turned out to be about.** The framing was "run on Windows"; the
real requirement is that *any* machine can reason about *any* other machine's
paths, because after sync the box rendering the dashboard is usually not the
box the path came from. On a Mac, `os.path.basename` of a Windows path returns
the whole string, `os.path.isdir` calls a live remote worktree dead, and
`$HOME` cannot be looked up for a host you are not on. So the flavor lives in
the path, comparison is flavor-aware (Windows folds case, POSIX does not), and
stored paths are never rewritten — `project_id = hash(root_path)`, so
normalizing in place would fork the history rather than fix it. See
docs/GROUPING.md § "Paths belong to a machine, not to this one".

### Per-machine probe cache — done (migration 004)

The cached `git_remote` / `git_common_dir` / `path_exists` used to live on
`project`. But `project_id = hash(root_path)`, so a laptop and a desktop that
both keep work at `/Users/you/Coding/X` are **one** project row describing two
different disks, and whichever machine ran `cci group auto` last overwrote the
other. The symptom: delete a checkout on the laptop and the desktop marks the
project you are working in right now `(gone)`.

It now lives in `project_probe (project_id, host_id)`, and every reader says
which machine it means — the ladder prefers the local answer and falls back to
the freshest other one, while "is this path gone" is `MAX` across hosts,
because live on any machine means not gone.

### Postgres + sync — done

`cci sync push | pull | status`, over a shared PostgreSQL. Postgres is a
meeting point, not a replacement: each machine pushes the rows it owns and
pulls everyone else's back into its own SQLite file, so the dashboard,
`metrics`, `stats` and `derive` keep reading SQLite and never learn it
happened. That is why the read path needed no porting at all.

- The migrations stay one source of truth. The only thing the `.sql` files
  cannot express is integer width — SQLite's INTEGER is 64-bit, PostgreSQL's
  is int4 (max 2.1e9), epoch-ms is ~1.79e12 — so `db.translate_ddl` widens
  every INTEGER to BIGINT on the way out. No second schema to drift.
- **Conflicts have explicit rules, not last-writer-wins.** A pin is a human
  saying where a project belongs, so a push from a machine that never heard
  about it must not unpin it. `host.first_seen` only ever moves backwards.
- **`sync.EXCLUDED` says what stays behind and why** — `ingest_file` (local
  paths, no analytical value) and the three cost tables (rates are resolved
  locally; v1 deliberately made `cci price set` a layer no sync touches).
- psycopg is an optional extra. The base install stays dependency-free.

Measured: 191,475 rows push in 5.5 s, pull in 8.9 s, peak RSS 44 MB, re-push
idempotent, headline total round-trips exactly.

### Redaction — done (the design gate for teams)

`docs/REDACTION.md`, `redact.py`, `cci privacy`.

**The finding that shaped it: publishing `project_id` publishes `root_path`.**
The id *is* `sha256(root_path)`, so a colleague can hash a guess. 555 guesses
built only from a username, eight conventional directory names and the repo
names in the remotes recovered **20% of this corpus outright**
(`docs/probes/leakage.py`). Hashing is therefore not redaction, and salting
would break the cross-machine identity the whole schema rests on — so
published rows are re-keyed on the normalized remote, which the viewer already
has.

**The boundary is repo access**, which is where the auth plan below already
pointed: a row may be published only if it belongs to a repo, and only to
people who can already see that repo. Work with no remote has nothing to
derive permission from and stays local — 8% of active time here, withheld
*and counted*, because a view that quietly omits your hours is not private, it
is wrong.

**Redaction happens on the laptop.** The shared database never receives a
path. Filtering at query time fails the first time anything goes wrong and
there is no un-leaking; this is the discipline that has kept prompt text out
of the schema, for the same reason.

`redact.FIELDS` classifies all 110 schema columns closed-by-default, with a
test that fails when a migration adds one nobody has ruled on. It fired for
real on v1's three cost tables during the merge.

### Accounts and teams — not started

What is left is auth and scoping, not privacy plumbing.

- **GitHub OAuth is the right front door**, and not only for convenience:
  groups already carry `forge`/`owner`/`repo`, so a team's scope can be
  *derived* from repo access rather than hand-maintained.
- `host_id` becomes a child of an account; an account belongs to teams.
  `redact.publication()` already takes `actor` as a parameter for this.
- Needs a tenant column and row-level scoping on every query.
- **No publish transport exists.** `redact` builds the projection and `cci
  privacy` shows it; nothing sends it anywhere yet.

Two constraints that the projection cannot enforce and the API must, from its
first commit:

- **Aggregates must be computed inside the viewer's scope.** A precomputed
  "Alice: 40 h this week" spanning repos Bob cannot see leaks their existence
  the moment Bob reads the total.
- **Branch names need a per-repo opt-out.** They are publishable under the
  rule above — repo access shows the branch list — but they are still free
  text, and `feat/restricted-org-dbs` may say more than its author meant.

### Known gaps in what shipped

- **`install-task.ps1` has never run on Windows.** No Windows box, no `pwsh`.
  A review caught three real defects (missing `cmd /c` outer quotes,
  `[TimeSpan]::MaxValue` rejected as a repetition duration, `.Source` throwing
  under `Set-StrictMode`); all are fixed, and the file still needs one real
  run before anyone trusts it.
- **The read path is SQLite-only.** `db.to_dialect` is deliberately naive — it
  covers the DDL and the sync statements and is not a general query
  translator. Pointing the dashboard at Postgres is a separate piece of work.
- **`sync push` sends everything this host owns, every time.** Idempotent and
  fast enough at 191 k rows, but there is no `--since`.
- **Cost is published nowhere.** Excluded from sync by design and classified
  closed for publication. A per-repo cost aggregate is a reasonable thing for
  a team to see and there is no field for it yet.
- **An old `config.toml` stores an absolute `db_path`.** Commit `d41f374` made
  new configs relative so that copying a config directory is safe, but it did
  not rewrite existing ones — so on a config predating it, `--config-dir
  <copy>` still silently writes to the *original* database. This has now
  caused damage twice: the two corrupted fixture rows that motivated
  `d41f374`, and a migration applied to the live database during this work.
  `cci init` could rewrite the path in place; until it does, check the file
  before trusting a copy.

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
