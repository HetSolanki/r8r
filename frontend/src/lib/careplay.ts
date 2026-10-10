/** Cluster Autoscaler v1.35.0 scale-down over the whole cluster -- the port of
 *  rightsizer/engine/careplay.py, step for step, so the console and the engine agree on
 *  every node an option frees. See that module for the rules and their sources. */

import {
  Cluster, DELETE_TAINT, SOFT_TAINT, fits, isBare, isDs, isMirror, schedule, selectorMatches, staticOk,
  subjectOf, type KNode, type KPod, type Selector,
} from "@/lib/kscheduler"

const REPLICATED_KINDS = new Set(["ReplicationController", "Job", "ReplicaSet", "StatefulSet"])
const GPU_LABEL = "k8s.amazonaws.com/accelerator"
const LONG_TERMINATING_EXTRA = 30

export interface Group {
  name: string; min: number; max: number; options: Record<string, string>
  /** The ASG's zones, in its order; a new instance lands in the one with the
   *  fewest, ties in this order. Empty when unknown (the template's zone is kept). */
  zones?: string[]
}

/** careplay.ZONE_LABELS: every label naming the zone, rewritten when a node
 *  is launched elsewhere (a PV's node affinity reads topology.ebs.csi.aws.com/zone). */
export const ZONE_LABELS = ["topology.kubernetes.io/zone", "failure-domain.beta.kubernetes.io/zone",
                            "topology.ebs.csi.aws.com/zone"]
export const ZONE_ID_LABEL = "topology.k8s.aws/zone-id"

export const zoneOf = (n: KNode): string => n.labels[ZONE_LABELS[0]] ?? n.labels[ZONE_LABELS[1]] ?? ""

/** careplay.zone_ids: zone -> AWS zone-id from the nodes standing there, learned
 *  before a replay removes any. */
export function zoneIds(cluster: Cluster): Record<string, string> {
  const out: Record<string, string> = {}
  for (const n of cluster.nodes.values()) {
    const z = zoneOf(n)
    if (z && n.labels[ZONE_ID_LABEL] && !(z in out)) out[z] = n.labels[ZONE_ID_LABEL]
  }
  return out
}

/** careplay.launch_zone: where the ASG puts the group's next instance -- the
 *  zone with the fewest of its nodes, ties in the ASG's order; "" when unknown. */
export function launchZone(cluster: Cluster, groupOf: Record<string, string>, group: string,
                           info: Group | undefined): string {
  const zones = info?.zones ?? []
  if (!zones.length) return ""
  const count = new Map(zones.map((z) => [z, 0]))
  for (const n of cluster.order) {
    if (groupOf[n] !== group) continue
    const z = zoneOf(cluster.nodes.get(n)!)
    if (count.has(z)) count.set(z, count.get(z)! + 1)
  }
  return zones.reduce((best, z) => (count.get(z)! < count.get(best)! ? z : best), zones[0])
}

/** careplay.in_zone: `template` as launched in `zone`. */
export function inZone(template: KNode, zone: string, ids: Record<string, string>): KNode {
  if (!zone || zone === zoneOf(template)) return template
  const labels = { ...template.labels }
  for (const key of ZONE_LABELS) if (key in labels) labels[key] = zone
  if (ids[zone]) labels[ZONE_ID_LABEL] = ids[zone]
  else delete labels[ZONE_ID_LABEL]
  return { ...template, labels }
}
export interface Budget { ns: string; name: string; selector: Selector | null; allowed: number | null }
export interface Settings {
  threshold: number; gpuThreshold: number; ignoreDaemonsets: boolean; ignoreMirror: boolean
  skipLocalStorage: boolean; skipSystemPods: boolean; skipCustomControllers: boolean
  systemTimeout: number; parallelism: number; drainParallelism: number; expendableCutoff: number
  okUnready: number; maxUnreadyPct: number; unneededTime: number; scanInterval: number; enabled: boolean
}

export interface Result {
  removed: string[]; removedEmpty: string[]; added: Record<string, number>
  vetoed: Record<string, string>; blockers: Record<string, [string, string][]>
  notEvacuable: string[]; atMin: string[]; risky: string[]; iterations: number; healthy: boolean
  cluster: Cluster; groupOf: Record<string, string>
}

export const nodesIn = (r: Result, group: string) => r.cluster.order.filter((n) => r.groupOf[n] === group)

const truthy = (v: unknown) => ["true", "1"].includes(String(v).toLowerCase())

function longTerminating(p: KPod, now: number | null): boolean {
  return p.deleting !== null && now !== null && now > p.deleting + (p.grace ?? 30) + LONG_TERMINATING_EXTRA
}

function matching(pod: KPod, budgets: Budget[]): Budget[] {
  return budgets.filter((b) => b.ns === pod.ns && selectorMatches(b.selector, pod.labels))
}

