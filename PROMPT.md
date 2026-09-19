# CC-Insights — Build Prompt

> Hand this to an orchestrating agent. It is the single source of truth for the
> build. Work packages are designed to fan out to subagents in parallel once
> Stage 0 is merged.

## Mission

Build a local-first tool that ingests the logs coding agents already write on
this machine, stores a normalized *metadata-only* history in SQLite, and
visualizes when / how long / how concurrently those agents ran.

Two stages. **Stage 1 is urgent** (agent log dirs are pruned on a rolling basis,
so unrecorded history is lost permanently). Stage 2 is not.

## Read first — do not skip

1. `docs/FINDINGS.md` — measured ground truth from the real logs. Contains the
   gap distribution, concurrency numbers, and five gotchas that will otherwise
   cost you a day each. **Do not re-derive these.**
2. `docs/probes/` — working reference implementations of active-time and
   concurrency computation. They are correct; productionize rather than reinvent.
3. `~/.claude/CLAUDE.md` — model selection policy for subagent dispatch.

## Non-negotiables

These are design decisions, already made. Do not relitigate them in a work package.

1. **Metadata only. Never store message content.** Ingest timestamps, session
   ids, cwd, git branch, model, tool names, token counts. Never prompt or
   response text, tool arguments, or file contents. This is what makes the
   future cloud mode safe and keeps secrets out of the DB. A work package that
   needs content to work is a work package with a wrong design.
2. **Active time, not wall-span.** Wall-span overstates usage 25x (see
   FINDINGS §2). Active time = sum of inter-event gaps capped at
   `idle_threshold` (default 300 s, configurable).
3. **`host_id` on every row from day one.** The multi-machine future is a config
   change, not a migration. Generate a stable UUID once, store it in config.
4. **Idempotent ingest.** Re-running ingest over the same logs must be a no-op.
   Deterministic ids (hashes of natural keys) + `ON CONFLICT DO UPDATE`.
5. **Postgres-portable SQL.** See § Schema rules. No SQLite-only syntax.
6. **No "time saved" metric.** Logs measure agent time, not the human
   counterfactual. Any such number would be agent-hours x an invented
   multiplier. Ship leading indicators instead and let the user apply their own
   multiplier explicitly, in the UI, where it is visibly theirs. See ROADMAP.

## Schema (the integration contract)

This is fixed before fan-out so packages can be built in parallel against it.
Migration `001_init.sql`.

