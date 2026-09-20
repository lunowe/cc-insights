/**
 * Client-side recomputation of every endpoint from the raw span list.
 *
 * The committed fixtures are *unfiltered* snapshots, so with no server running
 * the only way for a filter to move the numbers is to redo the arithmetic here
 * over `timeline.json`. Each function below was checked against the fixtures:
 * with no filter applied they reproduce `summary.json`, `concurrency.json`,
 * `projects.json`, `daily.json` (activeMs + bySource) and `heatmap.json`
 * field-for-field.
 *
 * When `VITE_API_URL` is set none of this runs — the server does the same work
 * over SQL. See `api.ts`.
 */

import type { Filters } from "./filters"
import type {
  Agents,
  Concurrency,
  Daily,
  GroupRow,
  Groups,
  Heatmap,
  Meta,
  Projects,
  Source,
  Span,
  Summary,
} from "./types"

const SOURCES: Source[] = ["claude_code", "codex", "opencode"]

const durationOf = (s: Span) => s.end - s.start

/* ── the project roster ──────────────────────────────────────────────────────
   A span carries a `projectId` and nothing about groups, so every group-aware
   recomputation needs the project → group mapping alongside it. `/api/projects`
   is where that mapping lives on the wire, so the unfiltered capture of it is
   the index the rest of this file joins against.
   ──────────────────────────────────────────────────────────────────────────── */

export type ProjectInfo = {
  projectId: string
  name: string
  rootPath: string
  groupId: string | null
  groupName: string | null
  groupPinned: boolean
  /** true = on disk · false = gone · null = not probed. Never conflate the last two. */
  pathExists: boolean | null
  /**
   * The path's UNFILTERED list-price equivalent, from `projects.json`. A span
   * list cannot re-price itself, so this is passed through under a filter and
   * flagged by `DashboardData.eventFactsUnfiltered`. 0 for a path known only
   * from `meta`, which carries no cost.
   */
  cost: number
}

export type ProjectIndex = Map<string, ProjectInfo>

/**
 * Build the index from the two unfiltered captures. `projects.json` is
 * authoritative for group membership; `meta.json` fills in any project it
 * somehow lacks, so an id seen on a span is never dropped for want of a name.
 */
export function buildProjectIndex(meta: Meta, base: Projects): ProjectIndex {
  const index: ProjectIndex = new Map()
  for (const p of meta.projects) {
    index.set(p.projectId, {
      projectId: p.projectId,
      name: p.name,
      rootPath: p.rootPath,
      groupId: null,
      groupName: null,
      groupPinned: false,
      // `meta` has no existence flag, so a project known only from `meta` is
      // unprobed, which is exactly what `null` means. Never `false`.
      pathExists: null,
      cost: 0,
    })
  }
  for (const p of base.projects) {
    index.set(p.projectId, {
      projectId: p.projectId,
      name: p.name,
      rootPath: p.rootPath,
      groupId: p.groupId,
      groupName: p.groupName,
      groupPinned: p.groupPinned,
      pathExists: p.pathExists,
      cost: p.cost,
    })
  }
  return index
}

/**
 * Narrow the span list. Matches `docs/API.md`: `from` is an inclusive lower
 * bound and `to` an exclusive upper bound **on span start**, not on overlap.
 *
 * `project` and `group` **union** with each other — `?group=G&project=P` keeps
 * the spans of G *plus* the spans of P, never their intersection. A span that
 * satisfies both is still one span, so a project and one of its own paths
 * select exactly the project. That union then intersects with `source`,
 * `from`, `to` and `role`.
 *
 * A span now carries its own `groupId`, so the mapping is read from the span
 * itself; `index` is only the fallback for a capture taken before that field
 * existed.
 */
