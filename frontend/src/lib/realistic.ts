/** What production would do with a selection: the engine's model, ported.
 *
 *  A line-for-line port of `rightsizer/engine/realistic.py`, the Cluster Autoscaler
 *  replay in `autoscaler.simulate_ca_scale_down` and `eviction.simulate_group`.
 *  The dialog prices a reader's own selection here, so it must land where the
 *  engine lands for the published option -- and it is checked against every one
 *  of them (smoke.mjs, "everything ticked is the option"). Two models of one
 *  number were how the dialog came to say 22 nodes beside an option of 15.
 *
 *  Order matters for an exact port and is kept deliberately: node names sort as
 *  strings, dictionaries keep insertion order, sorts are stable, and ties break
 *  on pod name exactly as the Python does.
 */

import { pack, type PackPod } from "@/lib/pack"
import { liveNewType, liveSameType, type LiveModel, type LiveOutcome, type Requests } from "@/lib/live"

// -- inputs (export_ui._model) ----------------------------------------------

export interface Trend {
  t: number[]
  nodes: (number | null)[]
  cpu: (number | null)[]
  cpuCap: (number | null)[]
  mem: (number | null)[]
  memCap: (number | null)[]
}

export interface Model {
  nodes: string[]
  keys: string[]
  /** `a`: the pod's creation time (unix seconds), when collected. */
  placed: { p: string; ns: string; n: number; k: number; c: number; m: number; o: string; a?: number | null }[]
  allocCpu: number
  allocMem: number
  dsCpu: number
  dsMem: number
  /** kubelet max-pods on today's nodes, DaemonSets counted; 0 = unknown. */
  maxPods?: number
  /** DaemonSet pods per node, which a new node runs too. */
  dsPods?: number
  start: number
  /** When the placement was observed (unix seconds). */
  now?: number | null
  ca: {
    source: string; enabled: boolean; threshold: number; ignoresDaemonsets: boolean
    skipsSystemPods: boolean; skipsLocalStorage: boolean; bulk: number
    ignoresMirror?: boolean; customControllers?: boolean; systemTimeout?: number
    parallelism?: number; drainParallelism?: number
  }
  rules: {
    drainBlocked: Record<string, string>
    pdb: Record<string, { ns: string; name: string; minAvailable: string | null; maxUnavailable: string | null }>
    replicas: Record<string, number>
    nodeSelector: Record<string, Record<string, string>>
    nodeAffinity: Record<string, Record<string, string[]>[]>
    tolerations: Record<string, { key: string; operator: string; value: string; effect: string }[]>
    antiaffinity: string[]
    nodeLabels: Record<string, Record<string, string>>
    nodeTaints: Record<string, { key: string; value: string; effect: string }[]>
    unschedulable: string[]
    /** `namespace/pod` -> the zone its EBS volume lives in. */
    zoneBound?: Record<string, string>
    safeToEvict?: string[]
    notSafe?: string[]
    localStorage?: string[]
    hardSpread?: Record<string, { key: string; maxSkew: number }[]>
    scaleDownDisabled?: string[]
    /** status.disruptionsAllowed per budget, "ns/name". */
    pdbAllowed?: Record<string, number | null>
  }
  loads: Record<string, [number, number]>
  dsBusy: [number, number]
  today: { hotCpu: number; hotMem: number }
  trend?: Trend | null
  levers: Record<string, { deltas: Deltas; keep: Keep; requests?: Requests }>
  /** The node group this model is, and its cluster's live objects (attached at
   *  load): with both, options replay exactly (kube-scheduler + CA v1.35). */
  group?: string
  live?: LiveModel | null
}

export type Deltas = Record<string, { cpu: number; mem: number }>
export type Keep = Record<string, number>

interface Pod {
  ns: string
  name: string
  node: string
  cpu: number
  mem: number
  owner: string
  wkey: string
  created?: number | null
}

const isDs = (p: Pod) => p.owner === "DaemonSet"
const isMirror = (p: Pod) => p.owner === "Node"
const isBare = (p: Pod) => !p.owner || p.owner === "Unknown"
const subjectOf = (p: Pod) => (isBare(p) || !p.wkey ? `${p.ns}/${p.name}` : p.wkey)
const strCmp = (a: string, b: string) => (a < b ? -1 : a > b ? 1 : 0)
const sortedNodes = (pods: Pod[]) => [...new Set(pods.map((p) => p.node).filter(Boolean))].sort(strCmp)

// Thresholds of rightsizer/engine/pressure.py.
const MEM_CRITICAL = 1.0
const MEM_WARNING = 0.9
const CPU_WARNING = 1.0

// -- building the pod set ------------------------------------------------------

/** Today's placement with the edits applied. `dsDeltas` mirrors the two engine
 *  paths: `simulate_group` resizes every row, `placed_pods` skips DaemonSets. */
function podsFrom(model: Model, deltas: Deltas, dsDeltas: boolean): Pod[] {
  return model.placed.map((r) => {
    const wkey = model.keys[r.k]
    const d = (dsDeltas || r.o !== "DaemonSet") ? deltas[wkey] : undefined
    return {
      ns: r.ns, name: r.p, node: r.n >= 0 ? model.nodes[r.n] : "",
      cpu: Math.max(0, r.c + (d?.cpu ?? 0)), mem: Math.max(0, r.m + (d?.mem ?? 0)),
      owner: r.o, wkey, created: r.a ?? null,
    }
  })
}

/** `realistic.apply_keep`: drop surplus replicas from the nodes holding the
 *  most of a workload's replicas first, ties to the pod name sorting last. */
