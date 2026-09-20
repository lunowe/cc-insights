/**
 * Pure layout and time math for the timeline swimlane. No React, no DOM, so
 * every function here can be checked against the fixtures in isolation.
 */
import { format } from "date-fns"

import type { Source, Span } from "@/lib/types"

export type Window = { from: number; to: number }

export const MINUTE = 60_000
export const HOUR = 3_600_000
export const DAY = 86_400_000

export type Lane = {
  threadId: string
  sessionId: string
  isSubagent: boolean
  /** 0 = root, 1 = subagent under a root, 2 = subagent under a subagent. */
  depth: number
  agentName: string | null
  source: Source
  /** Spans overlapping the window, by start. */
  spans: Span[]
  firstStart: number
}

/**
 * One session's lanes. `groupId`/`groupName` are the **project**; `projectId`/
 * `projectName` are the on-disk **path** it was checked out at. The header
 * shows the project, the tooltip shows the path.
 */
export type SessionGroup = {
  sessionId: string
  projectId: string | null
  projectName: string | null
  groupId: string | null
  groupName: string | null
  source: Source
  lanes: Lane[]
  firstStart: number
}

export const overlapping = (spans: Span[], w: Window) =>
  spans.filter((s) => s.end > w.from && s.start < w.to)

/**
 * One lane per thread, grouped by session, subagents nested under the thread
 * that spawned them. A subagent whose parent has no span in the window is
 * still shown, one level under the session, so no work is ever hidden.
 */
export function buildGroups(spans: Span[], w: Window): SessionGroup[] {
  const vis = overlapping(spans, w).sort((a, b) => a.start - b.start)

  type T = Lane & { parentThreadId: string | null }
  const threads = new Map<string, T>()
  for (const s of vis) {
    let t = threads.get(s.threadId)
    if (t === undefined) {
      t = {
        threadId: s.threadId,
        sessionId: s.sessionId,
        isSubagent: s.isSubagent,
        depth: 0,
        agentName: s.agentName,
        source: s.source,
        spans: [],
        firstStart: s.start,
        parentThreadId: s.parentThreadId,
      }
      threads.set(s.threadId, t)
    }
    t.spans.push(s)
  }

  const bySession = new Map<string, T[]>()
  for (const t of threads.values()) {
    const list = bySession.get(t.sessionId)
    if (list === undefined) bySession.set(t.sessionId, [t])
    else list.push(t)
  }

  const groups: SessionGroup[] = []
  for (const [sessionId, list] of bySession) {
    const inSession = new Set(list.map((t) => t.threadId))
    const children = new Map<string, T[]>()
    const roots: T[] = []
    for (const t of list) {
      const parent =
        t.parentThreadId !== null && inSession.has(t.parentThreadId)
          ? t.parentThreadId
          : null
      if (parent === null) roots.push(t)
      else {
        const c = children.get(parent)
        if (c === undefined) children.set(parent, [t])
        else c.push(t)
      }
    }
    const byStart = (a: T, b: T) => a.firstStart - b.firstStart
    // Real roots first, orphaned subagents after them.
    roots.sort(
      (a, b) => Number(a.isSubagent) - Number(b.isSubagent) || byStart(a, b),
    )

    const lanes: Lane[] = []
    const visit = (t: T, depth: number) => {
      lanes.push({ ...t, depth })
      for (const c of (children.get(t.threadId) ?? []).sort(byStart))
        visit(c, depth + 1)
    }
    for (const r of roots) visit(r, r.isSubagent ? 1 : 0)

    const first = list[0]
    const projectSpan = first.spans[0]
    groups.push({
      sessionId,
      projectId: projectSpan.projectId,
      projectName: projectSpan.projectName,
      groupId: projectSpan.groupId,
      groupName: projectSpan.groupName,
      source: first.source,
      lanes,
      firstStart: Math.min(...list.map((t) => t.firstStart)),
    })
  }
  groups.sort((a, b) => a.firstStart - b.firstStart)
  return groups
}

/* ── sweep line over the window ─────────────────────────────────────────── */

export type Step = { t: number; level: number }

export type WindowStats = {
  activeMs: number
  wallMs: number
  peak: number
  peakAt: number | null
  threads: number
  sessions: number
  /** Step function of concurrent threads across the window. */
  steps: Step[]
}

export function windowStats(spans: Span[], w: Window): WindowStats {
  const events: { at: number; delta: number }[] = []
  const threads = new Set<string>()
  const sessions = new Set<string>()
  let activeMs = 0
  for (const s of overlapping(spans, w)) {
    // Count the thread even when its clipped span is zero-length, so the
    // "All" window agrees with the tiles' thread and session counts.
    threads.add(s.threadId)
    sessions.add(s.sessionId)
    const from = Math.max(s.start, w.from)
    const to = Math.min(s.end, w.to)
    if (to <= from) continue
    activeMs += to - from
    events.push({ at: from, delta: 1 }, { at: to, delta: -1 })
  }
  events.sort((a, b) => a.at - b.at || a.delta - b.delta)

  const steps: Step[] = [{ t: w.from, level: 0 }]
  let level = 0
  let prev: number | null = null
  let wallMs = 0
  let peak = 0
  let peakAt: number | null = null
  for (const e of events) {
    if (prev !== null && e.at > prev && level > 0) wallMs += e.at - prev
    level += e.delta
    if (level > peak) {
      peak = level
      peakAt = e.at
    }
    steps.push({ t: e.at, level })
    prev = e.at
  }
  steps.push({ t: w.to, level: 0 })
  return {
    activeMs,
    wallMs,
    peak,
    peakAt,
    threads: threads.size,
    sessions: sessions.size,
    steps,
  }
}

/* ── local calendar helpers ─────────────────────────────────────────────── */

