# CC-Insights — Build Prompt

> Single source of truth for the build. Work packages fan out to subagents in
> parallel once Stage 0 is merged.
>
> **Status: Stage 0 complete. WP1 (fixtures), WP2 (Claude adapter) and WP3
> (Codex adapter) delivered; 116 tests green. WP2b (subagent transcripts) in
> flight. Next: WP4 (ingest), WP5 (derivation).**
>
> ⚠️ Ground truth was corrected on 2026-09-19 — three bugs, including a glob
> that missed 52% of the Claude Code corpus. **Every acceptance number below
> comes from `docs/probes/canonical_metrics.py`. Re-run it rather than trusting
> a number quoted from memory.**

## Mission

Ingest the logs coding agents already write on this machine, store a normalized
*metadata-only* history in SQLite, and visualize when / how long / how
concurrently those agents ran.

**Stage 1 is urgent** — agent log directories are pruned on a rolling basis, so
unrecorded history is lost permanently. Stage 2 is not urgent.

## Read first — do not skip

1. **`docs/FINDINGS.md`** — measured ground truth. Contains the definition of
   active time, the per-source dedup keys, the Codex thread hierarchy, and
   seven gotchas that each cost a day to rediscover. **Do not re-derive.**
2. **`docs/probes/canonical_metrics.py`** — the reference implementation of the
   time math. WP5 must reproduce its output exactly.
3. **`migrations/001_init.sql`** — the schema, with the portability contract in
   its header comment.
4. **`src/cc_insights/sources/base.py`** — the adapter contract.
5. `~/.claude/CLAUDE.md` — model selection policy.

## Non-negotiables

Already decided. Do not relitigate inside a work package.

1. **Metadata only. Never store message content.** Timestamps, ids, cwd,
   branch, model, tool names, token counts. Never prompt/response text, tool
   arguments, or file contents. `RawEvent` is a frozen slots dataclass
   specifically so an adapter *cannot* smuggle content through it. Content
   found in the DB is a release blocker.
2. **Active time = sum of active span durations.** A gap above the threshold
   contributes **zero**, never a capped `threshold`. See FINDINGS §0 — the
   capped form inflates the total by 59%.
3. **`host_id` on every row.** Multi-machine is a config change, not a migration.
4. **Idempotent ingest.** Deterministic hash ids + `ON CONFLICT DO UPDATE`.
   Re-running over unchanged logs inserts zero rows.
5. **Postgres-portable SQL.** See the header of `migrations/001_init.sql`.
   `tests/test_db.py::test_migration_sql_stays_postgres_portable` enforces it.
6. **No "time saved" metric.** Logs measure agent time, not the human
   counterfactual. See `docs/ROADMAP.md`.

## The data model in one paragraph

A **session** is one root conversation. A **thread** is one serial stream of
events within it. Codex threads are explicit — one log file each, with
`payload.id`, `payload.session_id` and a `source.subagent` marker. Claude Code
logs only the root thread, so its subagent threads are *derived* from `Agent`
`tool_use` → `tool_result` pairs and carry no events of their own. Modelling
both as threads is what lets one timeline render both sources. **Spans** are
derived per thread. Schema: `migrations/001_init.sql` — that file is
authoritative; this document does not duplicate it.

---

# Stage 0 — Foundation ✅ DONE

`pyproject.toml`, `migrations/001_init.sql`, `config.py`, `db.py` (migration
runner), `ids.py` (deterministic ids), `sources/base.py` (adapter protocol),
`cli.py` (`init` / `status` / `config`), 21 passing tests.

Verify with `.venv/bin/python -m pytest -q` and `cci --config-dir <tmp> init`.

---

# Stage 1 — Ingest & Store

WP1–WP3 are independent and dispatch together. WP4 needs WP2 or WP3. WP5 needs
only the schema. **Keep each package to one sitting** — if a package grows,
split it rather than letting one agent run long.

**WP1. Synthetic fixtures** → *muse-spark-1.3*
- `tests/fixtures/claude_code/*.jsonl` and `tests/fixtures/codex/*.jsonl`,
  matching the real shapes in FINDINGS §2–4.
