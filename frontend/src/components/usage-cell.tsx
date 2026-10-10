import { AlertTriangle } from "lucide-react"
import { cn, gib, millicores } from "@/lib/utils"

interface UsageCellProps {
  request: number
  recommended: number | null
  avg?: number
  p95?: number
  peak?: number
  utilPct?: number | null
  /** The per-replica distribution: p50…p99, max. */
  ladder?: Record<string, number>
  /** null means no limit is set on the container. */
  limit?: number | null
  kind: "cpu" | "mem"
}

/**
 * A request change with the usage that justifies it directly underneath.
 *
 * The recommendation alone is an assertion; avg / p95 / peak is the evidence.
 * When peak already exceeds the request the row is flagged, because that is the
 * case where a cut is least safe and the number alone would not show it.
 */
export function UsageCell({
  request, recommended, avg, p95, peak, utilPct, ladder, limit, kind,
}: UsageCellProps) {
  const fmt = kind === "cpu" ? millicores : gib
  const over = typeof utilPct === "number" && utilPct > 100
  const hasUsage = avg !== undefined || p95 !== undefined || peak !== undefined
  return (
    <div className="text-right">
      <div className="font-mono tnum">
        <span className="text-muted-foreground">{fmt(request)}</span>
        {recommended !== null && (
          <>
            {" → "}
            <span className={cn("font-medium", recommended < request ? "text-series-1" : "text-status-serious")}>
              {fmt(recommended)}
            </span>
          </>
        )}
      </div>
      {hasUsage && (
        <div className="mt-0.5 font-mono text-[10.5px] text-muted-foreground tnum">
          avg {fmt(avg ?? 0)} · p95 {fmt(p95 ?? 0)} · peak {fmt(peak ?? 0)}
        </div>
      )}
      {/* p99 against p95 is the question "how much is in the tail". On
          core-trigger they are 198m and 682m -- a 3.4x jump that neither the
          p95 nor the peak on its own reveals. Shown only when the gap is wide
          enough to change a decision. */}
      {ladder?.p99 !== undefined && p95 !== undefined && ladder.p99 > p95 * 1.5 && (
        <div className="mt-0.5 font-mono text-[10.5px] text-status-warning tnum"
             title={`p50 ${fmt(ladder.p50 ?? 0)} · p75 ${fmt(ladder.p75 ?? 0)} · `
                    + `p90 ${fmt(ladder.p90 ?? 0)} · p95 ${fmt(ladder.p95 ?? 0)} · `
                    + `p99 ${fmt(ladder.p99)} · max ${fmt(ladder.max ?? 0)}`}>
          p99 {fmt(ladder.p99)} · {(ladder.p99 / p95).toFixed(1)}× p95
        </div>
      )}
      {/* What happens past the request. CPU: with no limit it never throttles
          -- the request only sets the pod's share under contention -- and that
          is what makes a tight request tolerable. Memory: a limit is an OOM kill,
          no limit leaves the pod to node-pressure eviction. A request above the
          limit is rejected by the API server, so that case is flagged. */}
      <div className="mt-0.5 font-mono text-[10.5px] text-muted-foreground tnum">
        {limit ? (
          recommended !== null && recommended > limit ? (
            <span className="font-semibold text-status-critical"
                  title="The recommended request is above the limit; the API server rejects a request greater than its limit, so the limit must be raised too">
              limit {fmt(limit)} · below the new request
            </span>
          ) : (
            <span className="text-status-warning"
                  title={kind === "cpu"
                    ? "A limit caps the container outright, so exceeding the request is a hard throttle"
                    : "Using more than the limit OOM-kills the container"}>
              limit {fmt(limit)}
            </span>
          )
        ) : (
          <span title={kind === "cpu"
            ? "No CPU limit: the request sets this pod's share under contention, it never caps it"
            : "No memory limit: the container is never OOM-killed at a cap, but using more than its request makes it a first candidate for eviction when the node runs short"}>
            no limit
          </span>
        )}
      </div>
      {typeof utilPct === "number" && (
        <div
          className={cn(
            "mt-0.5 inline-flex items-center gap-1 font-mono text-[10.5px] tnum",
            over ? "font-semibold text-status-critical" : "text-muted-foreground",
          )}
          title={over ? "Peak already exceeds the request — cutting it further is risky" : undefined}
        >
          {over && <AlertTriangle className="h-2.5 w-2.5" />}
          peak = {utilPct.toFixed(0)}% of request
        </div>
      )}
    </div>
  )
}