```sql
CREATE TABLE schema_migrations (
  version     INTEGER PRIMARY KEY,
  applied_at  INTEGER NOT NULL           -- epoch ms UTC
);

CREATE TABLE host (
  host_id     TEXT PRIMARY KEY,          -- stable UUID, generated once into config
  hostname    TEXT NOT NULL,
  os          TEXT,
  first_seen  INTEGER NOT NULL,
  last_seen   INTEGER NOT NULL
);

CREATE TABLE project (
  project_id  TEXT PRIMARY KEY,          -- sha256(root_path)[:32]
  root_path   TEXT NOT NULL UNIQUE,
  name        TEXT NOT NULL              -- basename, human-facing
);

CREATE TABLE session (
  id           TEXT PRIMARY KEY,         -- sha256(host_id|source|native_id)[:32]
  native_id    TEXT NOT NULL,            -- sessionId / session_meta.session_id
  source       TEXT NOT NULL,            -- 'claude_code' | 'codex'
  host_id      TEXT NOT NULL REFERENCES host(host_id),
  project_id   TEXT REFERENCES project(project_id),
  cwd          TEXT,
  git_branch   TEXT,
  cli_version  TEXT,
  started_at   INTEGER NOT NULL,         -- epoch ms UTC
  ended_at     INTEGER NOT NULL,
  event_count  INTEGER NOT NULL DEFAULT 0,
  active_ms    INTEGER NOT NULL DEFAULT 0,
  UNIQUE (host_id, source, native_id)
);

CREATE TABLE event (
  id                 TEXT PRIMARY KEY,   -- sha256(session_id|ordinal)[:32]
  session_id         TEXT NOT NULL REFERENCES session(id),
  ts                 INTEGER NOT NULL,   -- epoch ms UTC
  ordinal            INTEGER NOT NULL,   -- per-session monotonic
  kind               TEXT NOT NULL,      -- user_prompt|assistant|tool_use|tool_result|system
  model              TEXT,               -- NULL for non-model events; drop '<synthetic>'
  tool_name          TEXT,
  tool_use_id        TEXT,               -- for tool_use/tool_result pairing
  input_tokens       INTEGER,
  output_tokens      INTEGER,
  cache_read_tokens  INTEGER,
  cache_write_tokens INTEGER,
  UNIQUE (session_id, ordinal)
);

CREATE TABLE span (                      -- derived: contiguous active work blocks
  id           TEXT PRIMARY KEY,
  session_id   TEXT NOT NULL REFERENCES session(id),
  started_at   INTEGER NOT NULL,
  ended_at     INTEGER NOT NULL,
  event_count  INTEGER NOT NULL,
  attended     INTEGER                   -- 1 user present, 0 unattended, NULL unknown
);

CREATE TABLE subagent_span (             -- derived: Agent tool_use -> tool_result
  id           TEXT PRIMARY KEY,
  session_id   TEXT NOT NULL REFERENCES session(id),
  tool_use_id  TEXT NOT NULL,
  agent_type   TEXT,
  started_at   INTEGER NOT NULL,
  ended_at     INTEGER,                  -- NULL if never returned
  UNIQUE (session_id, tool_use_id)
);

CREATE TABLE ingest_file (               -- incremental ingest bookkeeping
  host_id      TEXT NOT NULL,
  path         TEXT NOT NULL,
  source       TEXT NOT NULL,
  size_bytes   INTEGER NOT NULL,
  mtime_ms     INTEGER NOT NULL,
  bytes_read   INTEGER NOT NULL,
  lines_read   INTEGER NOT NULL,
  last_ingest  INTEGER NOT NULL,
  PRIMARY KEY (host_id, path)
);
```

### Schema rules (Postgres portability)

- **All timestamps are `INTEGER` epoch milliseconds UTC.** Never ISO strings,
  never local time. Postgres migration is `to_timestamp(ts/1000.0)`.
- **Every epoch-ms column maps to `BIGINT` in Postgres, never `INTEGER`.**
  SQLite's `INTEGER` is 64-bit, but Postgres `INTEGER` is int4 (max 2.1e9) and
  epoch-ms is ~1.79e12 — a plain port overflows on the first insert. This also
  applies to `size_bytes` and `bytes_read` in `ingest_file`. The Postgres
  migration must be written with `BIGINT`, and a test must assert a
  present-day timestamp round-trips.
- **All ids are `TEXT`.** No `AUTOINCREMENT`, no integer surrogate keys.
- Upserts use `INSERT ... ON CONFLICT (...) DO UPDATE` — valid in both engines.
- No `strftime`, no `julianday`, no dynamic typing tricks. Date bucketing for
  reports happens in application code, not in stored SQL.
- Migrations are numbered `.sql` files applied in order, tracked in
  `schema_migrations`. Never edit a shipped migration; add a new one.

---

# Stage 0 — Foundation (SERIAL, blocks everything)

**Owner: orchestrator (opus-5). Do not delegate — this is the contract every
other package builds against.**

**WP0. Repo skeleton + schema + adapter protocol**
- Python 3.11+, `src/cc_insights/`, `pyproject.toml`, pytest.
- `migrations/001_init.sql` exactly as above; migration runner.
- `config.py`: TOML at `~/.config/cc-insights/config.toml` — `db_path`,
  `idle_threshold_s = 300`, `host_id` (generated on first run), source globs.
- `sources/base.py`: the adapter protocol every source implements —
  ```python
  class SourceAdapter(Protocol):
      name: str
      def discover(self) -> Iterable[Path]: ...
      def parse(self, path: Path, from_byte: int) -> Iterator[RawEvent]: ...
  ```
  `RawEvent` is a dataclass mapping 1:1 onto the `event` table plus the session
  fields needed to upsert `session`.
- **Done when:** `pytest` passes on an empty suite, `cci init` creates the DB
  with migration 001 applied, and `sources/base.py` is importable.
- **Stop:** do not write any adapter here.

