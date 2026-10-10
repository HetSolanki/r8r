import { Fragment, useMemo } from "react"
import { Tile, type Ref } from "@/components/hpa-projection"
import { LineKey, MultiChart, type Series } from "@/components/multi-chart"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { HEADROOM } from "@/lib/realistic"
import { cn } from "@/lib/utils"
import type { NodeGroupTrend, Scenario } from "@/types"

const pct = (v: number | null | undefined) => (v == null ? "—" : `${Math.round(v * 100)}%`)
const stamp = (t: number) =>
  new Date(t * 1000).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })
/** Kubelet's hard eviction starts at 85% used on the image filesystem. */
const EVICT = 0.85

interface Point {
  t: number
  nodesNow: number
  nodesAfter: number
  cpuNow: number | null
  cpuAfter: number
  memNow: number | null
  memAfter: number
  diskNow: number | null
  diskAfter: number
  cpu: number
  mem: number
  disk: number | null
}

type Key = "cpu" | "mem" | "disk"

/** The group's measured usage over the window, against the capacity it had and
 *  the capacity this option would leave.
 *
 *  "After" keeps the group's own scaling pattern: where it ran 30 nodes, the
 *  option runs 30 × (its nodes ÷ today's), never below its floor. A fixed-size
 *  group is simply the option's node count throughout. Usage is node-level --
 *  DaemonSets, kubelet and system daemons included -- and is taken as
 *  unchanged, so the difference between the lines is the capacity alone. */
