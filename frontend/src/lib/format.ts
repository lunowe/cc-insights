import type { Daily, GroupOrigin, Source } from "./types"

export const SOURCE_LABEL: Record<Source, string> = {
  claude_code: "Claude Code",
  codex: "Codex",
  opencode: "opencode",
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

const compact = new Intl.NumberFormat("en", {
  notation: "compact",
  maximumFractionDigits: 1,
})

/** `138.1M`, `11.1B`, `12.4K` — for token counts, where the magnitude is the point. */
export const formatCompact = (n: number) => compact.format(n)

/* ── money ───────────────────────────────────────────────────────────────────
   Every cost on this page is a LIST-PRICE EQUIVALENT — what the traffic would
   have cost at published API rates — and never a bill. These helpers only
   format; the words "not a bill" belong beside every number they produce.

   `currency` arrives as a string from the API: an ISO code like "USD", or
   "mixed" when the rates that met disagree. Intl throws on "mixed", and a
   made-up symbol would be worse than none, so anything that is not a
   three-letter code is rendered as a bare number with the unit spelled out
   by `currencyUnit()`.
   ──────────────────────────────────────────────────────────────────────────── */

export const MIXED_CURRENCY = "mixed"

const isIsoCurrency = (c: string) => /^[A-Z]{3}$/.test(c)

const moneyFormatters = new Map<string, Intl.NumberFormat>()

function moneyFormatter(
  currency: string,
  opts: Intl.NumberFormatOptions,
): Intl.NumberFormat | null {
  if (!isIsoCurrency(currency)) return null
  const key = `${currency}\u0000${JSON.stringify(opts)}`
  let f = moneyFormatters.get(key)
  if (f === undefined) {
    try {
      f = new Intl.NumberFormat(undefined, { style: "currency", currency, ...opts })
    } catch {
      // A well-formed but unknown code. Fall back to a bare number.
      return null
    }
    moneyFormatters.set(key, f)
  }
  return f
}

/** Whole units from 100 up, cents below: `$13,273`, `$18.63`, `$0.01`. */
export function formatCost(amount: number, currency: string): string {
  const digits = Math.abs(amount) >= 100 ? 0 : 2
  const opts = { minimumFractionDigits: digits, maximumFractionDigits: digits }
  return (
    moneyFormatter(currency, opts)?.format(amount) ??
    new Intl.NumberFormat(undefined, opts).format(amount)
  )
}

/** `$7.8K`, `$216` — for axis ticks and bar-end labels. */
export function formatCostCompact(amount: number, currency: string): string {
  const opts: Intl.NumberFormatOptions =
    Math.abs(amount) >= 1000
      ? { notation: "compact", maximumFractionDigits: 1 }
      : { maximumFractionDigits: Math.abs(amount) >= 100 ? 0 : 2 }
  return (
    moneyFormatter(currency, opts)?.format(amount) ??
    new Intl.NumberFormat("en", opts).format(amount)
  )
}

/**
 * The unit to print beside a number `formatCost` could not decorate: `null`
 * when the symbol already carries it, `"mixed currencies"` when the rates
 * disagreed, else the raw code so nothing is ever silently unitless.
 */
export function currencyUnit(currency: string): string | null {
  if (currency === MIXED_CURRENCY) return "mixed currencies"
  return moneyFormatter(currency, {}) === null ? currency : null
}

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

/**
 * How long ago, in the coarsest unit that is still honest: `4 m ago`,
 * `3 h ago`, `12 d ago`. Freshness is the question, not the exact instant, and
 * the exact instant is always one hover away.
 */
export function formatSince(ts: number, now: number = Date.now()): string {
  const ms = now - ts
  if (ms < 0) return "just now"
  if (ms < MINUTE) return "just now"
  if (ms < HOUR) return `${Math.floor(ms / MINUTE)} m ago`
  if (ms < 86_400_000) return `${Math.floor(ms / HOUR)} h ago`
  return `${Math.floor(ms / 86_400_000)} d ago`
}
