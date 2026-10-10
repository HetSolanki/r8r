/** Shapes emitted by `export_ui.py`. Keep in sync with that script. */

export type Severity = "outage" | "no-headroom" | "correctness" | "routine"

export interface Blocker {
  code: string
  severity: string
  detail: string
}

export interface Action {
  kind: "scale-in" | "instance-downsize" | "memory-increase" | "annotate" | "requests"
  subject: string
  summary: string
  /** Signed: negative reduces spend, positive increases it, zero is a correctness fix. */
  delta: number
  mechanism: string
  severity: Severity
  surface: string
  prerequisite: string
  verifiedBy: string
  command: string
  evidence: string[]
  blocked: boolean
  blockers: Blocker[]
}

/** What a change does to reserved capacity. Signed and named, because freeing
 *  capacity and needing more are different findings, and most real changes do
 *  both at once — CPU down, memory up. */
export type Direction = "frees" | "needs" | "mixed" | "none"

/** One workload request change that feeds a node group's scenario. */
export interface Driver {
  ns: string
  workload: string
  container: string
  replicas: number
  cpuReq: number
  cpuRec: number | null
  memReq: number
  memRec: number | null
  /** Fleet-wide, signed: negative frees capacity, positive needs more. */
  cpuDelta: number
  memDelta: number
  direction: Direction
  /** Observed usage per replica over the window — the evidence for the
   *  recommendation. Absent on an older dataset. */
  cpuAvg?: number
  cpuP95?: number
  cpuPeak?: number
  /** The per-replica distribution the request is sized against: p50…p99, max.
   *  Lets a reader ask how often a container sits above a proposed request
   *  rather than infer it from one percentile. */
  cpuLadder?: Record<string, number>
  memLadder?: Record<string, number>
  /** No limit means the request never throttles the container — it only sets
   *  its share under contention. A limit caps it outright. */
  cpuLimit?: number | null
  memLimit?: number | null
  /** Peak as a share of the current request. Over 100 means the container has
   *  been using more than it reserved. */
  cpuUtilPct?: number | null
  cpuSamples?: number
  memAvg?: number
  memP95?: number
  memPeak?: number
  memUtilPct?: number | null
  memSamples?: number
  memOomKilled?: boolean | null
  /** How the recommendation was derived, in words. */
  basis?: string
}

export interface ReplaySummary {
  minutes: number
  avg_replicas: number
  peak_replicas: number
  over_request_minutes: number
  over_target_minutes: number
  max_util: number
  longest_over_request: number
  unreserved_core_minutes: number
}

/** One HPA whose replica floor sits above what its target implies. */
export interface ReplicaChange {
  ns: string
  workload: string
  /** The container the HPA's CPU metric is measured on; names its trend file. */
  container?: string
  hpa: string
  minReplicas: number
  suggestedMin: number
  maxReplicas: number
  /** Pods of this workload on this node group right now. */
  runningNow?: number
  /** Replicas actually running, averaged over the window. Above the floor means
   *  the floor came down partway through. */
  avgReplicas?: number
  /** Non-empty when the window spans more than one configuration, so the
   *  percentages describe a blend rather than a state. */
  mixedWindow?: string
  /** 30-day maximum pod count. Counts both sides of a rolling update, so it can
   *  sit well above both the floor and what the HPA ever wanted. */
  peakReplicas?: number
  /** Deprecated alias of `peakReplicas` — it was labelled "Running", which it
   *  is not. */
  observedReplicas: number
  cpuTarget: number
  requestPerReplica: number
  /** The request the suggested floor was sized against -- the recommended one
   *  when the option also cuts requests. */
  requestAfter?: number
  /** "replay-1m": floor from the per-minute controller replay. "equilibrium":
   *  from 5-minute quantiles, when no minute history was collected. */
  method?: "replay-1m" | "equilibrium"
  /** Replay summaries for today's floor and the suggested one. */
  replayNow?: ReplaySummary | null
  replayAfter?: ReplaySummary | null
  /** How closely the replay of today tracked the real HPA. */
  validation?: { minutes: number; within_one: number; exact: number; mae: number;
                 model_avg: number; actual_avg: number } | null
  behaviorSource?: string
  lagMinutes?: number | null
  p95PerReplica: number
  utilPct: number | null
  freedCores: number
  /** What the 30-day HPA replay concluded, before the per-step cap. */
  /** Observed CPU utilisation per replica, against the HPA's own target. The
   *  gap between them is why the floor, not the HPA, is setting the count. */
  utilAvgPct?: number
  utilMaxPct?: number
  targetPct?: number
  /** Replicas wanted at the average, for symmetry with `peakDesired`. */
  neededAvg?: number
  replayFloor?: number
  /** The busiest sample wanted several times this floor: the HPA has a long
   *  climb, so the change is worth staging. */
  longScaleUp?: boolean
  /** Replicas the HPA wanted at the busiest sample in the window. */
  peakDesired?: number
  desired?: Record<string, number>
  basis?: string
}

