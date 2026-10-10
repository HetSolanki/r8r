import { useMemo, useState } from "react"
import { ClusterName } from "@/components/cloud-icon"
import { Badge } from "@/components/ui/badge"
import { VERDICT } from "@/components/storage-panel"
import { NET_VERDICT } from "@/components/network-rows"
import { Button } from "@/components/ui/button"
import { Card, CardHeader, CardTitle } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { ScenarioDialog } from "@/views/scenario-dialog"
import { WorkloadDialog } from "@/views/workload-dialog"
import { cn, usd0, windowLabel, windowShortened } from "@/lib/utils"
import { sizeText, typeText } from "@/lib/fleet-text"
import type { ContainerWithEnv, Driver, NodeGroup, Period, Scenario, Changes } from "@/types"

/** Recommendations: node groups with something to do, and their options.
 *
 *  Groups with nothing worth changing are left out entirely rather than listed
 *  with a label saying so -- a page of "OK" rows is a page the reader has to
 *  skip past to reach the ones that matter.
 */
export function Scenarios({ period }: { period: Period }) {
  const [env, setEnv] = useState(period.clusters[0]?.env ?? "")
  const [open, setOpen] = useState<{ group: NodeGroup; scenario: Scenario } | null>(null)
  // The trend replaces the option dialog rather than stacking on it, and the
  // option dialog comes back when the trend closes -- a reader who opened a
  // chart to check one container has not finished with the list behind it.
  const [trend, setTrend] = useState<ContainerWithEnv | null>(null)
  const [resume, setResume] = useState<{ group: NodeGroup; scenario: Scenario } | null>(null)
  const cluster = period.clusters.find((c) => c.env === env) ?? period.clusters[0]

  function showTrend(d: Driver) {
    // The driver row is a summary; the full container record carries the
    // fields the trend needs, so it is looked up rather than reconstructed.
    const full = cluster.containers.find(
      (c) => c.ns === d.ns && c.workload === d.workload && c.container === d.container,
    )
    if (!full) return
    setResume(open)
    setOpen(null)
    setTrend({ ...full, env: cluster.env })
  }

  const groups = useMemo(
    () =>
      cluster.nodegroups
        .filter((g) => !g.empty && g.advice && (g.scenarios ?? []).some((s) => s.key !== "asis"))
        .sort((a, b) => a.advice!.monthlyDelta - b.advice!.monthlyDelta),
    [cluster],
  )

  const skipped = cluster.nodegroups.filter(
    (g) => !g.empty && g.advice && !(g.scenarios ?? []).some((s) => s.key !== "asis"),
  )
  const total = groups.reduce((t, g) => t + g.advice!.monthlyDelta, 0)

  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center gap-2">
        {period.clusters.map((c) => (
          <Button key={c.env} size="sm" variant={c.env === cluster.env ? "default" : "outline"}
                  className="font-mono" onClick={() => setEnv(c.env)}>
            <ClusterName cluster={c} />
          </Button>
        ))}
        {cluster && (
          <span className={cn("ml-auto text-[12px]",
                              windowShortened(cluster) ? "text-status-warning" : "text-muted-foreground")}
                title="the metrics history every recommendation on this page is sized from">
            Based on {windowLabel(cluster)} of data
          </span>
        )}
      </div>

      <Card className="flex flex-wrap items-baseline justify-between gap-3 px-4 py-3 text-[13px]">
        <span>
          <strong className="font-semibold">{groups.length}</strong> node group
          {groups.length === 1 ? "" : "s"} with something to change
        </span>
        {total < 0 && (
          <span className="font-mono font-semibold text-series-1 tnum">
            {usd0(total)}<span className="font-normal text-muted-foreground"> / month</span>
          </span>
        )}
      </Card>

      <HpaCeilings groups={cluster.nodegroups ?? []} />

      {groups.map((g) => {
        const a = g.advice!
        const options = (g.scenarios ?? []).filter((s) => s.key !== "asis")
        return (
          <Card key={g.name} className="animate-rise">
            <CardHeader className="flex-row flex-wrap items-center justify-between gap-2 pb-2">
              <div className="flex flex-wrap items-baseline gap-2">
                <CardTitle className="font-mono">{g.name}</CardTitle>
                <span className="font-mono text-[12.5px] text-muted-foreground">
                  {sizeText(g)} · {usd0(g.monthlyCost)}/mo
                </span>
              </div>
              {a.findingsTotal > 0 && (
                <span className="text-[12px] text-muted-foreground">{a.findingsSummary}</span>
              )}
            </CardHeader>

            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Option</TableHead>
                  <TableHead>Instance</TableHead>
                  <TableHead className="text-right">Nodes</TableHead>
                  <TableHead className="text-right">Saving</TableHead>
                  <TableHead className="text-right">Steps</TableHead>
                  <TableHead>Why</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {options.map((s) => {
                  const recommended = s.key === a.scenarioKey
                  const alt = a.alternatives.find((x) => x.key === s.key)
                  return (
                    <TableRow key={s.key} role="button" tabIndex={0}
                      onClick={() => setOpen({ group: g, scenario: s })}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault()
                          setOpen({ group: g, scenario: s })
                        }
                      }}
                      className={cn("cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
                        recommended && "bg-series-1/5")}>
                      <TableCell>
                        <span className={cn("underline decoration-border underline-offset-[3px]",
                          recommended && "font-medium")}>
                          {s.label}
                        </span>
                        {recommended && (
                          <Badge variant="outline" className="ml-2 border-series-1 text-series-1">
                            recommended
                          </Badge>
                        )}
                        {/* This option is real, and cheaper in steps than the
                            replacement beside it -- but only once the pods
                            holding its nodes are dealt with. Saying so beats
                            dropping the row. */}
                        {s.storage && (s.storage.verdict === "unsafe" || s.storage.verdict === "warn") && (
                          <Badge variant="outline" className={cn("ml-2", VERDICT[s.storage.verdict].cls)}>
                            {VERDICT[s.storage.verdict].label}
                          </Badge>
                        )}
                        {s.network && (s.network.verdict === "unsafe" || s.network.verdict === "warn") && (
                          <Badge variant="outline" className={cn("ml-2", NET_VERDICT[s.network.verdict].cls)}
                                 title={s.network.notes.join("; ")}>
                            {NET_VERDICT[s.network.verdict].label}
                          </Badge>
                        )}
                        {s.margin && !s.margin.ok && (
                          <Badge variant="outline" className="ml-2 border-status-warning text-status-warning"
                                 title={s.margin.notes.join("; ")}>
                            no headroom
                          </Badge>
                        )}
                        {(s.drainBlocked ?? 0) > 0 && (
                          <Badge variant="outline"
                                 className="ml-2 border-status-warning text-status-warning">
                            blocked on {s.drainBlocked} node{s.drainBlocked === 1 ? "" : "s"}
                          </Badge>
                        )}
                      </TableCell>
                      <TableCell className="font-mono text-muted-foreground">{typeText(s)}</TableCell>
                      <TableCell className="text-right font-mono tnum">{s.nodes}</TableCell>
                      <TableCell className="text-right font-mono font-semibold text-series-1 tnum">
                        {usd0(s.deltaUsd)}
                      </TableCell>
                      <TableCell className="text-right font-mono tnum">
                        {(s.requires ?? []).length}
                        {s.changes && <div className="text-[10.5px] font-normal text-muted-foreground">{changesOf(s.changes)}</div>}
                      </TableCell>
                      <TableCell className="max-w-[280px] text-[12px] text-muted-foreground">
                        {(s.drainBlocked ?? 0) > 0
                          ? alt?.why ?? `CA will not evict ${(s.drainBlockers ?? []).length} workload(s) holding these nodes — fix those and this becomes a config change.`
                          : recommended ? a.reason.split(".")[0] + "." : alt?.why ?? ""}
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          </Card>
        )
      })}

      {groups.length === 0 && (
        <Card className="p-6 text-sm text-muted-foreground">
          Nothing to change on this cluster: every node group already costs less than any alternative.
        </Card>
      )}

      <p className="text-xs text-muted-foreground">
        <strong className="font-semibold">Steps</strong> is how many changes an option takes to carry
        out. Select a row for its packing, node configuration and the containers it changes.
        {skipped.length > 0 && (
          <> {skipped.length} node group{skipped.length === 1 ? " is" : "s are"} not listed —
            nothing cheaper exists for {skipped.length === 1 ? "it" : "them"}.</>
        )}
      </p>

      <ScenarioDialog
        key={open ? `${open.group.name}/${open.scenario.key}` : "closed"}
        env={cluster.env}
        collectedAt={cluster.collectedAt}
        group={open?.group ?? null}
        scenario={open?.scenario ?? null}
        asis={(open?.group.scenarios ?? []).find((s) => s.key === "asis")}
        onClose={() => setOpen(null)}
        onShowTrend={showTrend}
      />

      <WorkloadDialog
        container={trend}
        onClose={() => {
          setTrend(null)
          setOpen(resume)
          setResume(null)
        }}
      />
    </div>
  )
}

