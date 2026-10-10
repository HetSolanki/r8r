import { useMemo, useState } from "react"
import { ClusterName } from "@/components/cloud-icon"
import {
  getCoreRowModel, getFilteredRowModel, getPaginationRowModel, getSortedRowModel, useReactTable,
  type ColumnDef, type ColumnFiltersState, type FilterFn, type SortingState, type VisibilityState,
} from "@tanstack/react-table"
import { X } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Card as StatShell } from "@/components/ui/card"
import {
  DataTableBody, DataTableColumnHeader, DataTablePagination, DataTableViewOptions,
} from "@/components/data-table"
import { Delta, DirectionBadge } from "@/components/delta"
import { UsageCell } from "@/components/usage-cell"
import { WorkloadDialog } from "@/views/workload-dialog"
import { summarise } from "@/lib/fleet"

import type { ContainerWithEnv, Direction, Period } from "@/types"

/** Biggest mover in whichever direction -- an increase that risks an OOMKill
 *  matters as much as a cut that saves money. The default sort. */
const impact = (c: ContainerWithEnv) => Math.max(Math.abs(c.cpuDelta), Math.abs(c.memDelta))

const matchesQuery: FilterFn<ContainerWithEnv> = (row, _id, value: string) => {
  const q = value.trim().toLowerCase()
  const c = row.original
  return !q || `${c.workload} ${c.kind} ${c.container} ${c.ns} ${c.nodegroup}`.toLowerCase().includes(q)
}

const equals: FilterFn<ContainerWithEnv> = (row, id, value) => row.getValue(id) === value

