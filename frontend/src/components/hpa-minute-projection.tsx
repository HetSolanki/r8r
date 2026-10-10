import { useMemo, useState } from "react"
import { MIN_FLOOR, Panel, Tile, tipAt } from "@/components/hpa-projection"
import {
  checkFixtures, describeRules, replay, rleDecode, type HpaFile,
} from "@/lib/hpa-replay"
import { cn } from "@/lib/utils"
import { useFollowY } from "@/lib/follow-y"
import type { ReplicaChange } from "@/types"

const HOUR = 60
const pct = (v: number) => `${Math.round(v * 100)}%`
const stamp = (t: number) =>
  new Date(t * 1000).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })
const SOURCE: Record<string, string> = {
  spec: "read from the HPA spec",
  inferred: "inferred from how this HPA actually scaled",
  assumed: "assumed — the cluster could not be read and the HPA rarely scaled, so this is the "
    + "cautious policy every readable HPA in the fleet uses",
  default: "Kubernetes defaults, as the spec leaves them unset",
}

interface Bucket {
  t: number
  real: number | null
  now: number
  after: number
  uNow: number | null
  uAfter: number | null
  cpu: number | null
  overNow: number
  overAfter: number
}

/** The HPA replayed minute by minute: today's floor against the one being tried.
 *
 *  Both sides are the same controller model -- recommendation, tolerance,
 *  stabilization, the rate policies, and pods taking real minutes to become
 *  ready -- so the difference is the floor alone. What the real HPA did is the
 *  dotted line, and how closely the replay of today tracks it is stated. */
