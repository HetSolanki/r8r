import {
  useEffect, useMemo, useState, type KeyboardEvent, type PointerEvent, type ReactNode,
} from "react"
import { MinuteProjection } from "@/components/hpa-minute-projection"
import { project, type Projection } from "@/lib/hpa"
import { loadHpaFile, type HpaFile } from "@/lib/hpa-replay"
import { loadTrend, type TrendFile } from "@/lib/trends"
import { cn } from "@/lib/utils"
import { useFollowY } from "@/lib/follow-y"
import type { ReplicaChange } from "@/types"

/** Never below two: one replica is an outage on every rollout or node drain.
 *  The same floor the analysis applies. */
export const MIN_FLOOR = 2

const W = 720
const PAD = { l: 44, r: 12, t: 10, b: 22 }

const pct = (v: number) => `${Math.round(v * 100)}%`
const day = (t: number) =>
  new Date(t * 1000).toLocaleDateString("en-GB", { day: "numeric", month: "short" })
const stamp = (t: number) =>
  new Date(t * 1000).toLocaleString("en-GB", {
    day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  })

export interface Ref { value: number; label: string; strong?: boolean }

/** One small line chart on the shared time axis. Two series only -- "now" in
 *  slot 1 and "after" in slot 2 -- so the pair is the validated adjacent pair
 *  and identity never falls to colour alone: both are named in the legend. */
