import type { Meta } from "@/lib/types"

/**
 * Project → colour. Identity colours must follow the entity, never its rank
 * in the current view, so the assignment is made once from `meta.projects`
 * (unfiltered, all-time) and never changes when a filter narrows the page.
 * Five slots are all the theme validates; everything past them is "Other".
 */
export const PROJECT_SLOTS = 5

export const OTHER_COLOR = "var(--chart-other)"

export type ProjectPalette = {
  slotOf: Map<string, number>
  /** CSS colour for a project id; the de-emphasis gray when unranked. */
  colorOf: (projectId: string | null) => string
  /** Projects that hold a slot, in slot order. */
  ranked: { projectId: string; name: string; slot: number }[]
}

export function buildProjectPalette(meta: Meta): ProjectPalette {
  const ordered = [...meta.projects].sort((a, b) => b.activeMs - a.activeMs)
  const slotOf = new Map<string, number>()
  const ranked: ProjectPalette["ranked"] = []
  ordered.slice(0, PROJECT_SLOTS).forEach((p, i) => {
    slotOf.set(p.projectId, i + 1)
    ranked.push({ projectId: p.projectId, name: p.name, slot: i + 1 })
  })
  return {
    slotOf,
    ranked,
    colorOf: (projectId) => {
      if (projectId === null) return OTHER_COLOR
      const slot = slotOf.get(projectId)
      return slot === undefined ? OTHER_COLOR : `var(--chart-${slot})`
    },
  }
}
