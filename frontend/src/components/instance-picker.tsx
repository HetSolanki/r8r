import { useState } from "react"
import { X } from "lucide-react"
import { inferType } from "@/lib/retype"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { cn, usd0 } from "@/lib/utils"
import type { Retyped } from "@/lib/retype"

export interface PickerRow {
  type: string
  vcpu: number
  memGib: number
  /** null when the option's pods do not fit this type at all. */
  priced: Retyped | null
  /** Named by the reader: specs guessed from the name, price typed in. */
  custom?: boolean
  /** A size the group lists already: Karpenter can launch it today. */
  offered?: boolean
}

const SHOWN = 8

/** Every candidate node type for this option, priced by the same engine, so
 *  the choice is made against the numbers rather than taken on trust. */
export function InstancePicker({ rows, selected, recommended, today, onPick, onCustom, onRemove }: {
  rows: PickerRow[]
  selected: string
  /** The type this option was published with. */
  recommended: string
  /** The type the group runs today. */
  today: string
  onPick: (type: string) => void
  /** Simulate a type the export does not carry. */
  onCustom: (t: { name: string; vcpu: number; memGib: number; hourlyUsd: number }) => void
  /** Drop a type the reader added. */
  onRemove: (type: string) => void
}) {
  const [all, setAll] = useState(false)
  const fits = rows.filter((r) => r.priced)
    .sort((a, b) => a.priced!.scenario.deltaUsd - b.priced!.scenario.deltaUsd)
  const nofit = rows.filter((r) => !r.priced).length
  // Always keep the three that anchor the comparison visible.
  const pinned = new Set([selected, recommended, today])
  const collapsed = fits.filter((r, i) => i < SHOWN || pinned.has(r.type))
  const visible = all ? fits : collapsed
  // Types the reader added that do not fit at all have no row to remove them from.
  const unfitCustom = rows.filter((r) => r.custom && !r.priced)

  return (
    <div>
      <div className="overflow-x-auto rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Instance type</TableHead>
              <TableHead className="text-right">vCPU · memory</TableHead>
              <TableHead className="text-right">Nodes</TableHead>
              <TableHead className="text-right">vs today</TableHead>
              <TableHead>Checks</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {visible.map((r) => {
              const p = r.priced!
              const s = p.scenario
              const active = r.type === selected
              return (
                <TableRow key={r.type} role="button" aria-pressed={active}
                          className={cn("cursor-pointer", active && "bg-accent/60 hover:bg-accent/60")}
                          onClick={() => onPick(r.type)}>
                  <TableCell className="font-mono text-[12px]">
                    <span className={cn(active && "font-semibold")}>{r.type}</span>
                    {r.type === recommended && (
                      <Badge variant="outline" className="ml-2 border-series-1 text-series-1">this option</Badge>
                    )}
                    {r.type === today && (
                      <Badge variant="outline" className="ml-2">today</Badge>
                    )}
                    {r.offered && r.type !== today && (
                      <Badge variant="outline" className="ml-2 text-muted-foreground"
                             title="listed in the pool's shapeConfigs: Karpenter can launch this size today, once the pods' requests fit it">
                        offered
                      </Badge>
                    )}
                    {r.custom && (
                      <>
                        <Badge variant="outline" className="ml-2 border-status-warning text-status-warning"
                               title={`${r.vcpu} vCPU, ${r.memGib} GiB and $${r.priced?.scenario.hourlyUsd ?? "?"}/hour as entered; disk and network not known`}>
                          your specs
                        </Badge>
                        <RemoveButton type={r.type} onRemove={onRemove} />
                      </>
                    )}
                  </TableCell>
                  <TableCell className="text-right font-mono text-[12px] text-muted-foreground tnum">
                    {r.vcpu} · {r.memGib}Gi
                  </TableCell>
                  <TableCell className="text-right font-mono tnum">{s.nodes}</TableCell>
                  <TableCell className={cn("text-right font-mono tnum",
                    s.deltaUsd < -1 ? "text-series-1" : s.deltaUsd > 1 ? "text-status-serious" : "")}>
                    {s.deltaUsd < -1 ? `−${usd0(-s.deltaUsd)}` : s.deltaUsd > 1 ? `+${usd0(s.deltaUsd)}` : "—"}/mo
                  </TableCell>
                  <TableCell className="text-[11.5px]" title={[...p.notes, ...(s.margin?.notes ?? [])].join("; ")}>
                    <Checks priced={p} />
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11.5px] text-muted-foreground">
        {fits.length > collapsed.length && (
          <Button variant="link" size="sm" className="h-auto p-0 text-[11.5px]" aria-expanded={all}
                  onClick={() => setAll(!all)}>
            {all ? `show fewer (${collapsed.length})` : `show all ${fits.length} types`}
          </Button>
        )}
        {nofit > 0 && <span>{nofit} more type(s) cannot hold this group's largest pods.</span>}
        {unfitCustom.map((r) => (
          <span key={r.type} className="inline-flex items-center gap-1">
            <span className="font-mono">{r.type}</span> (your specs) cannot hold them
            <RemoveButton type={r.type} onRemove={onRemove} />
          </span>
        ))}
      </div>
      <p className="mt-1 max-w-[90ch] text-[11.5px] text-muted-foreground">
        Same changes as this option on each type: Cluster Autoscaler replayed for today's type, a
        by-request packing for the others, then the load, headroom, disk and network checks.
      </p>
      <CustomType known={rows.map((r) => r.type)} onPick={onPick} onCustom={onCustom} />
    </div>
  )
}

function RemoveButton({ type, onRemove }: { type: string; onRemove: (type: string) => void }) {
  return (
    <button type="button" aria-label={`Remove ${type}`} title={`Remove ${type}`}
            className="ml-1 inline-flex h-5 w-5 items-center justify-center rounded align-middle
                       text-muted-foreground hover:bg-accent hover:text-foreground
                       focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            onClick={(e) => { e.stopPropagation(); onRemove(type) }}>
      <X className="h-3.5 w-3.5" />
    </button>
  )
}

/** Name any type. One the export carries is picked with EC2's own specs and
 *  price; any other is sketched from its name, and the reader confirms the
 *  specs and gives the price -- the console cannot ask EC2. */
function CustomType({ known, onPick, onCustom }: {
  known: string[]
  onPick: (type: string) => void
  onCustom: (t: { name: string; vcpu: number; memGib: number; hourlyUsd: number }) => void
}) {
  const [name, setName] = useState("")
  const [spec, setSpec] = useState<{ vcpu: string; mem: string; price: string } | null>(null)
  const clean = name.trim().toLowerCase()
  const valid = /^[a-z]+\d+[a-z-]*\.\w+$/.test(clean)
  const go = () => {
    if (!valid) return
    if (known.includes(clean)) { onPick(clean); setSpec(null); return }
    const g = inferType(clean)
    setSpec({ vcpu: g.vcpu ? String(g.vcpu) : "", mem: g.memGib ? String(g.memGib) : "", price: "" })
  }
  const ready = spec && Number(spec.vcpu) > 0 && Number(spec.mem) > 0 && Number(spec.price) > 0
  const field = "h-7 rounded-md border bg-background px-2 font-mono text-[12px]"
  return (
    <div className="mt-3 rounded-md border border-dashed p-2.5 text-[12px]">
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor="custom-type" className="font-medium">Simulate another type</label>
        <input id="custom-type" value={name} placeholder="e.g. m7i.2xlarge" spellCheck={false}
               className={`${field} w-44`}
               onChange={(e) => { setName(e.target.value); setSpec(null) }}
               onKeyDown={(e) => e.key === "Enter" && go()} />
        <Button size="sm" variant="outline" className="h-7" disabled={!valid} onClick={go}>Simulate</Button>
        {name && !valid && <span className="text-muted-foreground">family.size, like r7i.xlarge</span>}
      </div>
      {spec && (
        <div className="mt-2 grid gap-2">
          <p className="max-w-[80ch] text-muted-foreground">
            <span className="font-mono">{clean}</span> is not in this export, so its specs are guessed
            from the name — check them against EC2 — and its price is yours to give (on-demand, before
            discount). Local disk and network bandwidth are unknown for it. To get EC2's own figures,
            add it to <span className="font-mono">instanceTypes</span> in clusters.json, or run{" "}
            <span className="font-mono">export_ui.py --types {clean}</span> with AWS access.
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <label className="flex items-center gap-1">vCPU
              <input aria-label="vCPU" className={`${field} w-16`} inputMode="numeric" value={spec.vcpu}
                     onChange={(e) => setSpec({ ...spec, vcpu: e.target.value })} /></label>
            <label className="flex items-center gap-1">memory GiB
              <input aria-label="memory GiB" className={`${field} w-20`} inputMode="decimal" value={spec.mem}
                     onChange={(e) => setSpec({ ...spec, mem: e.target.value })} /></label>
            <label className="flex items-center gap-1">$/hour
              <input aria-label="price per hour" className={`${field} w-24`} inputMode="decimal"
                     placeholder="0.4032" value={spec.price}
                     onChange={(e) => setSpec({ ...spec, price: e.target.value })} /></label>
            <Button size="sm" className="h-7" disabled={!ready}
                    onClick={() => onCustom({ name: clean, vcpu: Number(spec.vcpu), memGib: Number(spec.mem),
                                              hourlyUsd: Number(spec.price) })}>
              Simulate {clean}
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}

function Checks({ priced }: { priced: Retyped }) {
  const s = priced.scenario
  const flags: { label: string; cls: string }[] = []
  if (priced.verdict === "unsafe") flags.push({ label: "unsafe", cls: "border-status-critical text-status-critical" })
  else if (priced.tight) flags.push({ label: "no headroom", cls: "border-status-warning text-status-warning" })
  if (s.storage?.verdict === "unsafe" || s.storage?.verdict === "warn")
    flags.push({ label: s.storage.verdict === "unsafe" ? "disk will not fit" : "disk tight",
                 cls: s.storage.verdict === "unsafe" ? "border-status-critical text-status-critical"
                                                     : "border-status-warning text-status-warning" })
  if (s.network?.verdict === "unsafe" || s.network?.verdict === "warn")
    flags.push({ label: s.network.verdict === "unsafe" ? "network throttled" : "network tight",
                 cls: s.network.verdict === "unsafe" ? "border-status-critical text-status-critical"
                                                     : "border-status-warning text-status-warning" })
  if (!flags.length) return <span className="text-series-1">fits with headroom</span>
  return (
    <span className="flex flex-wrap gap-1">
      {flags.map((f) => <Badge key={f.label} variant="outline" className={f.cls}>{f.label}</Badge>)}
    </span>
  )
}