/** What an option changes, counted -- so two routes to one saving can be
 *  weighed by what each takes to ship. */
export interface Changes {
  requests: number
  hpas: number
  minSize: boolean
  newType: boolean
}

export type Family = "requests" | "hpa" | "both" | "minsize" | "newtype" | "unblock"

export interface Scenario {
  changes?: Changes
  family?: Family
  /** "asis" is the measured baseline. Every other key is generated from the
   *  change it represents, so the set varies per node group — a group with
   *  nothing worth doing has only the baseline. */
  key: string
  label: string
  instanceType: string
  nodes: number
  monthlyUsd: number
  /** Signed, against as-is. */
  deltaUsd: number
  cpuUtil: number
  memUtil: number
  binding: string
  feasible: boolean
  note: string
  requires: string[]
  /** A different instance type from today's, and which of `requires` reach it. */
  retype?: boolean
  retypeSteps?: string[]
  /** A Karpenter pool: the fleet Karpenter arrives at, by size. `instanceType`
   *  is then the commonest size and `nodes` the total; nobody sets either. */
  fleet?: FleetEntry[]
  /** On the baseline of a Karpenter pool: what consolidation does with no edits. */
  karpenter?: { fleetAfter: FleetEntry[]; nodesAfter: number; monthlyAfter: number;
                held: { node: string; reason: string }[]; notes: string[];
                stuck?: boolean } | null
  /** Nodes this option needs removed that Cluster Autoscaler will not drain.
   *  The option is still shown: the blockers are usually a one-line annotation,
   *  and hiding it hides that choice. */
  /** The HPAs this option changes, with the floors *this* option produces. An
   *  option that also cuts requests raises each HPA's equilibrium, so its
   *  floors differ from the replica-only option's. */
  replicaChanges?: ReplicaChange[]
  drainBlocked?: number
  /** Priced "once unblocked": the workloads fixed (`lift`), and the node
   *  count Cluster Autoscaler stops at without them. */
  blocked?: { lift?: string[]; nodesIfNot?: number; stays?: string[] }
  drainBlockers?: {
    subject: string
    reason: string
    nodes: string[]
    fix: string
    replicas?: number
    /** Only for a "no PDB" blocker. `safe` is false when a PDB alone cannot
     *  both unblock the eviction and keep the workload up — a single-replica
     *  service, or a batch pod where a PDB is the wrong instrument. */
    pdb?: { replicas: number; safe: boolean; yaml: string | null; note: string } | null
    /** A StatefulSet whose EBS volume is in a zone no staying node is in. */
    zoneWarning?: string
  }[]
  /** Blocked nodes that stay: Cluster Autoscaler fills them instead of
   *  emptying them, so what pins them needs no fix. */
  drainKept?: { node: string; subjects: string[] }[]
  /** The one-off alternative to changing any workload: drain these by hand. */
  drainByHand?: string[]
  /** The fields below arrived with the packing detail. They are optional because
   *  the bundle and fleet.json deploy separately and are cached differently — an
   *  older dataset omits them, and an unguarded read blanks the page. */
  /** True when scheduling constraints were unavailable and this scenario still
   *  reduces the node count — an upper bound, not a recommendation. */
  /** True when the placement shown is observed, not simulated. Only "as-is" is. */
  measured?: boolean
  unverified?: boolean
  /** Share of allocatable the packer was allowed to fill, e.g. 0.85. */
  headroom?: number
  /** Pods whose footprint came from observed usage rather than their request. */
  usageDrivenPods?: number
  nodeCpu?: number
  nodeMemGib?: number
  nodeMaxPods?: number
  vcpu?: number
  memGib?: number
  hourlyUsd?: number
  /** The node count this option never goes below: the group's minSize, or 0
   *  for an option that lowers it. */
  minNodes?: number
  /** Node-local disk before and after this option. Absent on older data. */
  storage?: StorageCheck
  network?: NetworkCheck
  /** Measured busy load on the resulting nodes, and the window's peaks. */
  load?: { verdict?: "ok" | "warn" | "unsafe" | "unknown"; notes?: string[]; hotCpu?: number
           hotMem?: number; peakCpu?: number; peakMem?: number }
  /** Headroom against the targets a recommendation must keep (memory peak
   *  80%, CPU peak 90%, busiest node 85% / 90% at busy load). An option
   *  without it is never recommended; the search offers the same change on
   *  enough nodes instead. */
  margin?: { ok: boolean; notes: string[]; over?: Record<string, number>; targets?: Record<string, number> }
  /** One entry per node, in fill order. Capped at 80. DaemonSet figures are
   *  reported separately: the packer treats them as fixed per-node overhead and
   *  CA leaves them out of its utilisation sum, but a node drawn without them
   *  looks emptier than it is. */
  placement?: {
    cpu: number; mem: number; pods: number
    dsCpu?: number; dsMem?: number; dsPods?: number
  }[]
}

