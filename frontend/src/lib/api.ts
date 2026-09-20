/**
 * The data layer, and the single switch this app turns on.
 *
 * The switch is thrown at **runtime**, not at build time, because one bundle
 * has to serve three situations and only the browser knows which it is in:
 *
 *   1. `VITE_API_URL` set        →  that server. The dev-server-with-proxy
 *                                   case, and an explicit override.
 *   2. same-origin `/api/meta`
 *      answers with JSON         →  same-origin `/api`. This is `cci serve`,
 *                                   which ships this bundle next to the real
 *                                   database. Same origin, so no CORS.
 *   3. neither                   →  the committed fixtures, recomputed in the
 *                                   browser (see `derive.ts`). `dist/index.html`
 *                                   opened straight off the filesystem still
 *                                   renders, with no server anywhere.
 *
 * Deciding this at build time was the bug: `cci serve` shipped a bundle that
 * could only ever show the snapshot committed in the repo, and it looked
 * plausible, which is what made it dangerous.
 *
 * The probe runs **once**, is cached, and is bounded by `PROBE_TIMEOUT_MS`. A
 * server that hangs costs one short timeout and then falls back to fixtures;
 * it never blocks first paint indefinitely.
 *
 * Everything downstream — tiles, tables, charts — calls `fetchDashboard` and
 * never learns which half it got.
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
  Cost,
  Daily,
  Groups,
  Heatmap,
  Meta,
  ProjectRow,
  Projects,
  Summary,
  Timeline,
} from "./types"

const RAW_API_URL = import.meta.env.VITE_API_URL
const CONFIGURED_URL =
  typeof RAW_API_URL === "string" && RAW_API_URL.length > 0
    ? RAW_API_URL.replace(/\/+$/, "")
    : null

export type DataMode = "live" | "fixtures"

/**
 * Long enough for a local server that is busy, short enough that nobody sits
 * in front of a blank page wondering. `cci serve` answers in single-digit ms.
 */
const PROBE_TIMEOUT_MS = 1500

/** Resolved once per page load. `null` means "no backend — use fixtures". */
let backendPromise: Promise<string | null> | null = null

/**
 * The probe has to fetch `/api/meta` anyway, so its answer is kept for the
 * first `getMeta` and then dropped. Consume-once, never a cache: `meta` moves
 * whenever the database is re-ingested and must not go stale behind the page.
 */
let probedMeta: Meta | null = null

async function probeSameOrigin(): Promise<string | null> {
  if (typeof window === "undefined") return null

  // `file://` has an opaque origin: there is nothing to probe, and trying only
  // buys a console error and a wasted timeout.
  const { protocol, origin } = window.location
  if (protocol !== "http:" && protocol !== "https:") return null

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), PROBE_TIMEOUT_MS)
  try {
    const res = await fetch(`${origin}/api/meta`, {
      signal: controller.signal,
      headers: { Accept: "application/json" },
    })
    if (!res.ok) return null
    // A dev server with no proxy answers every unknown path with index.html
    // and a 200, so status alone proves nothing. It has to parse as the JSON
    // we asked for before we believe there is an API behind it.
    if (!(res.headers.get("content-type") ?? "").includes("json")) return null
    const meta = (await res.json()) as Meta
    if (typeof meta !== "object" || meta === null || !("idleThresholdS" in meta))
      return null
    probedMeta = meta
    return origin
  } catch {
    return null
  } finally {
    clearTimeout(timer)
  }
}

function resolveBackend(): Promise<string | null> {
  backendPromise ??=
    CONFIGURED_URL !== null
      ? Promise.resolve(CONFIGURED_URL)
      : probeSameOrigin()
  return backendPromise
}