export function MinuteProjection({
  change, file, floor, onFloor,
}: {
  change: ReplicaChange
  file: HpaFile
  floor: number
  onFloor: (n: number) => void
}) {
  const [hover, setHover] = useState<number | null>(null)
  const follow = useFollowY()
  const cpu = useMemo(() => file.cpuM.map((v) => (v === null ? null : v / 1000)), [file])
  const real = useMemo(() => rleDecode(file.desired), [file])
  const drift = useMemo(() => checkFixtures(file, cpu), [file, cpu])
  const common = { target: file.target, maxReplicas: file.maxReplicas, behavior: file.behavior,
                   lagMinutes: file.lagMinutes, step: file.step }
  const requestNow = file.podRequest
  const requestAfter = change.requestAfter ?? file.podRequest
  const now = useMemo(() => replay(cpu, { ...common, podRequest: requestNow, minReplicas: file.minReplicas }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [cpu, file])
  const after = useMemo(() => replay(cpu, { ...common, podRequest: requestAfter, minReplicas: floor }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [cpu, file, floor, requestAfter])

  // The engine's own check: a replay of the floor as it was each minute,
  // against what the HPA asked for. Replaying *today's* floor against a month in
  // which someone moved it would read as the model being wrong.
  const fidelity = file.validation ?? change.validation ?? null
  const floorChanges = useMemo(() => {
    const out: { t: number; from: number; to: number }[] = []
    let prev: number | null = null
    let i = 0
    for (const [v, n] of file.minHistory ?? []) {
      if (v !== null && prev !== null && v !== prev) out.push({ t: file.start + i * file.step, from: prev, to: v })
      if (v !== null) prev = v
      i += n
    }
    return out
  }, [file])

  const buckets = useMemo(() => {
    const out: Bucket[] = []
    for (let s = 0; s < cpu.length; s += HOUR) {
      const b: Bucket = { t: file.start + s * file.step, real: null, now: 0, after: 0,
                          uNow: null, uAfter: null, cpu: null, overNow: 0, overAfter: 0 }
      for (let i = s; i < Math.min(s + HOUR, cpu.length); i++) {
        const r = real[i]
        if (r !== null && r !== undefined) b.real = Math.max(b.real ?? 0, r)
        b.now = Math.max(b.now, now.replicas[i])
        b.after = Math.max(b.after, after.replicas[i])
        const un = now.util[i], ua = after.util[i], c = cpu[i]
        if (un !== null) { b.uNow = Math.max(b.uNow ?? 0, un); if (un > 1) b.overNow++ }
        if (ua !== null) { b.uAfter = Math.max(b.uAfter ?? 0, ua); if (ua > 1) b.overAfter++ }
        if (c !== null) b.cpu = Math.max(b.cpu ?? 0, c)
      }
      out.push(b)
    }
    return out
  }, [cpu, real, now, after, file])

  const hb = hover !== null ? buckets[hover] : null
  const freed = now.avg_replicas * requestNow - after.avg_replicas * requestAfter
  const addedOver = after.over_request_minutes - now.over_request_minutes
  const lo = Math.min(MIN_FLOOR, change.suggestedMin)
  const hi = Math.max(file.minReplicas, change.suggestedMin, floor)

  return (
    <div className="grid gap-3">
      {drift.length > 0 && (
        <p className="rounded-md border border-status-critical/40 bg-status-critical/[0.06] px-2.5 py-1.5 text-[12px]">
          <strong className="font-semibold text-status-critical">This replay disagrees with the engine.</strong>{" "}
          Treat the figures below as unreliable: {drift[0]}
        </p>
      )}

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px]">
        <label htmlFor={`floor-${change.hpa}`} className="font-medium">Try minReplicas</label>
        <input id={`floor-${change.hpa}`} type="range" min={lo} max={hi} step={1} value={floor}
               onChange={(e) => onFloor(Number(e.target.value))}
               className="w-48 accent-[hsl(var(--series-2))]" />
        <span className="font-mono tnum font-semibold">{floor}</span>
        <span className="text-muted-foreground">
          current {file.minReplicas} · recommended {change.suggestedMin} · max {file.maxReplicas}
        </span>
        {floor !== change.suggestedMin && (
          <button type="button" onClick={() => onFloor(change.suggestedMin)}
                  className="text-muted-foreground underline underline-offset-2 hover:text-foreground">
            back to recommended
          </button>
        )}
      </div>

      <div className="grid gap-2 sm:grid-cols-3">
        <Tile label={`Today · minReplicas ${file.minReplicas}`} slot={1}>
          <div><span className="font-mono tnum font-semibold">{now.avg_replicas.toFixed(1)}</span>
            <span className="text-muted-foreground"> replicas avg · {now.peak_replicas} peak</span></div>
          <div><span className="font-mono tnum font-semibold">{now.over_request_minutes}</span>
            <span className="text-muted-foreground"> min above request · peak {pct(now.max_util)}</span></div>
          {fidelity && (
            <div className={cn("text-[11.5px]", fidelity.within_one < 0.8 ? "text-status-warning" : "text-muted-foreground")}>
              replay tracks the real HPA within ±1 replica {pct(fidelity.within_one)} of minutes
              (avg {fidelity.model_avg.toFixed(1)} vs real {fidelity.actual_avg.toFixed(1)})
            </div>
          )}
        </Tile>
        <Tile label={`At minReplicas ${floor}`} slot={2}>
          <div><span className="font-mono tnum font-semibold">{after.avg_replicas.toFixed(1)}</span>
            <span className="text-muted-foreground"> replicas avg · {after.peak_replicas} peak</span></div>
          <div><span className="font-mono tnum font-semibold">{after.over_request_minutes}</span>
            <span className="text-muted-foreground"> min above request · peak {pct(after.max_util)}</span></div>
          <div className="text-[11.5px] text-muted-foreground">
            longest stretch {after.longest_over_request} min
            {after.longest_over_request !== now.longest_over_request && <> (today {now.longest_over_request})</>}
          </div>
        </Tile>
        <Tile label="Difference">
          <div>
            {Math.abs(freed) < 0.05 ? <span className="font-semibold text-muted-foreground">no change</span> : (
              <>
                <span className={cn("font-mono tnum font-semibold", freed > 0 ? "text-series-1" : "text-status-serious")}>
                  {freed > 0 ? "−" : "+"}{Math.abs(freed).toFixed(1)} cores
                </span>
                <span className="text-muted-foreground"> reserved on average</span>
              </>
            )}
          </div>
          <div className={cn(addedOver > 30 ? "font-medium text-status-warning" : "text-muted-foreground")}>
            {addedOver > 0 ? `+${addedOver}` : addedOver} min above request over{" "}
            {Math.round(now.minutes / 1440)} days
          </div>
          {after.max_util > Math.max(now.max_util, 1.5) && (
            <div className="font-medium text-status-warning">
              ⚠ bursts reach {pct(after.max_util)} of request (today {pct(now.max_util)})
            </div>
          )}
        </Tile>
      </div>

      <div className="relative grid gap-2" {...follow.box}>
        <Panel
          title="Replicas"
          aria={`Replica count per hour: the real HPA, the replay of today, and the replay at minReplicas ${floor}`}
          points={buckets} step height={140} hover={hover} onHover={setHover}
          now={(b) => b.now} after={(b) => b.after} context={(b) => b.real}
          fmt={(v) => v.toFixed(0)}
          refs={[
            { value: file.minReplicas, label: `current min ${file.minReplicas}` },
            { value: floor, label: `new min ${floor}`, strong: true },
            { value: file.maxReplicas, label: `max ${file.maxReplicas}` },
          ]}
        />
        <Panel
          title="CPU per ready replica"
          aria={`Peak CPU per ready replica each hour, against the request and the HPA target of ${pct(file.target)}`}
          points={buckets} height={150} hover={hover} onHover={setHover}
          now={(b) => b.uNow} after={(b) => b.uAfter ?? 0}
          fmt={pct}
          refs={[
            { value: file.target, label: `HPA target ${pct(file.target)}`, strong: true },
            { value: 1, label: "100% of request" },
          ]}
        />
        {hb && (
          <div role="status" ref={follow.tip}
               className="pointer-events-none absolute top-6 z-10 min-w-[230px] rounded-md border bg-popover
                          px-2.5 py-2 text-[11.5px] text-popover-foreground shadow-md"
               style={tipAt(hover!, buckets.length)}>
            <div className="font-medium">{stamp(hb.t)}</div>
            <div className="mb-1 text-[10.5px] text-muted-foreground">busiest minute in this hour</div>
            {hb.cpu !== null && (
              <div className="mb-1 text-muted-foreground">
                CPU <span className="font-mono tnum text-foreground">{hb.cpu.toFixed(2)}</span> cores
                {hb.real !== null && <> · real HPA <span className="font-mono tnum text-foreground">{hb.real}</span></>}
              </div>
            )}
            <div className="grid grid-cols-[auto_1fr_auto] items-center gap-x-2 gap-y-0.5">
              <i className="inline-block h-2 w-2 rounded-[2px] bg-series-1" />
              <span>At min {file.minReplicas}</span>
              <span className="text-right font-mono tnum">
                {hb.now} · {hb.uNow !== null ? pct(hb.uNow) : "—"}
              </span>
              <i className="inline-block h-2 w-2 rounded-[2px] bg-series-2" />
              <span>At min {floor}</span>
              <span className="text-right font-mono tnum">
                {hb.after} · {hb.uAfter !== null ? pct(hb.uAfter) : "—"}
              </span>
            </div>
            {(hb.overNow > 0 || hb.overAfter > 0) && (
              <div className="mt-1 text-muted-foreground">
                above request {hb.overNow} → {hb.overAfter} min this hour
              </div>
            )}
          </div>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <i className="inline-block h-2 w-2 rounded-[2px] bg-series-1" /> replay at minReplicas {file.minReplicas}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <i className="inline-block h-2 w-2 rounded-[2px] bg-series-2" /> replay at minReplicas {floor}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <svg width="16" height="6" aria-hidden="true"><line x1="0" y1="3" x2="16" y2="3"
            stroke="hsl(var(--muted-foreground))" strokeWidth="1.25" strokeDasharray="1 2.5" /></svg>
          what the real HPA asked for
        </span>
        <span>· hover or use ← → on a chart to read an hour</span>
      </div>
      {floorChanges.length > 0 && (
        <p className="text-[11.5px] text-muted-foreground">
          minReplicas changed during the window:{" "}
          {floorChanges.map((c) => `${c.from} → ${c.to} on ${new Date(c.t * 1000)
            .toLocaleDateString("en-GB", { day: "numeric", month: "short" })}`).join("; ")}.
          The dotted line follows the floor in force at the time; both replays use the one being compared.
        </p>
      )}

      <p className="max-w-[80ch] text-[11.5px] text-muted-foreground">
        Replayed minute by minute over {Math.round(now.minutes / 1440)} days of 1-minute CPU, the way the
        controller works: it asks for{" "}
        <span className="font-mono">ceil(pod CPU ÷ (pod request × {pct(file.target)}))</span>, ignores moves
        inside 10%, and is limited to{" "}
        <strong className="font-semibold">{describeRules(file.behavior.up, "up")}</strong> and{" "}
        <strong className="font-semibold">{describeRules(file.behavior.down, "down")}</strong> (
        {SOURCE[file.behavior.source] ?? file.behavior.source}). New pods take{" "}
        <strong className="font-semibold">{Math.round(file.lagMinutes)} min</strong> to become ready, measured
        from this workload&apos;s own scale-ups; until then the ready pods carry the load, which is the exposure a
        lower floor adds. Each point is the busiest minute of an hour. The real controller acts every 15 seconds
        on metrics-server samples, so small differences from the dotted line are expected.
      </p>
    </div>
  )
}