export function applyKeep(pods: Pod[], keep: Keep): Pod[] {
  if (!Object.keys(keep).length) return pods
  const drop = new Set<number>()
  const byWl = new Map<string, number[]>()
  pods.forEach((p, i) => {
    if (p.wkey in keep && !isDs(p)) {
      if (!byWl.has(p.wkey)) byWl.set(p.wkey, [])
      byWl.get(p.wkey)!.push(i)
    }
  })
  for (const [wkey, mine] of byWl) {
    const surplus = Math.max(0, mine.length - Math.max(0, keep[wkey]))
    for (let t = 0; t < surplus; t++) {
      const count = new Map<string, number>()
      for (const i of mine) if (!drop.has(i)) count.set(pods[i].node, (count.get(pods[i].node) ?? 0) + 1)
      let victim = -1
      for (const i of mine) {
        if (drop.has(i)) continue
        if (victim < 0) { victim = i; continue }
        const a = count.get(pods[i].node) ?? 0
        const b = count.get(pods[victim].node) ?? 0
        if (a > b || (a === b && pods[i].name > pods[victim].name)) victim = i
      }
      drop.add(victim)
    }
  }
  return pods.filter((_, i) => !drop.has(i))
}

/** `eviction._reschedule_overflow`: pods a request increase overfilled are
 *  placed by kube-scheduler's score, else on a new node; a pod no empty node
 *  holds is unplaceable. Mutates `node` (unplaceable: ""), may push DaemonSet
 *  copies for new nodes onto `pods`; returns [nodes added, unplaceable]. */
function rescheduleOverflow(pods: Pod[], nodes: string[], model: Model): [number, Pod[]] {
  const allocCpu = Math.max(0, model.allocCpu)
  const allocMem = Math.max(0, model.allocMem)
  if (allocCpu <= 0 || allocMem <= 0) return [0, []]
  const on = new Map<string, Pod[]>(nodes.map((n) => [n, []]))
  for (const p of pods) on.get(p.node)?.push(p)
  // A new node starts with the DaemonSet pods the busiest node carries.
  let template: Pod[] = []
  for (const n of on.keys()) {
    const ds = on.get(n)!.filter(isDs).map((p, i) => ({ p, i }))
      .sort((x, y) => strCmp(x.p.name, y.p.name) || x.i - y.i).map((x) => x.p)
    if (ds.length > template.length) template = ds
  }
  let dsCpu = 0
  let dsMem = 0
  for (const d of template) { dsCpu += d.cpu; dsMem += d.mem }
  const cap = model.maxPods || 1e9
  const sums = (n: string): [number, number] => {
    let c = 0
    let m = 0
    for (const p of on.get(n)!) { c += p.cpu; m += p.mem }
    return [c, m]
  }
  const over = (n: string) => { const [c, m] = sums(n); return c > allocCpu + 1e-9 || m > allocMem + 1e-9 }
  const pending: Pod[] = []
  for (const n of nodes) {
    const here = on.get(n)!.filter((p) => !isDs(p))
      .map((p, i) => ({ p, i, k: p.cpu / allocCpu + p.mem / allocMem }))
      .sort((x, y) => x.k - y.k || x.i - y.i)
      .map((x) => x.p)
    while (here.length && over(n)) {
      const p = here.pop()!
      const list = on.get(n)!
      list.splice(list.indexOf(p), 1)
      pending.push(p)
    }
  }
  const anti = new Set(model.rules.antiaffinity)
  const feasible = (p: Pod, n: string) => {
    const list = on.get(n)!
    if (list.length + 1 > cap) return false
    const [c, m] = sums(n)
    if (p.cpu > allocCpu - c + 1e-9) return false
    if (p.mem > allocMem - m + 1e-9) return false
    if (n.startsWith("(new")) return true
    const present = new Set(list.filter((q) => anti.has(q.wkey)).map((q) => q.wkey))
    return canHost(model.rules, anti, p, n, present)
  }
  let added = 0
  const unplaceable: Pod[] = []
  const ordered = pending.map((p, i) => ({ p, i }))
    .sort((x, y) => y.p.cpu - x.p.cpu || y.p.mem - x.p.mem || x.i - y.i).map((x) => x.p)
  for (const p of ordered) {
    if (p.cpu > allocCpu - dsCpu + 1e-9 || p.mem > allocMem - dsMem + 1e-9) {
      p.node = ""
      unplaceable.push(p)
      continue
    }
    let best: string | null = null
    let bestScore = -1
    for (const n of on.keys()) {
      if (!feasible(p, n)) continue
      const score = schedulerScore(on.get(n)!, p, allocCpu, allocMem)
      if (score > bestScore) { best = n; bestScore = score }
    }
    if (best === null) {
      added += 1
      best = `(new node ${added})`
      const copies = template.map((d) => ({ ...d, node: best! }))
      on.set(best, copies)
      pods.push(...copies)
    }
    on.get(best)!.push(p)
    p.node = best
  }
  return [added, unplaceable]
}

// -- kube-scheduler's resource score (autoscaler.scheduler_score) -------------

const DEFAULT_MILLI_CPU = 100
const DEFAULT_MEMORY = 200 * 1024 * 1024
const MAX_NODE_SCORE = 100
const HOSTNAME_KEYS = ["kubernetes.io/hostname", "topology.kubernetes.io/hostname"]

function nonzero(p: Pod): [number, number] {
  const cpu = p.cpu * 1000
  return [cpu > 0 ? cpu : DEFAULT_MILLI_CPU, p.mem > 0 ? p.mem : DEFAULT_MEMORY]
}

