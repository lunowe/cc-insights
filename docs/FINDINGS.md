# Findings — measured ground truth

Regenerated 2026-09-19 by `docs/probes/canonical_metrics.py`, which is the
**spec for the time math**. Trust that script over any number quoted elsewhere;
where this document and the script disagree, the script wins.

> **Three ground-truth bugs were found and fixed after the first draft.** All
> three had already propagated into this file as "measured" fact, and two were
> caught by implementers who refused to accept an acceptance number they could
> not reproduce. They are documented in §5 because the lesson generalizes: a
> reference implementation that contains the bug it warns about will manufacture
> confident, wrong ground truth.

## 0. Definition — what "active time" means

> **Active time** is the sum of active *span* durations, where a span is a
> maximal run of events whose consecutive gaps are ≤ `idle_threshold`.
> A gap **above** the threshold contributes **zero**.

Spans are computed **per thread**, never per session. A session groups a root
thread with its subagent threads; merging them into one stream would hide
exactly the parallelism this tool exists to measure.

The tempting alternative, `sum(min(gap, threshold))`, invents `threshold`
seconds of work per idle gap and inflated an early draft by 59%. Do not
reintroduce it.

## 0b. The corpus moves while you measure it

Every number below drifts upward between runs, because this machine writes new
agent logs continuously — including the sessions doing this work. Between two
runs minutes apart the Claude Code event count moved 116,722 → 116,947.

**Therefore acceptance must not compare against a frozen number.** Load the
probe and the implementation *in one process at the same instant* and compare
key sets. That is exact and stable; a literal count is neither. Frozen numbers
below are dated and indicative only.

## 1. Corpus (as of 2026-09-19)

| | Claude Code | Codex |
| --- | --- | --- |
| Files | 120 main + 386 subagent | 215 |
| Sessions | 118 | 188 |
| **Threads** | **496** | **215** |
| Events (deduped) | 116,722 | 63,005 |
| Coverage | 2026-06-24 → 2026-09-19 | 2026-02-08 → 2026-09-19 |
| Active spans | 1,050 | 316 |
| **Active time** | **143.1 h** | **41.5 h** |
| Wall-clock with ≥1 active | 83.2 h | 36.1 h |
| **Parallelism multiplier** | **1.72x** | 1.15x |
| Peak concurrency | 7 | 4 |
| Time ≥2 concurrent | **33.9 h (41%)** | 3.3 h (9%) |

Claude Code concurrency distribution (share of the 83.2 h):

| threads | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| hours | 49.4 | 17.6 | 9.4 | 4.8 | 1.3 | 0.4 | 0.2 |
| share | 59.3% | 21.1% | 11.3% | 5.8% | 1.6% | 0.5% | 0.3% |

**Parallelism is the headline, not a footnote.** 41% of Claude Code wall-clock
has two or more threads running. An early draft put this at 12%, because it was
measuring only 48% of the data.

## 2. Claude Code writes subagent transcripts to disk

This was missed entirely at first and is the single largest correction.

- `~/.claude/projects/<project>/<session>/subagents/agent-<id>.jsonl` — 328 files
- `~/.claude/projects/<project>/<session>/subagents/workflows/<wf>/agent-<id>.jsonl` — 58 files

They carry **`isSidechain: true`**, an **`agentId`**, and a `sessionId` pointing
at the parent session. They hold **59,667 timestamped events — 52% of all
Claude Code activity — with zero uuid overlap** with the main transcripts.

Consequence: Claude Code subagent threads do **not** need deriving from
`Agent` tool_use/tool_result pairing. They are real threads with real
timestamps, exactly like Codex's. Pairing remains useful only to attribute a
subagent thread to the specific tool call that spawned it.

**Subagent transcripts name their agent type.** Assistant lines carry
`attributionAgent`, constant per `agentId` (0 of 371 agents carry two values)
and absent from main transcripts entirely. Observed on this corpus:
`general-purpose` 45,451 events, `claude` 8,193, `Explore` 5,669,
`workflow-subagent` 3,896, `codex:codex-rescue` 504. This is a free dimension
for the dashboard — which *kind* of agent consumes the time — and it is only
available because subagent transcripts are ingested. It is absent from the
opening user line, so sniff it from the head of the file, not per event, or a
resumed parse yields a different name than a whole-file parse.

`journal.jsonl` (8 files, 97 lines) has no `timestamp`/`uuid`/`sessionId` and
is dropped by the no-timestamp guard — it creates no phantom threads.

Classify a thread as a subagent by the event's own **`agentId`** (present on
100% of subagent events, 0% of main-transcript events), not by its path, so
classification survives a future layout change.

