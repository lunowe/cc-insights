import type { Role, Source } from "./types"

/**
 * The filter set, identical in shape to the `Filters` query string in
 * `docs/API.md`. The browser URL *is* the API query string: `toSearchParams`
 * produces exactly what `/api/summary?…` expects, so a shared link and a
 * backend request carry the same bytes.
 */
export type Filters = {
  /** project ids — one on-disk path each; empty = all */
  projects: string[]
  /**
   * group ids — one logical project each; empty = all.
   *
   * **`project` + `group` is a UNION**, not an intersection: `?group=G&project=P`
   * selects the spans of G *plus* the spans of P. Both select spans by which
   * project the span's session belongs to, so ticking one more stray project
   * after narrowing to a group widens the view rather than emptying it. That
   * union then intersects with `source`, `from`, `to` and `role` as usual.
   * See the "`project` + `group` is a UNION" section of `docs/API.md`.
   */
  groups: string[]
  /** empty = all */
  sources: Source[]
  /** inclusive lower bound on span start, epoch ms */
  from: number | null
  /** exclusive upper bound on span start, epoch ms */
  to: number | null
  role: Role
}

export const EMPTY_FILTERS: Filters = {
  projects: [],
  groups: [],
  sources: [],
  from: null,
  to: null,
  role: "all",
}

export const ALL_SOURCES: Source[] = ["claude_code", "codex", "opencode"]
export const ALL_ROLES: Role[] = ["all", "root", "subagent"]

/** True when nothing is narrowed — the unfiltered view. */
export function isUnfiltered(f: Filters): boolean {
  return (
    f.projects.length === 0 &&
    f.groups.length === 0 &&
    f.sources.length === 0 &&
    f.from === null &&
    f.to === null &&
    f.role === "all"
  )
}

export function activeFilterCount(f: Filters): number {
  return (
    (f.projects.length > 0 ? 1 : 0) +
    (f.groups.length > 0 ? 1 : 0) +
    (f.sources.length > 0 ? 1 : 0) +
    (f.from !== null || f.to !== null ? 1 : 0) +
    (f.role !== "all" ? 1 : 0)
  )
}

function isSource(v: string): v is Source {
  return v === "claude_code" || v === "codex" || v === "opencode"
}

function isRole(v: string): v is Role {
  return v === "all" || v === "root" || v === "subagent"
}

function readInt(v: string | null): number | null {
  if (v === null) return null
  const n = Number(v)
  return Number.isFinite(n) ? Math.trunc(n) : null
}

/** Parse filters out of a `?…` query string. Unknown values are dropped. */
export function fromSearchParams(search: string | URLSearchParams): Filters {
  const p =
    typeof search === "string" ? new URLSearchParams(search) : search
  const role = p.get("role")
  return {
    projects: p.getAll("project").filter(Boolean),
    groups: p.getAll("group").filter(Boolean),
    sources: p.getAll("source").filter(isSource),
    from: readInt(p.get("from")),
    to: readInt(p.get("to")),
    role: role !== null && isRole(role) ? role : "all",
  }
}

/**
 * Serialise filters. Key order is stable and defaults are omitted, so two
 * equivalent views always produce the same URL.
 */
export function toSearchParams(f: Filters): URLSearchParams {
  const p = new URLSearchParams()
  // Sorted and de-duplicated so two equivalent selections serialise byte-identically.
  for (const id of uniqueSorted(f.projects)) p.append("project", id)
  for (const id of uniqueSorted(f.groups)) p.append("group", id)
  for (const s of ALL_SOURCES) if (f.sources.includes(s)) p.append("source", s)
  if (f.from !== null) p.set("from", String(f.from))
  if (f.to !== null) p.set("to", String(f.to))
  if (f.role !== "all") p.set("role", f.role)
  return p
}

function uniqueSorted(ids: string[]): string[] {
  return [...new Set(ids)].sort()
}

export function toSearchString(f: Filters): string {
  const s = toSearchParams(f).toString()
  return s === "" ? "" : `?${s}`
}

export function filtersEqual(a: Filters, b: Filters): boolean {
  return toSearchParams(a).toString() === toSearchParams(b).toString()
}
