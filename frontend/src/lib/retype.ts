/** Re-price an option on an instance type of the reader's choosing.
 *
 *  The advice picks one type per route; a reader often has reasons of their
 *  own -- a family they already run, a reservation, a size they trust. This
 *  runs the same engine on the option's own changes with a different node:
 *  Cluster Autoscaler replayed for today's type, a by-request packing for any
 *  other, then the load, trend and headroom checks, and the ports of
 *  `storage.assess` and `network.assess`. So a picked type is judged exactly
 *  as the advice judges its own. */

import { realize, volumeZones, type Model } from "@/lib/realistic"
import type { NetworkCheck, NodeGroup, Scenario, StorageCheck } from "@/types"

const GIB = 1024 ** 3

export interface InstanceType {
  name: string
  vcpu: number
  memGib: number
  hourlyUsd: number
  monthlyPerNode: number
  storage: { gb: number; disks: number; type: string; nvme: string; known: boolean }
  net: { baseline: number; burst: number; source: "described" | "published" } | null
  /** Searched by the advice (the ladder), or only offered for comparison. */
  ladder?: boolean
  /** Specs guessed from the name and a price typed by the reader. */
  inferred?: boolean
  /** A size the group lists already (a Karpenter pool's rung): it can run today. */
  offered?: boolean
}

export interface InstanceChoices {
  types: InstanceType[]
  /** `snapshot.storage[group]`: what kubelet's disk held over the window. */
  disk: Record<string, number | boolean> | null
  /** `snapshot.network[group]`: the busiest and a typical node's traffic, Gbps. */
  net: Record<string, number | boolean> | null
  discount?: number
  hoursPerMonth?: number
}

/** vCPU per size, for the sizes whose count EC2 names consistently. */
const SIZE_VCPU: Record<string, number> = { medium: 1, large: 2, xlarge: 4 }
/** GiB per vCPU by family class: compute 2, general 4, memory 8, x 16. */
const GIB_PER_VCPU: Record<string, number> = { c: 2, m: 4, t: 4, g: 4, r: 8, z: 8, i: 8, d: 8, x: 16, u: 16 }

/** A type the export does not carry, sketched from its name. The reader
 *  checks the guess and supplies the price: nothing here can ask EC2. */
export function inferType(name: string): { vcpu: number | null; memGib: number | null } {
  const m = /^([a-z]+)(\d+)([a-z-]*)\.(\w+)$/.exec(name.trim().toLowerCase())
  if (!m) return { vcpu: null, memGib: null }
  const [, cls, , , size] = m
  const n = /^(\d+)xlarge$/.exec(size)
  let vcpu = n ? Number(n[1]) * 4 : SIZE_VCPU[size] ?? null
  if (cls === "t" && size === "medium") vcpu = 2
  const ratio = GIB_PER_VCPU[cls[0]] ?? 4
  return { vcpu, memGib: vcpu ? vcpu * ratio : null }
}

export function customType(name: string, vcpu: number, memGib: number, hourlyUsd: number,
                           choices: InstanceChoices): InstanceType {
  const monthly = hourlyUsd * (1 - (choices.discount ?? 0)) * (choices.hoursPerMonth ?? 730)
  return { name: name.trim(), vcpu, memGib, hourlyUsd, monthlyPerNode: monthly, inferred: true,
           storage: { gb: 0, disks: 0, type: "", nvme: "", known: false }, net: null }
}

export interface Retyped {
  scenario: Scenario
  /** The worst of the engine, disk and network verdicts. */
  verdict: "ok" | "warn" | "unsafe" | "unknown"
  notes: string[]
  /** Fits, but short of the headroom a recommendation keeps. */
  tight: boolean
}

// -- storage.assess ----------------------------------------------------------

const UNSAFE_SHARE = 0.85
const WARN_SHARE = 0.70
const MATCH_TOLERANCE = 0.10
const num = (v: number | boolean | undefined) => (typeof v === "number" ? v : 0)
const n0 = (v: number) => v.toLocaleString("en-US", { maximumFractionDigits: 0 })
const n1 = (v: number) => v.toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 })
const isSsd = (t?: InstanceType) => !!t && !!t.storage.gb && t.storage.type.toLowerCase() === "ssd"

