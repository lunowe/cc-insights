import { useMemo, useState } from "react"
import {
  ArrowDown,
  ArrowUp,
  ChevronRight,
  ExternalLink,
  GitBranch,
  Pin,
} from "lucide-react"
import { cn } from "cn"

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import {
  GROUP_ORIGIN_LABEL,
  daysStale,
  formatCount,
  formatDate,
  formatHours,
  isWorktreePath,
} from "@/lib/format"
import type { GroupRow, Groups, ProjectRow } from "@/lib/types"

type SortKey = "name" | "activeMs" | "paths" | "sessions" | "threads" | "lastTs"
type Dir = "asc" | "desc"

const COLUMNS: {
  key: SortKey
  label: string
  align: "left" | "right"
  /** Columns drop out narrowest-first so 375 px never needs a sideways scroll. */
  hide?: string
  defaultDir: Dir
}[] = [
  { key: "name", label: "Group / path", align: "left", defaultDir: "asc" },
  { key: "activeMs", label: "Active", align: "right", defaultDir: "desc" },
  {
    key: "paths",
    label: "Paths",
    align: "right",
    hide: "hidden sm:table-cell",
    defaultDir: "desc",
  },
  {
    key: "sessions",
    label: "Sessions",
    align: "right",
    hide: "hidden md:table-cell",
    defaultDir: "desc",
  },
  {
    key: "threads",
    label: "Threads",
    align: "right",
    hide: "hidden lg:table-cell",
    defaultDir: "desc",
  },
  {
    key: "lastTs",
    label: "Last seen",
    align: "right",
    hide: "hidden md:table-cell",
    defaultDir: "desc",
  },
]

const UNGROUPED = "\u0000ungrouped"

/** One row of the table's top level: a logical project, or the leftovers. */
type Bucket = {
  key: string
  kind: "group" | "ungrouped"
  name: string
  group: GroupRow | null
  members: ProjectRow[]
  activeMs: number
  paths: number
  sessions: number
  threads: number
  lastTs: number
}

function buildBuckets(groups: Groups, projects: ProjectRow[]): Bucket[] {
  const members = new Map<string, ProjectRow[]>()
  for (const p of projects) {
    const key = p.groupId ?? UNGROUPED
    const list = members.get(key)
    if (list === undefined) members.set(key, [p])
    else list.push(p)
  }

  const sum = (rows: ProjectRow[], pick: (r: ProjectRow) => number) =>
    rows.reduce((n, r) => n + pick(r), 0)

  const out: Bucket[] = groups.groups.map((g) => {
    const rows = members.get(g.groupId) ?? []
    members.delete(g.groupId)
    return {
      key: g.groupId,
      kind: "group" as const,
      name: g.name,
      group: g,
      members: rows,
      activeMs: g.activeMs,
      paths: g.projects,
      sessions: g.sessions,
      threads: g.threads,
      lastTs: g.lastTs,
    }
  })

  // A project row whose group is missing from `/api/groups` should not exist,
  // but if it ever does, show it rather than dropping its hours on the floor.
  for (const [key, rows] of members) {
    if (key === UNGROUPED) continue
    out.push({
      key,
      kind: "group",
      name: rows[0]?.groupName ?? key.slice(0, 8),
      group: null,
      members: rows,
      activeMs: sum(rows, (r) => r.activeMs),
      paths: rows.length,
      sessions: sum(rows, (r) => r.sessions),
      threads: sum(rows, (r) => r.threads),
      lastTs: Math.max(...rows.map((r) => r.lastTs)),
    })
  }

  const loose = members.get(UNGROUPED) ?? []
  if (groups.ungrouped.activeMs > 0 || loose.length > 0) {
    out.push({
      key: UNGROUPED,
      kind: "ungrouped",
      name: "Ungrouped",
      group: null,
      members: loose,
      // The endpoint's own figure, not the members' sum: it also carries the
      // rare span whose session has no project at all, and that difference is
      // exactly what keeps the invariant below exact.
      activeMs: groups.ungrouped.activeMs,
      paths: groups.ungrouped.projects,
      sessions: sum(loose, (r) => r.sessions),
      threads: sum(loose, (r) => r.threads),
      lastTs: loose.length > 0 ? Math.max(...loose.map((r) => r.lastTs)) : 0,
    })
  }

  return out
}

/**
 * The projects table, rolled up by group.
 *
 * A project row is one on-disk path; the logical project is the group behind
 * several of them. Flat, this list reads `atlas-chat` at 54.5 h and its own
 * worktree `tenant-restricted` at 39.8 h as unrelated work. Rolled up it reads
 * 111.6 h across 13 paths, which is the true number.
 */