export function NodeGroupTrendPanel({ trend, asis, scenario, nodes = scenario.nodes }: {
  trend: NodeGroupTrend
  asis: Scenario
  scenario: Scenario
  /** The node count to draw: the reader's selection when they changed one,
   *  the option's own otherwise. */
  nodes?: number
}) {
  const ratio = nodes / Math.max(asis.nodes, 1)
  const floor = Math.max(1, scenario.minNodes ?? 1)
  const cpuScale = asis.vcpu && scenario.vcpu ? scenario.vcpu / asis.vcpu : 1
  const memScale = asis.memGib && scenario.memGib ? scenario.memGib / asis.memGib : 1
  const disk = scenario.storage
  const diskScale = disk?.perNodeGbBefore && disk.perNodeGbAfter
    ? disk.perNodeGbAfter / disk.perNodeGbBefore
    : disk?.storeGbBefore && disk.storeGbAfter ? disk.storeGbAfter / disk.storeGbBefore : 1

  const points: Point[] = useMemo(() => {
    const out: Point[] = []
    trend.t.forEach((t, i) => {
      const n = trend.nodes[i]
      const cpu = trend.cpu[i]
      const mem = trend.mem[i]
      if (!n || cpu == null || mem == null) return
      const nAfter = Math.max(floor, Math.ceil(n * ratio - 1e-9))
      const perCpu = (trend.cpuCap[i] ?? 0) / n
      const perMem = (trend.memCap[i] ?? 0) / n
      const perDisk = (trend.diskCap[i] ?? 0) / n
      const d = trend.disk[i]
      out.push({
        t, nodesNow: Math.round(n), nodesAfter: nAfter, cpu, mem, disk: d,
        cpuNow: perCpu ? cpu / (perCpu * n) : null,
        cpuAfter: perCpu ? cpu / (perCpu * cpuScale * nAfter) : 0,
        memNow: perMem ? mem / (perMem * n) : null,
        memAfter: perMem ? mem / (perMem * memScale * nAfter) : 0,
        diskNow: d != null && perDisk ? d / (perDisk * n) : null,
        diskAfter: d != null && perDisk ? d / (perDisk * diskScale * nAfter) : 0,
      })
    })
    return out
  }, [trend, ratio, floor, cpuScale, memScale, diskScale])

  if (points.length < 2) {
    return <p className="text-[12px] text-muted-foreground">Not enough node usage recorded in this window.</p>
  }

  const stat = (get: (p: Point) => number | null) => {
    const v = points.map(get).filter((x): x is number => x != null)
    return v.length ? { avg: v.reduce((a, b) => a + b, 0) / v.length, peak: Math.max(...v) } : null
  }
  const has = (k: Key) => points.some((p) => p[`${k}Now`] != null)
  const rows: { key: Key; label: string; unit: string }[] = [
    { key: "cpu", label: "CPU", unit: "cores" },
    { key: "mem", label: "Memory", unit: "GiB" },
    ...(has("disk") ? [{ key: "disk" as Key, label: "Disk (kubelet)", unit: "GB" }] : []),
  ]
  const target = scenario.headroom && scenario.headroom < 1 ? scenario.headroom : null
  const refsFor = (k: Key): Ref[] =>
    k === "disk"
      ? [{ value: EVICT, label: `kubelet evicts at ${pct(EVICT)}`, strong: true }]
      : [{ value: 1, label: "100% of the nodes", strong: true },
         // The margin a recommendation keeps at the window's peak.
         { value: k === "mem" ? HEADROOM.peakMem : HEADROOM.peakCpu,
           label: `headroom target ${pct(k === "mem" ? HEADROOM.peakMem : HEADROOM.peakCpu)}` },
         ...(target ? [{ value: target, label: `packing target ${pct(target)}` }] : [])]
  const fmtUsed = (k: Key, p: Point) =>
    k === "cpu" ? `${p.cpu.toFixed(1)} cores` : k === "mem" ? `${p.mem.toFixed(0)} GiB`
      : p.disk == null ? "—" : `${p.disk.toFixed(0)} GB`

  // Share of the window this option could not hold what the nodes used. The
  // packing is by pod request and measured pod use; memory outside that --
  // memory-backed emptyDirs, kernel slab, a host process -- is invisible to it,
  // and on prod-us2's batch-2 was 17 of the node's 19 GiB.
  const over = (k: Key) => points.filter((p) => p[`${k}After`] > 1).length / points.length
  const memOver = over("mem")
  const cpuOver = over("cpu")

  // A resource keeps its slot on every tab; now is dashed, after solid.
  const SLOT: Record<Key, 1 | 2 | 3> = { cpu: 1, mem: 2, disk: 3 }
  const seriesFor = (keys: Key[]): Series<Point>[] => keys.flatMap((k) => {
    const label = rows.find((r) => r.key === k)!.label
    return [
      { id: `${k}-now`, label: `${label} now`, slot: SLOT[k], dashed: true, get: (p: Point) => p[`${k}Now`] },
      { id: `${k}-after`, label: `${label} after`, slot: SLOT[k], get: (p: Point) => p[`${k}After`] },
    ]
  })
  // The all-in-one view carries every threshold that applies to what it shows.
  const allRefs: Ref[] = [
    { value: 1, label: "100% of the nodes", strong: true },
    ...(has("disk") ? [{ value: EVICT, label: `disk: kubelet evicts at ${pct(EVICT)}` }] : []),
  ]
  const tip = (keys: Key[]) => (p: Point) => (
    <>
      <div className="font-medium">{stamp(p.t)}</div>
      <div className="mb-1 text-[10.5px] text-muted-foreground">busiest moment in this 6-hour bucket</div>
      <div className="grid grid-cols-[1fr_auto_auto] items-center gap-x-3 gap-y-0.5">
        <span />
        <span className="inline-flex items-center gap-1 text-muted-foreground">now</span>
        <span className="inline-flex items-center gap-1 text-muted-foreground">after</span>
        <span className="text-muted-foreground">nodes</span>
        <span className="text-right font-mono tnum">{p.nodesNow}</span>
        <span className="text-right font-mono tnum">{p.nodesAfter}</span>
        {keys.map((k) => {
          const label = rows.find((r) => r.key === k)!.label
          const after = p[`${k}After`]
          return (
            <Fragment key={k}>
              <span className="inline-flex items-center gap-1.5">
                <LineKey slot={SLOT[k]} />
                <span>{label} <span className="text-muted-foreground">· {fmtUsed(k, p)}</span></span>
              </span>
              <span className="text-right font-mono tnum">{pct(p[`${k}Now`])}</span>
              <span className={cn("text-right font-mono tnum",
                after >= (k === "disk" ? EVICT : 1) && "font-semibold text-status-critical")}>
                {pct(after)}
              </span>
            </Fragment>
          )
        })}
      </div>
    </>
  )
  const keys = rows.map((r) => r.key)

  return (
    <div className="grid gap-3">
      {memOver > 0 && (
        <p className={cn("rounded-md border px-3 py-2 text-[12.5px]",
          memOver > 0.1 ? "border-status-critical/40 bg-status-critical/5" : "border-status-warning/40 bg-status-warning/5")}>
          <strong className={cn("font-semibold", memOver > 0.1 ? "text-status-critical" : "text-status-warning")}>
            {memOver > 0.1 ? "This option cannot hold what the nodes use." : "Memory would exceed the nodes at times."}
          </strong>{" "}
          The nodes used more memory than this option has for {pct(memOver)} of the window. The packing
          only sees pods&apos; requests and measured use; the rest of what the nodes hold — memory-backed
          emptyDirs, kernel memory, host processes — is not in it. Find what holds that memory before
          acting on this option.
        </p>
      )}
      {cpuOver > 0 && (
        <p className="rounded-md border border-status-warning/40 bg-status-warning/5 px-3 py-2 text-[12.5px]">
          <strong className="font-semibold text-status-warning">CPU would saturate at times.</strong>{" "}
          For {pct(cpuOver)} of the window the nodes used more CPU than this option has. CPU is
          throttled rather than killed, and Cluster Autoscaler adds nodes only for pending pods, not
          for busy ones — so expect slower bursts, or a higher node count and cost than shown.
        </p>
      )}

      <div className="grid gap-2 sm:grid-cols-3">
        {rows.map(({ key, label }) => {
          const now = stat((p) => p[`${key}Now`])
          const after = stat((p) => p[`${key}After`])
          const danger = key === "disk" ? EVICT : 1
          return (
            <Tile key={key} label={label}>
              <div className="grid grid-cols-[auto_auto_1fr] items-center gap-x-2">
                <LineKey slot={SLOT[key]} dashed />
                <span className="text-muted-foreground">now</span>
                <span className="text-right font-mono tnum">
                  {pct(now?.avg)} <span className="text-muted-foreground">avg ·</span> {pct(now?.peak)}{" "}
                  <span className="text-muted-foreground">peak</span>
                </span>
                <LineKey slot={SLOT[key]} />
                <span className="text-muted-foreground">after</span>
                <span className="text-right font-mono tnum">
                  {pct(after?.avg)} <span className="text-muted-foreground">avg ·</span>{" "}
                  <span className={cn((after?.peak ?? 0) >= danger && "font-semibold text-status-critical")}>
                    {pct(after?.peak)}
                  </span>{" "}
                  <span className="text-muted-foreground">peak</span>
                </span>
              </div>
            </Tile>
          )
        })}
      </div>

      <Tabs defaultValue="all">
        <TabsList aria-label="Which resource to chart">
          <TabsTrigger value="all">All</TabsTrigger>
          {rows.map((r) => <TabsTrigger key={r.key} value={r.key}>{r.label}</TabsTrigger>)}
          <TabsTrigger value="nodes">Nodes</TabsTrigger>
        </TabsList>
        <TabsContent value="all" className="mt-3">
          <MultiChart points={points} series={seriesFor(keys)} refs={allRefs} fmt={pct} height={240}
                      aria="CPU, memory and disk utilisation of the node group over the window, today and under this option"
                      tip={tip(keys)} />
        </TabsContent>
        {rows.map(({ key, label }) => (
          <TabsContent key={key} value={key} className="mt-3">
            <MultiChart points={points} series={seriesFor([key])} refs={refsFor(key)} fmt={pct} height={240}
                        aria={`${label} utilisation of the node group over the window, today and under this option`}
                        tip={tip([key])} />
          </TabsContent>
        ))}
        <TabsContent value="nodes" className="mt-3">
          <MultiChart points={points} step height={200} fmt={(v) => v.toFixed(0)}
                      aria="Node count over the window: observed, and under this option"
                      series={[
                        { id: "n-now", label: `nodes now (${asis.instanceType})`, slot: 4, dashed: true, get: (p) => p.nodesNow },
                        { id: "n-after", label: `nodes after (${scenario.instanceType})`, slot: 4, get: (p) => p.nodesAfter },
                      ]}
                      tip={tip(keys)} />
        </TabsContent>
      </Tabs>

      <p className="max-w-[78ch] text-[11.5px] text-muted-foreground">
        Measured on the nodes by node-exporter: CPU busy time, memory in use (excluding reclaimable
        cache) and kubelet&apos;s disk, against the group&apos;s total at each point. This is what the
        nodes used, not what pods requested — the packing above is by request, which is what the
        scheduler and Cluster Autoscaler see. The same usage is divided by this option&apos;s
        capacity, with the group scaling as it did: {ratio.toFixed(2)} × the nodes it ran, never
        below {floor}. Node-level usage includes per-node overhead that shrinks with fewer nodes, so
        &quot;after&quot; reads slightly high. Each point is a 6-hour peak, so averages are averages
        of peaks.
      </p>
    </div>
  )
}
