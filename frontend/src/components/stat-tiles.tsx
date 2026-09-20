import type { ReactNode } from "react"
import { cn } from "cn"

import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import {
  SOURCE_LABEL,
  currencyUnit,
  formatCompact,
  formatCost,
  formatCount,
  formatDateTime,
  formatHours,
  formatMultiplier,
  formatPercent,
} from "@/lib/format"
import type { Concurrency, Cost, Daily, Summary } from "@/lib/types"

function Tile({
  label,
  value,
  unit,
  sub,
  hint,
  className,
  size = "md",
}: {
  label: string
  value: ReactNode
  unit?: string
  sub?: ReactNode
  hint?: ReactNode
  className?: string
  size?: "md" | "lg"
}) {
  const body = (
    <div
      className={cn(
        "flex flex-col justify-between gap-3 border-t border-l p-4 sm:p-5",
        className,
      )}
    >
      <p className="eyebrow">{label}</p>
      <p className="flex items-baseline gap-1.5">
        <span
          data-slot="stat-value"
          className={cn(
            "num leading-none font-medium tracking-[-0.03em]",
            size === "lg" ? "text-[2.75rem] sm:text-6xl" : "text-[1.75rem]",
          )}
        >
          {value}
        </span>
        {unit ? (
          <span className="text-sm text-muted-foreground">{unit}</span>
        ) : null}
      </p>
      {sub ? (
        <p className="text-[0.75rem] leading-snug text-muted-foreground">
          {sub}
        </p>
      ) : (
        <p aria-hidden className="text-[0.75rem] leading-snug">
          &nbsp;
        </p>
      )}
    </div>
  )

  if (hint === undefined) return body
  return (
    <Tooltip>
      <TooltipTrigger asChild>{body}</TooltipTrigger>
      <TooltipContent className="max-w-72">{hint}</TooltipContent>
    </Tooltip>
  )
}

function Panel({
  title,
  note,
  children,
  cols,
}: {
  title: string
  note?: ReactNode
  children: ReactNode
  cols: string
}) {
  return (
    <section>
      <div className="mb-2.5 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="eyebrow text-foreground">{title}</h2>
        {note ? (
          <p className="text-[0.75rem] text-muted-foreground">{note}</p>
        ) : null}
      </div>
      <div
        className={cn(
          "grid overflow-hidden rounded-lg border-r border-b bg-card",
          cols,
        )}
      >
        {children}
      </div>
    </section>
  )
}

export function ActivityTiles({
  summary,
  concurrency,
  daily,
}: {
  summary: Summary
  concurrency: Concurrency
  daily: Daily
}) {
  const activeDays = daily.days.filter((d) => d.activeMs > 0).length

  return (
    <Panel title="Active time" cols="grid-cols-2 lg:grid-cols-6">
      <Tile
        size="lg"
        className="col-span-2"
        label="Total active time"
        value={formatHours(summary.activeMs)}
        unit="hours"
        sub={
          <>
            {formatCount(summary.spans)} spans over {formatCount(activeDays)}{" "}
            {activeDays === 1 ? "day" : "days"} with activity
          </>
        }
        hint="The sum of span durations. A gap longer than the idle threshold ends a span and contributes nothing — it is never capped and never counted."
      />
      <Tile
        label="Sessions"
        value={formatCount(summary.sessions)}
        sub="Reachable from the spans in view"
      />
      <Tile
        label="Threads"
        value={formatCount(summary.threads)}
        sub="Root and subagent conversations"
      />
      <Tile
        label="Peak concurrency"
        value={formatCount(concurrency.peak)}
        unit={concurrency.peak === 1 ? "thread" : "threads"}
        sub={
          concurrency.peakAt === null
            ? "No overlap in view"
            : `First reached ${formatDateTime(concurrency.peakAt)}`
        }
        hint="The largest number of threads that were active at the same instant."
      />
      <Tile
        label="Parallelism"
        value={formatMultiplier(concurrency.multiplier)}
        sub={
          <>
            {formatHours(concurrency.activeMs)} h of work inside{" "}
            {formatHours(concurrency.wallMs)} h of wall clock
          </>
        }
        hint="Active time divided by wall-clock time with at least one thread running. 1.00× means everything ran one at a time."
      />
    </Panel>
  )
}

export function OriginTiles({ summary }: { summary: Summary }) {
  const total = summary.activeMs
  const buckets = [
    {
      label: "Human-initiated",
      ms: summary.humanInitiatedMs,
      sub: "A root thread whose opening turn a person typed.",
    },
    {
      label: "Autonomous",
      ms: summary.autonomousMs,
      sub: "A subagent thread: a model spawned it. Structural, not a claim about who was watching.",
    },
    {
      label: "Unattended root",
      ms: summary.unattendedRootMs,
      sub: "A root thread that resumed with no human turn in front of it.",
    },
  ]

  return (
    <Panel
      title="How the work began"
      note={
        <>
          These three partition the {formatHours(total)} h exactly — no span is
          counted twice.
        </>
      }
      cols="grid-cols-1 sm:grid-cols-3"
    >
      {buckets.map((b) => (
        <Tile
          key={b.label}
          label={b.label}
          value={formatHours(b.ms)}
          unit={`h · ${formatPercent(b.ms, total)}`}
          sub={b.sub}
        />
      ))}
    </Panel>
  )
}

/** `anthropic/claude-fable-5` → `claude-fable-5`; the provider is noise in a tile. */
const shortModel = (id: string) => id.slice(id.lastIndexOf("/") + 1)