export function describeDisk(t?: InstanceType): string {
  if (!t) return "unknown"
  if (!t.storage.known) return "not described"
  if (!t.storage.gb) return "none (EBS only)"
  const kind = t.storage.type.toUpperCase() || "disk"
  const nvme = ["required", "supported"].includes(t.storage.nvme) ? "NVMe " : ""
  const count = t.storage.disks || 1
  return `${count} × ${n0(t.storage.gb / count)} GB ${nvme}${kind}`
}

export function assessStorage(m: InstanceChoices["disk"], current: InstanceType | undefined,
                              proposed: InstanceType, nodesNow: number, nodesAfter: number): StorageCheck {
  const out: StorageCheck = {
    diskBefore: describeDisk(current), diskAfter: describeDisk(proposed),
    ssdBefore: isSsd(current), ssdAfter: isSsd(proposed),
    storeGbBefore: current?.storage.gb ?? 0, storeGbAfter: proposed.storage.gb ?? 0,
    notes: [], verdict: "unknown",
  } as StorageCheck
  const lostStore = !!out.storeGbBefore && !out.storeGbAfter && proposed.storage.known
  if (isSsd(current) && proposed.storage.known && !isSsd(proposed) && out.storeGbAfter)
    out.notes.push("the new type's instance storage is HDD, not SSD")
  if (!m) {
    if (lostStore) {
      out.verdict = "unsafe"
      out.notes.push("the current type has local NVMe and the proposed one has none; disk use was " +
                     "not measured, so anything kept there would move to the EBS root volume unchecked")
    } else {
      out.notes.push("node disk usage was not measured for this group -- re-run " +
                     "`rightsizer storage` against the snapshot")
    }
    return out
  }
  const fs = num(m.fs_gb)
  const usageKnown = m.usage_measured !== false
  if (!usageKnown) out.notes.push("disk size is known but its usage over the window was not measured; " +
                                  "re-run `rightsizer storage`")
  const storeNow = out.storeGbBefore
  const onStore = !!storeNow && !m.on_root && Math.abs(fs - storeNow) <= storeNow * MATCH_TOLERANCE
  const usable = onStore && storeNow ? fs / storeNow : 1.0
  const capAfter = onStore ? (out.storeGbAfter ? out.storeGbAfter * usable : num(m.root_gb)) : fs
  const hot = num(m.hot_peak_gb)
  const nNow = Math.max(nodesNow, 1)
  const nAfter = Math.max(nodesAfter, 1)
  const total = num(m.avg_node_peak_gb) ? num(m.avg_node_peak_gb) * nNow : num(m.total_peak_gb)
  const avgAfter = total / nAfter
  const hotAfter = Math.max(hot, avgAfter)
  const after = usageKnown ? { hotShare: capAfter ? hotAfter / capAfter : null } : null
  const allocRatio = num(m.eph_capacity_gb) ? num(m.eph_allocatable_gb) / num(m.eph_capacity_gb) : 0.9
  const allocAfter = capAfter * allocRatio
  const reqFail = num(m.eph_request_max_gb) > allocAfter || num(m.eph_requests_gb) > allocAfter * nAfter
  const share = after?.hotShare ?? 0
  let verdict: StorageCheck["verdict"] = usageKnown ? "ok" : "unknown"
  if (lostStore && onStore) {
    verdict = "unsafe"
    out.notes.push(`kubelet, images and pod logs live on the ${n0(storeNow)} GB local NVMe today; ` +
                   `the proposed type has none, so they move to the ${n0(num(m.root_gb))} GB EBS root volume`)
  }
  if (reqFail) {
    verdict = "unsafe"
    out.notes.push(`pods request ${n1(num(m.eph_requests_gb))} GB of ephemeral storage ` +
                   `(largest ${n1(num(m.eph_request_max_gb))} GB); ${nAfter} node(s) at ` +
                   `${n0(allocAfter)} GB allocatable cannot hold them`)
  }
  if (share >= UNSAFE_SHARE) {
    verdict = "unsafe"
    out.notes.push(`the fullest node would reach ${Math.round(share * 100)}% of its disk -- kubelet ` +
                   `evicts pods from ${Math.round(UNSAFE_SHARE * 100)}%`)
  } else if (share >= WARN_SHARE && (verdict === "ok" || verdict === "unknown")) {
    verdict = "warn"
    out.notes.push(`the fullest node would reach ${Math.round(share * 100)}% of its disk`)
  }
  out.verdict = verdict
  return out
}

