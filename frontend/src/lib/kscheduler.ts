/** kube-scheduler v1.34 filters and scores -- the port of rightsizer/engine/kscheduler.py.
 *
 *  Line for line with the engine: the console replays Cluster Autoscaler over the
 *  same live objects, and the two must agree on every node a pod can go to and the
 *  one it goes to. Integer arithmetic is kept where Go uses int64. */

export interface Selector { matchLabels: Record<string, string>; matchExpressions: Req[] }
export interface Req { key: string; operator: string; values: string[] }
export interface NodeTerm { matchExpressions: Req[]; matchFields: Req[] }
export interface PodTerm {
  labelSelector: Selector | null; namespaces: string[]; namespaceSelector: Selector | null; topologyKey: string
}
export interface Toleration { key: string; operator: string; value: string; effect: string }
export interface Taint { key: string; value: string; effect: string }
export interface Spread {
  maxSkew: number; topologyKey: string; whenUnsatisfiable: string; labelSelector: Selector | null
  minDomains?: number | null; nodeAffinityPolicy?: string; nodeTaintsPolicy?: string; matchLabelKeys?: string[]
}

export interface PodSpec {
  nodeSelector: Record<string, string>
  nodeRequired: NodeTerm[]
  nodePreferred: { weight: number; term: NodeTerm }[]
  tolerations: Toleration[]
  podAffinity: PodTerm[]
  podAnti: PodTerm[]
  podAffinityPref: { weight: number; term: PodTerm }[]
  podAntiPref: { weight: number; term: PodTerm }[]
  spread: Spread[]
  defaultSelector: Selector | null
}

export interface KPod {
  ns: string; name: string; node: string; spec: PodSpec
  ownerKind: string; ownerName: string; wkey: string
  labels: Record<string, string>
  created: number | null; deleting: number | null; grace: number | null; priority: number | null
  safeToEvict: string | null
  localVolumes: string[]
  cpu: number; mem: number; gpu: number; eph: number
  nzCpu: number; nzMem: number
  images: string[]
  ports: [number, string, string][]
  csi: [string, string][]
  pvTerms: NodeTerm[][]
}

export interface KNode {
  name: string; labels: Record<string, string>; taints: Taint[]
  cpu: number; mem: number; pods: number; gpu: number; eph: number
  unschedulable: boolean; ready: boolean; scaleDownDisabled: boolean; group: string
  images: Record<string, number>
  csiLimits: Record<string, number>
}

export const MAX_NODE_SCORE = 100
export const HOSTNAME = "kubernetes.io/hostname"
export const ZONE = "topology.kubernetes.io/zone"
const UNSCHEDULABLE_TAINT: Taint = { key: "node.kubernetes.io/unschedulable", value: "", effect: "NoSchedule" }
export const SOFT_TAINT: Taint = { key: "DeletionCandidateOfClusterAutoscaler", value: "", effect: "PreferNoSchedule" }
export const DELETE_TAINT: Taint = { key: "ToBeDeletedByClusterAutoscaler", value: "", effect: "NoSchedule" }
const SYSTEM_SPREAD = [{ maxSkew: 3, topologyKey: HOSTNAME }, { maxSkew: 5, topologyKey: ZONE }]
const MIN_IMAGE = 23 * 1024 * 1024
const MAX_IMAGE_PER_CONTAINER = 1000 * 1024 * 1024
const W = { taint: 3, nodeAffinity: 2, spread: 2, interPod: 2, fit: 1, balanced: 1, image: 1 }

export const isDs = (p: KPod) => p.ownerKind === "DaemonSet"
export const isMirror = (p: KPod) => p.ownerKind === "Node"
export const isBare = (p: KPod) => !p.ownerKind || p.ownerKind === "Unknown"
export const subjectOf = (p: KPod) => (isBare(p) || !p.wkey ? `${p.ns}/${p.name}` : p.wkey)

// -- selectors ----------------------------------------------------------------------