/** LeastAllocated (non-zero requests) + BalancedAllocation (actual), 0..200. */
function schedulerScore(podsOnNode: Pod[], pod: Pod, allocCpu: number, allocMem: number): number {
  const allocM = allocCpu * 1000
  let [reqC, reqM] = nonzero(pod)
  let actC = pod.cpu * 1000
  let actM = pod.mem
  for (const p of podsOnNode) {
    const [c, m] = nonzero(p)
    reqC += c
    reqM += m
    actC += p.cpu * 1000
    actM += p.mem
  }
  let least = 0
  for (const [req, cap] of [[reqC, allocM], [reqM, allocMem]] as const) {
    if (cap > 0 && req <= cap) least += Math.floor((cap - req) * MAX_NODE_SCORE / cap)
  }
  least = Math.floor(least / 2)
  const fc = allocM > 0 ? Math.min(1, actC / allocM) : 1
  const fm = allocMem > 0 ? Math.min(1, actM / allocMem) : 1
  const balanced = Math.floor((1 - Math.abs(fc - fm) / 2) * MAX_NODE_SCORE)
  return least + balanced
}

// -- eviction rules (eviction.EvictionRules) ----------------------------------

function tolerates(tols: Model["rules"]["tolerations"][string], taints: Model["rules"]["nodeTaints"][string]) {
  const one = (tol: (typeof tols)[number], taint: (typeof taints)[number]) => {
    const effect = tol.effect || ""
    if (effect && effect !== taint.effect) return false
    const key = tol.key || ""
    if (!key) return tol.operator === "Exists"
    if (key !== taint.key) return false
    if (tol.operator === "Exists") return true
    return (tol.value || "") === (taint.value || "")
  }
  for (const taint of taints) {
    if (taint.effect !== "NoSchedule" && taint.effect !== "NoExecute") continue
    if (!tols.some((t) => one(t, taint))) return false
  }
  return true
}

function canHost(rules: Model["rules"], anti: Set<string>, pod: Pod, node: string, present: Set<string>) {
  const labels = rules.nodeLabels[node]
  const selector = rules.nodeSelector[pod.wkey] ?? {}
  if (Object.keys(selector).length) {
    if (!labels) return false
    if (Object.entries(selector).some(([k, v]) => labels[k] !== v)) return false
  }
  const terms = rules.nodeAffinity[pod.wkey] ?? []
  if (terms.length) {
    if (!labels) return false
    const matches = (term: Record<string, string[]>) => Object.entries(term).every(
      ([k, values]) => k in labels && (!values.length || values.includes(labels[k])))
    if (!terms.some(matches)) return false
  }
  const taints = rules.nodeTaints[node] ?? []
  if (taints.length && !tolerates(rules.tolerations[pod.wkey] ?? [], taints)) return false
  if (anti.has(pod.wkey) && present.has(pod.wkey)) return false
  // Its volume cannot follow it to another zone.
  const zone = rules.zoneBound?.[`${pod.ns}/${pod.name}`]
  if (zone && (labels ?? {})["topology.kubernetes.io/zone"] !== zone) return false
  return true
}

// -- Cluster Autoscaler v1.35.0's scale-down loop (autoscaler.simulate_ca_scale_down)

interface Run {
  removed: string[]
  removedEmpty: string[]
  vetoed: Record<string, string>
  placement: Map<string, Pod[]>
  total: number
  added: number
}

const REPLICATED_KINDS = new Set(["ReplicationController", "Job", "ReplicaSet", "StatefulSet"])

type Verdict = ["skip" | "ds" | "move" | "block", string]

