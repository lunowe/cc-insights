import type { Daily, GroupOrigin, Source } from "./types"

export const SOURCE_LABEL: Record<Source, string> = {
  claude_code: "Claude Code",
  codex: "Codex",
}

const HOUR = 3_600_000
const MINUTE = 60_000

const num = new Intl.NumberFormat(undefined)
const oneDp = new Intl.NumberFormat(undefined, {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
})
const twoDp = new Intl.NumberFormat(undefined, {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

export const formatCount = (n: number) => num.format(n)

/** Hours with one decimal — the unit the rest of the project reports in. */
export function formatHours(ms: number): string {
  return oneDp.format(ms / HOUR)
}

export function formatMultiplier(x: number): string {
  return `${twoDp.format(x)}×`
}

/**
 * A duration read the way a person says it: `185 h 18 m`, `42 m`, `18 s`.
 * Never rounds a non-zero duration down to "0".
 */
export function formatDuration(ms: number): string {
  if (ms <= 0) return "0 s"
  const h = Math.floor(ms / HOUR)
  const m = Math.floor((ms % HOUR) / MINUTE)
  const s = Math.floor((ms % MINUTE) / 1000)
  if (h > 0) return m > 0 ? `${num.format(h)} h ${m} m` : `${num.format(h)} h`
  if (m > 0) return s > 0 ? `${m} m ${s} s` : `${m} m`
  return `${Math.max(s, 1)} s`
}

export function formatPercent(part: number, whole: number): string {
  if (whole <= 0) return "0%"
  const pct = (part / whole) * 100
  if (pct > 0 && pct < 1) return "<1%"
  return `${Math.round(pct)}%`
}

/** Epoch ms → local calendar date, e.g. `8 Feb 2026`. */
export function formatDate(ts: number): string {
  return new Date(ts).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
  })
}

/** Epoch ms → local date without the year, for dense tables. */
export function formatDateShort(ts: number): string {
  return new Date(ts).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
  })
}

export function formatDateTime(ts: number): string {
  return new Date(ts).toLocaleString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  })
}

/** ISO `YYYY-MM-DD` interpreted in local time, for `<input type="date">`. */
export function toDateInputValue(ts: number): string {
  const d = new Date(ts)
  return [
    d.getFullYear(),
    String(d.getMonth() + 1).padStart(2, "0"),
    String(d.getDate()).padStart(2, "0"),
  ].join("-")
}

/** Local midnight at the start of the day containing `ts`. */
export function startOfLocalDay(ts: number): number {
  const d = new Date(ts)
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()
}

/** Local midnight after the day containing `ts` — an exclusive upper bound. */
export function endOfLocalDayExclusive(ts: number): number {
  const d = new Date(ts)
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() + 1).getTime()
}

export function daysBetween(from: number, to: number): number {
  return Math.max(1, Math.round((to - from) / 86_400_000))
}

/** Safe read of a per-day source total; the wire omits sources with no time. */
export function bySourceMs(day: Daily["days"][number], source: Source): number {
  return day.bySource[source] ?? 0
}

/** `10 Feb — 19 Sep 2026`, collapsing a shared year. */
export function formatRange(from: number | null, to: number | null): string {
  if (from === null && to === null) return "All time"
  if (from === null) return `Up to ${formatDate(to as number)}`
  if (to === null) return `From ${formatDate(from)}`
  const a = new Date(from)
  const b = new Date(to)
  const left =
    a.getFullYear() === b.getFullYear() ? formatDateShort(from) : formatDate(from)
  return `${left} – ${formatDate(to)}`
}

/** How a group was detected, in words. See `docs/GROUPING.md`. */
export const GROUP_ORIGIN_LABEL: Record<GroupOrigin, string> = {
  git_remote: "same git remote",
  git_common_dir: "same git repo",
  path_worktree: "worktree path shape",
  path_ancestor: "inside another path",
  manual: "placed by hand",
}

/**
 * Known worktree path shapes, from the rule-3 ladder in `docs/GROUPING.md`.
 *
 * This is the *shape* of the path and nothing more — a fact about how the
 * checkout was made, orthogonal to whether it still exists. Both can be true
 * of the same row, and often are. Never derive existence from it: on the
 * author's corpus this matches 14 paths while only 10 are actually gone, and
 * 5 of the matches are live directories. `pathExists` is the only answer to
 * that question.
 */
export function isWorktreePath(rootPath: string): boolean {
  return (
    /\/\.claude\/worktrees\//.test(rootPath) ||
    /\/\.t3\/worktrees\//.test(rootPath) ||
    /\/conductor\/workspaces\//.test(rootPath)
  )
}

/** Days between a row's last activity and the newest activity anywhere. */
export function daysStale(lastTs: number, newestTs: number | null): number {
  if (newestTs === null) return 0
  return Math.max(0, Math.floor((newestTs - lastTs) / 86_400_000))
}
