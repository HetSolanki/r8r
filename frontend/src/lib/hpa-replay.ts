/** Port of `rightsizer/engine/hpa_replay.py` -- keep the two line-for-line.
 *
 *  The console replays the HPA itself so the floor slider can try any value;
 *  every history file carries the engine's own replay at today's floor and the
 *  suggested one, and `checkFixtures` refuses to draw when the port disagrees.
 */

import { hpaUrl } from "@/lib/data-paths"

export const TOLERANCE = 0.1
export const DEFAULT_LAG_MINUTES = 2

export interface Policy { type: "Pods" | "Percent"; value: number; period: number }
export interface Rules { stabilization: number; select: "Max" | "Min" | "Disabled"; policies: Policy[] }
export interface Behavior { up: Rules; down: Rules; source: "spec" | "inferred" | "assumed" | "default" }

export interface HpaFile {
  schema: number
  env: string
  namespace: string
  hpa: string
  workload: string
  start: number
  step: number
  cpuM: (number | null)[]
  desired: [number | null, number][]
  ready: [number | null, number][]
  behavior: Behavior
  lagMinutes: number
  cpuOnly: boolean
  target: number
  minReplicas: number
  maxReplicas: number
  podRequest: number
  /** minReplicas in force each minute, when collected. */
  minHistory?: [number | null, number][] | null
  /** The engine's replay of the floor as it was each minute, against the real HPA. */
  validation?: { minutes: number; within_one: number; exact: number; mae: number;
                 model_avg: number; actual_avg: number } | null
  fixtures: (Summary & { floor: number; request: number })[]
}

export interface Summary {
  minutes: number
  avg_replicas: number
  peak_replicas: number
  over_request_minutes: number
  over_target_minutes: number
  max_util: number
  longest_over_request: number
  unreserved_core_minutes: number
}

export interface ReplayResult extends Summary {
  replicas: number[]
  ready: number[]
  util: (number | null)[]
}

export function rleDecode<T>(runs: [T, number][]): T[] {
  const out: T[] = []
  for (const [v, n] of runs) for (let i = 0; i < n; i++) out.push(v)
  return out
}

const round = (x: number, dp: number) => Math.round(x * 10 ** dp) / 10 ** dp

/** Number of entries <= x in an ascending array (Python's bisect_right). */
function bisectRight(a: number[], x: number) {
  let lo = 0
  let hi = a.length
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (x < a[mid]) hi = mid
    else lo = mid + 1
  }
  return lo
}

function allowance(rules: Rules, replicas: number, events: [number, number][], t: number, step: number) {
  if (rules.select === "Disabled") return 0
  const limits: number[] = []
  for (const p of rules.policies) {
    if (p.period < step) {
      const perTick = Math.max(1, Math.floor(step / Math.max(p.period, 1)))
      limits.push(p.type === "Pods" ? p.value * perTick
        : Math.ceil(replicas * ((1 + p.value / 100) ** perTick - 1)))
      continue
    }
    const moved = events.reduce((s, [at, n]) => (t - at < p.period ? s + n : s), 0)
    const start = replicas - moved
    const cap = p.type === "Pods" ? p.value : Math.ceil(Math.max(start, 1) * p.value / 100)
    limits.push(Math.max(0, cap - moved))
  }
  if (!limits.length) return 1e9
  return rules.select !== "Min" ? Math.max(...limits) : Math.min(...limits)
}

