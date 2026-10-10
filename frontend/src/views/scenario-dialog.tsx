import { Fragment, useEffect, useMemo, useRef, useState } from "react"
import { ChartSpline, ChevronRight, RotateCcw } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog, DialogBody, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Delta } from "@/components/delta"
import { HpaProjection, MIN_FLOOR } from "@/components/hpa-projection"
import { NumberField } from "@/components/number-field"
import { PackViz } from "@/components/pack-viz"
import { RequestProjection } from "@/components/request-projection"
import { VERDICT } from "@/components/storage-panel"
import { NetworkRows } from "@/components/network-rows"
import { NodeGroupTrendPanel } from "@/components/nodegroup-trend"
import { InstancePicker, type PickerRow } from "@/components/instance-picker"
import { customType, retype, type InstanceType, type Retyped } from "@/lib/retype"
import { UsageCell } from "@/components/usage-cell"
import { cn, usd0 } from "@/lib/utils"
import { hpaId, isCustomised, priceSelection, reqId, useChoices, type Priced } from "@/lib/choices"
import type { Driver, NodeGroup, Scenario } from "@/types"
import { sizeText } from "@/lib/fleet-text"

/** Everything behind one scenario's number, in one place.
 *
 *  A scenario row says "38 nodes, −$6,098". On its own that is a claim. This
 *  shows the three things needed to act on it: which requests change, what a
 *  node becomes, and how the pods actually land. */
export function ScenarioDialog(props: {
  env: string
  collectedAt: string
  group: NodeGroup | null
  scenario: Scenario | null
  asis: Scenario | undefined
  onClose: () => void
  onShowTrend?: (driver: Driver) => void
}) {
  if (!props.scenario || !props.group) return null
  return <ScenarioDialogPicked {...props} group={props.group} scenario={props.scenario} />
}

/** The option as published, or re-priced on the instance type the reader picked. */
function ScenarioDialogPicked(props: Parameters<typeof ScenarioDialog>[0] & {
  group: NodeGroup; scenario: Scenario
}) {
  const { group, scenario, asis } = props
  const [type, setType] = useState(scenario.instanceType)
  // Types the reader named that the export does not carry, with their specs.
  const [custom, setCustom] = useState<InstanceType[]>([])
  useEffect(() => { setType(scenario.instanceType) }, [scenario.key, scenario.instanceType])
  const choices = group.instanceChoices
  // Every candidate, priced once when the option opens: the table compares
  // them, and picking one is then instant.
  const rows: PickerRow[] = useMemo(() => {
    if (!choices?.types?.length || !asis || !group.sim?.model) return []
    return [
      ...choices.types.map((t) => ({
        type: t.name, vcpu: t.vcpu, memGib: t.memGib, offered: t.offered,
        priced: t.name === scenario.instanceType
          ? { scenario, verdict: (scenario.load?.verdict ?? "unknown") as Retyped["verdict"], notes: [],
              tight: scenario.margin ? !scenario.margin.ok : false }
          : retype(group, scenario, asis, choices, t.name),
      })),
      ...custom.map((t) => ({ type: t.name, vcpu: t.vcpu, memGib: t.memGib, custom: true,
                              priced: retype(group, scenario, asis, choices, t.name, t) })),
    ]
  }, [choices, asis, group, scenario, custom])
  const picked = rows.find((r) => r.type === type)?.priced
  const shown = type !== scenario.instanceType && picked ? picked.scenario : scenario
  return (
    <ScenarioDialogInner {...props} scenario={shown}
      // A Karpenter pool has no type to pick: Karpenter chooses from what the
      // pool lists, and each option's fleet is that choice replayed.
      picker={rows.length > 1 && choices && !group.fleet?.length ? {
        rows, selected: type, recommended: scenario.instanceType, onPick: setType,
        onCustom: (t) => {
          setCustom((cs) => [...cs.filter((c) => c.name !== t.name),
                             customType(t.name, t.vcpu, t.memGib, t.hourlyUsd, choices)])
          setType(t.name)
        },
        onRemove: (name) => {
          setCustom((cs) => cs.filter((c) => c.name !== name))
          // Removing the type on show falls back to the option as published.
          if (type === name) setType(scenario.instanceType)
        },
        picked: type !== scenario.instanceType ? picked ?? null : null, original: scenario,
      } : undefined} />
  )
}

