import { HardDrive } from "lucide-react"
import { Tile } from "@/components/hpa-projection"
import { cn } from "@/lib/utils"
import type { DiskUse, StorageCheck } from "@/types"

const pct = (v: number | null | undefined) => (v == null ? "—" : `${Math.round(v * 100)}%`)
const gb = (v: number | null | undefined) =>
  v == null ? "—" : v >= 1000 ? `${(v / 1000).toFixed(2)} TB` : `${v.toFixed(v < 10 ? 1 : 0)} GB`

/** Kubelet's hard eviction starts at 85% used on the image filesystem. */
const EVICT = 0.85
const WARN = 0.7

export const VERDICT: Record<StorageCheck["verdict"], { label: string; cls: string }> = {
  ok: { label: "disk fits", cls: "border-series-1 text-series-1" },
  warn: { label: "disk tight", cls: "border-status-warning text-status-warning" },
  unsafe: { label: "disk will not fit", cls: "border-status-critical text-status-critical" },
  unknown: { label: "disk not measured", cls: "border-border text-muted-foreground" },
}

/** Local storage before and after one option.
 *
 *  An instance-type change is priced on CPU and memory; the disk moves with it
 *  and nothing in the packing sees that. This puts the two side by side: what
 *  each node has, how full the fullest and the average node get, and whether
 *  the SSD survives the move. */
export function StoragePanel({ storage, nodesBefore, nodesAfter, typeBefore, typeAfter }: {
  storage: StorageCheck
  nodesBefore: number
  nodesAfter: number
  typeBefore: string
  typeAfter: string
}) {
  const v = VERDICT[storage.verdict]
  const changedType = typeBefore !== typeAfter
  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap items-center gap-2 text-[12px]">
        <span className={cn("inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px] font-medium", v.cls)}>
          <HardDrive className="h-3 w-3" /> {v.label}
        </span>
        {storage.kubeletOnInstanceStore !== undefined && (
          <span className="text-muted-foreground">
            {storage.kubeletOnInstanceStore
              ? "kubelet, container images and pod logs live on the local instance-store disk today"
              : "kubelet writes to the EBS root volume today, so the instance type does not change its disk"}
          </span>
        )}
      </div>

      <div className="grid gap-2 sm:grid-cols-2">
        <Tile label={`Now · ${nodesBefore} × ${typeBefore}`} slot={1}>
          <DiskLines disk={storage.diskBefore} ssd={storage.ssdBefore}
                     perNode={storage.perNodeGbBefore} use={storage.before} />
        </Tile>
        <Tile label={`${changedType ? "After" : "Repacked"} · ${nodesAfter} × ${typeAfter}`} slot={2}>
          <DiskLines disk={storage.diskAfter} ssd={storage.ssdAfter}
                     perNode={storage.perNodeGbAfter} use={storage.after}
                     lostSsd={storage.ssdBefore && !storage.ssdAfter} />
        </Tile>
      </div>

      {(storage.before || storage.after) && (
        <div className="grid gap-1.5" aria-label="Disk use per node, before and after">
          <Meter label="Fullest node" before={storage.before?.hotShare} after={storage.after?.hotShare}
                 beforeGb={storage.before?.hotGb} afterGb={storage.after?.hotGb} />
          <Meter label="Average node" before={storage.before?.avgShare} after={storage.after?.avgShare} />
          <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-[11px] text-muted-foreground">
            <span className="inline-flex items-center gap-1.5">
              <i className="inline-block h-2 w-2 rounded-[2px] bg-series-1" /> now
            </span>
            <span className="inline-flex items-center gap-1.5">
              <i className="inline-block h-2 w-2 rounded-[2px] bg-series-2" /> after
            </span>
            <span>dashed line: kubelet starts evicting pods at {Math.round(EVICT * 100)}%</span>
            {storage.totalUsedPeakGb != null && (
              <span>· group holds {gb(storage.totalUsedPeakGb)} at its peak</span>
            )}
            {!!storage.ephRequestsGb && (
              <span>· pods request {gb(storage.ephRequestsGb)} ephemeral storage
                ({gb(storage.ephAllocatableGbAfter)} allocatable per node after)</span>
            )}
          </div>
        </div>
      )}

      {storage.notes.length > 0 && (
        <ul className={cn("grid list-disc gap-0.5 pl-5 text-[12px]",
          storage.verdict === "unsafe" ? "text-status-critical" : "text-muted-foreground")}>
          {storage.notes.map((n, i) => <li key={i}>{n}</li>)}
        </ul>
      )}

      <p className="max-w-[78ch] text-[11.5px] text-muted-foreground">
        Usage is the disk kubelet writes to on each node, measured over the window by node-exporter:
        the fullest node at its peak, and the group&apos;s total spread over the nodes this option
        leaves. The same bytes are assumed to move — images, emptyDirs and logs do not shrink with
        the node — so the fullest node after is at least as full as it is today. An EBS root volume
        is set by the launch template, not the instance type.
      </p>
    </div>
  )
}

