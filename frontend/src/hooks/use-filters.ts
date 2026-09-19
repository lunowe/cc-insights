import { useCallback, useEffect, useRef, useState } from "react"

import {
  filtersEqual,
  fromSearchParams,
  toSearchString,
  type Filters,
} from "@/lib/filters"

function readLocation(): Filters {
  return fromSearchParams(window.location.search)
}

export type SetFilters = (next: Filters | ((prev: Filters) => Filters)) => void

/**
 * Filter state lives in the URL, not in React. The query string is the single
 * source of truth, so a link carries the whole view, reload restores it, and
 * Back steps through the filters you tried. The serialised form is exactly the
 * API's `Filters` query string (see `lib/filters.ts`).
 */
export function useFilters(): [Filters, SetFilters] {
  const [filters, setState] = useState<Filters>(readLocation)
  const current = useRef(filters)

  useEffect(() => {
    const onPop = () => {
      const next = readLocation()
      current.current = next
      setState(next)
    }
    window.addEventListener("popstate", onPop)
    return () => window.removeEventListener("popstate", onPop)
  }, [])

  const setFilters = useCallback<SetFilters>((next) => {
    const value =
      typeof next === "function" ? next(current.current) : next
    if (filtersEqual(current.current, value)) return
    current.current = value
    window.history.pushState(
      null,
      "",
      `${window.location.pathname}${toSearchString(value)}${window.location.hash}`,
    )
    setState(value)
  }, [])

  return [filters, setFilters]
}