export function filterSpans(
  spans: Span[],
  f: Filters,
  index?: ProjectIndex,
): Span[] {
  const projects = f.projects.length > 0 ? new Set(f.projects) : null
  const groups = f.groups.length > 0 ? new Set(f.groups) : null
  const sources = f.sources.length > 0 ? new Set<string>(f.sources) : null

  return spans.filter((s) => {
    if (projects !== null || groups !== null) {
      const inProject =
        projects !== null && s.projectId !== null && projects.has(s.projectId)
      const groupId =
        s.groupId ??
        (s.projectId === null ? null : (index?.get(s.projectId)?.groupId ?? null))
      const inGroup = groups !== null && groupId !== null && groups.has(groupId)
      if (!inProject && !inGroup) return false
    }
    if (sources !== null && !sources.has(s.source)) return false
    if (f.from !== null && s.start < f.from) return false
    if (f.to !== null && s.start >= f.to) return false
    if (f.role === "root" && s.isSubagent) return false
    if (f.role === "subagent" && !s.isSubagent) return false
    return true
  })
}

/**
 * `base` supplies only the fields a span list cannot know: `events`, `tokens`
 * and `cost` are per-event facts that the timeline endpoint does not carry.
 * They are passed through unchanged and must not be presented as filtered —
 * see `DashboardData.eventFactsUnfiltered` in `api.ts`.
 */
export function deriveSummary(spans: Span[], base: Summary): Summary {
  const sessions = new Set<string>()
  const threads = new Set<string>()
  const bySource = new Map<Source, number>()
  let activeMs = 0
  let humanInitiatedMs = 0
  let autonomousMs = 0
  let unattendedRootMs = 0

  for (const s of spans) {
    const d = durationOf(s)
    activeMs += d
    sessions.add(s.sessionId)
    threads.add(s.threadId)
    bySource.set(s.source, (bySource.get(s.source) ?? 0) + d)

    // The three buckets partition activeMs exactly: a span is either subagent
    // work, or root work a human opened, or root work that resumed without one.
    if (s.isSubagent) autonomousMs += d
    else if (s.attended === 1) humanInitiatedMs += d
    else unattendedRootMs += d
  }

  return {
    sessions: sessions.size,
    threads: threads.size,
    events: base.events,
    spans: spans.length,
    activeMs,
    bySource: SOURCES.filter((s) => bySource.has(s)).map((source) => ({
      source,
      activeMs: bySource.get(source) ?? 0,
    })),
    humanInitiatedMs,
    autonomousMs,
    unattendedRootMs,
    tokens: base.tokens,
    cost: base.cost,
  }
}

type Interval = { at: number; delta: number }

export function deriveConcurrency(spans: Span[]): Concurrency {
  const events: Interval[] = []
  let activeMs = 0
  for (const s of spans) {
    if (s.end <= s.start) continue
    activeMs += durationOf(s)
    events.push({ at: s.start, delta: 1 }, { at: s.end, delta: -1 })
  }
  events.sort((a, b) => a.at - b.at || a.delta - b.delta)

  const timeAtLevel: Record<string, number> = {}
  let level = 0
  let prev: number | null = null
  let peak = 0
  let peakAt: number | null = null

  for (const e of events) {
    if (prev !== null && e.at > prev && level > 0) {
      const key = String(level)
      timeAtLevel[key] = (timeAtLevel[key] ?? 0) + (e.at - prev)
    }
    level += e.delta
    if (level > peak) {
      peak = level
      peakAt = e.at
    }
    prev = e.at
  }

  const wallMs = Object.values(timeAtLevel).reduce((a, b) => a + b, 0)
  return {
    timeAtLevel,
    peak,
    peakAt,
    wallMs,
    activeMs,
    multiplier: wallMs > 0 ? Math.round((activeMs / wallMs) * 1e4) / 1e4 : 0,
  }
}

/**
 * `currency` is the unfiltered capture's; `cost` on each row is that path's
 * unfiltered figure from the index, because a span list cannot re-price
 * itself. Under a filter the caller flags it (`eventFactsUnfiltered`) and
 * the UI shows a dash, never the number.
 */