function requirement(e: Req, labels: Record<string, string>): boolean {
  const has = e.key in labels
  switch (e.operator) {
    case "In": return has && e.values.includes(labels[e.key])
    case "NotIn": return !has || !e.values.includes(labels[e.key])
    case "Exists": return has
    case "DoesNotExist": return !has
    case "Gt": case "Lt": {
      if (!has || !e.values.length) return false
      const a = Number.parseInt(labels[e.key], 10)
      const b = Number.parseInt(e.values[0], 10)
      if (Number.isNaN(a) || Number.isNaN(b)) return false
      return e.operator === "Gt" ? a > b : a < b
    }
    default: return false
  }
}

export function selectorMatches(sel: Selector | null | undefined, labels: Record<string, string>): boolean {
  if (sel === null || sel === undefined) return false
  for (const [k, v] of Object.entries(sel.matchLabels ?? {})) if (labels[k] !== v) return false
  for (const e of sel.matchExpressions ?? []) if (!requirement(e, labels)) return false
  return true
}

export function nodeTermMatches(term: NodeTerm, node: KNode): boolean {
  const exprs = term.matchExpressions ?? []
  const fields = term.matchFields ?? []
  if (!exprs.length && !fields.length) return false
  if (!exprs.every((e) => requirement(e, node.labels))) return false
  return fields.every((f) => requirement(f, { "metadata.name": node.name }))
}

export function requiredNodeAffinity(spec: PodSpec, node: KNode): boolean {
  for (const [k, v] of Object.entries(spec.nodeSelector)) if (node.labels[k] !== v) return false
  if (spec.nodeRequired.length) return spec.nodeRequired.some((t) => nodeTermMatches(t, node))
  return true
}

export function tolerates(tol: Toleration, taint: Taint): boolean {
  if (tol.effect && tol.effect !== taint.effect) return false
  if (tol.key && tol.key !== taint.key) return false
  const op = tol.operator || "Equal"
  if (op === "Exists") return true
  if (op === "Equal") return (tol.value || "") === (taint.value || "")
  return false
}

function untolerated(taints: Taint[], tols: Toleration[], effects: string[]): boolean {
  return taints.some((t) => effects.includes(t.effect) && !tols.some((o) => tolerates(o, t)))
}

// -- the cluster -------------------------------------------------------------------------

export class Cluster {
  nodes: Map<string, KNode>
  order: string[]
  on: Map<string, KPod[]>
  namespaces: Record<string, Record<string, string>>
  extraTaints: Map<string, Taint[]>

  constructor(nodes: KNode[], pods: KPod[], namespaces: Record<string, Record<string, string>> = {}) {
    this.nodes = new Map(nodes.map((n) => [n.name, n]))
    this.order = [...this.nodes.keys()].sort(strCmp)
    this.on = new Map(this.order.map((n) => [n, []]))
    for (const p of pods) this.on.get(p.node)?.push(p)
    this.namespaces = namespaces
    this.extraTaints = new Map()
  }

  requested(node: string): [number, number, number, number] {
    let c = 0; let m = 0; let g = 0; let e = 0
    for (const p of this.on.get(node)!) { c += p.cpu; m += p.mem; g += p.gpu; e += p.eph }
    return [c, m, g, e]
  }

  nonzeroRequested(node: string): [number, number] {
    let c = 0; let m = 0
    for (const p of this.on.get(node)!) { c += p.nzCpu; m += p.nzMem }
    return [c, m]
  }

  taints(node: string): Taint[] {
    return [...this.nodes.get(node)!.taints, ...(this.extraTaints.get(node) ?? [])]
  }

  place(pod: KPod, node: string) { pod.node = node; this.on.get(node)!.push(pod) }

  unplace(pod: KPod) {
    const l = this.on.get(pod.node)!
    l.splice(l.indexOf(pod), 1)
  }

  removeNode(node: string): KPod[] {
    const pods = this.on.get(node) ?? []
    this.on.delete(node)
    this.nodes.delete(node)
    this.order.splice(this.order.indexOf(node), 1)
    this.extraTaints.delete(node)
    return pods
  }

