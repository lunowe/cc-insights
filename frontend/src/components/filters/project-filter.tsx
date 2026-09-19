import { useMemo, useState } from "react"
import { Check, ChevronsUpDown, FolderGit2 } from "lucide-react"
import { cn } from "cn"

import { Button } from "@/components/ui/button"
import {
  Command,
  CommandEmpty,
  CommandGroup,
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
import type { Meta } from "@/lib/types"

type Project = Meta["projects"][number]

export function ProjectFilter({
  projects,
  selected,
  onChange,
}: {
  projects: Project[]
  selected: string[]
  onChange: (next: string[]) => void
}) {
  const [open, setOpen] = useState(false)

  const ordered = useMemo(
    () => [...projects].sort((a, b) => b.activeMs - a.activeMs),
    [projects],
  )
  const byId = useMemo(
    () => new Map(projects.map((p) => [p.projectId, p])),
    [projects],
  )

  const label =
    selected.length === 0
      ? "All projects"
      : selected.length === 1
        ? (byId.get(selected[0])?.name ?? "1 project")
        : `${selected.length} projects`

  function toggle(id: string) {
    onChange(
      selected.includes(id)
        ? selected.filter((x) => x !== id)
        : [...selected, id],
    )
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          role="combobox"
          aria-expanded={open}
          className={cn(
            "h-9 max-w-full min-w-0 justify-between gap-2 px-3 font-normal",
            selected.length > 0 && "border-primary/45 text-foreground",
          )}
        >
          <FolderGit2
            className={cn(
              "size-3.5 shrink-0",
              selected.length > 0 ? "text-primary" : "text-muted-foreground",
            )}
          />
          <span className="truncate">{label}</span>
          {selected.length > 1 ? (
            <span className="num shrink-0 rounded-full bg-primary/12 px-1.5 text-[0.6875rem] text-primary">
              {selected.length}
            </span>
          ) : null}
          <ChevronsUpDown className="size-3.5 shrink-0 text-muted-foreground" />
        </Button>
      </PopoverTrigger>

      <PopoverContent
        align="start"
        className="w-[min(24rem,calc(100vw-2rem))] p-0"
      >
        <Command
          filter={(value, search) =>
            value.toLowerCase().includes(search.toLowerCase()) ? 1 : 0
          }
        >
          <CommandInput placeholder={`Search ${ordered.length} projects…`} />

          <div className="flex items-center justify-between border-b px-3 py-1.5 text-[0.6875rem] text-muted-foreground">
            <span className="num">
              {selected.length === 0
                ? `${ordered.length} projects, none filtered`
                : `${selected.length} of ${ordered.length} selected`}
            </span>
            <button
              type="button"
              disabled={selected.length === 0}
              onClick={() => onChange([])}
              className="rounded-sm px-1 py-0.5 font-medium text-foreground underline-offset-2 hover:underline disabled:pointer-events-none disabled:opacity-40"
            >
              Clear all
            </button>
          </div>

          <CommandList className="max-h-[min(20rem,50vh)]">
            <CommandEmpty>No project matches.</CommandEmpty>
            <CommandGroup>
              {ordered.map((p) => {
                const on = selected.includes(p.projectId)
                return (
                  <CommandItem
                    key={p.projectId}
                    value={`${p.name} ${p.rootPath} ${p.projectId}`}
                    onSelect={() => toggle(p.projectId)}
                    className="gap-2.5"
                  >
                    <span
                      className={cn(
                        "grid size-4 shrink-0 place-items-center rounded-[4px] border transition-colors",
                        on
                          ? "border-primary bg-primary text-primary-foreground"
                          : "border-input",
                      )}
                    >
                      {on ? <Check className="size-3" strokeWidth={3} /> : null}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-[0.8125rem]">
                        {p.name}
                      </span>
                      <span className="block truncate text-[0.6875rem] text-muted-foreground/80">
                        {p.rootPath}
                      </span>
                    </span>
                    <span className="num shrink-0 text-[0.6875rem] text-muted-foreground tabular-nums">
                      {formatHours(p.activeMs)} h
                    </span>
                  </CommandItem>
                )
              })}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  )
}
