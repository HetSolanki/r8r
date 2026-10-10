/** Build a pod set from a selection, pack it, and report what it risks.
 *
 *  Mirrors `_pods_from_placement` in the Python engine: a pod's footprint is
 *  `max(request, observed peak)`, because a container with no limit uses what
 *  it uses whatever its request says, and packing by the request alone fills a
 *  node that then cannot hold it.
 */

import { pack, type PackPod, type PackResult } from "@/lib/pack"
import { realize, type Deltas, type Keep, type Model, type NodeHeat, type Realized, type Shape } from "@/lib/realistic"
import { reqKey, type Requests } from "@/lib/live"

export interface SimContainer {
  name: string
  replicas: number
  cpuReq: number
  cpuRec: number | null
  memReq: number
  memRec: number | null
  actionable: boolean
  blockers: { code: string; severity: string; detail: string }[]
}

export interface SimWorkload {
  key: string
  ns: string
  name: string
  kind: string
  peakCpu: number
  peakMem: number
  /** Measured use per pod, the mean replica's p95. What a node actually holds;
   *  absent on an older dataset, when the request stands in for it. */
  busyCpu?: number | null
  busyMem?: number | null
  /** BestEffort | Burstable | Guaranteed, as configured today. */
  qos?: string | null
  antiaffinity: boolean
  containers: SimContainer[]
  hpa: {
    name: string
    min: number
    suggested: number
    replayFloor: number
    peakDesired: number
    max: number
    /** Replicas the load alone wants, {count: minutes}, at the pod request
     *  each was replayed at. See `runningReplicas`. */
    demand?: { request: number; minutes: Record<string, number> }[]
  } | null
  drainBlocked: string | null
}

export interface Sim {
  /** Nodes running today, so a selection is judged on what it would leave. */
  nodesNow?: number
  nodeCpu: number
  nodeMem: number
  maxPods: number
  headroom: number
  pricePerNode: number
  pods: { w: number; c: number; m: number }[]
  workloads: SimWorkload[]
  packFixtures: { scaleCpu: number; scaleMem: number; nodes: number; applyFloors?: boolean }[]
  /** The engine's realistic model (export_ui._model). Every node count below
   *  comes from it, so a selection lands where the engine lands. */
  model?: (Model & { minSize?: number }) | null
}

/** What the reader has ticked. Keyed `workloadKey::container`, and `workloadKey`
 *  for an HPA floor. A value of `undefined` means "take the recommendation". */
export interface Selection {
  containers: Record<string, { cpu?: number | null; mem?: number | null }>
  floors: Record<string, number>
}

export interface Risk {
  severity: "unsafe" | "risk" | "advisory"
  subject: string
  detail: string
}

export interface SimOutcome {
  before: PackResult
  after: PackResult
  nodesFreed: number
  monthlyDelta: number
  /** Pods whose footprint is still set by measured use, not the request. */
  absorbed: number
  risks: Risk[]
  /** The engine's view: where pods land once the controllers act, and the
   *  nodes whose measured load would not fit. */
  pressure?: { nodes: number; verdict: Realized["verdict"]; hot: NodeHeat[] }
}

const GIB = 1024 ** 3
const RANK = { unsafe: 0, risk: 1, advisory: 2 } as const
const fmtCpu = (c: number) => (c < 1 ? `${Math.round(c * 1000)}m` : `${c.toFixed(2)}`)
const fmtMem = (b: number) => (b >= GIB ? `${(b / GIB).toFixed(2)}Gi` : `${Math.round(b / 1024 ** 2)}Mi`)

