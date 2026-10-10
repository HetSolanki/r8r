import { Badge } from "@/components/ui/badge"
import { ClusterName } from "@/components/cloud-icon"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { StatCard } from "@/components/stat-card"
import { BarScaleNote, SpendBar, SpendLegend } from "@/components/spend-bar"
import { shortDateTime, usd0, windowLabel, windowShortened } from "@/lib/utils"
import type { AutoscalerEstimate, Cluster, NodeGroup, NodePressure, Period } from "@/types"
import { fleetText, typeText } from "@/lib/fleet-text"

export function Clusters({ period, env, setEnv }: { period: Period; env: string; setEnv: (e: string) => void }) {
  const c = period.clusters.find((x) => x.env === env) ?? period.clusters[0]
  const live = c.nodegroups.filter((g) => !g.empty).sort((a, b) => b.monthlyCost - a.monthlyCost)
  const empty = c.nodegroups.filter((g) => g.empty).sort((a, b) => (b.latent ?? 0) - (a.latent ?? 0))
  const latent = empty.reduce((s, g) => s + (g.latent ?? 0), 0)
  const max = Math.max(...live.map((g) => g.monthlyCost), 1)

  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap gap-2">
        {period.clusters.map((x) => (
          <Button
            key={x.env}
            size="sm"
            variant={x.env === c.env ? "default" : "outline"}
            className="font-mono"
            onClick={() => setEnv(x.env)}
          >
            <ClusterName cluster={x} />
          </Button>
        ))}
      </div>

      {windowShortened(c) && (
        <Card className="animate-rise border-status-warning/60 p-4 text-[12.5px]" role="status">
          <strong className="font-semibold text-status-warning">
            Sized from {c.windowDays} day{c.windowDays === 1 ? "" : "s"} of history, not the {c.requestedWindowDays} requested.
          </strong>
          <span className="text-muted-foreground">
            {" "}The metrics store only holds {c.windowDays} day{c.windowDays === 1 ? "" : "s"} for this cluster, so every
            recommendation below is based on that shorter window. A peak that comes round weekly or monthly may not be in it;
            re-collect once more history exists before acting on the larger changes.
          </span>
        </Card>
      )}

      <div className="grid gap-3 sm:grid-cols-3">
        <StatCard label="Run-rate" value={c.currentRunRate} sub={`${c.nodesTotal} nodes · ${c.region} · collected ${shortDateTime(c.collectedAt)} UTC · ${windowLabel(c)} of history`} delayMs={20} />
        <StatCard
          label="Net change"
          value={c.net}
          tone="text-series-1"
          sub={`${((c.net / c.currentRunRate) * 100).toFixed(1)}% of spend · following the recommendations`}
          delayMs={60}
        />
        <StatCard label="Projected" value={c.currentRunRate + c.net} sub="after ready actions" delayMs={100} />
      </div>

      <Card className="animate-rise" style={{ animationDelay: "140ms" }}>
        <CardHeader className="flex-row flex-wrap items-baseline justify-between gap-3">
          <CardTitle>Cost by node group</CardTitle>
          <SpendLegend />
        </CardHeader>
        <CardContent>
          <div className="grid gap-2.5">
            {live.map((g, i) => (
              <div key={g.name} className="grid grid-cols-[minmax(92px,150px)_1fr] items-center gap-3">
                <div className="truncate font-mono text-xs" title={g.name}>{g.name}</div>
                <div className="flex min-w-0 items-center gap-2.5">
                  <SpendBar keep={g.monthlyCost - g.saving} save={g.saving} max={max} delayMs={180 + i * 55} />
                  <div
                    className={`min-w-[62px] text-right font-mono text-xs tnum ${
                      g.saving ? "text-series-1" : "text-muted-foreground"
                    }`}
                  >
                    {g.saving ? `−${usd0(g.saving)}` : usd0(g.monthlyCost)}
                  </div>
                </div>
              </div>
            ))}
          </div>
          <BarScaleNote max={max} />
        </CardContent>
      </Card>

      <AutoscalerPanel groups={live} configured={!!c.caConfigured} autoscaler={c.autoscaler} />

      <Card className="animate-rise" style={{ animationDelay: "180ms" }}>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Node group</TableHead>
              <TableHead>Instance</TableHead>
              <TableHead className="text-right">Nodes</TableHead>
              <TableHead className="text-right">30d avg</TableHead>
              <TableHead className="text-right">min/des/max</TableHead>
              <TableHead className="text-right">CPU / RAM req</TableHead>
              <TableHead className="text-right">Cost</TableHead>
              <TableHead>Disposition</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {live.map((g) => (
              <TableRow key={g.name}>
                <TableCell className="font-mono font-medium">{g.name}</TableCell>
                <TableCell className="font-mono text-muted-foreground">{typeText(g)}</TableCell>
                <TableCell className="text-right font-mono tnum">
                  {g.nodes}
                  {g.target !== g.nodes && <span className="text-series-1"> &rarr; {g.target}</span>}
                  {(g.notReady || g.cordoned) ? (
                    <div className="text-[10.5px] text-status-critical">
                      {[g.notReady ? `${g.notReady} NotReady` : "", g.cordoned ? `${g.cordoned} cordoned` : ""]
                        .filter(Boolean).join(", ")}
                    </div>
                  ) : null}
                </TableCell>
                <TableCell className="text-right font-mono text-muted-foreground tnum" title={`30d min ${g.min30d} · peak ${g.peak30d}`}>
                  {g.avg30d}
                </TableCell>
                <TableCell className="text-right font-mono text-muted-foreground tnum">
                  {g.min}/{g.desired}/{g.max}
                </TableCell>
                <TableCell className="text-right font-mono text-muted-foreground tnum">
                  {g.cpuUtil}% / {g.memUtil}%
                </TableCell>
                <TableCell className="text-right font-mono tnum">{usd0(g.monthlyCost)}</TableCell>
                <TableCell>
                  {g.saving > 0 ? (
                    <div>
                      <Badge variant="outline" className="border-series-1 text-series-1">
                        &minus;{usd0(g.saving)}/mo
                      </Badge>
                      <div className="mt-1 text-[11.5px] text-muted-foreground">
                        {g.advice?.headline ?? g.action}
                      </div>
                      {/* The best option is often not the recommended one. It
                          used to be what this column showed, which made the
                          fleet total unreachable by following the advice. */}
                      {(g.savingIfEverything ?? 0) > g.saving + 1 && (
                        <div className="mt-0.5 text-[11px] text-muted-foreground/70">
                          up to &minus;{usd0(g.savingIfEverything!)}/mo with more steps — {g.action}
                        </div>
                      )}
                    </div>
                  ) : (
                    <span className="text-[12.5px] text-muted-foreground">
                      {g.why ?? `At floor — ${g.binding || "no change"}`}
                    </span>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>

      </Card>

      {empty.length > 0 && (
        <Card className="animate-rise" style={{ animationDelay: "220ms" }}>
          <CardHeader className="flex-row flex-wrap items-baseline justify-between gap-2.5">
            <div>
              <CardTitle>{empty.length} empty node groups</CardTitle>
              <CardDescription className="mt-1 max-w-[64ch]">
                Zero nodes, so zero cost today — not savings. This is what they would cost at their configured{" "}
                <span className="font-mono">max_size</span> if anything scheduled onto them.
              </CardDescription>
            </div>
            <div className="font-mono font-semibold text-status-serious tnum">{usd0(latent)}/mo ceiling</div>
          </CardHeader>
          <CardContent className="px-0 pb-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Node group</TableHead>
                  <TableHead>Instance</TableHead>
                  <TableHead className="text-right">max</TableHead>
                  <TableHead>Capacity</TableHead>
                  <TableHead className="text-right">Latent ceiling</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {empty.map((g) => (
                  <TableRow key={g.name}>
                    <TableCell className="font-mono">{g.name}</TableCell>
                    <TableCell className="font-mono text-muted-foreground">{g.instanceType}</TableCell>
                    <TableCell className="text-right font-mono tnum">{g.max}</TableCell>
                    <TableCell>
                      <Badge variant="secondary">{g.capacityType}</Badge>
                    </TableCell>
                    <TableCell className="text-right font-mono tnum">{usd0(g.latent ?? 0)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}
    </div>
  )
}

/** Cluster Autoscaler's own view, before any recommendation.
 *
 *  A node is removable only when it is below CA's utilisation threshold AND the
 *  node group has room above its minSize. Separating those two is the point:
 *  "CA wants to remove this and cannot" is a one-line ASG change, not a
 *  right-sizing project. */
/** Karpenter has no threshold, no minSize and no "unneeded" count: it
 *  provisions and consolidates, and the question is what fleet it ends up
 *  running. These figures come from replaying that on the recorded nodes and
 *  pods with nothing edited. */
function KarpenterPanel({ groups, title }: { groups: NodeGroup[]; title: string }) {
  const now = groups.reduce((t, g) => t + (g.autoscaler?.monthlyIfRemoved ?? 0), 0)
  return (
    <Card className="animate-rise" style={{ animationDelay: "160ms" }}>
      <CardHeader>
        <CardTitle>What {title} will do by itself</CardTitle>
        <CardDescription className="max-w-[78ch]">
          {title} replayed on today&apos;s nodes and pods, with nothing changed. It picks node counts
          and sizes itself.
        </CardDescription>
      </CardHeader>
      <CardContent className="px-0 pb-0">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Node pool</TableHead>
              <TableHead>Runs today</TableHead>
              <TableHead>After consolidation</TableHead>
              <TableHead className="text-right">Removes</TableHead>
              <TableHead className="text-right">Held by pods</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {groups.map((g) => {
              const a = g.autoscaler!
              const held = a.vetoes ?? []
              return (
                <TableRow key={g.name}>
                  <TableCell className="font-mono font-medium">{g.name}</TableCell>
                  <TableCell className="font-mono text-muted-foreground">
                    {fleetText(a.fleetToday?.length ? a.fleetToday : g.fleet) || `${g.nodes} × ${g.instanceType}`}
                  </TableCell>
                  <TableCell className="font-mono">
                    {fleetText(a.fleetAfter) || "nothing"}
                    {(a.reasons ?? []).length > 0 && (
                      <div className="text-[10.5px] text-status-warning">{a.reasons.join("; ")}</div>
                    )}
                  </TableCell>
                  <TableCell className="text-right font-mono font-semibold tnum">
                    {a.removableNodes > 0 ? (
                      <span className="text-series-1">
                        {a.removableNodes} · &minus;{usd0(a.monthlyIfRemoved)}/mo
                      </span>
                    ) : (
                      <span className="text-muted-foreground">0</span>
                    )}
                  </TableCell>
                  <TableCell className="text-right font-mono tnum">
                    {held.length > 0 ? (
                      <span className="text-status-critical"
                            title={held.map((v) => `${v.node}: ${v.reason}`).join("\n")}>
                        {held.length}
                      </span>
                    ) : "—"}
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
        {now > 0 && (
          <p className="px-6 py-3 text-[12.5px] text-muted-foreground">
            {title} removes {usd0(now)}/mo of nodes on its own, with no change from anyone.
          </p>
        )}
      </CardContent>
    </Card>
  )
}

/** Groups no autoscaler manages: nothing scales them, so nothing is simulated. */
function UnmanagedPanel({ groups, title }: { groups: NodeGroup[]; title: string }) {
  const one = groups.length === 1
  return (
    <Card className="animate-rise p-4 text-[12.5px] text-muted-foreground">
      <strong className="font-semibold text-foreground">
        Not autoscaled: <span className="font-mono">{groups.map((g) => g.name).join(", ")}</span>
      </strong>
      <p className="mt-1 max-w-[78ch]">
        {one ? "This group is" : "These groups are"} not a {title} pool, and no other autoscaler
        manages {one ? "it" : "them"}, so {one ? "its" : "their"} node count changes only when someone
        changes it: no scale-down is simulated. Resizing or reshaping {one ? "it" : "them"} by hand is
        still assessed in Recommendations.
      </p>
    </Card>
  )
}

function AutoscalerPanel({ groups, configured, autoscaler, replayFailed = false }: {
  groups: NodeGroup[]; configured: boolean; autoscaler?: Cluster["autoscaler"]
  /** These groups are the autoscaler's but its own replay could not run for
   *  them, beside groups it did replay: only they are approximated. */
  replayFailed?: boolean
}) {
  // Exports from before adapters carry no autoscaler: those are Cluster Autoscaler.
  const title = autoscaler?.title ?? "Cluster Autoscaler"
  const unmanaged = groups.filter((g) => g.autoscaled === false)
  if (unmanaged.length > 0) {
    const managed = groups.filter((g) => g.autoscaled !== false)
    return (
      <>
        {managed.length > 0 && <AutoscalerPanel groups={managed} configured={configured} autoscaler={autoscaler} />}
        <UnmanagedPanel groups={unmanaged} title={title} />
      </>
    )
  }
  const rows = groups.filter((g) => g.autoscaler)
  if (!configured || rows.length === 0) {
    return (
      <Card className="animate-rise p-4 text-[12.5px] text-muted-foreground">
        <strong className="font-semibold text-foreground">
          {title} configuration not read for this cluster.
        </strong>{" "}
        Scale-in figures assume a perfect repack rather than what the controller will actually do.
        Re-run <span className="font-mono">collect</span> with{" "}
        <span className="font-mono">--context</span> to read them off the running deployment, or pass
        them explicitly with <span className="font-mono">--ca-arg key=value</span>.
      </Card>
    )
  }
  const replayed = rows.filter((g) => g.autoscaler?.replayed)
  if (replayed.length === rows.length) {
    return <KarpenterPanel groups={rows} title={title} />
  }
  if (replayed.length > 0) {
    // Mixed: the autoscaler's own pools were replayed by its own algorithm;
    // judge each group by how it was modelled, not the cluster by its weakest.
    return (
      <>
        <KarpenterPanel groups={replayed} title={title} />
        <AutoscalerPanel groups={rows.filter((g) => !g.autoscaler?.replayed)} configured={configured}
                         autoscaler={autoscaler} replayFailed />
      </>
    )
  }
  const est = rows[0].autoscaler!
  const unchecked = est.unchecked ?? []
  /** Nodes under the utilisation threshold that a veto class still holds.
   *
   *  Counted from the vetoes themselves rather than from the difference between
   *  two totals: on db-1 the floor-aware estimate is already 0, so the
   *  subtraction reported 0 blocked while all three nodes were in fact held by
   *  pods CA will not evict. */
  const blocked = (a: AutoscalerEstimate) => (a.vetoes ?? []).length
  const now = rows.reduce((t, g) => t + (g.autoscaler?.monthlyIfRemoved ?? 0), 0)
  const ifLowered = rows.reduce((t, g) => t + (g.autoscaler?.monthlyIfFloorLowered ?? 0), 0)

  return (
    <Card className="animate-rise" style={{ animationDelay: "160ms" }}>
      <CardHeader>
        <CardTitle>
          {replayFailed
            ? `${title}, approximated: ${rows.map((g) => g.name).join(", ")}`
            : `What ${title} will do by itself`}
        </CardTitle>
        {autoscaler && !autoscaler.modelled && (
          <p className="max-w-[78ch] text-[12.5px] font-medium text-status-warning">
            {replayFailed
              ? `${title}'s own replay could not run for ${rows.length === 1 ? "this pool" : "these pools"}: these figures use`
              : `${title} is not replayed: these figures use`}{" "}
            Cluster Autoscaler&apos;s rules set to the nearest equivalent settings, and are an
            approximation.
          </p>
        )}
        <CardDescription className="max-w-[78ch]">
          Read from <span className="font-mono">{est.source}</span>. A node counts as unneeded below{" "}
          {Math.round(est.threshold * 100)}% utilisation, and is only removed if the group has room
          above its <span className="font-mono">minSize</span>. No request changes are involved.
        </CardDescription>
      </CardHeader>
      <CardContent className="px-0 pb-0">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Node group</TableHead>
              <TableHead className="text-right">Nodes</TableHead>
              <TableHead className="text-right">minSize</TableHead>
              <TableHead className="text-right">Below threshold</TableHead>
              <TableHead className="text-right">CA can remove</TableHead>
              <TableHead className="text-right">Held by floor</TableHead>
              <TableHead className="text-right">If the floor were lowered</TableHead>
              <TableHead className="text-right">Blocked by pods</TableHead>
              <TableHead className="text-right">Load after scale-down</TableHead>
              <TableHead className="text-right">Load CA cannot see</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((g) => {
              const a = g.autoscaler!
              return (
                <TableRow key={g.name}>
                  <TableCell className="font-mono font-medium">{g.name}</TableCell>
                  <TableCell className="text-right font-mono tnum">{g.nodes}</TableCell>
                  <TableCell className="text-right font-mono text-muted-foreground tnum">
                    {a.minSize ?? "—"}
                  </TableCell>
                  <TableCell className="text-right font-mono tnum">{a.unneededNodes}</TableCell>
                  <TableCell className="text-right font-mono font-semibold tnum">
                    {a.removableNodes > 0 ? (
                      <span className="text-series-1">
                        {a.removableNodes} · &minus;{usd0(a.monthlyIfRemoved)}/mo
                      </span>
                    ) : (
                      <span className="text-muted-foreground">0</span>
                    )}
                    {/* A node under the threshold that CA still will not touch
                        is the question people actually ask about this table, so
                        the reason sits next to the number rather than in a
                        footnote. */}
                    {blocked(a) > 0 && (
                      <div className="mt-0.5 text-[10.5px] font-normal text-status-warning">
                        {blocked(a)} blocked by pods CA cannot evict
                      </div>
                    )}
                  </TableCell>
                  <TableCell className="text-right font-mono tnum">
                    {a.floorBlocked > 0 ? (
                      <span className="text-status-warning">{a.floorBlocked}</span>
                    ) : (
                      "—"
                    )}
                  </TableCell>
                  <TableCell className="text-right font-mono tnum">
                    {a.monthlyIfFloorLowered > a.monthlyIfRemoved ? (
                      <span className="text-status-warning">&minus;{usd0(a.monthlyIfFloorLowered)}/mo</span>
                    ) : (
                      "—"
                    )}
                  </TableCell>
                  <TableCell className="text-right font-mono tnum">
                    {blocked(a) > 0 ? (
                      <span className="text-status-critical" title={(a.vetoes ?? [])
                        .map((v) => `${v.node}: ${v.reason}`).join("\n")}>
                        {blocked(a)}
                      </span>
                    ) : (
                      "—"
                    )}
                  </TableCell>
                  <TableCell className="text-right font-mono tnum">
                    <PressureCell rows={a.pressureAfterScaleDown} />
                  </TableCell>
                  <TableCell className="text-right font-mono text-muted-foreground tnum">
                    {a.unreservedLoad && (a.unreservedLoad.bestEffortPods || a.unreservedLoad.overRequestPods) ? (
                      <span title={`${a.unreservedLoad.bestEffortPods} pod(s) with no request or limit, ` +
                        `~${a.unreservedLoad.bestEffortMemGiB} GiB busy, 0 reserved; ` +
                        `${a.unreservedLoad.overRequestPods} pod(s) ~${a.unreservedLoad.overRequestMemGiB} GiB above their request`}>
                        {a.unreservedLoad.bestEffortPods} BE · {a.unreservedLoad.bestEffortMemGiB} GiB
                        <div className="text-[10.5px]">+{a.unreservedLoad.overRequestMemGiB} GiB over request</div>
                      </span>
                    ) : "—"}
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>

        <p className="mt-2 max-w-[92ch] px-4 text-[11.5px] text-muted-foreground">
          <span className="font-medium text-foreground">Load after scale-down</span> checks the nodes CA keeps against
          their <em>measured</em> load. CA and the scheduler place pods by requests, so a pod with no request (BestEffort)
          or one above its request lands wherever there is nominal room; this column says where that no longer fits.
        </p>
        {unchecked.length > 0 && (
          <p className="mt-2 max-w-[92ch] text-[11.5px] text-muted-foreground">
            <span className="font-medium text-status-warning">Not all checks ran.</span>{" "}
            These numbers apply the veto classes the snapshot has facts for; the
            following were unavailable, so nodes they would hold are still counted as
            removable: {unchecked.join("; ")}.
          </p>
        )}
        {est.checked && est.checked.length > 0 && (
          <p className="mt-1 max-w-[92ch] text-[11px] text-muted-foreground/70">
            Checked per pod on every candidate node: {est.checked.join(" · ")}.
          </p>
        )}
        <div className="grid gap-1 border-t p-4 text-[12px] text-muted-foreground">
          <div>
            <strong className="font-semibold text-foreground">
              &minus;{usd0(now)}/mo available with no change at all
            </strong>{" "}
            · <strong className="font-semibold text-foreground">&minus;{usd0(ifLowered)}/mo</strong> if
            every group&rsquo;s <span className="font-mono">minSize</span> were lowered to match what CA
            already considers unneeded.
          </div>
          {est.reasons.map((r, i) => (
            <div key={i}>· {r}</div>
          ))}
          <div className="mt-1">
            {autoscaler && !autoscaler.modelled
              ? `Cluster Autoscaler settings standing in for ${title}:`
              : "Deployed flags differing from upstream defaults:"}{" "}
            <span className="font-mono">
              {Object.entries(est.nonDefaultArgs).map(([k, v]) => `${k}=${v}`).join("  ")}
            </span>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

/** Nodes whose measured load will not fit once CA acts. Empty is the good answer. */
function PressureCell({ rows }: { rows?: NodePressure[] }) {
  if (!rows) return <span className="text-muted-foreground">not checked</span>
  if (rows.length === 0) return <span className="text-status-good">fits</span>
  const critical = rows.filter((r) => r.severity === "critical").length
  return (
    <span
      className={critical ? "text-status-critical" : "text-status-warning"}
      title={rows.map((r) => `${r.node} [${r.severity}]: memory ${r.memBusyGiB}/${r.memAllocGiB} GiB, ` +
        `cpu ${r.cpuBusy}/${r.cpuAlloc}` + (r.bestEffortPods.length ? ` · BestEffort: ${r.bestEffortPods.join(", ")}` : ""))
        .join("\n")}
    >
      {critical ? `${critical} critical` : `${rows.length} warning`}
    </span>
  )
}