/** "25 requests · 8 HPAs · minSize" -- what an option touches, so equal
 *  savings by different routes can be told apart at a glance. */
function changesOf(c: Changes): string {
  return [
    c.requests ? `${c.requests} request${c.requests === 1 ? "" : "s"}` : "",
    c.hpas ? `${c.hpas} HPA${c.hpas === 1 ? "" : "s"}` : "",
    c.minSize ? "minSize" : "",
    c.newType ? "new node group" : "",
  ].filter(Boolean).join(" · ")
}


/** HPAs held at maxReplicas. Not a saving -- a risk the floor advice does not
 *  show, and raising a ceiling costs nothing until the load needs it. */
function HpaCeilings({ groups }: { groups: NodeGroup[] }) {
  const rows = groups.flatMap((g) => (g.hpaCeilings ?? []).map((c) => ({ ...c, group: g.name })))
    .sort((a, b) => (a.when === b.when ? 0 : a.when === "now" ? -1 : 1)
                    || Math.max(b.pinnedMinutes, b.cappedNow) - Math.max(a.pinnedMinutes, a.cappedNow))
  if (!rows.length) return null
  const hours = (m: number) => (m >= 120 ? `${Math.round(m / 60)}h` : `${m} min`)
  return (
    <Card className="animate-rise">
      <CardHeader className="pb-2">
        <CardTitle className="text-[14px]">
          {rows.length} HPA{rows.length === 1 ? "" : "s"} held at maxReplicas
        </CardTitle>
        <p className="max-w-[90ch] text-[12.5px] text-muted-foreground">
          At its ceiling an HPA stops adding pods while load keeps rising, and the ready pods run
          hotter until latency or an OOM says so. Replayed minute by minute over the window: the
          suggested ceiling covers the most it wanted for 5+ minutes in a row, with 25% headroom.
          A higher ceiling costs nothing until the load needs it — but check the node group's
          maxSize can hold the extra pods.
        </p>
      </CardHeader>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>HPA</TableHead>
            <TableHead>Node group</TableHead>
            <TableHead className="text-right">maxReplicas</TableHead>
            <TableHead>At the ceiling</TableHead>
            <TableHead>Why</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((c) => (
            <TableRow key={`${c.group}:${c.hpa}`}>
              <TableCell className="font-mono text-[12px]">
                {c.key.split("/").slice(0, 2).join("/")}
                {c.when === "after" && (
                  <Badge variant="outline" className="ml-2 border-status-warning text-status-warning"
                         title="Holds today; the recommended request cut makes the same load want more replicas">
                    after the request cut
                  </Badge>
                )}
              </TableCell>
              <TableCell className="font-mono text-[12px] text-muted-foreground">{c.group}</TableCell>
              <TableCell className="text-right font-mono tnum">
                {c.max} → <strong className="font-semibold">{c.suggestedMax}</strong>
              </TableCell>
              <TableCell className="text-[12px]">
                {c.pinnedMinutes > 0 && <div>real HPA: {hours(c.pinnedMinutes)}</div>}
                <div className="text-muted-foreground">
                  replay: {hours(c.when === "now" ? c.cappedNow : c.cappedAfter)}, wanted up to{" "}
                  {c.when === "now" ? c.peakWantedNow : c.peakWantedAfter}
                </div>
              </TableCell>
              <TableCell className="max-w-[420px] text-[12px] text-muted-foreground" title={c.basis}>
                {c.basis}
                {c.cappedAtSuggested > 0 && (
                  <> · {hours(c.cappedAtSuggested)} still at {c.suggestedMax}: short spikes it does not chase</>
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </Card>
  )
}
