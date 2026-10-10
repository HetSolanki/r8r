import { useState, type KeyboardEvent, type PointerEvent, type ReactNode } from "react"
import type { Ref } from "@/components/hpa-projection"
import { useFollowY } from "@/lib/follow-y"

const W = 720
const PAD = { l: 44, r: 12, t: 12, b: 22 }

const day = (t: number) =>
  new Date(t * 1000).toLocaleDateString("en-GB", { day: "numeric", month: "short" })

export interface Series<P> {
  id: string
  label: string
  /** Categorical slot, in the theme's fixed order. It follows the entity -- a
   *  resource keeps its slot on every tab -- so a tab never repaints a line. */
  slot: 1 | 2 | 3 | 4
  /** "now" is dashed, "after" solid: the second channel, so the two readings
   *  of one resource never rely on colour to be told apart. */
  dashed?: boolean
  get: (p: P) => number | null
}

/** Line sample for legends and tooltips: the same colour and dash as the line. */
export function LineKey({ slot, dashed }: { slot: number; dashed?: boolean }) {
  return (
    <svg width="18" height="8" aria-hidden="true" className="shrink-0">
      <line x1="1" y1="4" x2="17" y2="4" stroke={`hsl(var(--series-${slot}))`} strokeWidth="2"
            strokeDasharray={dashed ? "4 3" : undefined} strokeLinecap="round" />
    </svg>
  )
}

/** Several series on one axis and one crosshair.
 *
 *  One axis only: every series passed in shares a unit. A different unit is a
 *  different chart -- the node count gets its own tab, not a second scale. */