function podsFor(sim: Sim, selection: Selection | null): PackPod[] {
  const { delta, keepCount } = selectionDeltas(sim, selection)
  const kept = new Map<number, number>()
  const out: PackPod[] = []
  for (const p of sim.pods) {
    const w = sim.workloads[p.w]
    const limit = keepCount.get(p.w)
    if (limit !== undefined) {
      const seen = kept.get(p.w) ?? 0
      if (seen >= limit) continue
      kept.set(p.w, seen + 1)
    }
    const d = delta.get(p.w) ?? { cpu: 0, mem: 0 }
    out.push({
      workload: w.key,
      // A pod's real footprint is the larger of what it reserves and what it
      // has been measured using.
      cpu: Math.max(Math.max(0, p.c + d.cpu), w.peakCpu),
      memory: Math.max(Math.max(0, p.m + d.mem), w.peakMem),
      antiaffinity: w.antiaffinity ? w.key : "",
    })
  }
  return out
}

/** Per workload: the summed request delta of its selected containers, and the
 *  replica floor chosen for it. One definition for the packer and the
 *  scheduler check, so the two can never apply different edits. */
function selectionDeltas(sim: Sim, selection: Selection | null) {
  const delta = new Map<number, { cpu: number; mem: number }>()
  const keepCount = new Map<number, number>()
  const touched = new Set<number>()
  sim.workloads.forEach((w, i) => {
    let cpu = 0
    let mem = 0
    if (selection) {
      for (const c of w.containers) {
        const chosen = selection.containers[`${w.key}::${c.name}`]
        if (!chosen) continue
        touched.add(i)
        const targetCpu = chosen.cpu === undefined ? c.cpuRec : chosen.cpu
        const targetMem = chosen.mem === undefined ? c.memRec : chosen.mem
        if (targetCpu !== null && targetCpu !== undefined) cpu += targetCpu - c.cpuReq
        if (targetMem !== null && targetMem !== undefined) mem += targetMem - c.memReq
      }
      const floor = selection.floors[w.key]
      if (floor !== undefined) {
        // Priced on what the HPA runs with this floor at the requests chosen,
        // never on the floor itself.
        const podRequest = w.containers.reduce((t, c) => {
          const chosen = selection.containers[`${w.key}::${c.name}`]
          const cpu = chosen ? (chosen.cpu === undefined ? c.cpuRec : chosen.cpu) : null
          return t + (cpu ?? c.cpuReq)
        }, 0)
        keepCount.set(i, runningReplicas(w.hpa?.demand, floor, podRequest))
        touched.add(i)
      }
    }
    delta.set(i, { cpu, mem })
  })
  return { delta, keepCount, touched }
}

/** `simulate.running_replicas`: the replicas an HPA with this floor runs on
 *  across 90% of the window's minutes, read off what the load alone asked
 *  for each minute. 5 replicas
 *  of 100m under 1 core of load run 15, whatever the floor says. The demand
 *  replayed at the request in force is used as is; any other request scales
 *  it, since utilisation is usage over request. */
export function runningReplicas(demand: { request: number; minutes: Record<string, number> }[] | undefined,
                                floor: number, podRequest: number): number {
  if (!demand?.length || podRequest <= 0) return floor
  const exact = demand.find((d) => Math.abs(d.request - podRequest) <= 1e-6 * Math.max(1, podRequest))
  const base = exact ?? demand.reduce((a, b) =>
    Math.abs(Math.log(b.request / podRequest)) < Math.abs(Math.log(a.request / podRequest)) ? b : a)
  const ratio = exact ? 1 : base.request / podRequest
  const runs = new Map<number, number>()
  let minutes = 0
  for (const [wanted, n] of Object.entries(base.minutes)) {
    let w = Number(wanted)
    if (ratio !== 1) w = Math.ceil(w * ratio - 1e-9)
    const r = Math.max(floor, w)
    runs.set(r, (runs.get(r) ?? 0) + n)
    minutes += n
  }
  let covered = 0
  for (const r of [...runs.keys()].sort((a, b) => a - b)) {
    covered += runs.get(r)!
    if (covered >= RUNNING_COVERAGE * minutes - 1e-9) return r
  }
  return floor
}

/** `simulate.RUNNING_COVERAGE`: minutes the priced replica count must cover. */
export const RUNNING_COVERAGE = 0.90

function workloadLabel(key: string): string {
  const [ns, ...rest] = key.split("/")
  return `${ns}/${rest.slice(0, -1).join("/")}`
}

