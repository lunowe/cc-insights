/**
 * Mirrors `docs/API.md` — the frozen wire contract.
 *
 * Two conventions hold everywhere and are never relaxed:
 *   - every `*Ms` field is a **duration in milliseconds**
 *   - every `*Ts` / `start` / `end` / `*At` field is an **epoch millisecond
 *     timestamp in UTC**. Convert to local time only at the point of display.
 */

export type Source = "claude_code" | "codex"
export type Role = "all" | "root" | "subagent"

/** GET /api/meta — populates the filter controls. Not affected by filters. */
export type Meta = {
  hostname: string
  firstTs: number | null
  lastTs: number | null
  sources: Source[]
  projects: {
    projectId: string
    name: string
    rootPath: string
    activeMs: number
  }[]
  agents: { agentName: string; source: Source }[]
  models: string[]
  /** Gap above which a span breaks. Every duration in this app depends on it. */
  idleThresholdS: number
  generatedAt: number
}

/** GET /api/summary */
export type Summary = {
  sessions: number
  threads: number
  events: number
  spans: number
  activeMs: number
  bySource: { source: Source; activeMs: number }[]
  /** humanInitiated + autonomous + unattendedRoot === activeMs, exactly. */
  humanInitiatedMs: number
  /** Subagent threads: a model spawned them. Structural, not a claim about
   *  whether a human was watching. */
  autonomousMs: number
  unattendedRootMs: number
  tokens: {
    input: number
    output: number
    cacheRead: number
    cacheWrite: number
  }
}

export type Span = {
  spanId: string
  threadId: string
  sessionId: string
  projectId: string | null
  projectName: string | null
  source: Source
  /** Claude Code records an agent *type*; Codex records a random per-thread
   *  nickname. Never present a Codex nickname as an agent type. */
  agentName: string | null
  isSubagent: boolean
  parentThreadId: string | null
  attended: 0 | 1 | null
  start: number
  end: number
}

/** GET /api/timeline — the swimlane. One row per span. */
export type Timeline = {
  spans: Span[]
  /** True when the server capped the result; tell the user, never drop silently. */
  truncated: boolean
  limit: number
}

/** GET /api/daily — one row per local calendar day in range, gaps filled with 0.
 *
 *  NOTE: `API.md` types `bySource` as `Record<Source, number>`, but the
 *  committed fixtures omit sources with no activity on a day (a zero-activity
 *  day carries `{}`). Typed as partial so consumers are forced to handle the
 *  gap; see `bySourceMs()` in `format.ts`. Flagged to the contract owner. */
export type Daily = {
  days: {
    date: string
    activeMs: number
    wallMs: number
    bySource: Partial<Record<Source, number>>
  }[]
}

/** GET /api/concurrency — sweep-line over the filtered spans. */
export type Concurrency = {
  /** "1" -> ms spent with exactly 1 thread active, "2" -> ms, ... */
  timeAtLevel: Record<string, number>
  peak: number
  peakAt: number | null
  /** Wall-clock time with at least one thread active. */
  wallMs: number
  /** Sum of span durations. */
  activeMs: number
  /** activeMs / wallMs. */
  multiplier: number
}

/** GET /api/projects */
export type Projects = {
  projects: {
    projectId: string
    name: string
    rootPath: string
    activeMs: number
    sessions: number
    threads: number
    firstTs: number
    lastTs: number
  }[]
}

export type ProjectRow = Projects["projects"][number]

/** GET /api/agents */
export type Agents = {
  agents: {
    agentName: string
    source: Source
    threads: number
    activeMs: number
  }[]
}

/** GET /api/heatmap — local weekday x hour. weekday 0 = Monday. */
export type Heatmap = {
  cells: { weekday: number; hour: number; activeMs: number }[]
}
