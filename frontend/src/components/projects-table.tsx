import { useMemo, useState } from "react"
import { ArrowDown, ArrowUp } from "lucide-react"
import { cn } from "cn"

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { formatCount, formatDate, formatHours } from "@/lib/format"
import type { ProjectRow } from "@/lib/types"

type SortKey = "name" | "activeMs" | "sessions" | "threads" | "firstTs" | "lastTs"
type Dir = "asc" | "desc"

const COLUMNS: {
  key: SortKey
  label: string
  align: "left" | "right"
  /** Columns drop out narrowest-first so 375 px never needs a sideways scroll. */
  hide?: string
  defaultDir: Dir
}[] = [
  { key: "name", label: "Project", align: "left", defaultDir: "asc" },
  { key: "activeMs", label: "Active", align: "right", defaultDir: "desc" },
  {
    key: "sessions",
    label: "Sessions",
    align: "right",
    hide: "hidden sm:table-cell",
    defaultDir: "desc",
  },
  {
    key: "threads",
    label: "Threads",
    align: "right",
    hide: "hidden sm:table-cell",
    defaultDir: "desc",
  },
  {
    key: "firstTs",
    label: "First seen",
    align: "right",
    hide: "hidden lg:table-cell",
    defaultDir: "asc",
  },
  {
    key: "lastTs",
    label: "Last seen",
    align: "right",
    hide: "hidden md:table-cell",
    defaultDir: "desc",
  },
]

export function ProjectsTable({ projects }: { projects: ProjectRow[] }) {
  const [sort, setSort] = useState<{ key: SortKey; dir: Dir }>({
    key: "activeMs",
    dir: "desc",
  })

  const rows = useMemo(() => {
    const sign = sort.dir === "asc" ? 1 : -1
    return [...projects].sort((a, b) => {
      if (sort.key === "name") return sign * a.name.localeCompare(b.name)
      return sign * (a[sort.key] - b[sort.key])
    })
  }, [projects, sort])

  function onSort(key: SortKey, defaultDir: Dir) {
    setSort((s) =>
      s.key === key
        ? { key, dir: s.dir === "asc" ? "desc" : "asc" }
        : { key, dir: defaultDir },
    )
  }

  return (
    <section>
      <div className="mb-2.5 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="eyebrow text-foreground">Projects</h2>
        <p className="num text-[0.75rem] text-muted-foreground">
          {formatCount(rows.length)} in view
        </p>
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
                          on ? "opacity-100 text-primary" : "opacity-0",
                        )}
                      />
                    </button>
                  </TableHead>
                )
              })}
            </TableRow>
          </TableHeader>

          <TableBody>
            {rows.length === 0 ? (
              <TableRow className="hover:bg-transparent">
                <TableCell
                  colSpan={COLUMNS.length}
                  className="h-28 text-center text-sm text-muted-foreground"
                >
                  No spans match these filters.
                </TableCell>
              </TableRow>
            ) : (
              rows.map((p) => (
                <TableRow key={p.projectId}>
                  <TableCell className="max-w-0 py-2.5">
                    <span className="block truncate text-[0.8125rem] font-medium">
                      {p.name}
                    </span>
                    <span
                      className="block truncate text-[0.6875rem] text-muted-foreground/80"
                      title={p.rootPath}
                    >
                      {p.rootPath}
                    </span>
                  </TableCell>
                  <TableCell className="num py-2.5 text-right whitespace-nowrap">
                    {formatHours(p.activeMs)}
                    <span className="text-muted-foreground"> h</span>
                  </TableCell>
                  <TableCell className="num hidden py-2.5 text-right text-muted-foreground sm:table-cell">
                    {formatCount(p.sessions)}
                  </TableCell>
                  <TableCell className="num hidden py-2.5 text-right text-muted-foreground sm:table-cell">
                    {formatCount(p.threads)}
                  </TableCell>
                  <TableCell className="num hidden py-2.5 text-right whitespace-nowrap text-muted-foreground lg:table-cell">
                    {formatDate(p.firstTs)}
                  </TableCell>
                  <TableCell className="num hidden py-2.5 text-right whitespace-nowrap text-muted-foreground md:table-cell">
                    {formatDate(p.lastTs)}
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>
    </section>
  )
}