// -- network.assess ----------------------------------------------------------

const NET_WARN_SHARE = 0.70

function bandwidthOf(t?: InstanceType) {
  return t?.net ? { base: t.net.baseline, burst: Math.max(t.net.burst || t.net.baseline, t.net.baseline),
                    source: t.net.source } : null
}

export function describeBandwidth(t?: InstanceType) {
  const bw = bandwidthOf(t)
  if (!bw) return "not described"
  return bw.burst > bw.base ? `${bw.base} Gbps, burst to ${bw.burst}` : `${bw.base} Gbps`
}

export function assessNetwork(m: InstanceChoices["net"], current: InstanceType | undefined,
                              proposed: InstanceType, nodesNow: number, nodesAfter: number): NetworkCheck {
  const before = bandwidthOf(current)
  const after = bandwidthOf(proposed)
  const out: NetworkCheck = {
    bandwidthBefore: describeBandwidth(current), bandwidthAfter: describeBandwidth(proposed),
    baselineBefore: before?.base ?? null, baselineAfter: after?.base ?? null,
    burstBefore: before?.burst ?? null, burstAfter: after?.burst ?? null,
    source: after?.source ?? null, notes: [], verdict: "unknown",
  }
  if (!after) {
    out.notes.push(`network bandwidth of ${proposed.name} is not known`)
    return out
  }
  if (before && after.base < before.base)
    out.notes.push(`baseline per node drops ${before.base} → ${after.base} Gbps`)
  if (before && before.burst <= before.base && after.burst > after.base)
    out.notes.push("the new type reaches its headline bandwidth only while it has network credits")
  if (!m) {
    out.notes.push("node network traffic was not measured for this group")
    return out
  }
  const dir = (k: string) => Math.max(num(m[`rx_${k}`]), num(m[`tx_${k}`]))
  const spread = Math.max(nodesNow, 1) / Math.max(nodesAfter, 1)
  const hotPeak = dir("hot_peak"); const hotSus = dir("hot_sustained")
  const peakAfter = Math.max(hotPeak, dir("avg_peak") * spread)
  const susAfter = Math.max(hotSus, dir("avg_sustained") * spread)
  out.before = { hotPeakGbps: hotPeak, hotSustainedGbps: hotSus,
                 sustainedShare: before ? hotSus / before.base : null }
  out.after = { hotPeakGbps: peakAfter, hotSustainedGbps: susAfter, sustainedShare: susAfter / after.base }
  let verdict: NetworkCheck["verdict"] = "ok"
  if (susAfter > after.base) {
    verdict = "unsafe"
    out.notes.push(`the busiest node would average ${susAfter.toFixed(2)} Gbps over an hour, above the ` +
                   `${after.base} Gbps baseline -- network credits run out and EC2 throttles it to the baseline`)
  }
  if (peakAfter > after.burst) {
    verdict = "unsafe"
    out.notes.push(`the busiest node would reach ${peakAfter.toFixed(2)} Gbps, above the ${after.burst} Gbps ceiling`)
  }
  if (verdict === "ok" && peakAfter > after.base) {
    verdict = "warn"
    out.notes.push(`the busiest node's peaks (${peakAfter.toFixed(2)} Gbps) exceed the ${after.base} Gbps ` +
                   "baseline and depend on network credits")
  } else if (verdict === "ok" && susAfter >= NET_WARN_SHARE * after.base) {
    verdict = "warn"
    out.notes.push(`the busiest node would average ${Math.round(susAfter / after.base * 100)}% of its ` +
                   `${after.base} Gbps baseline over its busiest hour`)
  }
  out.verdict = verdict
  return out
}

// -- the option on another type ----------------------------------------------

