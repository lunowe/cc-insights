import { useMemo } from "react"
import { Bar, BarChart, Cell, LabelList, XAxis, YAxis } from "recharts"

import {
  ChartContainer,
  ChartTooltip,
  type ChartConfig,
} from "@/components/ui/chart"
import { groupAgentsForDisplay } from "@/lib/derive"
import {
  SOURCE_LABEL,
  formatCount,
  formatHours,
  formatPercent,
} from "@/lib/format"
import type { Agents } from "@/lib/types"

import { BarEndLabel } from "./bar-label"
import { ChartFrame, Legend, type TableView } from "./chart-frame"
import { TipRow } from "./hover-tip"
import { OTHER_COLOR } from "./palette"
import { useMeasure } from "./use-measure"

const HOUR = 3_600_000
const INK = "var(--chart-ink)"

type Row = ReturnType<typeof groupAgentsForDisplay>[number] & {
  hours: number
  tip: string
}

const config = { hours: { label: "Active hours" } } satisfies ChartConfig

export function AgentsChart({ agents }: { agents: Agents }) {
  const [ref, width] = useMeasure<HTMLDivElement>()
  const grouped = useMemo(() => groupAgentsForDisplay(agents.agents), [agents])
  const total = grouped.reduce((n, r) => n + r.activeMs, 0)
  const rows = useMemo<Row[]>(
    () =>
      grouped.map((r) => ({
        ...r,
        hours: r.activeMs / HOUR,
        tip: `${formatHours(r.activeMs)} h · ${formatPercent(r.activeMs, total)}`,
      })),
    [grouped, total],
  )
  const codex = rows.find((r) => r.isNicknameGroup)
  const codexNicknames = agents.agents.filter(
    (a) => a.source === "codex",
  ).length

  const table = useMemo<TableView>(
    () => ({
      columns: [
        { key: "label", label: "Agent type" },
        { key: "source", label: "Source" },
        { key: "threads", label: "Threads", align: "right" },
        { key: "hours", label: "Active", align: "right" },
        { key: "share", label: "Share", align: "right" },
      ],
      rows: rows.map((r) => ({
        label: r.isNicknameGroup ? "Codex (all nicknames)" : r.label,
        source: SOURCE_LABEL[r.source],
        threads: formatCount(r.threads),
        hours: `${formatHours(r.activeMs)} h`,
        share: formatPercent(r.activeMs, total),
      })),
      note: "Only threads that carry an agent name appear here. Root threads record none.",
    }),
    [rows, total],
  )

  const narrow = width > 0 && width < 480
  const labelW = narrow ? 100 : 136
  const maxChars = Math.floor((labelW - 10) / 6.8)
  const truncate = (s: string) =>
    s.length <= maxChars ? s : `${s.slice(0, maxChars - 1)}…`
  const height = Math.max(rows.length, 1) * 26 + 12

  return (
    <ChartFrame
      id="chart-agents"
      title="Agent breakdown"
      description={
        <>
          Active time by the agent type a thread records. Claude Code names a
          real type; Codex names each thread after a random scientist, so
          {codex ? (
            <>
              {" "}
              its <span className="num">
                {formatCount(codexNicknames)}
              </span>{" "}
              nicknames collapse into one row
            </>
          ) : (
            " its nicknames collapse into one row"
          )}
          . Root threads carry no agent type and are not in this chart.
        </>
      }
      table={table}
    >
      {rows.length === 0 ? (
        <p className="py-8 text-center text-[0.8125rem] text-muted-foreground">
          No named agent threads in this view.
        </p>
      ) : (
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
                        swatch={row.isNicknameGroup ? OTHER_COLOR : INK}
                        label={
                          row.isNicknameGroup
                            ? "Codex, all nicknames"
                            : row.label
                        }
                        value={`${formatHours(row.activeMs)} h`}
                      />
                      <TipRow label="Source" value={SOURCE_LABEL[row.source]} />
                      <TipRow
                        label="Threads"
                        value={formatCount(row.threads)}
                      />
                      <TipRow
                        label="Share"
                        value={formatPercent(row.activeMs, total)}
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
                    key={`${r.source}-${r.label}`}
                    fill={r.isNicknameGroup ? OTHER_COLOR : INK}
                  />
                ))}
                <LabelList dataKey="tip" content={BarEndLabel} />
              </Bar>
            </BarChart>
          </ChartContainer>
          <Legend
            className="mt-2"
            items={[
              { label: "Claude Code agent type", color: INK },
              { label: "Codex — nicknames, not a type", color: OTHER_COLOR },
            ]}
          />
        </div>
      )}
    </ChartFrame>
  )
}
