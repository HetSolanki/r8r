import { Fragment, useEffect, useMemo, useState } from "react"
import { Tile, type Ref } from "@/components/hpa-projection"
import { LineKey, MultiChart, type Series } from "@/components/multi-chart"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { loadTrend, type TrendFile } from "@/lib/trends"
import { cn, gib, millicores } from "@/lib/utils"
import type { Driver } from "@/types"

const pct = (v: number | null) => (v == null ? "—" : `${Math.round(v * 100)}%`)
const stamp = (t: number) =>
  new Date(t * 1000).toLocaleString("en-GB", {
    day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  })

type Kind = "cpu" | "mem"
/** A resource keeps its slot on every tab -- the same as the node-group chart. */
const SLOT: Record<Kind, 1 | 2> = { cpu: 1, mem: 2 }
const LABEL: Record<Kind, string> = { cpu: "CPU", mem: "Memory" }
const FMT: Record<Kind, (v: number) => string> = { cpu: millicores, mem: gib }

interface Point {
  t: number
  /** Hottest replica in the bucket, the line a request is judged against. */
  cpuMax: number | null
  cpuP95: number | null
  memMax: number | null
  memP95: number | null
}

interface Side {
  kind: Kind
  now: number
  tried: number
  rec: number | null
  limit: number | null
  peak?: number
  onChange: (v: number) => void
}

/** A container's usage over the window, read against today's request and the
 *  one being tried -- the request-side counterpart of the HPA projection.
 *
 *  One chart with a tab per resource. The values are owned by the
 *  caller, which saves and prices them, so the sliders here, the number in the
 *  table and the node count never disagree. */