export function ProjectsTable({
  groups,
  projects,
  totalActiveMs,
  newestTs,
}: {
  groups: Groups
  projects: ProjectRow[]
  /** `summary.activeMs` for the same filter — the invariant's right-hand side. */
  totalActiveMs: number
  /** `meta.lastTs`: the newest activity anywhere, for judging staleness. */
  newestTs: number | null
}) {
  const [sort, setSort] = useState<{ key: SortKey; dir: Dir }>({
    key: "activeMs",
    dir: "desc",
  })

  const buckets = useMemo(
    () => buildBuckets(groups, projects),
    [groups, projects],
  )

  const sorted = useMemo(() => {
    const sign = sort.dir === "asc" ? 1 : -1
    const value = (b: Bucket) =>
      sort.key === "name" ? 0 : (b[sort.key] as number)
    const rows = [...buckets].sort((a, b) => {
      // Ungrouped is not a competitor in the ranking; it sits at the bottom.
      if (a.kind !== b.kind) return a.kind === "ungrouped" ? 1 : -1
      if (sort.key === "name") return sign * a.name.localeCompare(b.name)
      return sign * (value(a) - value(b))
    })
    return rows.map((b) => ({
      ...b,
      members: [...b.members].sort((x, y) => {
        if (sort.key === "name") return sign * x.name.localeCompare(y.name)
        if (sort.key === "paths") return y.activeMs - x.activeMs
        return sign * (x[sort.key] - y[sort.key])
      }),
    }))
  }, [buckets, sort])

  // A filter change rebuilds the list, so the open set is keyed by what is on
  // screen. One bucket in view means the person already narrowed to it.
  const bucketKeys = sorted.map((b) => b.key).join("\u0000")
  const [openState, setOpenState] = useState<{
    keys: string
    open: Set<string>
  }>(() => ({ keys: bucketKeys, open: new Set(defaultOpen(sorted)) }))
  const open =
    openState.keys === bucketKeys ? openState.open : new Set(defaultOpen(sorted))
  if (openState.keys !== bucketKeys) {
    setOpenState({ keys: bucketKeys, open })
  }

  function toggle(key: string) {
    setOpenState((s) => {
      const next = new Set(s.open)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return { keys: bucketKeys, open: next }
    })
  }

  function onSort(key: SortKey, defaultDir: Dir) {
    setSort((s) =>
      s.key === key
        ? { key, dir: s.dir === "asc" ? "desc" : "asc" }
        : { key, dir: defaultDir },
    )
  }

  const groupCount = sorted.filter((b) => b.kind === "group").length
  const groupMs = sorted
    .filter((b) => b.kind === "group")
    .reduce((n, b) => n + b.activeMs, 0)
  const ungroupedMs = groups.ungrouped.activeMs
  const balances = groupMs + ungroupedMs === totalActiveMs
  const allOpen = sorted.every((b) => open.has(b.key))

  return (
    <section>
      <div className="mb-2.5 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="eyebrow text-foreground">
          {groupCount === 0 ? "Projects" : "Projects by group"}
        </h2>
        <div className="flex items-baseline gap-3">
          <p className="num text-[0.75rem] text-muted-foreground">
            {groupCount > 0 ? (
              <>
                {formatCount(groupCount)}{" "}
                {groupCount === 1 ? "group" : "groups"} ·{" "}
              </>
            ) : null}
            {formatCount(projects.length)}{" "}
            {projects.length === 1 ? "path" : "paths"}
          </p>
          <button
            type="button"
            onClick={() =>
              setOpenState({
                keys: bucketKeys,
                open: allOpen ? new Set() : new Set(sorted.map((b) => b.key)),
              })
            }
            className="text-[0.75rem] text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
          >
            {allOpen ? "Collapse all" : "Expand all"}
          </button>
        </div>
      </div>

      <div className="overflow-hidden rounded-lg border bg-card">
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              {COLUMNS.map((c) => {
                const on = sort.key === c.key
                const Arrow = sort.dir === "asc" ? ArrowUp : ArrowDown
                return (
                  <TableHead
                    key={c.key}
                    aria-sort={
                      on
                        ? sort.dir === "asc"
                          ? "ascending"
                          : "descending"
                        : "none"
                    }
                    className={cn(
                      "h-9 p-0",
                      c.hide,
                      c.align === "right" && "text-right",
                    )}
                  >
                    <button
                      type="button"
                      onClick={() => onSort(c.key, c.defaultDir)}
                      className={cn(
                        "eyebrow flex h-9 w-full items-center gap-1 px-3 transition-colors hover:text-foreground",
                        c.align === "right" && "justify-end",
                        on && "text-foreground",
                      )}
                    >
                      {c.label}
                      <Arrow
                        className={cn(
                          "size-3 transition-opacity",
                          on ? "text-primary opacity-100" : "opacity-0",
                        )}
                      />
                    </button>
                  </TableHead>
                )
              })}
            </TableRow>
          </TableHeader>

          <TableBody>
            {sorted.length === 0 ? (
              <TableRow className="hover:bg-transparent">
                <TableCell
                  colSpan={COLUMNS.length}
                  className="h-28 text-center text-sm text-muted-foreground"
                >
                  No spans match these filters.
                </TableCell>
              </TableRow>
            ) : (
              sorted.map((b) => (
                <BucketRows
                  key={b.key}
                  bucket={b}
                  open={open.has(b.key)}
                  onToggle={() => toggle(b.key)}
                  newestTs={newestTs}
                />
              ))
            )}
          </TableBody>
        </Table>

        {sorted.length > 0 ? (
          <div className="border-t bg-muted/30 px-3 py-2 text-[0.75rem] text-muted-foreground">
            <span className="num">
              <span className="text-foreground">
                {formatHours(groupMs)} h
              </span>{" "}
              in {formatCount(groupCount)}{" "}
              {groupCount === 1 ? "group" : "groups"} +{" "}
              <span className="text-foreground">
                {formatHours(ungroupedMs)} h
              </span>{" "}
              ungrouped ={" "}
              <span className="text-foreground">
                {formatHours(totalActiveMs)} h
              </span>{" "}
              active
            </span>
            {balances ? (
              <span> — the whole of the view above, nothing hidden.</span>
            ) : (
              <span className="text-destructive">
                {" "}
                — these should be equal and are not. Off by{" "}
                {formatHours(Math.abs(totalActiveMs - groupMs - ungroupedMs))} h.
              </span>
            )}
          </div>
        ) : null}
      </div>
    </section>
  )
}