const columns: ColumnDef<ContainerWithEnv>[] = [
  {
    id: "workload",
    accessorKey: "workload",
    header: ({ column }) => <DataTableColumnHeader column={column} title="Workload" />,
    enableHiding: false,
    cell: ({ row: { original: c } }) => (
      <>
        <div className="font-mono font-medium underline decoration-border underline-offset-[3px]">
          {c.workload}
        </div>
        <div className="text-[11px] text-muted-foreground">
          {c.env} · {c.ns} · {c.nodegroup}
        </div>
      </>
    ),
  },
  {
    id: "status",
    accessorFn: (c) => (c.actionable === false ? 1 : 0),
    header: ({ column }) => <DataTableColumnHeader column={column} title="Status" />,
    meta: { label: "Status" },
    cell: ({ row: { original: c } }) => {
      // Why a container has no recommendation is as much a finding as the
      // recommendation itself -- OOMKilled, no request declared, too little data.
      const shown = (c.blockers ?? []).filter((b) => b.severity !== "advisory")
      if (c.actionable !== false) return <span className="text-[11.5px] text-status-good">change</span>
      return (
        <div className="flex max-w-[26ch] flex-wrap gap-1" title={shown.map((b) => `${b.code}: ${b.detail}`).join("\n")}>
          {shown.slice(0, 2).map((b) => (
            <Badge key={b.code} variant="outline" className={
              b.severity === "unsafe" ? "border-status-critical px-1.5 py-0 font-mono text-[10px] text-status-critical"
                : "border-status-warning px-1.5 py-0 font-mono text-[10px] text-status-warning"}>
              {b.code}
            </Badge>
          ))}
          {shown.length > 2 && <span className="text-[10px] text-muted-foreground">+{shown.length - 2}</span>}
        </div>
      )
    },
  },
  {
    id: "kind",
    accessorFn: (c) => c.kind ?? "",
    header: ({ column }) => <DataTableColumnHeader column={column} title="Type" />,
    meta: { label: "Type" },
    filterFn: equals,
    cell: ({ getValue }) => {
      const kind = getValue<string>()
      return kind ? <Badge variant="outline" className="px-1.5 py-0 text-[10px]">{kind}</Badge> : "—"
    },
  },
  {
    id: "container",
    accessorKey: "container",
    header: ({ column }) => <DataTableColumnHeader column={column} title="Container" />,
    meta: { label: "Container", className: "font-mono text-muted-foreground" },
  },
  {
    id: "replicas",
    accessorFn: (c) => c.replicasNow ?? c.replicas,
    header: ({ column }) => <DataTableColumnHeader column={column} title="Replicas" />,
    meta: { label: "Replicas", align: "right", className: "font-mono tnum" },
    sortDescFirst: true,
    cell: ({ row: { original: c } }) => (
      // The headline is what is running. The 30-day peak used to be the only
      // number here, under a heading that read as "current" -- 104 against 75
      // actually running.
      <>
        <div>{c.replicasNow ?? c.replicas}</div>
        {(c.replicasAvg ?? 0) > 0 && (
          <div className="text-[10.5px] text-muted-foreground" title="avg over the window · 30-day peak">
            avg {Math.round(c.replicasAvg!)} · peak {c.replicas}
          </div>
        )}
      </>
    ),
  },
  {
    id: "direction",
    accessorKey: "direction",
    header: ({ column }) => <DataTableColumnHeader column={column} title="Effect" />,
    meta: { label: "Effect" },
    enableSorting: false,
    filterFn: equals,
    cell: ({ row: { original: c } }) => <DirectionBadge direction={c.direction} />,
  },
  {
    id: "cpu",
    accessorFn: (c) => c.cpuUtilPct ?? -1,
    header: ({ column }) => (
      <DataTableColumnHeader column={column} title="CPU / replica · observed"
                             hint="Sort by peak as a share of the request" />
    ),
    meta: { label: "CPU / replica", align: "right" },
    sortDescFirst: true,
    cell: ({ row: { original: c } }) => (
      <UsageCell kind="cpu" request={c.cpuReq} recommended={c.cpuRec}
                 ladder={c.cpuLadder} limit={c.cpuLimit}
                 avg={c.cpuAvg} p95={c.cpuP95} peak={c.cpuPeak} utilPct={c.cpuUtilPct} />
    ),
  },
  {
    id: "cpuDelta",
    accessorKey: "cpuDelta",
    header: ({ column }) => <DataTableColumnHeader column={column} title="CPU Δ fleet" />,
    meta: { label: "CPU Δ fleet", align: "right" },
    cell: ({ row: { original: c } }) => <Delta value={c.cpuDelta} unit="c" />,
  },
  {
    id: "mem",
    accessorFn: (c) => c.memUtilPct ?? -1,
    header: ({ column }) => (
      <DataTableColumnHeader column={column} title="Memory / replica · observed"
                             hint="Sort by peak as a share of the request" />
    ),
    meta: { label: "Memory / replica", align: "right" },
    sortDescFirst: true,
    cell: ({ row: { original: c } }) => (
      <UsageCell kind="mem" request={c.memReq} recommended={c.memRec}
                 ladder={c.memLadder} limit={c.memLimit}
                 avg={c.memAvg} p95={c.memP95} peak={c.memPeak} utilPct={c.memUtilPct} />
    ),
  },
  {
    id: "memDelta",
    accessorKey: "memDelta",
    header: ({ column }) => <DataTableColumnHeader column={column} title="Memory Δ fleet" />,
    meta: { label: "Memory Δ fleet", align: "right" },
    cell: ({ row: { original: c } }) => <Delta value={c.memDelta} unit="Gi" />,
  },
  {
    id: "coinc",
    accessorFn: (c) => c.coinc ?? -1,
    header: ({ column }) => <DataTableColumnHeader column={column} title="Coinc" />,
    meta: { label: "Coinc", align: "right", className: "font-mono text-muted-foreground tnum" },
    cell: ({ row: { original: c } }) => (c.coinc !== null ? `${Math.round(c.coinc * 100)}%` : "—"),
  },
  // Hidden: carries the default "biggest mover" order as an ordinary sort, so
  // clicking any header replaces it and Reset brings it back.
  { id: "impact", accessorFn: impact, enableHiding: false },
]

const DEFAULT_SORT: SortingState = [{ id: "impact", desc: true }]