type Outcome = "skip" | "ds" | "move" | "block"

function drainVerdict(pod: KPod, s: Settings, budgets: Budget[], left: Map<string, number | null> | null,
                      now: number | null, lifted: boolean, lift: Set<string>): [Outcome, string] {
  if (isMirror(pod)) return ["skip", ""]
  if (longTerminating(pod, now)) return ["skip", ""]
  if (isDs(pod)) return ["ds", ""]
  if (pod.safeToEvict === "true") return ["move", ""]
  const subject = subjectOf(pod)
  if (lifted || lift.has(subject)) return ["move", ""]
  if (isBare(pod)) return ["block", `${subject} has no controller`]
  if (s.skipCustomControllers && !REPLICATED_KINDS.has(pod.ownerKind)) {
    return ["block", `${subject} is owned by a custom controller (${pod.ownerKind})`]
  }
  const mine = matching(pod, budgets)
  if (s.skipSystemPods && pod.ns === "kube-system" && !mine.length) {
    const age = pod.created === null || now === null ? null : now - pod.created
    if (age !== null && age > s.systemTimeout) return ["move", ""]
    return ["block", `${subject} has no PDB (kube-system pod ${pod.name}${age === null ? ", age unknown)" : ")"}`]
  }
  if (pod.safeToEvict === "false") return ["block", `${subject} is annotated safe-to-evict=false`]
  if (s.skipLocalStorage && pod.localVolumes.length) {
    return ["block", `${subject} uses local storage without safe-to-evict (${pod.localVolumes.join(", ")})`]
  }
  if (left) {
    for (const b of mine) {
      const a = left.get(`${b.ns}/${b.name}`)
      if (a !== null && a !== undefined && a < 1) return ["block", `PDB ${b.ns}/${b.name} allows no disruption (${subject})`]
    }
  }
  return ["move", ""]
}

const copyPod = (p: KPod, node = p.node): KPod => ({ ...p, node })
const copyNode = (n: KNode, name: string): KNode =>
  ({ ...n, name, images: {}, labels: { ...n.labels, "kubernetes.io/hostname": name } })

