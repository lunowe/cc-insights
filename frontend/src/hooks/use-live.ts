import { useEffect, useRef, useState } from "react"

import { resolveLiveUrl } from "@/lib/api"

/**
 * Listen to `cci watch --serve`, and refetch when the database moves.
 *
 * `/api/live` is a Server-Sent Events stream the watch loop feeds; a plain
 * `cci serve` answers it with a 404, and a page loaded from the bundled
 * sample has no backend at all. Both are ordinary states, not errors: this
 * hook reports `watching: false` and the header simply says nothing about
 * live updates.
 *
 * **Ticks are coalesced, not followed one for one.** A busy machine emits a
 * cycle every two seconds and a full refetch is ten requests, one of them a
 * 700 kB timeline. Refetching on every tick would spend more time fetching
 * than the agents spend working, so ticks set a flag and at most one refetch
 * happens per `MIN_REFETCH_MS`. Nothing is lost by dropping a tick: the
 * generation number is a "something changed" signal, not a payload.
 *
 * **A hidden tab does not refetch at all.** It fires once, immediately, when
 * the tab comes back if anything happened while it was away -- which is the
 * moment someone is actually looking.
 */
const MIN_REFETCH_MS = 5_000

export type LiveState = {
  /** A watcher is feeding this page. False for `cci serve` and sample mode. */
  watching: boolean
  /** Epoch ms of the last cycle that changed something, or null. */
  lastChangeAt: number | null
  /** Cycles seen on this connection. */
  changes: number
}

export function useLive(onChange: () => void): LiveState {
  const [state, setState] = useState<LiveState>({
    watching: false,
    lastChangeAt: null,
    changes: 0,
  })

  // The callback identity changes on every render of the caller; keeping it
  // in a ref means the stream is opened once rather than reconnected on each
  // parent render, which would drop every tick in between.
  const callback = useRef(onChange)
  callback.current = onChange

  useEffect(() => {
    let source: EventSource | null = null
    let closed = false
    let pending = false
    let timer: number | undefined
    let last = 0

    const flush = () => {
      if (!pending || closed) return
      if (document.hidden) return // wait for the tab to come back
      const wait = MIN_REFETCH_MS - (Date.now() - last)
      if (wait > 0) {
        window.clearTimeout(timer)
        timer = window.setTimeout(flush, wait)
        return
      }
      pending = false
      last = Date.now()
      callback.current()
    }

    const onVisible = () => {
      if (!document.hidden) flush()
    }
    document.addEventListener("visibilitychange", onVisible)

    resolveLiveUrl().then((url) => {
      if (url === null || closed) return
      source = new EventSource(url)

      source.addEventListener("hello", () => {
        setState((s) => ({ ...s, watching: true }))
      })

      source.addEventListener("change", (event) => {
        const at = readAt(event)
        setState((s) => ({
          watching: true,
          lastChangeAt: at ?? Date.now(),
          changes: s.changes + 1,
        }))
        pending = true
        flush()
      })

      // A 404 (no watcher) or a dropped connection both land here. The
      // browser will not retry a non-200, and retrying a 404 forever would
      // be worse than silence, so this just stops claiming to be live.
      source.onerror = () => {
        setState((s) => ({ ...s, watching: false }))
      }
    })

    return () => {
      closed = true
      window.clearTimeout(timer)
      document.removeEventListener("visibilitychange", onVisible)
      source?.close()
    }
  }, [])

  return state
}

/** The cycle's own timestamp, when the frame carries a readable one. */
function readAt(event: Event): number | null {
  const data = (event as MessageEvent<string>).data
  if (typeof data !== "string") return null
  try {
    const at = (JSON.parse(data) as { at?: unknown }).at
    return typeof at === "number" ? at : null
  } catch {
    return null
  }
}