function scaleDown(model: Model, nodes: string[], pods: Pod[], minSize: number, unschedulable: Set<string>,
                   lifted: boolean | Set<string> = false): Run {
  const { ca, rules } = model
  const run: Run = { removed: [], removedEmpty: [], vetoed: {}, placement: new Map(), total: 0, added: 0 }
  const allocCpu = Math.max(0, model.allocCpu)
  const allocMem = Math.max(0, model.allocMem)
  if (!ca.enabled) {
    run.vetoed["*"] = "scale-down is disabled"
    return run
  }
  if (allocCpu <= 0 || allocMem <= 0 || !nodes.length) return run

  const all = lifted === true
  const lift = lifted instanceof Set ? lifted : new Set<string>()
  const anti = new Set(rules.antiaffinity)
  const safe = new Set(rules.safeToEvict ?? [])
  const notSafe = new Set(rules.notSafe ?? [])
  const local = new Set(rules.localStorage ?? [])
  const noScaleDown = new Set(rules.scaleDownDisabled ?? [])
  const hardSpread = rules.hardSpread ?? {}
  const threshold = ca.threshold
  const customControllers = ca.customControllers ?? true
  const systemTimeout = ca.systemTimeout ?? 3600
  const parallelism = Math.max(1, ca.parallelism ?? 10)
  const drainParallelism = Math.max(1, ca.drainParallelism ?? 1)
  const now = model.now ?? null

  const remaining = [...nodes].sort(strCmp)
  const byNode = new Map<string, Pod[]>(remaining.map((n) => [n, []]))
  // Copies: the replay moves pods, and the caller's placement must not move with it.
  const ordered = pods.map((p, i) => ({ p, i }))
    .sort((x, y) => strCmp(x.p.ns, y.p.ns) || strCmp(x.p.name, y.p.name) || x.i - y.i).map((x) => x.p)
  for (const p of ordered) byNode.get(p.node)?.push({ ...p })
  const fresh = new Set<string>()

  const pdbKey = (wkey: string) => { const d = rules.pdb[wkey]; return d ? `${d.ns}/${d.name}` : null }
  const budgets = (): Map<string, number | null> =>
    new Map(Object.entries(rules.pdbAllowed ?? {}))

  const verdict = (p: Pod, pdbLeft: Map<string, number | null> | null): Verdict => {
    if (isMirror(p)) return ["skip", ""]
    if (isDs(p)) return ["ds", ""]
    if (safe.has(p.wkey)) return ["move", ""]
    const subject = subjectOf(p)
    if (all || lift.has(subject)) return ["move", ""]
    if (isBare(p)) return ["block", `${subject} has no controller`]
    if (customControllers && !REPLICATED_KINDS.has(p.owner)) {
      return ["block", `${subject} is owned by a custom controller (${p.owner})`]
    }
    if (ca.skipsSystemPods && p.ns === "kube-system" && !rules.pdb[p.wkey]) {
      const age = p.created === null || p.created === undefined || now === null ? null : now - p.created
      if (age !== null && age > systemTimeout) return ["move", ""]
      return ["block", `${subject} has no PDB (kube-system pod ${p.name}${age === null ? ", age unknown)" : ")"}`]
    }
    if (notSafe.has(p.wkey)) return ["block", `${subject} is annotated safe-to-evict=false`]
    if (p.wkey in rules.drainBlocked && !local.has(p.wkey)) return ["block", rules.drainBlocked[p.wkey]]
    if (ca.skipsLocalStorage && local.has(p.wkey)) {
      return ["block", `${subject} uses local storage without safe-to-evict`]
    }
    if (pdbLeft) {
      const k = pdbKey(p.wkey)
      if (k !== null) {
        const left = pdbLeft.get(k)
        if (left !== null && left !== undefined && left < 1) {
          return ["block", `PDB ${k} allows no disruption (${subject})`]
        }
      }
    }
    return ["move", ""]
  }

  const utilisation = (n: string) => {
    let cpu = 0
    let mem = 0
    let skipCpu = 0
    let skipMem = 0
    for (const p of byNode.get(n)!) {
      if ((ca.ignoresDaemonsets && isDs(p)) || (ca.ignoresMirror && isMirror(p))) {
        skipCpu += p.cpu
        skipMem += p.mem
        continue
      }
      cpu += p.cpu
      mem += p.mem
    }
    const capCpu = allocCpu - skipCpu
    const capMem = allocMem - skipMem
    return Math.max(capCpu > 0 ? cpu / capCpu : 1, capMem > 0 ? mem / capMem : 1)
  }

  const spreadOk = (p: Pod, target: string, where: Map<string, Pod[]>, among: string[]) => {
    const domain = (n: string, key: string): string | undefined =>
      HOSTNAME_KEYS.includes(key) ? n : (rules.nodeLabels[n] ?? {})[key]
    for (const c of hardSpread[p.wkey] ?? []) {
      const key = c.key || ""
      const skew = c.maxSkew ?? 1
      const counts = new Map<string, number>()
      for (const n of among) {
        if (!canHost(rules, anti, p, n, new Set())) continue
        const d = domain(n, key)
        if (d === undefined) continue
        let mine = 0
        for (const q of where.get(n)!) if (q.wkey === p.wkey) mine += 1
        counts.set(d, (counts.get(d) ?? 0) + mine)
      }
      const d = domain(target, key)
      if (d === undefined || !counts.size) continue
      if ((counts.get(d) ?? 0) + 1 - Math.min(...counts.values()) > skew) return false
    }
    return true
  }

  const fits = (p: Pod, target: string, where: Map<string, Pod[]>, among: string[]) => {
    if (unschedulable.has(target)) return false
    const here = where.get(target)!
    if (model.maxPods && here.length + 1 > model.maxPods) return false
    let c = 0
    let m = 0
    for (const q of here) { c += q.cpu; m += q.mem }
    if (p.cpu > allocCpu - c) return false
    if (p.mem > allocMem - m) return false
    const present = new Set(here.filter((q) => anti.has(q.wkey)).map((q) => q.wkey))
    if (!canHost(rules, anti, p, target, present)) return false
    return spreadOk(p, target, where, among)
  }

  // The scheduler plugin runner's rotation, kept for the whole replay.
  let cursor = 0
  const findPlace = (node: string, movers: Pod[], where: Map<string, Pod[]>, gone: string[]): boolean => {
    const listing = remaining.filter((n) => n !== node && !gone.includes(n))
    if (!listing.length && movers.length) return false
    const placed: [string, Pod][] = []
    for (const p of movers) {
      let found = -1
      for (let i = 0; i < listing.length; i++) {
        const idx = (cursor + i) % listing.length
        if (fits(p, listing[idx], where, listing)) { found = idx; break }
      }
      if (found < 0) {
        for (const [t, q] of placed) { const l = where.get(t)!; l.splice(l.indexOf(q), 1) }
        return false
      }
      const target = listing[found]
      cursor = (found + 1) % listing.length
      where.get(target)!.push(p)
      placed.push([target, p])
    }
    return true
  }

  const schedule = (p: Pod): string | null => {
    let best: string | null = null
    let bestScore = -1
    for (const n of remaining) {
      if (!fits(p, n, byNode, remaining)) continue
      const score = schedulerScore(byNode.get(n)!, p, allocCpu, allocMem)
      if (score > bestScore) { best = n; bestScore = score }
    }
    return best
  }

  let previous = new Set<string>()
  for (let it = 0; it < 500; it++) {
    if (remaining.length <= minSize) break
    const isEmpty = (n: string) => byNode.get(n)!.every((p) => isDs(p) || isMirror(p))
    const candidates = remaining.filter((n) => !fresh.has(n) && utilisation(n) <= threshold
      && (!noScaleDown.has(n) || all || lift.has(`node/${n}`)))
      .map((n) => ({ n, e: isEmpty(n) ? 0 : 1, p: previous.has(n) ? 0 : 1, i: remaining.indexOf(n) }))
      .sort((x, y) => x.e - y.e || x.p - y.p || x.i - y.i)
      .map((x) => x.n)

    const sim = new Map<string, Pod[]>([...byNode].map(([n, v]) => [n, [...v]]))
    const pdbLeft = budgets()
    const unneeded: string[] = []
    const moves = new Map<string, Pod[]>()
    const risky = new Set<string>()
    for (const node of candidates) {
      const movers: Pod[] = []
      let blocked = false
      for (const p of sim.get(node)!) {
        const [outcome] = verdict(p, pdbLeft)
        if (outcome === "block") { blocked = true; break }
        if (outcome === "move") movers.push(p)
      }
      if (blocked) continue
      if (!findPlace(node, movers, sim, unneeded)) continue
      sim.set(node, [])
      const count = new Map<string, number>()
      for (const p of movers) {
        const k = pdbKey(p.wkey)
        if (k !== null) count.set(k, (count.get(k) ?? 0) + 1)
      }
      for (const [k, c] of count) {
        const left = pdbLeft.get(k)
        if (left !== null && left !== undefined) {
          if (left < c) risky.add(node)
          pdbLeft.set(k, left - c)
        }
      }
      unneeded.push(node)
      moves.set(node, movers)
    }
    previous = new Set(unneeded)
    if (!unneeded.length) break

    let size = remaining.length
    const emptyOk: string[] = []
    let drainOk: string[] = []
    for (const node of [...unneeded.filter((n) => !moves.get(n)!.length),
                        ...unneeded.filter((n) => moves.get(n)!.length)]) {
      if (size - 1 < minSize) continue
      if (moves.get(node)!.length) drainOk.push(node)
      else emptyOk.push(node)
      size -= 1
    }
    drainOk = [...drainOk.filter((n) => !risky.has(n)), ...drainOk.filter((n) => risky.has(n))]
    const deleteEmpty = emptyOk.slice(0, parallelism)
    const room = parallelism - deleteEmpty.length
    const deleteDrain = drainOk.slice(0, Math.max(0, Math.min(drainParallelism, room)))
    if (!deleteEmpty.length && !deleteDrain.length) break
    for (const node of deleteEmpty) {
      remaining.splice(remaining.indexOf(node), 1)
      run.removedEmpty.push(node)
      byNode.delete(node)
    }
    // Every node being deleted is tainted before any is drained.
    const evicted: Pod[] = []
    let daemons: Pod[] = []
    for (const node of deleteDrain) {
      const here = byNode.get(node)!
      evicted.push(...here.filter((p) => !(isDs(p) || isMirror(p))))
      if (!daemons.length) daemons = here.filter(isDs)
      remaining.splice(remaining.indexOf(node), 1)
      run.removed.push(node)
      byNode.delete(node)
    }
    for (const p of evicted) {
      let target = schedule(p)
      if (target === null) {
        // Pending: Cluster Autoscaler scales the group up for it.
        run.added += 1
        target = `(new node ${run.added})`
        remaining.push(target)
        fresh.add(target)
        byNode.set(target, daemons.map((d) => ({ ...d, node: target! })))
      }
      p.node = target
      byNode.get(target)!.push(p)
    }
  }

  // Why each node that stays is kept.
  const finalLeft = budgets()
  for (const node of remaining) {
    if (fresh.has(node) || utilisation(node) > threshold) continue
    if (noScaleDown.has(node) && !(all || lift.has(`node/${node}`))) {
      run.vetoed[node] = `node ${node} is annotated cluster-autoscaler.kubernetes.io/scale-down-disabled`
      continue
    }
    for (const p of byNode.get(node)!) {
      const [outcome, why] = verdict(p, finalLeft)
      if (outcome === "block") { run.vetoed[node] = why; break }
    }
  }
  for (const n of remaining) run.placement.set(n, [...byNode.get(n)!])
  run.total = run.removed.length + run.removedEmpty.length
  return run
}

