import { useMemo } from "react"
import { format } from "date-fns"
import { Bar, BarChart, CartesianGrid, LabelList, XAxis, YAxis } from "recharts"

import {
  ChartContainer,
  ChartTooltip,
  type ChartConfig,
} from "@/components/ui/chart"
import { SOURCE_LABEL, formatHours, formatMultiplier } from "@/lib/format"
import type { Daily, Source } from "@/lib/types"

import { ColumnTopLabel } from "./bar-label"
import { ChartFrame, Legend, type TableView } from "./chart-frame"
import { TipRow } from "./hover-tip"
import { OTHER_COLOR } from "./palette"
import { useMeasure } from "./use-measure"

const HOUR = 3_600_000

type Row = {
  key: string
  t: number
  label: string
  activeMs: number
  wallMs: number
  /** active − wall: work that only exists because threads overlapped. */
  surplusMs: number
  wallH: number
  surplusH: number
  bySource: Partial<Record<Source, number>>
  days: number
}

const config = {
  wallH: { label: "Elapsed (≥1 thread running)", color: OTHER_COLOR },
  surplusH: {
    label: "Parallel surplus (active − elapsed)",
    color: "var(--primary)",
  },
} satisfies ChartConfig

function parseLocalDate(date: string): number {
  const [y, m, d] = date.split("-").map(Number)
  return new Date(y, m - 1, d).getTime()
}

/** Monday-start week containing a local day. */
function weekStart(t: number): number {
  const d = new Date(t)
  const back = (d.getDay() + 6) % 7
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() - back).getTime()
}

/**
 * Daily rows, or Monday-start weekly rows when a day would be narrower than
 * ~3px. Summing per-day wall-clock into a week is exact: days never overlap,
 * so the union of a week's spans is the sum of its days' unions.
 */
function bucket(days: Daily["days"], weekly: boolean): Row[] {
  const acc = new Map<number, Row>()
  for (const d of days) {
    const t = parseLocalDate(d.date)
    const k = weekly ? weekStart(t) : t
    let r = acc.get(k)
    if (r === undefined) {
      r = {
        key: String(k),
        t: k,
        label: weekly
          ? `w/c ${format(k, "d MMM")}`
          : format(k, "EEE d MMM yyyy"),
        activeMs: 0,
        wallMs: 0,
        surplusMs: 0,
        wallH: 0,
        surplusH: 0,
        bySource: {},
        days: 0,
      }
      acc.set(k, r)
    }
    r.activeMs += d.activeMs
    r.wallMs += d.wallMs
    r.days += 1
    for (const s of Object.keys(d.bySource) as Source[]) {
      r.bySource[s] = (r.bySource[s] ?? 0) + (d.bySource[s] ?? 0)
    }
  }
  const rows = [...acc.values()].sort((a, b) => a.t - b.t)
  for (const r of rows) {
    r.surplusMs = Math.max(0, r.activeMs - r.wallMs)
    r.wallH = r.wallMs / HOUR
    r.surplusH = r.surplusMs / HOUR
  }
  return rows
}