function ScenarioDialogInner({
  group,
  scenario,
  asis,
  env,
  collectedAt,
  onClose,
  onShowTrend,
  picker,
}: {
  /** The instance-type choice, when the group ships its candidates. */
  picker?: {
    rows: PickerRow[]; selected: string; recommended: string; onPick: (t: string) => void
    onCustom: (t: { name: string; vcpu: number; memGib: number; hourlyUsd: number }) => void
    onRemove: (type: string) => void
    picked: Retyped | null; original: Scenario
  }
  /** The cluster, for fetching trend files. */
  env: string
  /** When this cluster was collected, stored beside saved choices so a choice
   *  made against older data says so. */
  collectedAt: string
  group: NodeGroup
  scenario: Scenario
  asis: Scenario | undefined
  onClose: () => void
  /** Open the usage trend for one of the containers listed below. The caller
   *  swaps this dialog out rather than stacking one on top of it. */
  onShowTrend?: (driver: Driver) => void
}) {
  // Which HPA rows are expanded to their projection.
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  // The instance-type table: there when wanted, not in the way otherwise.
  const [showTypes, setShowTypes] = useState(false)
  // Which request rows are expanded to their utilisation projection.
  const [expandedReq, setExpandedReq] = useState<Set<string>>(new Set())
  const requires = scenario.requires ?? []
  // Derived from the steps, not from the key. A key prefix stopped describing
  // the option the moment levers could combine: "rightsize-replicas-r6id.2xlarge"
  // starts with "rightsize", so the container table showed and the HPA table --
  // the other half of what that option actually changes -- did not.
  const changesRequests = requires.some((r) => r.includes("container request changes"))
  const changesReplicas = requires.some((r) => r.includes("minReplicas"))
  // The scenario's own floors when it carries them: an option that also cuts
  // requests produces different ones from the replica-only option.
  const replicaChanges = useMemo(
    () => (changesReplicas ? (scenario.replicaChanges ?? group.replicaChanges ?? []) : []),
    [changesReplicas, scenario.replicaChanges, group.replicaChanges])
  // A node-config comparison needs specs on both sides; an older payload has neither.
  const changesNodes =
    !!asis && scenario.instanceType !== asis.instanceType && !!scenario.vcpu && !!asis.vcpu
  const drivers = useMemo(
    () => (changesRequests ? (group.drivers ?? []) : []), [changesRequests, group.drivers])

  const { choices, update, reset, persisted } = useChoices(
    `rightsizer:choices:${env}:${group.name}:${scenario.key}`, collectedAt)
  type RC = (typeof replicaChanges)[number]
  const hpaOn = (r: RC) => !choices.offHpa.includes(hpaId(r))
  const reqOn = (d: Driver) => !choices.offReq.includes(reqId(d))
  const floorOf = (r: RC) => choices.floors[hpaId(r)] ?? r.suggestedMin
  const cpuOf = (d: Driver) => choices.requests[reqId(d)]?.cpu ?? d.cpuRec
  const memOf = (d: Driver) => choices.requests[reqId(d)]?.mem ?? d.memRec
  // Fleet-wide deltas of what is ticked, at the values chosen.
  const cpuDeltaOf = (d: Driver) => {
    const v = cpuOf(d)
    return v == null ? 0 : (v - d.cpuReq) * Math.max(d.replicas, 1)
  }
  const memDeltaOf = (d: Driver) => {
    const v = memOf(d)
    return v == null ? 0 : (v - d.memReq) * Math.max(d.replicas, 1)
  }
  const freedOf = (r: RC) => (r.minReplicas - floorOf(r)) * r.requestPerReplica
  const cpuFreed = drivers.filter(reqOn).reduce((t, d) => t + Math.min(0, cpuDeltaOf(d)), 0)
  const memAdded = drivers.filter(reqOn).reduce((t, d) => t + Math.max(0, memDeltaOf(d)), 0)

  const priced = useMemo(
    // A Karpenter pool's row is Karpenter's own provisioning and consolidation
    // replayed in the CLI; the browser's packer is not that, so a partial
    // selection is not priced rather than priced by a different rule.
    () => (scenario.fleet?.length ? null : priceSelection(group, scenario, asis, drivers, replicaChanges, choices)),
    [group, scenario, asis, drivers, replicaChanges, choices],
  )
  const customised = isCustomised(choices)
  // The packing drawn is the one the header prices: the reader's selection when
  // they changed anything, the option as published otherwise.
  const live = customised && priced?.outcome.after.placement.length ? priced.outcome.after : null
  const placement = live?.placement ?? scenario.placement ?? []
  const placementNodes = live ? priced!.nodes : scenario.nodes
  // The utilisation trend follows the same selection as the header.
  const trendNodes = customised && priced ? priced.nodes : scenario.nodes
  const placementBinding = live ? live.binding : scenario.binding
  const hasLevers = drivers.length > 0 || replicaChanges.length > 0

  const setFloor = (r: RC, n: number) => update((c) => {
    const floors = { ...c.floors }
    if (n === r.suggestedMin) delete floors[hpaId(r)]
    else floors[hpaId(r)] = n
    return { ...c, floors }
  })
  const setRequest = (d: Driver, field: "cpu" | "mem", n: number) => update((c) => {
    const own = { ...(c.requests[reqId(d)] ?? {}) }
    const rec = field === "cpu" ? d.cpuRec : d.memRec
    if (rec != null && Math.abs(n - rec) < 1e-6) delete own[field]
    else own[field] = n
    const requests = { ...c.requests }
    if (Object.keys(own).length) requests[reqId(d)] = own
    else delete requests[reqId(d)]
    return { ...c, requests }
  })
  const resetRow = (kind: "hpa" | "req", id: string) => update((c) => {
    if (kind === "hpa") {
      const floors = { ...c.floors }
      delete floors[id]
      return { ...c, floors, offHpa: c.offHpa.filter((x) => x !== id) }
    }
    const requests = { ...c.requests }
    delete requests[id]
    return { ...c, requests, offReq: c.offReq.filter((x) => x !== id) }
  })
  const toggle = (kind: "offHpa" | "offReq", id: string, on: boolean) => update((c) => ({
    ...c,
    [kind]: on ? c[kind].filter((x) => x !== id) : [...new Set([...c[kind], id])],
  }))
  const toggleAll = (kind: "offHpa" | "offReq", ids: string[], on: boolean) => update((c) => ({
    ...c,
    [kind]: on ? c[kind].filter((x) => !ids.includes(x)) : [...new Set([...c[kind], ...ids])],
  }))
  const hpaIds = replicaChanges.map(hpaId)
  const reqIds = drivers.map(reqId)
  const hpaOnCount = replicaChanges.filter(hpaOn).length
  const reqOnCount = drivers.filter(reqOn).length

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2">
          <div className="min-w-0">
          <DialogTitle>
            <span className="font-mono">{group.name}</span> — {scenario.label}
          </DialogTitle>
          <DialogDescription>
            {sizeText(scenario)} · {usd0(scenario.monthlyUsd)}/mo ·{" "}
            {scenario.deltaUsd < 0 ? (
              <span className="font-semibold text-series-1">
                saves {usd0(Math.abs(scenario.deltaUsd))}/mo
              </span>
            ) : scenario.deltaUsd > 0 ? (
              <span className="text-status-serious">costs {usd0(scenario.deltaUsd)}/mo more</span>
            ) : (
              "no change against as-is"
            )}
            {" · bound by "}{scenario.binding}
            {picker && (
              <>
                {" · "}
                <button type="button" className="underline decoration-dotted underline-offset-2 hover:text-foreground"
                        aria-expanded={showTypes} onClick={() => setShowTypes((v) => !v)}>
                  {showTypes ? "hide instance types" : "compare instance types"}
                </button>
              </>
            )}
          </DialogDescription>
          </div>
          {/* In the header, not the body: the header stays put while the body
              scrolls, so the effect of a tick or a slider deep in a table is
              visible without scrolling back up. */}
          {hasLevers && (
            <SelectionHeader
              scenario={scenario}
              priced={priced}
              hpa={{ on: hpaOnCount, of: replicaChanges.length }}
              req={{ on: reqOnCount, of: drivers.length }}
              customised={customised}
              persisted={persisted}
              staleAgainst={customised && choices.collectedAt && choices.collectedAt !== collectedAt
                ? choices.collectedAt : undefined}
              onReset={reset}
            />
          )}
        </DialogHeader>

        <DialogBody className="grid gap-6">
          {hasLevers && <SelectionRisks priced={priced} />}
          {/* A different type the reader picked stays in view; the table
              itself opens only when asked for. */}
          {picker?.picked && (
            <div className={cn("rounded-md border px-3 py-2 text-[12.5px]",
              picker.picked.verdict === "unsafe" ? "border-status-critical/40 bg-status-critical/5"
                : picker.picked.tight ? "border-status-warning/40 bg-status-warning/5" : "border-border")}>
              <strong className="font-semibold">Your pick: {picker.selected}</strong> instead of{" "}
              <span className="font-mono">{picker.recommended}</span> —{" "}
              {scenario.deltaUsd <= picker.original.deltaUsd
                ? `saves ${usd0(picker.original.deltaUsd - scenario.deltaUsd)}/mo more`
                : `saves ${usd0(scenario.deltaUsd - picker.original.deltaUsd)}/mo less`}
              {" "}than this option, on {scenario.nodes} node{scenario.nodes === 1 ? "" : "s"}.
              {(picker.picked.verdict === "unsafe" || picker.picked.tight) && (
                <span className={picker.picked.verdict === "unsafe" ? "text-status-critical" : "text-status-warning"}>
                  {" "}{[...picker.picked.notes, ...(scenario.margin?.notes ?? [])].join("; ")}.
                </span>
              )}{" "}
              <Button variant="link" size="sm" className="h-auto p-0 text-[12.5px]"
                      onClick={() => picker.onPick(picker.recommended)}>
                back to {picker.recommended}
              </Button>
            </div>
          )}
          {picker && showTypes && (
            <section id="instance-types">
              <div className="flex items-baseline justify-between">
                <SectionTitle>Other instance types</SectionTitle>
                <Button variant="link" size="sm" className="h-auto p-0 text-[12px]"
                        onClick={() => setShowTypes(false)}>hide</Button>
              </div>
              <InstancePicker rows={picker.rows} selected={picker.selected}
                              recommended={picker.recommended} today={asis?.instanceType ?? ""}
                              onPick={picker.onPick} onCustom={picker.onCustom}
                              onRemove={picker.onRemove} />
            </section>
          )}
          {scenario.unverified && (
            <p className="rounded-md border border-status-warning/40 bg-status-warning/5 px-3 py-2 text-[12.5px]">
              <strong className="font-semibold text-status-warning">Unverified.</strong> Pod
              anti-affinity, topology spread and PodDisruptionBudgets live only in the Kubernetes API,
              and they were not collected for this cluster. A workload pinned one replica per node
              sets a floor this packing cannot see, so treat the node count as an upper bound on what
              is reachable — not as a recommendation. Re-run{" "}
              <span className="font-mono">collect</span> with <span className="font-mono">--context</span>{" "}
              to resolve it.
            </p>
          )}

          {!!scenario.usageDrivenPods && (
            <p className="rounded-md border px-3 py-2 text-[12.5px] text-muted-foreground">
              <strong className="font-semibold text-foreground">
                {scenario.usageDrivenPods} pod{scenario.usageDrivenPods === 1 ? "" : "s"} packed by
                observed usage, not by request.
              </strong>{" "}
              Those containers used more than they reserved — typically because no limit is set — so
              packing them at their request would fill a node that cannot hold them. The larger of the
              two figures was used.
            </p>
          )}

          {!scenario.feasible && (
            <p className="rounded-md border border-status-critical/40 bg-status-critical/5 px-3 py-2 text-[12.5px]">
              <strong className="font-semibold text-status-critical">Infeasible.</strong> {scenario.note}
            </p>
          )}

          <section>
            <SectionTitle>
              {scenario.measured ? "How the pods sit today" : "How the pods would land"}
            </SectionTitle>
            {placement.length > 0 ? (
              <>
                {live && (
                  <p className="mb-2 text-[12px] text-muted-foreground">
                    Your selection, as the header prices it — not the option as published.
                  </p>
                )}
                <PackViz
                  nodes={placement}
                  nodeCpu={scenario.nodeCpu ?? 0}
                  nodeMem={scenario.nodeMemGib ?? 0}
                  binding={placementBinding}
                  truncated={Math.max(0, placementNodes - placement.length)}
                  measured={scenario.measured && !live}
                />
              </>
            ) : (
              <p className="text-[12.5px] text-muted-foreground">
                No packing detail in this dataset. Regenerate it with{" "}
                <span className="font-mono">python3 export_ui.py --env {env}</span>.
              </p>
            )}
          </section>

          {group.trend && asis && scenario.key !== "asis" && (
            <section>
              <SectionTitle>Utilisation over the window — now and with {customised ? "your selection" : "this option"}</SectionTitle>
              <NodeGroupTrendPanel trend={group.trend} asis={asis} scenario={scenario} nodes={trendNodes} />
            </section>
          )}

          {/* Same type, fewer nodes: no configuration table, but the same
              traffic now goes through fewer network cards. */}
          {!changesNodes && scenario.network
            && (scenario.network.verdict === "unsafe" || scenario.network.verdict === "warn") && (
            <section>
              <SectionTitle>Network</SectionTitle>
              <div className="overflow-x-auto rounded-md border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Property</TableHead>
                      <TableHead className="text-right">Now</TableHead>
                      <TableHead className="text-right">With this option</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    <NetworkRows network={scenario.network} />
                  </TableBody>
                </Table>
              </div>
            </section>
          )}

          {changesNodes && asis && (
            <section>
              <SectionTitle>Node configuration</SectionTitle>
              <div className="overflow-x-auto rounded-md border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Property</TableHead>
                      <TableHead className="text-right">Current — {asis.instanceType}</TableHead>
                      <TableHead className="text-right">Proposed — {scenario.instanceType}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    <SpecRow label="vCPU" a={asis.vcpu} b={scenario.vcpu} />
                    <SpecRow label="Memory" a={asis.memGib} b={scenario.memGib} unit="Gi" />
                    <SpecRow
                      label="Allocatable CPU (after overhead)"
                      a={asis.nodeCpu} b={scenario.nodeCpu} unit="c" dp={2}
                    />
                    <SpecRow
                      label="Allocatable memory (after overhead)"
                      a={asis.nodeMemGib} b={scenario.nodeMemGib} unit="Gi" dp={1}
                    />
                    <SpecRow label="maxPods per node" a={asis.nodeMaxPods} b={scenario.nodeMaxPods} />
                    {scenario.storage && (
                      <TableRow>
                        <TableCell className="font-medium">Instance storage</TableCell>
                        <TableCell className="text-right font-mono text-muted-foreground tnum">
                          {scenario.storage.diskBefore}
                        </TableCell>
                        <TableCell className={cn("text-right font-mono tnum",
                          scenario.storage.diskAfter !== scenario.storage.diskBefore && "font-semibold",
                          scenario.storage.ssdBefore && !scenario.storage.ssdAfter && "text-status-critical")}>
                          {scenario.storage.diskAfter}
                        </TableCell>
                      </TableRow>
                    )}
                    {/* The disk chart shows how full; this says why it matters.
                        Only when it does -- a fitting disk needs no row. */}
                    {scenario.storage && (scenario.storage.verdict === "unsafe" || scenario.storage.verdict === "warn") && (
                      <TableRow className="hover:bg-transparent">
                        <TableCell colSpan={3} className="text-[12px]">
                          <Badge variant="outline" className={cn("mr-2", VERDICT[scenario.storage.verdict].cls)}>
                            {VERDICT[scenario.storage.verdict].label}
                          </Badge>
                          <span className={scenario.storage.verdict === "unsafe" ? "text-status-critical" : "text-muted-foreground"}>
                            {scenario.storage.notes.join("; ")}
                          </span>
                        </TableCell>
                      </TableRow>
                    )}
                    {scenario.network && <NetworkRows network={scenario.network} />}
                    <SpecRow label="On-demand $/hour" a={asis.hourlyUsd} b={scenario.hourlyUsd} dp={4} money />
                    <SpecRow label="Nodes" a={asis.nodes} b={scenario.nodes} />
                    <TableRow>
                      <TableCell className="font-medium">Monthly cost</TableCell>
                      <TableCell className="text-right font-mono tnum">{usd0(asis.monthlyUsd)}</TableCell>
                      <TableCell className="text-right font-mono font-semibold tnum">
                        {usd0(scenario.monthlyUsd)}
                      </TableCell>
                    </TableRow>
                  </TableBody>
                </Table>
              </div>
              <p className="mt-2 max-w-[74ch] text-[12px] text-muted-foreground">
                System and DaemonSet overhead is carried across as an absolute amount, not a
                proportion — a smaller instance does not get a smaller kubelet reservation. Node group
                instance types are immutable, so this means creating a replacement group and draining
                the old one.
              </p>
            </section>
          )}

          {replicaChanges.length > 0 && (
            <section>
              <SectionTitle>
                HPAs to change
                <span className="ml-2 font-normal text-muted-foreground">
                  {hpaOnCount} of {replicaChanges.length} selected, freeing{" "}
                  {replicaChanges.filter(hpaOn).reduce((t, r) => t + freedOf(r), 0).toFixed(1)} cores
                </span>
              </SectionTitle>
              <p className="mb-2 max-w-[78ch] text-[12px] text-muted-foreground">
                Each floor comes from replaying the HPA minute by minute over the window, with its own
                scale-up and scale-down policies and the minutes new pods take to become ready. The
                recommendation is the lowest floor that adds at most half an hour above request over the
                month and keeps bursts under 150% of request (or today&apos;s peak, if higher) — a lower floor
                saves pods but leaves fewer to absorb a burst while the HPA catches up. Never below 2.
                Rows marked <span className="font-mono">estimate</span> had no minute history and use the
                older 5-minute method.
              </p>
              {/* The two options genuinely differ here, and the reason is not
                  guessable from the numbers: utilisation is usage/request, so
                  cutting the request raises it and the HPA wants more replicas.
                  api-server goes to 31 with requests unchanged and only
                  to 72 once they are cut. */}
              {replicaChanges.some((r) => r.mixedWindow) && (
                <p className="mb-2 max-w-[78ch] rounded-md border border-status-warning/30
                              bg-status-warning/[0.06] px-2.5 py-1.5 text-[12px] text-muted-foreground">
                  <strong className="font-semibold text-status-warning">
                    Some of these windows span more than one configuration.
                  </strong>{" "}
                  {replicaChanges.filter((r) => r.mixedWindow)
                    .map((r) => `${r.workload} ${r.mixedWindow}`).join("; ")}
                  . Utilisation is measured against the replica count that was live at each
                  moment, so those figures are right — but the floor shown as "current" is
                  only the latest of several.
                </p>
              )}
              {changesRequests && (
                <p className="mb-2 max-w-[78ch] rounded-md border border-status-warning/30
                              bg-status-warning/[0.06] px-2.5 py-1.5 text-[12px] text-muted-foreground">
                  These floors are computed against the{" "}
                  <strong className="font-semibold">reduced</strong> requests this option also
                  applies, so they differ from the option that leaves requests alone. Utilisation is
                  usage ÷ request: cutting the request raises it, and the HPA answers by wanting
                  more replicas. Taking both levers fully is not available — that is the trade-off
                  between these two options, not an inconsistency.
                </p>
              )}
              <p className="mb-2 text-[12px] text-muted-foreground">
                Open a workload to see its replicas and CPU per replica over the window, now and at the
                new floor.
              </p>
              <div className="overflow-x-auto rounded-md border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="w-8">
                        <SelectAll label="all HPAs" on={hpaOnCount} of={replicaChanges.length}
                                   onChange={(on) => toggleAll("offHpa", hpaIds, on)} />
                      </TableHead>
                      <TableHead>Workload</TableHead>
                      <TableHead className="text-right">minReplicas</TableHead>
                      <TableHead className="whitespace-normal text-right">Running<br />now</TableHead>
                      <TableHead className="text-right">Utilisation</TableHead>
                      <TableHead className="whitespace-normal text-right">Replicas<br />needed</TableHead>
                      <TableHead className="whitespace-normal text-right"
                                 title="Minutes over the window the ready pods ran above their CPU request, from the per-minute HPA replay">
                        Burst<br />exposure
                      </TableHead>
                      <TableHead className="text-right">Frees</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {replicaChanges.map((r, i) => {
                      const id = `${r.ns}/${r.hpa}`
                      const isOpen = expanded.has(id)
                      const toggleOpen = () => setExpanded((prev) => {
                        const next = new Set(prev)
                        if (next.has(id)) next.delete(id)
                        else next.add(id)
                        return next
                      })
                      const on = hpaOn(r)
                      const chosen = floorOf(r)
                      const cid = hpaId(r)
                      return (
                      <Fragment key={i}>
                      <TableRow className={cn(isOpen && "bg-muted/40", !on && "opacity-50")}>
                        <TableCell>
                          <input type="checkbox" checked={on}
                                 aria-label={`include ${r.hpa}`}
                                 onChange={(e) => toggle("offHpa", cid, e.target.checked)}
                                 className="h-3.5 w-3.5 accent-[hsl(var(--series-1))]" />
                        </TableCell>
                        <TableCell>
                          <button type="button" onClick={toggleOpen} aria-expanded={isOpen}
                                  aria-controls={`proj-${i}`}
                                  title={isOpen ? "Hide the projection" : "Show replicas and utilisation, now and at the new floor"}
                                  className="group -mx-1 flex items-start gap-1 rounded px-1 text-left
                                             hover:bg-accent focus-visible:outline-none focus-visible:ring-2
                                             focus-visible:ring-ring">
                            <ChevronRight className={cn("mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform",
                              isOpen && "rotate-90")} />
                            <span>
                              <span className="block font-mono font-medium">{r.workload}</span>
                              <span className="block text-[11px] text-muted-foreground">
                                {r.ns} · {r.hpa}
                              </span>
                            </span>
                          </button>
                        </TableCell>
                        <TableCell className="whitespace-nowrap text-right font-mono tnum">
                          <span className="text-muted-foreground">{r.minReplicas} → </span>
                          <NumberField label={`minReplicas for ${r.hpa}`} value={chosen} min={MIN_FLOOR}
                                       changed={chosen !== r.suggestedMin}
                                       onCommit={(n) => setFloor(r, Math.round(n))} />
                          {chosen !== r.suggestedMin && (
                            <div className="text-[10.5px] text-muted-foreground">
                              recommended {r.suggestedMin}{" "}
                              <RowReset label={`Reset ${r.hpa} to the recommendation`}
                                        onClick={() => resetRow("hpa", cid)} />
                            </div>
                          )}
                        </TableCell>
                        <TableCell className="text-right font-mono text-muted-foreground tnum">
                          <div>{r.runningNow ?? r.observedReplicas}</div>
                          {/* Three counts, because they differ and the reader
                              needs to know which one the utilisation is
                              against. `avg` above the floor means the floor
                              moved during the window. */}
                          <div className="text-[10.5px] text-muted-foreground">
                            avg {r.avgReplicas ?? "—"} · peak {r.peakReplicas ?? r.observedReplicas}
                          </div>
                        </TableCell>
                        <TableCell className="text-right font-mono tnum">
                          {/* Against the target the HPA steers on. An average
                              far below it is the whole reason the floor, not
                              the HPA, is deciding the replica count. */}
                          {r.utilAvgPct !== undefined ? (
                            <>
                              <div>
                                avg {r.utilAvgPct}%
                                <span className="text-muted-foreground"> · max {r.utilMaxPct}%</span>
                              </div>
                              <div className="text-[10.5px] text-muted-foreground">
                                target {r.targetPct}%
                              </div>
                            </>
                          ) : (
                            "—"
                          )}
                        </TableCell>
                        <TableCell className="text-right font-mono tnum">
                          {/* What the HPA would have asked for at those two
                              moments, and the floor the replay settles on. */}
                          {r.replayFloor !== undefined ? (
                            <span title={r.basis}>
                              <div>
                                {r.neededAvg} at avg
                                <span className={r.longScaleUp
                                  ? "text-status-warning" : "text-muted-foreground"}>
                                  {" "}· {r.peakDesired} at peak
                                </span>
                              </div>
                              <div className="text-[10.5px] text-muted-foreground">
                                floor {r.replayFloor}
                              </div>
                            </span>
                          ) : (
                            "—"
                          )}
                        </TableCell>
                        <TableCell className="whitespace-nowrap text-right font-mono tnum">
                          {r.replayNow && r.replayAfter ? (
                            <span title={r.basis}>
                              <div>
                                {r.replayNow.over_request_minutes} → {r.replayAfter.over_request_minutes}
                                <span className="text-muted-foreground"> min</span>
                              </div>
                              <div className="text-[10.5px] text-muted-foreground">
                                peak {Math.round(r.replayNow.max_util * 100)}% → {Math.round(r.replayAfter.max_util * 100)}%
                              </div>
                            </span>
                          ) : (
                            <span className="text-[11px] text-muted-foreground" title="No minute history for this HPA: the floor is a 5-minute estimate">
                              estimate
                            </span>
                          )}
                        </TableCell>
                        <TableCell className="text-right font-mono text-series-1 tnum">
                          {on ? `${freedOf(r).toFixed(1)}c` : "—"}
                        </TableCell>
                      </TableRow>
                      {isOpen && (
                        <TableRow id={`proj-${i}`} className="hover:bg-transparent">
                          <TableCell colSpan={8} className="bg-muted/20 px-4 py-4">
                            <HpaProjection change={r} env={env} floor={chosen}
                                           onFloor={(n) => setFloor(r, n)} />
                          </TableCell>
                        </TableRow>
                      )}
                      </Fragment>
                      )
                    })}
                  </TableBody>
                </Table>
              </div>
              <p className="mt-2 max-w-[78ch] text-[12px] text-muted-foreground">
                A replica floor is often set for reasons CPU cannot see — zone spread, connection
                warmth, burst latency, or an incident someone responded to. Confirm with the owning
                team before applying, and consider stepping down rather than jumping to the figure.
              </p>
            </section>
          )}

          {changesRequests && (
            <section>
              <SectionTitle>
                Requests to change
                <span className="ml-2 font-normal text-muted-foreground">
                  {reqOnCount} of {drivers.length} container{drivers.length === 1 ? "" : "s"} selected
                </span>
              </SectionTitle>
              <div className="mb-2 flex flex-wrap gap-4 text-[12.5px] text-muted-foreground">
                <span>CPU <Delta value={cpuFreed} unit="c" /></span>
                <span>Memory <Delta value={memAdded} unit="Gi" /></span>
              </div>
              {memAdded > Math.abs(cpuFreed) * 2 && (
                <p className="mb-2 max-w-[74ch] text-[12px] text-muted-foreground">
                  These free CPU but need more memory, which is why this scenario is memory-bound.
                  The CPU headroom they create cannot be used.
                </p>
              )}
              <p className="mb-2 text-[12px] text-muted-foreground">
                Open a workload to try a request and see its CPU and memory utilisation over the window,
                today and at the new value.
              </p>
              <p className="mb-2 max-w-[78ch] text-[12px] text-muted-foreground">
                Each row shows what is reserved today, what is proposed, and the usage measured over the
                window. CPU is sized from p95 because it throttles under pressure; memory from the peak
                because it is OOMKilled. A row marked <strong className="font-semibold">peak &gt; 100% of
                request</strong> is already using more than it reserved — cutting it further is the least
                safe change on this list.
              </p>
              <div className="overflow-x-auto rounded-md border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="w-8">
                        <SelectAll label="all request changes" on={reqOnCount} of={drivers.length}
                                   onChange={(on) => toggleAll("offReq", reqIds, on)} />
                      </TableHead>
                      <TableHead>Workload</TableHead>
                      <TableHead className="text-right">Repl</TableHead>
                      <TableHead className="whitespace-normal text-right">CPU / replica<br />observed</TableHead>
                      <TableHead className="whitespace-normal text-right">Apply CPU<br />Δ fleet</TableHead>
                      <TableHead className="whitespace-normal text-right">Memory / replica<br />observed</TableHead>
                      <TableHead className="whitespace-normal text-right">Apply memory<br />Δ fleet</TableHead>
                      {onShowTrend && <TableHead className="w-8" />}
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {drivers.map((d, i) => {
                      const id = reqId(d)
                      const on = reqOn(d)
                      const cpu = cpuOf(d)
                      const mem = memOf(d)
                      const own = choices.requests[id]
                      const isOpen = expandedReq.has(id)
                      const toggleOpen = () => setExpandedReq((prev) => {
                        const next = new Set(prev)
                        if (next.has(id)) next.delete(id)
                        else next.add(id)
                        return next
                      })
                      return (
                      <Fragment key={i}>
                      <TableRow className={cn(isOpen && "bg-muted/40", !on && "opacity-50")}>
                        <TableCell>
                          <input type="checkbox" checked={on}
                                 aria-label={`include ${d.workload}/${d.container}`}
                                 onChange={(e) => toggle("offReq", id, e.target.checked)}
                                 className="h-3.5 w-3.5 accent-[hsl(var(--series-1))]" />
                        </TableCell>
                        <TableCell>
                          <button type="button" onClick={toggleOpen} aria-expanded={isOpen}
                                  aria-controls={`reqproj-${i}`}
                                  title={isOpen ? "Hide the projection" : "Try a request and see utilisation, now and at the new value"}
                                  className="group -mx-1 flex items-start gap-1 rounded px-1 text-left
                                             hover:bg-accent focus-visible:outline-none focus-visible:ring-2
                                             focus-visible:ring-ring">
                            <ChevronRight className={cn("mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform",
                              isOpen && "rotate-90")} />
                            <span>
                              <span className="block font-mono font-medium">{d.workload}</span>
                              <span className="block text-[11px] text-muted-foreground">{d.ns} · {d.container}</span>
                              {d.basis && (
                                <span className="mt-0.5 block font-mono text-[10px] text-muted-foreground">{d.basis}</span>
                              )}
                            </span>
                          </button>
                        </TableCell>
                        <TableCell className="text-right font-mono tnum">{d.replicas}</TableCell>
                        <TableCell>
                          <UsageCell kind="cpu" request={d.cpuReq} recommended={d.cpuRec}
                                     ladder={d.cpuLadder} limit={d.cpuLimit}
                                     avg={d.cpuAvg} p95={d.cpuP95} peak={d.cpuPeak}
                                     utilPct={d.cpuUtilPct} />
                        </TableCell>
                        <TableCell className="whitespace-nowrap text-right">
                          {cpu == null ? <span className="text-muted-foreground">—</span> : (
                            <NumberField label={`CPU request for ${d.container}, millicores`} unit="m"
                                         value={Math.round(cpu * 1000)} min={1}
                                         changed={own?.cpu !== undefined}
                                         onCommit={(n) => setRequest(d, "cpu", n / 1000)} />
                          )}
                          <div className="mt-0.5 text-[11.5px]">
                            {on ? <Delta value={cpuDeltaOf(d)} unit="c" /> : <span className="text-muted-foreground">—</span>}
                          </div>
                        </TableCell>
                        <TableCell>
                          <UsageCell kind="mem" request={d.memReq} recommended={d.memRec}
                                     ladder={d.memLadder} limit={d.memLimit}
                                     avg={d.memAvg} p95={d.memP95} peak={d.memPeak}
                                     utilPct={d.memUtilPct} />
                        </TableCell>
                        <TableCell className="whitespace-nowrap text-right">
                          {mem == null ? <span className="text-muted-foreground">—</span> : (
                            <NumberField label={`Memory request for ${d.container}, MiB`} unit="Mi"
                                         value={Math.round(mem * 1024)} min={1}
                                         changed={own?.mem !== undefined}
                                         onCommit={(n) => setRequest(d, "mem", n / 1024)} />
                          )}
                          <div className="mt-0.5 text-[11.5px]">
                            {on ? <Delta value={memDeltaOf(d)} unit="Gi" /> : <span className="text-muted-foreground">—</span>}
                          </div>
                          {own && (
                            <div className="text-[10.5px] text-muted-foreground">
                              <RowReset label={`Reset ${d.container} to the recommendation`}
                                        onClick={() => resetRow("req", id)} />
                            </div>
                          )}
                        </TableCell>
                        {onShowTrend && (
                          <TableCell className="text-right">
                            <button type="button"
                                    title={`Usage over the window for ${d.container}`}
                                    aria-label={`Usage trend for ${d.workload}/${d.container}`}
                                    onClick={() => onShowTrend(d)}
                                    className="inline-flex h-7 w-7 items-center justify-center rounded-md
                                               text-muted-foreground transition-colors hover:bg-muted
                                               hover:text-foreground focus-visible:outline-none
                                               focus-visible:ring-1 focus-visible:ring-ring">
                              <ChartSpline className="h-3.5 w-3.5" />
                            </button>
                          </TableCell>
                        )}
                      </TableRow>
                      {isOpen && (
                        <TableRow id={`reqproj-${i}`} className="hover:bg-transparent">
                          <TableCell colSpan={onShowTrend ? 10 : 9} className="bg-muted/20 px-4 py-4">
                            <RequestProjection driver={d} env={env} cpu={cpu} mem={mem}
                                               onCpu={(n) => setRequest(d, "cpu", n)}
                                               onMem={(n) => setRequest(d, "mem", n)} />
                          </TableCell>
                        </TableRow>
                      )}
                      </Fragment>
                      )
                    })}
                  </TableBody>
                </Table>
              </div>
            </section>
          )}

          {requires.length > 0 && (
            <section>
              <SectionTitle>Before this pays out</SectionTitle>
              <ul className="grid list-disc gap-1 pl-5 text-[12.5px] text-muted-foreground">
                {requires.map((r, i) => <li key={i}>{r}</li>)}
              </ul>
            </section>
          )}

          {/* Shown rather than filtered out: each of these is usually a
              one-line annotation or a small PDB, and fixing them is often
              cheaper than the node group replacement this option competes
              with. The reader should be able to weigh the two. */}
          {(scenario.drainBlockers?.length ?? 0) > 0 && (
            <section>
              <SectionTitle>
                Unblock {scenario.drainBlocked} node{scenario.drainBlocked === 1 ? "" : "s"} —
                fix {scenario.drainBlockers!.length} workload{scenario.drainBlockers!.length === 1 ? "" : "s"}
              </SectionTitle>
              <p className="mb-2 max-w-[80ch] text-[12.5px] text-muted-foreground">
                Cluster Autoscaler will not empty a node while any of these pods is on it, so
                today it stops at {scenario.blocked?.nodesIfNot ?? "more"} nodes. Only the nodes that
                go need unblocking — every pod pinning each of them is listed. The nodes that stay
                are filled, not emptied, and need nothing.
              </p>
              <div className="grid gap-1.5">
                {scenario.drainBlockers!.map((b) => (
                  <div key={b.subject}
                       className="rounded-md border border-status-warning/30 bg-status-warning/[0.06] px-2.5 py-1.5">
                    <div className="flex flex-wrap items-baseline gap-x-2">
                      <span className="font-mono text-[12px] font-medium">{b.subject}</span>
                      <span className="text-[11px] text-muted-foreground">
                        holds {b.nodes.length} node{b.nodes.length === 1 ? "" : "s"}
                        {b.replicas ? ` · ${b.replicas} replica${b.replicas === 1 ? "" : "s"}` : ""}
                      </span>
                    </div>
                    <div className="mt-0.5 text-[11.5px] text-muted-foreground">
                      {b.fix}
                      <span className="ml-1 font-mono text-[10.5px] opacity-80">
                        on {b.nodes.join(", ")}
                      </span>
                    </div>
                    {b.zoneWarning && (
                      <div className="mt-0.5 text-[11.5px] text-status-critical">⚠ {b.zoneWarning}</div>
                    )}
                    {b.pdb && (
                      <div className="mt-1.5">
                        <div className={cn(
                          "text-[11.5px]",
                          b.pdb.safe ? "text-muted-foreground" : "text-status-critical",
                        )}>
                          {/* The replica count is the deciding fact: with one
                              replica there is no budget that both permits the
                              eviction and keeps the workload serving. */}
                          {b.pdb.safe ? "" : "⚠ "}{b.pdb.note}
                        </div>
                        {b.pdb.yaml && (
                          <pre className="mt-1 overflow-x-auto rounded bg-muted/60 px-2 py-1.5
                                          font-mono text-[10.5px] leading-[1.45] text-muted-foreground">
{b.pdb.yaml}
                          </pre>
                        )}
                      </div>
                    )}
                  </div>
                ))}
              </div>
              {(scenario.drainKept?.length ?? 0) > 0 && (
                <div className="mt-3 text-[12px] text-muted-foreground">
                  <div className="font-medium text-foreground">Stays — no fix needed</div>
                  {scenario.drainKept!.map((k) => (
                    <div key={k.node} className="mt-0.5">
                      <span className="font-mono text-[11.5px]">{k.node}</span>
                      {" "}keeps {k.subjects.join(", ")}
                    </div>
                  ))}
                </div>
              )}
              {(scenario.drainByHand?.length ?? 0) > 0 && (
                <div className="mt-3 text-[12px] text-muted-foreground">
                  <div className="font-medium text-foreground">Or drain by hand, once, in a window</div>
                  <p className="max-w-[80ch]">
                    No workload changes: cordon and drain the nodes that go and Cluster Autoscaler
                    removes them once empty. kubectl honours PodDisruptionBudgets as CA does;
                    <span className="font-mono"> --delete-emptydir-data</span> discards the scratch
                    data those pods keep on the node. The blockers still stop CA doing this by
                    itself the next time the group scales out and back.
                  </p>
                  <pre className="mt-1 overflow-x-auto rounded bg-muted/60 px-2 py-1.5 font-mono text-[10.5px] leading-[1.45]">
{scenario.drainByHand!.join("\n")}
                  </pre>
                </div>
              )}
            </section>
          )}
        </DialogBody>
      </DialogContent>
    </Dialog>
  )
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-[0.05em] text-muted-foreground">{children}</h3>
}

