/**
 * Mirrors `docs/API.md` — the frozen wire contract.
 *
 * Two conventions hold everywhere and are never relaxed:
 *   - every `*Ms` field is a **duration in milliseconds**
 *   - every `*Ts` / `start` / `end` / `*At` field is an **epoch millisecond
 *     timestamp in UTC**. Convert to local time only at the point of display.
 */

export type Source = "claude_code" | "codex" | "opencode"
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
  /**
   * Where the money came from, so any surface can footnote its own totals
   * without a second round trip. `approximations` are models the price
   * catalog matched to a NEAR RELATIVE rather than to themselves: defensible
   * as a default, never acceptable to hide. `unpricedModels` have no rate at
   * all — their tokens are unknown, not free.
   */
  pricing: {
    catalog: PriceCatalog
    currency: string
    approximations: Approximation[]
    unpricedModels: string[]
  }
  generatedAt: number
}

/** Provenance of the rates. Every field optional: a hand-written catalog has none. */
export type PriceCatalog = {
  repo?: string
  commit?: string
  fetched_at?: string
  license?: string
}

/** A model priced at a relative's rates because it has none of its own. */
export type Approximation = { model: string; pricedAs: string }

/**
 * Shared by `/api/summary.cost` and `/api/cost`.
 *
 * A **list-price equivalent**, not a bill: what the filtered traffic would
 * have cost at published API rates. A Claude Max or ChatGPT Plus subscription
 * charges a flat fee however many tokens run through it, and opencode reports
 * 0 for every call. Useful for comparing projects, models and months; wrong
 * in an invoice. Every renderer labels it as such and shows `unpricedTokens`
 * beside it.
 */
export type CostTotals = {
  /** In `currency` units. */
  total: number
  /** "USD", or "mixed" if the rates that met disagree. Never a symbol. */
  currency: string
  /**
   * `cacheWrite` is the FIVE-MINUTE rate and `cacheWrite1h` the one-hour
   * one: the same tokens cost 1.25x and 2x base input, and which applies is
   * recorded per request rather than assumed.
   */
  byComponent: {
    input: number
    output: number
    cacheRead: number
    cacheWrite: number
    cacheWrite1h: number
  }
  pricedEvents: number
  /**
   * Priced off a model carried forward from an earlier event in the same
   * thread, because Codex records usage on events that name no model.
   */
  attributedEvents: number
  /**
   * Tokens inside the filtered spans that no rate covered. NOT zero-cost:
   * unknown. Shown wherever `total` is shown.
   */
  unpricedTokens: number
  /**
   * Cache-write tokens whose source never recorded a TTL, priced at the
   * cheaper five-minute rate. Makes `total` a FLOOR for those tokens rather
   * than a midpoint. 0 once `cci backfill` has filled what the logs hold.
   */
  assumed5mTokens: number
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
  /**
   * `cacheWrite` is ALL cache writes; `cacheWrite1h` is the part that bought
   * a one-hour TTL, at 2x base input instead of 1.25x. The five-minute part
   * is the difference.
   */
  tokens: {
    input: number
    output: number
    cacheRead: number
    cacheWrite: number
    cacheWrite1h: number
  }
  cost: CostTotals
}

export type Span = {
  spanId: string
  threadId: string
  sessionId: string
  /** The on-disk path. In UI copy this is a **path**, never a project. */
  projectId: string | null
  projectName: string | null
  /**
   * The **project** this span belongs to (`project_group` in the schema).
   * A swimlane lane is labelled and coloured by this, never by `projectName`:
   * the path is called `tenant-restricted` while the project is atlas-chat,
   * and labelling by path splits one project into a dozen differently-named,
   * differently-coloured lanes. Null when the path has no project yet.
   */
  groupId: string | null
  groupName: string | null
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
    /** List-price equivalent for the day, in `currency`. */
    cost: number
  }[]
  currency: string
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
 *  `pathExists` is deliberately tri-state and the three states are NOT
 *  interchangeable:
 *
 *    true   — the directory is on disk.
 *    false  — it is gone: a finished worktree, a deleted checkout. Its hours
 *             were really worked and must still be counted; they are history.
 *    null   — detection has not probed this path yet. **Not** the same as
 *             gone, and never to be rendered as such.
 *
 *  Path shape is not a substitute for this field: inferring existence from the
 *  worktree shapes in `docs/GROUPING.md` mislabels 5 live worktrees out of 15
 *  on the author's corpus. See `isWorktreePath()`, which answers a different
 *  question and may be true of a live path and a dead one alike. */
export type Projects = {
  projects: {
    projectId: string
    name: string
    rootPath: string
    groupId: string | null
    groupName: string | null
    groupPinned: boolean
    /** true = on disk · false = gone · null = not probed yet. See above. */
    pathExists: boolean | null
    activeMs: number
    sessions: number
    threads: number
    firstTs: number
    lastTs: number
    /** List-price equivalent, in `currency`. Not a bill. */
    cost: number
  }[]
  currency: string
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

/** Why tokens went unpriced. Stored by the backend, never inferred here. */
export type UnpricedReason = "no_rate" | "no_model" | "no_component"

/**
 * GET /api/cost — the list-price equivalent, broken down and qualified.
 *
 * NOT A BILL (see `CostTotals`). Every caveat is machine-readable so the UI
 * shows it rather than paraphrasing it.
 */
export type Cost = CostTotals & {
  byModel: { model: string; cost: number; events: number; attributed: number }[]
  bySource: { source: Source; cost: number }[]
  /** Local calendar days, no gap fill. */
  daily: { date: string; cost: number }[]
  /**
   * What could not be priced, and why:
   *   no_rate       the model has no rate on file at that date
   *   no_model      nothing in the thread said which model ran
   *   no_component  the model is priced, but not for this token component
   *                 (OpenAI publishes no cache-write rate)
   */
  unpriced: {
    model: string | null
    reason: UnpricedReason
    tokens: number
    events: number
  }[]
  /**
   * Models the catalog priced as a near relative. On the author's corpus
   * `claude-fable-5-1` is priced as `claude-fable-5`, whose cache reads cost
   * four times as much — thousands of dollars of difference. Shown next to
   * the total, never buried.
   */
  approximations: Approximation[]
  catalog: PriceCatalog
}