export function localDay(ts: number): Window {
  const d = new Date(ts)
  return {
    from: new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime(),
    to: new Date(d.getFullYear(), d.getMonth(), d.getDate() + 1).getTime(),
  }
}

/** Monday-start local week. */
export function localWeek(ts: number): Window {
  const d = new Date(ts)
  const back = (d.getDay() + 6) % 7
  return {
    from: new Date(d.getFullYear(), d.getMonth(), d.getDate() - back).getTime(),
    to: new Date(
      d.getFullYear(),
      d.getMonth(),
      d.getDate() - back + 7,
    ).getTime(),
  }
}

export function localMonth(ts: number): Window {
  const d = new Date(ts)
  return {
    from: new Date(d.getFullYear(), d.getMonth(), 1).getTime(),
    to: new Date(d.getFullYear(), d.getMonth() + 1, 1).getTime(),
  }
}

/** Every local midnight strictly inside the window. */
export function midnightsWithin(w: Window): number[] {
  const out: number[] = []
  let cur = localDay(w.from).to
  let guard = 0
  while (cur < w.to && guard++ < 1000) {
    out.push(cur)
    const d = new Date(cur)
    cur = new Date(d.getFullYear(), d.getMonth(), d.getDate() + 1).getTime()
  }
  return out
}

/* ── axis ticks ─────────────────────────────────────────────────────────── */

export type Tick = { t: number; label: string; major: boolean }

type StepSpec =
  | { kind: "minutes"; n: number }
  | { kind: "hours"; n: number }
  | { kind: "days"; n: number }
  | { kind: "months"; n: number }

const STEPS: StepSpec[] = [
  { kind: "minutes", n: 5 },
  { kind: "minutes", n: 10 },
  { kind: "minutes", n: 15 },
  { kind: "minutes", n: 30 },
  { kind: "hours", n: 1 },
  { kind: "hours", n: 2 },
  { kind: "hours", n: 3 },
  { kind: "hours", n: 6 },
  { kind: "hours", n: 12 },
  { kind: "days", n: 1 },
  { kind: "days", n: 2 },
  { kind: "days", n: 7 },
  { kind: "days", n: 14 },
  { kind: "months", n: 1 },
  { kind: "months", n: 2 },
  { kind: "months", n: 3 },
]

const approxMs = (s: StepSpec) =>
  s.kind === "minutes"
    ? s.n * MINUTE
    : s.kind === "hours"
      ? s.n * HOUR
      : s.kind === "days"
        ? s.n * DAY
        : s.n * 30.4 * DAY

/**
 * Ticks at "nice" local boundaries, chosen so labels sit at least `minPx`
 * apart. Steps walk the local calendar (not fixed ms) so a DST change never
 * skews an hour label.
 */
export function timeTicks(w: Window, plotW: number, minPx = 64): Tick[] {
  const len = w.to - w.from
  const step =
    STEPS.find((s) => plotW / (len / approxMs(s)) >= minPx) ??
    STEPS[STEPS.length - 1]

  const out: Tick[] = []
  const d0 = new Date(w.from)
  let cur: Date
  const next = (d: Date): Date => {
    switch (step.kind) {
      case "minutes":
        return new Date(
          d.getFullYear(),
          d.getMonth(),
          d.getDate(),
          d.getHours(),
          d.getMinutes() + step.n,
        )
      case "hours":
        return new Date(
          d.getFullYear(),
          d.getMonth(),
          d.getDate(),
          d.getHours() + step.n,
        )
      case "days":
        return new Date(d.getFullYear(), d.getMonth(), d.getDate() + step.n)
      case "months":
        return new Date(d.getFullYear(), d.getMonth() + step.n, 1)
    }
  }
  switch (step.kind) {
    case "minutes":
      cur = new Date(
        d0.getFullYear(),
        d0.getMonth(),
        d0.getDate(),
        d0.getHours(),
        0,
      )
      break
    case "hours":
      cur = new Date(d0.getFullYear(), d0.getMonth(), d0.getDate(), 0)
      break
    case "days": {
      cur = new Date(d0.getFullYear(), d0.getMonth(), d0.getDate())
      if (step.n === 7 || step.n === 14) {
        const back = (cur.getDay() + 6) % 7
        cur = new Date(cur.getFullYear(), cur.getMonth(), cur.getDate() - back)
      }
      break
    }
    case "months":
      cur = new Date(d0.getFullYear(), d0.getMonth(), 1)
      break
  }

  let guard = 0
  while (cur.getTime() <= w.to && guard++ < 2000) {
    const t = cur.getTime()
    if (t >= w.from) {
      const midnight = cur.getHours() === 0 && cur.getMinutes() === 0
      let label: string
      let major = false
      if (step.kind === "minutes" || step.kind === "hours") {
        label = midnight ? format(cur, "d MMM") : format(cur, "HH:mm")
        major = midnight
      } else if (step.kind === "days") {
        label =
          cur.getDate() === 1 || out.length === 0
            ? format(cur, "d MMM")
            : format(cur, "d")
        major = cur.getDate() === 1
      } else {
        label = format(cur, "MMM yy")
        major = cur.getMonth() === 0
      }
      out.push({ t, label, major })
    }
    cur = next(cur)
  }
  return out
}

/** Human title for the window: a day, a week, a month, or a range. */
export function describeWindow(w: Window): string {
  const len = w.to - w.from
  const a = new Date(w.from)
  const b = new Date(w.to - 1)
  if (len <= DAY + HOUR && localDay(w.from).from === w.from)
    return format(a, "EEE d MMM yyyy")
  const sameYear = a.getFullYear() === b.getFullYear()
  return `${format(a, sameYear ? "d MMM" : "d MMM yyyy")} – ${format(b, "d MMM yyyy")}`
}
