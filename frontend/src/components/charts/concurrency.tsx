import { useMemo } from "react"
import { Bar, BarChart, Cell, LabelList, XAxis, YAxis } from "recharts"

import {
  ChartContainer,
  ChartTooltip,
  type ChartConfig,
} from "@/components/ui/chart"
import { formatHours, formatPercent } from "@/lib/format"
import type { Concurrency } from "@/lib/types"

import { BarEndLabel } from "./bar-label"
import { ChartFrame, Legend, type TableView } from "./chart-frame"
import { useMeasure } from "./use-measure"
import { TipRow } from "./hover-tip"
import { OTHER_COLOR } from "./palette"

const HOUR = 3_600_000

type Row = {
  level: number
  label: string
  ms: number
  hours: number
  share: number
  tip: string
}

const config = { hours: { label: "Elapsed hours" } } satisfies ChartConfig

export function ConcurrencyChart({
  concurrency,
}: {
  concurrency: Concurrency
}) {
  const [ref, width] = useMeasure<HTMLDivElement>()
  const narrow = width > 0 && width < 480
  const rows = useMemo<Row[]>(() => {
    const levels = Object.keys(concurrency.timeAtLevel).map(Number)
    const top = Math.max(concurrency.peak, ...levels, 1)
    const out: Row[] = []
    for (let level = 1; level <= top; level++) {
      const ms = concurrency.timeAtLevel[String(level)] ?? 0
      const share = concurrency.wallMs > 0 ? ms / concurrency.wallMs : 0
      out.push({
        level,
        label: level === 1 ? "1 thread" : `${level} threads`,
        ms,
        hours: ms / HOUR,
        share,
        tip: `${formatHours(ms)} h · ${formatPercent(ms, concurrency.wallMs)}`,
      })
    }
    return out
  }, [concurrency])

  const parallelMs = rows
    .filter((r) => r.level >= 2)
    .reduce((n, r) => n + r.ms, 0)

  const table = useMemo<TableView>(
    () => ({
      columns: [
        { key: "label", label: "Threads at once" },
        { key: "hours", label: "Elapsed", align: "right" },
        { key: "share", label: "Share", align: "right" },
      ],
      rows: rows.map((r) => ({
        label: r.label,
        hours: `${formatHours(r.ms)} h`,
        share: formatPercent(r.ms, concurrency.wallMs),
      })),
      note: `Sweep line over the filtered spans. Shares are of the ${formatHours(concurrency.wallMs)} h with at least one thread running.`,
    }),
    [rows, concurrency.wallMs],
  )

  const height = rows.length * 26 + 12

  return (
    <ChartFrame
      id="chart-concurrency"
      title="Concurrency distribution"
      description={
        <>
          How the{" "}
          <span className="num">{formatHours(concurrency.wallMs)} h</span> with
          at least one thread running split by how many were running at once.{" "}
          <span className="num">{formatHours(parallelMs)} h</span> (
          <span className="num">
            {formatPercent(parallelMs, concurrency.wallMs)}
          </span>
          ) had two or more — that is where{" "}
          <span className="num">{formatHours(concurrency.activeMs)} h</span> of
          work fit into the elapsed time.
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
              dataKey="label"
              width={narrow ? 72 : 84}
              tickLine={false}
              axisLine={false}
              tick={{ fontSize: 11 }}
              interval={0}
            />
            <ChartTooltip
              cursor={{ fill: "var(--muted)", fillOpacity: 0.7 }}
              content={(p) => {
                const row = p.payload?.[0]?.payload as Row | undefined
                if (!p.active || row === undefined) return null
                return (
                  <div className="grid min-w-44 gap-1 rounded-md border border-border/60 bg-popover px-2.5 py-2 text-[0.75rem] text-popover-foreground shadow-lg">
                    <p className="font-medium">{row.label} running at once</p>
                    <TipRow
                      label="Elapsed time"
                      value={`${formatHours(row.ms)} h`}
                    />
                    <TipRow
                      label="Share of elapsed"
                      value={formatPercent(row.ms, concurrency.wallMs)}
                    />
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
                <Cell
                  key={r.level}
                  fill={r.level >= 2 ? "var(--primary)" : OTHER_COLOR}
                />
              ))}
              <LabelList dataKey="tip" content={BarEndLabel} />
            </Bar>
          </BarChart>
        </ChartContainer>
      </div>
      <Legend
        className="mt-2"
        items={[
          { label: "One thread — sequential", color: OTHER_COLOR },
          { label: "Two or more — parallel", color: "var(--primary)" },
        ]}
      />
    </ChartFrame>
  )
}
