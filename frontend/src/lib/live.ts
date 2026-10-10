/** The exact replay in the console: the live cluster from the export, an option's
 *  edits applied the way they happen, then Cluster Autoscaler v1.35.0 -- the port of
 *  careplay.state_from_kube / migrate / estimate and realistic.kube_same_type /
 *  kube_new_type. */

import { replay, nodesIn, type Budget, type Group, type Result, type Settings, inZone, launchZone, zoneIds, zoneOf } from "@/lib/careplay"
import {
  Cluster, fits, isDs, isMirror, isBare, schedule, selectorMatches,
  type KNode, type KPod, type PodSpec, type Selector,
} from "@/lib/kscheduler"

export interface LiveContainer { name: string; cpu: number | null; mem: number | null; gpu: number; eph: number; sidecar?: boolean }
export interface LivePod extends Omit<KPod, "spec" | "node" | "labels"> {
  node: string
  s: number            // index into specs
  l: number            // index into labels
  w: string            // workload name (logical), for request edits
  containers: LiveContainer[]
  init: LiveContainer[]
  overhead: { cpu: number; mem: number }
}
export interface LivePdb {
  ns: string; name: string; selector: Selector | null
  minAvailable: string | null; maxUnavailable: string | null
  disruptionsAllowed: number | null; currentHealthy: number | null; expectedPods: number | null
}
export interface LiveModel {
  now: number | null
  settings: Settings
  groups: Record<string, Group>
  nodes: KNode[]
  specs: PodSpec[]
  labels: Record<string, string>[]
  pods: LivePod[]
  pdbs: LivePdb[]
  namespaces: Record<string, Record<string, string>>
  /** Nodes per group after CA does what it would anyway, nothing changed. */
  baseline: Record<string, number>
}

/** (namespace, workload, container) -> [cpu millicores | null, memory bytes | null]. */
export type Requests = Record<string, [number | null, number | null]>
export const reqKey = (ns: string, workload: string, container: string) => `${ns}\u0000${workload}\u0000${container}`

const DEFAULT_MILLI_CPU = 100
const DEFAULT_MEMORY = 200 * 1024 * 1024

export function podRequests(containers: LiveContainer[], init: LiveContainer[], overhead: { cpu: number; mem: number },
                            nonzero: boolean): [number, number, number, number] {
  const req = (c: LiveContainer): number[] => {
    let cpu = c.cpu; let mem = c.mem
    if (nonzero) { cpu = cpu ?? DEFAULT_MILLI_CPU; mem = mem ?? DEFAULT_MEMORY }
    return [cpu ?? 0, mem ?? 0, c.gpu ?? 0, c.eph ?? 0]
  }
  let total = [0, 0, 0, 0]
  for (const c of containers) { const r = req(c); total = total.map((a, i) => a + r[i]) }
  let sidecars = [0, 0, 0, 0]
  let initMax = [0, 0, 0, 0]
  for (const c of init) {
    let r = req(c)
    if (c.sidecar) {
      total = total.map((a, i) => a + r[i])
      sidecars = sidecars.map((a, i) => a + r[i])
      r = [...sidecars]
    } else {
      r = r.map((a, i) => a + sidecars[i])
    }
    initMax = initMax.map((a, i) => Math.max(a, r[i]))
  }
  total = total.map((a, i) => Math.max(a, initMax[i]))
  total[0] += overhead?.cpu ?? 0
  total[1] += overhead?.mem ?? 0
  return [total[0], total[1], total[2], total[3]]
}

function toPod(m: LiveModel, p: LivePod, requests?: Requests): KPod {
  let containers = p.containers
  if (requests) {
    containers = containers.map((c) => {
      const r = requests[reqKey(p.ns, p.w, c.name)]
      if (!r) return c
      return { ...c, cpu: r[0] ?? c.cpu, mem: r[1] ?? c.mem }
    })
  }
  const [cpu, mem, gpu, eph] = podRequests(containers, p.init, p.overhead, false)
  const [nzCpu, nzMem] = podRequests(containers, p.init, p.overhead, true)
  return {
    ns: p.ns, name: p.name, node: p.node, spec: m.specs[p.s], ownerKind: p.ownerKind, ownerName: p.ownerName,
    wkey: p.wkey, labels: m.labels[p.l], created: p.created, deleting: p.deleting, grace: p.grace,
    priority: p.priority, safeToEvict: p.safeToEvict, localVolumes: p.localVolumes,
    cpu, mem, gpu, eph, nzCpu, nzMem, images: p.images, ports: p.ports, csi: p.csi, pvTerms: p.pvTerms,
  }
}

