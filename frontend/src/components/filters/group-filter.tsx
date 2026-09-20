import { useMemo, useState } from "react"
import { Boxes, Check, ChevronsUpDown } from "lucide-react"
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
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { formatHours } from "@/lib/format"
import type { Meta } from "@/lib/types"

type Group = Meta["groups"][number]

/**
 * The coarse control, and the first one in the bar: a group is the logical
 * project behind several on-disk paths, so it is what a person means when they
 * say "atlas-chat". The project filter beside it is the fine one.
 *
 * Before `cci group auto` has run `meta.groups` is `[]` — the normal first
 * state, not an error — and the control renders inert with the command that
 * fills it.
 */
export function GroupFilter({
  groups,
  selected,
  onChange,
}: {
  groups: Group[]
  selected: string[]
  onChange: (next: string[]) => void
}) {
  const [open, setOpen] = useState(false)

  const ordered = useMemo(
    () => [...groups].sort((a, b) => b.activeMs - a.activeMs),
    [groups],
  )
  const byId = useMemo(
    () => new Map(groups.map((g) => [g.groupId, g])),
    [groups],
  )

  if (groups.length === 0) return <NoGroupsYet />

  const label =
    selected.length === 0
      ? "All groups"
      : selected.length === 1
        ? (byId.get(selected[0])?.name ?? "1 group")
        : `${selected.length} groups`

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
          aria-label="Filter by group"
          className={cn(
            "h-9 max-w-full min-w-0 justify-between gap-2 px-3 font-normal",
            selected.length > 0 && "border-primary/45 text-foreground",
          )}
        >
          <Boxes
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
          <CommandInput placeholder={`Search ${ordered.length} groups…`} />

          <div className="flex items-center justify-between border-b px-3 py-1.5 text-[0.6875rem] text-muted-foreground">
            <span className="num">
              {selected.length === 0
                ? `${ordered.length} groups, none filtered`
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
            <CommandEmpty>No group matches.</CommandEmpty>
            <CommandGroup>
              {ordered.map((g) => {
                const on = selected.includes(g.groupId)
                return (
                  <CommandItem
                    key={g.groupId}
                    value={`${g.name} ${g.groupId}`}
                    onSelect={() => toggle(g.groupId)}
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
                    <span className="min-w-0 flex-1 truncate text-[0.8125rem]">
                      {g.name}
                    </span>
                    <span className="num shrink-0 text-[0.6875rem] text-muted-foreground tabular-nums">
                      {formatHours(g.activeMs)} h
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

function NoGroupsYet() {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        {/* `span` wrapper: a disabled button fires no pointer events, so the
            tooltip would never open on its own. */}
        <span tabIndex={0} className="inline-flex rounded-md">
          <Button
            variant="outline"
            disabled
            aria-label="No groups detected yet"
            className="pointer-events-none h-9 gap-2 px-3 font-normal"
          >
            <Boxes className="size-3.5 shrink-0 text-muted-foreground" />
            <span className="truncate">No groups yet</span>
          </Button>
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-w-64">
        Every project is its own row until grouping has run. <code>cci group
        auto</code> folds worktrees and subdirectories into the repo they belong
        to.
      </TooltipContent>
    </Tooltip>
  )
}