/** What a node group's nodes used and had, every 6 hours over the window.
 *  Usage and capacity are both the bucket's peak. Aligned on `t`; a gap in
 *  one series is null there. CPU in cores, memory in GiB, disk in GB. */
export interface NodeGroupTrend {
  t: number[]
  nodes: (number | null)[]
  cpu: (number | null)[]
  cpuCap: (number | null)[]
  mem: (number | null)[]
  memCap: (number | null)[]
  disk: (number | null)[]
  diskCap: (number | null)[]
}

export interface DiskUse {
  /** Group total ÷ (nodes × disk): how full the average node is. */
  avgShare: number | null
  /** The fullest node's share of its own disk, at its peak. */
  hotShare: number | null
  hotGb: number
}

/** Whether the nodes' local disks still hold what they hold today.
 *
 *  Kubelet, images and pod logs live on the instance-store NVMe on `d` types,
 *  so a smaller size or a type without one moves that data, and packing onto
 *  fewer nodes puts the same bytes on fewer disks. */
/** Network bandwidth before and after one option. EC2 limits each direction
 *  separately; figures are the busier one, in Gbps. `*Peak` is the highest
 *  5-minute rate over the window, `*Sustained` the highest 1-hour average --
 *  what the baseline must carry once burst credits are spent. */
export interface NetworkCheck {
  /** "3.125 Gbps, burst to 12.5", "12.5 Gbps", "not described". */
  bandwidthBefore: string
  bandwidthAfter: string
  baselineBefore: number | null
  baselineAfter: number | null
  burstBefore: number | null
  burstAfter: number | null
  /** "described" (EC2's answer, cached) or "published" (built-in table). */
  source: "described" | "published" | null
  before?: { hotPeakGbps: number; hotSustainedGbps: number; sustainedShare: number | null }
  after?: { hotPeakGbps: number; hotSustainedGbps: number; sustainedShare: number }
  notes: string[]
  verdict: "ok" | "warn" | "unsafe" | "unknown"
}

export interface StorageCheck {
  /** "1 × 474 GB NVMe SSD", "none (EBS only)", "not described". */
  diskBefore: string
  diskAfter: string
  ssdBefore: boolean
  ssdAfter: boolean
  storeGbBefore: number
  storeGbAfter: number
  kubeletOnInstanceStore?: boolean
  /** The filesystem kubelet writes to, per node. */
  perNodeGbBefore?: number
  perNodeGbAfter?: number
  totalUsedPeakGb?: number | null
  before?: DiskUse | null
  after?: DiskUse | null
  ephRequestsGb?: number
  ephAllocatableGbAfter?: number
  verdict: "ok" | "warn" | "unsafe" | "unknown"
  notes: string[]
}

