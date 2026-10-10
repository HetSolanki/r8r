import { Fragment, useMemo, useState } from "react"
import { cn } from "@/lib/utils"
import { useFollowY } from "@/lib/follow-y"

export interface TrendSeries {
  bands: Record<string, [number, number][]>
  sample: { pod: string; points: [number, number][] }[]
  unit: string
  podIdentities: number
}

export type Mode = "spread" | "replicas" | "total"

/** The categorical theme, in its fixed order.
 *
 *  The order is the CVD-safety mechanism rather than a preference: these eight
 *  steps were validated as a sequence, so a line takes slot 1, 2, 3... and the
 *  list is never cycled. That cap is also why the three modes exist -- bands and
 *  individual replicas together are twelve series, and the twelve-colour palette
 *  that would need does not exist in a form anyone can tell apart.
 */
const SLOT = [1, 2, 3, 4, 5, 6, 7, 8] as const

/** Tailwind only emits class names it can see spelled out, so the legend
 *  swatches are literal -- `bg-series-${n}` compiles to nothing. */
const SWATCH = [
  "bg-series-1", "bg-series-2", "bg-series-3", "bg-series-4",
  "bg-series-5", "bg-series-6", "bg-series-7", "bg-series-8",
] as const

const BAND_LABEL: Record<string, string> = {
  max: "hottest replica",
  p95: "p95 across replicas",
  median: "median replica",
  min: "coolest replica",
}

/** A pod name minus the workload prefix -- the part that identifies the replica. */
function replicaLabel(pod: string) {
  const tail = pod.split("-").slice(-2).join("-")
  return tail.length >= 5 ? tail : pod.slice(-12)
}

/** Annotation drawn over the plot has to survive a line passing behind it.
 *
 *  The outline is a separate pass rather than `paint-order: stroke`, which is
 *  not honoured by every SVG renderer -- where it is ignored the stroke paints
 *  *over* the glyphs and the label vanishes, which is worse than no halo.
 */
function HaloText({
  x, y, anchor = "end", children,
}: {
  x: number
  y: number
  anchor?: "start" | "end"
  children: React.ReactNode
}) {
  return (
    <>
      <text x={x} y={y} textAnchor={anchor} stroke="hsl(var(--card))" strokeWidth="4.5"
            strokeLinejoin="round" fill="none" style={{ fontSize: 9.5 }}>
        {children}
      </text>
      <text x={x} y={y} textAnchor={anchor} className="fill-muted-foreground"
            style={{ fontSize: 9.5 }}>
        {children}
      </text>
    </>
  )
}

interface Line {
  key: string
  label: string
  points: [number, number][]
  slot: number
}

/**
 * Usage over the window, drawn to one scale.
 *
 * Three readings, because they answer different questions and share no axis:
 *
 * `spread` is the shape of the fleet at each instant -- max / p95 / median / min
 * taken **across** replicas. The gap between the top line and the median is the
 * skew a single per-replica number hides.
 *
 * `replicas` is the individual pods, each its own colour, for the question the
 * bands cannot answer: is the top line one hot replica every time, or a
 * different one each time?
 *
 * `total` is every replica summed -- what the node group must actually hold. It
 * is emphatically not `max × replicas`, and seeing the two together is the point.
 *
 * Clicking a legend entry isolates that line and rescales to it; clicking it
 * again restores the rest. Ctrl/Cmd-click adds or removes one without isolating.
 */
