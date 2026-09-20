import { useCallback, useState } from "react"
import { AlertTriangle, FilterX, Scissors } from "lucide-react"

import { AppHeader } from "@/components/app-header"
import { ChartSlots } from "@/components/chart-slots"
import { FilterBar } from "@/components/filters/filter-bar"
import { ProjectsTable } from "@/components/projects-table"
import { ActivityTiles, CostTiles, OriginTiles } from "@/components/stat-tiles"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { TooltipProvider } from "@/components/ui/tooltip"
import { useDashboard } from "@/hooks/use-dashboard"
import { useFilters } from "@/hooks/use-filters"
import { useLive } from "@/hooks/use-live"
import { EMPTY_FILTERS } from "@/lib/filters"
import { formatCount, formatDateTime } from "@/lib/format"
import type { DataMode } from "@/lib/api"
import type { Meta } from "@/lib/types"

export function App() {
  const [filters, setFilters] = useFilters()
  // `cci watch --serve` pushes a tick when the database moves; bumping the
  // revision refetches the same filters. With no watcher behind the page
  // this never fires and nothing on screen mentions it.
  const [revision, setRevision] = useState(0)
  const live = useLive(useCallback(() => setRevision((n) => n + 1), []))
  const { data, loading, error } = useDashboard(filters, revision)

  return (
    <TooltipProvider delayDuration={200}>
      <div className="flex min-h-svh flex-col">
        <div className="z-40 bg-background/85 backdrop-blur-md sm:sticky sm:top-0">
          {/* `mode` is only known once the backend probe has resolved; until
              then the badge stays absent rather than guessing "sample". */}
          <AppHeader
            meta={data?.meta ?? null}
            mode={data?.mode ?? null}
            live={live}
          />
          <FilterBar
            meta={data?.meta ?? null}
            roster={data?.roster ?? []}
            filters={filters}
            setFilters={setFilters}
            summary={data?.summary ?? null}
            loading={loading}
          />
        </div>

        <main className="mx-auto w-full max-w-[110rem] flex-1 space-y-6 px-4 py-6 sm:space-y-8 sm:px-6 sm:py-8 lg:px-8">
          {error !== null && data === null ? (
            <ErrorState message={error} />
          ) : data === null ? (
            <LoadingState />
          ) : (
            <>
              {error !== null ? <ErrorState message={error} inline /> : null}

              {data.timeline.truncated ? (
                <Notice icon={Scissors}>
                  The server capped this range at{" "}
                  {formatCount(data.timeline.limit)} spans and returned the
                  widest ones. Narrow the range to see everything.
                </Notice>
              ) : null}

              {data.summary.spans === 0 ? (
                <EmptyState onReset={() => setFilters(EMPTY_FILTERS)} />
              ) : (
                <>
                  <ActivityTiles
                    summary={data.summary}
                    concurrency={data.concurrency}
                    daily={data.daily}
                  />
                  <OriginTiles summary={data.summary} />
                  <CostTiles
                    cost={data.cost}
                    unavailable={data.eventFactsUnfiltered}
                  />
                  <ChartSlots data={data} loading={loading} />
                  <ProjectsTable
                    groups={data.groups}
                    projects={data.projects.projects}
                    totalActiveMs={data.summary.activeMs}
                    newestTs={data.meta.lastTs}
                    currency={data.projects.currency}
                    costUnavailable={data.eventFactsUnfiltered}
                  />
                </>
              )}
            </>
          )}
        </main>

        <Footer meta={data?.meta ?? null} mode={data?.mode ?? null} />
      </div>
    </TooltipProvider>
  )
}

