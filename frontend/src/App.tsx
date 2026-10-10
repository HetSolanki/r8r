import { useEffect, useState } from "react"
import { ArrowLeftRight, Boxes, Cloud as CloudGlyph, GitCompare, LayoutDashboard, Moon, Server, SlidersHorizontal, Sun } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { CloudIcon, cloudLabel, cloudOf } from "@/components/cloud-icon"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Compare } from "@/views/compare"
import { Scenarios } from "@/views/scenarios"
import { Simulator } from "@/views/simulator"
import { Clusters } from "@/views/clusters"
import { Overview } from "@/views/overview"
import { Workloads } from "@/views/workloads"
import { selectPeriod } from "@/lib/data-paths"
import { loadFleet, summarise } from "@/lib/fleet"
import { cn, dateSpan, shortDateTime } from "@/lib/utils"
import type { Fleet, Period } from "@/types"

const VIEWS = [
  { id: "overview", label: "Overview", Icon: LayoutDashboard },
  { id: "clusters", label: "Clusters", Icon: Boxes },
  { id: "scenarios", label: "Recommendations", Icon: GitCompare },
  { id: "simulator", label: "Simulator", Icon: SlidersHorizontal },
  { id: "workloads", label: "Workloads", Icon: Server },
  { id: "compare", label: "Compare", Icon: ArrowLeftRight },
] as const

type ViewId = (typeof VIEWS)[number]["id"]

const TITLES: Record<ViewId, { title: string; sub: string }> = {
  overview: { title: "Fleet overview", sub: "Every collected cluster, what it costs and what is reducible." },
  clusters: { title: "Clusters", sub: "Node groups, utilisation and disposition for one cluster." },
  scenarios: {
    title: "Recommendations",
    sub: "Node groups with something to change, and the options for each.",
  },
  simulator: {
    title: "What if",
    sub: "Pick the changes you would actually make and see what they free — and what they risk.",
  },
  workloads: { title: "Workload requests", sub: "Per-container request recommendations across the fleet." },
  compare: {
    title: "Before and after",
    sub: "Two windows side by side: what the tool predicted against what actually happened.",
  },
}

function useTheme() {
  const [dark, setDark] = useState(() => {
    const stored = localStorage.getItem("rs-theme")
    if (stored) return stored === "dark"
    return window.matchMedia("(prefers-color-scheme: dark)").matches
  })
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark)
    try {
      localStorage.setItem("rs-theme", dark ? "dark" : "light")
    } catch {
      /* private mode: the toggle still works for this session */
    }
  }, [dark])
  return { dark, setDark }
}

/** The cloud filter, remembered per viewer. Storage can be unavailable (a
 *  private window); the filter then simply starts at "all" each time. */
const CLOUD_KEY = "rs-cloud"
function useCloudFilter() {
  const [cloud, setCloud] = useState<string>(() => {
    try {
      return localStorage.getItem(CLOUD_KEY) ?? "all"
    } catch {
      return "all"
    }
  })
  useEffect(() => {
    try {
      localStorage.setItem(CLOUD_KEY, cloud)
    } catch {
      /* not remembered; still applied */
    }
  }, [cloud])
  return { cloud, setCloud }
}

/** Distinct clusters per cloud across every window, in a stable order. */
function cloudCounts(fleet: Fleet): [string, number][] {
  const envs = new Map<string, Set<string>>()
  for (const p of fleet.periods)
    for (const c of p.clusters) {
      const k = cloudOf(c)
      if (!envs.has(k)) envs.set(k, new Set())
      envs.get(k)!.add(c.env)
    }
  return [...envs].map(([k, s]) => [k, s.size] as [string, number]).sort((a, b) => a[0].localeCompare(b[0]))
}

/** The fleet as one cloud sees it. Windows with none of its clusters drop
 *  out; uncollected and unloadable clusters carry no cloud, so they show only
 *  under "all" rather than under a cloud they may not be in. */
