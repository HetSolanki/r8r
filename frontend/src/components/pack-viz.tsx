import { useEffect, useState } from "react"
import { cn } from "@/lib/utils"

export interface PackedNode {
  /** Schedulable pods only — what the packer places. */
  cpu: number
  mem: number
  pods: number
  /** DaemonSet overhead: fixed per node, never packed away. */
  dsCpu?: number
  dsMem?: number
  dsPods?: number
}

/**
 * The packing, drawn.
 *
 * One box per node; the fill is how much of that node's allocatable capacity the
 * placed pods actually use, on whichever resource binds. Nodes appear in the
 * order First-Fit Decreasing filled them, so the shape of the result is visible:
 * an even wall means the packing is tight, a long tail of near-empty boxes means
 * fragmentation or a constraint that forced nodes open.
 */
export function PackViz({
  nodes,
  nodeCpu,
  nodeMem,
  binding,
  truncated,
  measured = false,
}: {
  nodes: PackedNode[]
  nodeCpu: number
  nodeMem: number
  binding: string
  truncated: number
  measured?: boolean
}) {
  const [shown, setShown] = useState(0)

  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setShown(nodes.length)
      return
    }
    setShown(0)
    let i = 0
    const step = Math.max(1, Math.round(nodes.length / 28))
    const timer = setInterval(() => {
      i += step
      setShown(Math.min(i, nodes.length))
      if (i >= nodes.length) clearInterval(timer)
    }, 28)
    return () => clearInterval(timer)
  }, [nodes])

  const byMemory = binding.startsWith("mem")
  // Capacity passed in is net of DaemonSets, which is what the packer uses. The
  // drawing adds them back so a box shows the node's real occupancy.
  const dsFirst = nodes[0] ?? {}
  const dsShare = byMemory ? (dsFirst.dsMem ?? 0) : (dsFirst.dsCpu ?? 0)
  const capacity = (byMemory ? nodeMem : nodeCpu) + dsShare
  const unit = byMemory ? "Gi" : "c"

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2 text-[12px] text-muted-foreground">
        <span>
          <strong className="font-semibold text-foreground">
            {measured ? "Observed placement" : "Simulated packing"}
          </strong>{" "}
          · {nodes.length} node{nodes.length === 1 ? "" : "s"} · fill shown on{" "}
          <strong className="font-semibold text-foreground">{byMemory ? "memory" : "CPU"}</strong>, the
          binding resource · each node holds {capacity.toFixed(byMemory ? 1 : 2)}
          {unit} allocatable
        </span>
      </div>
      <div className="flex flex-wrap gap-1">
        {nodes.slice(0, shown).map((n, i) => {
          const ds = byMemory ? (n.dsMem ?? 0) : (n.dsCpu ?? 0)
          const app = byMemory ? n.mem : n.cpu
          const dsPct = capacity > 0 ? Math.min(100, (ds / capacity) * 100) : 0
          const appPct = capacity > 0 ? Math.min(100 - dsPct, (app / capacity) * 100) : 0
          const total = dsPct + appPct
          const allPods = n.pods + (n.dsPods ?? 0)
          return (
            <div
              key={i}
              title={
                `node ${i + 1}: ${allPods} pods total\n` +
                `  ${n.pods} schedulable · ${n.cpu.toFixed(2)}c · ${n.mem.toFixed(1)}Gi\n` +
                `  ${n.dsPods ?? 0} DaemonSet · ${(n.dsCpu ?? 0).toFixed(2)}c · ${(n.dsMem ?? 0).toFixed(1)}Gi\n` +
                `  ${total.toFixed(0)}% of ${capacity.toFixed(byMemory ? 1 : 2)}${unit} allocatable`
              }
              className="relative h-16 w-[26px] overflow-hidden rounded-[3px] border bg-muted/50"
            >
              <div
                className="absolute bottom-0 left-0 right-0 bg-muted-foreground/40"
                style={{ height: `${dsPct}%` }}
              />
              <div
                className={cn(
                  "absolute left-0 right-0 transition-[height] duration-300 ease-out",
                  total > 92 ? "bg-status-serious" : "bg-series-1",
                )}
                style={{ bottom: `${dsPct}%`, height: `${appPct}%` }}
              />
              <span className="absolute inset-x-0 bottom-0.5 text-center text-[8.5px] font-medium tabular-nums text-foreground/70">
                {allPods}
              </span>
            </div>
          )
        })}
      </div>
      <div className="mt-2 flex flex-wrap gap-3 text-[11.5px] text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <i className="inline-block h-2.5 w-2.5 rounded-sm bg-series-1" /> schedulable pods
        </span>
        <span className="inline-flex items-center gap-1.5">
          <i className="inline-block h-2.5 w-2.5 rounded-sm bg-muted-foreground/40" /> DaemonSets
        </span>
        <span className="inline-flex items-center gap-1.5">
          <i className="inline-block h-2.5 w-2.5 rounded-sm bg-status-serious" /> above 92%
        </span>
        <span>number in each box is its total pod count</span>
        <span>
          · DaemonSets are drawn but never packed away: they are recreated on
          whatever nodes remain, and Cluster Autoscaler leaves them out of its
          utilisation test
        </span>
        {truncated > 0 && <span>· {truncated} further nodes not drawn</span>}
      </div>
    </div>
  )
}
