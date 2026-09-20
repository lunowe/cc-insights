import { useMemo } from "react"
import { Bar, BarChart, Cell, LabelList, XAxis, YAxis } from "recharts"

import {
  ChartContainer,
  ChartTooltip,
  type ChartConfig,
} from "@/components/ui/chart"
import { formatCount, formatHours, formatPercent } from "@/lib/format"
import type { Groups } from "@/lib/types"

import { BarEndLabel } from "./bar-label"
import { ChartFrame, type TableView } from "./chart-frame"
import { TipRow } from "./hover-tip"
import { OTHER_COLOR, type ProjectPalette } from "./palette"
import { useMeasure } from "./use-measure"

const HOUR = 3_600_000
const TOP_N = 8

type Row = {
  id: string
  name: string
  ms: number
  hours: number
  sessions: number
  threads: number
  color: string
  tip: string
  isTail: boolean
  tailCount: number
}

const config = { hours: { label: "Active hours" } } satisfies ChartConfig

/**
 * One bar per **project**, not per on-disk path. Broken down by path this
 * chart put atlas-chat at 55.7 h and its own worktree at 39.8 h two rows
 * apart, as if they were rival pieces of work; by project it is one bar at
 * 111.6 h. Paths live in the table below, where the detail belongs.
 */
export function ProjectsChart({
  groups,
  palette,
}: {
  groups: Groups
  palette: ProjectPalette
}) {
  const [ref, width] = useMeasure<HTMLDivElement>()
  const unplacedMs = groups.ungrouped.activeMs
  const total =
    groups.groups.reduce((n, g) => n + g.activeMs, 0) + unplacedMs
  const rows = useMemo<Row[]>(() => {
    const sorted = [...groups.groups].sort((a, b) => b.activeMs - a.activeMs)
    const head = sorted.slice(0, TOP_N)
    const tail = sorted.slice(TOP_N)
    const out: Row[] = head.map((g) => ({
      id: g.groupId,
      name: g.name,
      ms: g.activeMs,
      hours: g.activeMs / HOUR,
      sessions: g.sessions,
      threads: g.threads,
      color: palette.colorOf(g.groupId),
      tip: `${formatHours(g.activeMs)} h · ${formatPercent(g.activeMs, total)}`,
      isTail: false,
      tailCount: 0,
    }))
    if (tail.length > 0) {
      const ms = tail.reduce((n, g) => n + g.activeMs, 0)
      out.push({
        id: "__tail",
        name: `${tail.length} other project${tail.length === 1 ? "" : "s"}`,
        ms,
        hours: ms / HOUR,
        sessions: tail.reduce((n, g) => n + g.sessions, 0),
        threads: tail.reduce((n, g) => n + g.threads, 0),
        color: OTHER_COLOR,
        tip: `${formatHours(ms)} h · ${formatPercent(ms, total)}`,
        isTail: true,
        tailCount: tail.length,
      })
    }
    if (unplacedMs > 0) {
      out.push({
        id: "__unplaced",
        // Short on purpose: this is an axis tick, and it must not wrap.
        name: `No project (${groups.ungrouped.projects})`,
        ms: unplacedMs,
        hours: unplacedMs / HOUR,
        sessions: 0,
        threads: 0,
        color: OTHER_COLOR,
        tip: `${formatHours(unplacedMs)} h · ${formatPercent(unplacedMs, total)}`,
        isTail: true,
        tailCount: groups.ungrouped.projects,
      })
    }
    return out
  }, [groups, palette, total, unplacedMs])

  const table = useMemo<TableView>(
    () => ({
      columns: [
        { key: "name", label: "Project" },
        { key: "paths", label: "Paths", align: "right" },
        { key: "hours", label: "Active", align: "right" },
        { key: "share", label: "Share", align: "right" },
        { key: "sessions", label: "Sessions", align: "right" },
        { key: "threads", label: "Threads", align: "right" },
      ],
      rows: [
        ...[...groups.groups]
          .sort((a, b) => b.activeMs - a.activeMs)
          .map((g) => ({
            name: g.name,
            paths: formatCount(g.projects),
            hours: `${formatHours(g.activeMs)} h`,
            share: formatPercent(g.activeMs, total),
            sessions: formatCount(g.sessions),
            threads: formatCount(g.threads),
          })),
        ...(unplacedMs > 0
          ? [
              {
                name: "No project",
                paths: formatCount(groups.ungrouped.projects),
                hours: `${formatHours(unplacedMs)} h`,
                share: formatPercent(unplacedMs, total),
                sessions: "—",
                threads: "—",
              },
            ]
          : []),
      ],
      note: "Every project in the filtered range, not only the top rows the chart shows. Paths counts the on-disk checkouts each one covers.",
    }),
    [groups, total, unplacedMs],
  )

  const narrow = width > 0 && width < 480
  const labelW = narrow ? 92 : 132
  const maxChars = Math.floor((labelW - 10) / 6.8)
  const truncate = (s: string) =>
    s.length <= maxChars ? s : `${s.slice(0, maxChars - 1)}…`
  const height = rows.length * 26 + 12

  return (
    <ChartFrame
      id="chart-projects"
      title="Project breakdown"
      description={
        <>
          {groups.groups.length === 0
            ? `Nothing has been folded into a project yet, so every path in view shares one row.`
            : groups.groups.length > TOP_N
              ? `Top ${TOP_N} projects by active time; the other ${groups.groups.length - TOP_N} share one row.`
              : groups.groups.length === 1
                ? "The one project in view."
                : `All ${groups.groups.length} projects in view, by active time.`}{" "}
          A project is one repo however many paths it was checked out at, so a
          worktree adds to its project rather than standing beside it. Colours
          are the swimlane&rsquo;s: the five largest projects all-time keep a
          hue, everything else is gray, whatever the filter.
        </>
      }
      table={table}
    >
      <div ref={ref} className="min-w-0">
        <ChartContainer
          config={config}
          className="aspect-auto w-full"
          style={{ height }}
        >
          <BarChart
            data={rows}
            layout="vertical"
            margin={{ top: 4, right: narrow ? 96 : 104, left: 0, bottom: 4 }}
            barCategoryGap={3}
          >
            <XAxis type="number" hide domain={[0, "dataMax"]} />
            <YAxis
              type="category"
              dataKey="name"
              width={labelW}
              tickLine={false}
              axisLine={false}
              tick={{ fontSize: 11 }}
              tickFormatter={truncate}
              interval={0}
            />
            <ChartTooltip
              cursor={{ fill: "var(--muted)", fillOpacity: 0.7 }}
              content={(p) => {
                const row = p.payload?.[0]?.payload as Row | undefined
                if (!p.active || row === undefined) return null
                return (
                  <div className="grid min-w-48 gap-1 rounded-md border border-border/60 bg-popover px-2.5 py-2 text-[0.75rem] text-popover-foreground shadow-lg">
                    <TipRow
                      swatch={row.color}
                      label={row.name}
                      value={`${formatHours(row.ms)} h`}
                    />
                    <TipRow
                      label="Share of active"
                      value={formatPercent(row.ms, total)}
                    />
                    <TipRow
                      label="Sessions"
                      value={formatCount(row.sessions)}
                    />
                    <TipRow label="Threads" value={formatCount(row.threads)} />
                  </div>
                )
              }}
            />
            <Bar
              dataKey="hours"
              radius={[0, 2, 2, 0]}
              maxBarSize={20}
              isAnimationActive={false}
            >
              {rows.map((r) => (
                <Cell key={r.id} fill={r.color} />
              ))}
              <LabelList dataKey="tip" content={BarEndLabel} />
            </Bar>
          </BarChart>
        </ChartContainer>
      </div>
    </ChartFrame>
  )
}