/** Narrowed to one or two logical projects? Then the paths are the point. */
function defaultOpen(buckets: Bucket[]): string[] {
  return buckets.length <= 2 ? buckets.map((b) => b.key) : []
}

function BucketRows({
  bucket: b,
  open,
  onToggle,
  newestTs,
}: {
  bucket: Bucket
  open: boolean
  onToggle: () => void
  newestTs: number | null
}) {
  const ungrouped = b.kind === "ungrouped"
  const pinned = b.group?.pinnedProjects ?? 0
  const expandable = b.members.length > 0

  return (
    <>
      <TableRow
        className={cn(
          "border-b-0",
          open && "bg-muted/40",
          // Visibly not one of the groups above, but never hidden: its hours
          // are the difference between the group rows and the page total.
          ungrouped && "border-t border-dashed text-muted-foreground",
        )}
      >
        <TableCell className="max-w-0 py-2.5 pr-2">
          <div className="flex min-w-0 items-center gap-1.5">
            <button
              type="button"
              onClick={onToggle}
              disabled={!expandable}
              aria-expanded={open}
              aria-label={`${open ? "Collapse" : "Expand"} ${b.name}`}
              className="-ml-1 flex min-w-0 flex-1 items-center gap-1.5 rounded-sm text-left disabled:cursor-default"
            >
              <ChevronRight
                className={cn(
                  "size-3.5 shrink-0 text-muted-foreground transition-transform",
                  open && "rotate-90",
                  !expandable && "opacity-0",
                )}
              />
              <span
                className={cn(
                  "truncate text-[0.8125rem] font-medium",
                  ungrouped ? "italic" : "text-foreground",
                )}
              >
                {b.name}
              </span>
            </button>

            {pinned > 0 ? <PinnedMarker count={pinned} /> : null}

            {b.group?.webUrl != null ? (
              <Tooltip>
                <TooltipTrigger asChild>
                  <a
                    href={b.group.webUrl}
                    target="_blank"
                    rel="noreferrer noopener"
                    onClick={(e) => e.stopPropagation()}
                    aria-label={`Open ${b.name} on ${b.group.forge ?? "the forge"}`}
                    className="shrink-0 rounded-sm p-0.5 text-muted-foreground transition-colors hover:text-primary"
                  >
                    <ExternalLink className="size-3.5" />
                  </a>
                </TooltipTrigger>
                <TooltipContent className="max-w-72 break-all">
                  {b.group.webUrl}
                </TooltipContent>
              </Tooltip>
            ) : null}
          </div>

          <p className="mt-0.5 ml-3 truncate text-[0.6875rem] text-muted-foreground/80">
            {ungrouped ? (
              <>
                Not a group — {formatCount(b.paths)} unplaced{" "}
                {b.paths === 1 ? "path" : "paths"}
              </>
            ) : (
              <>
                {formatCount(b.paths)} {b.paths === 1 ? "path" : "paths"}
                {b.group !== null ? (
                  <> · {GROUP_ORIGIN_LABEL[b.group.origin]}</>
                ) : null}
              </>
            )}
          </p>
        </TableCell>

        <TableCell className="num py-2.5 text-right font-medium whitespace-nowrap">
          {formatHours(b.activeMs)}
          <span className="font-normal text-muted-foreground"> h</span>
        </TableCell>
        <TableCell className="num hidden py-2.5 text-right text-muted-foreground sm:table-cell">
          {formatCount(b.paths)}
        </TableCell>
        <TableCell className="num hidden py-2.5 text-right text-muted-foreground md:table-cell">
          {formatCount(b.sessions)}
        </TableCell>
        <TableCell className="num hidden py-2.5 text-right text-muted-foreground lg:table-cell">
          {formatCount(b.threads)}
        </TableCell>
        <TableCell className="num hidden py-2.5 text-right whitespace-nowrap text-muted-foreground md:table-cell">
          {b.lastTs > 0 ? formatDate(b.lastTs) : "—"}
        </TableCell>
      </TableRow>

      {open
        ? b.members.map((p) => (
            <MemberRow key={p.projectId} project={p} newestTs={newestTs} />
          ))
        : null}
    </>
  )
}