function desiredHealthy(b: LivePdb, expected: number): number | null {
  const scaled = (v: string): number | null => {
    const t = String(v).trim()
    const n = t.endsWith("%") ? Math.ceil((expected * Number(t.slice(0, -1))) / 100) : Math.trunc(Number(t))
    return Number.isFinite(n) ? n : null
  }
  if (b.maxUnavailable !== null && b.maxUnavailable !== undefined) {
    const mu = scaled(b.maxUnavailable)
    return mu === null ? null : Math.max(0, expected - mu)
  }
  if (b.minAvailable !== null && b.minAvailable !== undefined) return scaled(b.minAvailable)
  return null
}

function budgetsFor(m: LiveModel, pods: KPod[]): Budget[] {
  return m.pdbs.map((b) => {
    const now = pods.filter((p) => p.ns === b.ns && selectorMatches(b.selector, p.labels)).length
    let allowed: number | null
    if (b.disruptionsAllowed !== null && b.expectedPods !== null && b.currentHealthy !== null && now === b.expectedPods) {
      allowed = b.disruptionsAllowed
    } else {
      const healthy = (b.currentHealthy ?? now) - ((b.expectedPods || now) - now)
      const desired = desiredHealthy(b, now)
      allowed = desired === null ? null : Math.max(0, healthy - desired)
    }
    return { ns: b.ns, name: b.name, selector: b.selector, allowed }
  })
}

export interface LiveState {
  cluster: Cluster; groupOf: Record<string, string>; groups: Record<string, Group>
  budgets: Budget[]; unplaceable: KPod[]; added: Record<string, number>
}

const withName = (n: KNode, name: string): KNode =>
  ({ ...n, name, images: {}, labels: { ...n.labels, "kubernetes.io/hostname": name } })