/** The engine lever set an option was priced with, read from its steps. */
function modeOf(requires: string[], key: string) {
  const requests = requires.some((r) => r.includes("container request changes"))
  const replicas = requires.some((r) => r.includes("minReplicas"))
  return [requests ? (key.startsWith("memory") ? "memory" : "all") : "", replicas ? "replicas" : ""]
    .filter(Boolean).join("+")
}

/** Steps a type change adds, for payloads from before they were listed per scenario. */
const NODE_STEP = /^(replace the node group|span the new group|set the new node group's minSize|update the node pool's shape config|add \{ ocpus|already offered to Karpenter)/

/** A plan template with the type's name and, for an OCI size, its OCPUs and GB. */
function fillStep(template: string, type: string) {
  const m = /:(\d+(?:\.\d+)?)ocpu-(\d+(?:\.\d+)?)gb/.exec(type)
  return template.replace("{type}", type).replace("{ocpus}", m?.[1] ?? "").replace("{gb}", m?.[2] ?? "")
}

/** The step that reaches `type`, in the words of whoever owns the group's shape. */
export function retypeStep(group: Pick<NodeGroup, "retype">, type: string) {
  const plan = group.retype
  if (!plan) return "replace the node group (instance type is immutable)"
  return plan.offered.includes(type) && plan.ifOffered ? fillStep(plan.ifOffered, type) : fillStep(plan.template, type)
}

function stepsFor(group: Pick<NodeGroup, "retype">, scenario: Scenario, type: string, current: string,
                  nodes: number, zones: string[]) {
  const typeSteps = new Set(scenario.retypeSteps ?? [])
  const out: string[] = []
  for (const r of scenario.requires ?? []) {
    if (typeSteps.has(r) || NODE_STEP.test(r)) continue
    // The floor follows the node count the new type reaches.
    out.push(r.replace(/^(lower the node group minSize from \d+ to )\d+$/, `$1${Math.max(nodes, 1)}`))
  }
  if (type !== current) {
    out.splice(out.findIndex((r) => /minSize|give requests/.test(r)) >>> 0, 0, retypeStep(group, type))
    if (zones.length > 1)
      out.push(`span the new group across ${zones.join(", ")} with a node in each -- pods on zonal ` +
               "volumes can only restart in their volume's zone")
  }
  return out
}

function labelFor(label: string, type: string, current: string) {
  const bare = label.replace(/,?\s*move to [a-z0-9.-]+/i, "").replace(/^,\s*/, "").trim()
  const parts = [bare, type !== current ? `move to ${type}` : ""].filter(Boolean)
  const text = parts.join(", ") || `Move to ${type}`
  return text.charAt(0).toUpperCase() + text.slice(1)
}

/** `scenario` with its changes kept and its node type swapped for `type`. */
export function retype(group: NodeGroup, scenario: Scenario, asis: Scenario,
                       choices: InstanceChoices, type: string, custom?: InstanceType): Retyped | null {
  const model = group.sim?.model as Model | undefined
  const t = custom ?? choices.types.find((x) => x.name === type)
  const now = choices.types.find((x) => x.name === asis.instanceType)
  if (!model || !t || !asis.vcpu || !asis.memGib || !asis.nodeCpu || !asis.nodeMemGib) return null
  const sameType = type === asis.instanceType
  const levers = model.levers?.[modeOf(scenario.requires ?? [], scenario.key)] ?? { deltas: {}, keep: {} }
  const minNodes = scenario.minNodes ?? 0
  const maxPods = scenario.nodeMaxPods ?? asis.nodeMaxPods ?? 110
  // The same lift the option was priced with: the named fixes, or all of them.
  const lift = !sameType ? false
    : scenario.blocked?.lift?.length ? scenario.blocked.lift : (scenario.drainBlocked ?? 0) > 0
  const r = realize(model, levers.deltas, levers.keep, {
    sameType, minNodes, maxPods, nodesNow: asis.nodes,
    vcpu: t.vcpu, memGib: t.memGib, currentVcpu: asis.vcpu, currentMemGib: asis.memGib,
    lifted: lift, name: type,
    storeGbNow: now?.storage.gb ?? 0, storeGb: t.storage.known ? (t.storage.gb ?? 0) : undefined,
    rootGb: num(choices.disk?.root_gb),
  }, model.live ? (levers as { requests?: Record<string, [number | null, number | null]> }).requests ?? {} : undefined)
  if (!r || r.outcome.unplaceable.length || r.outcome.nodes <= 0) return null
  const nodes = r.outcome.nodes
  // Same overhead and DaemonSets per node as today, as `scenarios` assumes.
  const nodeCpu = asis.nodeCpu + (t.vcpu - asis.vcpu)
  const nodeMemGib = asis.nodeMemGib + (t.memGib - asis.memGib)
  if (nodeCpu <= 0 || nodeMemGib <= 0) return null
  const monthlyUsd = t.monthlyPerNode * nodes
  let cpu = 0, mem = 0
  const placement = [...r.outcome.placement.entries()].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .slice(0, 80).map(([, pods]) => {
      const own = pods.filter((p) => p.owner !== "DaemonSet")
      const c = own.reduce((s, p) => s + p.cpu, 0)
      const m = own.reduce((s, p) => s + p.mem, 0)
      return { cpu: c, mem: m / GIB, pods: own.length, dsCpu: model.dsCpu, dsMem: model.dsMem / GIB,
               dsPods: pods.length - own.length }
    })
  for (const pods of r.outcome.placement.values()) for (const p of pods) {
    if (p.owner === "DaemonSet") continue
    cpu += p.cpu; mem += p.mem
  }
  const cu = cpu / (nodeCpu * nodes)
  const mu = mem / (nodeMemGib * GIB * nodes)
  const pods = [...r.outcome.placement.values()].flat()
  const zones = volumeZones(model, pods)
  const storage = assessStorage(choices.disk, now, t, asis.nodes, nodes)
  const network = assessNetwork(choices.net, now, t, asis.nodes, nodes)
  const notes = [...r.notes, ...(storage.verdict === "unsafe" || storage.verdict === "warn" ? storage.notes : []),
                 ...(network.verdict === "unsafe" || network.verdict === "warn" ? network.notes : [])]
  const rank = { unknown: 0, ok: 1, warn: 2, unsafe: 3 } as const
  const verdict = [r.verdict, storage.verdict, network.verdict]
    .reduce((a, b) => (rank[b] > rank[a] ? b : a), "unknown" as Retyped["verdict"])
  const marginNotes = [...r.margin.notes,
    ...(storage.verdict === "warn" ? [`local disk: ${storage.notes.join("; ")}`] : []),
    ...(network.verdict === "warn" ? [`network: ${network.notes.join("; ")}`] : [])]
  const requires = stepsFor(group, scenario, type, asis.instanceType, nodes, zones)
  return {
    verdict, notes, tight: marginNotes.length > 0,
    scenario: {
      ...scenario,
      label: labelFor(scenario.label, type, asis.instanceType),
      instanceType: type, nodes, monthlyUsd, deltaUsd: monthlyUsd - asis.monthlyUsd,
      vcpu: t.vcpu, memGib: t.memGib, hourlyUsd: t.hourlyUsd,
      nodeCpu: Math.round(nodeCpu * 1000) / 1000, nodeMemGib: Math.round(nodeMemGib * 100) / 100,
      cpuUtil: cu, memUtil: mu,
      binding: cu >= mu ? "cpu" : "memory",
      requires, storage, network,
      margin: { ok: marginNotes.length === 0, notes: marginNotes },
      load: { verdict: r.verdict, notes: r.notes, hotCpu: r.load.hotCpu,
              hotMem: r.load.hotMem, peakCpu: r.trend.peakCpu, peakMem: r.trend.peakMem },
      placement,
      model: r.outcome.mode,
      // A different node type is a new option: no blocker list or HPA floors
      // carry over that were computed for another one.
      ...(sameType ? {} : { drainBlocked: undefined, drainBlockers: undefined, drainKept: undefined,
                            drainByHand: undefined, blocked: undefined }),
    } as Scenario,
  }
}

export { attachLive } from "@/lib/live"