export function DailyActiveChart({
  daily,
  idleThresholdS,
}: {
  daily: Daily
  idleThresholdS: number
}) {
  const [ref, width] = useMeasure<HTMLDivElement>()
  const dayCount = daily.days.length
  const weekly = width > 0 && (width - 44) / Math.max(dayCount, 1) < 3
  const rows = useMemo(() => bucket(daily.days, weekly), [daily.days, weekly])

  const max = rows.reduce<Row | null>(
    (m, r) => (m === null || r.activeMs > m.activeMs ? r : m),
    null,
  )
  const parallelDays = daily.days.filter((d) => d.activeMs > d.wallMs).length
  const activeDays = daily.days.filter((d) => d.activeMs > 0).length
  // The busiest single day, stated regardless of bucketing so a weekly view
  // never hides the day-level fact.
  const busiestDay = daily.days.reduce<Daily["days"][number] | null>(
    (m, d) => (m === null || d.activeMs > m.activeMs ? d : m),
    null,
  )

  const table = useMemo<TableView>(
    () => ({
      columns: [
        { key: "label", label: weekly ? "Week" : "Day" },
        { key: "active", label: "Active", align: "right" },
        { key: "wall", label: "Elapsed", align: "right" },
        { key: "ratio", label: "Ratio", align: "right" },
        { key: "claude", label: "Claude Code", align: "right" },
        { key: "codex", label: "Codex", align: "right" },
      ],
      rows: rows
        .filter((r) => r.activeMs > 0)
        .map((r) => ({
          label: r.label,
          active: `${formatHours(r.activeMs)} h`,
          wall: `${formatHours(r.wallMs)} h`,
          ratio: r.wallMs > 0 ? formatMultiplier(r.activeMs / r.wallMs) : "—",
          claude: `${formatHours(r.bySource.claude_code ?? 0)} h`,
          codex: `${formatHours(r.bySource.codex ?? 0)} h`,
        })),
      note: "Hours per local calendar day. Elapsed is the union of that day's spans; active is their sum.",
    }),
    [rows, weekly],
  )

  const tickEvery = Math.max(1, Math.ceil(rows.length / (width < 480 ? 4 : 7)))

  return (
    <ChartFrame
      id="chart-daily-active"
      title="Daily active hours"
      description={
        <>
          Active time per local day, split into elapsed time (at least one
          thread running) and the surplus above it. A bar with an amber cap is a
          day agents ran in parallel.{" "}
          <span className="num">{parallelDays}</span> of{" "}
          <span className="num">{activeDays}</span> active days did.
          {busiestDay !== null && busiestDay.activeMs > 0 ? (
            <>
              {" "}
              Busiest day:{" "}
              {format(parseLocalDate(busiestDay.date), "EEE d MMM yyyy")},{" "}
              <span className="num">{formatHours(busiestDay.activeMs)} h</span>{" "}
              of work in{" "}
              <span className="num">{formatHours(busiestDay.wallMs)} h</span>{" "}
              elapsed.
            </>
          ) : null}
        </>
      }
      table={table}
      footer={
        <span>
          {weekly
            ? "Bucketed by Monday-start week because a day would be under 3px wide. "
            : ""}
          A span ends after <span className="num">{idleThresholdS} s</span>{" "}
          without an event; idle gaps count zero.
        </span>
      }
    >
      <div ref={ref} className="min-w-0">
        <ChartContainer config={config} className="aspect-auto h-52 w-full">
          <BarChart
            data={rows}
            margin={{ top: 18, right: 8, left: 0, bottom: 0 }}
            barCategoryGap={weekly ? "18%" : "12%"}
          >
            <CartesianGrid vertical={false} stroke="var(--rule)" />
            <XAxis
              dataKey="key"
              tickLine={false}
              axisLine={false}
              interval={tickEvery - 1}
              tick={{ fontSize: 10 }}
              tickFormatter={(v: string) => format(Number(v), "d MMM")}
              tickMargin={6}
            />
            <YAxis
              tickLine={false}
              axisLine={false}
              width={40}
              tick={{ fontSize: 10 }}
              tickFormatter={(v: number) => `${v} h`}
            />
            <ChartTooltip
              cursor={{ fill: "var(--muted)", fillOpacity: 0.7 }}
              content={(p) => {
                const row = p.payload?.[0]?.payload as Row | undefined
                if (!p.active || row === undefined) return null
                return <DailyTip row={row} />
              }}
            />
            <Bar
              dataKey="wallH"
              stackId="a"
              fill="var(--color-wallH)"
              stroke="var(--card)"
              strokeWidth={weekly ? 1 : 0}
              isAnimationActive={false}
              maxBarSize={24}
            />
            <Bar
              dataKey="surplusH"
              stackId="a"
              fill="var(--color-surplusH)"
              stroke="var(--card)"
              strokeWidth={weekly ? 1 : 0}
              radius={[2, 2, 0, 0]}
              isAnimationActive={false}
              maxBarSize={24}
            >
              {max !== null && max.activeMs > 0 ? (
                <LabelList
                  dataKey="key"
                  content={(p) => (
                    <ColumnTopLabel
                      props={p}
                      only={max.key}
                      text={`${formatHours(max.activeMs)} h in ${formatHours(max.wallMs)} h`}
                      plotRight={width - 8}
                    />
                  )}
                />
              ) : null}
            </Bar>
          </BarChart>
        </ChartContainer>
      </div>
      <Legend
        className="mt-2"
        items={[
          { label: config.wallH.label, color: config.wallH.color },
          { label: config.surplusH.label, color: config.surplusH.color },
        ]}
      />
    </ChartFrame>
  )
}

function DailyTip({ row }: { row: Row }) {
  return (
    <div className="grid min-w-52 gap-1 rounded-md border border-border/60 bg-popover px-2.5 py-2 text-[0.75rem] text-popover-foreground shadow-lg">
      <p className="font-medium">{row.label}</p>
      <TipRow
        label="Active (sum of spans)"
        value={`${formatHours(row.activeMs)} h`}
      />
      <TipRow
        swatch={config.wallH.color}
        label="Elapsed (≥1 running)"
        value={`${formatHours(row.wallMs)} h`}
      />
      <TipRow
        swatch={config.surplusH.color}
        label="Parallel surplus"
        value={`${formatHours(row.surplusMs)} h`}
      />
      {row.wallMs > 0 ? (
        <TipRow
          label="Active ÷ elapsed"
          value={formatMultiplier(row.activeMs / row.wallMs)}
        />
      ) : null}
      {(Object.keys(row.bySource) as Source[]).map((s) => (
        <TipRow
          key={s}
          label={SOURCE_LABEL[s]}
          value={`${formatHours(row.bySource[s] ?? 0)} h`}
        />
      ))}
    </div>
  )
}
