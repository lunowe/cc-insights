import { useEffect, useState } from "react"

import { fetchDashboard, type DashboardData } from "@/lib/api"
import { toSearchParams, type Filters } from "@/lib/filters"

export type DashboardState = {
  /** Kept across refetches so a filter change updates in place, never blanks. */
  data: DashboardData | null
  loading: boolean
  error: string | null
}

/**
 * `revision` is a refetch trigger with no meaning of its own: bump it and the
 * same filters are fetched again. Watch mode drives it (`use-live.ts`), which
 * is why the effect below keys on it as well as on the filter set.
 */
export function useDashboard(filters: Filters, revision = 0): DashboardState {
  const [state, setState] = useState<DashboardState>({
    data: null,
    loading: true,
    error: null,
  })

  const key = toSearchParams(filters).toString()

  useEffect(() => {
    const controller = new AbortController()
    let live = true
    setState((s) => ({ ...s, loading: true, error: null }))

    fetchDashboard(filters, controller.signal).then(
      (data) => {
        if (live) setState({ data, loading: false, error: null })
      },
      (err: unknown) => {
        if (!live) return
        if (err instanceof DOMException && err.name === "AbortError") return
        setState((s) => ({
          ...s,
          loading: false,
          error: err instanceof Error ? err.message : String(err),
        }))
      },
    )

    return () => {
      live = false
      controller.abort()
    }
    // `key` is the serialised filter set; the object identity is irrelevant.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, revision])

  return state
}
