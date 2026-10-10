/** What the reader has changed from an option's recommendation, kept per option.
 *
 *  Only deviations are stored -- unticked rows and overridden values -- so the
 *  default is "every change ticked, at the recommended value", and Reset is
 *  simply an empty record. Values are this reader's working notes, so they live
 *  in this browser; nothing here reaches the analysis.
 */
import { useCallback, useEffect, useState } from "react"
import { simulate, type Selection, type SimOutcome } from "@/lib/simulate"
import type { Driver, NodeGroup, ReplicaChange, Scenario } from "@/types"

const GIB = 1024 ** 3

export interface OptionChoices {
  /** HPA ids (`ns/workload`) left out. */
  offHpa: string[]
  /** Request ids (`ns/workload/container`) left out. */
  offReq: string[]
  /** Chosen minReplicas, by HPA id. Absent means the recommendation. */
  floors: Record<string, number>
  /** Chosen requests, by request id: CPU in cores, memory in GiB. */
  requests: Record<string, { cpu?: number; mem?: number }>
  /** When these choices were last changed, and against which collection. */
  savedAt?: string
  collectedAt?: string
}

export const EMPTY: OptionChoices = { offHpa: [], offReq: [], floors: {}, requests: {} }

export const hpaId = (r: ReplicaChange) => `${r.ns}/${r.workload}`
export const reqId = (d: Driver) => `${d.ns}/${d.workload}/${d.container}`

export function isCustomised(c: OptionChoices) {
  return c.offHpa.length > 0 || c.offReq.length > 0
    || Object.keys(c.floors).length > 0 || Object.keys(c.requests).length > 0
}

function read(key: string): OptionChoices | null {
  try {
    const raw = window.localStorage.getItem(key)
    if (!raw) return null
    const doc = JSON.parse(raw)
    return {
      offHpa: Array.isArray(doc.offHpa) ? doc.offHpa : [],
      offReq: Array.isArray(doc.offReq) ? doc.offReq : [],
      floors: doc.floors && typeof doc.floors === "object" ? doc.floors : {},
      requests: doc.requests && typeof doc.requests === "object" ? doc.requests : {},
      savedAt: doc.savedAt,
      collectedAt: doc.collectedAt,
    }
  } catch {
    return null
  }
}

/** The choices for one option, saved as they change. Storage can be missing or
 *  refuse writes (private windows, blocked site data); the page still works,
 *  the choices just do not survive a reload. */
export function useChoices(storageKey: string, collectedAt: string) {
  const [choices, setChoices] = useState<OptionChoices>(() => read(storageKey) ?? EMPTY)
  const [persisted, setPersisted] = useState(true)

  useEffect(() => { setChoices(read(storageKey) ?? EMPTY) }, [storageKey])

  const update = useCallback((fn: (c: OptionChoices) => OptionChoices) => {
    setChoices((prev) => {
      const next = { ...fn(prev), savedAt: new Date().toISOString(), collectedAt }
      try {
        if (isCustomised(next)) window.localStorage.setItem(storageKey, JSON.stringify(next))
        else window.localStorage.removeItem(storageKey)
        setPersisted(true)
      } catch {
        setPersisted(false)
      }
      return next
    })
  }, [storageKey, collectedAt])

  const reset = useCallback(() => {
    try { window.localStorage.removeItem(storageKey) } catch { /* nothing to clear */ }
    setChoices(EMPTY)
  }, [storageKey])

  return { choices, update, reset, persisted }
}

export interface Priced {
  outcome: SimOutcome
  /** Node count and monthly cost of the selection on this option's node type. */
  nodes: number
  monthlyUsd: number
  /** Signed, against what runs today -- the same baseline the option's own
   *  figure uses, so ticking everything reproduces it exactly. */
  deltaUsd: number
  /** Changes that could not be priced because the packer has no pods for them. */
  unpriced: number
  /** Set when the packed count was held at what runs today, and why. */
  heldAtToday?: string
}

/** Price the selection the way the engine priced the option.
 *
 *  Same model (`rightsizer/engine/realistic.py`, ported in `lib/realistic.ts`): for
 *  today's instance type the edits land on today's placement and Cluster
 *  Autoscaler's scale-down is replayed; for another type the pods are placed
 *  by request onto fresh nodes. Same levers too -- the reader's ticks and
 *  values for the rows shown, the engine's own for any workload the dialog
 *  does not list -- so with everything ticked this is the published option,
 *  to the node and the dollar. smoke.mjs checks that for every option. */