---

# Stage 1 — Ingest & Store (PARALLEL after WP0)

WP1–WP4 have no dependencies on each other. Dispatch simultaneously.

**WP1. Synthetic fixtures** → *muse-spark-1.3*
- Generate `tests/fixtures/claude_code/*.jsonl` and `tests/fixtures/codex/*.jsonl`:
  synthetic log lines matching the real shapes documented in FINDINGS §5.
- Must cover: multi-file session resume, a 6-hour idle gap, two overlapping
  sessions, an `Agent` tool_use with a matching tool_result, a `<synthetic>`
  model event, and a truncated final line (crash mid-write).
- **Done when:** every fixture file is valid JSONL (one object per line) and a
  provided `validate_fixtures.py` exits 0.
- **Stop:** fixtures only. Do not touch `src/`.
- *Rationale: mechanical, spec is exact, output is machine-verifiable.*

**WP2. Claude Code adapter** → *gpt-5.6 (or fable-5.1 if Codex is limited)*
- Implement `sources/claude_code.py` against the WP0 protocol.
- Glob `~/.claude/projects/*/*.jsonl`. Top-level `cwd`/`gitBranch`/`version`.
- Sessionize on the `sessionId` **field**, not filename (FINDINGS §1).
- Extract `message.usage` token fields; drop `<synthetic>` models.
- Emit `tool_use_id` for tool_use/tool_result so WP4 can pair subagent spans.
- Tolerate truncated trailing lines without losing the file.
- **Done when:** parses all WP1 Claude fixtures, and a real-corpus run reproduces
  **120 sessions / 53,915 events** (±1% for logs written since the probe).
- **Stop:** no derived metrics, no span logic. Parse and normalize only.

**WP3. Codex adapter** → *gpt-5.6 (or fable-5.1 if Codex is limited)*
- Implement `sources/codex.py`. Glob `~/.codex/sessions/*/*/*/*.jsonl`.
- Metadata is nested under `payload`; the `session_meta` line carries `cwd`,
  `originator`, `cli_version`, `model_provider` (FINDINGS §5.4).
- Also read `~/.codex/archived_sessions/`.
- **Done when:** parses all WP1 Codex fixtures and a real-corpus run yields
  **212 sessions**, coverage starting 2026-02-08.
- **Stop:** parse and normalize only.

**WP4. Derivation engine** → *gpt-5.6 (or fable-5.1 if Codex is limited)*
- `derive.py`, operating purely on the `event` table — **independent of adapters**.
- `active spans`: split a session's events wherever the gap exceeds
  `idle_threshold`; write to `span`. Port `docs/probes/gap_distribution.py`.
- `attended`: an inter-event gap that ends at a `user_prompt` = user was present
  (reading/thinking) → `attended = 1`. A gap inside an agent turn = unattended
  → `attended = 0`.
- `subagent_span`: pair `Agent` tool_use → tool_result on `tool_use_id`.
  **`isSidechain` is always 0 — ignore it** (FINDINGS §5.1).
- `concurrency`: sweep-line over spans. Port `docs/probes/concurrency.py`.
- **Done when:** on the real corpus it reproduces FINDINGS §4 exactly —
  582 spans, 65.6 active h, 56.9 h wall-clock, 1.15x multiplier, peak 5 on
  2026-09-09. These are regression tests.
- **Stop:** no CLI, no output formatting.

**WP5. CLI + scheduling** → *muse-spark-1.3 for the plist, gpt-5.6 for the CLI*
- `cci init | ingest [--source X] | derive | stats | export`.
- `ingest` is incremental via `ingest_file` (resume at `bytes_read`; re-read from
  0 if `size_bytes` shrank — file was rotated).
- launchd plist, 15-minute interval, logging to `~/.config/cc-insights/logs/`.
- **Done when:** two consecutive `cci ingest` runs — the second inserts 0 rows
  and completes in < 2 s.

**WP6. Test suite + CI** → *gpt-5.6*
- Unit tests per adapter on fixtures; golden-number regression tests from
  FINDINGS §4; idempotency test (ingest twice → identical row counts).
- **Done when:** `pytest` green, coverage ≥ 80% on `src/cc_insights/`.