// -- the two controllers --------------------------------------------------------

export interface Outcome {
  nodes: number
  mode: "autoscaler" | "scheduler"
  placement: Map<string, Pod[]>
  added: number
  vetoed: Record<string, string>
  unplaceable: PackPod[]
}

/** `realistic.same_type`: the edits land on today's placement, then CA runs. */
export function sameType(model: Model, deltas: Deltas, keep: Keep, minNodes: number,
                         lifted: boolean | Set<string> = false): Outcome | null {
  let pods = podsFrom(model, deltas, true)
  pods = applyKeep(pods, keep)
  let nodes = sortedNodes(pods)
  if (!nodes.length) return null
  const [added, unplaceable] = rescheduleOverflow(pods, nodes, model)
  pods = pods.filter((p) => p.node)
  nodes = sortedNodes(pods)
  const run = scaleDown(model, nodes, pods, minNodes || 0, new Set(model.rules.unschedulable), lifted)
  if (!run.placement.size && !run.total) {
    for (const p of pods) {
      if (!p.node) continue
      if (!run.placement.has(p.node)) run.placement.set(p.node, [])
      run.placement.get(p.node)!.push(p)
    }
  }
  return {
    nodes: Math.max(model.start + added + run.added - run.total, minNodes, 1),
    mode: "autoscaler", placement: run.placement, added: added + run.added, vetoed: run.vetoed,
    // A pod no node of this type holds never schedules: not an option for this group.
    unplaceable: unplaceable.map((p): PackPod => ({
      name: `${p.ns}/${p.name}`, workload: p.wkey, cpu: p.cpu, memory: p.mem, antiaffinity: "",
    })),
  }
}