- Must cover: the same event replayed into two files with identical timestamp
  (Claude resume); two Codex threads sharing one `session_id` with overlapping
  ordinals but different timestamps; a 6-hour idle gap; two overlapping
  sessions; an `Agent` tool_use with matching tool_result; a `<synthetic>`
  model event; a truncated final line.
- **Done when:** every file is valid JSONL and `validate_fixtures.py` exits 0.
- **Stop:** fixtures only. Do not touch `src/`.

**WP2. Claude Code adapter** → *opus-5*
- `sources/claude_code.py` implementing the `SourceAdapter` protocol.
- Sessionize on the `sessionId` **field**, not filename (FINDINGS §4.5).
- `native_event_id` = event `uuid`; fall back to `ids.content_fallback_id(line)`
  for `queue-operation` / `pr-link` / `file-history-delta` (FINDINGS §2).
- Root thread only: `native_thread_id == native_session_id`. Emit `tool_use_id`
  on Agent tool_use/tool_result so WP5 can derive subagent threads. **Do not
  derive threads here.**
- Map to `EventKind`; drop `<synthetic>` models; extract `message.usage`.
- Set `byte_end` on every event so ingest can resume mid-file. Skip a truncated
  trailing line without raising.
- **Done when:** parses all WP1 Claude fixtures, and over the real corpus yields
  **118 sessions / 496 threads / 116,722 deduped events**, coverage from
  2026-06-24 (±1% on events only, for logs written since).
- **Stop:** parse and normalize only. No DB, no spans.
- **Status: delivered.** WP2b adds the `subagents/` transcripts (52% of the
  corpus) as subagent threads and applies the stem session-id fallback.

**WP3. Codex adapter** → *opus-5*
- `sources/codex.py`. Globs in `config.DEFAULT_SOURCE_GLOBS["codex"]`.
- Resolve `session_meta` **once per file** and apply to all its events
  (FINDINGS §4.3). `native_thread_id` = `payload.id`,
  `native_session_id` = `payload.session_id`, `parent_native_thread_id` =
  `payload.parent_thread_id`, `is_subagent` = `"subagent" in payload.source`,
  `agent_name` = `payload.agent_nickname`.
- `native_event_id` = `f"{thread_id}:{ordinal}"` — **never `ordinal` alone**
  (FINDINGS §2: that drops 8,709 events).
- **Done when:** parses all WP1 Codex fixtures, and over the real corpus yields
  **188 sessions / 215 threads / 27 subagent threads / 63,005 deduped events**,
  coverage from 2026-02-08.
- **Stop:** parse and normalize only.
- **Status: delivered and verified.** The original 54,296 target was itself the
  output of the bare-ordinal bug; 63,005 is correct. See FINDINGS §5.

**WP4. Ingest layer** → *opus-5*
- `ingest.py`: drive adapters, upsert `project` / `session` / `thread` / `event`.
- Session and thread `started_at`/`ended_at`/`event_count` are aggregates over
  their events. `cwd`/`git_branch`/`cli_version` take the value from the
  **latest** event, so worktree switches land correctly.
- Incremental via `ingest_file`: resume at `bytes_read`; restart from 0 if
  `size_bytes` shrank (rotation).
- **Done when:** two consecutive full ingests — the second inserts 0 rows and
  finishes in < 2 s; row counts match WP2/WP3 acceptance numbers.
- **Stop:** no derived metrics.

**WP5. Derivation engine** → *opus-5*
- `derive.py`, operating purely on `event`/`thread` — independent of adapters.
- **Spans per thread**, using the FINDINGS §0 definition. Port
  `docs/probes/canonical_metrics.py`.
- **Claude subagent threads are NOT derived** — they are real files with real
  timestamps (FINDINGS §2), ingested by WP2b. Use `Agent` `tool_use` →
  `tool_result` pairing only to attribute a subagent thread to the tool call
  that spawned it, not to invent its span.
- `attended`: a gap ending at a `user_prompt` means the user was present → 1;
  a gap inside an agent turn → 0.
- Concurrency by sweep-line over spans.
- **Done when:** per-thread output reproduces FINDINGS §1 exactly —
  Claude Code **1,050 spans / 143.1 h active / 83.2 h wall / 1.72x / peak 7 /
  33.9 h (41%) at ≥2**; Codex **316 spans / 41.5 h active / 36.1 h wall /
  1.15x / peak 4**.
- **Stop:** no CLI, no formatting.