/** What Cluster Autoscaler would do to a node group on its own, given the flags
 *  it is actually deployed with. Distinct from every scenario: those describe
 *  what could fit, this describes what the controller will remove without anyone
 *  changing a request. */
export interface FleetEntry { type: string; nodes: number }

export interface AutoscalerEstimate {
  source: string
  /** True when this is Karpenter's own provisioning + consolidation replay,
   *  rather than Cluster Autoscaler's rules. */
  replayed?: boolean
  fleetToday?: FleetEntry[]
  fleetAfter?: FleetEntry[]
  threshold: number
  unneededNodes: number
  removableNodes: number
  floorBlocked: number
  minSize: number | null
  monthlyIfRemoved: number
  monthlyIfFloorLowered: number
  reasons: string[]
  nonDefaultArgs: Record<string, string | number | boolean>
  /** What a node count alone would claim, before the per-pod veto classes are
   *  applied. `removableNodes` is the number after them. */
  removableIfUnblocked?: number
  /** Per node: why Cluster Autoscaler will not remove it. */
  vetoes?: { node: string; reason: string }[]
  notEvacuable?: string[]
  /** Which veto classes were actually evaluated, and which facts were missing
   *  so the rest could not be. */
  checked?: string[]
  unchecked?: string[]
  simulated?: boolean
  /** Unused-node count alone, with nothing vetoed: the gap to `removableNodes`
   *  is what CA will refuse to touch. */
  monthlyIfNothingBlocked?: number
  /** Nodes whose measured load would not fit once CA acts -- CA packs by
   *  requests, so load from pods without requests lands where there is no room
   *  for it. Empty is the good answer. */
  pressureAfterScaleDown?: NodePressure[]
  pressureIfFloorLowered?: NodePressure[]
  /** Load the scheduler and CA cannot see on this group. */
  unreservedLoad?: UnreservedLoad
}

/** A correctness or stability finding on a node group. Separate from cost:
 *  a group can be priced correctly and still need attention. */
export interface Problem {
  /** `cpu-throttled` and `cpu-below-use` are performance findings: they cost
   *  money to fix rather than save it. A right-sizer that reports only the
   *  savings is one you stop trusting the first time these bite. */
  kind:
    | "oom-risk"
    | "exceeds-request"
    | "no-request"
    | "drain-blocked"
    | "cpu-throttled"
    | "cpu-below-use"
  subject: string
  detail: string
  severity: "critical" | "serious" | "warning"
}

/** The recommendation for one node group — the decision, not the search.
 *  `alternatives` holds only options that trade something real against the
 *  choice: fewer steps, or a materially different saving. */
export interface Advice {
  group: string
  verdict: "act" | "watch" | "healthy" | "blocked"
  headline: string
  reason: string
  scenarioKey: string | null
  monthlyDelta: number
  steps: string[]
  /** The best option on each other route, never the pick. */
  alternatives: { key: string; label: string; monthlyDelta: number; steps: number; why: string; family?: Family }[]
  /** A few worst examples only. The counts carry the rest. */
  problems: Problem[]
  findingCounts: Record<string, number>
  findingsTotal: number
  /** One line: what is wrong here, by kind and count. */
  findingsSummary: string
  diagnostics: {
    binding?: string
    gibPerCoreDemanded?: number
    gibPerCoreOffered?: number
    shapeMismatch?: string
    floorBound?: boolean
    minSize?: number
    tight?: boolean
  }
}

export interface NodePressure {
  node: string
  severity: "critical" | "warning"
  memBusyGiB: number
  memAllocGiB: number
  cpuBusy: number
  cpuAlloc: number
  bestEffortPods: string[]
}

export interface UnreservedLoad {
  bestEffortPods: number
  bestEffortMemGiB: number
  bestEffortCpu: number
  overRequestPods: number
  overRequestMemGiB: number
}

/** The markdown report's disposition for one group, from the same function. */
export type ScaleIn = "yes" | "no" | "n/a"

/** How a group reaches a different instance type, from whoever owns its shape
 *  (`rightsizer.adapters`): a managed pool is replaced or updated, a Karpenter
 *  pool gains a size or has it already. Templates carry {type}, {ocpus}, {gb}. */