export function Panel<P extends { t: number }>({
  title, points, now, after, context, fmt, refs, height, hover, onHover, step, aria,
}: {
  title: string
  points: P[]
  now: (p: P) => number | null
  after: (p: P) => number
  /** A faint reference line in ink, not a series hue: shown for orientation,
   *  never compared against. */
  context?: (p: P) => number | null
  fmt: (v: number) => string
  refs: Ref[]
  height: number
  hover: number | null
  onHover: (i: number | null) => void
  /** Replica counts are whole numbers held until the next change. */
  step?: boolean
  aria: string
}) {
  const H = height
  const x0 = points[0].t
  const x1 = points[points.length - 1].t
  const values = points.flatMap((p) => [now(p) ?? 0, after(p), context?.(p) ?? 0])
  const dataMax = Math.max(...values, 0)
  // A reference line may widen the scale by a third; past that it would squash
  // the data, so it is left off and named in the caption instead.
  const inRange = refs.filter((r) => r.value <= Math.max(dataMax, 1e-9) * 1.35 || r.strong)
  const y1 = Math.max(dataMax, ...inRange.map((r) => r.value)) * 1.1 || 1
  const sx = (t: number) => PAD.l + ((t - x0) / Math.max(x1 - x0, 1)) * (W - PAD.l - PAD.r)
  const sy = (v: number) => H - PAD.b - (v / y1) * (H - PAD.b - PAD.t)

  const path = (get: (p: P) => number | null) => {
    let d = ""
    let prevY: number | null = null
    points.forEach((p) => {
      const v = get(p)
      if (v === null) { prevY = null; return }
      const x = sx(p.t).toFixed(1)
      const y = sy(v).toFixed(1)
      if (prevY === null) d += `M${x},${y}`
      else d += step ? `H${x}V${y}` : `L${x},${y}`
      prevY = v
    })
    return d
  }

  function pick(e: PointerEvent<SVGSVGElement>) {
    const box = e.currentTarget.getBoundingClientRect()
    const x = ((e.clientX - box.left) / box.width) * W
    const t = x0 + ((x - PAD.l) / (W - PAD.l - PAD.r)) * (x1 - x0)
    let best = 0
    points.forEach((p, i) => { if (Math.abs(p.t - t) < Math.abs(points[best].t - t)) best = i })
    onHover(best)
  }
  function keys(e: KeyboardEvent<SVGSVGElement>) {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return
    e.preventDefault()
    const cur = hover ?? (e.key === "ArrowLeft" ? points.length : -1)
    onHover(Math.min(points.length - 1, Math.max(0, cur + (e.key === "ArrowLeft" ? -1 : 1))))
  }

  const hp = hover !== null ? points[hover] : null
  const ticks = [0, y1 / 2, y1]
  const offScale = refs.filter((r) => !inRange.includes(r))

  return (
    <div>
      <div className="mb-0.5 flex items-baseline justify-between gap-2">
        <span className="text-[11.5px] font-semibold">{title}</span>
        {offScale.length > 0 && (
          <span className="text-[10.5px] text-muted-foreground">
            {offScale.map((r) => `${r.label} (off scale)`).join(" · ")}
          </span>
        )}
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full touch-none select-none focus-visible:outline-none
                                              focus-visible:ring-2 focus-visible:ring-ring rounded"
           role="img" aria-label={aria} tabIndex={0}
           onPointerMove={pick} onPointerDown={pick} onPointerLeave={() => onHover(null)}
           onKeyDown={keys} onBlur={() => onHover(null)}>
        {ticks.map((v, i) => (
          <g key={i}>
            <line x1={PAD.l} x2={W - PAD.r} y1={sy(v)} y2={sy(v)}
                  stroke="hsl(var(--border))" strokeWidth="1" />
            <text x={PAD.l - 6} y={sy(v) + 3} textAnchor="end" className="fill-muted-foreground"
                  style={{ fontSize: 10 }}>{fmt(v)}</text>
          </g>
        ))}
        <text x={PAD.l} y={H - 6} className="fill-muted-foreground" style={{ fontSize: 10 }}>{day(x0)}</text>
        <text x={W - PAD.r} y={H - 6} textAnchor="end" className="fill-muted-foreground"
              style={{ fontSize: 10 }}>{day(x1)}</text>

        {/* Thresholds are annotation, not data: ink and dash, never a series hue. */}
        {inRange.map((r) => (
          <g key={r.label}>
            <line x1={PAD.l} x2={W - PAD.r} y1={sy(r.value)} y2={sy(r.value)}
                  stroke="hsl(var(--muted-foreground))" strokeWidth={r.strong ? 1.5 : 1}
                  strokeDasharray={r.strong ? "6 3" : "2 3"} opacity={r.strong ? 0.9 : 0.6} />
            <text x={W - PAD.r - 2} y={sy(r.value) - 4} textAnchor="end" stroke="hsl(var(--card))"
                  strokeWidth="4" strokeLinejoin="round" fill="none" style={{ fontSize: 9.5 }}>{r.label}</text>
            <text x={W - PAD.r - 2} y={sy(r.value) - 4} textAnchor="end" className="fill-muted-foreground"
                  style={{ fontSize: 9.5 }}>{r.label}</text>
          </g>
        ))}

        {context && (
          <path d={path(context)} fill="none" stroke="hsl(var(--muted-foreground))" strokeWidth="1.25"
                strokeDasharray="1 2.5" strokeLinecap="round" opacity={0.7} />
        )}
        {[{ get: now, slot: 1 }, { get: after, slot: 2 }].map(({ get, slot }) => (
          <g key={slot}>
            <path d={path(get)} fill="none" stroke="hsl(var(--card))" strokeWidth="4"
                  strokeLinejoin="round" opacity={0.85} />
            <path d={path(get)} fill="none" stroke={`hsl(var(--series-${slot}))`} strokeWidth="2"
                  strokeLinejoin="round" strokeLinecap="round" />
          </g>
        ))}

        {hp && (
          <g pointerEvents="none">
            <line x1={sx(hp.t)} x2={sx(hp.t)} y1={PAD.t} y2={H - PAD.b}
                  stroke="hsl(var(--foreground))" strokeWidth="1" opacity={0.35} />
            {[{ v: now(hp), slot: 1 }, { v: after(hp), slot: 2 }].map(({ v, slot }) =>
              v === null ? null : (
                <circle key={slot} cx={sx(hp.t)} cy={sy(v)} r={4.5}
                        fill={`hsl(var(--series-${slot}))`} stroke="hsl(var(--card))" strokeWidth="2" />
              ))}
          </g>
        )}
      </svg>
    </div>
  )
}