/**
 * The list-price equivalent and the three things that qualify it.
 *
 * The total is what this traffic would have cost at published API rates and
 * never what was paid — a subscription charges a flat fee however many tokens
 * run through it. That sentence sits in the panel header rather than a
 * tooltip so it is in every screenshot the number is in. The three tiles
 * beside it are the caveats `docs/API.md` makes machine-readable: tokens no
 * rate covered, money resting on a relative's rates, and events whose model
 * was inferred. Each is a number, because "some of this is uncertain" is not
 * something a reader can act on and "$6,414 of it is" is.
 */
export function CostTiles({
  cost,
  unavailable,
}: {
  cost: Cost
  /** `DashboardData.eventFactsUnfiltered`: the figure ignores the filter. */
  unavailable: boolean
}) {
  const { currency } = cost
  const unit = currencyUnit(currency)
  const money = (n: number) => formatCost(n, currency)

  const approximated = new Set(cost.approximations.map((a) => a.model))
  const approxCost = cost.byModel
    .filter((m) => approximated.has(m.model))
    .reduce((n, m) => n + m.cost, 0)
  const unpriced = [...cost.unpriced].sort((a, b) => b.tokens - a.tokens)
  const unpricedModels = unpriced.filter((u) => u.model !== null).length
  const topUnpriced = unpriced[0]

  if (unavailable) {
    return (
      <Panel
        title="Cost at list price"
        note="Sample data can only be priced whole — clear the filters to see the all-time figure."
        cols="grid-cols-2 lg:grid-cols-6"
      >
        <Tile
          size="lg"
          className="col-span-2"
          label="List-price equivalent"
          value="—"
          sub="Cost is a per-event fact the bundled sample cannot narrow by span. It is not zero; it is not shown."
        />
        <Tile label="Unpriced tokens" value="—" sub="Unavailable under a filter in sample mode." />
        <Tile label="Priced as a relative" value="—" sub="Unavailable under a filter in sample mode." />
        <Tile label="Attributed events" value="—" sub="Unavailable under a filter in sample mode." />
      </Panel>
    )
  }

  return (
    <Panel
      title="Cost at list price"
      note="What this traffic would have cost at published API rates. Not a bill: a subscription charges a flat fee however many tokens run through it."
      cols="grid-cols-2 lg:grid-cols-6"
    >
      <Tile
        size="lg"
        className="col-span-2"
        label="List-price equivalent"
        value={money(cost.total)}
        unit={unit ?? undefined}
        sub={
          <>
            {formatCount(cost.pricedEvents)} priced events at published API
            rates. A comparison figure for projects, models and months — not
            what was paid.
          </>
        }
        hint={
          <>
            Tokens × the published per-token rate for each model and token
            component, summed. Cache reads and writes are priced at their own
            rates, not folded into input.
            {cost.bySource.length > 1 ? (
              <>
                {" "}
                By source:{" "}
                {cost.bySource.map((s, i) => (
                  <span key={s.source}>
                    {i > 0 ? " · " : ""}
                    <span className="num">{money(s.cost)}</span>{" "}
                    {SOURCE_LABEL[s.source]}
                  </span>
                ))}
                .
              </>
            ) : null}
          </>
        }
      />
      <Tile
        label="Unpriced tokens"
        value={formatCompact(cost.unpricedTokens)}
        unit="tokens"
        sub={
          cost.unpricedTokens === 0 ? (
            "Every token in view had a rate."
          ) : (
            <>
              No rate covered them — unknown, not free.
              {topUnpriced !== undefined ? (
                <>
                  {" "}
                  {unpricedModels > 1
                    ? `${unpricedModels} models, mostly `
                    : "Mostly "}
                  {topUnpriced.model ?? "events naming no model"} (
                  <span className="num">{formatCompact(topUnpriced.tokens)}</span>
                  ).
                </>
              ) : null}
            </>
          )
        }
        hint={
          unpriced.length === 0 ? undefined : (
            <>
              Not in the total, and not zero-cost — there is simply no rate on
              file.{" "}
              {unpriced.map((u, i) => (
                <span key={`${u.model ?? ""}-${u.reason}`}>
                  {i > 0 ? " · " : ""}
                  {u.model ?? "no model named"}{" "}
                  <span className="num">{formatCompact(u.tokens)}</span>
                </span>
              ))}
            </>
          )
        }
      />
      <Tile
        label="Priced as a relative"
        value={cost.approximations.length === 0 ? "0" : money(approxCost)}
        unit={
          cost.approximations.length === 0
            ? "models"
            : `${formatPercent(approxCost, cost.total)} of total`
        }
        sub={
          cost.approximations.length === 0 ? (
            "Every priced model in view has a rate of its own."
          ) : (
            <>
              {cost.approximations.map((a, i) => (
                <span key={a.model}>
                  {i > 0 ? ", " : ""}
                  <span className="text-foreground">{a.model}</span> at{" "}
                  {shortModel(a.pricedAs)} rates
                </span>
              ))}
              . The catalog has none of their own, so this much of the total
              could sit well off the true figure.
            </>
          )
        }
        hint="A model with no published rate is priced at its nearest relative's. Defensible as a default, and a real source of error: relatives can differ several-fold on cache-read pricing, which is most of the bill."
      />
      <Tile
        label="Attributed events"
        value={formatCount(cost.attributedEvents)}
        unit={`of ${formatCount(cost.pricedEvents)}`}
        sub="Priced off a model carried forward from earlier in the thread — Codex logs usage without naming one."
        hint="Codex records token usage on events that name no model; the model is on an earlier event in the same thread. Each thread is walked in order carrying the last model seen forward. This is how many priced events rest on that inference."
      />
    </Panel>
  )
}