/** Pods bigger than a whole node: the selection cannot run on this shape. */
function unplaceableRisk(pods: PackPod[], nodeMem: number): Risk {
  const by = new Map<string, { n: number; mem: number }>()
  for (const p of pods) {
    const x = by.get(p.workload) ?? { n: 0, mem: 0 }
    x.n += 1
    x.mem = Math.max(x.mem, p.memory)
    by.set(p.workload, x)
  }
  const list = [...by.entries()].map(([k, x]) => `${workloadLabel(k)} ×${x.n} (${fmtMem(x.mem)})`).join(", ")
  return {
    severity: "unsafe",
    subject: "Pods larger than a node",
    detail:
      `${pods.length} pod(s) would need more than a whole node holds (${fmtMem(nodeMem)} allocatable): ` +
      `${list}. They would stay Pending, so the node count above is not reachable on this instance ` +
      `type — lower that request, or choose a larger node.`,
  }
}

/** The engine's load verdict, with the pods that would suffer named -- and
 *  which of them the reader never selected, since those are the surprise. */
function loadRisks(r: Realized, touched: Set<string>): Risk[] {
  const victims = new Map<string, { bestEffort: boolean; selected: boolean }>()
  for (const n of r.load.hot) for (const e of n.exposed) {
    if (!victims.has(e.wkey)) victims.set(e.wkey, { bestEffort: e.bestEffort, selected: touched.has(e.wkey) })
  }
  const others = [...victims.entries()].filter(([, v]) => !v.selected)
  const names = others.slice(0, 4).map(([k, v]) => `${workloadLabel(k)}${v.bestEffort ? " (no request)" : ""}`)
    .join(", ") + (others.length > 4 ? ` and ${others.length - 4} more` : "")
  const who = others.length
    ? ` ${others.length} of the workloads using more than they reserve on those nodes are not in this selection: ${names}.`
    : victims.size ? " Every workload over its request there is one this selection changes." : ""
  if (r.verdict === "unsafe" || r.verdict === "warn") {
    return [{
      severity: r.verdict === "unsafe" ? "unsafe" : "risk",
      subject: "Node load after this change",
      detail: `once the controllers act, ${r.notes.join("; ")}.` +
        (r.verdict === "unsafe" ? " The kubelet evicts to recover memory, pods with no request first." : "") + who,
    }]
  }
  if (!r.margin.ok) {
    return [{
      severity: "risk",
      subject: "Headroom after this change",
      detail: `it fits, but ${r.margin.notes.join("; ")}. The recommendations keep that margin -- ` +
        "one busy day or a new replica is all that stands between these nodes and the OOM killer. " +
        "Keep a node more, or change less at once." + who,
    }]
  }
  if (r.load.hot.length) {
    return [{
      severity: "advisory",
      subject: "Node load",
      detail: `${r.load.hot.length} node(s) run this hot already today; this selection does not make it worse.${who}`,
    }]
  }
  return []
}

