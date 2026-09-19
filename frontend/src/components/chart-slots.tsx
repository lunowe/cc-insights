import { useMemo } from "react"
import { cn } from "cn"

import {
  AgentsChart,
  ConcurrencyChart,
  DailyActiveChart,
  ProjectsChart,
  TimelineSwimlane,
  WeekHeatmap,
  buildProjectPalette,
} from "@/components/charts"
import type { DashboardData } from "@/lib/api"

/**
 * The six charts, laid out in the slots reserved for them. Every chart reads
 * the same `DashboardData` the tiles and table use, so a filter moves all of
 * them together. While a refetch is in flight the previous render is held at
 * reduced opacity rather than blanked.
 */
export function ChartSlots({
  data,
  loading,
}: {
  data: DashboardData
  loading: boolean
}) {
  // Colour is assigned from the unfiltered project list so a project keeps
  // its hue no matter which filter is active.
  const palette = useMemo(() => buildProjectPalette(data.meta), [data.meta])

  return (
    <section
      className={cn("transition-opacity", loading && "opacity-60")}
      aria-busy={loading}
    >
      <div className="mb-2.5 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="eyebrow text-foreground">Visualisations</h2>
        <p className="text-[0.75rem] text-muted-foreground">
          All six follow the filters above · local time
        </p>
      </div>

      <div className="grid gap-3 sm:gap-4">
        <TimelineSwimlane data={data} palette={palette} />
        <div className="grid gap-3 sm:gap-4 lg:grid-cols-2">
          <DailyActiveChart
            daily={data.daily}
            idleThresholdS={data.meta.idleThresholdS}
          />
          <ConcurrencyChart concurrency={data.concurrency} />
          <ProjectsChart projects={data.projects} palette={palette} />
          <AgentsChart agents={data.agents} />
        </div>
        <WeekHeatmap heatmap={data.heatmap} />
      </div>
    </section>
  )
}