function SpecRow({
  label, a = 0, b = 0, unit = "", dp = 0, money = false,
}: {
  label: string
  a: number | undefined
  b: number | undefined
  unit?: string
  dp?: number
  money?: boolean
}) {
  const fmt = (n: number) => (money ? `$${n.toFixed(dp)}` : `${n.toFixed(dp)}${unit}`)
  const changed = Math.abs(a - b) > 1e-9
  return (
    <TableRow>
      <TableCell className="font-medium">{label}</TableCell>
      <TableCell className="text-right font-mono text-muted-foreground tnum">{fmt(a)}</TableCell>
      <TableCell className={cn("text-right font-mono tnum", changed && "font-semibold")}>
        {fmt(b)}
        {changed && (
          <Badge variant="outline" className={cn("ml-2", b < a ? "border-series-1 text-series-1" : "border-status-serious text-status-serious")}>
            {b < a ? "−" : "+"}{Math.abs(b - a).toFixed(dp)}
          </Badge>
        )}
      </TableCell>
    </TableRow>
  )
}

/** Select-all for a column of checkboxes; mixed when only some are ticked. */
function SelectAll({ label, on, of, onChange }: {
  label: string; on: number; of: number; onChange: (on: boolean) => void
}) {
  const ref = useRef<HTMLInputElement>(null)
  useEffect(() => { if (ref.current) ref.current.indeterminate = on > 0 && on < of }, [on, of])
  return (
    <input ref={ref} type="checkbox" checked={of > 0 && on === of} aria-label={`include ${label}`}
           onChange={(e) => onChange(e.target.checked)}
           className="h-3.5 w-3.5 accent-[hsl(var(--series-1))]" />
  )
}

