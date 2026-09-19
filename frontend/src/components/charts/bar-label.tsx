import type { CartesianViewBox, LabelProps } from "recharts"

/**
 * End-of-bar value label drawn as a plain `<text>`. Recharts' own `Label`
 * hands its `Text` a width, which word-wraps a label like "72.2 h · 65%"
 * when the bar reaches the right margin; this never wraps.
 */
export function BarEndLabel(props: LabelProps) {
  const vb = props.viewBox as CartesianViewBox | undefined
  if (vb === undefined || vb.x === undefined || vb.y === undefined) return null
  const w = vb.width ?? 0
  const h = vb.height ?? 0
  return (
    <text
      x={vb.x + w + 6}
      y={vb.y + h / 2}
      dy="0.35em"
      fontSize={10}
      fill="var(--foreground)"
      className="num"
    >
      {props.value}
    </text>
  )
}

/**
 * Column-top label for exactly one column — the extreme, matched by the
 * label's own value (its row key) so it does not depend on an index prop.
 * Anchors away from the nearest plot edge so it never clips.
 */
export function ColumnTopLabel({
  props,
  only,
  text,
  plotRight,
}: {
  props: LabelProps
  only: string
  text: string
  plotRight: number
}) {
  const vb = props.viewBox as CartesianViewBox | undefined
  if (
    String(props.value) !== only ||
    vb === undefined ||
    vb.x === undefined ||
    vb.y === undefined
  )
    return null
  const w = vb.width ?? 0
  const cx = vb.x + w / 2
  const anchor = cx > plotRight - 60 ? "end" : cx < 60 ? "start" : "middle"
  const x = anchor === "end" ? vb.x + w : anchor === "start" ? vb.x : cx
  return (
    <text
      x={x}
      y={vb.y - 5}
      textAnchor={anchor}
      fontSize={10}
      fill="var(--foreground)"
      className="num"
    >
      {text}
    </text>
  )
}
