/** Replaying an HPA over a workload's recorded CPU, at a floor of our choosing.
 *
 *  The same model as `simulate._desired_at` in the analysis: for CPU the HPA's
 *  `ceil(replicas x current/target)` reduces to `ceil(total / (request x
 *  target))` because the replica count cancels, clamped to [minReplicas,
 *  maxReplicas]. Total usage is taken as independent of the replica count --
 *  the work arriving does not change because it is spread over fewer pods.
 *
 *  Kept deliberately identical, tolerance quirk included: a projection that
 *  disagreed with the floor it is illustrating would be worse than none. */

const HPA_TOLERANCE = 0.1

export function desiredAt(total: number, request: number, target: number): number {
  if (request <= 0 || target <= 0) return 0
  const ratio = total / (request * target)
  if (Math.abs(ratio - 1) <= HPA_TOLERANCE) return 1
  return Math.max(1, Math.ceil(ratio))
}

export interface ProjectionPoint {
  t: number
  /** Concurrent CPU of every replica, cores: the busiest moment in the bucket. */
  total: number
  /** Pods actually running -- context only. It counts both sides of a rolling
   *  update and whatever floor was live at the time, so it is never the "now"
   *  side of the comparison. */
  observed: number | null
  /** The replay at today's minReplicas, and at the one being tried. Both sides
   *  come from the same model, because a saving measured from a real count to a
   *  simulated one flatters the simulation. */
  replicasNow: number
  /** Per-replica utilisation against the request, as a fraction (0.8 = 80%). */
  utilNow: number
  replicasAfter: number
  utilAfter: number
  /** The HPA, not the floor, set the count at this point (after the change). */
  scaled: boolean
  /** Even maxReplicas could not bring utilisation to target. */
  capped: boolean
}

interface Side { replicasAvg: number; replicasMax: number; utilAvg: number; utilMax: number; coresAvg: number }

export interface Projection {
  points: ProjectionPoint[]
  now: Side
  after: Side
  /** Share of the window the HPA would have scaled above the new floor. */
  scaledShare: number
  /** How many separate times it would have had to scale up off the floor. */
  scaleUps: number
  cappedShare: number
}

const mean = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0)

function replicasAt(total: number, request: number, target: number, floor: number, max: number) {
  const desired = desiredAt(total, request, target)
  const ceiling = max > 0 ? max : Infinity
  return { replicas: Math.min(Math.max(desired, floor), ceiling), desired, ceiling }
}

export function project({
  total, replicas, requestNow, requestAfter, target, floorNow, floor, max,
}: {
  total: [number, number][]
  replicas: [number, number][]
  requestNow: number
  requestAfter: number
  target: number
  /** Today's minReplicas. */
  floorNow: number
  /** The minReplicas being tried. */
  floor: number
  max: number
}): Projection {
  const seen = new Map(replicas.map(([t, v]) => [t, v]))
  const points: ProjectionPoint[] = total.map(([t, cores]) => {
    const now = replicasAt(cores, requestNow, target, floorNow, max)
    const after = replicasAt(cores, requestAfter, target, floor, max)
    const obs = seen.get(t)
    return {
      t,
      total: cores,
      observed: obs && obs > 0 ? obs : null,
      replicasNow: now.replicas,
      utilNow: requestNow > 0 ? cores / (now.replicas * requestNow) : 0,
      replicasAfter: after.replicas,
      utilAfter: requestAfter > 0 ? cores / (after.replicas * requestAfter) : 0,
      scaled: after.desired > floor,
      capped: after.desired > after.ceiling,
    }
  })

  const side = (r: (p: ProjectionPoint) => number, u: (p: ProjectionPoint) => number, req: number): Side => ({
    replicasAvg: mean(points.map(r)),
    replicasMax: Math.max(0, ...points.map(r)),
    utilAvg: mean(points.map(u)),
    utilMax: Math.max(0, ...points.map(u)),
    coresAvg: mean(points.map((p) => r(p) * req)),
  })
  let scaleUps = 0
  points.forEach((p, i) => { if (p.scaled && !points[i - 1]?.scaled) scaleUps++ })
  const n = Math.max(points.length, 1)
  return {
    points,
    now: side((p) => p.replicasNow, (p) => p.utilNow, requestNow),
    after: side((p) => p.replicasAfter, (p) => p.utilAfter, requestAfter),
    scaledShare: points.filter((p) => p.scaled).length / n,
    scaleUps,
    cappedShare: points.filter((p) => p.capped).length / n,
  }
}