export function Tile({ label, slot, children, className }: {
  label: string; slot?: 1 | 2; children: ReactNode; className?: string
}) {
  return (
    <div className={cn("rounded-md border px-3 py-2", className)}>
      <div className="flex items-center gap-1.5 text-[10.5px] font-semibold uppercase tracking-[0.06em]
                      text-muted-foreground">
        {slot && <i className={cn("inline-block h-2 w-2 rounded-[2px]",
          slot === 1 ? "bg-series-1" : "bg-series-2")} />}
        {label}
      </div>
      <div className="mt-1 grid gap-0.5 text-[12.5px]">{children}</div>
    </div>
  )
}

/** Beside the crosshair, on whichever side has room. The charts scale to the
 *  container width, so the crosshair's x as a share of it is fixed. */
export function tipAt(i: number, n: number) {
  const x = ((PAD.l + (i / Math.max(n - 1, 1)) * (W - PAD.l - PAD.r)) / W) * 100
  return x > 55 ? { right: `${100 - x + 1.5}%` } : { left: `${x + 1.5}%` }
}

/** What this workload's replicas and their utilisation looked like over the
 *  window, beside what the HPA would have done with the recommended floor. */
/** The minute replay when this HPA has history, the 6-hourly trend otherwise. */
export function HpaProjection(props: {
  change: ReplicaChange
  env: string
  floor: number
  onFloor: (n: number) => void
}) {
  const { change, env } = props
  const [file, setFile] = useState<HpaFile | null | undefined>(undefined)
  useEffect(() => {
    setFile(undefined)
    loadHpaFile(env, change.ns, change.hpa).then(setFile).catch(() => setFile(null))
  }, [env, change.ns, change.hpa])
  if (file === undefined) return <p className="text-[12px] text-muted-foreground">Loading history…</p>
  if (file) return <MinuteProjection {...props} file={file} />
  return <TrendProjection {...props} />
}

