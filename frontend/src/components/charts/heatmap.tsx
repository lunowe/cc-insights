import { useMemo, useState } from "react"

import { formatHours } from "@/lib/format"
import type { Heatmap } from "@/lib/types"

import { ChartFrame, type TableView } from "./chart-frame"
import { HoverTip, TipRow, localPoint, type Tip } from "./hover-tip"
import { useMeasure } from "./use-measure"

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
const DAYS_LONG = [
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
  "Sunday",
]
const HOURS = Array.from({ length: 24 }, (_, h) => h)

export function WeekHeatmap({ heatmap }: { heatmap: Heatmap }) {
  const [ref, width] = useMeasure<HTMLDivElement>()
  const [tip, setTip] = useState<Tip | null>(null)

  const { grid, max, peak, rowTotals } = useMemo(() => {
    const grid: number[][] = DAYS.map(() => HOURS.map(() => 0))
    for (const c of heatmap.cells) {
      if (c.weekday >= 0 && c.weekday < 7 && c.hour >= 0 && c.hour < 24) {
        grid[c.weekday][c.hour] += c.activeMs
      }
    }
    let max = 0
    let peak: { weekday: number; hour: number } | null = null
    grid.forEach((row, w) =>
      row.forEach((v, h) => {
        if (v > max) {
          max = v
          peak = { weekday: w, hour: h }
        }
      }),
    )
    const rowTotals = grid.map((row) => row.reduce((a, b) => a + b, 0))
    return {
      grid,
      max,
      peak: peak as { weekday: number; hour: number } | null,
      rowTotals,
    }
  }, [heatmap])

  const narrow = width > 0 && width < 560
  const hourLabelEvery = narrow ? 6 : 3

  const table = useMemo<TableView>(
    () => ({
      columns: [
        { key: "day", label: "Weekday" },
        ...HOURS.map((h) => ({
          key: `h${h}`,
          label: String(h).padStart(2, "0"),
          align: "right" as const,
        })),
        { key: "total", label: "Total", align: "right" as const },
      ],
      rows: DAYS.map((d, w) => {
        const row: Record<string, string> = {
          day: d,
          total: `${formatHours(rowTotals[w])} h`,
        }
        HOURS.forEach((h) => {
          row[`h${h}`] = grid[w][h] > 0 ? formatHours(grid[w][h]) : "·"
        })
        return row
      }),
      note: "Active hours per local weekday and hour of day; a span crossing an hour boundary is split between the two hours.",
    }),
    [grid, rowTotals],
  )

  const fillFor = (v: number) => {
    if (v <= 0 || max <= 0) return "var(--muted)"
    const p = Math.round(8 + 92 * (v / max))
    return `color-mix(in oklch, var(--heat-hi) ${p}%, var(--heat-lo))`
  }

  return (
    <ChartFrame
      id="chart-heatmap"
      title="Weekday × hour"
      description={
        <>
          Active hours by local weekday and hour of day, Monday first — when in
          the week the agents actually run.
          {peak !== null ? (
            <>
              {" "}
              Busiest hour: {DAYS_LONG[peak.weekday]}{" "}
              <span className="num">
                {String(peak.hour).padStart(2, "0")}:00–
                {String(peak.hour + 1).padStart(2, "0")}:00
              </span>
              , <span className="num">{formatHours(max)} h</span> in total.
            </>
          ) : null}
        </>
      }
      table={table}
    >
      <div
        ref={ref}
        className="relative min-w-0"
        onPointerLeave={() => setTip(null)}
      >
        <div
          className="grid gap-[2px]"
          style={{
            gridTemplateColumns: `${narrow ? "2rem" : "2.5rem"} repeat(24, minmax(0, 1fr)) ${narrow ? "0" : "3.25rem"}`,
          }}
          role="img"
          aria-label="Heatmap of active hours by weekday and hour"
        >
          <div />
          {HOURS.map((h) => (
            <div
              key={h}
              className="num pb-0.5 text-[0.625rem] leading-none text-muted-foreground"
            >
              {h % hourLabelEvery === 0 ? String(h).padStart(2, "0") : ""}
            </div>
          ))}
          <div />
          {DAYS.map((d, w) => (
            <RowCells
              key={d}
              label={d}
              weekday={w}
              values={grid[w]}
              total={rowTotals[w]}
              narrow={narrow}
              isPeak={(h) =>
                peak !== null && peak.weekday === w && peak.hour === h
              }
              fillFor={fillFor}
              onEnter={(e, h) => {
                const host = ref.current
                if (host === null) return
                const p = localPoint(host, e)
                const v = grid[w][h]
                setTip({
                  x: p.x,
                  y: p.y,
                  body: (
                    <div className="grid gap-1">
                      <p className="font-medium">
                        {DAYS_LONG[w]}{" "}
                        <span className="num">
                          {String(h).padStart(2, "0")}:00–
                          {String(h + 1).padStart(2, "0")}:00
                        </span>
                      </p>
                      <TipRow
                        label="Active in this hour"
                        value={`${formatHours(v)} h`}
                      />
                      <TipRow
                        label={`All of ${d}`}
                        value={`${formatHours(rowTotals[w])} h`}
                      />
                    </div>
                  ),
                })
              }}
            />
          ))}
        </div>
        <HoverTip tip={tip} width={width} />
      </div>
      <div className="mt-3 flex items-center gap-2 text-[0.625rem] text-muted-foreground">
        <span className="num">0 h</span>
        <span
          aria-hidden
          className="h-2 w-28 rounded-[2px]"
          style={{
            background:
              "linear-gradient(90deg in oklch, var(--heat-lo), var(--heat-hi))",
          }}
        />
        <span className="num">{formatHours(max)} h</span>
        <span className="ml-2">
          per hour-of-week cell; empty cells had no activity
        </span>
      </div>
    </ChartFrame>
  )
}

function RowCells({
  label,
  weekday,
  values,
  total,
  narrow,
  isPeak,
  fillFor,
  onEnter,
}: {
  label: string
  weekday: number
  values: number[]
  total: number
  narrow: boolean
  isPeak: (hour: number) => boolean
  fillFor: (v: number) => string
  onEnter: (e: React.PointerEvent<HTMLElement>, hour: number) => void
}) {
  return (
    <>
      <div className="flex items-center text-[0.6875rem] text-muted-foreground">
        {label}
      </div>
      {values.map((v, h) => (
        <div
          key={`${weekday}-${h}`}
          tabIndex={0}
          aria-label={`${DAYS_LONG[weekday]} ${h}:00, ${formatHours(v)} hours`}
          className="h-5 rounded-[2px] outline-none focus-visible:ring-2 focus-visible:ring-ring"
          style={{
            background: fillFor(v),
            boxShadow: isPeak(h)
              ? "inset 0 0 0 1.5px var(--foreground)"
              : undefined,
          }}
          onPointerEnter={(e) => onEnter(e, h)}
          onPointerMove={(e) => onEnter(e, h)}
          onFocus={(e) =>
            onEnter(e as unknown as React.PointerEvent<HTMLElement>, h)
          }
        />
      ))}
      <div className="num flex items-center justify-end text-[0.625rem] text-muted-foreground">
        {narrow ? "" : `${formatHours(total)} h`}
      </div>
    </>
  )
}
