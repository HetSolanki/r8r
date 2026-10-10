/** A port of `rightsizer/engine/simulate.py::pack`, for the in-browser simulator.
 *
 *  The console is static, so a per-selection answer cannot come from the
 *  Python engine and cannot be precomputed -- the subsets are combinatorial.
 *  This is the one place the money math exists twice, which is the failure this
 *  project already had when `whatif()` and `scenarios()` disagreed about
 *  instance types.
 *
 *  The guard is `sim.packFixtures`: cases packed by the Python engine, shipped
 *  with the node count each produced. `checkFixtures` replays them here, and
 *  the smoke test fails if any disagrees. Change one implementation without the
 *  other and the build stops.
 */

const GIB = 1024 ** 3

export interface SimPod {
  /** Index into `workloads`. */
  w: number
  /** CPU request as placed, in cores. */
  c: number
  /** Memory request as placed, in bytes. */
  m: number
}

export interface PackPod {
  workload: string
  cpu: number
  memory: number
  antiaffinity: string
  /** Tiebreak name. The engine sorts equal pods by name; without one the pod's
   *  index stands in, which is what the packing fixtures were built with. */
  name?: string
}

interface PackNode {
  cpuFree: number
  memFree: number
  podsFree: number
  present: Set<string>
  pods: PackPod[]
}

export interface PackResult {
  nodes: number
  unplaceable: PackPod[]
  cpuUtil: number
  memUtil: number
  podUtil: number
  binding: "cpu" | "memory" | "pods" | "no capacity" | "no pods"
  placement: { cpu: number; mem: number; pods: number; dsCpu?: number; dsMem?: number; dsPods?: number }[]
}

/** Largest first by the more binding dimension, then a stable name tiebreak.
 *  Mirrors `Pod.sort_key`: (-max(cpu, mem/GIB/4), -cpu, name). */
function sortKey(p: PackPod, name: string): [number, number, string] {
  return [-Math.max(p.cpu, p.memory / GIB / 4), -p.cpu, name]
}

function compare(a: [number, number, string], b: [number, number, string]) {
  return a[0] - b[0] || a[1] - b[1] || (a[2] < b[2] ? -1 : a[2] > b[2] ? 1 : 0)
}

export function pack(
  pods: PackPod[],
  opts: { cpuPerNode: number; memPerNode: number; maxPods: number; headroom: number },
): PackResult {
  const { cpuPerNode, memPerNode, maxPods, headroom } = opts
  if (cpuPerNode <= 0 || memPerNode <= 0 || maxPods <= 0) {
    return { nodes: 0, unplaceable: [...pods], cpuUtil: 0, memUtil: 0, podUtil: 0,
             binding: "no capacity", placement: [] }
  }

  // Fill only to the headroom target; utilisation is still reported against
  // full allocatable, so a reader sees how full the node really is.
  const usableCpu = cpuPerNode * headroom
  const usableMem = memPerNode * headroom
  const usablePods = Math.max(1, Math.trunc(maxPods * headroom))

  const nodes: PackNode[] = []
  const unplaceable: PackPod[] = []

  const ordered = pods
    .map((p, i) => ({ p, k: sortKey(p, p.name ?? String(i)) }))
    .sort((a, b) => compare(a.k, b.k))
    .map((x) => x.p)

  for (const pod of ordered) {
    // Oversized is measured against full allocatable: a pod that fits the node
    // but not the headroom budget is a packing constraint, not a broken pod.
    if (pod.cpu > cpuPerNode || pod.memory > memPerNode) {
      unplaceable.push(pod)
      continue
    }
    let placed = false
    for (const node of nodes) {
      if (node.podsFree <= 0) continue
      if (pod.cpu > node.cpuFree || pod.memory > node.memFree) continue
      if (pod.antiaffinity && node.present.has(pod.antiaffinity)) continue
      node.cpuFree -= pod.cpu
      node.memFree -= pod.memory
      node.podsFree -= 1
      if (pod.antiaffinity) node.present.add(pod.antiaffinity)
      node.pods.push(pod)
      placed = true
      break
    }
    if (!placed) {
      const fresh: PackNode = {
        cpuFree: usableCpu - pod.cpu,
        memFree: usableMem - pod.memory,
        podsFree: usablePods - 1,
        present: new Set(pod.antiaffinity ? [pod.antiaffinity] : []),
        pods: [pod],
      }
      nodes.push(fresh)
    }
  }

  if (nodes.length === 0) {
    return { nodes: 0, unplaceable, cpuUtil: 0, memUtil: 0, podUtil: 0,
             binding: "no pods", placement: [] }
  }

  const n = nodes.length
  const cpuUsed = nodes.reduce((t, x) => t + (usableCpu - x.cpuFree), 0)
  const memUsed = nodes.reduce((t, x) => t + (usableMem - x.memFree), 0)
  const podsUsed = nodes.reduce((t, x) => t + (usablePods - x.podsFree), 0)
  const cpuUtil = cpuUsed / (cpuPerNode * n)
  const memUtil = memUsed / (memPerNode * n)
  const podUtil = podsUsed / (maxPods * n)
  const worst = Math.max(cpuUtil, memUtil, podUtil)
  const binding = worst === cpuUtil ? "cpu" : worst === memUtil ? "memory" : "pods"

  return {
    nodes: n,
    unplaceable,
    cpuUtil,
    memUtil,
    podUtil,
    binding,
    placement: nodes.map((x) => ({
      cpu: usableCpu - x.cpuFree,
      mem: usableMem - x.memFree,
      pods: x.pods.length,
    })),
  }
}
