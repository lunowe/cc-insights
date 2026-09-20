import type { Meta } from "@/lib/types"

/**
 * Project → colour.
 *
 * "Project" here is the user-facing one: atlas-chat, not one of its thirteen
 * on-disk paths (`docs/GROUPING.md`). Colouring by path was the bug — it split
 * one project into a dozen hues and named a lane after a worktree. Every
 * surface that colours work keys off the id this returns, so atlas-chat is
 * one colour in the swimlane, the breakdown and the legend alike.
 *
 * Identity colours follow the entity, never its rank in the current view, so
 * the assignment is made once from `meta` (unfiltered, all-time) and does not
 * move when a filter narrows the page. Five slots are all the theme validates;
 * everything past them is "Other".
 */
export const PROJECT_SLOTS = 5

export const OTHER_COLOR = "var(--chart-other)"

export type ProjectPalette = {
  slotOf: Map<string, number>
  /** CSS colour for a project id; the de-emphasis gray when unranked. */
  colorOf: (id: string | null) => string
  /** Projects that hold a slot, in slot order. */
  ranked: { id: string; name: string; slot: number }[]
}

/**
 * The id a span or row is coloured by: its project, or its path when detection
 * has not placed it in one. Keeping the fallback means unplaced work still
 * gets a stable identity rather than silently merging into one grey mass.
 */
export function colorKey(
  groupId: string | null | undefined,
  projectId: string | null | undefined,
): string | null {
  return groupId ?? projectId ?? null
}

export function buildProjectPalette(meta: Meta): ProjectPalette {
  // Before `cci group auto` has run there are no projects, only paths. Ranking
  // paths then is not a compromise — it is the whole truth the app has.
  const candidates =
    meta.groups.length > 0
      ? meta.groups.map((g) => ({
          id: g.groupId,
          name: g.name,
          activeMs: g.activeMs,
        }))
      : meta.projects.map((p) => ({
          id: p.projectId,
          name: p.name,
          activeMs: p.activeMs,
        }))

  const ordered = [...candidates].sort((a, b) => b.activeMs - a.activeMs)
  const slotOf = new Map<string, number>()
  const ranked: ProjectPalette["ranked"] = []
  ordered.slice(0, PROJECT_SLOTS).forEach((c, i) => {
    slotOf.set(c.id, i + 1)
    ranked.push({ id: c.id, name: c.name, slot: i + 1 })
  })

  return {
    slotOf,
    ranked,
    colorOf: (id) => {
      if (id === null) return OTHER_COLOR
      const slot = slotOf.get(id)
      return slot === undefined ? OTHER_COLOR : `var(--chart-${slot})`
    },
  }
}