export function Workloads({ period }: { period: Period }) {
  const [env, setEnv] = useState("all")
  // Actionable by default -- the list this view always showed -- with the
  // containers held back one click away rather than absent.
  const [status, setStatus] = useState<"actionable" | "held" | "all">("actionable")
  const [open, setOpen] = useState<ContainerWithEnv | null>(null)
  const [sorting, setSorting] = useState<SortingState>(DEFAULT_SORT)
  const [columnFilters, setColumnFilters] = useState<ColumnFiltersState>([])
  const [globalFilter, setGlobalFilter] = useState("")
  const [columnVisibility, setColumnVisibility] = useState<VisibilityState>({ impact: false })
  const all = useMemo(() => summarise(period).containers, [period])

  const byEnv = useMemo(
    () => all.filter((c) =>
      (env === "all" || c.env === env)
      && (status === "all" || (status === "held") === (c.actionable === false))),
    [all, env, status],
  )
  const heldCount = useMemo(
    () => all.filter((c) => (env === "all" || c.env === env) && c.actionable === false).length,
    [all, env],
  )

  const table = useReactTable({
    data: byEnv,
    columns,
    state: { sorting, columnFilters, globalFilter, columnVisibility },
    onSortingChange: setSorting,
    onColumnFiltersChange: setColumnFilters,
    onGlobalFilterChange: setGlobalFilter,
    onColumnVisibilityChange: setColumnVisibility,
    globalFilterFn: matchesQuery,
    getRowId: (c) => `${c.env}/${c.ns}/${c.workload}/${c.kind}/${c.container}`,
    getCoreRowModel: getCoreRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    initialState: { pagination: { pageIndex: 0, pageSize: 25 } },
  })

  const kind = (table.getColumn("kind")?.getFilterValue() as string | undefined) ?? "all"
  const dir = (table.getColumn("direction")?.getFilterValue() as Direction | undefined) ?? "all"

  // The summary answers "what does this slice do": cluster and type narrow it;
  // the effect buttons and the search only narrow the list being read.
  const scoped = useMemo(() => byEnv.filter((c) => kind === "all" || c.kind === kind), [byEnv, kind])
  const kinds = useMemo(() => {
    const n = new Map<string, number>()
    for (const c of byEnv) if (c.kind) n.set(c.kind, (n.get(c.kind) ?? 0) + 1)
    return [...n].sort((a, b) => b[1] - a[1])
  }, [byEnv])

  const cpuFreed = scoped.reduce((t, c) => t + Math.min(0, c.cpuDelta), 0)
  const memAdded = scoped.reduce((t, c) => t + Math.max(0, c.memDelta), 0)
  const counts = scoped.reduce<Record<string, number>>((acc, c) => {
    acc[c.direction] = (acc[c.direction] ?? 0) + 1
    return acc
  }, {})

  const filtered = globalFilter !== "" || columnFilters.length > 0 || sorting !== DEFAULT_SORT
  const reset = () => {
    setGlobalFilter("")
    setColumnFilters([])
    setSorting(DEFAULT_SORT)
  }

  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant={env === "all" ? "default" : "outline"} onClick={() => setEnv("all")}>
          All clusters
        </Button>
        {period.clusters.map((c) => (
          <Button
            key={c.env}
            size="sm"
            variant={env === c.env ? "default" : "outline"}
            className="font-mono"
            onClick={() => setEnv(c.env)}
          >
            <ClusterName cluster={c} />
          </Button>
        ))}
        <span className="mx-1 h-5 w-px bg-border" aria-hidden />
        {(
          [
            ["actionable", "To change"],
            ["held", `Left alone (${heldCount})`],
            ["all", "All containers"],
          ] as ["actionable" | "held" | "all", string][]
        ).map(([k, label]) => (
          <Button key={k} size="sm" variant={status === k ? "default" : "outline"} onClick={() => setStatus(k)}>
            {label}
          </Button>
        ))}
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <StatShell className="animate-rise p-4">
          <div className="text-[10.5px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">
            CPU freed
          </div>
          <div className="mt-1 text-[clamp(18px,3vw,24px)] font-semibold">
            <Delta value={cpuFreed} unit=" cores" />
          </div>
          <div className="mt-0.5 text-xs text-muted-foreground">across every cut</div>
        </StatShell>
        <StatShell className="animate-rise p-4" style={{ animationDelay: "50ms" }}>
          <div className="text-[10.5px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">
            Memory needed
          </div>
          <div className="mt-1 text-[clamp(18px,3vw,24px)] font-semibold">
            <Delta value={memAdded} unit=" GiB" />
          </div>
          <div className="mt-0.5 text-xs text-muted-foreground">stability fixes, not savings</div>
        </StatShell>
        <StatShell className="animate-rise p-4" style={{ animationDelay: "100ms" }}>
          <div className="text-[10.5px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">
            Net effect
          </div>
          <div className="mt-1 text-[13px] leading-snug">
            {memAdded > Math.abs(cpuFreed) * 2
              ? "Memory grows faster than CPU shrinks — node counts stay memory-bound."
              : "CPU savings outweigh the memory added."}
          </div>
        </StatShell>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {(
          [
            ["all", `Everything (${scoped.length})`],
            ["frees", `Frees capacity (${counts.frees ?? 0})`],
            ["needs", `Needs more (${counts.needs ?? 0})`],
            ["mixed", `CPU down, RAM up (${counts.mixed ?? 0})`],
          ] as [Direction | "all", string][]
        ).map(([k, label]) => (
          <Button key={k} size="sm" variant={dir === k ? "default" : "outline"}
                  onClick={() => table.getColumn("direction")?.setFilterValue(k === "all" ? undefined : k)}>
            {label}
          </Button>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Input
          id="workload-filter"
          type="search"
          placeholder="Filter workloads…"
          aria-label="Filter workloads"
          className="max-w-[240px]"
          value={globalFilter}
          onChange={(e) => setGlobalFilter(e.target.value)}
        />
        <Select value={kind}
                onValueChange={(v) => table.getColumn("kind")?.setFilterValue(v === "all" ? undefined : v)}>
          <SelectTrigger className="w-[180px]" aria-label="Workload type">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All types ({byEnv.length})</SelectItem>
            {kinds.map(([k, n]) => <SelectItem key={k} value={k}>{k} ({n})</SelectItem>)}
          </SelectContent>
        </Select>
        {filtered && (
          <Button size="sm" variant="ghost" onClick={reset}>
            Reset <X />
          </Button>
        )}
        <span className="text-[12.5px] text-muted-foreground">
          {table.getFilteredRowModel().rows.length} shown
        </span>
        <DataTableViewOptions table={table} />
      </div>

      <Card className="animate-rise">
        <DataTableBody table={table} onRowClick={setOpen} empty="No workloads match these filters." />
      </Card>
      <DataTablePagination table={table} />

      <WorkloadDialog container={open} onClose={() => setOpen(null)} />

      <p className="max-w-[74ch] text-xs text-muted-foreground">
        Select a row for its CPU and memory over the window, with the spread across replicas and
        their total. Click a column heading to sort by it; the CPU and memory columns sort by peak as a
        share of the request.{" "}
        <strong className="font-semibold">avg · p95 · peak</strong> is the usage measured per replica over
        the window — the evidence for each change. CPU is sized from p95 because it throttles; memory from
        the peak because it is OOMKilled.{" "}
        <strong className="font-semibold">Δ fleet</strong> is the change across every replica, signed:
        blue frees capacity, orange needs more. A memory increase costs nothing until it forces an extra
        node — the Scenarios view shows whether it does.{" "}
        <strong className="font-semibold">Coinc</strong> is what all
        replicas used together at their busiest moment, against what you would reserve assuming every replica
        peaked at once — low means the fleet needs far less than per-replica sizing implies. It is undefined below
        two replicas.
      </p>
    </div>
  )
}