/** `realistic.new_type`: by request onto fresh nodes, 100% of allocatable. */
export function newType(model: Model, deltas: Deltas, keep: Keep, cpuPerNode: number,
                        memPerNode: number, maxPods: number, minNodes: number): Outcome {
  const anti = new Set(model.rules.antiaffinity)
  const pods = applyKeep(podsFrom(model, deltas, false), keep).filter((p) => !isDs(p))
  const byPack = new Map<PackPod, Pod>()
  const packPods: PackPod[] = pods.map((p) => {
    const pp: PackPod = { workload: p.wkey, cpu: p.cpu, memory: p.mem,
                          antiaffinity: anti.has(p.wkey) ? p.wkey : "", name: `${p.ns}/${p.name}` }
    byPack.set(pp, p)
    return pp
  })
  const packed = pack(packPods, { cpuPerNode, memPerNode, maxPods, headroom: 1.0 })
  const placement = new Map<string, Pod[]>()
  // The packer reports utilisation, not membership; rebuild it the way it
  // placed, which is first fit in its own order -- replayed here exactly.
  replayFirstFit(packPods, cpuPerNode, memPerNode, maxPods).forEach((node, i) => {
    placement.set(`(new ${i + 1})`, node.map((pp) => byPack.get(pp)!))
  })
  return {
    nodes: Math.max(packed.nodes, minNodes, pods.length ? 1 : 0, volumeZones(model, pods).length),
    mode: "scheduler", placement, added: 0, vetoed: {}, unplaceable: packed.unplaceable,
  }
}

/** `realistic.volume_zones`: the zones a new group needs a node in, one for
 *  each zone holding a pod's EBS volume. */
export function volumeZones(model: Model, pods: Pod[]): string[] {
  const bound = model.rules.zoneBound ?? {}
  return [...new Set(pods.map((p) => bound[`${p.ns}/${p.name}`]).filter((z): z is string => !!z))].sort()
}

/** First-fit decreasing at 100%, returning which pods share a node. Same order
 *  and rules as `pack`, which only returns counts. */
function replayFirstFit(pods: PackPod[], cpu: number, mem: number, maxPods: number) {
  const GIB = 1024 ** 3
  const key = (p: PackPod, i: number): [number, number, string] =>
    [-Math.max(p.cpu, p.memory / GIB / 4), -p.cpu, p.name ?? String(i)]
  const ordered = pods.map((p, i) => ({ p, k: key(p, i) }))
    .sort((a, b) => a.k[0] - b.k[0] || a.k[1] - b.k[1] || strCmp(a.k[2], b.k[2]))
    .map((x) => x.p)
  const nodes: { cpu: number; mem: number; pods: PackPod[]; present: Set<string> }[] = []
  for (const p of ordered) {
    if (p.cpu > cpu || p.memory > mem) continue
    let node = nodes.find((n) => n.pods.length < maxPods && p.cpu <= n.cpu && p.memory <= n.mem
      && !(p.antiaffinity && n.present.has(p.antiaffinity)))
    if (!node) {
      node = { cpu, mem, pods: [], present: new Set() }
      nodes.push(node)
    }
    node.cpu -= p.cpu
    node.mem -= p.memory
    node.pods.push(p)
    if (p.antiaffinity) node.present.add(p.antiaffinity)
  }
  return nodes.map((n) => n.pods)
}

// -- checks ------------------------------------------------------------------------

export interface NodeHeat {
  node: string
  cpu: number
  mem: number
  /** Pods using more than they reserve, BestEffort first -- evicted first. */
  exposed: { pod: string; wkey: string; bestEffort: boolean; memOver: number }[]
}

export interface Load {
  verdict: "ok" | "warn" | "unsafe" | "unknown"
  hotCpu: number
  hotMem: number
  notes: string[]
  /** Nodes at or past a threshold, with the pods on them that use more than
   *  they reserve. Extra to the Python, which reports only the verdict. */
  hot: NodeHeat[]
}

/** `realistic.load_check`, plus which pods sit on the nodes it flags. */
export function loadCheck(
  out: Outcome, allocCpu: number, allocMem: number, model: Model,
  replicasNow: Record<string, number>, replicasKept: Record<string, number>,
): Load {
  if (!out.placement.size || allocCpu <= 0 || allocMem <= 0) {
    return { verdict: "unknown", hotCpu: 0, hotMem: 0, notes: ["no placement to check"], hot: [] }
  }
  let hotCpu = 0
  let hotMem = 0
  let overMem = 0
  let overCpu = 0
  const hot: NodeHeat[] = []
  for (const [node, pods] of out.placement) {
    let cpu = 0
    let mem = 0
    let hasDs = false
    const exposed: NodeHeat["exposed"] = []
    for (const p of pods) {
      const ds = isDs(p)
      hasDs = hasDs || ds
      const load = model.loads[p.wkey]
      let scale = 1
      if (!ds && replicasKept[p.wkey]) scale = Math.max(1, (replicasNow[p.wkey] ?? 0) / replicasKept[p.wkey])
      const c = (load ? load[0] : p.cpu) * scale
      const m = (load ? load[1] : p.mem) * scale
      cpu += c
      mem += m
      if (!ds && load && (m > p.mem * 1.01 || c > p.cpu * 1.01)) {
        exposed.push({ pod: `${p.ns}/${p.name}`, wkey: p.wkey,
                       bestEffort: p.cpu === 0 && p.mem === 0, memOver: Math.max(0, m - p.mem) })
      }
    }
    if (!hasDs) {
      cpu += model.dsBusy[0]
      mem += model.dsBusy[1]
    }
    const c = cpu / allocCpu
    const m = mem / allocMem
    hotCpu = Math.max(hotCpu, c)
    hotMem = Math.max(hotMem, m)
    if (m >= MEM_CRITICAL) overMem += 1
    if (c >= CPU_WARNING) overCpu += 1
    if (m >= MEM_WARNING || c >= CPU_WARNING) {
      exposed.sort((a, b) => Number(b.bestEffort) - Number(a.bestEffort) || b.memOver - a.memOver)
      hot.push({ node, cpu: c, mem: m, exposed })
    }
  }
  const baseMem = model.today.hotMem
  const notes: string[] = []
  let verdict: Load["verdict"] = "ok"
  const pct = (x: number) => `${Math.round(x * 100)}%`
  if (hotMem >= MEM_CRITICAL && hotMem > baseMem + 0.02) {
    verdict = "unsafe"
    notes.push(`${overMem} node(s) would need ${pct(hotMem)} of their memory at busy load -- the kubelet evicts pods before that`)
  } else if (hotMem >= MEM_WARNING && hotMem > baseMem + 0.02) {
    verdict = "warn"
    notes.push(`the busiest node would use ${pct(hotMem)} of its memory at busy load`)
  }
  if (hotCpu >= CPU_WARNING && hotCpu > model.today.hotCpu + 0.02) {
    verdict = "unsafe"
    notes.push(`${overCpu} node(s) would need ${pct(hotCpu)} of their CPU at busy load`)
  }
  return { verdict, hotCpu: Math.round(hotCpu * 1e4) / 1e4, hotMem: Math.round(hotMem * 1e4) / 1e4, notes, hot }
}