export function TrendChart({
  series,
  mode,
  request,
  recommended,
  height = 180,
}: {
  series: TrendSeries
  mode: Mode
  request?: number
  recommended?: number | null
  height?: number
}) {
  // Keyed by mode so isolating a band does not silently hide a replica line
  // after the reader switches views.
  const [shown, setShown] = useState<Record<string, string[] | null>>({})
  const visible = shown[mode] ?? null
  // The hovered timestamp. A time rather than an index, so it survives the
  // reader isolating a line or switching mode under the pointer.
  const [hoverT, setHoverT] = useState<number | null>(null)
  const follow = useFollowY()

  const lines = useMemo<Line[]>(() => {
    if (mode === "total") {
      return [{ key: "total", label: "all replicas summed", points: series.bands.total ?? [], slot: SLOT[0] }]
    }
    if (mode === "replicas") {
      return series.sample.slice(0, SLOT.length).map((s, i) => ({
        key: s.pod,
        label: replicaLabel(s.pod),
        points: s.points,
        slot: SLOT[i],
      }))
    }
    return (["max", "p95", "median", "min"] as const).map((key, i) => ({
      key,
      label: BAND_LABEL[key],
      points: series.bands[key] ?? [],
      slot: SLOT[i],
    }))
  }, [series, mode])

  const isVisible = (key: string) => visible === null || visible.includes(key)
  const drawn = lines.filter((l) => isVisible(l.key) && l.points.length > 0)

  function toggle(key: string, additive: boolean) {
    setShown((prev) => {
      const cur = prev[mode] ?? null
      if (additive) {
        const base = cur ?? lines.map((l) => l.key)
        const next = base.includes(key) ? base.filter((k) => k !== key) : [...base, key]
        // Hiding the last line would leave an empty chart with no way back, so
        // an empty selection reads as "show everything" instead.
        return { ...prev, [mode]: next.length === 0 || next.length === lines.length ? null : next }
      }
      const isolated = cur?.length === 1 && cur[0] === key
      return { ...prev, [mode]: isolated ? null : [key] }
    })
  }

  const all = lines.flatMap((l) => l.points)
  if (all.length === 0) {
    return <p className="text-[12.5px] text-muted-foreground">No usage recorded in this window.</p>
  }

  // The time axis is the collection window, never the visible lines': an
  // individual pod only exists for part of the month, and letting it set the
  // domain would silently re-scale time when the reader switches modes, making
  // two views of the same workload impossible to compare.
  const span = [
    ...Object.values(series.bands).flat().map(([t]) => t),
    ...all.map(([t]) => t),
  ]
  const x0 = Math.min(...span)
  const x1 = Math.max(...span)

  // Scale to what is on screen: isolating the median replica should zoom into
  // it, which is the reason to isolate at all.
  const ys = (drawn.length ? drawn : lines).flatMap((l) => l.points.map(([, v]) => v))
  const dataMax = Math.max(...ys) || 1

  // A request several times the peak is the normal case here -- that gap is the
  // whole argument for the recommendation. Letting it set the scale, though,
  // flattens every line into the bottom fifth of the canvas. So a threshold
  // widens the scale by at most a third; past that it is drawn at the top edge
  // with its multiple of the peak, which states the gap more plainly than a
  // squashed chart ever did.
  const cap = dataMax * 1.35
  const thresholds = mode === "total"
    ? []
    : ([
        { kind: "request" as const, value: request },
        { kind: "recommended" as const, value: recommended ?? undefined },
      ].filter((g): g is { kind: "request" | "recommended"; value: number } =>
        typeof g.value === "number" && g.value > 0))
  const y1 =
    Math.max(dataMax, ...thresholds.filter((g) => g.value <= cap).map((g) => g.value)) * 1.08 || 1

  // Room for the axis labels inside the viewBox, so nothing is clipped.
  const W = 720
  const H = height
  const padL = 52
  const padR = 12
  const padT = 10
  const padB = 24

  // Thresholds above the plotted range get their own strip at the top -- a
  // broken axis. Clamping them all to one top edge instead put request and
  // recommendation on the same pixel: 130 of 200 charts here drew a 0.25 request
  // and a 0.05 recommendation as a single line, which is precisely the
  // comparison the chart exists to make.
  const BREAK_ROW = 12
  const above = thresholds.filter((g) => g.value > y1).sort((a, b) => b.value - a.value)
  const breakH = above.length ? 5 + above.length * BREAK_ROW : 0
  const plotTop = padT + breakH
  const sx = (t: number) => padL + ((t - x0) / Math.max(x1 - x0, 1)) * (W - padL - padR)
  const sy = (v: number) => H - padB - (v / y1) * (H - padB - plotTop)
  const path = (pts: [number, number][]) =>
    pts.map(([t, v], i) => `${i ? "L" : "M"}${sx(t).toFixed(1)},${sy(v).toFixed(1)}`).join(" ")

  // Two decimals reads "0.00" for a container using five millicores, which is
  // most of them -- the precision has to follow the magnitude.
  const fmt = (v: number) =>
    v === 0 ? "0"
    : v >= 10 ? v.toFixed(0)
    : v >= 1 ? v.toFixed(1)
    : v >= 0.1 ? v.toFixed(2)
    : v >= 0.01 ? v.toFixed(3)
    : v.toFixed(4)

  // Both thresholds always get a line. One sitting far above the data is pinned
  // to the top edge rather than dropped, because "the request is somewhere up
  // there" is the most useful thing this chart says -- the label then carries
  // the true value and its multiple, so the pin cannot be misread as the value.
  //
  // That multiple is against the peak *on screen*, which is deliberately not
  // called "the peak": these series are sampled 6-hourly, so a brief spike
  // between two sample points never appears here, while the recommendation is
  // built from the snapshot's own max over the full-resolution window. For one
  // container measured here the chart peaks at 2.44 GiB against the snapshot's
  // 2.82. Same metric, different sampling -- so the wording has to say which.
  const marks = thresholds.map((g) => {
    const row = above.findIndex((a) => a.kind === g.kind)
    return {
      ...g,
      clipped: row >= 0,
      // Ordered by value inside the strip, largest at the very top, so their
      // vertical order still reads correctly even off the scale.
      y: row >= 0 ? padT + 4 + row * BREAK_ROW : sy(g.value),
    }
  })
  const req = marks.find((m) => m.kind === "request")
  const rec = marks.find((m) => m.kind === "recommended")

  // The gap between the two lines *is* the right-sizing, so it is shaded and
  // named. Reading it off two numbers and subtracting them is the work this
  // chart exists to save.
  const change =
    req && rec && Math.abs(rec.value - req.value) > req.value * 0.02
      ? {
          cut: rec.value < req.value,
          top: Math.min(req.y, rec.y),
          bottom: Math.max(req.y, rec.y),
          delta: Math.abs(rec.value - req.value),
          pct: Math.round(((rec.value - req.value) / req.value) * 100),
        }
      : null
  const changeInk = change?.cut ? "hsl(var(--status-good))" : "hsl(var(--status-warning))"

  // Threshold labels share the right margin and can land on each other -- a
  // clipped request sits at the top, and a recommendation just under the top
  // gridline sits there too. Stack them instead.
  const labels = marks
    .map((m) => ({
      key: m.kind,
      // An out-of-range threshold owns its whole row, so its label sits on the
      // line and the halo punches through it. One placed below would land on
      // the next row's line instead.
      y: m.clipped ? m.y + 3.5 : m.y - 4,
      text: m.clipped
        ? `↑ ${m.kind} ${fmt(m.value)} · ${(m.value / dataMax).toFixed(1)}× the peak shown`
        : `${m.kind} ${fmt(m.value)}`,
    }))
    .sort((a, b) => a.y - b.y)
  labels.reduce((prev, l) => {
    if (l.y - prev < 12) l.y = prev + 12
    return l.y
  }, -Infinity)

  // Every timestamp a visible line has a point at. Series share the trend's
  // step grid, so a point is read exactly rather than interpolated.
  const timeline = [...new Set(drawn.flatMap((l) => l.points.map(([t]) => t)))].sort((a, b) => a - b)
  const nearest = (t: number) =>
    timeline.reduce((best, x) => (Math.abs(x - t) < Math.abs(best - t) ? x : best), timeline[0])
  function pick(e: React.PointerEvent<SVGSVGElement>) {
    if (!timeline.length) return
    const box = e.currentTarget.getBoundingClientRect()
    const x = ((e.clientX - box.left) / box.width) * W
    setHoverT(nearest(x0 + ((x - padL) / (W - padL - padR)) * (x1 - x0)))
  }
  function keys(e: React.KeyboardEvent<SVGSVGElement>) {
    if ((e.key !== "ArrowLeft" && e.key !== "ArrowRight") || !timeline.length) return
    e.preventDefault()
    const i = hoverT === null ? -1 : timeline.indexOf(nearest(hoverT))
    const next = e.key === "ArrowLeft" ? (i < 0 ? timeline.length - 1 : i - 1) : i + 1
    setHoverT(timeline[Math.min(timeline.length - 1, Math.max(0, next))])
  }
  const at = (pts: [number, number][], t: number) => pts.find(([x]) => x === t)?.[1]
  const hover = hoverT === null || !timeline.length ? null : (() => {
    const t = nearest(hoverT)
    const rows = drawn
      .map((l) => ({ key: l.key, label: l.label, slot: l.slot, value: at(l.points, t) }))
      .filter((r): r is { key: string; label: string; slot: number; value: number } => r.value !== undefined)
      .sort((a, b) => b.value - a.value)
    return { t, rows, replicas: at(series.bands.replicas ?? [], t) }
  })()
  const share = (v: number, of: number | undefined) =>
    of && of > 0 ? `${Math.round((v / of) * 100)}%` : null
  const stamp = (t: number) =>
    new Date(t * 1000).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })

  const ticks = [0, y1 / 2, y1]
  const day = (t: number) =>
    new Date(t * 1000).toLocaleDateString("en-GB", { day: "numeric", month: "short" })
  const isolated = visible !== null && visible.length === 1

  return (
    <div className="relative" {...follow.box}>
      <svg viewBox={`0 0 ${W} ${H}`}
           className="w-full touch-none rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
           role="img" tabIndex={0}
           aria-label={`Usage over the window, ${series.unit}. Hover, or use the arrow keys, to read a point.`}
           onPointerMove={pick} onPointerDown={pick} onPointerLeave={() => setHoverT(null)}
           onKeyDown={keys} onBlur={() => setHoverT(null)}>
        {ticks.map((v, i) => (
          <g key={i}>
            <line x1={padL} x2={W - padR} y1={sy(v)} y2={sy(v)}
                  stroke="hsl(var(--border))" strokeWidth="1" />
            <text x={padL - 6} y={sy(v) + 3} textAnchor="end"
                  className="fill-muted-foreground" style={{ fontSize: 10 }}>
              {fmt(v)}
            </text>
          </g>
        ))}
        {/* The axis break: everything above this rule is out of scale, drawn on
            its own rows rather than squashed into the plot. */}
        {breakH > 0 && (
          <g>
            <line x1={padL} x2={W - padR} y1={plotTop - 3} y2={plotTop - 3}
                  stroke="hsl(var(--border))" strokeWidth="1" strokeDasharray="1 3" />
            <path d={`M${padL - 5},${plotTop + 1} l5,-4 l-5,-4`} fill="none"
                  stroke="hsl(var(--border))" strokeWidth="1.25" />
          </g>
        )}
        <text x={padL} y={H - 6} className="fill-muted-foreground" style={{ fontSize: 10 }}>
          {day(x0)}
        </text>
        <text x={W - padR} y={H - 6} textAnchor="end"
              className="fill-muted-foreground" style={{ fontSize: 10 }}>
          {day(x1)}
        </text>

        {/* Thresholds are annotation, not data, so they wear ink rather than a
            hue -- a hue here would read as another series. */}
        {/* The band between request and recommendation, behind the data: the
            height of it is the per-replica change. */}
        {change && change.bottom - change.top > 2 && (
          <>
            <rect x={padL} y={change.top} width={W - padL - padR}
                  height={change.bottom - change.top} fill={changeInk} opacity={0.09} />
            {change.bottom - change.top >= 15 && (
              <HaloText x={padL + 6} y={(change.top + change.bottom) / 2 + 3} anchor="start">
                {change.cut ? "reclaim" : "add"} {fmt(change.delta)} {series.unit} per replica ·{" "}
                {change.pct > 0 ? "+" : ""}{change.pct}%
              </HaloText>
            )}
          </>
        )}

        {/* Request is what is reserved today, recommendation is the target, so
            they are told apart by colour and dash rather than sharing one grey.
            Neither wears a series hue -- that would read as another replica. */}
        {marks.map((m) => (
          <line key={m.kind} x1={padL} x2={W - padR} y1={m.y} y2={m.y}
                stroke={m.kind === "request" ? "hsl(var(--muted-foreground))" : changeInk}
                strokeWidth={m.kind === "recommended" ? 2 : 1.25}
                strokeDasharray={m.kind === "request" ? "3 3" : "9 4"}
                opacity={m.clipped ? 0.5 : 0.95} />
        ))}

        {/* A 2px surface casing under each line keeps a crossing legible rather
            than letting two hues blend at the intersection. */}
        {drawn.map((l) => (
          <g key={l.key}>
            <path d={path(l.points)} fill="none" stroke="hsl(var(--card))"
                  strokeWidth={isolated ? 5 : 4} strokeLinejoin="round" opacity={0.85} />
            <path d={path(l.points)} fill="none" stroke={`hsl(var(--series-${l.slot}))`}
                  strokeWidth={isolated ? 2.25 : 1.75}
                  strokeLinejoin="round" strokeLinecap="round" />
          </g>
        ))}

        {/* Last, so a threshold label stays readable over whatever crosses it. */}
        {labels.map((l) => (
          <HaloText key={l.key} x={W - padR} y={l.y}>{l.text}</HaloText>
        ))}

        {hover && (
          <g pointerEvents="none">
            <line x1={sx(hover.t)} x2={sx(hover.t)} y1={plotTop} y2={H - padB}
                  stroke="hsl(var(--foreground))" strokeWidth="1" opacity={0.35} />
            {hover.rows.map((r) => (
              <circle key={r.key} cx={sx(hover.t)} cy={sy(r.value)} r={4}
                      fill={`hsl(var(--series-${r.slot}))`} stroke="hsl(var(--card))" strokeWidth="2" />
            ))}
          </g>
        )}
      </svg>

      {hover && (
        <div role="status" ref={follow.tip}
             className="pointer-events-none absolute top-1 z-10 min-w-[200px] max-w-[300px] rounded-md border
                        bg-popover px-2.5 py-2 text-[11.5px] text-popover-foreground shadow-md"
             style={sx(hover.t) / W > 0.55
               ? { right: `${100 - (sx(hover.t) / W) * 100 + 1.5}%` }
               : { left: `${(sx(hover.t) / W) * 100 + 1.5}%` }}>
          <div className="mb-1 font-medium">{stamp(hover.t)}</div>
          <div className="grid grid-cols-[auto_1fr_auto_auto] items-center gap-x-2 gap-y-0.5">
            {hover.rows.map((r) => {
              // Total is every replica summed, so its share is of the fleet's
              // reservation, not of one replica's request.
              const of = mode === "total"
                ? (request && hover.replicas ? request * hover.replicas : undefined)
                : request
              const pct = share(r.value, of)
              return (
                <Fragment key={r.key}>
                  <i className={cn("inline-block h-2 w-2 rounded-[2px]", SWATCH[r.slot - 1])} />
                  <span className={cn("truncate", mode === "replicas" && "font-mono")}>{r.label}</span>
                  <span className="text-right font-mono tnum">{fmt(r.value)}</span>
                  <span className="text-right font-mono text-muted-foreground tnum">
                    {pct ? `${pct} of req` : ""}
                  </span>
                </Fragment>
              )
            })}
          </div>
          <div className="mt-1 text-muted-foreground">
            {series.unit}
            {hover.replicas !== undefined && <> · {Math.round(hover.replicas)} replicas running</>}
            {mode === "total" && hover.replicas && hover.rows[0] && (
              <> · {fmt(hover.rows[0].value / hover.replicas)} per replica</>
            )}
            {request ? <> · request {fmt(request)}</> : null}
          </div>
        </div>
      )}

      {/* The two thresholds in words. Pixels can put them a hair apart -- or in
          the same strip when both sit above the plotted range -- so the numbers
          are stated outright rather than left to be read off the chart. */}
      {req && rec && (
        <div className="mt-1.5 flex flex-wrap items-center gap-x-2 text-[11px]">
          <span className="inline-flex items-center gap-1.5 text-muted-foreground">
            <svg width="16" height="6" aria-hidden="true" className="shrink-0">
              <line x1="0" y1="3" x2="16" y2="3" stroke="hsl(var(--muted-foreground))"
                    strokeWidth="1.25" strokeDasharray="3 3" />
            </svg>
            request {fmt(req.value)}
          </span>
          <span className="text-muted-foreground/50">→</span>
          <span className="inline-flex items-center gap-1.5 text-muted-foreground">
            <svg width="16" height="6" aria-hidden="true" className="shrink-0">
              <line x1="0" y1="3" x2="16" y2="3" stroke={changeInk}
                    strokeWidth="2" strokeDasharray="9 4" />
            </svg>
            recommended {fmt(rec.value)} {series.unit}
          </span>
          {change && (
            <span className="font-medium" style={{ color: changeInk }}>
              · {change.cut ? "reclaim" : "add"} {fmt(change.delta)} per replica (
              {change.pct > 0 ? "+" : ""}{change.pct}%)
            </span>
          )}
          {!change && (
            <span className="text-muted-foreground/70">· already sized</span>
          )}
        </div>
      )}

      {/* Three of the eight slots fall below 3:1 on the light surface, so every
          line is named here rather than left to colour alone. */}
      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]">
        {lines.map((l) => {
          const on = isVisible(l.key)
          return (
            <button key={l.key} type="button" title={l.key}
                    aria-pressed={on}
                    onClick={(e) => toggle(l.key, e.ctrlKey || e.metaKey)}
                    className={cn(
                      "inline-flex items-center gap-1.5 rounded px-1 py-0.5 transition-opacity",
                      "hover:bg-muted focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
                      on ? "text-muted-foreground" : "text-muted-foreground/40",
                    )}>
              <i className={cn("inline-block h-2 w-2 shrink-0 rounded-[2px]",
                SWATCH[l.slot - 1], on ? "" : "opacity-25")} />
              <span className={mode === "replicas" ? "font-mono" : undefined}>{l.label}</span>
            </button>
          )
        })}
        <span className="text-muted-foreground/70">· {series.unit}</span>
        {visible !== null ? (
          <button type="button" onClick={() => setShown((p) => ({ ...p, [mode]: null }))}
                  className="text-muted-foreground/70 underline underline-offset-2 hover:text-foreground">
            show all
          </button>
        ) : (
          <span className="text-muted-foreground/50">click a name to isolate it</span>
        )}
      </div>
    </div>
  )
}