export function RequestProjection({
  driver, env, cpu, mem, onCpu, onMem,
}: {
  driver: Driver
  env: string
  /** Cores and GiB being tried; null when the analysis made no recommendation. */
  cpu: number | null
  mem: number | null
  onCpu: (cores: number) => void
  onMem: (gib: number) => void
}) {
  const [trend, setTrend] = useState<TrendFile | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    setTrend(null)
    setError(null)
    loadTrend(env, driver.ns, driver.workload, driver.container)
      .then(setTrend)
      .catch((e: Error) => setError(e.message))
  }, [env, driver.ns, driver.workload, driver.container])

  const points: Point[] = useMemo(() => {
    if (!trend) return []
    const at = (b: Record<string, [number, number][]>, name: string) =>
      new Map((b[name] ?? []).map(([t, v]) => [t, v]))
    const cMax = at(trend.cpu.bands, "max"), cP95 = at(trend.cpu.bands, "p95")
    const mMax = at(trend.memory.bands, "max"), mP95 = at(trend.memory.bands, "p95")
    const stamps = [...new Set([...cMax.keys(), ...mMax.keys()])].sort((a, b) => a - b)
    return stamps.map((t) => ({
      t, cpuMax: cMax.get(t) ?? null, cpuP95: cP95.get(t) ?? null,
      memMax: mMax.get(t) ?? null, memP95: mP95.get(t) ?? null,
    }))
  }, [trend])

  if (error) return <p className="text-[12px] text-muted-foreground">No projection: {error}.</p>
  if (!trend) return <p className="text-[12px] text-muted-foreground">Loading usage…</p>
  if (points.length < 2) return <p className="text-[12px] text-muted-foreground">No usage recorded in this window.</p>

  const sides: Side[] = [
    ...(cpu != null ? [{ kind: "cpu" as Kind, now: driver.cpuReq, tried: cpu, rec: driver.cpuRec,
                         limit: driver.cpuLimit ?? null, peak: driver.cpuPeak, onChange: onCpu }] : []),
    ...(mem != null ? [{ kind: "mem" as Kind, now: driver.memReq, tried: mem, rec: driver.memRec,
                         limit: driver.memLimit ?? null, peak: driver.memPeak, onChange: onMem }] : []),
  ]
  const max = (k: Kind) => (p: Point) => (k === "cpu" ? p.cpuMax : p.memMax)
  const p95 = (k: Kind) => (p: Point) => (k === "cpu" ? p.cpuP95 : p.memP95)
  const seriesFor = (list: Side[]): Series<Point>[] => list.flatMap((sd) => [
    { id: `${sd.kind}-now`, label: `${LABEL[sd.kind]} at ${FMT[sd.kind](sd.now)} (now)`, slot: SLOT[sd.kind],
      dashed: true, get: (p: Point) => { const v = max(sd.kind)(p); return v == null || sd.now <= 0 ? null : v / sd.now } },
    { id: `${sd.kind}-after`, label: `${LABEL[sd.kind]} at ${FMT[sd.kind](sd.tried)}`, slot: SLOT[sd.kind],
      get: (p: Point) => { const v = max(sd.kind)(p); return v == null ? null : v / sd.tried } },
  ])
  const refsFor = (sd: Side): Ref[] => [
    { value: 1, label: "request", strong: true },
    ...(sd.limit && sd.tried > 0 ? [{ value: sd.limit / sd.tried, label: `limit ${FMT[sd.kind](sd.limit)}` }] : []),
  ]
  const tip = (list: Side[]) => (p: Point) => (
    <>
      <div className="font-medium">{stamp(p.t)}</div>
      <div className="mb-1 text-[10.5px] text-muted-foreground">busiest moment in this 6-hour bucket</div>
      <div className="grid grid-cols-[1fr_auto_auto] items-center gap-x-3 gap-y-0.5">
        <span />
        <span className="text-muted-foreground">now</span>
        <span className="text-muted-foreground">tried</span>
        {list.map((sd) => {
          const v = max(sd.kind)(p)
          const q = p95(sd.kind)(p)
          const after = v == null ? null : v / sd.tried
          return (
            <Fragment key={sd.kind}>
              <span className="inline-flex items-center gap-1.5">
                <LineKey slot={SLOT[sd.kind]} />
                <span>{LABEL[sd.kind]} <span className="text-muted-foreground">
                  · hottest {v == null ? "—" : FMT[sd.kind](v)}{q != null && ` · p95 ${FMT[sd.kind](q)}`}
                </span></span>
              </span>
              <span className="text-right font-mono tnum">{v == null || sd.now <= 0 ? "—" : pct(v / sd.now)}</span>
              <span className={cn("text-right font-mono tnum", (after ?? 0) > 1 && "font-semibold text-status-warning")}>
                {pct(after)}
              </span>
            </Fragment>
          )
        })}
      </div>
    </>
  )

  return (
    <div className="grid gap-4">
      <Tabs defaultValue={sides[0]?.kind}>
        <TabsList aria-label="Which resource to chart">
          {sides.map((sd) => <TabsTrigger key={sd.kind} value={sd.kind}>{LABEL[sd.kind]}</TabsTrigger>)}
        </TabsList>
        {sides.map((sd) => (
          <TabsContent key={sd.kind} value={sd.kind} className="mt-3 grid gap-3">
            <ResourceControls side={sd} driver={driver} points={points} max={max(sd.kind)} p95={p95(sd.kind)} />
            <MultiChart points={points} series={seriesFor([sd])} refs={refsFor(sd)} fmt={pct}
                        aria={`${LABEL[sd.kind]} utilisation of the hottest replica, at today's request and the one being tried`}
                        tip={tip([sd])} />
          </TabsContent>
        ))}
      </Tabs>

      <p className="max-w-[78ch] text-[11.5px] text-muted-foreground">
        Utilisation is the hottest replica&apos;s usage ÷ request. Each point is the busiest moment in a
        6-hour bucket across every pod the workload ran, so &quot;hours above request&quot; counts 6-hour
        periods in which at least one replica crossed it — the cautious reading. Usage is taken as
        unchanged: a request moves the line, not the work. CPU above its request is only throttled when
        a limit is set; memory above its request is not killed until the limit, but makes the pod a first
        candidate for eviction when its node runs short.
      </p>
    </div>
  )
}