  addNode(node: KNode) {
    this.nodes.set(node.name, node)
    this.on.set(node.name, [])
    this.order = [...this.nodes.keys()].sort(strCmp)
  }

  fork(): Cluster {
    const c = Object.create(Cluster.prototype) as Cluster
    c.nodes = new Map(this.nodes)
    c.order = [...this.order]
    c.on = new Map([...this.on].map(([n, v]) => [n, [...v]]))
    c.namespaces = this.namespaces
    c.extraTaints = new Map([...this.extraTaints].map(([n, v]) => [n, [...v]]))
    return c
  }
}

export const strCmp = (a: string, b: string) => (a < b ? -1 : a > b ? 1 : 0)

// -- affinity terms -------------------------------------------------------------------------

const termCache = new WeakMap<object, WeakMap<object, Map<string, boolean>>>()

export function termMatches(term: PodTerm, ownerNs: string, target: KPod,
                            namespaces: Record<string, Record<string, string>>): boolean {
  let byLabels = termCache.get(term)
  if (!byLabels) { byLabels = new WeakMap(); termCache.set(term, byLabels) }
  let byNs = byLabels.get(target.labels)
  if (!byNs) { byNs = new Map(); byLabels.set(target.labels, byNs) }
  const k = `${ownerNs}\u0000${target.ns}`
  const hit = byNs.get(k)
  if (hit !== undefined) return hit
  const named = !term.namespaces.length && term.namespaceSelector === null ? [ownerNs] : term.namespaces
  const inNs = named.includes(target.ns) || selectorMatches(term.namespaceSelector, namespaces[target.ns] ?? {})
  const r = inNs && selectorMatches(term.labelSelector, target.labels)
  byNs.set(k, r)
  return r
}

// -- filters ---------------------------------------------------------------------------------

export function staticOk(pod: KPod, node: KNode): boolean {
  if (node.unschedulable && !pod.spec.tolerations.some((t) => tolerates(t, UNSCHEDULABLE_TAINT))) return false
  if (untolerated(node.taints, pod.spec.tolerations, ["NoSchedule", "NoExecute"])) return false
  if (!requiredNodeAffinity(pod.spec, node)) return false
  for (const terms of pod.pvTerms) {
    if (terms.length && !terms.some((t) => nodeTermMatches(t, node))) return false
  }
  return true
}

export function fits(pod: KPod, node: string, cluster: Cluster, statik?: boolean): boolean {
  const n = cluster.nodes.get(node)!
  const ok = statik === undefined ? staticOk(pod, n) : statik
  if (!ok) return false
  const extra = cluster.extraTaints.get(node)
  if (extra && untolerated(extra, pod.spec.tolerations, ["NoSchedule", "NoExecute"])) return false
  const here = cluster.on.get(node)!
  if (here.length + 1 > n.pods) return false
  if (pod.cpu || pod.mem || pod.gpu || pod.eph) {
    const [c, m, g, e] = cluster.requested(node)
    if (pod.cpu > 0 && pod.cpu > n.cpu - c) return false
    if (pod.mem > 0 && pod.mem > n.mem - m) return false
    if (pod.eph > 0 && pod.eph > n.eph - e) return false
    if (pod.gpu > 0 && pod.gpu > n.gpu - g) return false
  }
  if (pod.ports.length) {
    for (const [port, proto, ip] of pod.ports) {
      for (const p of here) {
        for (const [up, uproto, uip] of p.ports) {
          if (port === up && proto === uproto && (ip === uip || ip === "0.0.0.0" || uip === "0.0.0.0")) return false
        }
      }
    }
  }
  if (pod.csi.length) {
    const attached = new Map<string, Set<string>>()
    for (const p of here) for (const [d, h] of p.csi) {
      if (!attached.has(d)) attached.set(d, new Set())
      attached.get(d)!.add(h)
    }
    for (const drv of new Set(pod.csi.map(([d]) => d))) {
      const limit = n.csiLimits[drv]
      if (limit === undefined || limit === null) continue
      const have = attached.get(drv) ?? new Set<string>()
      const fresh = new Set(pod.csi.filter(([d, h]) => d === drv && !have.has(h)).map(([, h]) => h))
      if (fresh.size && have.size + fresh.size > limit) return false
    }
  }
  if (!podAffinityOk(pod, n, cluster)) return false
  const hard = pod.spec.spread.filter((c) => c.whenUnsatisfiable === "DoNotSchedule")
  return !(hard.length && !spreadOk(pod, n, cluster, hard))
}