### Stage 1 gate

Before any Stage 2 work starts: run ingest over the real corpus and confirm the
FINDINGS §4 numbers reproduce. Then **schedule the launchd job immediately** —
history stops being lost at that moment, which is the whole point of Stage 1.

---

# Stage 2 — Insights & Visualization (PARALLEL after Stage 1 gate)

**WP7. Metrics layer** → *gpt-5.6*
- `metrics.py` — one function per question, each returning plain rows:
  daily active hours; concurrency histogram; per-project/branch totals;
  session-length distribution; model & token mix; attended vs unattended ratio;
  hour-of-day x day-of-week heatmap matrix.
- Timezone conversion (UTC → local) happens **here**, once, not in the UI.
- **Done when:** each function has a test asserting against the real corpus.
- **Stop:** data only. No HTML, no colors.

**WP8. Timeline swimlane** → *opus-5 or fable-5.1 (taste ≥ 7 required)*
- The centerpiece view: one horizontal lane per session across a day/week,
  colored by project, overlap visually obvious at a glance. Subagent spans
  render as a thinner sub-lane inside their parent session's lane.
- Read the `dataviz` skill before writing chart code.
- Self-contained HTML + inline SVG/canvas. Light and dark. Works at phone width.
- **Done when:** rendering the real corpus makes the 6.7 h of parallel time and
  the 5-way peak on 2026-09-09 immediately visible without reading a number.

**WP9. Dashboard shell** → *opus-5 or fable-5.1 (taste ≥ 7 required)*
- Hosts WP8 plus: daily active-hours bars, project breakdown, session-length
  histogram, hour-of-day heatmap, model/token mix. Date-range + source filters.
- Generated as a single static HTML file from the DB (`cci dashboard`). No server.
- **Done when:** one command produces a file that opens correctly offline.

**WP10. Time-tracking export** → *muse-spark-1.3*
- `cci export --from X --to Y --format csv|json`, grouped by project/branch/day,
  with active hours per bucket.
- Every export embeds the `idle_threshold` used and the generation timestamp —
  a number without its threshold is not defensible in a timesheet.
- **Done when:** round-trips into a spreadsheet with correct totals.

**WP11. Review gate** → *fable-5.1, plus gpt-6 as an independent second pass*
- Review the full implementation for correctness of the time math, schema
  portability, and any place message content leaked into the DB.
- **Done when:** both reviews return no critical findings.

---

## Fan-out rules

- **Stage 0 is serial and undelegated.** Everything else is parallel *because*
  the schema and adapter protocol are fixed first. Do not start Stage 1 before
  WP0 is merged — fan-out without a fixed contract produces four
  incompatible halves.
- Dispatch WP1–WP4 in a single batch, WP7–WP10 in a single batch.
- **Model assignment** follows `~/.claude/CLAUDE.md`:
  - muse-spark-1.3 (free, ~4 s/call, intelligence 3): WP1, WP10, the plist,
    docstrings, smoke checks. Only work whose output is *mechanically
    verifiable*. Invoke as:
    `opencode run -m opencode/muse-spark-1.3-contributor-free --dir <path> --auto "<prompt>"`
  - gpt-5.6 via the codex plugin: WP2–WP6, WP7. **Currently rate-limited** — if
    still limited, escalate these to fable-5.1, never down to muse-spark.
  - opus-5 / fable-5.1: WP0, WP8, WP9 (taste-critical and design-critical).
- **Every subagent prompt must carry**: the relevant FINDINGS section, its
  single deliverable, its done-when condition, and its explicit stop condition.
  These models will otherwise keep going indefinitely.
- Verify subagent output yourself against the done-when condition. Do not
  trust a self-reported "done".

## Guardrails

- If a package's real-corpus numbers disagree with FINDINGS §4, **the code is
  wrong, not the findings** — they were measured directly. Investigate before
  changing an expected value.
- Never commit `*.db` or anything under `data/` (already in `.gitignore`).
- If content text is found anywhere in the DB, that is a release blocker.
- If Stage 1 grows past ~2 weeks of work, ship WP0+WP2+WP5 alone and schedule
  the ingest job. A running ingest beats a perfect one.
