# CC-Insights local API — frozen contract

`cci serve` starts a read-only HTTP server on `127.0.0.1:8787` that serves this
JSON API and the built frontend. Backend and frontend are built in parallel
against this document; **it is frozen** — if it needs to change, say so rather
than diverging.

Extended twice, both times **additively**, and no field that existed before
either change has changed name, type or meaning:

1. Project grouping (`docs/GROUPING.md`): the `group` filter, `GET
   /api/groups`, `groupId`/`groupName`/`groupPinned` on `/api/projects`, and
   `groups` on `/api/meta`.
2. Cost: `GET /api/cost`, `cost` on `/api/summary`, `/api/daily` days and
   `/api/projects` rows, `currency` beside those, and `pricing` on
   `/api/meta`.

- All times on the wire are **epoch milliseconds UTC**. The frontend converts to
  local for display; the backend never guesses a timezone.
- All durations are **milliseconds**, named `*Ms`.
- Every endpoint accepts the same `Filters` query string.
- Read-only. No POST, no auth, localhost only.
- Every `cost` is a **list-price equivalent** in `currency` units: what the
  filtered traffic would have cost at published API rates. **It is not a
  bill** — a subscription charges a flat fee however many tokens run through
  it. A renderer must label it as such, and must show `unpricedTokens`
  alongside it: tokens no rate covered are unknown, not free.
- **Every per-event number here is measured inside the surviving spans**, and
  a thread with a single event yields no span at all. So `summary.events`,
  `summary.tokens` and every `cost` exclude such a thread — the same rule
  that already governs `sessions`/`threads`/`spans`, applied consistently.
  `cci cost` on the command line counts every event instead and can
  therefore read very slightly higher. Zero such threads exist on the
  author's corpus; the rule is stated so the first one is not a surprise.

## Filters (query string, all optional)

| param | type | meaning |
| --- | --- | --- |
| `project` | repeated | `project_id`; repeat to include several. Omitted = all |
| `group` | repeated | `group_id`; repeat to include several. Omitted = all |
| `source` | repeated | `claude_code` \| `codex` \| `opencode`. Omitted = all |
| `from` | epoch ms | inclusive lower bound on span start |
| `to` | epoch ms | exclusive upper bound on span start |
| `role` | string | `all` (default) \| `root` \| `subagent` |

`GET /api/summary?project=abc&project=def&source=codex&role=root`

A filter narrows **spans**; counts of sessions/threads are counts of those
reachable from the surviving spans.

An unknown `project` or `group` id matches nothing; it is never an error.

### `project` + `group` is a UNION

A project row is one on-disk path; a **group** is the logical project behind
several of them (`docs/GROUPING.md`). Both select spans by which project the
span's session belongs to, so the two **union** with each other:

`?group=G&project=P` = "the spans of group G, **plus** the spans of project P"

not "the spans of project P that are also in group G". A user who narrows to a
group and then ticks one more stray project expects to see both, and an
intersection would empty the dashboard whenever P is not a member of G.
Repeating either parameter already unions, and this is the same rule across
the two.

That union then **intersects** with `source`, `from`, `to` and `role` as
usual: `?group=G&project=P&source=codex` is "(G or P) and codex".

## Types

