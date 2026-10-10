import { useState } from "react"
import { ArrowRight } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { cn, usd0 } from "@/lib/utils"
import type { Fleet, NodeGroup } from "@/types"

/** Two windows side by side.
 *
 *  This is the only view that can say whether a past recommendation was any
 *  good: pick the window before a change and the window after it, and read the
 *  node counts and spend that actually resulted. Predicted-versus-realised is
 *  the measurement the rest of the tool cannot make about itself. */
export function Compare({ fleet }: { fleet: Fleet }) {
  const [leftId, setLeftId] = useState(fleet.periods[fleet.periods.length - 1].id)
  const [rightId, setRightId] = useState(fleet.periods[0].id)
  const left = fleet.periods.find((p) => p.id === leftId)!
  const right = fleet.periods.find((p) => p.id === rightId)!

  const envs = Array.from(
    new Set([...left.clusters, ...right.clusters].map((c) => c.env)),
  ).sort()
  const [env, setEnv] = useState(envs[0])

  const l = left.clusters.find((c) => c.env === env)
  const r = right.clusters.find((c) => c.env === env)

  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <PeriodPicker fleet={fleet} value={leftId} onChange={setLeftId} label="Before" />
        <ArrowRight className="h-4 w-4 shrink-0 text-muted-foreground" />
        <PeriodPicker fleet={fleet} value={rightId} onChange={setRightId} label="After" />
        <span className="mx-1 h-5 w-px bg-border" />
        {envs.map((e) => (
          <Button
            key={e}
            size="sm"
            variant={e === env ? "default" : "outline"}
            className="font-mono"
            onClick={() => setEnv(e)}
          >
            {e}
          </Button>
        ))}
      </div>

      {!l || !r ? (
        <Card className="p-6 text-sm text-muted-foreground">
          <strong className="font-semibold text-foreground">{env}</strong> was not collected in both
          windows, so there is nothing to compare. Run{" "}
          <span className="font-mono">
            rightsizer --env {env} --end &lt;date&gt; run
          </span>{" "}
          for the missing window.
        </Card>
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-3">
            <DeltaCard label="Run-rate" before={l.currentRunRate} after={r.currentRunRate} />
            <DeltaCard label="Nodes" before={l.nodesTotal} after={r.nodesTotal} money={false} />
            <DeltaCard
              label="Still identified"
              before={Math.abs(l.net)}
              after={Math.abs(r.net)}
              invert
            />
          </div>

          <Card className="animate-rise">
            <CardHeader>
              <CardTitle>Node groups, before and after</CardTitle>
              <CardDescription>
                {left.label} → {right.label}. A group that shrank is what the change actually did; the
                predicted column is what the tool said at the time.
              </CardDescription>
            </CardHeader>
            <CardContent className="px-0 pb-0">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Node group</TableHead>
                    <TableHead className="text-right">Nodes before</TableHead>
                    <TableHead className="text-right">Predicted then</TableHead>
                    <TableHead className="text-right">Nodes after</TableHead>
                    <TableHead className="text-right">Actual Δ</TableHead>
                    <TableHead className="text-right">Predicted Δ</TableHead>
                    <TableHead className="text-right">Accuracy</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {pairs(l.nodegroups, r.nodegroups).map(([name, before, after]) => {
                    if (!before || before.empty) return null
                    const predictedTarget = bestScenarioNodes(before)
                    const actualDelta = before.nodes - (after?.nodes ?? 0)
                    const predictedDelta = before.nodes - predictedTarget
                    const accuracy =
                      actualDelta > 0 ? (predictedDelta / actualDelta) * 100 : null
                    return (
                      <TableRow key={name}>
                        <TableCell className="font-mono font-medium">{name}</TableCell>
                        <TableCell className="text-right font-mono tnum">{before.nodes}</TableCell>
                        <TableCell className="text-right font-mono text-muted-foreground tnum">
                          {predictedTarget}
                        </TableCell>
                        <TableCell className="text-right font-mono tnum">
                          {after ? after.nodes : "—"}
                        </TableCell>
                        <TableCell
                          className={cn(
                            "text-right font-mono tnum",
                            actualDelta > 0 && "text-series-1 font-semibold",
                          )}
                        >
                          {actualDelta > 0 ? `−${actualDelta}` : actualDelta < 0 ? `+${-actualDelta}` : "—"}
                        </TableCell>
                        <TableCell className="text-right font-mono text-muted-foreground tnum">
                          {predictedDelta > 0 ? `−${predictedDelta}` : "—"}
                        </TableCell>
                        <TableCell className="text-right font-mono tnum">
                          {accuracy === null ? (
                            "—"
                          ) : (
                            <span
                              className={cn(
                                accuracy >= 80 && accuracy <= 125
                                  ? "text-status-good"
                                  : accuracy >= 50
                                    ? "text-status-warning"
                                    : "text-status-critical",
                              )}
                            >
                              {Math.round(accuracy)}%
                            </span>
                          )}
                        </TableCell>
                      </TableRow>
                    )
                  })}
                </TableBody>
              </Table>
            </CardContent>
          </Card>

          <p className="max-w-[78ch] text-xs text-muted-foreground">
            <strong className="font-semibold">Accuracy</strong> is predicted node reduction over actual.
            Under 100% means the tool was conservative; over 100% means it recommended more than was
            taken. It cannot account for workloads that left the cluster entirely — no sizing engine
            predicts a decision to stop running something.
          </p>
        </>
      )}
    </div>
  )
}

