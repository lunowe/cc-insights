# CC-Insights local API — frozen contract

`cci serve` starts a read-only HTTP server on `127.0.0.1:8787` that serves this
JSON API and the built frontend. Backend and frontend are built in parallel
against this document; **it is frozen** — if it needs to change, say so rather
than diverging.

- All times on the wire are **epoch milliseconds UTC**. The frontend converts to
  local for display; the backend never guesses a timezone.
- All durations are **milliseconds**, named `*Ms`.
- Every endpoint accepts the same `Filters` query string.
- Read-only. No POST, no auth, localhost only.

## Filters (query string, all optional)

| param | type | meaning |
| --- | --- | --- |
| `project` | repeated | `project_id`; repeat to include several. Omitted = all |
| `source` | repeated | `claude_code` \| `codex`. Omitted = all |
| `from` | epoch ms | inclusive lower bound on span start |
| `to` | epoch ms | exclusive upper bound on span start |
| `role` | string | `all` (default) \| `root` \| `subagent` |

`GET /api/summary?project=abc&project=def&source=codex&role=root`

A filter narrows **spans**; counts of sessions/threads are counts of those
reachable from the surviving spans.

## Types

```ts
type Source = "claude_code" | "codex";
type Role = "all" | "root" | "subagent";

// GET /api/meta  — everything needed to populate the filter controls.
// Not affected by filters.
type Meta = {
  hostname: string;
  firstTs: number | null;
  lastTs: number | null;
  sources: Source[];
  projects: { projectId: string; name: string; rootPath: string; activeMs: number }[];
  agents: { agentName: string; source: Source }[];
  models: string[];
  idleThresholdS: number;   // must be shown wherever a duration is exported
  generatedAt: number;
};

// GET /api/summary
type Summary = {
  sessions: number; threads: number; events: number; spans: number;
  activeMs: number;
  bySource: { source: Source; activeMs: number }[];
  // These three partition activeMs exactly.
  humanInitiatedMs: number;   // root thread, a person typed the opening turn
  autonomousMs: number;       // subagent thread: a model spawned it
  unattendedRootMs: number;   // root thread that resumed with no human turn
  tokens: { input: number; output: number; cacheRead: number; cacheWrite: number };
};

// GET /api/timeline — the swimlane. One row per span.
// Cap: if the range yields > `limit` spans the server returns the widest
// `limit` spans and sets `truncated`, so the UI can tell the user rather than
// silently dropping work.
type Timeline = {
  spans: {
    spanId: string; threadId: string; sessionId: string;
    projectId: string | null; projectName: string | null;
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
  days: { date: string; activeMs: number; wallMs: number;
          bySource: Record<Source, number> }[];
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

// GET /api/projects
type Projects = {
  projects: { projectId: string; name: string; rootPath: string;
              activeMs: number; sessions: number; threads: number;
              firstTs: number; lastTs: number }[];
};

// GET /api/agents  — Claude records an agent TYPE; Codex records a random
// per-thread nickname, so the frontend groups Codex under one row.
type Agents = {
  agents: { agentName: string; source: Source; threads: number; activeMs: number }[];
};

// GET /api/heatmap — local weekday x hour. weekday 0 = Monday.
type Heatmap = { cells: { weekday: number; hour: number; activeMs: number }[] };
```

## Errors

`400` with `{"error": "..."}` for a malformed filter. `404` with the same shape
for an unknown path. Never return `200` with an error body.

## Fixtures

`frontend/src/fixtures/*.json` holds a real capture of every endpoint, generated
by `scripts/dump_fixtures.py` from a live database. The frontend must render
correctly from these with no server running, so the UI can be built and reviewed
independently of the backend.
