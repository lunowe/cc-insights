import { useEffect, useState } from "react"

import { fetchDashboard, type DashboardData } from "@/lib/api"
import { toSearchParams, type Filters } from "@/lib/filters"

export type DashboardState = {
  /** Kept across refetches so a filter change updates in place, never blanks. */
  data: DashboardData | null
  loading: boolean
  error: string | null
}

export function useDashboard(filters: Filters): DashboardState {
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
  }, [key])

  return state
}
