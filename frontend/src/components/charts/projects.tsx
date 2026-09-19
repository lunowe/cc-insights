import { useMemo } from "react"
import { Bar, BarChart, Cell, LabelList, XAxis, YAxis } from "recharts"

import {
  ChartContainer,
  ChartTooltip,
  type ChartConfig,
} from "@/components/ui/chart"
import { formatCount, formatHours, formatPercent } from "@/lib/format"
import type { Projects } from "@/lib/types"

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

export function ProjectsChart({
  projects,
  palette,
}: {
  projects: Projects
  palette: ProjectPalette
}) {
  const [ref, width] = useMeasure<HTMLDivElement>()
  const total = projects.projects.reduce((n, p) => n + p.activeMs, 0)

  const rows = useMemo<Row[]>(() => {
    const sorted = [...projects.projects].sort(
      (a, b) => b.activeMs - a.activeMs,
    )
    const head = sorted.slice(0, TOP_N)
    const tail = sorted.slice(TOP_N)
    const out: Row[] = head.map((p) => ({
      id: p.projectId,
      name: p.name,
      ms: p.activeMs,
      hours: p.activeMs / HOUR,
      sessions: p.sessions,
      threads: p.threads,
      color: palette.colorOf(p.projectId),
      tip: `${formatHours(p.activeMs)} h · ${formatPercent(p.activeMs, total)}`,
      isTail: false,
      tailCount: 0,
    }))
    if (tail.length > 0) {
      const ms = tail.reduce((n, p) => n + p.activeMs, 0)
      out.push({
        id: "__tail",
        name: `${tail.length} other project${tail.length === 1 ? "" : "s"}`,
        ms,
        hours: ms / HOUR,
        sessions: tail.reduce((n, p) => n + p.sessions, 0),
        threads: tail.reduce((n, p) => n + p.threads, 0),
        color: OTHER_COLOR,
        tip: `${formatHours(ms)} h · ${formatPercent(ms, total)}`,
        isTail: true,
        tailCount: tail.length,
      })
    }
    return out
  }, [projects, palette, total])

  const table = useMemo<TableView>(
    () => ({
      columns: [
        { key: "name", label: "Project" },
        { key: "hours", label: "Active", align: "right" },
        { key: "share", label: "Share", align: "right" },
        { key: "sessions", label: "Sessions", align: "right" },
        { key: "threads", label: "Threads", align: "right" },
      ],
      rows: [...projects.projects]
        .sort((a, b) => b.activeMs - a.activeMs)
        .map((p) => ({
          name: p.name,
          hours: `${formatHours(p.activeMs)} h`,
          share: formatPercent(p.activeMs, total),
          sessions: formatCount(p.sessions),
          threads: formatCount(p.threads),
        })),
      note: "Every project in the filtered range, not only the top rows the chart shows.",
    }),
    [projects, total],
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
          {projects.projects.length > TOP_N
            ? `Top ${TOP_N} projects by active time; the other ${projects.projects.length - TOP_N} share one row.`
            : projects.projects.length === 1
              ? "The one project in view."
              : `All ${projects.projects.length} projects in view, by active time.`}{" "}
          Colours are the swimlane&rsquo;s: the five largest projects all-time
          keep a hue, everything else is gray, whatever the filter.
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