function bestScenarioNodes(g: NodeGroup): number {
  const feasible = (g.scenarios ?? []).filter((s) => s.feasible && s.key !== "asis")
  if (!feasible.length) return g.target || g.nodes
  return feasible.sort((a, b) => a.deltaUsd - b.deltaUsd)[0].nodes
}

function pairs(before: NodeGroup[], after: NodeGroup[]): [string, NodeGroup | undefined, NodeGroup | undefined][] {
  const names = Array.from(new Set([...before, ...after].map((g) => g.name))).sort()
  return names.map((n) => [n, before.find((g) => g.name === n), after.find((g) => g.name === n)])
}

function PeriodPicker({
  fleet,
  value,
  onChange,
  label,
}: {
  fleet: Fleet
  value: string
  onChange: (id: string) => void
  label: string
}) {
  return (
    <label className="flex items-center gap-2 text-[12.5px] text-muted-foreground">
      {label}
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="h-8 rounded-md border border-input bg-background px-2 text-[13px] text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {fleet.periods.map((p) => (
          <option key={p.id} value={p.id}>
            {p.label}
          </option>
        ))}
      </select>
    </label>
  )
}

function DeltaCard({
  label,
  before,
  after,
  money = true,
  invert = false,
}: {
  label: string
  before: number
  after: number
  money?: boolean
  invert?: boolean
}) {
  const delta = after - before
  const good = invert ? delta > 0 : delta < 0
  const fmt = (n: number) => (money ? usd0(n) : String(Math.round(n)))
  return (
    <Card className="animate-rise p-4">
      <div className="text-[10.5px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">
        {label}
      </div>
      <div className="mt-1.5 flex items-baseline gap-2 font-mono tnum">
        <span className="text-muted-foreground">{fmt(before)}</span>
        <ArrowRight className="h-3 w-3 text-muted-foreground" />
        <span className="text-[clamp(18px,3vw,24px)] font-semibold tracking-[-0.02em]">{fmt(after)}</span>
      </div>
      <div className={cn("mt-0.5 text-xs", good ? "text-series-1" : "text-muted-foreground")}>
        {delta === 0 ? "unchanged" : `${delta < 0 ? "−" : "+"}${fmt(Math.abs(delta))}`}
      </div>
    </Card>
  )
}