function podAffinityOk(pod: KPod, node: KNode, cluster: Cluster): boolean {
  const ns = cluster.namespaces
  for (const [otherName, pods] of cluster.on) {
    const other = cluster.nodes.get(otherName)!
    for (const e of pods) {
      for (const t of e.spec.podAnti) {
        const key = t.topologyKey
        if (key in node.labels && other.labels[key] === node.labels[key] && termMatches(t, e.ns, pod, ns)) return false
      }
    }
  }
  for (const t of pod.spec.podAnti) {
    const key = t.topologyKey
    if (!(key in node.labels)) continue
    for (const [otherName, pods] of cluster.on) {
      if (cluster.nodes.get(otherName)!.labels[key] !== node.labels[key]) continue
      if (pods.some((e) => termMatches(t, pod.ns, e, ns))) return false
    }
  }
  for (const t of pod.spec.podAffinity) {
    const key = t.topologyKey
    if (!(key in node.labels)) return false
    let found = false
    for (const [o, pods] of cluster.on) {
      if (cluster.nodes.get(o)!.labels[key] !== node.labels[key]) continue
      if (pods.some((e) => termMatches(t, pod.ns, e, ns))) { found = true; break }
    }
    if (!found) {
      let anywhere = false
      for (const pods of cluster.on.values()) if (pods.some((e) => termMatches(t, pod.ns, e, ns))) { anywhere = true; break }
      if (anywhere || !termMatches(t, pod.ns, pod, ns)) return false
    }
  }
  return true
}

function spreadSelector(c: Spread, pod: KPod): Selector | null {
  let sel = c.labelSelector
  const keys = c.matchLabelKeys ?? []
  if (keys.length && sel !== null) {
    const extra: Record<string, string> = {}
    for (const k of keys) if (k in pod.labels) extra[k] = pod.labels[k]
    if (Object.keys(extra).length) {
      sel = { matchLabels: { ...(sel.matchLabels ?? {}), ...extra }, matchExpressions: [...(sel.matchExpressions ?? [])] }
    }
  }
  return sel
}

function count(pods: KPod[], sel: Selector | null, ns: string): number {
  if (sel === null || !(Object.keys(sel.matchLabels ?? {}).length || (sel.matchExpressions ?? []).length)) return 0
  let n = 0
  for (const p of pods) if (p.deleting === null && p.ns === ns && selectorMatches(sel, p.labels)) n += 1
  return n
}

function included(c: { nodeAffinityPolicy?: string; nodeTaintsPolicy?: string }, pod: KPod, node: KNode): boolean {
  if ((c.nodeAffinityPolicy || "Honor") === "Honor" && !requiredNodeAffinity(pod.spec, node)) return false
  return !((c.nodeTaintsPolicy || "Ignore") === "Honor"
    && untolerated(node.taints, pod.spec.tolerations, ["NoSchedule", "NoExecute"]))
}

function spreadOk(pod: KPod, node: KNode, cluster: Cluster, hard: Spread[]): boolean {
  for (const c of hard) {
    const key = c.topologyKey
    if (!(key in node.labels)) return false
    const sel = spreadSelector(c, pod)
    const counts = new Map<string, number>()
    for (const name of cluster.order) {
      const n = cluster.nodes.get(name)!
      if (!(key in n.labels) || !included(c, pod, n)) continue
      counts.set(n.labels[key], (counts.get(n.labels[key]) ?? 0) + count(cluster.on.get(name)!, sel, pod.ns))
    }
    let minMatch = counts.size ? Math.min(...counts.values()) : 0
    if (counts.size < (c.minDomains || 1)) minMatch = 0
    const self = selectorMatches(sel, pod.labels) ? 1 : 0
    if ((counts.get(node.labels[key]) ?? 0) + self - minMatch > (c.maxSkew ?? 1)) return false
  }
  return true
}

