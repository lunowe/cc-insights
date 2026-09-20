import { Activity, Database, FlaskConical, Radio } from "lucide-react"

import { ThemeToggle } from "@/components/theme-toggle"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import type { LiveState } from "@/hooks/use-live"
import type { DataMode } from "@/lib/api"
import { formatCount, formatDateTime, formatRange, formatSince } from "@/lib/format"
import type { Meta } from "@/lib/types"

export function AppHeader({
  meta,
  mode,
  live,
}: {
  meta: Meta | null
  /** null until the backend probe resolves. */
  mode: DataMode | null
  /** Watch mode, when a `cci watch --serve` is feeding this page. */
  live?: LiveState
}) {
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
          {live?.watching ? <WatchingBadge live={live} /> : null}
          {mode !== null ? <ModeBadge mode={mode} meta={meta} /> : null}
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

/**
 * Shown only while a watcher is actually feeding this page.
 *
 * It is a claim about the present tense, so it appears only once the stream
 * has said hello and disappears the moment it drops: a badge that says "live"
 * because it did at some point is the failure it exists to prevent. The dot
 * pulses, because the one thing it is asserting is that this is moving.
 */
function WatchingBadge({ live }: { live: LiveState }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          className="inline-flex items-center gap-1.5 rounded-full border border-primary/40 bg-primary/8 px-2.5 py-1 text-[0.6875rem] text-primary"
        >
          <span className="relative grid size-3 place-items-center">
            <span className="absolute size-2 animate-ping rounded-full bg-primary/60" />
            <span className="size-1.5 rounded-full bg-primary" />
          </span>
          <span>watching</span>
          {live.changes > 0 ? (
            <>
              <span aria-hidden className="text-primary/40">|</span>
              <span className="num whitespace-nowrap">
                {formatCount(live.changes)}
              </span>
            </>
          ) : null}
        </button>
      </TooltipTrigger>
      <TooltipContent className="max-w-72">
        <code>cci watch</code> is following the logs; this page refreshes as
        they grow, at most once every few seconds.
        {live.lastChangeAt !== null ? (
          <> Last change: {formatDateTime(live.lastChangeAt)}.</>
        ) : (
          <> Nothing has changed since this page opened.</>
        )}
      </TooltipContent>
    </Tooltip>
  )
}

/**
 * Where the numbers came from, and — when they came from a real database —
 * how fresh they are.
 *
 * "Live" over a database last ingested three weeks ago is its own quiet lie,
 * so the badge carries the newest recorded activity beside the word. And a
 * local tool showing a stranger's bundled sample is surprising enough that
 * "fixtures" will not do: it says **sample data**, in a colour that stops the
 * eye, on every screen width.
 */
function ModeBadge({ mode, meta }: { mode: DataMode; meta: Meta | null }) {
  if (mode !== "live") {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            className="inline-flex items-center gap-1.5 rounded-full border border-dashed border-primary/50 bg-primary/8 px-2.5 py-1 text-[0.6875rem] text-primary"
          >
            <FlaskConical className="size-3" />
            <span>sample data</span>
          </button>
        </TooltipTrigger>
        <TooltipContent className="max-w-72">
          Not this machine&rsquo;s data — a sample bundled into the page, so it
          renders with no server running. Start <code>cci serve</code> and open
          the address it prints to see your own.
        </TooltipContent>
      </Tooltip>
    )
  }

  const newest = meta?.lastTs ?? null
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          className="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[0.6875rem] text-muted-foreground"
        >
          <Database className="size-3 text-primary" />
          <span className="text-foreground">live</span>
          {newest !== null ? (
            <>
              <span aria-hidden className="text-border">|</span>
              <span className="num whitespace-nowrap">
                {formatSince(newest)}
              </span>
            </>
          ) : null}
        </button>
      </TooltipTrigger>
      <TooltipContent className="max-w-72">
        Reading this machine&rsquo;s database through <code>cci serve</code>;
        filters are applied server-side.
        {newest !== null ? (
          <>
            {" "}
            Newest recorded activity: {formatDateTime(newest)}. If that is older
            than you expect, the database has not been re-ingested since.
          </>
        ) : null}
      </TooltipContent>
    </Tooltip>
  )
}