function RowReset({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button type="button" onClick={onClick} title={label} aria-label={label}
            className="inline-flex items-center gap-0.5 rounded px-0.5 underline underline-offset-2
                       hover:text-foreground focus-visible:outline-none focus-visible:ring-1
                       focus-visible:ring-ring">
      <RotateCcw className="h-2.5 w-2.5" /> reset
    </button>
  )
}

const saving = (delta: number) =>
  delta < -0.5 ? `saves ${usd0(-delta)}/mo` : delta > 0.5 ? `costs ${usd0(delta)}/mo more` : "no change"

/** The reader's selection, repriced live, at the right of the header.
 *
 *  Priced by the same packer, node type, minSize and baseline as the option,
 *  so with everything ticked it is the recommendation's own number. Nodes are
 *  whole: a small change often moves nothing until it crosses one. */
function SelectionHeader({
  scenario, priced, hpa, req, customised, persisted, staleAgainst, onReset,
}: {
  scenario: Scenario
  priced: Priced | null
  hpa: { on: number; of: number }
  req: { on: number; of: number }
  customised: boolean
  persisted: boolean
  staleAgainst?: string
  onReset: () => void
}) {
  const gap = priced ? priced.deltaUsd - scenario.deltaUsd : 0
  const unplaceable = priced?.outcome.after.unplaceable.length ?? 0
  const counts = [
    hpa.of > 0 && `${hpa.on}/${hpa.of} HPA${hpa.of === 1 ? "" : "s"}`,
    req.of > 0 && `${req.on}/${req.of} request${req.of === 1 ? "" : "s"}`,
  ].filter(Boolean).join(" · ")

  return (
    <section aria-label="Your selection" className="mr-8 min-w-[15rem] text-right">
      {priced ? (
        <div className="flex items-baseline justify-end gap-2">
          <span className="font-mono text-[15px] font-semibold tnum" data-testid="selection-nodes">
            {priced.nodes} nodes
          </span>
          <span className={cn("text-[12.5px]", priced.deltaUsd < -0.5 ? "font-medium text-series-1" : "text-muted-foreground")}
                data-testid="selection-saving">
            {saving(priced.deltaUsd)}
          </span>
        </div>
      ) : (
        <div className="text-[12px] text-muted-foreground"
             title={scenario.fleet?.length
               ? "This fleet is Karpenter's provisioning and consolidation replayed by the CLI; a partial selection is not re-priced in the browser"
               : "No pod placement was recorded for this group; re-run collect with --context"}>
          {scenario.fleet?.length ? "fleet chosen by Karpenter; selection not re-priced here" : "selection not repriceable"}
        </div>
      )}
      <div className="mt-0.5 flex flex-wrap items-center justify-end gap-x-2 text-[11px] text-muted-foreground">
        <span>{counts}</span>
        <span role="status">
          {!persisted ? "· not saved in this browser"
            : customised
              ? Math.abs(gap) > 0.5
                ? `· ${usd0(Math.abs(gap))}/mo ${gap > 0 ? "less" : "more"} than recommended`
                : "· your values, same result"
              : "· as recommended"}
        </span>
        {customised && (
          <button type="button" onClick={onReset} title="Reset to the recommendation"
                  className="inline-flex items-center gap-0.5 rounded px-0.5 underline underline-offset-2
                             hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring">
            <RotateCcw className="h-2.5 w-2.5" /> reset
          </button>
        )}
      </div>
      {priced?.heldAtToday && (
        <div className="mt-0.5 text-[11px] text-muted-foreground" title={priced.heldAtToday}>
          held at today&apos;s {priced.nodes} node{priced.nodes === 1 ? "" : "s"}
        </div>
      )}
      {unplaceable > 0 && (
        <div className="mt-0.5 text-[11px] font-medium text-status-critical">
          {unplaceable} pod{unplaceable === 1 ? "" : "s"} no longer fit on any node
        </div>
      )}
      {!!priced?.unpriced && (
        <div className="mt-0.5 text-[11px] text-muted-foreground">
          {priced.unpriced} change{priced.unpriced === 1 ? "" : "s"} with no pods here, not counted
        </div>
      )}
      {staleAgainst && (
        <div className="mt-0.5 text-[11px] text-status-warning">
          saved against the {new Date(staleAgainst).toLocaleDateString("en-GB", { day: "numeric", month: "short" })} collection — check, or reset
        </div>
      )}
    </section>
  )
}

/** What the selection puts at risk. Kept in the body: each needs its detail. */
function SelectionRisks({ priced }: { priced: Priced | null }) {
  const risks = (priced?.outcome.risks ?? []).filter((r) => r.severity !== "advisory")
  if (!risks.length) return null
  return (
    <div className="grid gap-1">
      {risks.slice(0, 4).map((r, i) => (
        <div key={i} className={cn("rounded-md border px-2.5 py-1.5 text-[12px]",
          r.severity === "unsafe" ? "border-status-critical/40 bg-status-critical/[0.06]"
            : "border-status-warning/40 bg-status-warning/[0.06]")}>
          <span className="font-mono text-[11.5px] font-medium">{r.subject}</span>{" "}
          <span className="text-muted-foreground">{r.detail}</span>
        </div>
      ))}
      {risks.length > 4 && (
        <div className="text-[11.5px] text-muted-foreground">and {risks.length - 4} more</div>
      )}
    </div>
  )
}
