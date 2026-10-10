import { useMemo, useState } from "react"
import { ClusterName } from "@/components/cloud-icon"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { StatCard } from "@/components/stat-card"
import { CopyButton } from "@/components/copy-button"
import { checkFixtures, simulate, type Selection, type Sim } from "@/lib/simulate"
import { cn, gib, millicores } from "@/lib/utils"
import type { NodeGroup, Period } from "@/types"

const GIB = 1024 ** 3

/** StatCard formats its value as currency; these are counts and labels. */
function Tile({ label, value, sub, delayMs = 0, testId }: {
  label: string; value: string; sub: string; delayMs?: number; testId?: string
}) {
  return (
    <Card className="animate-rise p-4" style={{ animationDelay: `${delayMs}ms` }}
          data-nodes={testId}>
      <div className="text-[10.5px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">
        {label}
      </div>
      <div className="mt-1.5 font-mono text-[clamp(18px,3.2vw,24px)] font-semibold tracking-[-0.02em] tnum">
        {value}
      </div>
      <div className="mt-0.5 text-xs text-muted-foreground">{sub}</div>
    </Card>
  )
}

/** Pick the changes you would actually make, and see what they do.
 *
 *  The recommender sizes every container it can; a PR changes a handful. This
 *  packs whatever is ticked, against the same engine behind the published
 *  numbers, and says what the selection risks -- including the two traps that
 *  are invisible in a list of per-container recommendations: a request cut
 *  below measured use frees nothing, and cutting a request while lowering an
 *  HPA floor makes the HPA scale back out.
 */
