import { useMemo, useState } from "react"
import { Check, ChevronDown, ChevronsUpDown, FolderGit2 } from "lucide-react"
import { cn } from "cn"

import { Button } from "@/components/ui/button"
import {
  Command,
  CommandEmpty,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import { formatHours } from "@/lib/format"
import type { Meta, ProjectRow } from "@/lib/types"

/**
 * One control, one hierarchy.
 *
 * A **project** is atlas-chat. A **path** is one of the thirteen places it
 * was checked out — a worktree, a subdirectory (`docs/GROUPING.md`). Two
 * side-by-side controls reading "All groups" and "All projects" asked the user
 * to hold a distinction the product had never explained; nested, the subtree
 * *is* the explanation of why atlas-chat reads 111.6 h.
 *
 * Both granularities still reach the wire unchanged: a project sends `group=`,
 * a path sends `project=`, and the two union there exactly as before.
 */

export type Selection = { projects: string[]; paths: string[] }

type ProjectOption = {
  id: string
  name: string
  activeMs: number
  paths: ProjectRow[]
}

const UNPLACED = "\u0000unplaced"

function buildOptions(
  projects: Meta["groups"],
  roster: ProjectRow[],
): { options: ProjectOption[]; unplaced: ProjectRow[] } {
  const byProject = new Map<string, ProjectRow[]>()
  const unplaced: ProjectRow[] = []
  for (const p of roster) {
    if (p.groupId === null) {
      unplaced.push(p)
      continue
    }
    const list = byProject.get(p.groupId)
    if (list === undefined) byProject.set(p.groupId, [p])
    else list.push(p)
  }

  const options = projects
    .map((g) => ({
      id: g.groupId,
      name: g.name,
      activeMs: g.activeMs,
      paths: (byProject.get(g.groupId) ?? []).sort(
        (a, b) => b.activeMs - a.activeMs,
      ),
    }))
    .sort((a, b) => b.activeMs - a.activeMs)

  unplaced.sort((a, b) => b.activeMs - a.activeMs)
  return { options, unplaced }
}

export function ProjectFilter({
  projects,
  roster,
  selected,
  onChange,
}: {
  /** `meta.groups` — the project roster, all-time, unaffected by filters. */
  projects: Meta["groups"]
  /** Every path, unfiltered, carrying which project it belongs to. */
  roster: ProjectRow[]
  selected: Selection
  onChange: (next: Selection) => void
}) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState("")
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  const { options, unplaced } = useMemo(
    () => buildOptions(projects, roster),
    [projects, roster],
  )

  const projectNames = useMemo(
    () => new Map(options.map((o) => [o.id, o.name])),
    [options],
  )
  const pathNames = useMemo(
    () => new Map(roster.map((p) => [p.projectId, p.name])),
    [roster],
  )

  const total = selected.projects.length + selected.paths.length
  const label =
    total === 0
      ? "All projects"
      : total === 1
        ? (projectNames.get(selected.projects[0]) ??
          pathNames.get(selected.paths[0]) ??
          "1 selected")
        : `${total} selected`

  /* Search matches a project by its own name or by any path inside it, and a
     matched subtree opens itself — otherwise typing a worktree's name would
     find nothing, which is exactly the lookup this control exists for. */
  const q = query.trim().toLowerCase()
  const matches = (s: string) => s.toLowerCase().includes(q)
  const pathMatches = (p: ProjectRow) =>
    matches(p.name) || matches(p.rootPath)

  const shown = useMemo(() => {
    if (q === "") return options.map((o) => ({ o, hits: null as null | ProjectRow[] }))
    const out: { o: ProjectOption; hits: null | ProjectRow[] }[] = []
    for (const o of options) {
      if (matches(o.name)) out.push({ o, hits: null })
      else {
        const hits = o.paths.filter(pathMatches)
        if (hits.length > 0) out.push({ o, hits })
      }
    }
    return out
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [options, q])

  const shownUnplaced = q === "" ? unplaced : unplaced.filter(pathMatches)

  function toggleProject(id: string) {
    onChange({
      ...selected,
      projects: selected.projects.includes(id)
        ? selected.projects.filter((x) => x !== id)
        : [...selected.projects, id],
    })
  }

  function togglePath(id: string) {
    onChange({
      ...selected,
      paths: selected.paths.includes(id)
        ? selected.paths.filter((x) => x !== id)
        : [...selected.paths, id],
    })
  }

  function toggleExpanded(id: string) {
    setExpanded((s) => {
      const next = new Set(s)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const pathCount = roster.length
  const noProjectsYet = options.length === 0

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          role="combobox"
          aria-expanded={open}
          aria-label="Filter by project"
          className={cn(
            "h-9 max-w-full min-w-0 justify-between gap-2 px-3 font-normal",
            total > 0 && "border-primary/45 text-foreground",
          )}
        >
          <FolderGit2
            className={cn(
              "size-3.5 shrink-0",
              total > 0 ? "text-primary" : "text-muted-foreground",
            )}
          />
          <span className="truncate">{label}</span>
          {total > 1 ? (
            <span className="num shrink-0 rounded-full bg-primary/12 px-1.5 text-[0.6875rem] text-primary">
              {total}
            </span>
          ) : null}
          <ChevronsUpDown className="size-3.5 shrink-0 text-muted-foreground" />
        </Button>
      </PopoverTrigger>

      <PopoverContent
        align="start"
        className="w-[min(26rem,calc(100vw-2rem))] p-0"
      >
        {/* cmdk's own matching cannot see a collapsed subtree, so the filtering
            is done above and cmdk is told to keep whatever is rendered. */}
        <Command filter={() => 1} shouldFilter={false}>
          <CommandInput
            value={query}
            onValueChange={setQuery}
            placeholder={
              noProjectsYet
                ? `Search ${pathCount} paths…`
                : `Search ${options.length} projects and ${pathCount} paths…`
            }
          />

          <div className="flex items-center justify-between border-b px-3 py-1.5 text-[0.6875rem] text-muted-foreground">
            <span className="num">
              {total === 0
                ? noProjectsYet
                  ? `${pathCount} paths, none filtered`
                  : `${options.length} projects, none filtered`
                : `${total} selected`}
            </span>
            <button
              type="button"
              disabled={total === 0}
              onClick={() => onChange({ projects: [], paths: [] })}
              className="rounded-sm px-1 py-0.5 font-medium text-foreground underline-offset-2 hover:underline disabled:pointer-events-none disabled:opacity-40"
            >
              Clear all
            </button>
          </div>

          <CommandList className="max-h-[min(22rem,55vh)]">
            {shown.length === 0 && shownUnplaced.length === 0 ? (
              <CommandEmpty>No project or path matches.</CommandEmpty>
            ) : null}

            {shown.map(({ o, hits }) => {
              const on = selected.projects.includes(o.id)
              const open = hits !== null || expanded.has(o.id)
              // A project that is only ever one path has no subtree worth
              // opening — the row already is the path.
              const expandable = o.paths.length > 1
              const kids = hits ?? o.paths
              return (
                <div key={o.id}>
                  <CommandItem
                    value={o.id}
                    onSelect={() => toggleProject(o.id)}
                    className="gap-2.5"
                  >
                    <Box on={on} />
                    <span className="min-w-0 flex-1 truncate text-[0.8125rem]">
                      {o.name}
                    </span>
                    {expandable ? (
                      <button
                        type="button"
                        aria-label={`${open ? "Collapse" : "Expand"} the paths of ${o.name}`}
                        aria-expanded={open}
                        onPointerDown={(e) => e.stopPropagation()}
                        onClick={(e) => {
                          e.stopPropagation()
                          toggleExpanded(o.id)
                        }}
                        className="num flex shrink-0 items-center gap-0.5 rounded-sm px-1 py-0.5 text-[0.6875rem] text-muted-foreground hover:bg-background hover:text-foreground"
                      >
                        {o.paths.length} paths
                        <ChevronDown
                          className={cn(
                            "size-3 transition-transform",
                            open && "rotate-180",
                          )}
                        />
                      </button>
                    ) : null}
                    <span className="num w-14 shrink-0 text-right text-[0.6875rem] text-muted-foreground tabular-nums">
                      {formatHours(o.activeMs)} h
                    </span>
                  </CommandItem>

                  {open
                    ? kids.map((p) => (
                        <PathItem
                          key={p.projectId}
                          path={p}
                          on={selected.paths.includes(p.projectId)}
                          onToggle={() => togglePath(p.projectId)}
                        />
                      ))
                    : null}
                </div>
              )
            })}

            {shownUnplaced.length > 0 ? (
              <div>
                <p className="px-3 pt-2 pb-1 text-[0.6875rem] text-muted-foreground">
                  {noProjectsYet ? (
                    <>
                      Paths, one row each. <code>cci group auto</code> folds
                      them into the projects they belong to.
                    </>
                  ) : (
                    <>Paths not yet part of any project</>
                  )}
                </p>
                {shownUnplaced.map((p) => (
                  <PathItem
                    key={p.projectId}
                    path={p}
                    on={selected.paths.includes(p.projectId)}
                    onToggle={() => togglePath(p.projectId)}
                    flush
                  />
                ))}
              </div>
            ) : null}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  )
}

function PathItem({
  path: p,
  on,
  onToggle,
  flush = false,
}: {
  path: ProjectRow
  on: boolean
  onToggle: () => void
  flush?: boolean
}) {
  return (
    <CommandItem
      value={`${UNPLACED}:${p.projectId}`}
      onSelect={onToggle}
      className={cn("gap-2.5", !flush && "pl-8")}
    >
      <Box on={on} small />
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1.5">
          <span className="truncate text-[0.8125rem]">{p.name}</span>
          {p.pathExists === false ? (
            <span className="shrink-0 text-[0.625rem] text-destructive">
              gone
            </span>
          ) : null}
        </span>
        <span className="block truncate text-[0.6875rem] text-muted-foreground/80">
          {p.rootPath}
        </span>
      </span>
      <span className="num w-14 shrink-0 text-right text-[0.6875rem] text-muted-foreground tabular-nums">
        {formatHours(p.activeMs)} h
      </span>
    </CommandItem>
  )
}

function Box({ on, small = false }: { on: boolean; small?: boolean }) {
  return (
    <span
      className={cn(
        "grid shrink-0 place-items-center rounded-[4px] border transition-colors",
        small ? "size-3.5" : "size-4",
        on ? "border-primary bg-primary text-primary-foreground" : "border-input",
      )}
    >
      {on ? <Check className="size-2.5" strokeWidth={3} /> : null}
    </span>
  )
}