function forCloud(fleet: Fleet, cloud: string): Fleet {
  if (cloud === "all") return fleet
  return {
    ...fleet,
    periods: fleet.periods
      .map((p) => ({ ...p, clusters: p.clusters.filter((c) => cloudOf(c) === cloud) }))
      .filter((p) => p.clusters.length > 0),
    uncollected: [],
    unavailable: [],
  }
}

/** Per-cluster collection times, for the hover text beside the window. */
function collectedTimes(period: Period): string {
  return period.clusters.map((c) => `${c.env}: collected ${shortDateTime(c.collectedAt)} UTC`).join("\n")
}

export default function App() {
  const [fleet, setFleet] = useState<Fleet | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [view, setView] = useState<ViewId>(() => {
    const hash = window.location.hash.replace("#", "") as ViewId
    return VIEWS.some((v) => v.id === hash) ? hash : "overview"
  })
  const [env, setEnv] = useState<string>("")
  const [periodId, setPeriodId] = useState<string>("")
  const { dark, setDark } = useTheme()
  const { cloud, setCloud } = useCloudFilter()

  useEffect(() => {
    loadFleet()
      .then((f) => {
        setFleet(f)
        setPeriodId(f.periods[0]?.id ?? "")
        setEnv(f.periods[0]?.clusters[0]?.env ?? "")
      })
      .catch((e: Error) => setError(e.message))
  }, [])

  useEffect(() => {
    window.history.replaceState(null, "", `#${view}`)
  }, [view])

  const open = (next: string, nextEnv?: string) => {
    if (nextEnv) setEnv(nextEnv)
    setView(next as ViewId)
  }

  if (error) {
    return (
      <div className="mx-auto max-w-lg p-8">
        <h1 className="text-lg font-semibold">Could not load the dataset</h1>
        <p className="mt-2 text-sm text-muted-foreground">{error}</p>
        <p className="mt-4 text-sm text-muted-foreground">
          Generate it with <span className="font-mono">python3 export_ui.py</span> from the{" "}
          <span className="font-mono">rightsizer</span> checkout, which writes{" "}
          <span className="font-mono">public/data/</span>; if only the index is lost,{" "}
          <span className="font-mono">python3 export_ui.py --index-only</span> rebuilds it. Then reload.
        </p>
      </div>
    )
  }

  if (!fleet) {
    return (
      <div className="flex min-h-screen items-center justify-center text-sm text-muted-foreground">
        Loading fleet…
      </div>
    )
  }

  const clouds = cloudCounts(fleet)
  // A remembered cloud this dataset no longer has is "all", not an empty page.
  const activeCloud = cloud !== "all" && clouds.some(([k]) => k === cloud) ? cloud : "all"
  const shown = forCloud(fleet, activeCloud)
  const period = shown.periods.find((p) => p.id === periodId) ?? shown.periods[0]
  // HPA history and trends are read from the folder of the period being viewed.
  selectPeriod(period.id)
  const counts = summarise(period)
  const navCounts: Record<ViewId, number | null> = {
    overview: null,
    clusters: period.clusters.length,
    scenarios: period.clusters.reduce(
      (n, c) => n + c.nodegroups.filter(
        (g) => !g.empty && (g.scenarios ?? []).some((s) => s.key !== "asis")).length,
      0,
    ),
    simulator: period.clusters.reduce(
      (n, c) => n + c.nodegroups.filter((g) => !!g.sim).length, 0,
    ),
    workloads: counts.containers.filter((x) => x.actionable !== false).length,
    compare: shown.periods.length,
  }

  return (
    <div className="grid min-h-screen md:grid-cols-[228px_1fr]">
      <aside className="flex flex-col gap-5 border-b bg-sidebar p-4 md:sticky md:top-0 md:h-screen md:border-b-0 md:border-r">
        <div className="flex items-start justify-between gap-2">
          <div>
            <div className="text-[15px] font-bold tracking-[-0.01em]">Right-Sizing</div>
            <div className="mt-0.5 text-[11.5px] text-muted-foreground">
              {period.clusters.length} clusters · {dateSpan(period.clusters.map((c) => c.collectedAt))}
            </div>
          </div>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 shrink-0"
            aria-label={dark ? "Switch to light theme" : "Switch to dark theme"}
            onClick={() => setDark(!dark)}
          >
            {dark ? <Sun /> : <Moon />}
          </Button>
        </div>

        <nav aria-label="Sections" className="flex flex-wrap gap-1 md:grid md:gap-0.5">
          {VIEWS.map(({ id, label, Icon }) => (
            <button
              key={id}
              onClick={() => setView(id)}
              aria-current={view === id ? "page" : undefined}
              className={cn(
                "flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-[13.5px] font-medium transition-colors md:w-full",
                view === id
                  ? "bg-primary text-primary-foreground"
                  : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
              )}
            >
              <Icon className="h-3.5 w-3.5 opacity-80" />
              {label}
              {navCounts[id] !== null && (
                <span className="ml-auto text-[11.5px] opacity-65 tnum">{navCounts[id]}</span>
              )}
            </button>
          ))}
        </nav>

        <div className="mt-auto hidden gap-2 text-[11.5px] text-muted-foreground md:grid">
          <Badge variant="outline" className="justify-self-start">Read-only</Badge>
          <p>
            Dollars are a ceiling: list price less {Math.round(fleet.discount * 100)}%, with no Savings Plan
            modelling.
          </p>
          <p>No prediction here has been checked against a CUR bill.</p>
        </div>
      </aside>

      <main className="min-w-0 px-4 pb-16 pt-6 md:px-6">
        <header className="mb-5 flex animate-rise flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="text-[clamp(19px,3vw,23px)] font-bold tracking-[-0.02em]">{TITLES[view].title}</h1>
            <p className="mt-1 max-w-[72ch] text-[13px] text-muted-foreground">{TITLES[view].sub}</p>
          </div>
          <div className="flex shrink-0 flex-wrap items-center gap-3">
          {clouds.length > 1 && (
            <div className="flex items-center gap-2 text-[12.5px] text-muted-foreground">
              <span id="cloud-label">Cloud</span>
              {/* A Select rather than a native <select>: an <option> holds text only,
                  and the cloud's mark is the point of the filter. */}
              <Select value={activeCloud} onValueChange={setCloud}>
                <SelectTrigger id="cloud-select" aria-labelledby="cloud-label"
                               className="h-8 w-auto min-w-[150px] text-foreground">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">
                    <span className="flex items-center gap-2">
                      <CloudGlyph className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
                      All clouds ({clouds.reduce((n, [, c]) => n + c, 0)})
                    </span>
                  </SelectItem>
                  {clouds.map(([k, n]) => (
                    <SelectItem key={k} value={k}>
                      <span className="flex items-center gap-2">
                        <CloudIcon cloud={k} />
                        {cloudLabel(k)} ({n})
                      </span>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}
          {view !== "compare" && shown.periods.length > 1 && (
            <label className="flex shrink-0 items-center gap-2 text-[12.5px] text-muted-foreground">
              Window
              <select
                id="period-select"
                value={period.id}
                onChange={(e) => setPeriodId(e.target.value)}
                title={collectedTimes(period)}
                className="h-8 rounded-md border border-input bg-background px-2 text-[13px] text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                {shown.periods.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.label} · {dateSpan(p.clusters.map((c) => c.collectedAt))}
                  </option>
                ))}
              </select>
            </label>
          )}
          </div>
        </header>

        <div key={view} className="animate-rise">
          {view === "overview" && <Overview period={period} uncollected={shown.uncollected}
                                                unavailable={(shown.unavailable ?? []).filter((u) => u.period === period.id)}
                                                discount={fleet.discount} onOpen={open} />}
          {view === "clusters" && <Clusters period={period} env={env} setEnv={setEnv} />}
          {view === "scenarios" && <Scenarios period={period} />}
          {view === "simulator" && <Simulator period={period} />}
          {view === "workloads" && <Workloads period={period} />}
          {view === "compare" && <Compare fleet={shown} />}
        </div>
      </main>
    </div>
  )
}