export function deriveProjects(
  spans: Span[],
  index: ProjectIndex,
  currency: string,
): Projects {
  type Acc = {
    activeMs: number
    sessions: Set<string>
    threads: Set<string>
    firstTs: number
    lastTs: number
    name: string | null
  }
  const acc = new Map<string, Acc>()

  for (const s of spans) {
    if (s.projectId === null) continue
    let a = acc.get(s.projectId)
    if (a === undefined) {
      a = {
        activeMs: 0,
        sessions: new Set(),
        threads: new Set(),
        firstTs: s.start,
        lastTs: s.end,
        name: s.projectName,
      }
      acc.set(s.projectId, a)
    }
    a.activeMs += durationOf(s)
    a.sessions.add(s.sessionId)
    a.threads.add(s.threadId)
    if (s.start < a.firstTs) a.firstTs = s.start
    if (s.end > a.lastTs) a.lastTs = s.end
  }

  const projects = [...acc.entries()].map(([projectId, a]) => {
    const known = index.get(projectId)
    return {
      projectId,
      name: known?.name ?? a.name ?? projectId.slice(0, 8),
      rootPath: known?.rootPath ?? "",
      groupId: known?.groupId ?? null,
      groupName: known?.groupName ?? null,
      groupPinned: known?.groupPinned ?? false,
      pathExists: known?.pathExists ?? null,
      activeMs: a.activeMs,
      sessions: a.sessions.size,
      threads: a.threads.size,
      firstTs: a.firstTs,
      lastTs: a.lastTs,
      cost: known?.cost ?? 0,
    }
  })
  projects.sort((a, b) => b.activeMs - a.activeMs)
  return { projects, currency }
}

/**
 * `/api/groups` recomputed from a filtered span list.
 *
 * A ranking, not a roster: a group no surviving span reaches is absent.
 * `ungrouped` is the exact complement — every surviving span whose project has
 * no group, *including* the rare span carrying no project at all — which is
 * what makes `sum(groups) + ungrouped === summary.activeMs` hold under any
 * filter. `base` supplies the per-group metadata (origin, forge, web URL) that
 * a span list cannot know.
 */
export function deriveGroups(
  spans: Span[],
  index: ProjectIndex,
  base: Groups,
): Groups {
  type Acc = {
    name: string
    activeMs: number
    sessions: Set<string>
    threads: Set<string>
    projects: Set<string>
    pinned: Set<string>
    firstTs: number
    lastTs: number
  }
  const acc = new Map<string, Acc>()
  const ungroupedProjects = new Set<string>()
  let ungroupedMs = 0

  for (const s of spans) {
    const d = durationOf(s)
    const info = s.projectId === null ? undefined : index.get(s.projectId)
    const groupId = s.groupId ?? info?.groupId ?? null

    if (groupId === null) {
      // A span with no project at all still belongs to nothing, so its time
      // lands here; it just cannot raise the ungrouped *project* count.
      ungroupedMs += d
      if (s.projectId !== null) ungroupedProjects.add(s.projectId)
      continue
    }

    let a = acc.get(groupId)
    if (a === undefined) {
      a = {
        name: s.groupName ?? info?.groupName ?? groupId.slice(0, 8),
        activeMs: 0,
        sessions: new Set(),
        threads: new Set(),
        projects: new Set(),
        pinned: new Set(),
        firstTs: s.start,
        lastTs: s.end,
      }
      acc.set(groupId, a)
    }
    a.activeMs += d
    a.sessions.add(s.sessionId)
    a.threads.add(s.threadId)
    if (s.projectId !== null) {
      a.projects.add(s.projectId)
      if (info?.groupPinned === true) a.pinned.add(s.projectId)
    }
    if (s.start < a.firstTs) a.firstTs = s.start
    if (s.end > a.lastTs) a.lastTs = s.end
  }

  const meta = new Map(base.groups.map((g) => [g.groupId, g]))
  const groups: GroupRow[] = [...acc.entries()].map(([groupId, a]) => {
    const m = meta.get(groupId)
    return {
      groupId,
      name: m?.name ?? a.name,
      origin: m?.origin ?? "manual",
      forge: m?.forge ?? null,
      owner: m?.owner ?? null,
      repo: m?.repo ?? null,
      webUrl: m?.webUrl ?? null,
      activeMs: a.activeMs,
      sessions: a.sessions.size,
      threads: a.threads.size,
      projects: a.projects.size,
      pinnedProjects: a.pinned.size,
      firstTs: a.firstTs,
      lastTs: a.lastTs,
    }
  })
  groups.sort((a, b) => b.activeMs - a.activeMs)

  return {
    groups,
    ungrouped: { projects: ungroupedProjects.size, activeMs: ungroupedMs },
  }
}