export function priceSelection(
  group: NodeGroup, scenario: Scenario, asis: Scenario | undefined,
  drivers: Driver[], replicaChanges: ReplicaChange[], choices: OptionChoices,
): Priced | null {
  const sim = group.sim
  if (!sim || !asis || !scenario.nodes || !scenario.nodeCpu || !scenario.nodeMemGib || !scenario.nodeMaxPods) {
    return null
  }
  const requires = scenario.requires ?? []
  const changesRequests = requires.some((r) => r.includes("container request changes"))
  const changesReplicas = requires.some((r) => r.includes("minReplicas"))
  // "Raise memory requests" moves only memory, and only upwards.
  const memoryOnly = changesRequests && scenario.key.startsWith("memory")
  const mode = [changesRequests ? (memoryOnly ? "memory" : "all") : "", changesReplicas ? "replicas" : ""]
    .filter(Boolean).join("+")

  const find = (ns: string, name: string) => sim.workloads.find((w) => w.ns === ns && w.name === name)
  const selection: Selection = { containers: {}, floors: {} }
  const shownRequests = new Set<string>()
  const shownFloors = new Set<string>()
  let unpriced = 0

  for (const d of drivers) {
    const w = find(d.ns, d.workload)
    const c = w?.containers.find((x) => x.name === d.container)
    if (!w || !c) { unpriced++; continue }
    shownRequests.add(w.key)
    if (choices.offReq.includes(reqId(d))) continue
    const own = choices.requests[reqId(d)] ?? {}
    const cpu = own.cpu !== undefined ? own.cpu : c.cpuRec
    const mem = own.mem !== undefined ? own.mem * GIB : c.memRec
    selection.containers[`${w.key}::${d.container}`] = memoryOnly
      ? { cpu: null, mem: mem != null && mem > c.memReq ? mem : null }
      : { cpu, mem }
  }
  for (const r of replicaChanges) {
    const w = find(r.ns, r.workload)
    if (!w) { unpriced++; continue }
    shownFloors.add(w.key)
    if (choices.offHpa.includes(hpaId(r))) continue
    selection.floors[w.key] = choices.floors[hpaId(r)] ?? r.suggestedMin
  }

  // Nothing ticked is "change nothing", which is today as measured -- the same
  // as-is the option's own saving is measured against. Replaying Cluster
  // Autoscaler on an unchanged group can remove a node on its own; the engine
  // never credits that to an option, so the dialog must not either.
  const ticked = Object.keys(selection.containers).length + Object.keys(selection.floors).length
  if (ticked === 0) {
    const outcome = simulate(sim, selection)
    const nodes = asis.nodes
    return { outcome: { ...outcome, after: { ...outcome.before, nodes } }, nodes,
             monthlyUsd: asis.monthlyUsd, deltaUsd: 0, unpriced,
             heldAtToday: "nothing selected: this is today's configuration" }
  }

  // The engine's levers for workloads this dialog does not list.
  const engine = mode ? sim.model?.levers?.[mode] : undefined
  // Containers of listed workloads come from the selection; the rest are the
  // engine's. A request key is "ns\0workload\0container".
  const listed = new Set(sim.workloads.filter((w) => shownRequests.has(w.key)).map((w) => `${w.ns}\u0000${w.name}`))
  const fixed = {
    deltas: Object.fromEntries(Object.entries(engine?.deltas ?? {}).filter(([w]) => !shownRequests.has(w))),
    keep: Object.fromEntries(Object.entries(engine?.keep ?? {}).filter(([w]) => !shownFloors.has(w))),
    requests: Object.fromEntries(Object.entries(engine?.requests ?? {}).filter(([k]) =>
      !listed.has(k.split("\u0000").slice(0, 2).join("\u0000")))),
  }

  const pricePerNode = scenario.monthlyUsd / scenario.nodes
  const sameType = scenario.instanceType === asis.instanceType
  const outcome = simulate(sim, selection, {
    cpu: scenario.nodeCpu,
    mem: scenario.nodeMemGib * GIB,
    maxPods: scenario.nodeMaxPods,
    pricePerNode,
    minNodes: scenario.minNodes,
    fixed,
    shape: scenario.vcpu && scenario.memGib && asis.vcpu && asis.memGib ? {
      sameType, minNodes: scenario.minNodes ?? 0, maxPods: scenario.nodeMaxPods,
      nodesNow: sim.nodesNow ?? asis.nodes,
      vcpu: scenario.vcpu, memGib: scenario.memGib,
      currentVcpu: asis.vcpu, currentMemGib: asis.memGib,
      lifted: scenario.blocked?.lift?.length ? scenario.blocked.lift : (scenario.drainBlocked ?? 0) > 0,
    } : undefined,
  })
  const nodes = outcome.after.nodes
  const monthlyUsd = nodes * pricePerNode
  return { outcome, nodes, monthlyUsd, deltaUsd: monthlyUsd - asis.monthlyUsd, unpriced }
}

export { attachLive } from "@/lib/live"
