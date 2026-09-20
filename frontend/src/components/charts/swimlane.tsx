import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react"
import { ChevronLeft, ChevronRight, Crosshair } from "lucide-react"
import { cn } from "cn"

import { Button } from "@/components/ui/button"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import type { DashboardData } from "@/lib/api"
import {
  SOURCE_LABEL,
  formatCount,
  formatDateTime,
  formatDuration,
  formatHours,
  formatMultiplier,
} from "@/lib/format"
import type { Span } from "@/lib/types"

import { ChartFrame, Legend, type TableView } from "./chart-frame"
import { HoverTip, TipRow, localPoint, type Tip } from "./hover-tip"
import { OTHER_COLOR, colorKey, type ProjectPalette } from "./palette"
import {
  DAY,
  HOUR,
  MINUTE,
  buildGroups,
  describeWindow,
  localDay,
  localMonth,
  localWeek,
  midnightsWithin,
  timeTicks,
  windowStats,
  type SessionGroup,
  type Window,
} from "./swimlane-model"
import { useMeasure } from "./use-measure"

/* ── geometry ───────────────────────────────────────────────────────────── */

const SESSION_H = 20
const LANE_H = 14
const SESSION_GAP = 8
const AXIS_H = 22
const RIBBON_H = 44
const HEADER_H = AXIS_H + RIBBON_H
const RIGHT_PAD = 10
const MAX_LANES_PX = 460
const MIN_WINDOW = 15 * MINUTE
const MIN_BAR_PX = 2

type Scale = "day" | "week" | "month" | "all"

function scaleOf(w: Window, range: Window): Scale {
  const len = w.to - w.from
  if (w.from <= range.from && w.to >= range.to) return "all"
  if (len <= DAY + HOUR) return "day"
  if (len <= 8 * DAY) return "week"
  return "month"
}

function clampWindow(w: Window, range: Window): Window {
  const len = w.to - w.from
  const rangeLen = range.to - range.from
  if (len >= rangeLen) return range
  const from = Math.min(Math.max(w.from, range.from), range.to - len)
  return { from, to: from + len }
}

/* ── component ──────────────────────────────────────────────────────────── */

