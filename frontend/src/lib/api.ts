/**
 * The data layer, and the single switch this app turns on.
 *
 *   VITE_API_URL unset  →  read the committed fixtures and recompute every
 *                          filtered figure in the browser (see `derive.ts`).
 *   VITE_API_URL set    →  pass the same filters to `cci serve` as a query
 *                          string and let the backend do it.
 *
 * Everything downstream — tiles, tables, and WP10's charts — calls
 * `fetchDashboard` and never learns which half it got. Adding the server is a
 * one-line environment change, not a refactor.
 */

import {
  buildProjectIndex,
  deriveAgents,
  deriveConcurrency,
  deriveDaily,
  deriveGroups,
  deriveHeatmap,
  deriveProjects,
  deriveSummary,
  filterSpans,
  type ProjectIndex,
} from "./derive"
import { EMPTY_FILTERS, isUnfiltered, toSearchParams, type Filters } from "./filters"
import type {
  Agents,
  Concurrency,
  Daily,
  Groups,
  Heatmap,
  Meta,
  Projects,
  Summary,
  Timeline,
} from "./types"

const RAW_API_URL = import.meta.env.VITE_API_URL
const API_URL =
  typeof RAW_API_URL === "string" && RAW_API_URL.length > 0
    ? RAW_API_URL.replace(/\/+$/, "")
    : null

export type DataMode = "live" | "fixtures"

export const DATA_MODE: DataMode = API_URL === null ? "fixtures" : "live"

export class ApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = "ApiError"
    this.status = status
  }
}

/* ── live ────────────────────────────────────────────────────────────────── */

async function request<T>(
  path: string,
  filters: Filters,
  signal?: AbortSignal,
): Promise<T> {
  const query = toSearchParams(filters).toString()
  const url = `${API_URL}${path}${query === "" ? "" : `?${query}`}`
  const res = await fetch(url, { signal, headers: { Accept: "application/json" } })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body: unknown = await res.json()
      if (
        typeof body === "object" &&
        body !== null &&
        "error" in body &&
        typeof (body as { error: unknown }).error === "string"
      ) {
        detail = (body as { error: string }).error
      }
    } catch {
      /* non-JSON error body; keep the status text */
    }
    throw new ApiError(detail, res.status)
  }
  return (await res.json()) as T
}

/* ── fixtures ────────────────────────────────────────────────────────────── */

/** Loaded on demand so the 590 kB span capture is its own chunk, not boot cost. */
const fixture = {
  meta: () => import("../fixtures/meta.json").then((m) => m.default as unknown as Meta),
  summary: () =>
    import("../fixtures/summary.json").then((m) => m.default as unknown as Summary),
  timeline: () =>
    import("../fixtures/timeline.json").then((m) => m.default as unknown as Timeline),
  projects: () =>
    import("../fixtures/projects.json").then((m) => m.default as unknown as Projects),
  concurrency: () =>
    import("../fixtures/concurrency.json").then(
      (m) => m.default as unknown as Concurrency,
    ),
  daily: () => import("../fixtures/daily.json").then((m) => m.default as unknown as Daily),
  agents: () =>
    import("../fixtures/agents.json").then((m) => m.default as unknown as Agents),
  heatmap: () =>
    import("../fixtures/heatmap.json").then((m) => m.default as unknown as Heatmap),
  groups: () =>
    import("../fixtures/groups.json").then((m) => m.default as unknown as Groups),
}

/**
 * Spans carry a `projectId` and nothing about groups, so the fixtures path has
 * to join them against the unfiltered `/api/projects` capture before a group
 * filter can mean anything. Memoised: it is the same map on every keystroke.
 */
let indexPromise: Promise<ProjectIndex> | null = null

function projectIndex(): Promise<ProjectIndex> {
  indexPromise ??= Promise.all([fixture.meta(), fixture.projects()]).then(
    ([meta, base]) => buildProjectIndex(meta, base),
  )
  return indexPromise
}

/* ── the eight endpoints ─────────────────────────────────────────────────── */

/** GET /api/meta. Never filtered — it is what populates the filter controls. */
export async function getMeta(signal?: AbortSignal): Promise<Meta> {
  if (API_URL === null) return fixture.meta()
  return request<Meta>("/api/meta", EMPTY_FILTERS, signal)
}

export async function getSummary(f: Filters, signal?: AbortSignal): Promise<Summary> {
  if (API_URL === null) {
    const base = await fixture.summary()
    if (isUnfiltered(f)) return base
    const [{ spans }, index] = await Promise.all([fixture.timeline(), projectIndex()])
    return deriveSummary(filterSpans(spans, f, index), base)
  }
  return request<Summary>("/api/summary", f, signal)
}

export async function getTimeline(f: Filters, signal?: AbortSignal): Promise<Timeline> {
  if (API_URL === null) {
    const tl = await fixture.timeline()
    if (isUnfiltered(f)) return tl
    const index = await projectIndex()
    return { ...tl, spans: filterSpans(tl.spans, f, index) }
  }
  return request<Timeline>("/api/timeline", f, signal)
}