/** Every trap the Python `plan` command checks, evaluated on the selection. */
function risksFor(sim: Sim, selection: Selection): Risk[] {
  const out: Risk[] = []
  for (const w of sim.workloads) {
    for (const c of w.containers) {
      const chosen = selection.containers[`${w.key}::${c.name}`]
      if (!chosen) continue
      const subject = `${w.ns}/${w.name}/${c.name}`
      const targetCpu = chosen.cpu === undefined ? c.cpuRec : chosen.cpu
      const targetMem = chosen.mem === undefined ? c.memRec : chosen.mem

      // The workload's peak is summed across its containers, so it only bounds
      // a single-container workload precisely. Said as a share either way.
      if (targetMem != null && w.containers.length === 1 && w.peakMem > targetMem) {
        const over = w.peakMem - targetMem
        out.push({
          severity: w.peakMem > targetMem * 1.25 ? "unsafe" : "risk",
          subject,
          detail:
            `peaked at ${fmtMem(w.peakMem)} against a proposed request of ` +
            `${fmtMem(targetMem)} — ${fmtMem(over)} of its real use would be ` +
            `unreserved, and a Burstable pod over its request is evicted first ` +
            `under node pressure`,
        })
      }
      if (targetCpu != null && w.containers.length === 1 && w.peakCpu > targetCpu * 1.5) {
        out.push({
          severity: "advisory",
          subject,
          detail:
            `peaked at ${fmtCpu(w.peakCpu)} against ${fmtCpu(targetCpu)}; CPU is ` +
            `compressible so this throttles rather than fails, but latency will move`,
        })
      }
      for (const b of c.blockers) {
        if (b.severity !== "unsafe") continue
        out.push({ severity: "unsafe", subject, detail: b.detail })
      }
    }

    const floor = selection.floors[w.key]
    if (floor !== undefined && w.hpa) {
      if (floor < w.hpa.replayFloor) {
        out.push({
          severity: floor < w.hpa.replayFloor / 2 ? "unsafe" : "risk",
          subject: `${w.ns}/${w.name}`,
          detail:
            `the 30-day replay puts this HPA's floor at ${w.hpa.replayFloor}; ` +
            `${floor} leaves the HPA to cover more of the window than it was ` +
            `measured doing, and its busiest sample wanted ${w.hpa.peakDesired}`,
        })
      }
      // Utilisation is usage over request, so the request chosen decides how
      // many replicas the load keeps running -- whatever the floor says. Said
      // only when it actually happens, with the numbers, and priced on them.
      const podRequest = w.containers.reduce((t, c) => {
        const chosen = selection.containers[`${w.key}::${c.name}`]
        const cpu = chosen ? (chosen.cpu === undefined ? c.cpuRec : chosen.cpu) : null
        return t + (cpu ?? c.cpuReq)
      }, 0)
      const runs = runningReplicas(w.hpa.demand, floor, podRequest)
      if (runs > floor) {
        out.push({
          severity: runs >= w.hpa.min ? "unsafe" : "risk",
          subject: `${w.ns}/${w.name}`,
          detail:
            `at ${Math.round(podRequest * 1000)}m per pod the load keeps this HPA at ${runs} ` +
            `replicas for 90% of the window, so a floor of ${floor} does not hold; the option is ` +
            `priced on ${runs}` +
            (runs >= w.hpa.min ? `, which is no fewer than today's ${w.hpa.min} — the floor change saves nothing` : ""),
        })
      }
    }
  }
  return out.sort((a, b) => RANK[a.severity] - RANK[b.severity])
}

/** The node an option moves to. Absent means the group's current type. */
export interface NodeSpec {
  cpu: number
  /** Bytes. */
  mem: number
  maxPods: number
  pricePerNode: number
  /** The group's minSize: the autoscaler never goes below it, whatever packs. */
  minNodes?: number
  /** The engine's shape for this option: same type (CA replay) or a new one. */
  shape?: Shape
  /** Engine levers for workloads this selection cannot show, so "everything
   *  ticked" is exactly the published option. */
  fixed?: { deltas: Deltas; keep: Keep; requests?: Requests }
}

/** The selection as per-workload request deltas and replica floors, keyed the
 *  way the engine keys them. */
function levers(sim: Sim, selection: Selection) {
  const { delta, keepCount, touched } = selectionDeltas(sim, selection)
  const deltas: Deltas = {}
  const keep: Keep = {}
  const names = new Set<string>()
  sim.workloads.forEach((w, i) => {
    const d = delta.get(i)
    if (d && (d.cpu || d.mem)) deltas[w.key] = d
    const k = keepCount.get(i)
    if (k !== undefined) keep[w.key] = k
    if (touched.has(i)) names.add(w.key)
  })
  return { deltas, keep, touched: names }
}

