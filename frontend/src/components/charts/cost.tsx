import { useMemo } from "react"
import type { CartesianViewBox, LabelProps } from "recharts"
import { Bar, BarChart, Cell, LabelList, XAxis, YAxis } from "recharts"

import {
  ChartContainer,
  ChartTooltip,
  type ChartConfig,
} from "@/components/ui/chart"
import {
  formatCompact,
  formatCost,
  formatCostCompact,
  formatCount,
  formatPercent,
} from "@/lib/format"
import type { Cost, Summary, UnpricedReason } from "@/lib/types"

import { BarEndLabel } from "./bar-label"
import { ChartFrame, Legend, type TableView } from "./chart-frame"
import { TipRow } from "./hover-tip"
import { OTHER_COLOR } from "./palette"
import { useMeasure } from "./use-measure"

const INK = "var(--chart-ink)"
const TOP_N = 8

type ComponentKey = keyof Cost["byComponent"]

/**
 * The four token components in a FIXED order, each on its categorical slot.
 * Order is the CVD-safety mechanism and never follows the values: the two
 * bars below share it so a segment sits under its twin and the difference in
 * width is the whole point.
 */
const COMPONENTS: { key: ComponentKey; label: string; color: string }[] = [
  { key: "cacheRead", label: "Cache reads", color: "var(--chart-1)" },
  // Two prices for the same tokens: a write that lives an hour costs 2x base
  // input, one that lives five minutes 1.25x. Adjacent and in that order so
  // the pair reads as one thing split, which is what it is.
  { key: "cacheWrite1h", label: "Cache writes · 1h", color: "var(--chart-2)" },
  { key: "cacheWrite", label: "Cache writes · 5m", color: "var(--chart-5)" },
  { key: "output", label: "Output", color: "var(--chart-3)" },
  { key: "input", label: "Fresh input", color: "var(--chart-4)" },
]

const UNPRICED_REASON: Record<UnpricedReason, string> = {
  no_rate: "no rate on file",
  no_model: "no model named in the thread",
  no_component: "no rate for this token component",
}

/** `anthropic/claude-fable-5` → `claude-fable-5`. */
const shortModel = (id: string) => id.slice(id.lastIndexOf("/") + 1)

/** One 100 % bar: each component's share of that bar's whole. */
type ShareRow = {
  key: "tokens" | "cost"
  label: string
  whole: number
  cacheRead: number
  cacheWrite: number
  cacheWrite1h: number
  output: number
  input: number
}

type ModelRow = {
  id: string
  model: string
  label: string
  cost: number
  events: number
  attributed: number
  pricedAs: string | null
  color: string
  tip: string
  isTail: boolean
  tailCount: number
}

const shareConfig = Object.fromEntries(
  COMPONENTS.map((c) => [c.key, { label: c.label, color: c.color }]),
) satisfies ChartConfig

const modelConfig = { cost: { label: "List price" } } satisfies ChartConfig

/**
 * A percentage inside a stacked segment, drawn only when it fits with room
 * to spare. A segment too thin for its label keeps the value in the tooltip
 * and the table; nothing is ever clipped.
 */
function SegmentLabel(props: LabelProps) {
  const vb = props.viewBox as CartesianViewBox | undefined
  if (vb === undefined || vb.x === undefined || vb.y === undefined) return null
  const w = vb.width ?? 0
  const h = vb.height ?? 0
  const share = Number(props.value)
  if (!Number.isFinite(share) || share <= 0) return null
  const text = share < 0.01 ? "<1%" : `${Math.round(share * 100)}%`
  if (w < text.length * 6.5 + 12) return null
  return (
    <text
      x={vb.x + w / 2}
      y={vb.y + h / 2}
      dy="0.35em"
      textAnchor="middle"
      fontSize={10}
      fontWeight={500}
      fill="white"
      className="num"
    >
      {text}
    </text>
  )
}

/**
 * The list-price equivalent, read two ways.
 *
 * First by token component, as two 100 % bars — share of tokens above share of
 * price — because the finding worth seeing is an inversion: cache reads are
 * the cheapest tokens of the four and still most of the bill, purely on
 * volume, while output is a rounding error in tokens and a seventh of the
 * money. Two aligned bars show that without a second axis. Then by model, the
 * natural next question, with the models priced at a relative's rates marked
 * and the unpriced tokens named in the footer — they are in none of the bars
 * and are unknown, not free.
 */
