import { RotateCcw, X } from "lucide-react"
import { cn } from "cn"

import { DateRangeFilter } from "@/components/filters/date-range-filter"
import { ProjectFilter } from "@/components/filters/project-filter"
import { Button } from "@/components/ui/button"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import type { SetFilters } from "@/hooks/use-filters"
import {
  ALL_SOURCES,
  EMPTY_FILTERS,
  activeFilterCount,
  type Filters,
} from "@/lib/filters"
import { SOURCE_LABEL, formatCount, formatHours } from "@/lib/format"
import type { Meta, Role, Source, Summary } from "@/lib/types"

const ROLE_OPTIONS: { value: Role; label: string; hint: string }[] = [
  { value: "all", label: "All", hint: "Root and subagent threads together." },
  {
    value: "root",
    label: "Root",
    hint: "Top-level threads only — the ones you or a schedule started.",
  },
  {
    value: "subagent",
    label: "Subagent",
    hint: "Threads a model spawned. This is the autonomous bucket.",
  },
]

export function FilterBar({
  meta,
  filters,
  setFilters,
  summary,
  loading,
}: {
  meta: Meta | null
  filters: Filters
  setFilters: SetFilters
  summary: Summary | null
  loading: boolean
}) {
  const active = activeFilterCount(filters)
  const selectedNames = new Map(
    (meta?.projects ?? []).map((p) => [p.projectId, p.name] as const),
  )

  // Empty means "all": both sources render lit, and switching one off narrows
  // to the other. Turning the last one off returns to all rather than to zero.
  const sourceValue: string[] =
    filters.sources.length === 0 ? [...ALL_SOURCES] : filters.sources

  return (
    <div className="border-b">
      <div className="mx-auto max-w-[110rem] px-4 py-2.5 sm:px-6 lg:px-8">
        <div className="flex flex-wrap items-center gap-2">
          <ProjectFilter
            projects={meta?.projects ?? []}
            selected={filters.projects}
            onChange={(projects) => setFilters((f) => ({ ...f, projects }))}
          />

          <ToggleGroup
            type="multiple"
            variant="outline"
            size="sm"
            value={sourceValue}
            onValueChange={(next: string[]) => {
              const picked = next.filter((v): v is Source =>
                (ALL_SOURCES as string[]).includes(v),
              )
              setFilters((f) => ({
                ...f,
                sources:
                  picked.length === 0 || picked.length === ALL_SOURCES.length
                    ? []
                    : picked,
              }))
            }}
            aria-label="Source"
            className="h-9"
          >
            {ALL_SOURCES.map((s) => (
              <ToggleGroupItem
                key={s}
                value={s}
                className="h-9 px-3 text-[0.8125rem] font-normal"
              >
                {SOURCE_LABEL[s]}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>

          <ToggleGroup
            type="single"
            variant="outline"
            size="sm"
            value={filters.role}
            onValueChange={(next: string) => {
              if (next === "") return
              setFilters((f) => ({ ...f, role: next as Role }))
            }}
            aria-label="Thread role"
            className="h-9"
          >
            {/* No Radix tooltip here: `TooltipTrigger asChild` would overwrite
                the item's `data-state`, and the selected role would stop
                lighting up. A title attribute carries the hint instead. */}
            {ROLE_OPTIONS.map((r) => (
              <ToggleGroupItem
                key={r.value}
                value={r.value}
                title={r.hint}
                aria-label={`${r.label} threads — ${r.hint}`}
                className="h-9 px-3 text-[0.8125rem] font-normal"
              >
                {r.label}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>

          <DateRangeFilter
            value={{ from: filters.from, to: filters.to }}
            onChange={(r) => setFilters((f) => ({ ...f, ...r }))}
            bounds={{
              firstTs: meta?.firstTs ?? null,
              lastTs: meta?.lastTs ?? null,
            }}
          />

          {active > 0 ? (
            <Button
              variant="ghost"
              size="sm"
              className="h-9 gap-1.5 px-2.5 text-[0.8125rem] font-normal text-muted-foreground"
              onClick={() => setFilters(EMPTY_FILTERS)}
            >
              <RotateCcw className="size-3.5" />
              Reset
            </Button>
          ) : null}

          <div
            className={cn(
              "num ml-auto shrink-0 py-0.5 text-[0.75rem] whitespace-nowrap text-muted-foreground transition-opacity",
              loading && "opacity-40",
            )}
            aria-live="polite"
          >
            {summary === null ? (
              "—"
            ) : (
              <>
                <span className="font-medium text-foreground">
                  {formatHours(summary.activeMs)} h
                </span>
                <span className="text-muted-foreground/60"> · </span>
                {formatCount(summary.spans)} spans
              </>
            )}
          </div>
        </div>

        {filters.projects.length > 0 ? (
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            {filters.projects.map((id) => (
              <button
                key={id}
                type="button"
                onClick={() =>
                  setFilters((f) => ({
                    ...f,
                    projects: f.projects.filter((x) => x !== id),
                  }))
                }
                className="group inline-flex max-w-52 items-center gap-1 rounded-full border border-primary/35 bg-primary/8 py-0.5 pr-1.5 pl-2.5 text-[0.75rem] transition-colors hover:border-primary/60"
              >
                <span className="truncate">
                  {selectedNames.get(id) ?? id.slice(0, 8)}
                </span>
                <X className="size-3 shrink-0 text-muted-foreground group-hover:text-foreground" />
              </button>
            ))}
          </div>
        ) : null}
      </div>
    </div>
  )
}