function DiskLines({ disk, ssd, perNode, use, lostSsd }: {
  disk: string; ssd: boolean; perNode?: number; use?: DiskUse | null; lostSsd?: boolean
}) {
  return (
    <>
      <div>
        <span className="font-mono tnum font-semibold">{disk}</span>
        <span className={cn("ml-1.5 text-[11px]", lostSsd ? "font-medium text-status-critical" : "text-muted-foreground")}>
          {ssd ? "local SSD" : lostSsd ? "no local SSD" : ""}
        </span>
      </div>
      {perNode !== undefined && (
        <div className="text-muted-foreground">
          kubelet disk <span className="font-mono tnum text-foreground">{gb(perNode)}</span> per node
        </div>
      )}
      {use ? (
        <div className="text-muted-foreground">
          fullest <span className={cn("font-mono tnum", shareCls(use.hotShare))}>{pct(use.hotShare)}</span>
          {" "}· average <span className={cn("font-mono tnum", shareCls(use.avgShare))}>{pct(use.avgShare)}</span>
        </div>
      ) : (
        <div className="text-muted-foreground">usage not measured</div>
      )}
    </>
  )
}

function shareCls(v: number | null | undefined) {
  if (v == null) return "text-foreground"
  return v >= EVICT ? "font-semibold text-status-critical" : v >= WARN ? "text-status-warning" : "text-foreground"
}

/** Two bars on one 0–100% track, with the eviction line marked. */
function Meter({ label, before, after, beforeGb, afterGb }: {
  label: string
  before?: number | null
  after?: number | null
  beforeGb?: number
  afterGb?: number
}) {
  const w = (v?: number | null) => `${Math.min(100, Math.max(0, (v ?? 0) * 100))}%`
  return (
    <div className="grid grid-cols-[7.5rem_1fr_9rem] items-center gap-2 text-[11.5px]">
      <span className="text-muted-foreground">{label}</span>
      <div className="relative grid h-[18px] gap-[2px] rounded-sm bg-muted/60 p-[2px]">
        <div className="h-full rounded-[2px] bg-series-1" style={{ width: w(before) }} />
        <div className={cn("h-full rounded-[2px]", (after ?? 0) >= EVICT ? "bg-status-critical" : "bg-series-2")}
             style={{ width: w(after) }} />
        <div className="absolute inset-y-0 border-l border-dashed border-foreground/60"
             style={{ left: `${EVICT * 100}%` }} aria-hidden="true" />
      </div>
      <span className="text-right font-mono tnum">
        {pct(before)} → <span className={shareCls(after)}>{pct(after)}</span>
        {beforeGb !== undefined && afterGb !== undefined && (
          <span className="block text-[10.5px] text-muted-foreground">{gb(beforeGb)} → {gb(afterGb)}</span>
        )}
      </span>
    </div>
  )
}