// -- scores --------------------------------------------------------------------------------

const idiv = (a: number, b: number) => Math.trunc(a / b)

function least(requested: number, capacity: number): number {
  if (capacity === 0 || requested > capacity) return 0
  return idiv((capacity - requested) * MAX_NODE_SCORE, capacity)
}

function fitScore(pod: KPod, node: KNode, cluster: Cluster): number {
  const [c, m] = cluster.nonzeroRequested(node.name)
  let total = 0; let weight = 0
  for (const [req, cap] of [[c + pod.nzCpu, node.cpu], [m + pod.nzMem, node.mem]]) {
    if (cap === 0) continue
    total += least(req, cap)
    weight += 1
  }
  return weight ? idiv(total, weight) : 0
}

function balancedScore(pod: KPod, node: KNode, cluster: Cluster): number {
  const [c, m] = cluster.requested(node.name)
  const fractions: number[] = []
  for (const [req, cap] of [[c + pod.cpu, node.cpu], [m + pod.mem, node.mem]]) {
    if (cap === 0) continue
    fractions.push(Math.min(1, req / cap))
  }
  let std = 0
  if (fractions.length === 2) std = Math.abs((fractions[0] - fractions[1]) / 2)
  return Math.trunc((1 - std) * MAX_NODE_SCORE)
}

function defaultNormalize(scores: Map<string, number>, reverse: boolean): Map<string, number> {
  let top = 0
  for (const v of scores.values()) if (v > top) top = v
  const out = new Map<string, number>()
  if (top === 0) {
    for (const [k, v] of scores) out.set(k, reverse ? MAX_NODE_SCORE : v)
    return out
  }
  for (const [k, v] of scores) {
    const s = idiv(MAX_NODE_SCORE * v, top)
    out.set(k, reverse ? MAX_NODE_SCORE - s : s)
  }
  return out
}

/** Go's math.Round: half away from zero. */
const goRound = (x: number) => (x >= 0 ? Math.floor(x + 0.5) : -Math.floor(-x + 0.5))

