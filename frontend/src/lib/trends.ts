import { trendUrl } from "@/lib/data-paths"
import type { TrendSeries } from "@/components/trend-chart"

/** Bumped in trends.py whenever a change alters the numbers a trend reports.
 *  A file below this was collected by a method that no longer agrees with the
 *  recommendation, so it is refused rather than drawn. */
export const TREND_SCHEMA = 3

export interface TrendFile {
  schema?: number
  namespace: string
  workload: string
  container: string
  /** Collected in the cluster-wide batch: bands only, no per-replica sample.
   *  Containers a recommendation touches are collected one by one instead. */
  batched?: boolean
  cpu: TrendSeries
  memory: TrendSeries
}

export function trendSlug(ns: string, workload: string, container: string) {
  return `${ns}__${workload}__${container}`.replace(/[^A-Za-z0-9_.-]/g, "-")
}

/** One container's trend file, checked before it is trusted.
 *
 *  Trends are fetched per container rather than shipped with the fleet payload:
 *  a busy workload produces hundreds of pod identities over a month, and sending
 *  every series to every reader to serve the few they open is the wrong trade. */
export async function loadTrend(
  env: string, ns: string, workload: string, container: string,
): Promise<TrendFile> {
  const r = await fetch(trendUrl(env, trendSlug(ns, workload, container)), { cache: "no-store" })
  if (!r.ok) throw new Error("no trend recorded for this container")
  // A static host serving a single-page app answers an unknown path with
  // index.html and a 200, so `ok` does not mean the file exists. Check what
  // actually came back rather than handing HTML to JSON.parse.
  const body = await r.text()
  if (!body.trimStart().startsWith("{")) throw new Error("no trend recorded for this container")
  const doc = JSON.parse(body)
  // Trend files are written separately from the bundle and can lag it, so check
  // the shape rather than trusting it. A malformed file should say so, not take
  // the dialog down.
  if (!doc?.cpu?.bands || !doc?.memory?.bands) throw new Error("trend file is missing its series")
  if (!doc.cpu.bands.max?.length && !doc.memory.bands.max?.length) {
    throw new Error(
      "Thanos holds no usage series for this container over the window — its pods finish " +
      "between two scrapes, which is typical of a short CronJob run",
    )
  }
  // Stale trends are the dangerous case, not missing ones: an old file renders
  // perfectly and quietly contradicts the number beside it. The previous method
  // sampled 6-hourly and under-reported one container's peak by 2.7x, which read
  // as the recommendation being wrong.
  if ((doc.schema ?? 1) < TREND_SCHEMA) {
    throw new Error(
      `this trend was collected with an older method (v${doc.schema ?? 1}, ` +
      `now v${TREND_SCHEMA}) and may plot other workloads' pods`,
    )
  }
  return doc as TrendFile
}
