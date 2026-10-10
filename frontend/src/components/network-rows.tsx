import { Badge } from "@/components/ui/badge"
import { TableCell, TableRow } from "@/components/ui/table"
import { cn } from "@/lib/utils"
import type { NetworkCheck } from "@/types"

export const NET_VERDICT: Record<NetworkCheck["verdict"], { label: string; cls: string }> = {
  ok: { label: "network fits", cls: "border-series-1 text-series-1" },
  warn: { label: "network tight", cls: "border-status-warning text-status-warning" },
  unsafe: { label: "network throttled", cls: "border-status-critical text-status-critical" },
  unknown: { label: "network not measured", cls: "border-border text-muted-foreground" },
}

const gbps = (v: number | undefined) => (v === undefined ? "—" : `${v.toFixed(2)} Gbps`)

/** Bandwidth rows for the node configuration table: the type's baseline and
 *  burst ceiling, what the busiest node carried, and -- only when it matters --
 *  why. Three columns: label, current, proposed. */
export function NetworkRows({ network }: { network: NetworkCheck }) {
  const flagged = network.verdict === "unsafe" || network.verdict === "warn"
  const lower = network.baselineBefore !== null && network.baselineAfter !== null
    && network.baselineAfter < network.baselineBefore
  return (
    <>
      <TableRow>
        <TableCell className="font-medium">
          Network bandwidth
          {network.source === "published" && (
            <span className="ml-1 text-[11px] text-muted-foreground" title="From the published EC2 table; refresh the instance cache to use EC2's own figures">
              (published)
            </span>
          )}
        </TableCell>
        <TableCell className="text-right font-mono text-muted-foreground tnum">{network.bandwidthBefore}</TableCell>
        <TableCell className={cn("text-right font-mono tnum", lower && "font-semibold")}>
          {network.bandwidthAfter}
        </TableCell>
      </TableRow>
      {network.before && network.after && (
        <>
          <TableRow>
            <TableCell className="font-medium" title="Highest 1-hour average on the busiest node -- what the baseline must carry once burst credits are spent">
              Busiest node, busiest hour
            </TableCell>
            <TableCell className="text-right font-mono text-muted-foreground tnum">{gbps(network.before.hotSustainedGbps)}</TableCell>
            <TableCell className={cn("text-right font-mono tnum",
              network.baselineAfter !== null && network.after.hotSustainedGbps > network.baselineAfter && "text-status-critical")}>
              {gbps(network.after.hotSustainedGbps)}
            </TableCell>
          </TableRow>
          <TableRow>
            <TableCell className="font-medium" title="Highest 5-minute rate on the busiest node -- above the baseline it spends burst credits">
              Busiest node, 5-minute peak
            </TableCell>
            <TableCell className="text-right font-mono text-muted-foreground tnum">{gbps(network.before.hotPeakGbps)}</TableCell>
            <TableCell className={cn("text-right font-mono tnum",
              network.baselineAfter !== null && network.after.hotPeakGbps > network.baselineAfter && "text-status-warning")}>
              {gbps(network.after.hotPeakGbps)}
            </TableCell>
          </TableRow>
        </>
      )}
      {flagged && (
        <TableRow className="hover:bg-transparent">
          <TableCell colSpan={3} className="text-[12px]">
            <Badge variant="outline" className={cn("mr-2", NET_VERDICT[network.verdict].cls)}>
              {NET_VERDICT[network.verdict].label}
            </Badge>
            <span className={network.verdict === "unsafe" ? "text-status-critical" : "text-muted-foreground"}>
              {network.notes.join("; ")}
            </span>
          </TableCell>
        </TableRow>
      )}
    </>
  )
}