```ts
type Source = "claude_code" | "codex" | "opencode";
type Role = "all" | "root" | "subagent";

// GET /api/meta  — everything needed to populate the filter controls.
// Not affected by filters.
type Meta = {
  hostname: string;
  firstTs: number | null;
  lastTs: number | null;
  sources: Source[];
  projects: { projectId: string; name: string; rootPath: string; activeMs: number }[];
  // The group filter control, in the same call as the project one. A roster,
  // like `projects`: a group with no time yet is still a choice, at 0 ms.
  // Empty until `cci group auto` has run, which is the normal first state.
  groups: { groupId: string; name: string; activeMs: number }[];
  agents: { agentName: string; source: Source }[];
  models: string[];
  idleThresholdS: number;   // must be shown wherever a duration is exported
  // Where the money came from, so a page can footnote its own totals without
  // a second round trip. `approximations` are models the price catalog
  // matched to a NEAR RELATIVE rather than to themselves -- defensible as a
  // default, never acceptable to hide. `unpricedModels` have no rate at all.
  pricing: {
    catalog: { repo?: string; commit?: string; fetched_at?: string; license?: string };
    currency: string;
    approximations: { model: string; pricedAs: string }[];
    unpricedModels: string[];
  };
  generatedAt: number;
};

// GET /api/summary
type Summary = {
  // sessions/threads/events count rows REACHABLE FROM THE SURVIVING SPANS, not
  // global table counts -- a global total under role=subagent would describe
  // none of the numbers beside it. A thread with a single event yields no span
  // and so is absent here by design.
  sessions: number; threads: number; events: number; spans: number;
  activeMs: number;
  bySource: { source: Source; activeMs: number }[];
  // These three partition activeMs exactly.
  humanInitiatedMs: number;   // root thread, a person typed the opening turn
  autonomousMs: number;       // subagent thread: a model spawned it
  unattendedRootMs: number;   // root thread that resumed with no human turn
  tokens: { input: number; output: number; cacheRead: number; cacheWrite: number };
  cost: CostTotals;
};

// Shared by /api/summary.cost and /api/cost. A LIST-PRICE EQUIVALENT, not a
// bill -- see the note at the top.
type CostTotals = {
  total: number;                 // in `currency` units
  currency: string;              // "USD", or "mixed" if rates disagree
  byComponent: { input: number; output: number; cacheRead: number; cacheWrite: number };
  pricedEvents: number;
  // Priced off a model carried forward from an earlier event in the same
  // thread, because Codex records usage on events that name no model.
  attributedEvents: number;
  // Tokens inside the filtered spans that no rate covered. NOT zero-cost:
  // unknown. Show this wherever `total` is shown.
  unpricedTokens: number;
};

// GET /api/timeline — the swimlane. One row per span.
// Cap: if the range yields > `limit` spans the server returns the widest
// `limit` spans and sets `truncated`, so the UI can tell the user rather than
// silently dropping work.
type Timeline = {
  spans: {
    spanId: string; threadId: string; sessionId: string;
    projectId: string | null; projectName: string | null;
    // The LOGICAL project this span belongs to. A swimlane lane must be
    // labelled and coloured by this, not by projectName: a worktree's path is
    // called `tenant-restricted` while the project is atlas-chat, and
    // labelling by path splits one project across several differently-named,
    // differently-coloured lanes. Null when the path is not grouped yet.
    groupId: string | null; groupName: string | null;
    source: Source; agentName: string | null;
    isSubagent: boolean;
    parentThreadId: string | null;
    attended: 0 | 1 | null;
    start: number; end: number;
  }[];
  truncated: boolean;
  limit: number;
};

// GET /api/daily — one row per local calendar day in range, gaps filled with 0.
type Daily = {
  // activeMs = sum of that day's span time. wallMs = the UNION of that day's
  // spans, so concurrent work is counted once: activeMs > wallMs exactly when
  // agents ran in parallel that day. bySource OMITS a source with no activity,
  // so it is Partial, not a total Record.
  days: { date: string; activeMs: number; wallMs: number;
          bySource: Partial<Record<Source, number>>;
          cost: number }[];
  currency: string;
};

// GET /api/concurrency — sweep-line over the filtered spans.
type Concurrency = {
  timeAtLevel: Record<string, number>;  // "1" -> ms, "2" -> ms, ...
  peak: number;
  peakAt: number | null;
  wallMs: number;      // wall-clock with >= 1 active
  activeMs: number;    // sum of span durations
  multiplier: number;  // activeMs / wallMs
};

// GET /api/projects — one row per on-disk path. `groupId`/`groupName` are null
// for an ungrouped project, which is legal. `groupPinned` means a human placed
// this project in that group, so detection must never move it.
type Projects = {
  projects: { projectId: string; name: string; rootPath: string;
              groupId: string | null; groupName: string | null;
              groupPinned: boolean;
              // false = the directory is gone (a finished worktree, a deleted
              // checkout). Ten such paths hold real recorded time on the
              // author's corpus, so the UI must be able to mark them historical
              // rather than imply they are still live. null = detection has not
              // probed this path yet; that is NOT the same as gone, and the UI
              // must not render it as such. Path shape is not a substitute:
              // inferring it that way mislabels 5 live worktrees out of 15.
              pathExists: boolean | null;
              activeMs: number; sessions: number; threads: number;
              firstTs: number; lastTs: number;
              cost: number }[];   // list-price equivalent, in `currency`
  currency: string;
};

// GET /api/groups — one row per LOGICAL project (docs/GROUPING.md): the 5 rows
// and 9 dead worktree paths of one repo add up here instead of reading as 14
// unrelated projects.
//
// A ranking, not a roster: a group no surviving span reaches is absent rather
// than present with zeros. `meta.groups` is the roster.
//
// `ungrouped` is the exact complement — every surviving span whose project has
// no group, including the rare span whose session carries no project at all —
// so that, under ANY filter:
//     sum(groups[].activeMs) + ungrouped.activeMs === summary.activeMs
// Before `cci group auto` has ever run, `groups` is [] and `ungrouped` holds
// the whole corpus. That is the starting state, not an error.
//
// `projects`/`pinnedProjects` are filter-aware like every other count here:
// they count the members the surviving spans reach, not membership on paper.
type Groups = {
  groups: {
    groupId: string; name: string;
    origin: "git_remote" | "git_common_dir" | "path_worktree" | "path_ancestor" | "manual";
    forge: string | null; owner: string | null; repo: string | null; webUrl: string | null;
    activeMs: number; sessions: number; threads: number; projects: number;
    pinnedProjects: number;
    firstTs: number; lastTs: number;
  }[];
  ungrouped: { projects: number; activeMs: number };  // projects with no group
};

// GET /api/agents  — Claude records an agent TYPE; Codex records a random
// per-thread nickname, so the frontend groups Codex under one row.
type Agents = {
  agents: { agentName: string; source: Source; threads: number; activeMs: number }[];
};

// GET /api/heatmap — local weekday x hour. weekday 0 = Monday.
type Heatmap = { cells: { weekday: number; hour: number; activeMs: number }[] };

// GET /api/cost — the list-price equivalent, broken down and qualified.
//
// NOT A BILL. A Claude Max or ChatGPT Plus subscription charges a flat monthly
// fee no matter how many tokens run through it, and opencode reports 0 for
// every call it makes. This number is for comparing projects, models and
// months against each other; it is wrong in an invoice. Every caveat below is
// machine-readable so a renderer can show them rather than paraphrase them.
type Cost = CostTotals & {
  byModel: { model: string; cost: number; events: number; attributed: number }[];
  bySource: { source: Source; cost: number }[];
  daily: { date: string; cost: number }[];      // local calendar days, no gap fill
  // What could not be priced, and why:
  //   no_rate      the model has no rate on file at that date
  //   no_model     nothing in the thread said which model ran (model is null)
  //   no_component the model is priced, but not for this token component
  //                (OpenAI publishes no cache-write rate)
  unpriced: { model: string | null;
              reason: "no_rate" | "no_model" | "no_component";
              tokens: number; events: number }[];
  // Models the catalog priced as a near relative. On the author's corpus
  // `claude-fable-5-1` is priced as `claude-fable-5`, whose cache reads cost
  // four times as much -- thousands of dollars of difference on a corpus with
  // billions of cache-read tokens. Show it next to the total.
  approximations: { model: string; pricedAs: string }[];
  catalog: { repo?: string; commit?: string; fetched_at?: string; license?: string };
};
```