/** careplay.state_from_kube: HPA floors, then request rollouts. */
export function stateFrom(m: LiveModel, requests: Requests | null, keep: Record<string, number> | null): LiveState {
  const base = m.pods.filter((p) => p.node).map((p) => toPod(m, p))
  const cl = new Cluster(m.nodes, base, m.namespaces)
  const ids = zoneIds(cl)
  const groupOf: Record<string, string> = {}
  for (const n of m.nodes) if (n.group) groupOf[n.name] = n.group
  // Zones: the ASG's when exported, else where the group's nodes stand.
  const nodeZones: Record<string, string[]> = {}
  for (const n of m.nodes) {
    const z = zoneOf(n)
    if (n.group && z && !(nodeZones[n.group] ??= []).includes(z)) nodeZones[n.group].push(z)
  }
  const groups: Record<string, Group> = Object.fromEntries(Object.entries(m.groups).map(([k, g]) =>
    [k, { ...g, zones: g.zones?.length ? [...g.zones] : [...(nodeZones[k] ?? [])].sort() }]))
  for (const [g, zs] of Object.entries(nodeZones)) groups[g] ??= { name: g, min: 0, max: 0, options: {}, zones: [...zs].sort() }
  const st: LiveState = { cluster: cl, groupOf, groups, budgets: [], unplaceable: [], added: {} }
  if (keep && Object.keys(keep).length) {
    const byWl = new Map<string, KPod[]>()
    for (const name of cl.order) for (const p of cl.on.get(name)!) {
      if (p.wkey in keep && !isDs(p)) {
        if (!byWl.has(p.wkey)) byWl.set(p.wkey, [])
        byWl.get(p.wkey)!.push(p)
      }
    }
    for (const wkey of [...byWl.keys()].sort()) {
      const mine = byWl.get(wkey)!
      const surplus = Math.max(0, mine.length - Math.max(0, keep[wkey]))
      for (let t = 0; t < surplus; t++) {
        const count = new Map<string, number>()
        for (const p of mine) count.set(p.node, (count.get(p.node) ?? 0) + 1)
        let victim = mine[0]
        for (const p of mine) {
          const a = count.get(p.node)!; const b = count.get(victim.node)!
          if (a > b || (a === b && p.name > victim.name)) victim = p
        }
        mine.splice(mine.indexOf(victim), 1)
        cl.unplace(victim)
      }
    }
  }
  if (requests && Object.keys(requests).length) {
    const updated = new Map(m.pods.filter((p) => p.node).map((p) => [`${p.ns}/${p.name}`, toPod(m, p, requests)]))
    const changed = [...new Set(base.filter((p) => {
      const q = updated.get(`${p.ns}/${p.name}`)
      return q && (q.cpu !== p.cpu || q.mem !== p.mem)
    }).map((p) => p.wkey))].sort()
    const templates = new Map<string, KNode>()
    for (const n of cl.order) templates.set(groupOf[n], cl.nodes.get(n)!)
    const daemonsOf = new Map<string, KPod[]>()
    for (const n of [...cl.order].reverse()) daemonsOf.set(groupOf[n], cl.on.get(n)!.filter(isDs))
    for (const wkey of changed) {
      const pods: KPod[] = []
      for (const n of cl.order) for (const p of cl.on.get(n)!) if (p.wkey === wkey) pods.push(p)
      pods.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))
      for (const old of pods) {
        const neu = updated.get(`${old.ns}/${old.name}`)
        if (!neu) continue
        if (isDs(old) || ["Job", "Node", ""].includes(old.ownerKind)) {
          const at = old.node
          cl.unplace(old)
          cl.place({ ...neu, node: at }, at)
          continue
        }
        const surge = old.ownerKind === "ReplicaSet"
        if (!surge) cl.unplace(old)
        const fresh: KPod = { ...neu, node: "" }
        let target = schedule(fresh, cl)
        if (target === null) {
          const g = groupOf[old.node] ?? ""
          const tmpl = templates.get(g)
          const ds = daemonsOf.get(g) ?? []
          const dsCpu = ds.reduce((a, d) => a + d.cpu, 0)
          const dsMem = ds.reduce((a, d) => a + d.mem, 0)
          if (!tmpl || fresh.cpu > tmpl.cpu - dsCpu || fresh.mem > tmpl.mem - dsMem) {
            st.unplaceable.push(fresh)
            if (surge) cl.unplace(old)
            continue
          }
          st.added[g] = (st.added[g] ?? 0) + 1
          const name = `(scale-up ${g} ${st.added[g]})`
          cl.addNode(withName(inZone(tmpl, launchZone(cl, groupOf, g, st.groups[g]), ids), name))
          groupOf[name] = g
          for (const d of ds) cl.place({ ...d, node: name }, name)
          target = name
        }
        if (surge) cl.unplace(old)
        cl.place(fresh, target)
      }
    }
  }
  const all: KPod[] = []
  for (const n of cl.order) all.push(...cl.on.get(n)!)
  st.budgets = budgetsFor(m, all)
  return st
}

export function forkState(s: LiveState): LiveState {
  return {
    cluster: s.cluster.fork(), groupOf: { ...s.groupOf },
    groups: Object.fromEntries(Object.entries(s.groups).map(([k, g]) => [k, { ...g }])),
    budgets: s.budgets, unplaceable: [...s.unplaceable], added: { ...s.added },
  }
}

export function replayState(m: LiveModel, s: LiveState, lift: Set<string> = new Set(), lifted = false): Result {
  const res = replay(s.cluster, m.settings, s.groupOf, s.groups, s.budgets, m.now, lift, lifted)
  for (const [g, n] of Object.entries(s.added)) res.added[g] = (res.added[g] ?? 0) + n
  return res
}

