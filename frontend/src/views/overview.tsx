import { Badge } from "@/components/ui/badge"
import { ClusterName } from "@/components/cloud-icon"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { StatCard } from "@/components/stat-card"
import { BarScaleNote, SpendBar, SpendLegend } from "@/components/spend-bar"
import { SEVERITY, isSaving, isStability, summarise } from "@/lib/fleet"
import { shortDateTime, usd0, windowLabel, windowShortened } from "@/lib/utils"
import type { Period, Severity } from "@/types"

export function Overview({
  period,
  uncollected,
  unavailable = [],
  discount,
  onOpen,
}: {
  period: Period
  uncollected: string[]
  /** Clusters in the index whose file for this period could not be used. */
  unavailable?: { env: string; reason: string }[]
  discount: number
  onOpen: (view: string, env?: string) => void
}) {
  const t = summarise(period)
  const max = Math.max(...period.clusters.map((c) => c.currentRunRate), 1)
  const savers = t.actions.filter(isSaving).sort((a, b) => a.delta - b.delta)

  const severityCounts = t.actions.filter(isStability).reduce<Record<string, number>>((acc, a) => {
    acc[a.severity] = (acc[a.severity] ?? 0) + 1
    return acc
  }, {})

  // A cluster whose prices did not resolve adds $0 to every total above it
  // without saying so; the fleet figures must carry that caveat up front.
  const flagged = period.clusters.filter((c) => (c.exportWarnings ?? []).length > 0)

  return (
    <div className="grid gap-4">
      {flagged.length > 0 && (
        <Card className="animate-rise border-status-warning/60 p-4 text-[12.5px]">
          <strong className="font-semibold text-status-warning">
            {flagged.length} cluster(s) need attention before these totals are read:
          </strong>
          <div className="mt-1.5 grid gap-1">
            {flagged.map((c) => (
              <div key={c.env}>
                <button className="inline-flex font-mono underline underline-offset-2" onClick={() => onOpen("clusters", c.env)}>
                  <ClusterName cluster={c} inherit />
                </button>
                <span className="text-muted-foreground"> — {(c.exportWarnings ?? []).join(" ")}</span>
              </div>
            ))}
          </div>
        </Card>
      )}
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard big label="Fleet run-rate" value={t.spend} sub={`${period.clusters.length} clusters collected`} delayMs={20} />
        <StatCard
          big
          label="Identified savings"
          value={t.net}
          tone="text-series-1"
          sub={`${usd0(t.net * 12)} / year · ${((t.net / t.spend) * 100).toFixed(1)}%`}
          delayMs={70}
        />
        <StatCard big label="Projected run-rate" value={t.projected} sub="after every ready action" delayMs={120} />
        <StatCard label="Latent ceiling" value={t.latent} tone="text-status-serious" sub="empty groups at max_size" delayMs={170} />
      </div>

      <Card className="animate-rise" style={{ animationDelay: "180ms" }}>
        <CardHeader className="flex-row flex-wrap items-baseline justify-between gap-3">
          <div>
            <CardTitle>Monthly compute by cluster</CardTitle>
            <CardDescription className="mt-1">
              List price less {Math.round(discount * 100)}% enterprise discount. Savings Plans are not
              modelled, so these are ceilings.
            </CardDescription>
          </div>
          <SpendLegend />
        </CardHeader>
        <CardContent>
          <div className="grid gap-3">
            {period.clusters.map((c, i) => (
              <div key={c.env} className="grid grid-cols-[minmax(84px,128px)_1fr] items-center gap-3">
                <button
                  onClick={() => onOpen("clusters", c.env)}
                  className="min-w-0 text-left font-mono text-[12.5px] underline decoration-border underline-offset-[3px] hover:decoration-foreground"
                >
                  <ClusterName cluster={c} inherit className="max-w-full" />
                  <span className="block text-[10.5px] text-muted-foreground no-underline" title="when this cluster's data was collected (UTC)">
                    {shortDateTime(c.collectedAt)}
                  </span>
                  {windowShortened(c) && (
                    <span
                      className="block text-[10.5px] text-status-warning no-underline"
                      title="the metrics store held less history than was asked for; this cluster's recommendations rest on the shorter window"
                    >
                      {windowLabel(c)} of data
                    </span>
                  )}
                </button>
                <div className="flex min-w-0 items-center gap-2.5">
                  <SpendBar keep={c.currentRunRate + c.net} save={-c.net} max={max} delayMs={220 + i * 80} />
                  <div className="min-w-[70px] text-right font-mono text-xs tnum">{usd0(c.currentRunRate)}</div>
                  <div className="min-w-[62px] text-right font-mono text-xs text-series-1 tnum">{usd0(c.net)}</div>
                </div>
              </div>
            ))}
          </div>
          <BarScaleNote max={max} extra="Columns are current spend, then the reducible amount." />
        </CardContent>
      </Card>

      <div className="grid gap-3 lg:grid-cols-2">
        <Card className="animate-rise" style={{ animationDelay: "230ms" }}>
          <CardHeader>
            <CardTitle>Biggest opportunities</CardTitle>
            <CardDescription>Across every collected cluster, ranked by monthly impact.</CardDescription>
          </CardHeader>
          <CardContent className="grid gap-2">
            {savers.slice(0, 5).map((a, i) => (
              <button key={i} onClick={() => onOpen("scenarios", a.env)} className="flex w-full items-center gap-2.5 text-left">
                <span className="min-w-[70px] font-mono text-[11px] text-muted-foreground">
                  {a.env}
                </span>
                <span className="min-w-0 flex-1 truncate text-[12.5px]">
                  <strong className="font-semibold">{a.subject}</strong>
                  <span className="text-muted-foreground"> · {a.summary}</span>
                </span>
                <span className="font-mono text-[12.5px] font-semibold text-series-1 tnum">{usd0(a.delta)}</span>
              </button>
            ))}
          </CardContent>
        </Card>

        <Card className="animate-rise" style={{ animationDelay: "270ms" }}>
          <CardHeader>
            <CardTitle>Stability findings</CardTitle>
            <CardDescription>Workloads above their memory request. These cost nothing to fix.</CardDescription>
          </CardHeader>
          <CardContent className="grid gap-2">
            {(["outage", "no-headroom", "correctness"] as Severity[])
              .filter((k) => severityCounts[k])
              .map((k) => (
                <div key={k} className="flex items-center gap-2.5">
                  <span className={`h-2 w-2 shrink-0 rounded-full bg-current ${SEVERITY[k].className}`} aria-hidden />
                  <span className="flex-1 text-[13px]">{SEVERITY[k].label}</span>
                  <span className="font-mono font-semibold tnum">{severityCounts[k]}</span>
                </div>
              ))}
            <Button variant="outline" size="sm" className="mt-2 justify-self-start" onClick={() => onOpen("workloads")}>
              Review stability fixes
            </Button>
          </CardContent>
        </Card>
      </div>

      {unavailable.length > 0 && (
        <Card className="animate-rise border-status-warning" style={{ animationDelay: "300ms" }}>
          <CardHeader>
            <CardTitle>
              {unavailable.length} cluster{unavailable.length === 1 ? "" : "s"} could not be loaded
            </CardTitle>
            <CardDescription>
              Left out of every figure on this page. Each cluster has its own file, so the rest are unaffected.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-1 text-[12.5px]">
            {unavailable.map((u) => (
              <div key={u.env}>
                <span className="font-mono font-medium">{u.env}</span>{" "}
                <span className="text-muted-foreground">— {u.reason}</span>
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      <Card className="animate-rise" style={{ animationDelay: "310ms" }}>
        <CardHeader>
          <CardTitle>{uncollected.length} clusters not yet collected</CardTitle>
          <CardDescription>
            Fleet figures cover only the {period.clusters.length} collected clusters. Nothing here estimates the
            spend or savings of the rest.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-1.5">
          {uncollected.map((e) => (
            <Badge key={e} variant="secondary" className="font-mono">{e}</Badge>
          ))}
        </CardContent>
      </Card>
    </div>
  )
}