/** Utilisation by request over a placement, as `scenarios.utilisation` reports it. */
function utilisation(placement: Map<string, { cpu: number; mem: number; owner: string }[]>,
                     nodes: number, cpu: number, mem: number, maxPods: number): PackResult {
  let c = 0
  let m = 0
  let count = 0
  const summary: PackResult["placement"] = []
  for (const pods of placement.values()) {
    let nc = 0
    let nm = 0
    let np = 0
    let dc = 0
    let dm = 0
    let dp = 0
    for (const p of pods) {
      if (p.owner === "DaemonSet") { dc += p.cpu; dm += p.mem; dp += 1; continue }
      nc += p.cpu
      nm += p.mem
      np += 1
    }
    c += nc
    m += nm
    count += np
    summary.push({ cpu: nc, mem: nm / GIB, pods: np, dsCpu: dc, dsMem: dm / GIB, dsPods: dp })
  }
  const n = Math.max(nodes, 1)
  const cu = cpu > 0 ? c / (cpu * n) : 0
  const mu = mem > 0 ? m / (mem * n) : 0
  const pu = maxPods > 0 ? count / (maxPods * n) : 0
  const worst = Math.max(cu, mu, pu)
  return { nodes, unplaceable: [], cpuUtil: cu, memUtil: mu, podUtil: pu,
           binding: worst === cu ? "cpu" : worst === mu ? "memory" : "pods", placement: summary }
}

export function simulate(sim: Sim, selection: Selection, target?: NodeSpec): SimOutcome {
  const price = target?.pricePerNode ?? sim.pricePerNode
  const afterPods = podsFor(sim, selection)
  const absorbed = afterPods.filter((p) => {
    const w = sim.workloads.find((x) => x.key === p.workload)
    return !!w && (p.cpu <= w.peakCpu + 1e-9 || p.memory <= w.peakMem + 1)
  }).length
  const model = sim.model
  const realized = model ? realizeSelection(sim, model, selection, target) : null
  if (!model || !realized) {
    // A dataset without the engine's model: the old ideal packer, labelled by
    // nothing but kept so an older export still renders.
    const opts = { cpuPerNode: sim.nodeCpu, memPerNode: sim.nodeMem, maxPods: sim.maxPods, headroom: sim.headroom }
    const before = pack(podsFor(sim, null), opts)
    const packed = pack(afterPods, target
      ? { cpuPerNode: target.cpu, memPerNode: target.mem, maxPods: target.maxPods, headroom: sim.headroom }
      : opts)
    const after = target?.minNodes && packed.nodes < target.minNodes ? { ...packed, nodes: target.minNodes } : packed
    return {
      before, after, nodesFreed: before.nodes - after.nodes,
      monthlyDelta: after.nodes * price - before.nodes * sim.pricePerNode, absorbed,
      risks: [...(packed.unplaceable.length ? [unplaceableRisk(packed.unplaceable, target?.mem ?? sim.nodeMem)] : []),
              ...risksFor(sim, selection)].sort((a, b) => RANK[a.severity] - RANK[b.severity]),
    }
  }

  const { r, touched } = realized
  const nodesNow = sim.nodesNow ?? model.start
  const cpu = target?.cpu ?? sim.nodeCpu
  const mem = target?.mem ?? sim.nodeMem
  const maxPods = target?.maxPods ?? sim.maxPods
  const today = new Map<string, { cpu: number; mem: number; owner: string }[]>()
  for (const p of model.placed) {
    if (p.n < 0) continue
    const n = model.nodes[p.n]
    if (!today.has(n)) today.set(n, [])
    today.get(n)!.push({ cpu: p.c, mem: p.m, owner: p.o })
  }
  const before = utilisation(today, nodesNow, sim.nodeCpu, sim.nodeMem, sim.maxPods)
  const placement = new Map([...r.outcome.placement].map(([n, pods]) =>
    [n, pods.map((p) => ({ cpu: p.cpu, mem: p.mem, owner: p.owner }))]))
  const after = { ...utilisation(placement, r.outcome.nodes, cpu, mem, maxPods),
                  unplaceable: r.outcome.unplaceable }
  return {
    before,
    after,
    nodesFreed: before.nodes - after.nodes,
    monthlyDelta: after.nodes * price - before.nodes * sim.pricePerNode,
    absorbed,
    risks: [
      ...(r.outcome.unplaceable.length ? [unplaceableRisk(r.outcome.unplaceable, mem)] : []),
      ...loadRisks(r, touched),
      ...risksFor(sim, selection),
    ].sort((a, b) => RANK[a.severity] - RANK[b.severity]),
    pressure: { nodes: after.nodes, verdict: r.verdict, hot: r.load.hot },
  }
}