function MemberRow({
  project: p,
  newestTs,
}: {
  project: ProjectRow
  newestTs: number | null
}) {
  const worktree = isWorktreePath(p.rootPath)
  const stale = daysStale(p.lastTs, newestTs)

  return (
    <TableRow className="border-b-0 bg-muted/15 text-muted-foreground last:border-b">
      <TableCell className="max-w-0 py-1.5 pl-7">
        <div className="flex min-w-0 items-center gap-1.5">
          <span className="truncate text-[0.8125rem]" title={p.name}>
            {p.name}
          </span>
          {worktree ? <WorktreeMarker /> : null}
          {p.groupPinned ? <PinnedMarker /> : null}
        </div>
        <span
          className="block truncate text-[0.6875rem] text-muted-foreground/70"
          title={p.rootPath}
        >
          {p.rootPath}
        </span>
      </TableCell>
      <TableCell className="num py-1.5 text-right whitespace-nowrap">
        {formatHours(p.activeMs)}
        <span className="text-muted-foreground/70"> h</span>
      </TableCell>
      <TableCell className="hidden py-1.5 sm:table-cell" />
      <TableCell className="num hidden py-1.5 text-right md:table-cell">
        {formatCount(p.sessions)}
      </TableCell>
      <TableCell className="num hidden py-1.5 text-right lg:table-cell">
        {formatCount(p.threads)}
      </TableCell>
      <TableCell className="num hidden py-1.5 text-right whitespace-nowrap md:table-cell">
        {formatDate(p.lastTs)}
        {stale >= 30 ? (
          <span className="ml-1 text-muted-foreground/60">
            · {Math.floor(stale / 30)} mo ago
          </span>
        ) : null}
      </TableCell>
    </TableRow>
  )
}

function PinnedMarker({ count }: { count?: number }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex shrink-0 items-center gap-0.5 rounded-full bg-primary/12 px-1.5 py-0.5 text-[0.625rem] text-primary">
          <Pin className="size-2.5" />
          {count !== undefined ? <span className="num">{count}</span> : null}
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-w-64">
        {count !== undefined
          ? `${count} of these paths ${count === 1 ? "was" : "were"} placed in this group by hand. `
          : "A human placed this path in this group. "}
        <code>cci group auto</code> will never move it.
      </TooltipContent>
    </Tooltip>
  )
}

function WorktreeMarker() {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex shrink-0 items-center gap-0.5 rounded-full bg-muted px-1.5 py-0.5 text-[0.625rem] text-muted-foreground">
          <GitBranch className="size-2.5" />
          worktree
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-w-72">
        A scratch worktree of this repo, placed by its path shape. Most of these
        are deleted once the branch lands, so the hours are usually history.
        The API does not report whether the directory still exists, so this page
        will not claim either way.
      </TooltipContent>
    </Tooltip>
  )
}