/** careplay.estimate: CA's binpacking estimator. */
export function estimate(pending: KPod[], template: KNode, daemons: KPod[], cluster: Cluster, maxNew: number,
                         group: string): number {
  const groups = new Map<string, KPod[]>()
  const specId = new Map<object, number>()
  for (const p of pending) {
    if (!specId.has(p.spec)) specId.set(p.spec, specId.size)
    const key = JSON.stringify([p.ns, p.ownerKind, p.ownerName, specId.get(p.spec), p.cpu, p.mem, p.gpu, p.ports])
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key)!.push(p)
  }
  const podScore = (ps: KPod[]) => (template.cpu ? ps[0].cpu / template.cpu : 0) + (template.mem ? ps[0].mem / template.mem : 0)
  const ordered = [...groups.values()].sort((a, b) => podScore(b) - podScore(a) || (a[0].name < b[0].name ? -1 : a[0].name > b[0].name ? 1 : 0))
  const sim = cluster.fork()
  const newNames: string[] = []
  const withPods = new Set<string>()
  let last = ""
  let cursor = 0
  let available = true
  const add = () => {
    const name = `(estimate ${group} ${newNames.length + 1})`
    sim.addNode(withName(template, name))
    for (const d of daemons) sim.on.get(name)!.push({ ...d, node: name })
    newNames.push(name)
    return name
  }
  for (const ps of ordered) {
    const rest = [...ps]
    while (rest.length && newNames.length) {
      const pod = rest[0]
      let hit: string | null = null
      for (let i = 0; i < newNames.length; i++) {
        const n = newNames[(cursor + i) % newNames.length]
        if (fits(pod, n, sim)) { hit = n; cursor = (cursor + i + 1) % newNames.length; break }
      }
      if (hit === null) break
      sim.on.get(hit)!.push(pod)
      withPods.add(hit)
      rest.shift()
    }
    if (!available) continue
    for (const pod of rest) {
      if (last && fits(pod, last, sim)) { sim.on.get(last)!.push(pod); withPods.add(last); continue }
      if (last && !withPods.has(last)) break
      if (newNames.length >= maxNew) { available = false; break }
      last = add()
      if (!fits(pod, last, sim)) break
      sim.on.get(last)!.push(pod)
      withPods.add(last)
    }
  }
  return withPods.size
}

/** careplay.migrate: drain the group, scheduler first, CA scale-up for the rest. */
export function migrate(s: LiveState, group: string, template: KNode, minSize: number, maxSize = 1000) {
  const cl = s.cluster
  const ids = zoneIds(cl)  // before the old nodes go: one may be a zone's only node
  const old = cl.order.filter((n) => s.groupOf[n] === group)
  const daemons = old.length ? cl.on.get(old[0])!.filter(isDs).map((p) => ({ ...p })) : []
  let pending: KPod[] = []
  for (const n of old) {
    for (const p of cl.on.get(n)!) if (!(isDs(p) || isMirror(p) || isBare(p))) pending.push({ ...p, node: "" })
    cl.removeNode(n)
    delete s.groupOf[n]
  }
  s.groups[group] = { ...(s.groups[group] ?? { name: group, options: {}, max: 0 }), min: minSize, max: maxSize }
  let added = 0
  /** One instance, where the ASG launches it: its least-populated zone. */
  const addNode = () => {
    added += 1
    const name = `(${template.name} ${added})`
    cl.addNode(withName(inZone(template, launchZone(cl, s.groupOf, group, s.groups[group]), ids), name))
    s.groupOf[name] = group
    for (const d of daemons) cl.place({ ...d, node: name }, name)
    return name
  }
  /** What CA believes a new node looks like: a running node of the group,
   *  so one zone -- a pod pinned elsewhere does not fit it and triggers nothing. */
  const caTemplate = () => {
    const mine = cl.order.filter((n) => s.groupOf[n] === group)
    return mine.length ? inZone(template, zoneOf(cl.nodes.get(mine[0])!), ids) : template
  }
  for (let i = 0; i < minSize; i++) addNode()
  for (let round = 0; pending.length && round < 50; round++) {
    const still: KPod[] = []
    for (const p of pending) {
      const t = schedule(p, cl)
      if (t === null) still.push(p)
      else cl.place(p, t)
    }
    pending = still
    if (!pending.length) break
    const need = estimate(pending, caTemplate(), daemons, cl, maxSize - added, group)
    if (need <= 0) break
    for (let i = 0; i < need; i++) addNode()
  }
  s.unplaceable.push(...pending)
}

export interface LiveOutcome {
  nodes: number
  added: number
  unplaceable: KPod[]
  vetoed: Record<string, string>
  blockers: Record<string, [string, string][]>
  placement: Map<string, KPod[]>
}

function outcomeFor(res: Result, st: LiveState, group: string, nodes: number): LiveOutcome {
  const names = nodesIn(res, group)
  const mine = new Set(names)
  return {
    nodes, added: res.added[group] ?? 0, unplaceable: st.unplaceable,
    vetoed: Object.fromEntries(Object.entries(res.vetoed).filter(([n]) => mine.has(n))),
    blockers: Object.fromEntries(Object.entries(res.blockers).filter(([n]) => mine.has(n))),
    placement: new Map(names.map((n) => [n, res.cluster.on.get(n)!])),
  }
}