## `GET /api/live` — the watch-mode stream

Not one of the endpoints above, and deliberately outside the `Filters`
contract: those are filtered, cacheable and capturable as a fixture, and a
stream is none of the three.

`cci watch --serve` runs the pipeline in the background and feeds this
endpoint, so an open dashboard refreshes when a log file grows instead of
polling. Content type `text/event-stream`.

```
event: hello
data: {"generation": 12, "last": {...}, "heartbeatS": 20}

: keep-alive                        <- a comment frame; fires no event

event: change
data: {"generation": 13, "at": 1789913704558, "changedFiles": 1,
       "eventsInserted": 3, "sessions": 10, "spans": 1430,
       "activeMs": 695256036, "cost": 13341.69, "currency": "USD",
       "durationS": 0.155, "errors": []}
```

- `spans`, `activeMs` and `cost` are **corpus totals after the cycle**;
  `eventsInserted` and `sessions` are that cycle's delta.
- `generation` only advances on a cycle that changed something, so a client
  may refetch on every `change` without looping.
- **With no watcher running the path returns `404`.** That is what makes an
  `EventSource` give up instead of reconnecting forever, and it is how a page
  learns there is nothing live to listen to. Treat the 404 as "not watching",
  not as an error worth showing.

## Errors

`400` with `{"error": "..."}` for a malformed filter. `404` with the same shape
for an unknown path. Never return `200` with an error body.

## Fixtures

`frontend/src/fixtures/*.json` holds a real capture of every endpoint, generated
by `scripts/dump_fixtures.py` from a live database. The frontend must render
correctly from these with no server running, so the UI can be built and reviewed
independently of the backend.

One file per endpoint, both ways: a fixture with no endpoint behind it is dead
weight the frontend may still be reading, and an endpoint with no fixture is
one the frontend cannot be built against offline.
`tests/test_metrics.py::test_every_contract_endpoint_is_implemented` enforces
it. `/api/live` has no fixture because it is not an endpoint in this sense.