export function deriveAgents(spans: Span[]): Agents {
  const acc = new Map<
    string,
    { agentName: string; source: Source; threads: Set<string>; activeMs: number }
  >()
  for (const s of spans) {
    if (s.agentName === null) continue
    const key = `${s.source}\u0000${s.agentName}`
    let a = acc.get(key)
    if (a === undefined) {
      a = { agentName: s.agentName, source: s.source, threads: new Set(), activeMs: 0 }
      acc.set(key, a)
    }
    a.threads.add(s.threadId)
    a.activeMs += durationOf(s)
  }
  const agents = [...acc.values()].map((a) => ({
    agentName: a.agentName,
    source: a.source,
    threads: a.threads.size,
    activeMs: a.activeMs,
  }))
  agents.sort((a, b) => b.activeMs - a.activeMs)
  return { agents }
}

/**
 * Codex names each thread after a random scientist or philosopher; Claude Code
 * and opencode record a real agent *type* ("Explore", "general"). Listing
 * "Aristotle" beside "general-purpose" would imply a taxonomy that does not
 * exist, so every Codex row collapses into one. Exported for whoever renders
 * the agent breakdown.
 */
export function groupAgentsForDisplay(agents: Agents["agents"]) {
  const typed = agents.filter((a) => a.source !== "codex")
  const codex = agents.filter((a) => a.source === "codex")
  const rows = typed.map((a) => ({
    label: a.agentName,
    source: a.source,
    threads: a.threads,
    activeMs: a.activeMs,
    isNicknameGroup: false,
  }))
  if (codex.length > 0) {
    rows.push({
      label: "Codex",
      source: "codex" as const,
      threads: codex.reduce((n, a) => n + a.threads, 0),
      activeMs: codex.reduce((n, a) => n + a.activeMs, 0),
      isNicknameGroup: true,
    })
  }
  rows.sort((a, b) => b.activeMs - a.activeMs)
  return rows
}

/* ── local calendar bucketing ────────────────────────────────────────────────
   A span that crosses midnight belongs partly to each day. Boundaries are
   computed with the local-component `Date` constructor, which is DST-correct:
   on a spring-forward day the 02:00 boundary normalises to 03:00, which is
   exactly the wall-clock behaviour we want.
   ──────────────────────────────────────────────────────────────────────────── */

function nextLocalDay(ms: number): number {
  const d = new Date(ms)
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() + 1).getTime()
}

function nextLocalHour(ms: number): number {
  const d = new Date(ms)
  return new Date(
    d.getFullYear(),
    d.getMonth(),
    d.getDate(),
    d.getHours() + 1,
  ).getTime()
}

function localDateKey(ms: number): string {
  const d = new Date(ms)
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, "0")
  const day = String(d.getDate()).padStart(2, "0")
  return `${y}-${m}-${day}`
}

/** JS getDay(): 0 = Sunday. The contract says weekday 0 = Monday. */
function mondayFirstWeekday(d: Date): number {
  return (d.getDay() + 6) % 7
}