export function replay(cluster: Cluster, s: Settings, groupOf: Record<string, string>,
                       groups: Record<string, Group>, budgets: Budget[], now: number | null,
                       lift: Set<string> = new Set(), lifted = false, maxIterations = 500): Result {
  const ids = zoneIds(cluster)  // before any node goes: one may be a zone's only
  const res: Result = {
    removed: [], removedEmpty: [], added: {}, vetoed: {}, blockers: {}, notEvacuable: [], atMin: [],
    risky: [], iterations: 0, healthy: true, cluster, groupOf,
  }
  for (const name of [...cluster.order]) {
    cluster.on.set(name, cluster.on.get(name)!.filter((p) => p.priority === null || p.priority >= s.expendableCutoff))
  }
  if (!s.enabled) { res.vetoed["*"] = "scale-down is disabled"; return res }
  let unready = 0
  for (const n of cluster.nodes.values()) if (!n.ready) unready += 1
  if (unready > s.okUnready && unready > (s.maxUnreadyPct / 100) * cluster.nodes.size) {
    res.healthy = false
    res.vetoed["*"] = `cluster unhealthy: ${unready} of ${cluster.nodes.size} nodes unready`
    return res
  }

  const statik = new Map<object, Map<string, boolean>>()
  const ok = (pod: KPod, node: string, where: Cluster) => {
    let m = statik.get(pod.spec)
    if (!m) { m = new Map(); statik.set(pod.spec, m) }
    let v = m.get(node)
    if (v === undefined) { v = staticOk(pod, where.nodes.get(node)!); m.set(node, v) }
    return fits(pod, node, where, v)
  }
  const opt = (g: string, key: string): string | undefined => groups[g]?.options?.[key]
  const isGpu = (n: KNode) => GPU_LABEL in n.labels || n.gpu > 0

  const utilisation = (node: string): number => {
    const n = cluster.nodes.get(node)!
    const g = groupOf[node] ?? ""
    const o = opt(g, "ignoredaemonsetsutilization")
    const ignoreDs = o === undefined ? s.ignoreDaemonsets : truthy(o)
    if (isGpu(n)) {
      let used = 0; let skip = 0
      for (const p of cluster.on.get(node)!) {
        if ((ignoreDs && isDs(p)) || (s.ignoreMirror && isMirror(p))) { skip += p.gpu; continue }
        if (longTerminating(p, now)) continue
        used += p.gpu
      }
      return n.gpu - skip > 0 ? used / (n.gpu - skip) : 0
    }
    let c = 0; let m = 0; let sc = 0; let sm = 0
    for (const p of cluster.on.get(node)!) {
      if ((ignoreDs && isDs(p)) || (s.ignoreMirror && isMirror(p))) { sc += p.cpu; sm += p.mem; continue }
      if (longTerminating(p, now)) continue
      c += p.cpu; m += p.mem
    }
    const cu = n.cpu - sc > 0 ? c / (n.cpu - sc) : 0
    const mu = n.mem - sm > 0 ? m / (n.mem - sm) : 0
    return Math.max(cu, mu)
  }
  const threshold = (node: string): number => {
    const g = groupOf[node] ?? ""
    const n = cluster.nodes.get(node)!
    const o = opt(g, isGpu(n) ? "scaledowngpuutilizationthreshold" : "scaledownutilizationthreshold")
    return o === undefined ? (isGpu(n) ? s.gpuThreshold : s.threshold) : Number(o)
  }
  const budgetsNow = () => new Map(budgets.map((b) => [`${b.ns}/${b.name}`, b.allowed]))
  const verdict = (p: KPod, left: Map<string, number | null> | null) => drainVerdict(p, s, budgets, left, now, lifted, lift)
  const groupSize = (g: string) => cluster.order.filter((n) => groupOf[n] === g).length

  let cursor = 0
  let hintsOld = new Map<KPod, string>()
  let hintsCur = new Map<KPod, string>()
  const fresh = new Set<string>()
  let previous: string[] = []
  const nPar = s.parallelism
  const loopsPerUnneeded = s.scanInterval > 0 ? Math.max(1, Math.floor(s.unneededTime / s.scanInterval)) : 1

  const findPlace = (node: string, movers: KPod[], sim: Cluster, gone: Set<string>): boolean => {
    const heldNode = sim.nodes.get(node)!
    const held = sim.removeNode(node)
    const listing = [...sim.order]
    const placed: [string, KPod][] = []
    for (const pod of movers) {
      let target: string | null = null
      const hint = hintsCur.get(pod) ?? hintsOld.get(pod)
      if (hint && sim.nodes.has(hint) && !gone.has(hint) && ok(pod, hint, sim)) {
        target = hint
      } else {
        for (let i = 0; i < listing.length; i++) {
          const idx = (cursor + i) % listing.length
          if (!gone.has(listing[idx]) && ok(pod, listing[idx], sim)) {
            target = listing[idx]
            cursor = (idx + 1) % listing.length
            break
          }
        }
      }
      if (target === null) {
        for (const [t, q] of placed) { const l = sim.on.get(t)!; l.splice(l.indexOf(q), 1) }
        sim.addNode(heldNode)
        sim.on.set(node, held)
        return false
      }
      sim.on.get(target)!.push(pod)
      placed.push([target, pod])
      hintsCur.set(pod, target)
    }
    return true
  }

  while (res.iterations < maxIterations) {
    res.iterations += 1
    const atMin = new Set(Object.values(groups).filter((g) => groupSize(g.name) <= g.min).map((g) => g.name))
    const isEmpty = (n: string) => cluster.on.get(n)!.every((p) => ["ds", "skip"].includes(verdict(p, null)[0]))
    const prev = new Set(previous)
    const candidates = cluster.order
      .filter((n) => groupOf[n] in groups && !atMin.has(groupOf[n]) && !fresh.has(n)
        && (!cluster.nodes.get(n)!.scaleDownDisabled || lifted || lift.has(`node/${n}`))
        && utilisation(n) <= threshold(n))
      .map((n) => ({ n, e: isEmpty(n) ? 0 : 1, p: prev.has(n) ? 0 : 1, i: cluster.order.indexOf(n) }))
      .sort((a, b) => a.e - b.e || a.p - b.p || a.i - b.i)
      .map((x) => x.n)

    const sim = cluster.fork()
    const left = budgetsNow()
    const unneeded: string[] = []
    const moversOf = new Map<string, KPod[]>()
    const risky = new Set<string>()
    const limit = Math.min(previous.length + 2 * nPar, nPar * loopsPerUnneeded + nPar)
    for (const node of candidates) {
      if (unneeded.length >= limit) break
      const movers: KPod[] = []
      let blocked = false
      for (const pod of sim.on.get(node)!) {
        const [v] = verdict(pod, left)
        if (v === "block") { blocked = true; break }
        if (v === "move") movers.push(pod)
      }
      if (blocked) continue
      if (!findPlace(node, movers, sim, new Set(unneeded))) continue
      const count = new Map<string, number>()
      for (const pod of movers) for (const b of matching(pod, budgets)) {
        const k = `${b.ns}/${b.name}`
        count.set(k, (count.get(k) ?? 0) + 1)
      }
      for (const [k, c] of count) {
        const a = left.get(k)
        if (a !== null && a !== undefined) {
          if (a < c) risky.add(node)
          left.set(k, a - c)
        }
      }
      unneeded.push(node)
      moversOf.set(node, movers)
    }
    hintsOld = hintsCur
    hintsCur = new Map()
    previous = [...unneeded]
    if (!unneeded.length) break

    const sizes = new Map(Object.keys(groups).map((g) => [g, groupSize(g)]))
    const emptyOk: string[] = []
    let drainOk: string[] = []
    for (const node of [...unneeded.filter((n) => !moversOf.get(n)!.length), ...unneeded.filter((n) => moversOf.get(n)!.length)]) {
      const g = groupOf[node]
      if ((sizes.get(g) ?? 0) - 1 < groups[g].min) continue
      if (moversOf.get(node)!.length) drainOk.push(node)
      else emptyOk.push(node)
      sizes.set(g, sizes.get(g)! - 1)
    }
    drainOk = [...drainOk.filter((n) => !risky.has(n)), ...drainOk.filter((n) => risky.has(n))]
    const deleteEmpty = emptyOk.slice(0, nPar)
    const deleteDrain = drainOk.slice(0, Math.max(0, Math.min(s.drainParallelism, nPar - deleteEmpty.length)))
    if (!deleteEmpty.length && !deleteDrain.length) break
    for (const node of deleteEmpty) { cluster.removeNode(node); res.removedEmpty.push(node) }
    for (const node of deleteDrain) cluster.extraTaints.set(node, [DELETE_TAINT])
    for (const node of unneeded) {
      if (cluster.nodes.has(node) && !deleteDrain.includes(node)) cluster.extraTaints.set(node, [SOFT_TAINT])
    }
    const evicted: [KPod, string][] = []
    const templates = new Map<string, [KNode, KPod[]]>()
    for (const node of deleteDrain) {
      for (const pod of cluster.on.get(node)!) {
        if (isDs(pod) || isMirror(pod)) continue
        if (verdict(pod, null)[0] === "skip") continue
        evicted.push([pod, node])
      }
      templates.set(groupOf[node] ?? "", [cluster.nodes.get(node)!, cluster.on.get(node)!.filter(isDs)])
    }
    for (const [pod, node] of evicted) {
      cluster.unplace(pod)
      if (isBare(pod)) continue
      const neu = copyPod(pod, "")
      let target = schedule(neu, cluster, cluster.order.filter((n) => !deleteDrain.includes(n)))
      if (target === null) {
        const g = groupOf[node] ?? ""
        res.added[g] = (res.added[g] ?? 0) + 1
        const [tmpl, daemons] = templates.get(g)!
        const name = `(new ${g} ${res.added[g]})`
        cluster.addNode(copyNode(inZone(tmpl, launchZone(cluster, groupOf, g, groups[g]), ids), name))
        groupOf[name] = g
        fresh.add(name)
        for (const d of daemons) cluster.place(copyPod(d, name), name)
        target = name
      }
      cluster.place(neu, target)
    }
    for (const node of deleteDrain) {
      cluster.removeNode(node)
      res.removed.push(node)
      if (risky.has(node)) res.risky.push(node)
    }
    for (const [node, t] of [...cluster.extraTaints]) {
      if (t.length === 1 && t[0] === SOFT_TAINT) cluster.extraTaints.delete(node)
    }
  }

  const atMin = new Set(Object.values(groups).filter((g) => groupSize(g.name) <= g.min).map((g) => g.name))
  const left = budgetsNow()
  for (const node of cluster.order) {
    const g = groupOf[node]
    if (!(g in groups) || fresh.has(node) || utilisation(node) > threshold(node)) continue
    if (atMin.has(g)) { res.atMin.push(node); continue }
    if (cluster.nodes.get(node)!.scaleDownDisabled && !(lifted || lift.has(`node/${node}`))) {
      const why = `node ${node} is annotated cluster-autoscaler.kubernetes.io/scale-down-disabled`
      res.vetoed[node] = why
      res.blockers[node] = [[`node/${node}`, why]]
      continue
    }
    const found = new Map<string, string>()
    const movers: KPod[] = []
    for (const pod of cluster.on.get(node)!) {
      const [v, why] = verdict(pod, left)
      if (v === "block") { if (!found.has(subjectOf(pod))) found.set(subjectOf(pod), why) }
      else if (v === "move") movers.push(pod)
    }
    if (found.size) {
      res.vetoed[node] = [...found.values()][0]
      res.blockers[node] = [...found.entries()]
      continue
    }
    const saved = cursor
    const savedHints = new Map(hintsCur)
    if (!findPlace(node, movers, cluster.fork(), new Set())) res.notEvacuable.push(node)
    cursor = saved
    hintsCur = savedHints
  }
  return res
}