export function TimelineSwimlane({
  data,
  palette,
}: {
  data: DashboardData
  palette: ProjectPalette
}) {
  const spans = data.timeline.spans
  const [containerRef, width] = useMeasure<HTMLDivElement>()
  const hatchId = useId()

  /** Full extent of the filtered spans, padded to whole local days. */
  const range = useMemo<Window>(() => {
    let min = Number.POSITIVE_INFINITY
    let max = Number.NEGATIVE_INFINITY
    for (const s of spans) {
      if (s.start < min) min = s.start
      if (s.end > max) max = s.end
    }
    if (!Number.isFinite(min)) {
      const now = Date.now()
      return localDay(now)
    }
    return { from: localDay(min).from, to: localDay(max).to }
  }, [spans])

  /**
   * Default view: the local day that holds the concurrency peak of the
   * filtered spans. It is the one window guaranteed to show the most
   * parallelism the data has, and it is the same instant the "peak" tile
   * reports, so the two agree by construction.
   */
  const peakAt = data.concurrency.peakAt
  const autoWindow = useMemo<Window>(() => {
    if (peakAt !== null) return clampWindow(localDay(peakAt), range)
    return clampWindow(localDay(range.to - 1), range)
  }, [peakAt, range])

  const [userWindow, setUserWindow] = useState<Window | null>(null)
  // A new filter set yields a new peak; go back to it rather than leaving the
  // viewer on a day the new data may not touch.
  const resetKey = `${peakAt}:${spans.length}:${data.summary.activeMs}`
  const lastReset = useRef(resetKey)
  if (lastReset.current !== resetKey) {
    lastReset.current = resetKey
    if (userWindow !== null) setUserWindow(null)
  }
  const win = userWindow ?? autoWindow
  const setWindow = useCallback(
    (w: Window) => setUserWindow(clampWindow(w, range)),
    [range],
  )

  const scale = scaleOf(win, range)
  const groups = useMemo(() => buildGroups(spans, win), [spans, win])
  const stats = useMemo(() => windowStats(spans, win), [spans, win])

  /* layout */
  const narrow = width > 0 && width < 520
  const gutter = narrow ? 92 : 148
  const plotW = Math.max(40, width - gutter - RIGHT_PAD)
  const len = win.to - win.from
  const x = useCallback(
    (t: number) => gutter + ((t - win.from) / len) * plotW,
    [gutter, win.from, len, plotW],
  )
  const tAt = useCallback(
    (px: number) => win.from + ((px - gutter) / plotW) * len,
    [gutter, win.from, len, plotW],
  )

  const rows = useMemo(() => {
    let y = 0
    const placed = groups.map((g) => {
      const top = y
      y += SESSION_H + g.lanes.length * LANE_H + SESSION_GAP
      return { group: g, top }
    })
    return { placed, height: Math.max(y - SESSION_GAP, 0) }
  }, [groups])

  const ticks = useMemo(
    () => timeTicks(win, plotW, narrow ? 56 : 68),
    [win, plotW, narrow],
  )
  const midnights = useMemo(
    () => (len <= 40 * DAY ? midnightsWithin(win) : []),
    [win, len],
  )

  /* navigation */
  const center = win.from + len / 2
  const setScale = (s: Scale) => {
    // Narrowing from the whole range: land on the peak, not the range's
    // arbitrary midpoint. Otherwise keep the viewer where they are.
    const anchor =
      (userWindow === null || scale === "all") && peakAt !== null
        ? peakAt
        : center
    if (s === "day") setWindow(localDay(anchor))
    else if (s === "week") setWindow(localWeek(anchor))
    else if (s === "month") setWindow(localMonth(anchor))
    else setWindow(range)
  }
  const shift = (dir: -1 | 1) =>
    setWindow({ from: win.from + dir * len, to: win.to + dir * len })
  const jumpToPeak = () => {
    if (peakAt === null) return
    setWindow(localDay(peakAt))
  }
  const atStart = win.from <= range.from
  const atEnd = win.to >= range.to

  /* pan by drag, zoom by ctrl/⌘-wheel, pan by horizontal wheel */
  const drag = useRef<{ startX: number; from: number; moved: boolean } | null>(
    null,
  )
  const [dragging, setDragging] = useState(false)
  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return
    if (localPoint(e.currentTarget, e).x < gutter) return
    drag.current = { startX: e.clientX, from: win.from, moved: false }
    e.currentTarget.setPointerCapture(e.pointerId)
  }
  const onPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const d = drag.current
    if (d === null) return
    const dx = e.clientX - d.startX
    if (!d.moved && Math.abs(dx) < 4) return
    d.moved = true
    if (!dragging) setDragging(true)
    const dt = (dx / plotW) * len
    setWindow({ from: d.from - dt, to: d.from - dt + len })
  }
  const onPointerUp = () => {
    drag.current = null
    setDragging(false)
  }

  const scrollRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    const el = scrollRef.current
    if (el === null) return
    const onWheel = (e: WheelEvent) => {
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault()
        const { x: px } = localPoint(el, e)
        const t = tAt(px)
        const factor = Math.exp(e.deltaY * 0.0015)
        const next = Math.max(
          MIN_WINDOW,
          Math.min(range.to - range.from, len * factor),
        )
        const frac = (t - win.from) / len
        setWindow({ from: t - frac * next, to: t + (1 - frac) * next })
      } else if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
        e.preventDefault()
        const dt = (e.deltaX / plotW) * len
        setWindow({ from: win.from + dt, to: win.to + dt })
      }
    }
    el.addEventListener("wheel", onWheel, { passive: false })
    return () => el.removeEventListener("wheel", onWheel)
  }, [tAt, len, win.from, win.to, plotW, range, setWindow])

  /* tooltip */
  const [tip, setTip] = useState<Tip | null>(null)
  const showSpan = (e: ReactPointerEvent<SVGElement>, s: Span) => {
    const host = containerRef.current
    if (host === null || drag.current?.moved) return
    const p = localPoint(host, e)
    const kind = s.isSubagent
      ? `Subagent · ${s.source !== "codex" && s.agentName ? s.agentName : SOURCE_LABEL[s.source]}`
      : s.attended === 0
        ? "Root thread · resumed with no human turn"
        : "Root thread · human-initiated"
    setTip({
      x: p.x,
      y: p.y,
      body: (
        <div className="grid gap-1">
          <TipRow
            swatch={palette.colorOf(colorKey(s.groupId, s.projectId))}
            label={s.groupName ?? s.projectName ?? "No project"}
            value={formatDuration(s.end - s.start)}
          />
          {/* The path is worth showing — just never instead of the project. */}
          {s.projectName !== null && s.projectName !== s.groupName ? (
            <p className="num text-muted-foreground">path {s.projectName}</p>
          ) : null}
          <p className="text-muted-foreground">{kind}</p>
          <p className="num text-muted-foreground">
            {formatDateTime(s.start)} → {formatDateTime(s.end)}
          </p>
          <p className="num text-muted-foreground">
            {SOURCE_LABEL[s.source]} · session {s.sessionId.slice(0, 8)}
          </p>
        </div>
      ),
    })
  }

  /* table twin */
  const table = useMemo<TableView>(() => {
    const rowsOut: TableView["rows"] = []
    for (const g of groups) {
      for (const lane of g.lanes) {
        for (const s of lane.spans) {
          rowsOut.push({
            project: g.groupName ?? g.projectName ?? "—",
            path: g.projectName ?? "—",
            thread: lane.isSubagent
              ? `subagent${lane.depth > 1 ? ` (depth ${lane.depth})` : ""} · ${
                  s.source !== "codex" && s.agentName
                    ? s.agentName
                    : SOURCE_LABEL[s.source]
                }`
              : `root · ${SOURCE_LABEL[s.source]}`,
            start: formatDateTime(s.start),
            end: formatDateTime(s.end),
            duration: formatDuration(s.end - s.start),
          })
        }
      }
    }
    return {
      columns: [
        { key: "project", label: "Project" },
        { key: "path", label: "Path" },
        { key: "thread", label: "Thread" },
        { key: "start", label: "Start", align: "right" },
        { key: "end", label: "End", align: "right" },
        { key: "duration", label: "Active", align: "right" },
      ],
      rows: rowsOut,
      note: `Spans overlapping ${describeWindow(win)}, local time. A span ends after ${data.meta.idleThresholdS} s without an event.`,
    }
  }, [groups, win, data.meta.idleThresholdS])

  const ribbonMax = Math.max(stats.peak, 1)
  const ribbonY = (level: number) =>
    AXIS_H + RIBBON_H - 4 - (level / ribbonMax) * (RIBBON_H - 12)
  const ribbonPath = useMemo(() => {
    let d = ""
    let prevLevel = 0
    stats.steps.forEach((s, i) => {
      const px = x(s.t)
      if (i === 0) d += `M${px},${ribbonY(0)}`
      else d += `L${px},${ribbonY(prevLevel)}`
      d += `L${px},${ribbonY(s.level)}`
      prevLevel = s.level
    })
    return d
    // ribbonY is derived from stats.peak, which is part of stats.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stats, x])

  const peakInWindow =
    stats.peakAt !== null && stats.peakAt >= win.from && stats.peakAt <= win.to
      ? stats.peakAt
      : null

  const lanesHeight = Math.min(rows.height, MAX_LANES_PX)

  return (
    <ChartFrame
      id="chart-timeline-swimlane"
      title="Timeline swimlane"
      description={
        <>
          One lane per thread, gathered by session and coloured by project — a
          project&rsquo;s worktrees and subdirectories share its name and hue,
          and the path is in each span&rsquo;s tooltip. Subagent
          lanes sit indented under the thread that spawned them. The ribbon
          counts threads running at once. Drag to pan, ⌘/Ctrl + wheel to zoom.
        </>
      }
      controls={
        <div className="flex flex-wrap items-center gap-1.5">
          <ToggleGroup
            type="single"
            variant="outline"
            size="sm"
            value={scale}
            onValueChange={(v) => {
              if (v) setScale(v as Scale)
            }}
            aria-label="Window length"
            className="h-7 [&>button]:h-7 [&>button]:min-w-0 [&>button]:px-2 [&>button]:text-[0.6875rem]"
          >
            <ToggleGroupItem value="day">Day</ToggleGroupItem>
            <ToggleGroupItem value="week">Week</ToggleGroupItem>
            <ToggleGroupItem value="month">Month</ToggleGroupItem>
            <ToggleGroupItem value="all">All</ToggleGroupItem>
          </ToggleGroup>
          <div className="flex items-center gap-0.5">
            <Button
              variant="outline"
              size="icon-xs"
              className="h-7 w-7"
              onClick={() => shift(-1)}
              disabled={atStart || scale === "all"}
              aria-label="Earlier"
            >
              <ChevronLeft />
            </Button>
            <Button
              variant="outline"
              size="icon-xs"
              className="h-7 w-7"
              onClick={() => shift(1)}
              disabled={atEnd || scale === "all"}
              aria-label="Later"
            >
              <ChevronRight />
            </Button>
          </div>
          <Button
            variant="outline"
            size="xs"
            className="h-7 text-[0.6875rem]"
            onClick={jumpToPeak}
            disabled={peakAt === null}
            title="Jump to the day with the most concurrent threads"
          >
            <Crosshair aria-hidden />
            Peak
          </Button>
        </div>
      }
      table={table}
      footer={
        <span>
          Bars are active spans (a gap over{" "}
          <span className="num">{data.meta.idleThresholdS} s</span> ends one);
          anything shorter than {MIN_BAR_PX}px is widened to stay visible — the
          tooltip has the true duration. Times are local.
        </span>
      }
      className="min-h-72"
    >
      <WindowSummary win={win} stats={stats} />

      <div
        ref={containerRef}
        className="relative mt-3 min-w-0 select-none"
        onPointerLeave={() => setTip(null)}
      >
        <div
          ref={scrollRef}
          className={cn(
            "relative overflow-y-auto overflow-x-hidden overscroll-contain rounded-md border bg-card",
            dragging ? "cursor-grabbing" : "cursor-grab",
          )}
          style={{ maxHeight: HEADER_H + MAX_LANES_PX + 2 }}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
        >
          {/* sticky axis + concurrency ribbon */}
          <svg
            width={width || 0}
            height={HEADER_H}
            className="sticky top-0 z-10 block bg-card"
            aria-hidden
          >
            <line
              x1={gutter}
              x2={gutter + plotW}
              y1={AXIS_H - 0.5}
              y2={AXIS_H - 0.5}
              className="stroke-rule"
            />
            {ticks.map((tk) => (
              <g key={tk.t}>
                <line
                  x1={x(tk.t)}
                  x2={x(tk.t)}
                  y1={AXIS_H - 5}
                  y2={AXIS_H}
                  className={
                    tk.major ? "stroke-muted-foreground" : "stroke-border"
                  }
                />
                <text
                  x={x(tk.t)}
                  y={AXIS_H - 8}
                  textAnchor={
                    x(tk.t) < gutter + 24
                      ? "start"
                      : x(tk.t) > gutter + plotW - 24
                        ? "end"
                        : "middle"
                  }
                  className={cn(
                    "num fill-muted-foreground text-[10px]",
                    tk.major && "fill-foreground",
                  )}
                >
                  {tk.label}
                </text>
              </g>
            ))}
            {midnights.map((m) => (
              <line
                key={m}
                x1={x(m)}
                x2={x(m)}
                y1={AXIS_H}
                y2={HEADER_H}
                className="stroke-rule"
              />
            ))}
            {/* ribbon scale in the gutter: top = the window's peak, bottom = 0 */}
            <text
              x={gutter - 8}
              y={ribbonY(ribbonMax) + 4}
              textAnchor="end"
              className="text-[10px]"
            >
              <tspan className="num fill-foreground font-medium">
                {stats.peak}
              </tspan>
              <tspan className="fill-muted-foreground"> at once</tspan>
            </text>
            <text
              x={gutter - 8}
              y={ribbonY(0) + 1}
              textAnchor="end"
              className="num fill-muted-foreground text-[10px]"
            >
              0
            </text>
            <line
              x1={gutter}
              x2={gutter + plotW}
              y1={ribbonY(ribbonMax)}
              y2={ribbonY(ribbonMax)}
              className="stroke-rule"
            />
            <line
              x1={gutter}
              x2={gutter + plotW}
              y1={ribbonY(0) + 0.5}
              y2={ribbonY(0) + 0.5}
              className="stroke-border"
            />
            <path
              d={`${ribbonPath}L${x(win.to)},${ribbonY(0)}Z`}
              fill="var(--primary)"
              fillOpacity={0.22}
            />
            <path
              d={ribbonPath}
              fill="none"
              stroke="var(--primary)"
              strokeWidth={1.5}
              strokeLinejoin="round"
            />
            {peakInWindow !== null ? (
              <g>
                <line
                  x1={x(peakInWindow)}
                  x2={x(peakInWindow)}
                  y1={AXIS_H + 2}
                  y2={HEADER_H}
                  stroke="var(--primary)"
                  strokeOpacity={0.8}
                />
                <PeakLabel
                  px={x(peakInWindow)}
                  y={AXIS_H + 12}
                  right={gutter + plotW}
                  text={`peak ${stats.peak}`}
                />
              </g>
            ) : null}
          </svg>

          {/* lanes */}
          {groups.length === 0 ? (
            <div
              className="flex items-center justify-center px-4 text-center text-[0.8125rem] text-muted-foreground"
              style={{ height: 120 }}
            >
              No activity in {describeWindow(win)}. Try “Peak”, or pan with the
              arrows.
            </div>
          ) : (
            <svg
              width={width || 0}
              height={rows.height}
              className="block"
              role="img"
              aria-label={`Timeline of ${stats.threads} threads in ${stats.sessions} sessions, ${describeWindow(win)}`}
            >
              <defs>
                <pattern
                  id={hatchId}
                  patternUnits="userSpaceOnUse"
                  width={4}
                  height={4}
                  patternTransform="rotate(45)"
                >
                  <rect
                    width={2}
                    height={4}
                    fill="var(--card)"
                    fillOpacity={0.6}
                  />
                </pattern>
              </defs>
              {midnights.map((m) => (
                <line
                  key={m}
                  x1={x(m)}
                  x2={x(m)}
                  y1={0}
                  y2={rows.height}
                  className="stroke-rule"
                />
              ))}
              {rows.placed.map(({ group, top }) => (
                <SessionRows
                  key={group.sessionId}
                  group={group}
                  top={top}
                  gutter={gutter}
                  narrow={narrow}
                  win={win}
                  x={x}
                  color={palette.colorOf(
                    colorKey(group.groupId, group.projectId),
                  )}
                  hatchId={hatchId}
                  onEnter={showSpan}
                  onLeave={() => setTip(null)}
                />
              ))}
              {peakInWindow !== null ? (
                <line
                  x1={x(peakInWindow)}
                  x2={x(peakInWindow)}
                  y1={0}
                  y2={rows.height}
                  stroke="var(--primary)"
                  strokeOpacity={0.55}
                />
              ) : null}
            </svg>
          )}
        </div>
        {rows.height > lanesHeight ? (
          <p className="pointer-events-none absolute right-2 bottom-1 rounded-sm bg-card/90 px-1.5 py-0.5 text-[0.625rem] text-muted-foreground">
            scroll for {groups.reduce((n, g) => n + g.lanes.length, 0)} lanes
          </p>
        ) : null}
        <HoverTip tip={tip} width={width} />
      </div>

      <Overview
        days={data.daily.days}
        range={range}
        win={win}
        onPick={(t) => setWindow({ from: t - len / 2, to: t + len / 2 })}
      />

      <Legend
        className="mt-3"
        items={[
          ...palette.ranked.map((p) => ({
            label: p.name,
            color: `var(--chart-${p.slot})`,
          })),
          { label: "Other projects", color: OTHER_COLOR },
          {
            label: "Root resumed with no human turn",
            color: "var(--chart-ink)",
            hatched: true,
          },
        ]}
      />
    </ChartFrame>
  )
}

/* ── pieces ─────────────────────────────────────────────────────────────── */

function WindowSummary({
  win,
  stats,
}: {
  win: Window
  stats: ReturnType<typeof windowStats>
}) {
  const ratio = stats.wallMs > 0 ? stats.activeMs / stats.wallMs : 0
  return (
    <p className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 text-[0.8125rem]">
      <span className="font-medium">{describeWindow(win)}</span>
      <span className="text-muted-foreground">
        <span className="num font-medium text-foreground">
          {formatHours(stats.activeMs)} h
        </span>{" "}
        of agent work in{" "}
        <span className="num font-medium text-foreground">
          {formatHours(stats.wallMs)} h
        </span>{" "}
        elapsed
        {stats.wallMs > 0 ? (
          <>
            {" "}
            (<span className="num">{formatMultiplier(ratio)}</span>)
          </>
        ) : null}
      </span>
      <span className="text-muted-foreground">
        peak{" "}
        <span className="num font-medium text-foreground">{stats.peak}</span> at
        once
      </span>
      <span className="text-muted-foreground">
        <span className="num">{formatCount(stats.threads)}</span>{" "}
        {stats.threads === 1 ? "thread" : "threads"} ·{" "}
        <span className="num">{formatCount(stats.sessions)}</span>{" "}
        {stats.sessions === 1 ? "session" : "sessions"}
      </span>
    </p>
  )
}

function PeakLabel({
  px,
  y,
  right,
  text,
}: {
  px: number
  y: number
  right: number
  text: string
}) {
  const w = text.length * 6.2 + 10
  const flip = px + 6 + w > right
  const lx = flip ? px - 6 - w : px + 6
  return (
    <g>
      <rect
        x={lx}
        y={y - 9}
        width={w}
        height={14}
        rx={3}
        fill="var(--primary)"
      />
      <text
        x={lx + 5}
        y={y + 1.5}
        className="num text-[10px] font-medium"
        fill="var(--primary-foreground)"
      >
        {text}
      </text>
    </g>
  )
}

function truncate(text: string, maxPx: number, charPx = 6.1): string {
  const max = Math.max(3, Math.floor(maxPx / charPx))
  return text.length <= max ? text : `${text.slice(0, max - 1)}…`
}

function SessionRows({
  group,
  top,
  gutter,
  narrow,
  win,
  x,
  color,
  hatchId,
  onEnter,
  onLeave,
}: {
  group: SessionGroup
  top: number
  gutter: number
  narrow: boolean
  win: Window
  x: (t: number) => number
  color: string
  hatchId: string
  onEnter: (e: ReactPointerEvent<SVGElement>, s: Span) => void
  onLeave: () => void
}) {
  // The project, never the path: a lane headed `retry-budget-spike` is the bug
  // this fixes — that worktree is atlas-chat, and so are twelve others.
  const name = group.groupName ?? group.projectName ?? "No project"
  const n = group.lanes.length
  const header = narrow
    ? `${name} · ${n}`
    : `${name} · ${SOURCE_LABEL[group.source]} · ${n} thread${n === 1 ? "" : "s"}`
  return (
    <g transform={`translate(0,${top})`}>
      <line x1={0} x2="100%" y1={0.5} y2={0.5} className="stroke-rule" />
      <rect x={6} y={6} width={8} height={8} rx={2} fill={color} />
      <text x={20} y={14} className="fill-foreground text-[11px] font-medium">
        {header}
      </text>
      {group.lanes.map((lane, i) => {
        const y = SESSION_H + i * LANE_H
        const indent = 8 + lane.depth * 10
        const label = lane.isSubagent
          ? lane.source !== "codex" && lane.agentName
            ? lane.agentName
            : "subagent"
          : "root"
        return (
          <g key={lane.threadId} transform={`translate(0,${y})`}>
            {lane.depth > 0 ? (
              <path
                d={`M${indent - 5},1 V${LANE_H / 2 + 1} H${indent - 1}`}
                fill="none"
                className="stroke-border"
              />
            ) : null}
            <text
              x={indent + 2}
              y={LANE_H - 3.5}
              className={cn(
                "text-[10px]",
                lane.isSubagent ? "fill-muted-foreground" : "fill-foreground",
              )}
            >
              {truncate(label, gutter - indent - 10, 5.6)}
            </text>
            {lane.spans.map((s) => {
              const x1 = x(Math.max(s.start, win.from))
              const x2 = x(Math.min(s.end, win.to))
              const w = Math.max(x2 - x1, MIN_BAR_PX)
              const hatched = !s.isSubagent && s.attended === 0
              return (
                <g
                  key={s.spanId}
                  onPointerEnter={(e) => onEnter(e, s)}
                  onPointerMove={(e) => onEnter(e, s)}
                  onPointerLeave={onLeave}
                >
                  <rect
                    x={x1}
                    y={2}
                    width={w}
                    height={LANE_H - 4}
                    rx={2}
                    fill={color}
                  />
                  {hatched ? (
                    <rect
                      x={x1}
                      y={2}
                      width={w}
                      height={LANE_H - 4}
                      rx={2}
                      fill={`url(#${hatchId})`}
                    />
                  ) : null}
                  {/* hit target larger than the mark */}
                  <rect
                    x={x1 - 3}
                    y={0}
                    width={w + 6}
                    height={LANE_H}
                    fill="transparent"
                  />
                </g>
              )
            })}
          </g>
        )
      })}
    </g>
  )
}

/**
 * The whole filtered range as one strip of daily active hours, with the
 * current window marked. Orientation and a way to jump: click a day.
 */
function Overview({
  days,
  range,
  win,
  onPick,
}: {
  days: DashboardData["daily"]["days"]
  range: Window
  win: Window
  onPick: (t: number) => void
}) {
  const [ref, width] = useMeasure<HTMLDivElement>()
  const H = 30
  const bars = useMemo(() => {
    const max = days.reduce((m, d) => Math.max(m, d.activeMs), 0)
    return days
      .map((d) => {
        const [y, m, day] = d.date.split("-").map(Number)
        const t = new Date(y, m - 1, day).getTime()
        return {
          t,
          h: max > 0 ? (d.activeMs / max) * (H - 4) : 0,
          activeMs: d.activeMs,
        }
      })
      .filter((b) => b.t >= range.from - DAY && b.t < range.to)
  }, [days, range])
  const span = range.to - range.from
  const px = (t: number) => ((t - range.from) / span) * width
  const dayW = Math.max(1, (DAY / span) * width - 0.5)
  const winX1 = Math.max(0, px(win.from))
  const winX2 = Math.min(width, px(win.to))

  return (
    <div ref={ref} className="mt-2 min-w-0">
      <svg
        width={width || 0}
        height={H}
        className="block cursor-pointer rounded-sm"
        role="img"
        aria-label="Overview of the whole range; click to move the window"
        onClick={(e) => {
          const p = localPoint(e.currentTarget as unknown as HTMLElement, e)
          onPick(range.from + (p.x / width) * span)
        }}
      >
        <rect
          x={0}
          y={0}
          width={width || 0}
          height={H}
          fill="var(--muted)"
          fillOpacity={0.5}
        />
        {bars.map((b) =>
          b.h > 0 ? (
            <rect
              key={b.t}
              x={px(b.t)}
              y={H - 2 - Math.max(b.h, 1)}
              width={dayW}
              height={Math.max(b.h, 1)}
              fill="var(--chart-ink)"
              fillOpacity={0.55}
            />
          ) : null,
        )}
        <rect
          x={winX1}
          y={0}
          width={Math.max(2, winX2 - winX1)}
          height={H}
          fill="var(--primary)"
          fillOpacity={0.18}
          stroke="var(--primary)"
          strokeWidth={1}
        />
      </svg>
      <div className="mt-0.5 flex justify-between text-[0.625rem] text-muted-foreground">
        <span className="num">{describeWindow(range).split(" – ")[0]}</span>
        <span>click to move the window</span>
        <span className="num">
          {describeWindow(range).split(" – ")[1] ?? ""}
        </span>
      </div>
    </div>
  )
}