export function CostChart({
  cost,
  tokens,
  unavailable,
}: {
  cost: Cost
  /** `summary.tokens` for the same filter — the token side of the inversion. */
  tokens: Summary["tokens"]
  /** `DashboardData.eventFactsUnfiltered`: the figures ignore the filter. */
  unavailable: boolean
}) {
  const [ref, width] = useMeasure<HTMLDivElement>()
  const { currency } = cost
  const money = (n: number) => formatCost(n, currency)

  const tokenTotal =
    tokens.input + tokens.output + tokens.cacheRead + tokens.cacheWrite
  // `tokens.cacheWrite` is every cache write; the five-minute count is what
  // is left once the one-hour ones are taken out. Computed once here so the
  // bar, the tooltip and the table cannot disagree.
  const tokensBy = useMemo<Record<ComponentKey, number>>(
    () => ({
      input: tokens.input,
      output: tokens.output,
      cacheRead: tokens.cacheRead,
      cacheWrite: Math.max(0, tokens.cacheWrite - tokens.cacheWrite1h),
      cacheWrite1h: tokens.cacheWrite1h,
    }),
    [tokens],
  )
  const shares = useMemo<ShareRow[]>(() => {
    const row = (
      key: ShareRow["key"],
      label: string,
      by: Cost["byComponent"],
      whole: number,
    ): ShareRow => ({
      key,
      label,
      whole,
      cacheRead: whole > 0 ? by.cacheRead / whole : 0,
      cacheWrite: whole > 0 ? by.cacheWrite / whole : 0,
      cacheWrite1h: whole > 0 ? by.cacheWrite1h / whole : 0,
      output: whole > 0 ? by.output / whole : 0,
      input: whole > 0 ? by.input / whole : 0,
    })
    return [
      row("tokens", "Tokens", tokensBy, tokenTotal),
      row("cost", "List price", cost.byComponent, cost.total),
    ]
  }, [tokensBy, tokenTotal, cost.byComponent, cost.total])

  const pricedAs = useMemo(
    () => new Map(cost.approximations.map((a) => [a.model, a.pricedAs])),
    [cost.approximations],
  )

  const models = useMemo<ModelRow[]>(() => {
    const sorted = [...cost.byModel].sort((a, b) => b.cost - a.cost)
    const head = sorted.slice(0, TOP_N)
    const tail = sorted.slice(TOP_N)
    const out: ModelRow[] = head.map((m) => {
      const approx = pricedAs.get(m.model) ?? null
      return {
        id: m.model,
        model: m.model,
        // The ≈ is the mark for "a relative's rates"; the footer spells it out.
        label: approx === null ? m.model : `≈ ${m.model}`,
        cost: m.cost,
        events: m.events,
        attributed: m.attributed,
        pricedAs: approx,
        color: INK,
        tip: `${formatCostCompact(m.cost, currency)} · ${formatPercent(m.cost, cost.total)}`,
        isTail: false,
        tailCount: 0,
      }
    })
    if (tail.length > 0) {
      const sum = tail.reduce((n, m) => n + m.cost, 0)
      out.push({
        id: "__tail",
        model: "",
        label: `${tail.length} other model${tail.length === 1 ? "" : "s"}`,
        cost: sum,
        events: tail.reduce((n, m) => n + m.events, 0),
        attributed: tail.reduce((n, m) => n + m.attributed, 0),
        pricedAs: null,
        color: OTHER_COLOR,
        tip: `${formatCostCompact(sum, currency)} · ${formatPercent(sum, cost.total)}`,
        isTail: true,
        tailCount: tail.length,
      })
    }
    return out
  }, [cost.byModel, cost.total, currency, pricedAs])

  const unpriced = useMemo(
    () => [...cost.unpriced].sort((a, b) => b.tokens - a.tokens),
    [cost.unpriced],
  )
  const unpricedModels = unpriced.filter((u) => u.model !== null).length

  const table = useMemo<TableView>(
    () => ({
      columns: [
        { key: "kind", label: "" },
        { key: "name", label: "Component / model" },
        { key: "cost", label: "List price", align: "right" },
        { key: "share", label: "Share", align: "right" },
        { key: "tokens", label: "Tokens", align: "right" },
        { key: "events", label: "Events", align: "right" },
        { key: "note", label: "Note" },
      ],
      rows: [
        ...COMPONENTS.map((c) => ({
          kind: "Component",
          name: c.label,
          cost: money(cost.byComponent[c.key]),
          share: formatPercent(cost.byComponent[c.key], cost.total),
          tokens: formatCompact(tokensBy[c.key]),
          events: "—",
          note: `${formatPercent(tokensBy[c.key], tokenTotal)} of tokens`,
        })),
        ...[...cost.byModel]
          .sort((a, b) => b.cost - a.cost)
          .map((m) => {
            const approx = pricedAs.get(m.model)
            const notes: string[] = []
            if (approx !== undefined) notes.push(`priced as ${shortModel(approx)}`)
            if (m.attributed > 0)
              notes.push(`${formatCount(m.attributed)} attributed`)
            return {
              kind: "Model",
              name: m.model,
              cost: money(m.cost),
              share: formatPercent(m.cost, cost.total),
              tokens: "—",
              events: formatCount(m.events),
              note: notes.join(" · "),
            }
          }),
        ...unpriced.map((u) => ({
          kind: "Unpriced",
          name: u.model ?? "(no model named)",
          cost: "—",
          share: "—",
          tokens: formatCompact(u.tokens),
          events: formatCount(u.events),
          note: UNPRICED_REASON[u.reason],
        })),
      ],
      note: "List price at published API rates — not a bill. Unpriced rows had no rate, so their tokens are unknown, not free, and are in no total here.",
    }),
    // `money` closes over `currency`, which is the only thing it depends on.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [cost, tokens, tokenTotal, pricedAs, unpriced, currency],
  )

  const narrow = width > 0 && width < 480
  const labelW = narrow ? 120 : 168
  const maxChars = Math.floor((labelW - 10) / 6.8)
  const truncate = (s: string) =>
    s.length <= maxChars ? s : `${s.slice(0, maxChars - 1)}…`
  const modelHeight = Math.max(models.length, 1) * 26 + 12

  const cacheShare = formatPercent(cost.byComponent.cacheRead, cost.total)

  if (unavailable) {
    return (
      <ChartFrame
        id="chart-cost"
        title="Where the list price goes"
        description="What the traffic in view would have cost at published API rates, by token component and by model. Not a bill."
      >
        <p className="py-8 text-center text-[0.8125rem] text-muted-foreground">
          Sample data can only be priced whole. Clear the filters to see the
          all-time breakdown.
        </p>
      </ChartFrame>
    )
  }

  return (
    <ChartFrame
      id="chart-cost"
      title="Where the list price goes"
      description={
        <>
          What the traffic in view would have cost at published API rates —{" "}
          <span className="num">{money(cost.total)}</span> — split by token
          component and by model. Not a bill: a subscription charges a flat
          fee regardless.
          {cost.pricedEvents > 0 ? (
            <>
              {" "}
              Cache reads are the cheapest of the four per token and still{" "}
              <span className="num">{cacheShare}</span> of the price:{" "}
              <span className="num">{formatCompact(tokens.cacheRead)}</span>{" "}
              of them ran against{" "}
              <span className="num">{formatCompact(tokens.input)}</span> fresh
              input. Output is{" "}
              <span className="num">
                {formatPercent(cost.byComponent.output, cost.total)}
              </span>{" "}
              of the money on{" "}
              <span className="num">
                {formatPercent(tokens.output, tokenTotal)}
              </span>{" "}
              of the tokens.
            </>
          ) : null}
        </>
      }
      table={table}
      footer={
        <>
          {cost.unpricedTokens > 0 ? (
            <span>
              <span className="num text-foreground">
                {formatCompact(cost.unpricedTokens)}
              </span>{" "}
              tokens
              {unpricedModels > 0
                ? ` across ${unpricedModels} model${unpricedModels === 1 ? "" : "s"}`
                : ""}{" "}
              had no rate and sit in none of these bars — unknown, not free
              {unpriced.length > 0 ? (
                <>
                  {" "}
                  (
                  {unpriced.slice(0, 2).map((u, i) => (
                    <span key={`${u.model ?? ""}-${u.reason}`}>
                      {i > 0 ? ", " : ""}
                      {u.model ?? "no model named"}{" "}
                      <span className="num">{formatCompact(u.tokens)}</span>
                    </span>
                  ))}
                  {unpriced.length > 2 ? ", …" : ""})
                </>
              ) : null}
              .{" "}
            </span>
          ) : null}
          {cost.approximations.length > 0 ? (
            <span>
              ≈ marks a model priced at a relative&rsquo;s rates because the
              catalog has none of its own:{" "}
              {cost.approximations.map((a, i) => (
                <span key={a.model}>
                  {i > 0 ? ", " : ""}
                  {a.model} as {shortModel(a.pricedAs)}
                </span>
              ))}
              .{" "}
            </span>
          ) : null}
          {cost.attributedEvents > 0 ? (
            <span>
              <span className="num text-foreground">
                {formatCount(cost.attributedEvents)}
              </span>{" "}
              of{" "}
              <span className="num">{formatCount(cost.pricedEvents)}</span>{" "}
              priced events named no model and took the last one seen in their
              thread.
            </span>
          ) : null}
        </>
      }
    >
      {cost.pricedEvents === 0 ? (
        <p className="py-8 text-center text-[0.8125rem] text-muted-foreground">
          Nothing in view could be priced.
        </p>
      ) : (
        <div
          ref={ref}
          className="grid min-w-0 gap-x-8 gap-y-6 lg:grid-cols-[minmax(0,5fr)_minmax(0,6fr)]"
        >
          <div className="min-w-0">
            <p className="mb-2 text-[0.6875rem] font-medium text-muted-foreground">
              By token component — share of tokens, then share of price
            </p>
            <ChartContainer
              config={shareConfig}
              className="aspect-auto w-full"
              style={{ height: 84 }}
            >
              <BarChart
                data={shares}
                layout="vertical"
                margin={{ top: 4, right: 4, left: 0, bottom: 4 }}
                barCategoryGap={6}
              >
                <XAxis type="number" hide domain={[0, 1]} />
                <YAxis
                  type="category"
                  dataKey="label"
                  width={narrow ? 60 : 72}
                  tickLine={false}
                  axisLine={false}
                  tick={{ fontSize: 11 }}
                  interval={0}
                />
                <ChartTooltip
                  cursor={{ fill: "var(--muted)", fillOpacity: 0.7 }}
                  content={(p) => {
                    const row = p.payload?.[0]?.payload as ShareRow | undefined
                    if (!p.active || row === undefined) return null
                    const isCost = row.key === "cost"
                    return (
                      <div className="grid min-w-52 gap-1 rounded-md border border-border/60 bg-popover px-2.5 py-2 text-[0.75rem] text-popover-foreground shadow-lg">
                        <p className="font-medium">
                          {isCost
                            ? `${money(cost.total)} at list price`
                            : `${formatCompact(tokenTotal)} tokens`}
                        </p>
                        {COMPONENTS.map((c) => (
                          <TipRow
                            key={c.key}
                            swatch={c.color}
                            label={c.label}
                            value={
                              isCost
                                ? `${money(cost.byComponent[c.key])} · ${formatPercent(cost.byComponent[c.key], cost.total)}`
                                : `${formatCompact(tokensBy[c.key])} · ${formatPercent(tokensBy[c.key], tokenTotal)}`
                            }
                          />
                        ))}
                      </div>
                    )
                  }}
                />
                {COMPONENTS.map((c, i) => (
                  <Bar
                    key={c.key}
                    dataKey={c.key}
                    stackId="share"
                    fill={c.color}
                    stroke="var(--card)"
                    strokeWidth={1}
                    maxBarSize={22}
                    radius={i === COMPONENTS.length - 1 ? [0, 2, 2, 0] : 0}
                    isAnimationActive={false}
                  >
                    <LabelList dataKey={c.key} content={SegmentLabel} />
                  </Bar>
                ))}
              </BarChart>
            </ChartContainer>
            <Legend
              className="mt-2"
              items={COMPONENTS.map((c) => ({
                label: (
                  <>
                    {c.label}{" "}
                    <span className="num">
                      {formatCostCompact(cost.byComponent[c.key], currency)}
                    </span>
                  </>
                ),
                color: c.color,
              }))}
            />
          </div>

          <div className="min-w-0">
            <p className="mb-2 text-[0.6875rem] font-medium text-muted-foreground">
              By model
              {cost.byModel.length > TOP_N
                ? ` — top ${TOP_N}, the other ${cost.byModel.length - TOP_N} share one row`
                : ""}
            </p>
            <ChartContainer
              config={modelConfig}
              className="aspect-auto w-full"
              style={{ height: modelHeight }}
            >
              <BarChart
                data={models}
                layout="vertical"
                margin={{ top: 4, right: narrow ? 88 : 96, left: 0, bottom: 4 }}
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
                    const row = p.payload?.[0]?.payload as ModelRow | undefined
                    if (!p.active || row === undefined) return null
                    return (
                      <div className="grid min-w-52 gap-1 rounded-md border border-border/60 bg-popover px-2.5 py-2 text-[0.75rem] text-popover-foreground shadow-lg">
                        <TipRow
                          swatch={row.color}
                          label={row.isTail ? row.label : row.model}
                          value={money(row.cost)}
                        />
                        <TipRow
                          label="Share of list price"
                          value={formatPercent(row.cost, cost.total)}
                        />
                        <TipRow
                          label="Priced events"
                          value={formatCount(row.events)}
                        />
                        {row.attributed > 0 ? (
                          <TipRow
                            label="Model carried forward"
                            value={`${formatCount(row.attributed)} · ${formatPercent(row.attributed, row.events)}`}
                          />
                        ) : null}
                        {row.pricedAs !== null ? (
                          <p className="mt-0.5 text-muted-foreground">
                            Priced as {row.pricedAs}: a relative&rsquo;s
                            rates, not its own.
                          </p>
                        ) : null}
                      </div>
                    )
                  }}
                />
                <Bar
                  dataKey="cost"
                  radius={[0, 2, 2, 0]}
                  maxBarSize={20}
                  isAnimationActive={false}
                >
                  {models.map((r) => (
                    <Cell key={r.id} fill={r.color} />
                  ))}
                  <LabelList dataKey="tip" content={BarEndLabel} />
                </Bar>
              </BarChart>
            </ChartContainer>
          </div>
        </div>
      )}
    </ChartFrame>
  )
}
