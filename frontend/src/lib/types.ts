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
  /**
   * The group filter's roster — a group with no time yet is still a choice, at
   * 0 ms. `[]` until `cci group auto` has run, which is the normal first state
   * and not an error. See `docs/GROUPING.md`.
   */
  groups: { groupId: string; name: string; activeMs: number }[]
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

/** GET /api/projects — one row per on-disk path.
 *
 *  A project row is one path; the *logical* project it belongs to is the group
 *  (`docs/GROUPING.md`). `groupId`/`groupName` are null for an ungrouped
 *  project, which is legal, not an error. `groupPinned` means a human placed
 *  this project in that group, so detection must never move it.
 *
 *  NOTE: the wire carries no "does this path still exist on disk" flag. The DB
 *  has `project.path_exists`, but neither `API.md` nor `/api/projects` exposes
 *  it, so the UI cannot honestly mark a path as gone. See `isWorktreePath()`. */
export type Projects = {
  projects: {
    projectId: string
    name: string
    rootPath: string
    groupId: string | null
    groupName: string | null
    groupPinned: boolean
    activeMs: number
    sessions: number
    threads: number
    firstTs: number
    lastTs: number
  }[]
}

export type ProjectRow = Projects["projects"][number]

/** How a group was detected. Strongest evidence first; `manual` is a human. */
export type GroupOrigin =
  | "git_remote"
  | "git_common_dir"
  | "path_worktree"
  | "path_ancestor"
  | "manual"

/** GET /api/groups — one row per LOGICAL project.
 *
 *  A ranking, not a roster: a group no surviving span reaches is absent rather
 *  than present with zeros (`meta.groups` is the roster). `ungrouped` is the
 *  exact complement, so under ANY filter:
 *
 *      sum(groups[].activeMs) + ungrouped.activeMs === summary.activeMs
 */
export type Groups = {
  groups: {
    groupId: string
    name: string
    origin: GroupOrigin
    forge: string | null
    owner: string | null
    repo: string | null
    webUrl: string | null
    activeMs: number
    sessions: number
    threads: number
    /** Members the surviving spans reach — not membership on paper. */
    projects: number
    pinnedProjects: number
    firstTs: number
    lastTs: number
  }[]
  ungrouped: { projects: number; activeMs: number }
}

export type GroupRow = Groups["groups"][number]

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