/** The engine's model on a selection: its levers, plus any the caller fixes. */
function realizeSelection(sim: Sim, model: Model & { minSize?: number }, selection: Selection,
                          target?: NodeSpec): { r: Realized; touched: Set<string> } | null {
  const { deltas, keep, touched } = levers(sim, selection)
  for (const [w, d] of Object.entries(target?.fixed?.deltas ?? {})) if (!(w in deltas)) deltas[w] = d
  for (const [w, n] of Object.entries(target?.fixed?.keep ?? {})) if (!(w in keep)) keep[w] = n
  // Per container, for the exact replay on live pods: what each ticked
  // container is set to, plus the engine's own for workloads not listed.
  let requests: Requests | undefined
  if (model.live) {
    requests = { ...(target?.fixed?.requests ?? {}) }
    for (const w of sim.workloads) {
      for (const c of w.containers) {
        const chosen = selection.containers[`${w.key}::${c.name}`]
        if (!chosen) continue
        const cpu = chosen.cpu === undefined ? c.cpuRec : chosen.cpu
        const mem = chosen.mem === undefined ? c.memRec : chosen.mem
        if (cpu == null && mem == null) continue
        requests[reqKey(w.ns, w.name, c.name)] = [cpu == null ? null : Math.round(cpu * 1000),
                                                  mem == null ? null : Math.round(mem)]
      }
    }
  }
  // Today's shape unless the caller names another. Allocatable stands in for
  // the instance's size, which leaves no overhead: full allocatable exactly.
  const shape: Shape = target?.shape ?? {
    sameType: true, minNodes: target?.minNodes ?? model.minSize ?? 0, maxPods: sim.maxPods,
    nodesNow: sim.nodesNow ?? model.start,
    vcpu: model.allocCpu, memGib: model.allocMem / GIB,
    currentVcpu: model.allocCpu, currentMemGib: model.allocMem / GIB,
  }
  const r = realize(model, deltas, keep, shape, requests)
  return r ? { r, touched } : null
}

/** Replay the Python engine's own cases. A mismatch means the two packers have
 *  diverged, and every number this view shows is no longer this tool's. */
export function checkFixtures(sim: Sim): string[] {
  const out: string[] = []
  for (const f of sim.packFixtures) {
    const kept = new Map<string, number>()
    const pods: PackPod[] = []
    for (const p of sim.pods) {
      const w = sim.workloads[p.w]
      // The replica case: keep only as many pods of a workload as its floor.
      if (f.applyFloors && w.hpa) {
        const seen = kept.get(w.key) ?? 0
        if (seen >= w.hpa.suggested) continue
        kept.set(w.key, seen + 1)
      }
      pods.push({
        workload: w.key,
        cpu: Math.max(p.c * f.scaleCpu, w.peakCpu),
        memory: Math.max(p.m * f.scaleMem, w.peakMem),
        antiaffinity: w.antiaffinity ? w.key : "",
      })
    }
    const got = pack(pods, {
      cpuPerNode: sim.nodeCpu, memPerNode: sim.nodeMem,
      maxPods: sim.maxPods, headroom: sim.headroom,
    }).nodes
    if (got !== f.nodes) {
      out.push(
        `cpu x${f.scaleCpu} mem x${f.scaleMem}` +
        (f.applyFloors ? " +HPA floors" : "") +
        `: engine packed ${f.nodes} nodes, console packed ${got}`,
      )
    }
  }
  return out
}

export { attachLive } from "@/lib/live"