/** One resource's slider and its now / tried / difference tiles. */
function ResourceControls({ side, driver, points, max, p95 }: {
  side: Side
  driver: Driver
  points: Point[]
  max: (p: Point) => number | null
  p95: (p: Point) => number | null
}) {
  const { kind, now, tried, rec, limit, peak, onChange } = side
  const fmt = FMT[kind]
  // The slider works in the unit the table's field uses, so a value set in one
  // lands on a whole step in the other.
  const unit = kind === "cpu" ? 1000 : 1024
  const label = LABEL[kind]
  const present = points.filter((p) => max(p) != null)

  const stats = (request: number) => {
    if (request <= 0 || !present.length) return null
    const util = present.map((p) => max(p)! / request)
    const typical = present.map((p) => (p95(p) ?? max(p)!) / request)
    const over = util.filter((u) => u > 1).length
    return {
      peak: Math.max(...util),
      typicalAvg: typical.reduce((a, b) => a + b, 0) / typical.length,
      overBuckets: over,
      overHours: over * 6,
    }
  }
  const sNow = stats(now)
  const sTried = stats(tried)

  const seen = Math.max(peak ?? 0, ...present.map((p) => max(p)!))
  const lo = Math.max(1, Math.floor(Math.min(now, rec ?? now, seen || now) * 0.5 * unit))
  const hi = Math.ceil(Math.max(now, rec ?? 0, seen, tried) * 1.25 * unit)
  const stepU = kind === "cpu" ? (hi > 2000 ? 25 : 5) : (hi > 4096 ? 64 : 16)
  const replicas = Math.max(driver.replicas, 1)
  const fleet = (tried - now) * replicas
  const overLimit = limit != null && limit > 0 && tried > limit
  const belowPeak = tried < seen
  const id = `req-${kind}-${driver.ns}-${driver.workload}-${driver.container}`

  return (
    <section className="grid gap-2" aria-label={`${label} request projection`}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px]">
        <label htmlFor={id} className="inline-flex items-center gap-1.5 font-medium">
          <LineKey slot={SLOT[kind]} /> Try {label} request
        </label>
        <input id={id} type="range" min={lo} max={hi} step={stepU}
               value={Math.round(tried * unit)}
               onChange={(e) => onChange(Number(e.target.value) / unit)}
               className={cn("w-48", kind === "cpu" ? "accent-[hsl(var(--series-1))]" : "accent-[hsl(var(--series-2))]")} />
        <span className="font-mono tnum font-semibold">{fmt(tried)}</span>
        <span className="text-muted-foreground">
          current {fmt(now)}
          {rec != null && <> · recommended {fmt(rec)}</>}
          {" "}· peak seen {fmt(seen)}
          {limit ? <> · limit {fmt(limit)}</> : " · no limit"}
        </span>
        {rec != null && Math.abs(tried - rec) > 1e-6 && (
          <button type="button" onClick={() => onChange(rec)}
                  className="text-muted-foreground underline underline-offset-2 hover:text-foreground">
            back to recommended
          </button>
        )}
      </div>

      <div className="grid gap-2 sm:grid-cols-3">
        <Tile label={`${label} now · ${fmt(now)}`}>
          <UtilLines s={sNow} total={present.length} />
        </Tile>
        <Tile label={`${label} at ${fmt(tried)}`}>
          <UtilLines s={sTried} total={present.length} />
        </Tile>
        <Tile label="Difference">
          <div>
            {Math.abs(fleet) < (kind === "cpu" ? 0.0005 : 0.001) ? (
              <span className="font-semibold text-muted-foreground">no change</span>
            ) : (
              <>
                <span className={cn("font-mono tnum font-semibold",
                  fleet < 0 ? "text-series-1" : "text-status-serious")}>
                  {fleet < 0 ? "−" : "+"}{fmt(Math.abs(fleet))}
                </span>
                <span className="text-muted-foreground"> reserved across {replicas} replica{replicas === 1 ? "" : "s"}</span>
              </>
            )}
          </div>
          {overLimit && (
            <div className="font-medium text-status-critical">
              ⚠ above the {fmt(limit!)} limit — Kubernetes rejects a request greater than its limit
            </div>
          )}
          {!overLimit && belowPeak && (
            <div className={cn(kind === "mem" || limit ? "font-medium text-status-warning" : "text-muted-foreground")}>
              {kind === "cpu"
                ? limit
                  ? "⚠ below the peak seen; the limit, not this request, is what throttles"
                  : "below the peak seen; with no limit it borrows idle CPU rather than throttling"
                : "⚠ below the peak seen: the pod runs over its reservation and is evicted first under node memory pressure"}
            </div>
          )}
          {kind === "mem" && driver.memOomKilled && (
            <div className="font-medium text-status-critical">⚠ OOMKilled in the window</div>
          )}
        </Tile>
      </div>
    </section>
  )
}

function UtilLines({ s, total }: {
  s: { peak: number; typicalAvg: number; overBuckets: number; overHours: number } | null
  total: number
}) {
  if (!s) return <div className="text-muted-foreground">no request set</div>
  return (
    <>
      <div>
        <span className="font-mono tnum font-semibold">{pct(s.typicalAvg)}</span>
        <span className="text-muted-foreground"> p95 replica, avg</span>
      </div>
      <div>
        <span className={cn("font-mono tnum font-semibold", s.peak > 1 && "text-status-warning")}>
          {pct(s.peak)}
        </span>
        <span className="text-muted-foreground"> hottest replica, peak</span>
      </div>
      <div className="text-muted-foreground">
        above request{" "}
        <span className={cn("font-mono tnum", s.overBuckets > 0 && "text-foreground")}>
          {s.overHours}h
        </span>{" "}
        of {total * 6}h
      </div>
    </>
  )
}