function spreadScores(pod: KPod, feasible: string[], cluster: Cluster): Map<string, number> | null {
  const own = pod.spec.spread.filter((c) => c.whenUnsatisfiable === "ScheduleAnyway")
  let constraints: { topologyKey: string; maxSkew: number; selector: Selector | null; nodeAffinityPolicy: string; nodeTaintsPolicy: string }[]
  let requireAll: boolean
  if (own.length) {
    constraints = own.map((c) => ({ topologyKey: c.topologyKey, maxSkew: c.maxSkew ?? 1, selector: spreadSelector(c, pod),
      nodeAffinityPolicy: c.nodeAffinityPolicy || "Honor", nodeTaintsPolicy: c.nodeTaintsPolicy || "Ignore" }))
    requireAll = true
  } else if (pod.spec.spread.length) {
    return null
  } else {
    if (pod.spec.defaultSelector === null) return null
    constraints = SYSTEM_SPREAD.map((c) => ({ topologyKey: c.topologyKey, maxSkew: c.maxSkew,
      selector: pod.spec.defaultSelector, nodeAffinityPolicy: "Honor", nodeTaintsPolicy: "Ignore" }))
    requireAll = false
  }
  const hasAll = (n: KNode) => constraints.every((c) => c.topologyKey in n.labels)
  const ignored = new Set<string>()
  const tp = constraints.map(() => new Map<string, number>())
  const size = constraints.map(() => 0)
  for (const name of feasible) {
    const n = cluster.nodes.get(name)!
    if (requireAll && !hasAll(n)) { ignored.add(name); continue }
    constraints.forEach((c, i) => {
      if (c.topologyKey === HOSTNAME) return
      const value = n.labels[c.topologyKey]
      if (!tp[i].has(value)) { tp[i].set(value, 0); size[i] += 1 }
    })
  }
  const weights = constraints.map((c, i) =>
    Math.log((c.topologyKey === HOSTNAME ? feasible.length - ignored.size : size[i]) + 2))
  for (const name of cluster.order) {
    const n = cluster.nodes.get(name)!
    if (requireAll && !hasAll(n)) continue
    constraints.forEach((c, i) => {
      if (!included(c, pod, n)) return
      const value = n.labels[c.topologyKey]
      if (!tp[i].has(value)) return
      tp[i].set(value, tp[i].get(value)! + count(cluster.on.get(name)!, c.selector, pod.ns))
    })
  }
  const raw = new Map<string, number>()
  for (const name of feasible) {
    const n = cluster.nodes.get(name)!
    if (ignored.has(name)) { raw.set(name, 0); continue }
    let score = 0
    constraints.forEach((c, i) => {
      const key = c.topologyKey
      if (!(key in n.labels)) return
      const cnt = key === HOSTNAME ? count(cluster.on.get(name)!, c.selector, pod.ns) : tp[i].get(n.labels[key])!
      score += cnt * weights[i] + (c.maxSkew - 1)
    })
    raw.set(name, goRound(score))
  }
  let lo = Infinity; let hi = 0
  for (const [k, v] of raw) {
    if (ignored.has(k)) continue
    if (v < lo) lo = v
    if (v > hi) hi = v
  }
  if (lo === Infinity) lo = 0
  const out = new Map<string, number>()
  for (const [k, v] of raw) {
    if (ignored.has(k)) out.set(k, 0)
    else if (hi === 0) out.set(k, MAX_NODE_SCORE)
    else out.set(k, idiv(MAX_NODE_SCORE * (hi + lo - v), hi))
  }
  return out
}

function interPodScores(pod: KPod, feasible: string[], cluster: Cluster): Map<string, number> | null {
  const ns = cluster.namespaces
  const hasOwn = pod.spec.podAffinityPref.length > 0 || pod.spec.podAntiPref.length > 0
  const topo = new Map<string, Map<string, number>>()
  let any = false
  const add = (key: string, value: string, w: number) => {
    if (!topo.has(key)) topo.set(key, new Map())
    const m = topo.get(key)!
    m.set(value, (m.get(value) ?? 0) + w)
    any = true
  }
  for (const name of cluster.order) {
    const node = cluster.nodes.get(name)!
    if (!Object.keys(node.labels).length) continue
    for (const e of cluster.on.get(name)!) {
      const es = e.spec
      if (!hasOwn && !(es.podAffinity.length || es.podAnti.length || es.podAffinityPref.length || es.podAntiPref.length)) continue
      for (const wt of pod.spec.podAffinityPref) {
        const t = wt.term
        if (termMatches(t, pod.ns, e, ns) && t.topologyKey in node.labels) add(t.topologyKey, node.labels[t.topologyKey], wt.weight)
      }
      for (const wt of pod.spec.podAntiPref) {
        const t = wt.term
        if (termMatches(t, pod.ns, e, ns) && t.topologyKey in node.labels) add(t.topologyKey, node.labels[t.topologyKey], -wt.weight)
      }
      for (const t of es.podAffinity) {
        if (termMatches(t, e.ns, pod, ns) && t.topologyKey in node.labels) add(t.topologyKey, node.labels[t.topologyKey], 1)
      }
      for (const wt of es.podAffinityPref) {
        const t = wt.term
        if (termMatches(t, e.ns, pod, ns) && t.topologyKey in node.labels) add(t.topologyKey, node.labels[t.topologyKey], wt.weight)
      }
      for (const wt of es.podAntiPref) {
        const t = wt.term
        if (termMatches(t, e.ns, pod, ns) && t.topologyKey in node.labels) add(t.topologyKey, node.labels[t.topologyKey], -wt.weight)
      }
    }
  }
  if (!any && !topo.size) return null
  const raw = new Map<string, number>()
  for (const name of feasible) {
    const labels = cluster.nodes.get(name)!.labels
    let s = 0
    for (const [k, vals] of topo) if (k in labels) s += vals.get(labels[k]) ?? 0
    raw.set(name, s)
  }
  if (!topo.size) return raw
  let lo = Infinity; let hi = -Infinity
  for (const v of raw.values()) { if (v < lo) lo = v; if (v > hi) hi = v }
  if (lo === Infinity) { lo = 0; hi = 0 }
  const diff = hi - lo
  const out = new Map<string, number>()
  for (const [k, v] of raw) out.set(k, diff > 0 ? Math.trunc(MAX_NODE_SCORE * ((v - lo) / diff)) : 0)
  return out
}