function Footer({
  meta,
  mode,
}: {
  meta: Meta | null
  mode: DataMode | null
}) {
  return (
    <footer className="mt-4 border-t">
      <div className="mx-auto flex max-w-[110rem] flex-col gap-1.5 px-4 py-5 text-[0.75rem] leading-relaxed text-muted-foreground sm:px-6 lg:px-8">
        <p>
          Active time is the sum of span durations. A gap longer than{" "}
          <span className="num font-medium text-foreground">
            {meta?.idleThresholdS ?? "—"} s
          </span>{" "}
          ends a span and contributes zero — it is never capped and never
          counted. Quote that threshold with any duration you export.
        </p>
        <p>
          Timestamps are epoch milliseconds UTC on the wire and rendered in this
          browser&rsquo;s local time.
          {meta ? (
            <>
              {" "}
              Captured on{" "}
              <span className="num">{meta.hostname}</span>,{" "}
              <span className="num">{formatDateTime(meta.generatedAt)}</span>.
            </>
          ) : null}
        </p>
        <p>
          Every cost on this page is a{" "}
          <span className="font-medium text-foreground">
            list-price equivalent
          </span>
          : tokens × published API rates. It is not a bill — a subscription
          charges a flat fee however many tokens run through it — and tokens
          with no rate on file are left out as unknown, not free.
          {meta?.pricing.catalog.repo ? (
            <>
              {" "}
              Rates from{" "}
              <span className="num">{meta.pricing.catalog.repo}</span>
              {meta.pricing.catalog.commit ? (
                <>
                  {" "}
                  @{" "}
                  <span className="num">
                    {meta.pricing.catalog.commit.slice(0, 7)}
                  </span>
                </>
              ) : null}
              {meta.pricing.catalog.fetched_at ? (
                <>
                  , fetched{" "}
                  <span className="num">
                    {formatDateTime(Date.parse(meta.pricing.catalog.fetched_at))}
                  </span>
                </>
              ) : null}
              .
            </>
          ) : null}
        </p>
        {/* Without this the line above reads as if this machine produced the
            numbers, which in sample mode is the one thing it did not. */}
        {mode === "fixtures" ? (
          <p>
            <span className="font-medium text-foreground">
              These are sample numbers, not yours.
            </span>{" "}
            The page is showing a capture bundled into the build so it renders
            with no server. Run{" "}
            <code className="num rounded bg-muted px-1 py-0.5 text-foreground">
              cci serve
            </code>{" "}
            and open the address it prints to see this machine&rsquo;s data.
          </p>
        ) : null}
      </div>
    </footer>
  )
}

function Notice({
  icon: Icon,
  children,
}: {
  icon: typeof Scissors
  children: React.ReactNode
}) {
  return (
    <div className="flex items-start gap-2.5 rounded-lg border border-primary/35 bg-primary/8 px-4 py-3 text-[0.8125rem]">
      <Icon className="mt-0.5 size-4 shrink-0 text-primary" />
      <p>{children}</p>
    </div>
  )
}

function ErrorState({
  message,
  inline = false,
}: {
  message: string
  inline?: boolean
}) {
  return (
    <div
      className={
        "flex items-start gap-2.5 rounded-lg border border-destructive/40 bg-destructive/8 px-4 py-3 text-[0.8125rem]" +
        (inline ? "" : " mt-8")
      }
    >
      <AlertTriangle className="mt-0.5 size-4 shrink-0 text-destructive" />
      <p>
        <span className="font-medium">Could not load the data.</span>{" "}
        <span className="text-muted-foreground">{message}</span>
      </p>
    </div>
  )
}

function EmptyState({ onReset }: { onReset: () => void }) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed px-6 py-16 text-center">
      <FilterX className="size-5 text-muted-foreground" />
      <p className="text-sm font-medium">No spans match these filters.</p>
      <p className="max-w-sm text-[0.8125rem] text-muted-foreground">
        Every figure on this page is derived from the spans in view, so there is
        nothing to report.
      </p>
      <Button variant="outline" size="sm" onClick={onReset}>
        Reset filters
      </Button>
    </div>
  )
}

function LoadingState() {
  return (
    <div className="space-y-6 sm:space-y-8">
      <div className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border bg-border lg:grid-cols-6">
        <Skeleton className="col-span-2 h-40 rounded-none" />
        {Array.from({ length: 4 }, (_, i) => (
          <Skeleton key={i} className="h-40 rounded-none" />
        ))}
      </div>
      <div className="grid grid-cols-1 gap-px overflow-hidden rounded-lg border bg-border sm:grid-cols-3">
        {Array.from({ length: 3 }, (_, i) => (
          <Skeleton key={i} className="h-32 rounded-none" />
        ))}
      </div>
      <Skeleton className="h-64 rounded-lg" />
    </div>
  )
}
