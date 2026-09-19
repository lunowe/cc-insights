import { useState } from "react"
import { CalendarRange } from "lucide-react"
import type { DateRange } from "react-day-picker"
import { cn } from "cn"

import { Button } from "@/components/ui/button"
import { Calendar } from "@/components/ui/calendar"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import { useMediaQuery } from "@/hooks/use-media-query"
import {
  endOfLocalDayExclusive,
  formatRange,
  startOfLocalDay,
} from "@/lib/format"

const DAY = 86_400_000

export type Range = { from: number | null; to: number | null }

/**
 * `to` on the wire is an **exclusive** upper bound on span start, but people
 * pick inclusive days. The calendar shows `to - 1 ms` and writes back the
 * midnight after the day you clicked.
 */
export function DateRangeFilter({
  value,
  onChange,
  bounds,
}: {
  value: Range
  onChange: (next: Range) => void
  bounds: { firstTs: number | null; lastTs: number | null }
}) {
  const [open, setOpen] = useState(false)
  const twoUp = useMediaQuery("(min-width: 640px)")

  const isDefault = value.from === null && value.to === null
  const shownFrom = value.from ?? bounds.firstTs
  const shownTo = value.to !== null ? value.to - 1 : bounds.lastTs

  const selected: DateRange | undefined =
    shownFrom === null
      ? undefined
      : {
          from: new Date(shownFrom),
          to: shownTo === null ? undefined : new Date(shownTo),
        }

  function setPreset(days: number | null) {
    if (days === null || bounds.lastTs === null) {
      // With no data there is nothing to anchor a relative range to.
      onChange({ from: null, to: null })
    } else {
      // Relative to the last recorded activity, not to today: a capture that
      // stopped a week ago should still answer "last 7 days" with its own week.
      const anchor = bounds.lastTs
      onChange({
        from: startOfLocalDay(anchor - (days - 1) * DAY),
        to: endOfLocalDayExclusive(anchor),
      })
    }
    setOpen(false)
  }

  function onSelect(next: DateRange | undefined) {
    if (next?.from === undefined) {
      onChange({ from: null, to: null })
      return
    }
    onChange({
      from: startOfLocalDay(next.from.getTime()),
      to: endOfLocalDayExclusive((next.to ?? next.from).getTime()),
    })
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          className={cn(
            "h-9 max-w-full min-w-0 justify-start gap-2 px-3 font-normal",
            !isDefault && "border-primary/45",
          )}
        >
          <CalendarRange
            className={cn(
              "size-3.5 shrink-0",
              isDefault ? "text-muted-foreground" : "text-primary",
            )}
          />
          <span className="num truncate text-[0.8125rem]">
            {formatRange(shownFrom, shownTo)}
          </span>
          {isDefault ? (
            <span className="hidden text-[0.6875rem] text-muted-foreground/70 sm:inline">
              full range
            </span>
          ) : null}
        </Button>
      </PopoverTrigger>

      <PopoverContent
        align="start"
        className="w-[min(46rem,calc(100vw-2rem))] p-0"
      >
        <div className="flex flex-wrap items-center gap-1 border-b p-2">
          {(
            [
              ["Last 7 days", 7],
              ["Last 30 days", 30],
              ["Last 90 days", 90],
              ["Full range", null],
            ] as const
          ).map(([label, days]) => (
            <Button
              key={label}
              variant="ghost"
              size="sm"
              className="h-7 px-2 text-[0.75rem] font-normal"
              onClick={() => setPreset(days)}
            >
              {label}
            </Button>
          ))}
        </div>
        <Calendar
          mode="range"
          required={false}
          selected={selected}
          onSelect={onSelect}
          defaultMonth={
            shownTo !== null ? new Date(shownTo) : undefined
          }
          startMonth={
            bounds.firstTs !== null ? new Date(bounds.firstTs) : undefined
          }
          endMonth={bounds.lastTs !== null ? new Date(bounds.lastTs) : undefined}
          numberOfMonths={twoUp ? 2 : 1}
          weekStartsOn={1}
          className="w-full [--cell-size:--spacing(8)]"
        />
      </PopoverContent>
    </Popover>
  )
}