function eachPiece(
  span: Span,
  next: (ms: number) => number,
  visit: (from: number, to: number) => void,
) {
  let cur = span.start
  while (cur < span.end) {
    const boundary = next(cur)
    const to = Math.min(span.end, boundary > cur ? boundary : span.end)
    visit(cur, to)
    cur = to
  }
}

/**
 * `base` is the unfiltered daily capture: it supplies `currency` and each
 * day's `cost`, which a span list cannot recompute. Those pass through
 * unfiltered, flagged by `eventFactsUnfiltered`, and are never rendered under
 * a filter in fixtures mode.
 */
export function deriveDaily(spans: Span[], f: Filters, base: Daily): Daily {
  const baseCost = new Map(base.days.map((d) => [d.date, d.cost]))
  const active = new Map<string, number>()
  const bySource = new Map<string, Partial<Record<Source, number>>>()
  // Per-day wall clock: union of the day's clipped pieces, so two threads
  // running side by side count once.
  const pieces = new Map<string, Interval[]>()

  let min = Number.POSITIVE_INFINITY
  let max = Number.NEGATIVE_INFINITY

  for (const s of spans) {
    if (s.start < min) min = s.start
    if (s.end > max) max = s.end
    eachPiece(s, nextLocalDay, (from, to) => {
      const key = localDateKey(from)
      const d = to - from
      active.set(key, (active.get(key) ?? 0) + d)
      const bs = bySource.get(key) ?? {}
      bs[s.source] = (bs[s.source] ?? 0) + d
      bySource.set(key, bs)
      const list = pieces.get(key)
      const evs: Interval[] = [
        { at: from, delta: 1 },
        { at: to, delta: -1 },
      ]
      if (list === undefined) pieces.set(key, evs)
      else list.push(...evs)
    })
  }

  const days: Daily["days"] = []
  if (!Number.isFinite(min)) return { days, currency: base.currency }

  // "gaps filled with 0" across the requested range, or the observed range.
  const start = f.from !== null ? Math.min(f.from, min) : min
  const end = f.to !== null ? Math.max(f.to, max) : max

  let cursor = new Date(start)
  cursor = new Date(cursor.getFullYear(), cursor.getMonth(), cursor.getDate())
  let guard = 0
  while (cursor.getTime() <= end && guard++ < 20000) {
    const key = localDateKey(cursor.getTime())
    days.push({
      date: key,
      activeMs: active.get(key) ?? 0,
      wallMs: unionMs(pieces.get(key)),
      bySource: bySource.get(key) ?? {},
      cost: baseCost.get(key) ?? 0,
    })
    cursor = new Date(nextLocalDay(cursor.getTime()))
  }
  return { days, currency: base.currency }
}

function unionMs(events: Interval[] | undefined): number {
  if (events === undefined || events.length === 0) return 0
  const sorted = [...events].sort((a, b) => a.at - b.at || a.delta - b.delta)
  let level = 0
  let prev: number | null = null
  let total = 0
  for (const e of sorted) {
    if (prev !== null && e.at > prev && level > 0) total += e.at - prev
    level += e.delta
    prev = e.at
  }
  return total
}

export function deriveHeatmap(spans: Span[]): Heatmap {
  const acc = new Map<string, number>()
  for (const s of spans) {
    eachPiece(s, nextLocalHour, (from, to) => {
      const d = new Date(from)
      const key = `${mondayFirstWeekday(d)}:${d.getHours()}`
      acc.set(key, (acc.get(key) ?? 0) + (to - from))
    })
  }
  const cells = [...acc.entries()].map(([key, activeMs]) => {
    const [weekday, hour] = key.split(":").map(Number)
    return { weekday, hour, activeMs }
  })
  cells.sort((a, b) => a.weekday - b.weekday || a.hour - b.hour)
  return { cells }
}