export function replay(cpu: (number | null)[], o: {
  podRequest: number; target: number; minReplicas: number; maxReplicas: number
  behavior: Behavior; lagMinutes?: number; step?: number
}): ReplayResult {
  const step = o.step ?? 60
  const ceiling = o.maxReplicas > 0 ? o.maxReplicas : 1e6
  const lag = Math.max(0, Math.floor((o.lagMinutes ?? DEFAULT_LAG_MINUTES) + 0.5))
  const { behavior: b, podRequest: req, target } = o
  const recommend = (total: number) => Math.max(1, Math.ceil(total / (req * target)))

  let spec = Math.min(Math.max(o.minReplicas, 1), ceiling)
  const readyAt: number[] = new Array(spec).fill(-1e9)
  let recs: [number, number][] = []
  let upEvents: [number, number][] = []
  let downEvents: [number, number][] = []
  const outSpec: number[] = []
  const outReady: number[] = []
  const outUtil: (number | null)[] = []
  const horizon = Math.max(b.up.stabilization, b.down.stabilization,
    ...b.up.policies.map((p) => p.period), ...b.down.policies.map((p) => p.period), step)

  for (let i = 0; i < cpu.length; i++) {
    const total = cpu[i]
    const t = i * step
    const ready = bisectRight(readyAt, i) || spec
    if (total === null) {
      outSpec.push(spec); outReady.push(ready); outUtil.push(null)
      continue
    }
    const util = req > 0 ? total / (ready * req) : 0
    const ratio = target > 0 ? util / target : 1
    let rec = Math.abs(ratio - 1) <= TOLERANCE ? spec : recommend(total)
    rec = Math.min(Math.max(rec, o.minReplicas), ceiling)

    recs.push([t, rec])
    recs = recs.filter(([at]) => t - at <= horizon)
    let upRec = Infinity
    let downRec = -Infinity
    for (const [at, r] of recs) {
      if (t - at <= b.up.stabilization) upRec = Math.min(upRec, r)
      if (t - at <= b.down.stabilization) downRec = Math.max(downRec, r)
    }
    let next = spec
    if (next < upRec) next = upRec
    if (next > downRec) next = downRec

    upEvents = upEvents.filter(([at]) => t - at < horizon)
    downEvents = downEvents.filter(([at]) => t - at < horizon)
    if (next > spec) next = Math.min(next, spec + allowance(b.up, spec, upEvents, t, step))
    else if (next < spec) next = Math.max(next, spec - allowance(b.down, spec, downEvents, t, step))
    next = Math.min(Math.max(next, o.minReplicas), ceiling)

    if (next > spec) {
      for (let k = 0; k < next - spec; k++) readyAt.push(i + lag)
      upEvents.push([t, next - spec])
    } else if (next < spec) {
      readyAt.length = next
      downEvents.push([t, spec - next])
    }
    spec = next
    outSpec.push(spec); outReady.push(ready); outUtil.push(util)
  }

  let over = 0, run = 0, longest = 0, overTarget = 0, maxUtil = 0, unreserved = 0
  outUtil.forEach((u, i) => {
    if (u !== null && u > 1.0) { over++; run++; longest = Math.max(longest, run) } else run = 0
    if (u !== null) {
      if (u > target) overTarget++
      maxUtil = Math.max(maxUtil, u)
    }
    const c = cpu[i]
    if (c !== null) unreserved += Math.max(0, c - outReady[i] * req)
  })
  const n = Math.max(outSpec.length, 1)
  return {
    replicas: outSpec, ready: outReady, util: outUtil, minutes: outSpec.length,
    avg_replicas: round(outSpec.reduce((a, x) => a + x, 0) / n, 3),
    peak_replicas: outSpec.reduce((a, x) => Math.max(a, x), 0),
    over_request_minutes: over, over_target_minutes: overTarget,
    max_util: round(maxUtil, 4), longest_over_request: longest,
    unreserved_core_minutes: round(unreserved, 2),
  }
}

/** Replay the engine's own cases. A mismatch means the port has drifted and
 *  nothing it draws is this tool's number. */
export function checkFixtures(file: HpaFile, cpu: (number | null)[]): string[] {
  const out: string[] = []
  for (const f of file.fixtures) {
    const r = replay(cpu, {
      podRequest: f.request, target: file.target, minReplicas: f.floor,
      maxReplicas: file.maxReplicas, behavior: file.behavior, lagMinutes: file.lagMinutes,
      step: file.step,
    })
    for (const k of ["avg_replicas", "peak_replicas", "over_request_minutes", "max_util"] as const) {
      if (Math.abs(r[k] - f[k]) > 1e-3) {
        out.push(`floor ${f.floor}: ${k} engine ${f[k]}, console ${r[k]}`)
      }
    }
  }
  return out
}

export async function loadHpaFile(env: string, ns: string, hpa: string): Promise<HpaFile | null> {
  const r = await fetch(hpaUrl(env, `${ns}__${hpa}`.replace(/\//g, "-")), { cache: "no-store" })
  if (!r.ok) return null
  // A single-page host answers a missing file with index.html and a 200.
  const body = await r.text()
  if (!body.trimStart().startsWith("{")) return null
  const doc = JSON.parse(body)
  return doc?.schema >= 1 && Array.isArray(doc.cpuM) ? (doc as HpaFile) : null
}

/** How a direction's rules read to a person. */
export function describeRules(r: Rules, verb: "up" | "down") {
  if (r.select === "Disabled") return `never scales ${verb}`
  const pol = r.policies.map((p) => p.type === "Pods"
    ? `${p.value} pod${p.value === 1 ? "" : "s"} / ${p.period}s`
    : `${p.value}% / ${p.period}s`).join(r.select === "Min" ? " or (slower) " : " or ")
  const stab = r.stabilization ? ` after a ${r.stabilization}s window` : ""
  return `${verb} ${pol}${stab}`
}