export interface RetypePlan {
  offered: string[]
  ifOffered: string
  template: string
  surface: string
}

export interface NodeGroup {
  name: string
  /** "cloud" for the cloud's own node pool, else the autoscaler that defines it. */
  source?: string
  retype?: RetypePlan
  /** HPAs whose maxReplicas the load reaches -- today ("now"), or only once
   *  the recommended requests are applied ("after"). */
  hpaCeilings?: HpaCeiling[]
  /** Every candidate instance type with its specs and price, and the
   *  measured disk and network, so the console can price any of them. */
  instanceChoices?: import("@/lib/retype").InstanceChoices | null
  /** OVERPROVISIONED | CORRECTLY_SIZED | REDUCIBLE_BUT_BLOCKED | EMPTY | UNKNOWN */
  verdict?: string
  scaleIn?: ScaleIn
  why?: string
  /** Every reason the group-level recommendation is limited, not only the ones
   *  attached to an action. */
  blockers?: Blocker[]
  /** The numbers behind the node-count recommendation. */
  evidence?: Record<string, number | string>
  /** null when the snapshot predates the check -- unknown, not zero. */
  notReady?: number | null
  cordoned?: number | null
  nodes: number
  instanceType: string
  min: number | null
  desired: number | null
  max: number | null
  capacityType: string
  cpuUtil: number | null
  memUtil: number | null
  avg30d: number
  min30d: number
  peak30d: number
  target: number
  binding: string
  monthlyCost: number
  saving: number
  action: string
  prerequisite: string
  empty: boolean
  latent?: number
  /** The four-way decomposition: as-is, right-size only, re-type only, both.
   *  Empty for groups with no nodes or no instance type. Optional because the
   *  payload is plain JSON and nothing validates it at runtime — an older export
   *  omits the key entirely, and reading it unguarded blanks the whole page. */
  scenarios?: Scenario[]
  /** The request changes that produce the right-size scenarios for this group. */
  drivers?: Driver[]
  /** The HPAs to change for the replica-floor option. */
  replicaChanges?: ReplicaChange[]
  /** 6-hourly node-level usage and capacity over the window. Absent on older data. */
  trend?: NodeGroupTrend | null
  /** Best option on the board, including migrations the advice rejects as not
   *  worth the steps. `saving` is what following the advice yields. */
  savingIfEverything?: number
  /** Everything the in-browser selection simulator needs to pack this group
   *  itself: the pod set, per-workload peaks, and reference cases the console's
   *  packer must reproduce. Absent on a group with no recorded placement. */
  sim?: import("@/lib/simulate").Sim | null
  autoscaler?: AutoscalerEstimate | null
  /** A Karpenter pool's nodes today by size; empty for a group of one set type. */
  fleet?: FleetEntry[]
  advice?: Advice | null
  /** How many shapes the search evaluated. Only the recommendation, the
   *  baseline and the genuine alternatives are shipped — the rest did their job
   *  during the search and have no reader. */
  optionsConsidered?: number
}

export interface Container {
  /** False when a blocker makes the recommendation unsafe or pointless; the
   *  row is still shipped so the reason is visible. */
  actionable?: boolean
  blockers?: Blocker[]
  ns: string
  workload: string
  container: string
  kind: string
  /** 30-day **maximum** pod count, not the current one. api-server
   *  reads 104 here while 75 run, because its floor was 100 until 16 Sept. */
  replicas: number
  /** Pods running right now, from observed placement. */
  replicasNow?: number
  /** Replicas averaged over the window — above the floor means it moved. */
  replicasAvg?: number
  nodegroup: string
  cpuReq: number
  cpuRec: number | null
  memReq: number
  memRec: number | null
  /** Fleet-wide concurrent peak (all replicas at once), distinct from the
   *  per-replica cpuPeak below. */
  cpuFleetPeak: number
  /** null below two replicas: with one replica the ratio is 1 by construction. */
  coinc: number | null
  /** Fleet-wide, signed: negative frees capacity, positive needs more. */
  cpuDelta: number
  memDelta: number
  direction: Direction
  /** Observed usage per replica over the window — the evidence for the
   *  recommendation. Absent on an older dataset. */
  cpuAvg?: number
  cpuP95?: number
  cpuPeak?: number
  /** The per-replica distribution the request is sized against: p50…p99, max.
   *  p99 far above p95 means the tail is where the work is. */
  cpuLadder?: Record<string, number>
  memLadder?: Record<string, number>
  /** null means no limit is set: the request never throttles this container,
   *  it only sets its share of a contended node. */
  cpuLimit?: number | null
  memLimit?: number | null
  /** Peak as a share of the current request. Over 100 means the container has
   *  been using more than it reserved. */
  cpuUtilPct?: number | null
  cpuSamples?: number
  memAvg?: number
  memP95?: number
  memPeak?: number
  memUtilPct?: number | null
  memSamples?: number
  memOomKilled?: boolean | null
  /** How the recommendation was derived, in words. */
  basis?: string
}

