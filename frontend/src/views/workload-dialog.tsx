import { useEffect, useState } from "react"
import { Button } from "@/components/ui/button"
import {
  Dialog, DialogBody, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"
import { TrendChart, type Mode } from "@/components/trend-chart"
import { gib, millicores } from "@/lib/utils"
import { loadTrend, type TrendFile } from "@/lib/trends"
import type { ContainerWithEnv } from "@/types"

/** The usage behind one container's recommendation. */
export function WorkloadDialog({
  container,
  onClose,
}: {
  container: ContainerWithEnv | null
  onClose: () => void
}) {
  const [trend, setTrend] = useState<TrendFile | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [mode, setMode] = useState<Mode>("spread")

  useEffect(() => {
    if (!container) return
    setTrend(null)
    setError(null)
    loadTrend(container.env, container.ns, container.workload, container.container)
      .then(setTrend)
      .catch((e: Error) => setError(e.message))
  }, [container])

  if (!container) return null
  const hasReplicas = (trend?.cpu.sample.length ?? 0) > 1

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            <span className="font-mono">{container.workload}</span>
            <span className="text-muted-foreground"> · {container.container}</span>
          </DialogTitle>
          <DialogDescription>
            {container.kind && <>{container.kind} · </>}
            {container.env} · {container.ns} · {container.nodegroup} ·{" "}
            {container.replicas} replica{container.replicas === 1 ? "" : "s"}
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="grid gap-5">
          {/* Everything known about why this container is or is not changed --
              the report's JSON carries it, so the console shows it. */}
          <div className="grid gap-2 rounded-md border p-3 text-[12.5px]">
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
              <span className={container.actionable === false ? "font-medium text-status-warning" : "font-medium text-status-good"}>
                {container.actionable === false ? "No change recommended" : "Change recommended"}
              </span>
              <span className="text-muted-foreground">
                CPU limit <span className="font-mono">{container.cpuLimit ? `${Math.round(container.cpuLimit * 1000)}m` : "none"}</span>
                {" · "}memory limit <span className="font-mono">{container.memLimit ? `${container.memLimit.toFixed(2)} GiB` : "none"}</span>
                {container.memOomKilled ? <span className="text-status-critical"> · OOMKilled in the window</span> : null}
              </span>
            </div>
            {container.basis && <div className="font-mono text-[11.5px] text-muted-foreground">{container.basis}</div>}
            {(container.blockers ?? []).map((b) => (
              <div key={b.code} className="grid gap-0.5">
                <span className={
                  b.severity === "unsafe" ? "font-mono text-[11.5px] text-status-critical"
                    : b.severity === "advisory" ? "font-mono text-[11.5px] text-muted-foreground"
                      : "font-mono text-[11.5px] text-status-warning"}>
                  {b.code} · {b.severity}
                </span>
                <span className="text-[12px] text-muted-foreground">{b.detail}</span>
              </div>
            ))}
          </div>

          <div className="flex flex-wrap gap-2">
            {MODES.map((m) => (
              <Button key={m.key} size="sm"
                      variant={mode === m.key ? "default" : "outline"}
                      disabled={m.key === "replicas" && !hasReplicas}
                      onClick={() => setMode(m.key)}>
                {m.label}
              </Button>
            ))}
          </div>

          {error && (
            <p className="rounded-md border px-3 py-2 text-[12.5px] text-muted-foreground">
              {error}. Trends are collected per cluster for the containers a recommendation
              touches. Generate them with{" "}
              <span className="font-mono">
                python3 -m rightsizer.cli --env {container.env} trends
              </span>
              .
            </p>
          )}
          {!trend && !error && (
            <p className="text-[12.5px] text-muted-foreground">Loading usage…</p>
          )}

          {trend && (
            <>
              <section>
                <Heading>
                  CPU
                  <Values>
                    request {millicores(container.cpuReq)}
                    {container.cpuRec !== null && <> → {millicores(container.cpuRec)}</>}
                    {" · per replica: p95 "}{millicores(container.cpuP95 ?? 0)}
                    {" · peak "}{millicores(container.cpuPeak ?? 0)}
                  </Values>
                </Heading>
                <TrendChart series={trend.cpu} mode={mode}
                            request={container.cpuReq} recommended={container.cpuRec} />
              </section>

              <section>
                <Heading>
                  Memory
                  <Values>
                    request {gib(container.memReq)}
                    {container.memRec !== null && <> → {gib(container.memRec)}</>}
                    {" · per replica: p95 "}{gib(container.memP95 ?? 0)}
                    {" · peak "}{gib(container.memPeak ?? 0)}
                  </Values>
                </Heading>
                <TrendChart series={trend.memory} mode={mode}
                            request={container.memReq} recommended={container.memRec} />
              </section>

              <p className="max-w-[80ch] text-[12px] text-muted-foreground">
                {mode === "spread" && (
                  <>
                    Each line is taken <strong className="font-semibold">across replicas</strong> at
                    that point, so the gap between the hottest and the median is the skew a single
                    per-replica number hides. The figures above run the other way — over{" "}
                    <strong className="font-semibold">time</strong>, for one replica — so the
                    header's p95 and the chart's "p95 across replicas" are different statistics and
                    will not match. CPU is sized from p95 because it throttles; memory from the peak
                    because it is OOMKilled.
                  </>
                )}
                {mode === "replicas" && (
                  <>
                    The individual pods behind those bands, up to eight of them. This answers what
                    the bands cannot: whether the top line is{" "}
                    <strong className="font-semibold">one</strong> hot replica every time — which
                    argues for fixing that pod — or a different one each time, which argues for
                    sizing them all.
                  </>
                )}
                {mode === "total" && (
                  <>
                    Every replica summed — what the node group must hold. This is{" "}
                    <strong className="font-semibold">not</strong> the hottest replica times the
                    replica count: replicas rarely peak together, and the difference is why sizing
                    capacity from per-replica figures over-provisions.
                  </>
                )}{" "}
                {trend.batched && (
                  <>
                    Individual replica lines are collected only for containers a recommendation
                    changes; this one shows the bands and the total.{" "}
                  </>
                )}
                {(trend.cpu.podIdentities ?? 0) > container.replicas * 2 && (
                  <>
                    {trend.cpu.podIdentities} distinct pod identities appeared over the window against{" "}
                    {container.replicas} replicas running — this workload churns through pods, so an
                    individual line is a pod's lifetime, not a replica's.
                  </>
                )}
              </p>
            </>
          )}
        </DialogBody>
      </DialogContent>
    </Dialog>
  )
}

const MODES: { key: Mode; label: string }[] = [
  { key: "spread", label: "Spread across replicas" },
  { key: "replicas", label: "Each replica" },
  { key: "total", label: "All replicas summed" },
]

function Heading({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="mb-2 flex flex-wrap items-baseline gap-x-3 text-[11px] font-semibold uppercase tracking-[0.05em] text-muted-foreground">
      {children}
    </h3>
  )
}

function Values({ children }: { children: React.ReactNode }) {
  return <span className="font-mono text-[11px] font-normal normal-case tracking-normal">{children}</span>
}