export async function getProjects(f: Filters, signal?: AbortSignal): Promise<Projects> {
  if (API_URL === null) {
    if (isUnfiltered(f)) return fixture.projects()
    const [{ spans }, index] = await Promise.all([fixture.timeline(), projectIndex()])
    return deriveProjects(filterSpans(spans, f, index), index)
  }
  return request<Projects>("/api/projects", f, signal)
}

export async function getConcurrency(
  f: Filters,
  signal?: AbortSignal,
): Promise<Concurrency> {
  if (API_URL === null) {
    if (isUnfiltered(f)) return fixture.concurrency()
    const [{ spans }, index] = await Promise.all([fixture.timeline(), projectIndex()])
    return deriveConcurrency(filterSpans(spans, f, index))
  }
  return request<Concurrency>("/api/concurrency", f, signal)
}

export async function getDaily(f: Filters, signal?: AbortSignal): Promise<Daily> {
  if (API_URL === null) {
    if (isUnfiltered(f)) return fixture.daily()
    const [{ spans }, index] = await Promise.all([fixture.timeline(), projectIndex()])
    return deriveDaily(filterSpans(spans, f, index), f)
  }
  return request<Daily>("/api/daily", f, signal)
}

export async function getAgents(f: Filters, signal?: AbortSignal): Promise<Agents> {
  if (API_URL === null) {
    if (isUnfiltered(f)) return fixture.agents()
    const [{ spans }, index] = await Promise.all([fixture.timeline(), projectIndex()])
    return deriveAgents(filterSpans(spans, f, index))
  }
  return request<Agents>("/api/agents", f, signal)
}

export async function getHeatmap(f: Filters, signal?: AbortSignal): Promise<Heatmap> {
  if (API_URL === null) {
    if (isUnfiltered(f)) return fixture.heatmap()
    const [{ spans }, index] = await Promise.all([fixture.timeline(), projectIndex()])
    return deriveHeatmap(filterSpans(spans, f, index))
  }
  return request<Heatmap>("/api/heatmap", f, signal)
}

export async function getGroups(f: Filters, signal?: AbortSignal): Promise<Groups> {
  if (API_URL === null) {
    const base = await fixture.groups()
    if (isUnfiltered(f)) return base
    const [{ spans }, index] = await Promise.all([fixture.timeline(), projectIndex()])
    return deriveGroups(filterSpans(spans, f, index), index, base)
  }
  return request<Groups>("/api/groups", f, signal)
}

/* ── one call for the whole page ─────────────────────────────────────────── */

export type DashboardData = {
  meta: Meta
  summary: Summary
  projects: Projects
  /** One row per logical project. `sum(groups) + ungrouped === summary.activeMs`. */
  groups: Groups
  concurrency: Concurrency
  timeline: Timeline
  daily: Daily
  agents: Agents
  heatmap: Heatmap
  mode: DataMode
  /** True when the view is narrowed. */
  filtered: boolean
  /**
   * In fixtures mode `summary.events` and `summary.tokens` are per-event facts
   * the timeline capture does not carry, so under a filter they stay at their
   * unfiltered values. Do not render them as filtered numbers.
   */
  tokensAreUnfiltered: boolean
}

export async function fetchDashboard(
  f: Filters,
  signal?: AbortSignal,
): Promise<DashboardData> {
  const filtered = !isUnfiltered(f)

  if (API_URL !== null) {
    const [
      meta,
      summary,
      projects,
      groups,
      concurrency,
      timeline,
      daily,
      agents,
      heatmap,
    ] = await Promise.all([
      getMeta(signal),
      getSummary(f, signal),
      getProjects(f, signal),
      getGroups(f, signal),
      getConcurrency(f, signal),
      getTimeline(f, signal),
      getDaily(f, signal),
      getAgents(f, signal),
      getHeatmap(f, signal),
    ])
    return {
      meta,
      summary,
      projects,
      groups,
      concurrency,
      timeline,
      daily,
      agents,
      heatmap,
      mode: "live",
      filtered,
      tokensAreUnfiltered: false,
    }
  }

  // Fixtures: load once, filter once, derive the rest from the same span list.
  const [meta, baseSummary, baseTimeline, baseGroups, index] = await Promise.all([
    fixture.meta(),
    fixture.summary(),
    fixture.timeline(),
    fixture.groups(),
    projectIndex(),
  ])

  if (!filtered) {
    const [projects, concurrency, daily, agents, heatmap] = await Promise.all([
      fixture.projects(),
      fixture.concurrency(),
      fixture.daily(),
      fixture.agents(),
      fixture.heatmap(),
    ])
    return {
      meta,
      summary: baseSummary,
      projects,
      groups: baseGroups,
      concurrency,
      timeline: baseTimeline,
      daily,
      agents,
      heatmap,
      mode: "fixtures",
      filtered: false,
      tokensAreUnfiltered: false,
    }
  }

  const spans = filterSpans(baseTimeline.spans, f, index)
  return {
    meta,
    summary: deriveSummary(spans, baseSummary),
    projects: deriveProjects(spans, index),
    groups: deriveGroups(spans, index, baseGroups),
    concurrency: deriveConcurrency(spans),
    timeline: { ...baseTimeline, spans },
    daily: deriveDaily(spans, f),
    agents: deriveAgents(spans),
    heatmap: deriveHeatmap(spans),
    mode: "fixtures",
    filtered: true,
    tokensAreUnfiltered: true,
  }
}