/** `realistic.trend_check`: would these nodes have held the month's peaks? */
export function trendCheck(trend: Trend | null | undefined, nodesNow: number, nodesAfter: number,
                           minNodes: number, cpuScale: number, memScale: number) {
  if (!trend || !trend.t?.length) return { verdict: "unknown" as const, notes: [] as string[] }
  const ratio = nodesAfter / Math.max(nodesNow, 1)
  const floor = Math.max(1, minNodes)
  const over = { cpu: 0, mem: 0 }
  const peak = { cpu: 0, mem: 0 }
  let points = 0
  for (let i = 0; i < trend.t.length; i++) {
    const n = trend.nodes[i]
    if (!n) continue
    const after = Math.max(floor, Math.ceil(n * ratio - 1e-9))
    let seen = false
    for (const [k, scale] of [["cpu", cpuScale], ["mem", memScale]] as const) {
      const used = trend[k][i]
      const cap = trend[k === "cpu" ? "cpuCap" : "memCap"][i]
      if (used === null || used === undefined || !cap) continue
      seen = true
      const share = used / (cap / n * scale * after)
      peak[k] = Math.max(peak[k], share)
      if (share > 1.0) over[k] += 1
    }
    if (seen) points += 1
  }
  if (!points) return { verdict: "unknown" as const, notes: [] as string[] }
  const peakCpu = Math.round(peak.cpu * 1e4) / 1e4
  const peakMem = Math.round(peak.mem * 1e4) / 1e4
  const notes: string[] = []
  let verdict: "ok" | "warn" | "unsafe" = "ok"
  const pct = (x: number) => `${Math.round(x * 100)}%`
  if (over.mem) {
    verdict = "unsafe"
    notes.push(`the nodes used more memory than this option has in ${over.mem} of ${points} 6-hour periods (peak ${pct(peak.mem)})`)
  }
  if (over.cpu > Math.max(1, Math.floor(points / 20))) {
    verdict = "unsafe"
    notes.push(`the nodes used more CPU than this option has in ${over.cpu} of ${points} 6-hour periods (peak ${pct(peak.cpu)})`)
  } else if (over.cpu && verdict === "ok") {
    verdict = "warn"
    notes.push(`CPU would saturate in ${over.cpu} of ${points} 6-hour periods`)
  }
  return { verdict, notes, peakCpu, peakMem }
}

/** `realistic.HEADROOM`: what an option must leave to be recommended. */
export const HEADROOM = { peakMem: 0.80, peakCpu: 0.90, hotMem: 0.85, hotCpu: 0.90 } as const
const HEADROOM_TEXT: Record<keyof typeof HEADROOM, string> = {
  peakMem: "the nodes' memory peaks at {v} of this option over the window",
  peakCpu: "the nodes' CPU peaks at {v} of this option over the window",
  hotMem: "the busiest node would use {v} of its memory at busy load",
  hotCpu: "the busiest node would use {v} of its CPU at busy load",
}

export interface Margin { ok: boolean; notes: string[] }

/** `realistic.margin` for CPU and memory: judged against today, so a group
 *  already above a target is not blamed on the change. */
export function margin(marks: Partial<Record<keyof typeof HEADROOM, number>>,
                       today: Partial<Record<keyof typeof HEADROOM, number>>): Margin {
  const notes: string[] = []
  for (const key of Object.keys(HEADROOM) as (keyof typeof HEADROOM)[]) {
    const v = marks[key]
    const target = HEADROOM[key]
    if (v === undefined || v <= target || v <= (today[key] ?? 0) + 0.02) continue
    notes.push(HEADROOM_TEXT[key].replace("{v}", `${Math.round(v * 100)}%`) +
               ` -- above the ${Math.round(target * 100)}% target`)
  }
  return { ok: !notes.length, notes }
}

// -- one call for a selection -----------------------------------------------------

export interface Realized {
  outcome: Outcome
  load: Load
  trend: ReturnType<typeof trendCheck>
  /** The worst of the two checks, with every note that led to it. */
  verdict: "ok" | "warn" | "unsafe" | "unknown"
  notes: string[]
  /** Headroom against `HEADROOM`. Fitting is not the same as recommendable. */
  margin: Margin
}

