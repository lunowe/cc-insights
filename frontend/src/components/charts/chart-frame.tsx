import { useId, useState, type ReactNode } from "react"
import { Table2 } from "lucide-react"
import { cn } from "cn"

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Toggle } from "@/components/ui/toggle"

export type TableColumn = {
  key: string
  label: string
  align?: "left" | "right"
}
export type TableView = {
  columns: TableColumn[]
  rows: Record<string, ReactNode>[]
  /** Optional caption under the table, e.g. what a column means. */
  note?: ReactNode
}

/**
 * The card every chart sits in. Same bones as the reserved slot it replaces
 * (stable `id` / `data-chart-slot`, eyebrow title, one-line description) plus
 * a controls area and the table-view twin every chart must have.
 */
export function ChartFrame({
  id,
  title,
  description,
  controls,
  footer,
  table,
  className,
  bodyClassName,
  children,
}: {
  id: string
  title: string
  description: ReactNode
  controls?: ReactNode
  footer?: ReactNode
  table?: TableView
  className?: string
  bodyClassName?: string
  children: ReactNode
}) {
  const [showTable, setShowTable] = useState(false)
  const captionId = useId()

  return (
    <figure
      id={id}
      data-chart-slot={id}
      aria-labelledby={captionId}
      className={cn(
        "flex min-w-0 flex-col rounded-lg border bg-card p-4 sm:p-5",
        className,
      )}
    >
      <figcaption
        id={captionId}
        className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2"
      >
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="eyebrow">{title}</span>
          <span className="text-[0.75rem] leading-snug text-muted-foreground">
            {description}
          </span>
        </div>
        <div className="flex min-w-0 flex-wrap items-center gap-1.5">
          {controls}
          {table ? (
            <Toggle
              size="sm"
              variant="outline"
              pressed={showTable}
              onPressedChange={setShowTable}
              aria-label={showTable ? "Show chart" : "Show as table"}
              className="h-7 gap-1 px-2 text-[0.6875rem] data-[state=on]:bg-accent"
            >
              <Table2 className="size-3.5" aria-hidden />
              Table
            </Toggle>
          ) : null}
        </div>
      </figcaption>

      <div className={cn("mt-4 min-w-0 flex-1", bodyClassName)}>
        {showTable && table ? <TableTwin table={table} /> : children}
      </div>

      {footer ? (
        <div className="mt-3 text-[0.6875rem] leading-snug text-muted-foreground">
          {footer}
        </div>
      ) : null}
    </figure>
  )
}

function TableTwin({ table }: { table: TableView }) {
  return (
    <div className="max-h-96 overflow-auto rounded-md border">
      <Table className="text-[0.75rem]">
        <TableHeader className="sticky top-0 bg-card">
          <TableRow>
            {table.columns.map((c) => (
              <TableHead
                key={c.key}
                className={cn("h-8", c.align === "right" && "text-right")}
              >
                {c.label}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {table.rows.map((row, i) => (
            <TableRow key={i}>
              {table.columns.map((c) => (
                <TableCell
                  key={c.key}
                  className={cn(
                    "py-1.5",
                    c.align === "right" && "num text-right",
                  )}
                >
                  {row[c.key]}
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
      {table.note ? (
        <p className="border-t px-3 py-2 text-[0.6875rem] text-muted-foreground">
          {table.note}
        </p>
      ) : null}
    </div>
  )
}

/** A compact swatch + label legend. Rects for bars/areas. */
export function Legend({
  items,
  className,
}: {
  items: { label: ReactNode; color: string; hatched?: boolean }[]
  className?: string
}) {
  return (
    <ul
      className={cn(
        "flex flex-wrap items-center gap-x-4 gap-y-1 text-[0.6875rem] text-muted-foreground",
        className,
      )}
    >
      {items.map((it, i) => (
        <li key={i} className="flex items-center gap-1.5">
          <span
            aria-hidden
            className="inline-block h-2.5 w-2.5 shrink-0 rounded-[2px]"
            style={{
              background: it.hatched
                ? `repeating-linear-gradient(135deg, ${it.color} 0 2px, var(--card) 2px 3.5px)`
                : it.color,
            }}
          />
          <span>{it.label}</span>
        </li>
      ))}
    </ul>
  )
}
