/** The reusable parts of shadcn's Data Table (TanStack Table v8 + the shadcn
 *  Table primitives). A view owns its `useReactTable` instance and composes
 *  these, so filtering state can also drive things outside the table. */
import type { KeyboardEvent, ReactNode } from "react"
import type { Column, Table as TanTable } from "@tanstack/react-table"
import { flexRender } from "@tanstack/react-table"
import {
  ArrowDown, ArrowUp, ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight, ChevronsUpDown,
  Settings2,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu, DropdownMenuCheckboxItem, DropdownMenuContent, DropdownMenuLabel,
  DropdownMenuSeparator, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { cn } from "@/lib/utils"

declare module "@tanstack/react-table" {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  interface ColumnMeta<TData, TValue> {
    /** Right-align header and cells (numeric columns). */
    align?: "right"
    /** Name in the column-visibility menu, when the header is not a string. */
    label?: string
    className?: string
  }
}

export function DataTableColumnHeader<TData, TValue>({
  column, title, className, hint,
}: { column: Column<TData, TValue>; title: string; className?: string; hint?: string }) {
  const right = column.columnDef.meta?.align === "right"
  if (!column.getCanSort()) return <div className={cn(right && "text-right", className)} title={hint}>{title}</div>
  const sorted = column.getIsSorted()
  const Icon = sorted === "asc" ? ArrowUp : sorted === "desc" ? ArrowDown : ChevronsUpDown
  return (
    <div className={cn("flex", right && "justify-end", className)}>
      <button
        type="button"
        title={hint ?? `Sort by ${title.toLowerCase()}`}
        aria-label={`Sort by ${title}${sorted ? `, currently ${sorted === "asc" ? "ascending" : "descending"}` : ""}`}
        onClick={column.getToggleSortingHandler()}
        className={cn(
          "-mx-1.5 inline-flex items-center gap-1 rounded px-1.5 py-0.5 uppercase tracking-[inherit] hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          sorted && "text-foreground",
        )}
      >
        {title}
        <Icon className={cn("h-3 w-3", !sorted && "opacity-50")} />
      </button>
    </div>
  )
}

export function DataTableViewOptions<TData>({ table }: { table: TanTable<TData> }) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm" className="ml-auto h-8">
          <Settings2 /> Columns
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-[190px]">
        <DropdownMenuLabel>Toggle columns</DropdownMenuLabel>
        <DropdownMenuSeparator />
        {table.getAllColumns()
          .filter((c) => c.getCanHide())
          .map((c) => (
            <DropdownMenuCheckboxItem
              key={c.id}
              checked={c.getIsVisible()}
              onCheckedChange={(v) => c.toggleVisibility(!!v)}
              onSelect={(e) => e.preventDefault()}
            >
              {c.columnDef.meta?.label ?? c.id}
            </DropdownMenuCheckboxItem>
          ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/** Page size meaning "every row"; stays correct as the filtered count changes. */
const ALL = 1_000_000_000

export function DataTablePagination<TData>({
  table, pageSizes = [25, 50, 100],
}: { table: TanTable<TData>; pageSizes?: number[] }) {
  const total = table.getFilteredRowModel().rows.length
  const { pageIndex, pageSize } = table.getState().pagination
  const from = total ? pageIndex * pageSize + 1 : 0
  const to = Math.min(total, (pageIndex + 1) * pageSize)
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 px-1 text-[12.5px] text-muted-foreground">
      <div className="tnum">{from}–{to} of {total}</div>
      <div className="flex flex-wrap items-center gap-4">
        <div className="flex items-center gap-2">
          <span>Rows per page</span>
          <Select
            value={pageSize === ALL ? "all" : String(pageSize)}
            onValueChange={(v) => table.setPageSize(v === "all" ? ALL : Number(v))}
          >
            <SelectTrigger className="h-8 w-[76px]" aria-label="Rows per page">
              <SelectValue />
            </SelectTrigger>
            <SelectContent side="top">
              {pageSizes.map((n) => <SelectItem key={n} value={String(n)}>{n}</SelectItem>)}
              <SelectItem value="all">All</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="tnum">Page {table.getPageCount() ? pageIndex + 1 : 0} of {table.getPageCount()}</div>
        <div className="flex items-center gap-1">
          {([
            [ChevronsLeft, "First page", () => table.setPageIndex(0), !table.getCanPreviousPage()],
            [ChevronLeft, "Previous page", () => table.previousPage(), !table.getCanPreviousPage()],
            [ChevronRight, "Next page", () => table.nextPage(), !table.getCanNextPage()],
            [ChevronsRight, "Last page", () => table.setPageIndex(table.getPageCount() - 1), !table.getCanNextPage()],
          ] as const).map(([Icon, label, go, disabled]) => (
            <Button key={label} variant="outline" size="icon" className="h-8 w-8"
                    aria-label={label} title={label} onClick={go} disabled={disabled}>
              <Icon />
            </Button>
          ))}
        </div>
      </div>
    </div>
  )
}

/** Header + body. Rows are clickable when `onRowClick` is given. */
export function DataTableBody<TData>({
  table, onRowClick, empty = "No results.",
}: { table: TanTable<TData>; onRowClick?: (row: TData) => void; empty?: ReactNode }) {
  const align = (a?: "right") => (a === "right" ? "text-right" : undefined)
  return (
    <Table>
      <TableHeader>
        {table.getHeaderGroups().map((hg) => (
          <TableRow key={hg.id}>
            {hg.headers.map((h) => (
              <TableHead key={h.id} className={cn(align(h.column.columnDef.meta?.align), h.column.columnDef.meta?.className)}
                         aria-sort={h.column.getIsSorted() === "asc" ? "ascending"
                                  : h.column.getIsSorted() === "desc" ? "descending" : undefined}>
                {h.isPlaceholder ? null : flexRender(h.column.columnDef.header, h.getContext())}
              </TableHead>
            ))}
          </TableRow>
        ))}
      </TableHeader>
      <TableBody>
        {table.getRowModel().rows.length ? (
          table.getRowModel().rows.map((row) => (
            <TableRow
              key={row.id}
              {...(onRowClick && {
                role: "button",
                tabIndex: 0,
                onClick: () => onRowClick(row.original),
                onKeyDown: (e: KeyboardEvent) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault()
                    onRowClick(row.original)
                  }
                },
                className: "cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
              })}
            >
              {row.getVisibleCells().map((cell) => (
                <TableCell key={cell.id} className={cn(align(cell.column.columnDef.meta?.align), cell.column.columnDef.meta?.className)}>
                  {flexRender(cell.column.columnDef.cell, cell.getContext())}
                </TableCell>
              ))}
            </TableRow>
          ))
        ) : (
          <TableRow>
            <TableCell colSpan={table.getVisibleLeafColumns().length} className="h-24 text-center text-muted-foreground">
              {empty}
            </TableCell>
          </TableRow>
        )}
      </TableBody>
    </Table>
  )
}
