import type { ReactNode } from "react"

export type Tip = { x: number; y: number; body: ReactNode }

/**
 * A pointer-following readout for the custom SVG charts. Positioned inside a
 * `relative` container in that container's coordinates; flips to the left of
 * the pointer when it would run past the right edge.
 */
export function HoverTip({ tip, width }: { tip: Tip | null; width: number }) {
  if (tip === null) return null
  const TIP_W = 232
  const flip = tip.x + 16 + TIP_W > width
  const left = flip ? Math.max(0, tip.x - 16 - TIP_W) : tip.x + 16
  return (
    <div
      role="status"
      className="pointer-events-none absolute z-30 w-58 rounded-md border border-border/60 bg-popover px-2.5 py-2 text-[0.75rem] leading-snug text-popover-foreground shadow-lg"
      style={{
        left,
        top: tip.y,
        transform: "translateY(-100%) translateY(-10px)",
      }}
    >
      {tip.body}
    </div>
  )
}

/** Tooltip row: value leads, label follows. */
export function TipRow({
  label,
  value,
  swatch,
}: {
  label: ReactNode
  value: ReactNode
  swatch?: string
}) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="flex min-w-0 items-center gap-1.5 text-muted-foreground">
        {swatch ? (
          <span
            aria-hidden
            className="inline-block size-2 shrink-0 rounded-[2px]"
            style={{ background: swatch }}
          />
        ) : null}
        <span className="truncate">{label}</span>
      </span>
      <span className="num shrink-0 font-medium text-foreground">{value}</span>
    </div>
  )
}

/** Convert a pointer event to coordinates inside `el`. */
export function localPoint(
  el: HTMLElement,
  e: { clientX: number; clientY: number },
) {
  const r = el.getBoundingClientRect()
  return { x: e.clientX - r.left, y: e.clientY - r.top }
}