export function MultiChart<P extends { t: number }>({
  points, series, refs = [], fmt, height = 220, step, aria, tip,
}: {
  points: P[]
  series: Series<P>[]
  refs?: Ref[]
  fmt: (v: number) => string
  height?: number
  step?: boolean
  aria: string
  /** Tooltip body for the hovered point. */
  tip: (p: P) => ReactNode
}) {
  const [hover, setHover] = useState<number | null>(null)
  const follow = useFollowY()
  const H = height
  const x0 = points[0].t
  const x1 = points[points.length - 1].t
  const values = points.flatMap((p) => series.map((s) => s.get(p) ?? 0))
  const dataMax = Math.max(...values, 0)
  // A reference line may widen the scale by a third; past that it would squash
  // the data, so it is left off and named above the chart instead.
  const inRange = refs.filter((r) => r.value <= Math.max(dataMax, 1e-9) * 1.35 || r.strong)
  const y1 = Math.max(dataMax, ...inRange.map((r) => r.value)) * 1.1 || 1
  const sx = (t: number) => PAD.l + ((t - x0) / Math.max(x1 - x0, 1)) * (W - PAD.l - PAD.r)
  const sy = (v: number) => H - PAD.b - (v / y1) * (H - PAD.b - PAD.t)
  const offScale = refs.filter((r) => !inRange.includes(r))

  const path = (get: (p: P) => number | null) => {
    let d = ""
    let prev: number | null = null
    points.forEach((p) => {
      const v = get(p)
      if (v === null) { prev = null; return }
      const x = sx(p.t).toFixed(1)
      const y = sy(v).toFixed(1)
      d += prev === null ? `M${x},${y}` : step ? `H${x}V${y}` : `L${x},${y}`
      prev = v
    })
    return d
  }

  function pick(e: PointerEvent<SVGSVGElement>) {
    const box = e.currentTarget.getBoundingClientRect()
    const x = ((e.clientX - box.left) / box.width) * W
    const t = x0 + ((x - PAD.l) / (W - PAD.l - PAD.r)) * (x1 - x0)
    let best = 0
    points.forEach((p, i) => { if (Math.abs(p.t - t) < Math.abs(points[best].t - t)) best = i })
    setHover(best)
  }
  function keys(e: KeyboardEvent<SVGSVGElement>) {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return
    e.preventDefault()
    const cur = hover ?? (e.key === "ArrowLeft" ? points.length : -1)
    setHover(Math.min(points.length - 1, Math.max(0, cur + (e.key === "ArrowLeft" ? -1 : 1))))
  }

  const hp = hover !== null ? points[hover] : null
  const xPct = hp ? (sx(hp.t) / W) * 100 : 0

  return (
    <div className="grid gap-1.5">
      {offScale.length > 0 && (
        <div className="text-right text-[10.5px] text-muted-foreground">
          {offScale.map((r) => `${r.label} (off scale)`).join(" · ")}
        </div>
      )}
      <div className="relative" {...follow.box}>
        <svg viewBox={`0 0 ${W} ${H}`} className="w-full touch-none select-none rounded
                                                focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
             role="img" aria-label={aria} tabIndex={0}
             onPointerMove={pick} onPointerDown={pick} onPointerLeave={() => setHover(null)}
             onKeyDown={keys} onBlur={() => setHover(null)}>
          {[0, y1 / 2, y1].map((v, i) => (
            <g key={i}>
              <line x1={PAD.l} x2={W - PAD.r} y1={sy(v)} y2={sy(v)} stroke="hsl(var(--border))" strokeWidth="1" />
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
                    stroke="hsl(var(--muted-foreground))" strokeWidth={r.strong ? 1.25 : 1}
                    strokeDasharray={r.strong ? "6 3" : "2 3"} opacity={r.strong ? 0.85 : 0.55} />
              {[{ halo: true }, { halo: false }].map(({ halo }) => (
                <text key={String(halo)} x={W - PAD.r - 2} y={sy(r.value) - 4} textAnchor="end"
                      {...(halo
                        ? { stroke: "hsl(var(--card))", strokeWidth: 4, strokeLinejoin: "round" as const, fill: "none" }
                        : { className: "fill-muted-foreground" })}
                      style={{ fontSize: 9.5 }}>{r.label}</text>
              ))}
            </g>
          ))}

          {series.map((s) => (
            <g key={s.id}>
              <path d={path(s.get)} fill="none" stroke="hsl(var(--card))" strokeWidth="4"
                    strokeLinejoin="round" opacity={0.8} />
              <path d={path(s.get)} fill="none" stroke={`hsl(var(--series-${s.slot}))`}
                    strokeWidth={s.dashed ? 1.5 : 2} strokeDasharray={s.dashed ? "5 3" : undefined}
                    strokeLinejoin="round" strokeLinecap="round" />
            </g>
          ))}

          {hp && (
            <g pointerEvents="none">
              <line x1={sx(hp.t)} x2={sx(hp.t)} y1={PAD.t} y2={H - PAD.b}
                    stroke="hsl(var(--foreground))" strokeWidth="1" opacity={0.35} />
              {series.map((s) => {
                const v = s.get(hp)
                return v === null ? null : (
                  <circle key={s.id} cx={sx(hp.t)} cy={sy(v)} r={s.dashed ? 3.5 : 4.5}
                          fill={s.dashed ? "hsl(var(--card))" : `hsl(var(--series-${s.slot}))`}
                          stroke={s.dashed ? `hsl(var(--series-${s.slot}))` : "hsl(var(--card))"}
                          strokeWidth="2" />
                )
              })}
            </g>
          )}
        </svg>

        {hp && (
          <div role="status" ref={follow.tip}
               className="pointer-events-none absolute top-2 z-10 min-w-[220px] rounded-md border bg-popover
                          px-2.5 py-2 text-[11.5px] text-popover-foreground shadow-md"
               style={xPct > 55 ? { right: `${100 - xPct + 1.5}%` } : { left: `${xPct + 1.5}%` }}>
            {tip(hp)}
          </div>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
        {series.map((s) => (
          <span key={s.id} className="inline-flex items-center gap-1.5">
            <LineKey slot={s.slot} dashed={s.dashed} /> {s.label}
          </span>
        ))}
        <span>· hover or use ← → to read a point</span>
      </div>
    </div>
  )
}