export function Simulator({ period }: { period: Period }) {
  const [env, setEnv] = useState(period.clusters[0]?.env ?? "")
  const cluster = period.clusters.find((c) => c.env === env) ?? period.clusters[0]
  const groups = useMemo(
    () => cluster.nodegroups.filter((g): g is NodeGroup & { sim: Sim } => !!g.sim),
    [cluster],
  )
  const [groupName, setGroupName] = useState(groups[0]?.name ?? "")
  const group = groups.find((g) => g.name === groupName) ?? groups[0]
  const [selection, setSelection] = useState<Selection>({ containers: {}, floors: {} })

  const sim = group?.sim
  const drift = useMemo(() => (sim ? checkFixtures(sim) : []), [sim])
  const outcome = useMemo(() => (sim ? simulate(sim, selection) : null), [sim, selection])

  if (!sim || !outcome) {
    return (
      <Card className="animate-rise p-4 text-[12.5px] text-muted-foreground">
        No node group in this cluster has recorded pod placement, so nothing can be packed.
        Re-run <span className="font-mono">collect</span> with{" "}
        <span className="font-mono">--context</span>.
      </Card>
    )
  }

  const rows = sim.workloads
    .filter((w) => w.containers.length > 0 || w.hpa)
    .sort((a, b) => a.name.localeCompare(b.name))
  const picked =
    Object.keys(selection.containers).length + Object.keys(selection.floors).length

  function toggleContainer(key: string, on: boolean) {
    setSelection((s) => {
      const containers = { ...s.containers }
      if (on) containers[key] = {}
      else delete containers[key]
      return { ...s, containers }
    })
  }
  function setValue(key: string, field: "cpu" | "mem", raw: string, fallback: number | null) {
    setSelection((s) => {
      const cur = s.containers[key] ?? {}
      const n = raw.trim() === "" ? undefined : Number(raw)
      return {
        ...s,
        containers: {
          ...s.containers,
          [key]: { ...cur, [field]: n === undefined || Number.isNaN(n) ? undefined : n },
        },
      }
    })
    void fallback
  }
  function toggleFloor(key: string, suggested: number, on: boolean) {
    setSelection((s) => {
      const floors = { ...s.floors }
      if (on) floors[key] = suggested
      else delete floors[key]
      return { ...s, floors }
    })
  }

  const planLines = [
    ...Object.entries(selection.containers).map(([k, v]) => {
      const [wkey, container] = k.split("::")
      const w = sim.workloads.find((x) => x.key === wkey)!
      const c = w.containers.find((x) => x.name === container)!
      const cpu = v.cpu === undefined ? c.cpuRec : v.cpu
      const mem = v.mem === undefined ? c.memRec : v.mem
      const bits = [
        cpu != null ? `cpu=${Math.round(cpu * 1000)}m` : "",
        mem != null ? `mem=${Math.round(mem / 1024 ** 2)}Mi` : "",
      ].filter(Boolean)
      return `${w.ns}/${w.name}/${container}  ${bits.join("  ")}`
    }),
    ...Object.entries(selection.floors).map(([k, n]) => {
      const w = sim.workloads.find((x) => x.key === k)!
      return `# HPA ${w.hpa?.name}: minReplicas ${w.hpa?.min} -> ${n}`
    }),
  ]

  return (
    <div className="grid gap-4">
      {drift.length > 0 && (
        <Card className="border-status-critical/40 bg-status-critical/[0.06] p-3 text-[12.5px]">
          <strong className="font-semibold text-status-critical">
            This simulator disagrees with the analysis engine.
          </strong>{" "}
          Its packer reproduced {sim.packFixtures.length - drift.length} of{" "}
          {sim.packFixtures.length} reference cases. Treat every number below as
          unreliable: {drift[0]}
        </Card>
      )}

      <div className="flex flex-wrap gap-2">
        {period.clusters.map((c) => (
          <Button key={c.env} size="sm" variant={c.env === env ? "default" : "outline"}
                  onClick={() => { setEnv(c.env); setSelection({ containers: {}, floors: {} }) }}>
            <ClusterName cluster={c} />
          </Button>
        ))}
        <span className="mx-1 self-center text-muted-foreground">·</span>
        {groups.map((g) => (
          <Button key={g.name} size="sm" variant={g.name === group.name ? "default" : "outline"}
                  onClick={() => { setGroupName(g.name); setSelection({ containers: {}, floors: {} }) }}>
            {g.name}
          </Button>
        ))}
      </div>

      <div className="grid gap-3 sm:grid-cols-4">
        <Tile label="Nodes" value={`${outcome.before.nodes} → ${outcome.after.nodes}`}
              sub={`${sim.nodeCpu.toFixed(1)} cores · ${(sim.nodeMem / GIB).toFixed(0)}GiB each`}
              testId={`${outcome.before.nodes}-${outcome.after.nodes}`} />
        {/* The only currency figure, so the only one StatCard suits. */}
        <StatCard label="Monthly" value={outcome.monthlyDelta}
                  sub={outcome.nodesFreed > 0 ? `${outcome.nodesFreed} node(s) freed`
                       : outcome.nodesFreed < 0 ? `${-outcome.nodesFreed} more node(s)`
                       : "no change"} delayMs={60} />
        <Tile label="Selected" value={String(picked)} sub="containers and HPA floors" delayMs={120} />
        <Tile label="Binding" value={outcome.after.binding}
              sub={`cpu ${(outcome.after.cpuUtil * 100).toFixed(0)}% · mem ${(outcome.after.memUtil * 100).toFixed(0)}%`}
              delayMs={180} />
      </div>

      {outcome.risks.length > 0 && (
        <Card className="animate-rise">
          <CardHeader>
            <CardTitle>What this selection risks</CardTitle>
            <CardDescription className="max-w-[80ch]">
              Checked against the same rules as the <span className="font-mono">plan</span>{" "}
              command. A request below measured use frees nothing and removes the
              reservation that was covering it.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-1.5">
            {outcome.risks.slice(0, 12).map((r, i) => (
              <div key={i} className={cn(
                "rounded-md border px-2.5 py-1.5 text-[12px]",
                r.severity === "unsafe" ? "border-status-critical/40 bg-status-critical/[0.06]"
                : r.severity === "risk" ? "border-status-warning/40 bg-status-warning/[0.06]"
                : "border-border",
              )}>
                <span className="font-mono text-[11.5px] font-medium">{r.subject}</span>
                <div className="mt-0.5 text-muted-foreground">{r.detail}</div>
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      <Card className="animate-rise" style={{ animationDelay: "80ms" }}>
        <CardHeader className="flex-row flex-wrap items-baseline justify-between gap-2">
          <div>
            <CardTitle>{group.name}</CardTitle>
            <CardDescription className="mt-1 max-w-[76ch]">
              Tick what you would change. Values default to the recommendation and can be
              overridden — a request larger than suggested is the normal case, and the
              packing shows what it costs.
            </CardDescription>
          </div>
          <div className="flex gap-2">
            <Button size="sm" variant="outline"
                    onClick={() => setSelection({ containers: {}, floors: {} })}>
              Clear
            </Button>
            {planLines.length > 0 && (
              <CopyButton text={planLines.join("\n")} label="Copy as plan file" />
            )}
          </div>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-8" />
                <TableHead>Container</TableHead>
                <TableHead className="text-right">CPU now</TableHead>
                <TableHead className="text-right">CPU target</TableHead>
                <TableHead className="text-right">Mem now</TableHead>
                <TableHead className="text-right">Mem target</TableHead>
                <TableHead className="text-right">Peak</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.flatMap((w) => [
                ...w.containers.map((c) => {
                  const key = `${w.key}::${c.name}`
                  const on = key in selection.containers
                  const v = selection.containers[key] ?? {}
                  const cpu = v.cpu === undefined ? c.cpuRec : v.cpu
                  const mem = v.mem === undefined ? c.memRec : v.mem
                  return (
                    <TableRow key={key} className={cn(on && "bg-series-1/5")}>
                      <TableCell>
                        <input type="checkbox" checked={on} aria-label={`select ${key}`}
                               onChange={(e) => toggleContainer(key, e.target.checked)}
                               className="h-3.5 w-3.5 accent-[hsl(var(--series-1))]" />
                      </TableCell>
                      <TableCell>
                        <div className="font-mono text-[12px]">{w.name}</div>
                        <div className="text-[11px] text-muted-foreground">
                          {c.name} · {c.replicas} replica{c.replicas === 1 ? "" : "s"}
                        </div>
                      </TableCell>
                      <TableCell className="text-right font-mono tnum">{millicores(c.cpuReq)}</TableCell>
                      <TableCell className="text-right">
                        {on ? (
                          <Input aria-label={`${key} cpu`} className="h-7 w-24 text-right font-mono"
                                 defaultValue={cpu == null ? "" : Math.round(cpu * 1000)}
                                 placeholder="m"
                                 onChange={(e) => setValue(key, "cpu",
                                   e.target.value === "" ? "" : String(Number(e.target.value) / 1000), c.cpuRec)} />
                        ) : (
                          <span className="font-mono text-muted-foreground tnum">
                            {c.cpuRec == null ? "—" : millicores(c.cpuRec)}
                          </span>
                        )}
                      </TableCell>
                      <TableCell className="text-right font-mono tnum">{gib(c.memReq / GIB)}</TableCell>
                      <TableCell className="text-right">
                        {on ? (
                          <Input aria-label={`${key} mem`} className="h-7 w-24 text-right font-mono"
                                 defaultValue={mem == null ? "" : Math.round(mem / 1024 ** 2)}
                                 placeholder="Mi"
                                 onChange={(e) => setValue(key, "mem",
                                   e.target.value === "" ? "" : String(Number(e.target.value) * 1024 ** 2), c.memRec)} />
                        ) : (
                          <span className="font-mono text-muted-foreground tnum">
                            {c.memRec == null ? "—" : gib(c.memRec / GIB)}
                          </span>
                        )}
                      </TableCell>
                      <TableCell className="text-right font-mono text-[11px] text-muted-foreground tnum">
                        {millicores(w.peakCpu)} · {gib(w.peakMem / GIB)}
                      </TableCell>
                    </TableRow>
                  )
                }),
                ...(w.hpa
                  ? [(
                      <TableRow key={`${w.key}::hpa`}
                                className={cn(w.key in selection.floors && "bg-series-3/5")}>
                        <TableCell>
                          <input type="checkbox" checked={w.key in selection.floors}
                                 aria-label={`select floor ${w.key}`}
                                 onChange={(e) => toggleFloor(w.key, w.hpa!.suggested, e.target.checked)}
                                 className="h-3.5 w-3.5 accent-[hsl(var(--series-3))]" />
                        </TableCell>
                        <TableCell colSpan={2}>
                          <div className="font-mono text-[12px]">{w.hpa.name}</div>
                          <div className="text-[11px] text-muted-foreground">
                            HPA floor · replay {w.hpa.replayFloor} · busiest sample wanted{" "}
                            {w.hpa.peakDesired}
                          </div>
                        </TableCell>
                        <TableCell className="text-right font-mono tnum">
                          minReplicas {w.hpa.min}
                        </TableCell>
                        <TableCell className="text-right" colSpan={3}>
                          {w.key in selection.floors ? (
                            <Input aria-label={`${w.key} floor`}
                                   className="h-7 w-24 text-right font-mono"
                                   defaultValue={selection.floors[w.key]}
                                   onChange={(e) => {
                                     const n = Number(e.target.value)
                                     if (!Number.isNaN(n) && n > 0) toggleFloor(w.key, n, true)
                                   }} />
                          ) : (
                            <span className="font-mono text-muted-foreground tnum">
                              → {w.hpa.suggested}
                            </span>
                          )}
                        </TableCell>
                      </TableRow>
                    )]
                  : []),
              ])}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <p className="max-w-[86ch] text-[12px] text-muted-foreground">
        Packed in the browser by a port of the analysis engine's own packer, checked
        against{" "}
        <span className="font-mono">{sim.packFixtures.length}</span> cases it produced.
        Cluster Autoscaler will not act on this by itself — see the blockers on each
        option in Recommendations. Copy the selection as a plan file and run{" "}
        <span className="font-mono">rightsizer --plan-file … plan</span> for the
        authoritative figure.
      </p>
    </div>
  )
}