const stateCache = new WeakMap<LiveModel, Map<string, LiveState>>()

function cachedState(m: LiveModel, requests: Requests, keep: Record<string, number>): LiveState {
  let c = stateCache.get(m)
  if (!c) { c = new Map(); stateCache.set(m, c) }
  const key = JSON.stringify([Object.entries(requests).sort(), Object.entries(keep).sort()])
  let s = c.get(key)
  if (!s) {
    s = stateFrom(m, Object.keys(requests).length ? requests : null, Object.keys(keep).length ? keep : null)
    c.set(key, s)
  }
  return s
}

/** realistic.kube_same_type. */
export function liveSameType(m: LiveModel, group: string, requests: Requests, keep: Record<string, number>,
                             minNodes: number, lifted: boolean | Set<string> = false): LiveOutcome | null {
  const base = cachedState(m, requests, keep)
  if (!Object.values(base.groupOf).includes(group)) return null
  const st = forkState(base)
  st.groups[group] = { ...(st.groups[group] ?? { name: group, max: 0, options: {} }), min: minNodes }
  const res = replayState(m, st, lifted instanceof Set ? lifted : new Set(), lifted === true)
  const today = base.cluster.order.filter((n) => base.groupOf[n] === group).length
  const left = nodesIn(res, group).length
  const background = Math.max(0, today - (m.baseline[group] ?? today))
  return outcomeFor(res, st, group, Math.max(left + background, minNodes, 1))
}

/** realistic.kube_new_type. */
/** realistic._ephemeral_after: the new node's ephemeral storage. On a type
 *  with an instance store the kubelet lives on it (when today's nodes do); a
 *  type with none puts it on the root volume. */
function ephemeralAfter(tmplEph: number, store?: { now: number; next: number; rootGb: number }): number {
  if (!store) return tmplEph
  const storeNow = store.now * 1e9
  const onStore = storeNow > 0 && tmplEph > 0.5 * storeNow
  if (!onStore) return tmplEph
  if (store.next) return Math.round(tmplEph * (store.next * 1e9 / storeNow))
  return store.rootGb ? Math.round(store.rootGb * 1e9 * 0.9) : tmplEph
}

export function liveNewType(m: LiveModel, group: string, requests: Requests, keep: Record<string, number>,
                            alloc: [number, number], maxPods: number, minNodes: number,
                            inst: { name: string; gpus?: number; store?: { now: number; next: number; rootGb: number } }
                            ): LiveOutcome | null {
  const base = cachedState(m, requests, keep)
  const old = base.cluster.order.filter((n) => base.groupOf[n] === group)
  if (!old.length) return null
  const st = forkState(base)
  const tmpl = st.cluster.nodes.get(old[0])!
  const template: KNode = {
    ...tmpl, name: inst.name,
    labels: { ...tmpl.labels, "node.kubernetes.io/instance-type": inst.name, "beta.kubernetes.io/instance-type": inst.name },
    cpu: Math.round(alloc[0] * 1000), mem: Math.round(alloc[1]), pods: maxPods || tmpl.pods,
    gpu: inst.gpus ?? 0, eph: ephemeralAfter(tmpl.eph, inst.store),
    images: {}, unschedulable: false, scaleDownDisabled: false,
  }
  migrate(st, group, template, minNodes)
  const res = replayState(m, st)
  return outcomeFor(res, st, group, Math.max(nodesIn(res, group).length, minNodes, 1))
}


/** Give every node group's model its cluster's live objects (exported once per
 *  cluster). The loader calls this; so does anything else reading fleet.json. */
export function attachLive(fleet: { periods?: { clusters?: { live?: unknown; nodegroups?: unknown[] }[] }[] }) {
  for (const period of fleet.periods ?? []) {
    for (const c of period.clusters ?? []) {
      if (!c.live) continue
      for (const g of c.nodegroups ?? []) {
        const model = (g as { sim?: { model?: Record<string, unknown> } | null }).sim?.model
        if (model) model.live = c.live
      }
    }
  }
  return fleet
}