export interface Shape {
  /** Same instance type as today: the CA replay. Otherwise a new node group. */
  sameType: boolean
  minNodes: number
  maxPods: number
  nodesNow: number
  /** The option's instance and today's: vCPU and memory in GiB, as described
   *  by EC2. Capacity is derived from these exactly as `scenarios` derives it,
   *  not from the rounded figures shown on the option. */
  vcpu: number
  memGib: number
  currentVcpu: number
  currentMemGib: number
  /** An option priced "once unblocked": CA's drain vetoes are lifted --
   *  every one, or only those of the workloads named. */
  lifted?: boolean | string[]
  /** The option's instance type, for its node label in the exact replay. */
  name?: string
  /** Local disk, GB: today's type's instance store, the option's (undefined
   *  when not described), and the measured root volume -- the new node's
   *  ephemeral storage follows them (`realistic._ephemeral_after`). */
  storeGbNow?: number
  storeGb?: number
  rootGb?: number
}

const GIB_BYTES = 1024 ** 3

/** `scenarios.capacity` and `full_alloc`: a node's allocatable on another
 *  instance type carries the same absolute system overhead as today's. */
function shapeCapacity(model: Model, s: Shape) {
  const overheadCpu = Math.max(0, s.currentVcpu - model.allocCpu)
  const overheadMem = Math.max(0, s.currentMemGib * GIB_BYTES - model.allocMem)
  const fullCpu = s.vcpu - overheadCpu
  const fullMem = s.memGib * GIB_BYTES - overheadMem
  return { fullCpu, fullMem, cpu: fullCpu - model.dsCpu, mem: fullMem - model.dsMem }
}

/** The exact replay's answer in this module's terms. */
function fromLive(o: LiveOutcome | null, mode: Outcome["mode"] = "autoscaler"): Outcome | null {
  if (!o) return null
  const placement = new Map<string, Pod[]>()
  for (const [n, pods] of o.placement) {
    placement.set(n, pods.map((p) => ({ ns: p.ns, name: p.name, node: n, cpu: p.cpu / 1000, mem: p.mem,
                                         owner: p.ownerKind, wkey: p.wkey, created: p.created })))
  }
  return {
    nodes: o.nodes, mode, placement, added: o.added, vetoed: o.vetoed,
    unplaceable: o.unplaceable.map((p): PackPod => ({ name: `${p.ns}/${p.name}`, workload: p.wkey,
                                                      cpu: p.cpu / 1000, memory: p.mem, antiaffinity: "" })),
  }
}

export function realize(model: Model, deltas: Deltas, keep: Keep, shape: Shape,
                        requests?: Requests): Realized | null {
  const cap = shapeCapacity(model, shape)
  if (model.live && model.group && requests !== undefined) {
    const lifted = Array.isArray(shape.lifted) ? new Set(shape.lifted) : !!shape.lifted
    const live = shape.sameType
      ? fromLive(liveSameType(model.live, model.group, requests, keep, shape.minNodes, lifted))
      : (cap.fullCpu <= 0 || cap.fullMem <= 0 ? null
        : fromLive(liveNewType(model.live, model.group, requests, keep, [cap.fullCpu, cap.fullMem],
                               shape.maxPods, shape.minNodes, {
                                 name: shape.name ?? "new",
                                 store: shape.storeGb === undefined ? undefined
                                   : { now: shape.storeGbNow ?? 0, next: shape.storeGb, rootGb: shape.rootGb ?? 0 },
                               }), "scheduler"))
    return live ? judge(model, live, keep, shape, cap) : null
  }
  const out = shape.sameType
    ? sameType(model, deltas, keep, shape.minNodes,
               Array.isArray(shape.lifted) ? new Set(shape.lifted) : !!shape.lifted)
    : (cap.cpu <= 0 || cap.mem <= 0 ? null
      : newType(model, deltas, keep, cap.cpu, cap.mem, Math.max(1, shape.maxPods - (model.dsPods ?? 0)),
                shape.minNodes))
  if (!out) return null
  return judge(model, out, keep, shape, cap)
}

function judge(model: Model, out: Outcome, keep: Keep, shape: Shape,
               cap: ReturnType<typeof shapeCapacity>): Realized {
  const replicasNow: Record<string, number> = {}
  for (const r of model.placed) {
    if (r.o === "DaemonSet") continue
    const w = model.keys[r.k]
    replicasNow[w] = (replicasNow[w] ?? 0) + 1
  }
  const kept: Record<string, number> = {}
  for (const [w, n] of Object.entries(keep)) kept[w] = Math.min(n, replicasNow[w] ?? n)
  const load = loadCheck(out, cap.fullCpu, cap.fullMem, model, replicasNow, kept)
  const trend = trendCheck(model.trend, shape.nodesNow, out.nodes, shape.minNodes,
                           shape.vcpu / shape.currentVcpu, shape.memGib / shape.currentMemGib)
  const rank = { unsafe: 3, warn: 2, ok: 1, unknown: 0 } as const
  const verdict = rank[trend.verdict] > rank[load.verdict] ? trend.verdict : load.verdict
  const trendToday = trendCheck(model.trend, shape.nodesNow, shape.nodesNow, shape.nodesNow, 1, 1)
  const room = margin(
    { hotCpu: load.hotCpu, hotMem: load.hotMem, peakCpu: trend.peakCpu, peakMem: trend.peakMem },
    { ...model.today, peakCpu: trendToday.peakCpu, peakMem: trendToday.peakMem })
  return { outcome: out, load, trend, verdict, notes: [...load.notes, ...trend.notes], margin: room }
}