⚠️ The earlier claim "**`isSidechain` is always false**" was an artifact of
looking only at `projects/*/*.jsonl`. It is false in *main* transcripts and
true in *subagent* transcripts. The flag is reliable; the glob was not.

## 3. Codex records threads explicitly

Each Codex log file is one **thread**. Its first `session_meta.payload` carries:

- `id` — this thread's own id (unique per file, 1:1 with the file)
- `session_id` — the **root** session, shared by all its threads
- `parent_thread_id`, `forked_from_id`, `subagent_history_start_ordinal`
- `source` — a **dict** only on subagent threads (`{"subagent": {...}}`); on
  root threads it is a plain **string** (`"vscode"`, `"cli"`). A literal
  `"subagent" in payload.source` is a substring test against a string and a
  false positive waiting to happen — gate on `isinstance(source, dict)`.

Totals: **188 root sessions, 215 threads, 27 subagent threads**, 7 roots with
more than one thread, max 9 under one root. Sibling threads hold *disjoint*
events — genuinely parallel work, not replay.

**Only the FIRST `session_meta` is authoritative.** Three files carry a second
one whose `payload.id` is the root session id rather than the thread id; taking
the last, or re-resolving per line, corrupts the thread count.

## 4. Dedup keys — different per source, both traps fatal

`native_event_id` must be unique **within its session**.

**Claude Code — the event `uuid`.** 1,295 (session, uuid) pairs appear twice;
1,294 are in *different files* with **identical timestamp and type**, because
resume replays history forward. Deduping on `uuid` collapses them; an
ingest-assigned ordinal would overcount by ~2.6%. `uuid` is absent only on
`queue-operation` / `pr-link` / `file-history-delta` (3,502 events) — fall back
to a hash of the raw line.

**Codex — `"<thread_id>:<ordinal>"`, never `ordinal` alone.** `ordinal` is
*thread-scoped* and restarts at 0 in every thread. Keying on
`(session_id, ordinal)` collides 2,352 times and **silently drops 8,709 real
events**; every collision group spans more than one timestamp and none occurs
within a single thread. Codex never replays history, so the correct key dedups
nothing — `63,005` raw lines yield `63,005` events.

**Session-id fallback uses the file STEM, not the basename.** 36
`file-history-delta` events carry no `sessionId`; their file stems are already
real session ids. A basename fallback (keeping `.jsonl`) mints 3 phantom
sessions — that is how 118 real sessions became a "measured" 121.

## 5. How the ground truth was wrong (keep this section)

1. **The Codex reference implementation contained the exact bug its own
   findings condemned** — `nid = str(ordinal)`. It produced 54,296 events, and
   that number was published as ground truth. `63,005 − 8,709 = 54,296`
   reproduces it precisely. Caught by the WP3 implementer, who reported that
   acceptance criteria #2 and #3 were mutually exclusive rather than quietly
   satisfying the wrong one.
2. **The basename/stem fallback** minted 3 phantom sessions (121 vs 118).
   Caught by the WP2 implementer.
3. **The glob missed 52% of the corpus.** Caught by the WP2 implementer, who
   noticed the stated file count (504) did not match what the stated glob
   returns (120).

The pattern: every number here is only as good as the script that produced it.
**If an implementation cannot reproduce a number, suspect the number too.**

## 6. Other gotchas

1. **`<synthetic>`** appears as a Claude model name — exclude it from model and
   cost breakdowns.
2. **Codex nests everything under `payload`**; Claude Code puts `cwd`,
   `gitBranch`, `version` at the top level of every event.
3. **Codex `input_tokens` is inclusive of `cached_input_tokens`.** Verified over
   25,181 usage blocks. Emit `input_tokens - cached_input_tokens` as fresh
   input, or cost math double-counts. Codex's `total/turn/thread_token_usage`
   fields are **cumulative** — summing them explodes the totals. Use
   `token_count.info.last_token_usage` and `token_usage_record.usage`.
4. **Codex `item_completed` mirrors the `response_item` stream** (10,325
   events, present in 214 of 215 files). Mapping it through its inner
   `item.type` double-counts every prompt, message and tool call.
5. **`attachment` is Claude Code's second-largest event type** (15,843
   timestamped lines) — system-injected context, not user turns.
6. **A session's events span multiple files.** Sessionize on the id *field*.
7. **PostgreSQL `INTEGER` is int4** (max 2.1e9); epoch-ms is ~1.79e12. Every
   timestamp column must be `BIGINT` on Postgres. Guarded in `tests/test_db.py`.
8. **Logs are a rolling window.** Claude reaches back only to June while Codex
   reaches February. Unrecorded history is lost permanently — which is why
   ingest is urgent and dashboards are not.

## 7. Scale

~180k events for the combined corpus. A full year of both stays under 1M rows.
**SQLite is not a performance compromise.** Postgres is a sharing decision.