function TrendProjection({
  change, env, floor, onFloor,
}: {
  change: ReplicaChange
  env: string
  /** The minReplicas being tried -- owned by the caller, which saves it and
   *  prices it, so the chart and the cost never disagree. */
  floor: number
  onFloor: (n: number) => void
}) {
  const [trend, setTrend] = useState<TrendFile | null>(null)
  const [error, setError] = useState<string | null>(null)
  const setFloor = onFloor
  const [hover, setHover] = useState<number | null>(null)
  const follow = useFollowY()

  useEffect(() => {
    setTrend(null)
    setError(null)
    if (!change.container) {
      setError("this dataset predates projections -- re-run export_ui.py")
      return
    }
    loadTrend(env, change.ns, change.workload, change.container)
      .then(setTrend)
      .catch((e: Error) => setError(e.message))
  }, [env, change.ns, change.workload, change.container])

  const requestNow = change.requestPerReplica
  const requestAfter = change.requestAfter ?? change.requestPerReplica
  const proj: Projection | null = useMemo(() => {
    const total = trend?.cpu.bands.total ?? []
    const replicas = trend?.cpu.bands.replicas ?? []
    if (total.length < 2) return null
    return project({
      total, replicas, requestNow, requestAfter,
      target: change.cpuTarget, floorNow: change.minReplicas, floor, max: change.maxReplicas,
    })
  }, [trend, requestNow, requestAfter, change.cpuTarget, change.minReplicas, change.maxReplicas, floor])

  if (error) {
    return <p className="text-[12px] text-muted-foreground">No projection: {error}.</p>
  }
  if (!trend) return <p className="text-[12px] text-muted-foreground">Loading usage…</p>
  if (!proj) return <p className="text-[12px] text-muted-foreground">No CPU usage recorded in this window.</p>

  const hp = hover !== null ? proj.points[hover] : null
  const freed = proj.now.coresAvg - proj.after.coresAvg
  const lo = Math.min(MIN_FLOOR, change.suggestedMin)
  const hi = Math.max(change.minReplicas, change.suggestedMin, floor)
  const requestCut = Math.abs(requestAfter - requestNow) > requestNow * 0.01

  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px]">
        <label htmlFor={`floor-${change.hpa}`} className="font-medium">Try minReplicas</label>
        <input id={`floor-${change.hpa}`} type="range" min={lo} max={hi} step={1} value={floor}
               onChange={(e) => setFloor(Number(e.target.value))}
               className="w-48 accent-[hsl(var(--series-2))]" />
        <span className="font-mono tnum font-semibold">{floor}</span>
        <span className="text-muted-foreground">
          current {change.minReplicas} · recommended {change.suggestedMin} · max {change.maxReplicas}
        </span>
        {floor !== change.suggestedMin && (
          <button type="button" onClick={() => setFloor(change.suggestedMin)}
                  className="text-muted-foreground underline underline-offset-2 hover:text-foreground">
            back to recommended
          </button>
        )}
      </div>

      <div className="grid gap-2 sm:grid-cols-3">
        <Tile label={`Now · minReplicas ${change.minReplicas}`} slot={1}>
          <div><span className="font-mono tnum font-semibold">{proj.now.replicasAvg.toFixed(0)}</span>
            <span className="text-muted-foreground"> replicas avg · {proj.now.replicasMax} peak</span></div>
          <div><span className="font-mono tnum font-semibold">{pct(proj.now.utilAvg)}</span>
            <span className="text-muted-foreground"> CPU per replica avg · {pct(proj.now.utilMax)} peak</span></div>
        </Tile>
        <Tile label={`At minReplicas ${floor}`} slot={2}>
          <div><span className="font-mono tnum font-semibold">{proj.after.replicasAvg.toFixed(0)}</span>
            <span className="text-muted-foreground"> replicas avg · {proj.after.replicasMax} peak</span></div>
          <div><span className="font-mono tnum font-semibold">{pct(proj.after.utilAvg)}</span>
            <span className="text-muted-foreground"> CPU per replica avg · {pct(proj.after.utilMax)} peak</span></div>
        </Tile>
        <Tile label="Difference">
          <div>
            {Math.abs(freed) < 0.05 ? (
              <span className="font-semibold text-muted-foreground">no change</span>
            ) : (
              <>
                <span className={cn("font-mono tnum font-semibold",
                  freed > 0 ? "text-series-1" : "text-status-serious")}>
                  {freed > 0 ? "−" : "+"}{Math.abs(freed).toFixed(1)} cores
                </span>
                <span className="text-muted-foreground"> reserved on average</span>
              </>
            )}
          </div>
          <div className="text-muted-foreground">
            HPA above the floor {pct(proj.scaledShare)} of the time
            {proj.scaleUps > 0 && <> · {proj.scaleUps} scale-up{proj.scaleUps === 1 ? "" : "s"}</>}
          </div>
          {proj.cappedShare > 0 && (
            <div className="font-medium text-status-warning">
              ⚠ at maxReplicas and still over target {pct(proj.cappedShare)} of the time
            </div>
          )}
        </Tile>
      </div>

      <div className="relative grid gap-2" {...follow.box}>
        <Panel
          title="Replicas"
          aria={`Replica count over the window: observed, and projected at minReplicas ${floor}`}
          points={proj.points} step height={140} hover={hover} onHover={setHover}
          now={(p) => p.replicasNow} after={(p) => p.replicasAfter} context={(p) => p.observed}
          fmt={(v) => v.toFixed(0)}
          refs={[
            { value: change.minReplicas, label: `current min ${change.minReplicas}` },
            { value: floor, label: `new min ${floor}`, strong: true },
            { value: change.maxReplicas, label: `max ${change.maxReplicas}` },
          ]}
        />
        <Panel
          title="CPU utilisation per replica"
          aria={`CPU utilisation per replica over the window against the HPA target of ${pct(change.cpuTarget)}`}
          points={proj.points} height={150} hover={hover} onHover={setHover}
          now={(p) => p.utilNow} after={(p) => p.utilAfter}
          fmt={pct}
          refs={[{ value: change.cpuTarget, label: `HPA target ${pct(change.cpuTarget)}`, strong: true }]}
        />

        {hp && (
          <div role="status" ref={follow.tip}
               className="pointer-events-none absolute top-6 z-10 min-w-[210px] rounded-md border bg-popover
                          px-2.5 py-2 text-[11.5px] text-popover-foreground shadow-md"
               style={tipAt(hover!, proj.points.length)}>
            <div className="font-medium">{stamp(hp.t)}</div>
            <div className="mb-1 text-[10.5px] text-muted-foreground">busiest moment in this 6-hour bucket</div>
            <div className="mb-1 text-muted-foreground">
              total CPU <span className="font-mono tnum text-foreground">{hp.total.toFixed(2)}</span> cores
            </div>
            <div className="grid grid-cols-[auto_1fr_auto] items-center gap-x-2 gap-y-0.5">
              <i className="inline-block h-2 w-2 rounded-[2px] bg-series-1" />
              <span>At min {change.minReplicas}</span>
              <span className="text-right font-mono tnum">
                {hp.replicasNow} × {pct(hp.utilNow)}
              </span>
              <i className="inline-block h-2 w-2 rounded-[2px] bg-series-2" />
              <span>At min {floor}</span>
              <span className="text-right font-mono tnum">
                {hp.replicasAfter} × {pct(hp.utilAfter)}
              </span>
            </div>
            {hp.observed !== null && (
              <div className="mt-1 text-muted-foreground">
                {hp.observed} pods observed running
              </div>
            )}
            {hp.scaled && (
              <div className="mt-1 text-muted-foreground">
                {hp.capped ? "HPA pinned at maxReplicas" : "HPA scaled above the floor here"}
              </div>
            )}
          </div>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <i className="inline-block h-2 w-2 rounded-[2px] bg-series-1" /> today, at minReplicas {change.minReplicas}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <i className="inline-block h-2 w-2 rounded-[2px] bg-series-2" /> projected at minReplicas {floor}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <svg width="16" height="6" aria-hidden="true"><line x1="0" y1="3" x2="16" y2="3"
            stroke="hsl(var(--muted-foreground))" strokeWidth="1.25" strokeDasharray="1 2.5" /></svg>
          pods observed running
        </span>
        <span>· hover or use ← → on a chart to read a point</span>
      </div>

      <p className="max-w-[78ch] text-[11.5px] text-muted-foreground">
        The projection replays the HPA over the recorded CPU:{" "}
        <span className="font-mono">replicas = clamp(ceil(total ÷ (request × target)), min, max)</span>,
        with the total taken as unchanged — the same work, spread over fewer pods. Both sides come from
        that one model, so the difference is the floor alone. The dotted line is what actually ran, for
        orientation: it includes pods surging during rollouts and any floor that was live earlier.
        {requestCut && (
          <> Utilisation after is against the <strong className="font-semibold">reduced</strong> request
            of {(requestAfter * 1000).toFixed(0)}m this option also applies (now {(requestNow * 1000).toFixed(0)}m).</>
        )}{" "}
        Each point is the busiest moment in a 6-hour bucket, so averages here are averages of those peaks
        and sit above the table's whole-window average — the cautious reading. The real HPA
        also reacts with a lag of a minute or more, and new pods need time to become ready, which this
        does not model.
      </p>
    </div>
  )
}