/** Exposed for tests and for the header, which must not guess. */
export async function resolveDataMode(): Promise<DataMode> {
  return (await resolveBackend()) === null ? "fixtures" : "live"
}

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
  base: string,
  path: string,
  filters: Filters,
  signal?: AbortSignal,
): Promise<T> {
  const query = toSearchParams(filters).toString()
  const url = `${base}${path}${query === "" ? "" : `?${query}`}`
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
  cost: () => import("../fixtures/cost.json").then((m) => m.default as unknown as Cost),
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

/* ── the nine endpoints ─────────────────────────────────────────────────── */

/** GET /api/meta. Never filtered — it is what populates the filter controls. */
export async function getMeta(signal?: AbortSignal): Promise<Meta> {
  const base = await resolveBackend()
  if (base === null) return fixture.meta()
  // The probe already paid for this exact request; spend its answer once.
  if (probedMeta !== null) {
    const meta = probedMeta
    probedMeta = null
    return meta
  }
  return request<Meta>(base, "/api/meta", EMPTY_FILTERS, signal)
}

export async function getSummary(f: Filters, signal?: AbortSignal): Promise<Summary> {
  const base = await resolveBackend()
  if (base === null) {
    const base = await fixture.summary()
    if (isUnfiltered(f)) return base
    const [{ spans }, index] = await Promise.all([fixture.timeline(), projectIndex()])
    return deriveSummary(filterSpans(spans, f, index), base)
  }
  return request<Summary>(base, "/api/summary", f, signal)
}

export async function getTimeline(f: Filters, signal?: AbortSignal): Promise<Timeline> {
  const base = await resolveBackend()
  if (base === null) {
    const tl = await fixture.timeline()
    if (isUnfiltered(f)) return tl
    const index = await projectIndex()
    return { ...tl, spans: filterSpans(tl.spans, f, index) }
  }
  return request<Timeline>(base, "/api/timeline", f, signal)
}

export async function getProjects(f: Filters, signal?: AbortSignal): Promise<Projects> {
  const base = await resolveBackend()
  if (base === null) {
    const capture = await fixture.projects()
    if (isUnfiltered(f)) return capture
    const [{ spans }, index] = await Promise.all([fixture.timeline(), projectIndex()])
    return deriveProjects(filterSpans(spans, f, index), index, capture.currency)
  }
  return request<Projects>(base, "/api/projects", f, signal)
}

export async function getConcurrency(
  f: Filters,
  signal?: AbortSignal,
): Promise<Concurrency> {
  const base = await resolveBackend()
  if (base === null) {
    if (isUnfiltered(f)) return fixture.concurrency()
    const [{ spans }, index] = await Promise.all([fixture.timeline(), projectIndex()])
    return deriveConcurrency(filterSpans(spans, f, index))
  }
  return request<Concurrency>(base, "/api/concurrency", f, signal)
}

export async function getDaily(f: Filters, signal?: AbortSignal): Promise<Daily> {
  const base = await resolveBackend()
  if (base === null) {
    const capture = await fixture.daily()
    if (isUnfiltered(f)) return capture
    const [{ spans }, index] = await Promise.all([fixture.timeline(), projectIndex()])
    return deriveDaily(filterSpans(spans, f, index), f, capture)
  }
  return request<Daily>(base, "/api/daily", f, signal)
}

export async function getAgents(f: Filters, signal?: AbortSignal): Promise<Agents> {
  const base = await resolveBackend()
  if (base === null) {
    if (isUnfiltered(f)) return fixture.agents()
    const [{ spans }, index] = await Promise.all([fixture.timeline(), projectIndex()])
    return deriveAgents(filterSpans(spans, f, index))
  }
  return request<Agents>(base, "/api/agents", f, signal)
}

export async function getHeatmap(f: Filters, signal?: AbortSignal): Promise<Heatmap> {
  const base = await resolveBackend()
  if (base === null) {
    if (isUnfiltered(f)) return fixture.heatmap()
    const [{ spans }, index] = await Promise.all([fixture.timeline(), projectIndex()])
    return deriveHeatmap(filterSpans(spans, f, index))
  }
  return request<Heatmap>(base, "/api/heatmap", f, signal)
}

/**
 * The **unfiltered** path list, for the filter control.
 *
 * `meta` carries a flat roster of paths with no indication of which project
 * each belongs to, so the hierarchy in the Projects control cannot be built
 * from it. This is the same `/api/projects` shape with no filters applied, so
 * narrowing the page never removes a choice from the control that offered it.
 */
export async function getProjectRoster(
  signal?: AbortSignal,
): Promise<ProjectRow[]> {
  const base = await resolveBackend()
  if (base === null) return (await fixture.projects()).projects
  return (await request<Projects>(base, "/api/projects", EMPTY_FILTERS, signal))
    .projects
}

export async function getGroups(f: Filters, signal?: AbortSignal): Promise<Groups> {
  const base = await resolveBackend()
  if (base === null) {
    const base = await fixture.groups()
    if (isUnfiltered(f)) return base
    const [{ spans }, index] = await Promise.all([fixture.timeline(), projectIndex()])
    return deriveGroups(filterSpans(spans, f, index), index, base)
  }
  return request<Groups>(base, "/api/groups", f, signal)
}

/**
 * GET /api/cost. In fixtures mode there is nothing to recompute from: cost is
 * a per-event fact and the span capture carries none, so the unfiltered
 * capture comes back whatever the filter. `fetchDashboard` flags that with
 * `eventFactsUnfiltered`; a caller using this directly must do the same.
 */
export async function getCost(f: Filters, signal?: AbortSignal): Promise<Cost> {
  const base = await resolveBackend()
  if (base === null) return fixture.cost()
  return request<Cost>(base, "/api/cost", f, signal)
}

/**
 * The URL of the watch-mode event stream, or null when this page has no
 * backend to listen to.
 *
 * Not a `get*` function because it is not a fetch: `/api/live` is an SSE
 * stream outside the `Filters` contract, held open by `cci watch --serve`
 * and answered with a 404 by a plain `cci serve`. See `use-live.ts`.
 */
export async function resolveLiveUrl(): Promise<string | null> {
  const base = await resolveBackend()
  return base === null ? null : `${base}/api/live`
}

/* ── one call for the whole page ─────────────────────────────────────────── */

export type DashboardData = {
  meta: Meta
  summary: Summary
  projects: Projects
  /** One row per logical project. `sum(groups) + ungrouped === summary.activeMs`. */
  groups: Groups
  /** Every path, unfiltered, with its project — what the filter control lists. */
  roster: ProjectRow[]
  concurrency: Concurrency
  timeline: Timeline
  daily: Daily
  agents: Agents
  heatmap: Heatmap
  /**
   * The list-price equivalent, broken down and qualified. NOT a bill: every
   * surface that shows `cost.total` says so and shows `unpricedTokens`,
   * `approximations` and `attributedEvents` beside it.
   */
  cost: Cost
  mode: DataMode
  /** True when the view is narrowed. */
  filtered: boolean
  /**
   * In fixtures mode, **per-event facts** — `summary.events`, `summary.tokens`,
   * and every `cost` (`cost`, `summary.cost`, `projects[].cost`,
   * `daily.days[].cost`) — cannot be recomputed from the span capture, so
   * under a filter they stay at their unfiltered values. When this is true,
   * render them as unavailable, never as filtered numbers: a cost that
   * silently ignores the filter is the one figure this page must not show.
   */
  eventFactsUnfiltered: boolean
}

export async function fetchDashboard(
  f: Filters,
  signal?: AbortSignal,
): Promise<DashboardData> {
  const filtered = !isUnfiltered(f)

  if ((await resolveBackend()) !== null) {
    const [
      meta,
      summary,
      projects,
      groups,
      roster,
      concurrency,
      timeline,
      daily,
      agents,
      heatmap,
      cost,
    ] = await Promise.all([
      getMeta(signal),
      getSummary(f, signal),
      getProjects(f, signal),
      getGroups(f, signal),
      getProjectRoster(signal),
      getConcurrency(f, signal),
      getTimeline(f, signal),
      getDaily(f, signal),
      getAgents(f, signal),
      getHeatmap(f, signal),
      getCost(f, signal),
    ])
    return {
      meta,
      summary,
      projects,
      groups,
      roster,
      concurrency,
      timeline,
      daily,
      agents,
      heatmap,
      cost,
      mode: "live",
      filtered,
      eventFactsUnfiltered: false,
    }
  }

  // Fixtures: load once, filter once, derive the rest from the same span list.
  const [
    meta,
    baseSummary,
    baseTimeline,
    baseGroups,
    baseProjects,
    baseDaily,
    cost,
    index,
  ] = await Promise.all([
    fixture.meta(),
    fixture.summary(),
    fixture.timeline(),
    fixture.groups(),
    fixture.projects(),
    fixture.daily(),
    fixture.cost(),
    projectIndex(),
  ])
  const roster = baseProjects.projects

  if (!filtered) {
    const [concurrency, agents, heatmap] = await Promise.all([
      fixture.concurrency(),
      fixture.agents(),
      fixture.heatmap(),
    ])
    return {
      meta,
      summary: baseSummary,
      projects: baseProjects,
      groups: baseGroups,
      roster,
      concurrency,
      timeline: baseTimeline,
      daily: baseDaily,
      agents,
      heatmap,
      cost,
      mode: "fixtures",
      filtered: false,
      eventFactsUnfiltered: false,
    }
  }

  const spans = filterSpans(baseTimeline.spans, f, index)
  return {
    meta,
    summary: deriveSummary(spans, baseSummary),
    projects: deriveProjects(spans, index, baseProjects.currency),
    groups: deriveGroups(spans, index, baseGroups),
    roster,
    concurrency: deriveConcurrency(spans),
    timeline: { ...baseTimeline, spans },
    daily: deriveDaily(spans, f, baseDaily),
    agents: deriveAgents(spans),
    heatmap: deriveHeatmap(spans),
    // Unfiltered on purpose — there is nothing to recompute it from. The flag
    // below is what keeps it off the page.
    cost,
    mode: "fixtures",
    filtered: true,
    eventFactsUnfiltered: true,
  }
}