export interface CostSummary {
  reductions: number
  reductionsCount: number
  increases: number
  increasesCount: number
  neutralCount: number
  net: number
  netAnnual: number
  currentRunRate: number
  projectedRunRate: number
  netPctOfSpend: number
  /** Identified but blocked: excluded from NET because acting would not save it. */
  blocked: number
  blockedCount: number
}

export interface Cluster {
  env: string
  cluster: string
  region: string
  account: string
  collectedAt: string
  /** Days of history every figure for this cluster was sized from. */
  windowDays: number
  /** Days asked for (--days). Larger than windowDays when the metrics store
   *  held less history, in which case the recommendations rest on less. */
  requestedWindowDays?: number
  hasFacts: boolean
  /** True when the node autoscaler's configuration was read for this cluster. */
  caConfigured?: boolean
  /** The cloud adapter the cluster was collected with ("aws"). */
  cloud?: string
  /** The node autoscaler found running. `modelled` is false when the engine
   *  approximates it with Cluster Autoscaler's rules rather than replaying it. */
  autoscaler?: { kind: string; title: string; source: string | null; modelled: boolean }
  /** Where region/account/profile came from: the snapshot, clusters.json, or inferred. */
  metaSource?: "snapshot" | "clusters.json" | "inferred"
  /** Nothing said where the cluster is, so prices use the default region. */
  regionAssumed?: boolean
  caThreshold?: number | null
  discount?: number
  /** The report's cost summary, from the function the report uses. */
  summary?: CostSummary
  /** Problems with the export itself -- unpriced cluster, assumed region. */
  exportWarnings?: string[]
  currentRunRate: number
  net: number
  /** Sum of every ready action, including options the advice rejects. */
  netIfEverything?: number
  containersActionable: number
  containersTotal: number
  nodesTotal: number
  warnings: string[]
  actions: Action[]
  nodegroups: NodeGroup[]
  containers: Container[]
}

/** One collected window. `end` is null for "now"; otherwise an ISO date the
 *  30-day window ended on, which is how a before/after comparison is built. */
export interface Period {
  id: string
  label: string
  end: string | null
  collectedAt: string
  clusters: Cluster[]
}

/** Bumped when the payload gains a field the UI relies on. */
export const SCHEMA_VERSION = 11

export interface Fleet {
  schema?: number
  generatedAt: string
  discount: number
  periods: Period[]
  /** Known clusters with no snapshot yet — listed so fleet totals are not mistaken for the whole fleet. */
  uncollected: string[]
  /** Clusters the index lists whose file could not be used: missing, malformed
   *  or exported by an older version. Left out of every figure, and said so. */
  unavailable?: { env: string; period: string; reason: string }[]
}

export type ActionWithEnv = Action & { env: string }
export type ContainerWithEnv = Container & { env: string }

export interface HpaCeiling {
  key: string
  hpa: string
  max: number
  suggestedMax: number
  when: "now" | "after"
  /** Minutes the real HPA sat at maxReplicas over the window. */
  pinnedMinutes: number
  /** Replay: minutes it wanted more and was held, and the most it wanted. */
  cappedNow: number
  peakWantedNow: number
  cappedAfter: number
  peakWantedAfter: number
  /** Minutes still held at the suggested ceiling: spikes it does not chase. */
  cappedAtSuggested: number
  basis: string
}
