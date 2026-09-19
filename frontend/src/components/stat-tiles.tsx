import type { ReactNode } from "react"
import { cn } from "cn"

import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import {
  formatCount,
  formatDateTime,
  formatHours,
  formatMultiplier,
  formatPercent,
} from "@/lib/format"
import type { Concurrency, Daily, Summary } from "@/lib/types"

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
  hint?: string
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
      <TooltipContent className="max-w-64">{hint}</TooltipContent>
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
