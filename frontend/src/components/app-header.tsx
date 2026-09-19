import { Activity, Database, Radio } from "lucide-react"

import { ThemeToggle } from "@/components/theme-toggle"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import type { DataMode } from "@/lib/api"
import { formatRange } from "@/lib/format"
import type { Meta } from "@/lib/types"

export function AppHeader({ meta, mode }: { meta: Meta | null; mode: DataMode }) {
  return (
    <header className="border-b border-rule">
      <div className="mx-auto flex max-w-[110rem] flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3 sm:px-6 lg:px-8">
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="grid size-7 shrink-0 place-items-center rounded-[5px] bg-primary text-primary-foreground">
            <Activity className="size-4" strokeWidth={2.5} />
          </span>
          <span className="font-mono text-[0.9375rem] font-semibold tracking-[-0.01em] whitespace-nowrap">
            cc<span className="text-primary">·</span>insights
          </span>
        </div>

        <div className="order-3 flex min-w-0 basis-full items-center gap-2 text-xs sm:order-none sm:basis-auto">
          <span className="text-muted-foreground/70">on</span>
          <span className="num truncate font-medium" title={meta?.hostname ?? ""}>
            {meta?.hostname ?? "—"}
          </span>
          <span aria-hidden className="text-border">|</span>
          <span className="num whitespace-nowrap text-muted-foreground">
            {meta ? formatRange(meta.firstTs, meta.lastTs) : "—"}
          </span>
        </div>

        <div className="ml-auto flex shrink-0 items-center gap-1.5">
          {meta ? <IdleThresholdBadge seconds={meta.idleThresholdS} /> : null}
          <ModeBadge mode={mode} />
          <ThemeToggle />
        </div>
      </div>
    </header>
  )
}

/**
 * Every duration on this page is defined by this number: a gap longer than it
 * ends a span, and the gap itself contributes nothing. It stays on screen.
 */
export function IdleThresholdBadge({ seconds }: { seconds: number }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          className="inline-flex items-center gap-1.5 rounded-full border border-dashed px-2.5 py-1 text-[0.6875rem] text-muted-foreground transition-colors hover:border-solid hover:text-foreground"
        >
          <Radio className="size-3" />
          <span className="num">idle &gt; {seconds}s</span>
        </button>
      </TooltipTrigger>
      <TooltipContent className="max-w-64">
        Idle threshold: {seconds} s. A gap longer than this ends a span and
        contributes zero time — it is never capped and never counted.
      </TooltipContent>
    </Tooltip>
  )
}

function ModeBadge({ mode }: { mode: DataMode }) {
  const live = mode === "live"
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          className="hidden items-center gap-1.5 rounded-full border px-2.5 py-1 text-[0.6875rem] text-muted-foreground sm:inline-flex"
        >
          <Database className="size-3" />
          <span>{live ? "live" : "fixtures"}</span>
        </button>
      </TooltipTrigger>
      <TooltipContent className="max-w-64">
        {live
          ? "Reading the local cci server; filters are applied server-side."
          : "Reading the committed capture in src/fixtures. Filters are recomputed in the browser. Set VITE_API_URL to read a live database."}
      </TooltipContent>
    </Tooltip>
  )
}
