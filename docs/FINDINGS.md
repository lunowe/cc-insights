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

## 3b. opencode keeps everything in one SQLite database

No log files. opencode 1.18 writes `~/.local/share/opencode/opencode.db` (WAL)
with `session` / `message` / `part` tables, each row's payload a JSON blob in a
`data` column. The `storage/**.json` layout older releases used is gone; only
`session_diff` and `migration` remain under `storage/`.

Measured on this machine (2026-09-20): **18 sessions — 9 roots and 9
subagents — 614 messages, 2,434 parts of which 728 are tool calls**, from
2026-02-16. The adapter turns that into **2,649 events across 9 sessions and
18 threads**, 2.56 h active.

Three consequences that shaped the adapter:

- **A subagent is a session row with `parent_id` set**, spawned by the `task`
  tool, with `agent` naming its type ("general", "explore"). Same shape as
  Codex, reached differently: the root's id is the session id for the whole
  tree, each row's own id is the thread id.
- **There is no byte offset to resume from**, and a WAL database's size and
  mtime sit unchanged while `-wal` accumulates, so any watermark keyed on them
  would skip real work. The adapter reports `byte_end = 0` on every event and
  the store is re-read in full each run — 2,649 events in 60 ms, with dedup
  collapsing what was already stored.
- **Two rows carry two real timestamps each.** A tool part has
  `state.time.start` / `state.time.end`; an assistant message has
  `time.created` / `time.completed` (580 of 583 assistant messages record the
  second). Taking only the first would end a session when its last turn
  *began*. Both halves are emitted; only the first carries tokens.

⚠️ **`message.data` for a user turn embeds file contents** — `summary.diffs`
carries the complete `before` text of every file touched — and `part.data`
holds prompts, tool arguments and command output. This is the one source where
the no-content rule needs a test rather than a convention, and it has one:
`tests/test_opencode.py::test_no_message_content_reaches_a_raw_event`.

opencode's own `session.cost` / `message.cost` columns are **0 on every row**
here: subscription and free-tier routes report no per-call price. Cost comes
from token counts and one pricing table for every source, never from this
column.

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

## 6b. Cost: the cheapest token is most of the bill

Measured 2026-09-20 over the whole corpus, at published API rates:

| component | tokens | list-price equivalent | share |
| --- | --- | --- | --- |
| cache read | 11,130 M | $7,824 | **59%** |
| cache write | 358 M | $3,448 | 26% |
| output | 50 M | $1,872 | 14% |
| fresh input | 59 M | $216 | 2% |

**Cache reads outnumber fresh input tokens roughly 190 to 1** and are the
majority of the cost despite being the cheapest component per token. Two
consequences, both load-bearing:

- Pricing all input at the input rate overstates the total by more than an
  order of magnitude; ignoring cache entirely understates it by 85%. The four
  components must be priced separately, which is why `model_price` has four
  rate columns and `event_cost` four cost columns.
- Any future "reduce my spend" advice that starts with output tokens is
  looking at 14% of the bill.

Three further facts a total has to disclose, all of them measured here:

1. **138 M tokens could not be priced at all** — `gpt-6-astra` (113 M) and
   opencode's free-tier routes. Reported, never counted as zero.
2. **12,412 Codex events record usage without naming a model.** The model is
   carried forward from earlier in the same thread. Getting a model and
   getting a price are different things: 11,097 were priced this way, 1,221
   inherited `gpt-6-astra`, which has no rate, and 46 have nothing to inherit
   at all.
3. **The price catalog was wrong about two models, both in the same
   direction: too expensive.**
   - It priced `claude-fable-5-1` as `claude-fable-5`. Fable 5.1 charges
     0.025x base input for a cache hit where every other model charges 0.1x
     — $0.25 against $1.00 per MTok — and on a corpus that is 59% cache
     reads that one substitution was **$2,752, or 21% of the total**.
   - It carried a price rise for `claude-sonnet-5` on 2026-09-01 to $3/$15
     that never happened; those are Sonnet 4.6's rates.

   Both are corrected in `src/cc_insights/price_overrides.json`, checked
   against the vendor's own pricing page. The corpus total fell from $13,418
   to **$10,674**. A near relative is a defensible default and an
   indefensible secret, which is why `pricing.approximations()` exists and
   every surface prints it.

4. **41% of cache-write tokens are 1-hour writes, which cost 60% more.**
   144.7M of 351.2M. Anthropic publishes the rule rather than only the
   numbers: a 5-minute cache write costs 1.25x base input and a 1-hour write
   2x, so on Fable-tier models that is $12.50 against $20 per MTok.
   ✅ Fixed in migration 005: `event.cache_write_1h_tokens` records the part
   of a write that bought an hour, and the two are priced separately. The
   corpus total went from $10,674 to **$11,630** — the missing $956.

   **The split is not random, and it is not something you configure by
   accident.** Claude Code puts every request in one of two buckets and
   picks a TTL per bucket:

   | bucket | 5m tokens | 1h tokens | 1h share |
   | --- | --- | --- | --- |
   | main conversation | 1.7M | 144.9M | **99%** |
   | subagents, workflows, compaction | 204.8M | 0 | **0%** |

   On a subscription within plan usage the main conversation gets the hour;
   once it draws on usage credits it drops to five minutes, because that is
   the cheaper write and the user is now paying. `promptCacheTtl` and
   `subagentPromptCacheTtl` override both. So a corpus's 5m/1h mix encodes
   *when its owner was over their plan limit* — which is a privacy-relevant
   inference, and a reason `cache_write_1h_tokens` is PRIVATE in
   `redact.py` like every other event column.

   A row whose source never reported the split keeps NULL, which means
   *unknown*, not zero. Those tokens are priced at the 5-minute rate and
   counted in `assumed5mTokens`, so a total says how much of itself rests on
   the assumption. `cci backfill` re-reads the logs still on disk and fills
   what it can: on this machine that was 100% of 350.1M tokens, because none
   of the cache-writing sessions had aged out yet.

## 7. Scale

~180k events for the combined corpus. A full year of both stays under 1M rows.
**SQLite is not a performance compromise.** Postgres is a sharing decision.