**WP6. CLI + scheduling** → *opus-5 (CLI), muse-spark-1.3 (plist)*
- Extend `cli.py` with `ingest`, `derive`, `stats`.
- launchd plist, 15-minute interval, logs to `<config_dir>/logs/`.
- **Done when:** `cci ingest && cci derive && cci stats` prints the FINDINGS
  numbers; the plist loads and fires.

**WP7. Test suite + CI** → *opus-5*
- Adapter tests on fixtures; golden-number regression tests from FINDINGS §1;
  an idempotency test (ingest twice → identical row counts); a test asserting
  no DB column contains message content.
- **Done when:** `pytest` green, coverage ≥ 80% on `src/cc_insights/`.

### Stage 1 gate

Run a full ingest over the real corpus, confirm FINDINGS §1 reproduces, then
**load the launchd job immediately**. History stops being lost at that moment —
that is the entire point of Stage 1. Do not start Stage 2 first.

---

# Stage 2 — Insights & Visualization

**WP8. Metrics layer** → *opus-5*
- `metrics.py`, one function per question returning plain rows: daily active
  hours; concurrency histogram; per-project/branch totals; session-length
  distribution; model & token mix; attended vs unattended; hour-of-day ×
  day-of-week matrix.
- UTC → local conversion happens **here**, once, not in the UI.
- **Done when:** each function has a test asserting against the real corpus.
- **Stop:** data only. No HTML, no colors.

**WP9. Timeline swimlane** → *opus-5 or fable-5.1 (taste ≥ 7)*
- The centerpiece: one lane per thread across a day/week, grouped by session,
  colored by project; subagent threads render as an indented sub-lane under
  their parent. Overlap must be obvious at a glance.
- Read the `dataviz` skill before writing chart code. Self-contained HTML +
  inline SVG. Light and dark. Works at phone width.
- **Done when:** rendering the real corpus makes the 6.9 h of parallel time and
  the 5-way peak on 2026-09-09 visible without reading a number.

**WP10. Dashboard shell** → *opus-5 or fable-5.1 (taste ≥ 7)*
- Hosts WP9 plus daily active-hours bars, project breakdown, session-length
  histogram, hour-of-day heatmap, model/token mix; date-range and source filters.
- `cci dashboard` emits one static HTML file. No server.
- **Done when:** the file opens correctly offline.

**WP11. Time-tracking export** → *muse-spark-1.3*
- `cci export --from X --to Y --format csv|json`, grouped by project/branch/day.
- Every export embeds the `idle_threshold` used and the generation timestamp —
  a number without its threshold is not defensible in a timesheet.

**WP12. Review gate** → *fable-5.1, plus gpt-6 as an independent second pass*
- Correctness of the time math, schema portability, and any path by which
  message content could reach the DB.

---

## Fan-out rules

- **Stage 0 was serial and is done.** Everything downstream is parallel
  *because* the schema and adapter protocol are frozen. Fan-out without a fixed
  contract produces incompatible halves.
- Dispatch WP1–WP3 as one batch; WP8–WP11 as one batch.
- **Model assignment** (see `~/.claude/CLAUDE.md`):
  - **muse-spark-1.3** — free, ~4 s/call, intelligence 3. Only mechanically
    verifiable output: WP1, WP11, the plist, docstrings, smoke checks.
    `opencode run -m opencode/muse-spark-1.3-contributor-free --dir <path> --auto "<prompt>"`
  - **opus-5** — standing in for gpt-5.6 while Codex is rate-limited: WP2–WP8.
    Keep packages to one sitting each; split rather than run one agent long.
  - **fable-5.1** — WP9, WP10 (taste-critical), WP12 (review).
- **Every subagent prompt carries**: the relevant FINDINGS section, one
  deliverable, its done-when condition, its explicit stop condition.
- Verify output against the done-when condition yourself. Never trust a
  self-reported "done".

## Guardrails

- If a package's real-corpus numbers disagree with FINDINGS §1, **the code is
  wrong, not the findings** — they come from one canonical script. Investigate
  before editing an expected value.
- Never commit `*.db` or `data/` (already in `.gitignore`).
- If Stage 1 slips past ~2 weeks, ship WP2 + WP4 + WP6 alone and load the
  launchd job. A running ingest beats a perfect one.
