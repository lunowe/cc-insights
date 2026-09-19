import { AlertTriangle, FilterX, Scissors } from "lucide-react"

import { AppHeader } from "@/components/app-header"
import { ChartSlots } from "@/components/chart-slots"
import { FilterBar } from "@/components/filters/filter-bar"
import { ProjectsTable } from "@/components/projects-table"
import { ActivityTiles, OriginTiles } from "@/components/stat-tiles"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { TooltipProvider } from "@/components/ui/tooltip"
import { useDashboard } from "@/hooks/use-dashboard"
import { useFilters } from "@/hooks/use-filters"
import { DATA_MODE } from "@/lib/api"
import { EMPTY_FILTERS } from "@/lib/filters"
import { formatCount, formatDateTime } from "@/lib/format"
import type { Meta } from "@/lib/types"

export function App() {
  const [filters, setFilters] = useFilters()
  const { data, loading, error } = useDashboard(filters)

  return (
    <TooltipProvider delayDuration={200}>
      <div className="flex min-h-svh flex-col">
        <div className="z-40 bg-background/85 backdrop-blur-md sm:sticky sm:top-0">
          <AppHeader meta={data?.meta ?? null} mode={DATA_MODE} />
          <FilterBar
            meta={data?.meta ?? null}
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
                  <ChartSlots data={data} loading={loading} />
                  <ProjectsTable projects={data.projects.projects} />
                </>
              )}
            </>
          )}
        </main>

        <Footer meta={data?.meta ?? null} />
      </div>
    </TooltipProvider>
  )
}

function Footer({ meta }: { meta: Meta | null }) {
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
