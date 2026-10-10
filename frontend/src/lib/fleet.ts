import { SCHEMA_VERSION } from "@/types"
import { attachLive } from "@/lib/live"
import { dataUrl, indexUrl, recordDataDir } from "@/lib/data-paths"
import type { ActionWithEnv, Cluster, ContainerWithEnv, Fleet, Period, Severity } from "@/types"

/** The index `export_ui.py` rebuilds from the per-cluster files. */
interface FleetIndex {
  layout: number
  generatedAt: string
  discount: number
  periods: {
    id: string; label: string; end: string | null; collectedAt: string
    clusters: { env: string; file: string; schema: number | null }[]
  }[]
  uncollected: string[]
  unreadable: { env: string; file: string; error: string }[]
}

/** A static single-page host answers a missing file with index.html and a 200,
 *  so `ok` does not mean the file exists: check what actually came back. */
async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { cache: "no-store" })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  const body = await res.text()
  if (!body.trimStart().startsWith("{")) throw new Error("not found")
  return JSON.parse(body) as T
}

/** Loads the dataset written by `export_ui.py`: the index, then every cluster
 *  file it lists, in parallel. In production these are static objects read from
 *  S3 by the same host that serves this bundle, so the UI never talks to a
 *  cluster.
 *
 *  One file per cluster and period, so one bad file costs that cluster, not the
 *  console: it is left out and listed under `unavailable`. */
export async function loadFleet(): Promise<Fleet> {
  let index: FleetIndex
  try {
    index = await getJson<FleetIndex>(indexUrl())
  } catch (e) {
    throw new Error(`Could not load ${indexUrl()} (${(e as Error).message}).`)
  }
  if (!index.layout || !Array.isArray(index.periods)) {
    throw new Error(`${indexUrl()} is not an index written by export_ui.py.`)
  }
  const unavailable: NonNullable<Fleet["unavailable"]> = index.unreadable.map((u) => ({
    env: u.env, period: u.file.split("/").pop()?.replace(/\.json$/, "") ?? "", reason: u.error,
  }))
  const periods: Period[] = await Promise.all(index.periods.map(async (p) => {
    const loaded = await Promise.all(p.clusters.map(async (entry): Promise<Cluster | null> => {
      const miss = (reason: string) => { unavailable.push({ env: entry.env, period: p.id, reason }); return null }
      // The bundle and the data deploy separately and are cached differently,
      // so a file can be older than the code reading it. An older payload does
      // not fail loudly -- it renders plausible, wrong numbers -- so check.
      if ((entry.schema ?? 0) < SCHEMA_VERSION) {
        return miss(`exported as schema ${entry.schema ?? "none"}, this build expects ${SCHEMA_VERSION}; ` +
                    `re-run export_ui.py --env ${entry.env}`)
      }
      try {
        const doc = await getJson<{ env: string; cluster: Cluster }>(dataUrl(entry.file))
        if (doc.env !== entry.env || !doc.cluster) return miss(`${entry.file} does not hold ${entry.env}`)
        // Its HPA history and trends sit beside it, in the same folder.
        recordDataDir(p.id, entry.env, entry.file)
        return doc.cluster
      } catch (e) {
        return miss(`could not load ${entry.file} (${(e as Error).message})`)
      }
    }))
    return { id: p.id, label: p.label, end: p.end, collectedAt: p.collectedAt,
             clusters: loaded.filter((c): c is Cluster => c !== null) } as Period
  }))
  const usable = periods.filter((p) => p.clusters.length > 0)
  if (!usable.length) {
    throw new Error(unavailable.length
      ? `No cluster could be loaded: ${unavailable.map((u) => `${u.env} — ${u.reason}`).join("; ")}`
      : `${indexUrl()} lists no clusters.`)
  }
  const fleet: Fleet = {
    schema: SCHEMA_VERSION, generatedAt: index.generatedAt, discount: index.discount,
    periods: usable, uncollected: index.uncollected, unavailable,
  }
  attachLive(fleet)
  return fleet
}

export const SEVERITY: Record<Severity, { label: string; className: string }> = {
  outage: { label: "Outage risk", className: "text-status-critical border-status-critical" },
  "no-headroom": { label: "No headroom", className: "text-status-serious border-status-serious" },
  correctness: { label: "Correctness", className: "text-status-warning border-status-warning" },
  routine: { label: "Routine", className: "text-status-good border-status-good" },
}

export const KIND_LABEL: Record<string, string> = {
  "scale-in": "Scale in",
  "instance-downsize": "Resize nodes",
  "memory-increase": "Raise memory",
  annotate: "Unblock drain",
  requests: "Requests",
}

export interface FleetTotals {
  spend: number
  net: number
  projected: number
  latent: number
  actions: ActionWithEnv[]
  containers: ContainerWithEnv[]
}

export function summarise(period: Period): FleetTotals {
  const spend = period.clusters.reduce((s, c) => s + c.currentRunRate, 0)
  const net = period.clusters.reduce((s, c) => s + c.net, 0)
  const latent = period.clusters.reduce(
    (s, c) => s + c.nodegroups.filter((g) => g.empty).reduce((t, g) => t + (g.latent ?? 0), 0),
    0,
  )
  return {
    spend,
    net,
    projected: spend + net,
    latent,
    actions: period.clusters.flatMap((c) => c.actions.map((a) => ({ ...a, env: c.env }))),
    containers: period.clusters.flatMap((c) => c.containers.map((x) => ({ ...x, env: c.env }))),
  }
}

/** Scenario keys in the order they should always be shown: the decomposition
 *  reads as a progression, so a stable order lets two periods be compared by eye. */
export const SCENARIO_ORDER = ["asis", "rightsize", "retype", "both"] as const

/** A saving action, i.e. one with a negative delta. */
export const isSaving = (a: { delta: number }) => a.delta < 0
/** A zero-cost fix that still matters — memory increases and drain unblockers. */
export const isStability = (a: { delta: number; severity: Severity }) =>
  a.delta === 0 && a.severity !== "routine"