function imageScore(pod: KPod, node: KNode, total: number, spread: Map<string, number>): number {
  let s = 0
  for (const img of pod.images) {
    const size = node.images[img]
    if (size !== undefined) s += Math.trunc(size * ((spread.get(img) ?? 0) / total))
  }
  const hi = MAX_IMAGE_PER_CONTAINER * Math.max(pod.images.length, 1)
  s = Math.min(Math.max(s, MIN_IMAGE), hi)
  return idiv(MAX_NODE_SCORE * (s - MIN_IMAGE), hi - MIN_IMAGE)
}

export function score(pod: KPod, feasible: string[], cluster: Cluster): Map<string, number> {
  const total = new Map(feasible.map((n) => [n, 0]))
  if (!feasible.length) return total
  const bump = (n: string, v: number) => total.set(n, total.get(n)! + v)
  const prefer = pod.spec.tolerations.filter((t) => !t.effect || t.effect === "PreferNoSchedule")
  const tt = new Map(feasible.map((n) => [n, cluster.taints(n).filter((t) => t.effect === "PreferNoSchedule"
    && !prefer.some((o) => tolerates(o, t))).length]))
  for (const [n, v] of defaultNormalize(tt, true)) bump(n, v * W.taint)
  if (pod.spec.nodePreferred.length) {
    const na = new Map(feasible.map((n) => [n, pod.spec.nodePreferred
      .filter((t) => nodeTermMatches(t.term, cluster.nodes.get(n)!)).reduce((a, t) => a + t.weight, 0)]))
    for (const [n, v] of defaultNormalize(na, false)) bump(n, v * W.nodeAffinity)
  }
  const sp = spreadScores(pod, feasible, cluster)
  if (sp) for (const [n, v] of sp) bump(n, v * W.spread)
  const ip = interPodScores(pod, feasible, cluster)
  if (ip) for (const [n, v] of ip) bump(n, v * W.interPod)
  const bestEffort = pod.cpu === 0 && pod.mem === 0
  const spreadCount = new Map<string, number>()
  for (const n of cluster.nodes.values()) for (const img of Object.keys(n.images)) spreadCount.set(img, (spreadCount.get(img) ?? 0) + 1)
  const totalNodes = cluster.nodes.size
  for (const n of feasible) {
    const node = cluster.nodes.get(n)!
    bump(n, fitScore(pod, node, cluster) * W.fit)
    if (!bestEffort) bump(n, balancedScore(pod, node, cluster) * W.balanced)
    bump(n, imageScore(pod, node, totalNodes, spreadCount) * W.image)
  }
  return total
}

export function schedule(pod: KPod, cluster: Cluster, candidates?: string[],
                         statik?: (pod: KPod, node: string) => boolean): string | null {
  const names = candidates ?? cluster.order
  const feasible = names.filter((n) => fits(pod, n, cluster, statik?.(pod, n)))
  if (!feasible.length) return null
  const totals = score(pod, feasible, cluster)
  let best = -Infinity
  for (const v of totals.values()) if (v > best) best = v
  return feasible.find((n) => totals.get(n) === best) ?? null
}
