# rightsizer

Deterministic EKS right-sizing from Prometheus/Thanos. No LLM in the math path:
every number is arithmetic over stored evidence, so two runs on one snapshot are
identical and any figure can be traced to the query that produced it.

## Why this exists

Container right-sizing saves nothing until a node disappears. AWS bills
node-hours. This tool follows the chain all the way:

    requests ↓ → packing density ↑ → node count ↓ → node-hours ↓

and refuses to report a saving when any link is broken. Those breaks are
**blockers**, and they are first-class output rather than silent skips.

## Install & run

```bash
# once: install into the checkout's venv and put the command on your PATH
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
ln -s "$PWD/.venv/bin/rightsizer" ~/.local/bin/rightsizer

rightsizer clusters                         # every cluster found locally, and its snapshot
rightsizer --env prod-eu1 run             # collect + analyze
rightsizer --env prod-eu1 analyze --stdout   # re-analyze, no re-query
rightsizer --env prod-us1,prod-eu1 run  # several in turn; `--env all` for every one
```

**The cluster name is enough.** Each cluster's directory --
`~/eks/prod/<env>/` or `~/eks/stage/<env>/` -- already holds its kubeconfig and an
`.envrc` exporting `KUBECONFIG`, `AWS_PROFILE` and `AWS_REGION`, and the context
is the EKS ARN, which carries the account. `--env` finds that directory and fills
`--context`, `--kubeconfig`, `--cluster`, `--profile`, `--region` and `--account`
from it, so nothing is copied out of an `.envrc` before a run. The `.envrc` is
parsed, never executed. Any flag given explicitly wins; `--no-local` turns the
lookup off; `$RIGHTSIZER_CLUSTERS` (colon-separated) replaces the roots searched.
Output goes to `out/` in the checkout wherever the command is run from
(`$RIGHTSIZER_OUT` or `--out` to change it).

`collect` also gathers the per-minute HPA history (a few minutes more; skip it
with `--no-hpa-history`). `hpa-history` adds it to an existing snapshot over the
snapshot's own window, so nothing else in the snapshot moves; run it again and it
fetches only the series an entry is missing. `--context` lets it read each HPA's
`behavior` from the cluster; without one, the scaling policy is inferred from how
the HPA actually scaled.

`collect` also measures node network traffic: for each node group, the busiest node's
highest 5-minute rate and highest 1-hour average on its physical interfaces (`ens*`,
`eth*`; the `eni*` pod veths would count the same bytes twice), receive and transmit
separately. Every option checks this against the proposed type's EC2 **baseline**
bandwidth and its **burst** ceiling, spread over the nodes the option leaves:

| Projected busiest node | Verdict |
|---|---|
| 1-hour average above the baseline, or a 5-minute peak above the burst ceiling | unsafe: network credits run out and EC2 throttles to baseline; never recommended |
| 5-minute peak above the baseline, or 1-hour average ≥ 70% of it | warn: depends on burst credits |

A smaller size in the same family keeps the same "up to 12.5 Gigabit" headline but has
a lower baseline (m6i.2xlarge 3.125 Gbps, m6i.xlarge 1.562). Bandwidth comes from
`describe-instance-types`, cached in `~/.cache/rightsizer/instances.json`. A type cached
before bandwidth was recorded falls back to a built-in table of published figures, and
the console marks those "(published)". The next run with AWS access refreshes them.
`rightsizer network` adds the traffic to an existing snapshot.

### Headroom: what a recommendation must leave

Fitting is not enough. Every option — request right-sizing, HPA floors, a lower
minSize, a new instance type, or one priced "once unblocked" — is checked against:

| Measure | Target |
|---|---|
| memory at the window's peak (node-exporter, 6-hourly peaks, the group's own scaling kept) | ≤ 80% |
| CPU at the window's peak | ≤ 90% |
| busiest node's memory at busy load (p95 of its pods) | ≤ 85% |
| busiest node's CPU at busy load | ≤ 90% |
| local disk / network | not "tight" (see above) |

It is judged against today: a group already above a target is a finding, not
something the option caused. An option that misses a target is never the pick. The
search then offers the same change on the fewest nodes that keep the margin, for
example "lower minSize to 2" instead of 1. The console charts both targets, and the
dialog warns when a custom selection breaks one.

`collect` writes `out/snapshot-<env>.json`; `analyze` reads only that. Methodology
changes replay against frozen data instead of re-querying Thanos, which takes ~7
minutes for a 30-day window.

Read-only throughout: HTTP GET against the Thanos query API, nothing else.

### Simulating a specific set of changes

The recommender sizes every container it can. A PR changes four of them, usually
a little more generously than suggested. `plan` answers what *that* does:

```bash
cat > cut.plan <<'EOF'
# namespace/workload/container  cpu=<qty>  mem=<qty>
apps/api-server/api-server   cpu=300m
apps/search-server/search-server     cpu=300m
apps/media-server/media-server  mem=5Gi   # above the recommendation
EOF

python3 -m rightsizer.cli --env prod-us1 --region us-east-1 --plan-file cut.plan plan
```

`cpu=recommended` takes the tool's number for that container. Both sides of the
comparison are packed by the same engine that produces the published
recommendations, so a node count here cannot drift from one there.

Three node counts are reported, not two: what is running, what the packer makes
of today's pods, and what it makes of the plan. The saving is model-to-model,
because mixing a real count with a simulated one is how a simulation flatters
itself.

It exits non-zero when anything is flagged `unsafe`, so CI can gate a PR on it.

**What it is really for.** A container with no limit uses what it uses, whatever
its request says. Cutting the request below measured use frees nothing -- the
node still has to hold the usage -- it just stops the pod being covered by a
reservation, and a Burstable pod over its request is the first thing evicted
under node pressure. The report says so per node group:

```
45 of them did not shrink -- their measured use already exceeds the request
this plan gives them, so the node still has to hold the usage
```

**What the cluster itself would do.** The packing above sizes pods by what they
use, which is kinder than the cluster. Cluster Autoscaler and the scheduler size
them by what they *request* -- and a pod with no request (BestEffort) is invisible
to both. So for every group the plan touches, `plan` also replays CA twice, with
today's requests and with the plan's, then checks the **measured** load on the
nodes CA keeps:

```
cluster autoscaler (live deployment ...), packing by requests only:
  today it could remove 0 node(s); with this plan 1: ip-10-12-0-5
  load it cannot see: 147 pod(s) with no request or limit using ~7.1 GiB ...
  busy load on the nodes that remain (measured use, p95 of the mean replica):
    [critical] ip-10-12-0-9: memory 31.2 of 29.3 GiB busy (106%; 21.0 GiB reserved), ...
```

Load is measured use, never the request -- a request consumes nothing, and the
kubelet evicts on what containers actually hold. Findings, in the SAFETY section:

| Code | Severity | Meaning |
|---|---|---|
| `memory-overcommit-after-scale-down` | unsafe | Once CA acts on the plan, a node's busy memory exceeds allocatable. Names the pods evicted first (BestEffort, then those above their request). Exits non-zero. |
| `load-exceeds-capacity-after-scale-down` | risk | Memory above 90% or CPU over-committed after CA acts: slowdown, not eviction. |
| `node-pressure-today` | risk | Already true before the plan; any scale-down here makes it worse. |
| `unreserved-load` | advisory | No-request and over-request load on a group the plan tightens, with suggested requests for the heaviest BestEffort workloads as ready-to-paste plan lines. |

The busy estimate runs about 1.4x real node memory on prod-us1 (it sums a p95 across
pods, assuming they are busy together): cautious, in the direction of a warning.

### The console

The console (`../frontend`) is what decision makers read. The export ships
the report's cost summary and per-group disposition, computed by the same
`report.summarise` and `report.disposition`, plus every container -- including
the ones left alone, and why.

```bash
python3 -m rightsizer.cli --env my-cluster --context <ctx> --cluster my-cluster \
    --region eu-west-2 --profile <aws-profile> --account <id> run
python3 export_ui.py                      # every cluster -> ../frontend/public/data/
python3 export_ui.py --env my-cluster     # just this one; the others are left as they are
python3 export_ui.py --index-only         # rebuild data/index.json from the cluster files
```

### Where files go

Everything is kept per cluster, so one cluster's files can be re-made, moved or
lost without touching another's. The paths are defined once, in
`rightsizer/core/layout.py`.

```
out/<env>/snapshot.json                 collect: the current window
out/<env>/snapshot-<YYYY-MM-DD>.json    collect --end: a backtest
out/<env>/report.json, report.md        analyze

../frontend/public/data/
  index.json                            which clusters and periods exist (rebuilt, never edited)
  <env>/current.json                    export_ui.py: one cluster, current window
  <env>/backtest-<YYYY-MM-DD>.json      export_ui.py: one cluster, one backtest
  <env>/hpa/*.json                      export_ui.py: per-HPA minute history
  <env>/trends/*.json                   rightsizer trends: per-container usage
```

Exporting writes only the clusters it was given, and every file is written to a
temporary name and renamed into place, so an interrupted export never leaves a
half-written file. `index.json` holds nothing that is not in the cluster files;
each export rebuilds it from disk, and if it is lost `--index-only` restores it.
A cluster file that is missing, malformed or from an older export is left out
of the index (or the console) and named there; the other clusters still load.

A checkout still in the older flat layout (`out/snapshot-<env>.json`,
`public/fleet.json`) keeps working: snapshots are read from either place.
`python3 scripts/migrate_layout.py --dry-run` shows the move, and without
`--dry-run` makes it -- copying into the new layout first and only then moving
the originals to `out/_legacy/` and `frontend/_legacy-data/`.

Clusters are **discovered**, not listed: every `out/<env>/snapshot.json` is the
current period, and every `out/<env>/snapshot-<YYYY-MM-DD>.json` a backtest
ending that day. Adding a cluster is running `collect` -- no code changes.
`collect` records `--cluster/--region/--profile/--account` in the snapshot, which
is where the export reads them from. For snapshots collected before that, or to
correct one, `clusters.json` (optional) overrides per cluster; it also lists
`known` clusters, so uncollected ones show as such, and labels backtest periods.
It holds account IDs, so git ignores it: copy `clusters.example.json` to
`clusters.json` and fill it in locally.
Anything still unknown is inferred (cluster = env, region from node labels) and
the console says so: a cluster priced in an assumed region, or with no prices at
all, is flagged on the Overview before its totals are read.

### Trends for the console

The console's workload dialog plots usage per replica over the window. Those
series are *not* in the snapshot — they are collected separately, one file per
container, so a reader downloads only the trend they open:

```bash
rightsizer --env prod-us1 trends
```

Every container in the snapshot gets a trend, not only the ones a recommendation
changes. Judging an instance-type change means reading what everything on those
nodes used. There are two ways to collect:

* **Containers a recommendation touches** are collected one by one, with up to eight
  individual replica lines as well as the bands.
* **Every other container** comes from one cluster-wide batch: 12 queries for the
  whole cluster, grouped by workload and container, chunked over time. It returns
  the same bands and total, from the same series and bucketing (the numbers match
  the one-by-one method exactly), without replica lines. The dialog says so.

Trends are per cluster, so run it once per `--env`. Containers already on disk are
skipped, and a batched file is upgraded once its container needs the detail. Pass
`--refresh` to re-collect everything, `--actionable-only` for the old behaviour,
and `--limit N` to cap the list (those a recommendation touches go first).
They go to `public/data/<env>/trends/` (`--ui-data` or `$RIGHTSIZER_UI_DATA` to
change the root). Deploying the console means shipping all of `public/data/`,
because the dialog fetches trends at runtime.

## What it reports

Savings are **node-hours**, never dollars. Converting requires Savings Plan
coverage for the instance type and account, and a node-hour freed against a
commitment is not a bill reduction unless other uncovered usage absorbs it.

Two savings figures, kept apart on purpose:

| Figure | Realised by |
|---|---|
| **from request cuts** (the headline) | lowering requests, which lets the group shrink |
| **from repacking** (excluded) | deliberately lowering the ASG desired count |

Cluster Autoscaler does not defragment — it removes a node only when that node's
pods already fit elsewhere, and never relocates pods to create that condition.
Merging these two into one number is how a scale-in projection becomes a bill
that does not move.

## Blockers

**Options priced "once unblocked".** Cluster Autoscaler refuses to empty a node while
any pod on it cannot be evicted. Every such pod is recorded, not just the first. An
option priced "once unblocked" fixes only what the nodes that go need: blockers are
fixed one node at a time, cheapest first (fewest workloads, StatefulSets last). The
nodes that stay keep whatever pins them and need nothing. The console lists every
blocker with its fix, the nodes that stay, and a one-off `kubectl drain` for the nodes
that go, which is the alternative to changing any workload.

**Volume zones.** A pod mounting a zonal volume (EBS; anything but EFS, NFS or FSx)
restarts only in that volume's zone, so the replay never moves it elsewhere, and a
replacement node group keeps a node in every zone holding one. `rightsizer storage`
records the claims (`kube_pod_spec_volumes_persistentvolumeclaims_info` and the claim's
storage class).


| Code | Severity | Meaning |
|---|---|---|
| `hpa-utilization-target` | zero-savings | HPA targets cpu/memory *utilization*. Utilization is usage/request, so cutting the request raises it, the HPA scales out, and `replicas × request` is invariant. |
| `repacking-required` | zero-savings | Capacity is free but only reachable by moving running pods. |
| `pod-density-bound` | zero-savings | Group is at the ENI-derived maxPods ceiling, not CPU/memory bound. |
| `pdb-blocks-drain` | unsafe | A PDB permits zero evictions; the node cannot be drained. |
| `pdb-unhealthy-blocks-drain` | unsafe | A PDB's budget is used up by unhealthy pods (e.g. CrashLoopBackOff): `current_healthy − desired_healthy` is 0, so its healthy replicas cannot be evicted. Transient — names the failing pods; fix them, then re-collect. |
| `oom-killed` | unsafe | Container was OOMKilled: this needs an increase. |
| `insufficient-data` | data | Series covers too little of the window to size from. |
| `no-request-declared` | data | Invisible to the autoscaler. A correctness fix, not a saving. |
| `low-request-coverage` | data | Sizing this group from requests would recommend deleting most of it. |
| `spans-node-groups` | data | Workload ran across groups; they are one tier and must be sized together. |
| `flat-node-count` | data | The autoscaler never moved this group; something else holds it. |
| `topology-not-evaluated` | data | Anti-affinity and topology spread are invisible in Prometheus. |
| `demand-exceeds-capacity` | data | Measured requests do not fit measured allocatable. |
| `no-workload-data` | data | Nothing attributable to this group — missing evidence, not an empty group. |

## Statistics

CPU and memory are asymmetric. CPU is compressible — overshoot costs latency, so
`p95 × 1.5`. Memory is not — overshoot is an OOMKill, so `30d peak × 1.5`, request
== limit, using `container_memory_working_set_bytes`. Both are computed per
replica and then maxed across replicas, never averaged: sharded workloads have
hot replicas.

Percentiles are computed client-side, not in PromQL, because queries are chunked
and percentiles do not compose across chunks.

## HPA floors

An HPA floor is judged by replaying the controller minute by minute over the
window, not by where the HPA would settle. On a bursty service those differ: 
ingest-worker went 5 → 9 on a two-minute burst of 6.1 cores that a 5-minute
rate averaged to 2.5, then took 40 minutes to come back down, one pod every ten.

The replay (`rightsizer/engine/hpa_replay.py`, ported line for line to the console):

* recommends `ceil(pod CPU ÷ (pod request × target))` from a **1-minute** rate of
  every container in the pod, and ignores moves inside the 10% tolerance;
* applies the HPA's **behavior** — stabilization windows and rate policies. Most
  HPAs here scale up at **1 pod per 30s**, which is what makes a low floor slow to
  recover from a burst;
* makes new pods ready after the delay **measured** from the workload's own
  scale-ups (at least one minute: the controller only reacts to a burst it has
  seen), with the ready pods carrying the load until then.

The recommended floor is the lowest that adds at most 30 minutes above request
per 30 days compared with today, and keeps the ready pods' peak under 150% of
request (or today's peak, if that is already higher). Floors never go below 2,
and HPAs on anything but CPU utilisation are left alone.

A replay of the floor that was actually in force each minute is compared with the
HPA's own `desired_replicas`, and the share of minutes within ±1 replica is
reported beside every recommendation. HPAs without minute history fall back to
the older 5-minute quantile estimate and are labelled as such.

## Known limits

- **Anti-affinity and topology spread are not evaluated** — they are not in
  Prometheus, and they frequently set the real floor above the capacity floor.
  Every group carries `topology-not-evaluated`; treat node counts as an upper
  bound until checked against the Kubernetes API.
- **No pricing.** Node-hours only, by design.
- **maxPods ceiling** needs the instance type; pass it to enable
  `pod-density-bound`.
- **Requires kube-state-metrics.** Preflight fails loudly if it is absent —
  without it there is no pod→workload mapping and every run would report a false
  clean bill of health.

## Code layout

One package per stage of the pipeline: `core` imports nothing else, and
`output` sits on top of everything. Two imports run the other way:
`sources/hpa_history.py` uses `engine/hpa_replay.py` to parse or infer each
HPA's scaling behaviour and lag and to compress the series, and
`engine/simulate.py` takes `dominant_nodegroup` from `analysis/recommend.py`.

```
rightsizer/
  cli.py        the `rightsizer` command
  core/         config, models (the snapshot), layout (every file path), stats
  adapters/     everything that differs between clouds and node autoscalers
    clouds/       CloudProvider interface (base.py); aws/ (eks.py, ec2.py);
                  oci/ (oke.py, shapes.py, pricing.py)
    autoscalers/  NodeAutoscaler interface (base.py); cluster_autoscaler.py,
                  karpenter.py, karpenter_oci.py
  sources/      read-only collection: thanos, queries (every PromQL string), kube,
                kfacts, local (cluster directories), shell (the read-only guard),
                collect, hpa_history, trends, ngtrends, network, storage
  engine/       replays of the cluster's controllers: autoscaler and careplay
                (Cluster Autoscaler v1.35), kscheduler, eviction, hpa_replay,
                pressure, realistic, simulate (packing and options)
  analysis/     recommend, blockers, actions, advise, plan
  output/       report (Markdown/JSON), console (the console export)
export_ui.py    wrapper for output/console.py, so `python3 export_ui.py` still works
scripts/        one-off maintenance (migrate_layout.py)
tests/
```

The console's TypeScript ports name the Python module they mirror (for
example `src/lib/realistic.ts` and `rightsizer/engine/realistic.py`); keep
them in step.

## Clouds and node autoscalers

Nothing outside `rightsizer/adapters/` names a cloud or an autoscaler. The rest
of the code asks an adapter:

| Question | Cloud adapter (`CloudProvider`) | Autoscaler adapter (`NodeAutoscaler`) |
|---|---|---|
| Which group is this node in? | `nodegroup_label_keys` (EKS: `eks.amazonaws.com/nodegroup`) | `group_label_keys` (Karpenter: `karpenter.sh/nodepool`) |
| Group bounds | `collect_nodegroups`, `collect_scaling_groups` | |
| Instance types and prices | `describe_instance_types`, `on_demand_price`, `candidate_ladder` | |
| Is it running, how is it set? | | `discover`, `from_flags` (`--ca-arg`) |
| What will it remove? | | `scale_down_settings`, `notes` |

**Which adapter a run uses.** `collect --cloud` names the cloud, otherwise it is
recognised from the context (an EKS ARN is `aws`). `collect --autoscaler` names
the autoscaler, otherwise each registered one tries to discover itself, in
order. Both are recorded in the snapshot (`meta.cloud`, `autoscaler`), so
`analyze`, `plan` and the console export use the same ones. Snapshots from
before this was recorded read as AWS and Cluster Autoscaler, exactly as before.

**Node groups in PromQL.** Every node-group query goes through
`queries.node_label_join`, which renames each registered adapter's group label
to one label, `nodegroup`. A new cloud's label is picked up by every query
with no other change. kube-state-metrics must expose the label on
`kube_node_labels` (`--metric-labels-allowlist=nodes=[...]`).

**What is modelled.** The engine replays Cluster Autoscaler v1.35.0 for the
groups it manages. For a Karpenter NodePool whose nodes and pods were read, it
replays Karpenter itself (`engine/karpenter.py`, written from v1.14.1):

* **Provisioning**: pending pods sorted by CPU then memory request, each added
  to an existing node, else to an in-flight claim (fewest pods first), else to
  a new claim whose candidate sizes are those the pod's requirements, volume
  zone, taints and the pool's remaining `limits` allow; each claim launches
  the cheapest size left on its list, in the zone the claim is pinned to.
* **Consolidation**: empty nodes go first; then a multi-node pass (binary
  search over the cheapest candidates, replaced by at most one cheaper node)
  and a single-node pass (delete, or replace with one cheaper size). A node
  stays when a pod on it is `karpenter.sh/do-not-disrupt`, has no controller,
  or has its PodDisruptionBudget exhausted; a spot node is only replaced
  spot-to-spot when 15 cheaper sizes remain.

The fleet it arrives at is the answer, so a pool's options read *Karpenter
runs 1 × 4ocpu-16gb* rather than a node count anyone sets. Where the pool's
nodes or pods were not recorded (an older snapshot, or no kube access), the
old mapping onto Cluster Autoscaler's rules is used and reported as an
approximation on the command line, in the console's Overview and on the
Clusters view.

### OCI (OKE) with Karpenter for OCI

OKE runs managed node pools; Karpenter for OCI (KPO) launches everything else
as self-managed instances that belong to no OKE pool. So the groups come from
two adapters: `--cloud oci` reads the managed pools (`oci ce node-pool list`)
and prices shapes, `--autoscaler karpenter-oci` turns each Karpenter NodePool
into a group from its OCINodeClass.

* **Instance types are shape and size**: `VM.Standard.E5.Flex:4ocpu-32gb`,
  with `:preemptible` for a spot pool (half price). A Flex shape name alone is
  every size, so a node's size is matched from its capacity against the sizes
  its NodeClass offers (`spec.shapeConfigs`); a pool running mixed sizes is
  priced at its commonest, and the run says so.
* **Prices** come from Oracle's public price list (no credentials), cached a
  week in `~/.cache/rightsizer/oci-prices.json`. OCI prices every region the same.
* **A NodePool has no floor** and its ceiling is `spec.limits`, shown as a node
  count at the current size.
* **Scale-down** is Karpenter's consolidation, replayed (see *What is
  modelled* above); the Clusters view's panel shows the fleet it runs today
  and the fleet it consolidates to with nothing changed.

**What a recommendation means for a Karpenter pool.** Nobody sets the node
count or the type of a Karpenter pool: Karpenter picks the size of every node
it launches from the pool's `shapeConfigs`, and consolidates onto a cheaper
listed size when the pods fit it -- by their *requests*. So every option is
the two things an operator can change, replayed through Karpenter, and the
row is the fleet Karpenter then runs (`fleet` in the export, which may mix
sizes):

1. **Request changes only.** The pods' requests are edited and Karpenter is
   replayed with the pool's own `shapeConfigs`. The fleet it consolidates to
   is the option; no other step is involved.
2. **A new rung.** The same, with one more size offered. The step names the
   `shapeConfigs` entry to add, and the Commands section shows the
   `karpenter.hcl` diff with the existing rungs kept -- removing a rung makes
   Karpenter replace every node running it (drift). A rung is only offered
   when Karpenter actually moves onto it in the replay.
3. **An OKE managed pool** (the `system` pool) is updated in place:
   `oci ce node-pool update --node-shape-config`, then its nodes are cycled.

The extra rungs tried are the same shape at 2, 4, 8, 16 and 32 GB per OCPU,
between half the smallest and twice the largest OCPU count the pool lists --
a memory-bound pool is offered more memory per OCPU, a CPU-bound one less,
which a fixed instance family cannot. Each size is packed at what Karpenter
for OCI itself computes a node of that size has for pods
(`vm_memory_overhead.go` and the kubelet's reservation, which grows with the
node), not at the running node's overhead. The console shows a pool's fleet in
the Instance column and has no type picker for it: picking a type Karpenter
would not choose is not something that can happen.

Against one cluster's own Prometheus (no Thanos), with 14 days of retention:

```bash
kubectl --context <oke-context> -n <monitoring-ns> port-forward svc/<prometheus> 9090 &

export RIGHTSIZER_GROUP_LABELS=example.com/node-pool   # names the OKE `system` pool
rightsizer --env oke-dev1 --no-local --autoscaler karpenter-oci \
    --prometheus-url http://localhost:9090 --days 14 \
    --kubeconfig ~/.kube/oke-dev1 --context <oke-context> run
python3 export_ui.py --env oke-dev1
```

* `--prometheus-url` drops the `env` matcher the shared Thanos needs; the
  snapshot records the source, so `trends`, `hpa-history` and the rest query
  the same Prometheus without the flag again.
* `--days` must fit the retention: collect refuses a window that starts
  before the oldest data, rather than sizing from half of it.
* kube-state-metrics must expose the group labels on `kube_node_labels`
  (`--metric-labels-allowlist=nodes=[karpenter.sh/nodepool,example.com/node-pool]`);
  collect refuses to run when no node carries one.
* The context is enough: a kubeconfig from `oci ce cluster create-kubeconfig`
  runs `oci ce cluster generate-token --cluster-id <ocid> --region <r>`, so
  `--cloud oci`, `--cluster` (the OCID the OCI CLI needs), `--region` and
  `--profile` are read from it. Any of them given explicitly wins.


1. Write `rightsizer/adapters/clouds/<name>/__init__.py` with a `CloudProvider`
   subclass: its `name`, `cli`, `nodegroup_label_keys`, `gpu_label_keys`,
   `matches_context`, and the abstract methods (node groups, catalogue,
   prices, `family_ladder`).
2. Register it: one line in `BUILTIN` in `adapters/clouds/__init__.py`, or,
   from another package, an entry point:
   ```toml
   [project.entry-points."rightsizer.clouds"]
   gcp = "rightsizer_gcp:GCPProvider"
   ```
3. Allow its CLI's read-only commands in `READONLY_CLIS` in `sources/shell.py`,
   with tests in `tests/test_readonly.py`. This is the only step that touches
   the read-only guard, deliberately: registering an adapter can never widen it.
4. Add it to `tests/test_adapters.py`. `FakeCloud` there is a complete, minimal
   example.

### Adding a node autoscaler

1. Write `rightsizer/adapters/autoscalers/<name>.py` with a `NodeAutoscaler`
   subclass: `discover` (read-only), `group_label_keys`, `scale_down_settings`,
   `drain_veto` (what it refuses to drain beyond Cluster Autoscaler's rules)
   and `notes`.
2. Set `models_scale_down = False` unless the engine replays it natively, and
   say in `notes` what the approximation is.
3. Register it in `BUILTIN` in `adapters/autoscalers/__init__.py` (order is the
   discovery order) or with a `rightsizer.autoscalers` entry point.

## Tests

```bash
python3 -m pytest tests/ -q
```

The suite asserts the invariants that would otherwise fail silently: every
selector pins `env` (Thanos is multi-tenant with no cluster label), join operands
are de-duplicated, DaemonSet exclusion uses `unless`, no percentile is computed in
PromQL, a blocked recommendation contributes zero savings, and a group with no
evidence is never sized.

## Where a new node lands

Cluster Autoscaler never launches a node: it raises the Auto Scaling group's
desired capacity, and the ASG launches the instances and picks their zones.
The replay keeps that split -- CA decides how many, the ASG decides where:

* `collect` records each ASG's AvailabilityZones and each instance's zone
  (`kube.asgs[].zones`, `instance_zones`); a snapshot from before that uses
  the zones its nodes stand in.
* A new instance goes to the ASG's zone with the fewest of the group's
  instances, ties in the ASG's own order -- the ASG's balancing rule. The
  modelled node carries that zone's labels (`topology.kubernetes.io/zone`,
  `failure-domain.beta.kubernetes.io/zone`, `topology.ebs.csi.aws.com/zone`,
  `topology.k8s.aws/zone-id`), its type's instance store or the root volume,
  and the group's taints and labels.
* Cluster Autoscaler sizes a scale-up with a *running* node of the group as
  its template -- one zone. A pod whose EBS volume is in another zone does
  not fit that template, so CA does not raise desired capacity for it,
  exactly as upstream behaves on a multi-zone ASG. The replay keeps that:
  such a pod stays Pending unless an instance lands in its zone for some
  other reason.
* kube-scheduler then places the pods, PV node affinity included.
* Not modelled: the ASG's own AZ rebalancing (a later launch in one zone and
  termination in another) and spot Capacity Rebalance. CA's scale-down, which
  terminates a specific instance with desired capacity decremented, is a
  plain removal.

This is what makes a three-zone database group (one volume per zone) come
out at three nodes of a smaller type rather than "unplaceable": a replacement
group spanning the same subnets gets one node per zone, and each database
follows its volume.

## Cluster Autoscaler

CA is what actually removes nodes, so its configuration bounds every scale-in
claim. The tool models its scale-down rule rather than assuming a perfect repack.

Flags come from one of two places, in this order — **neither needs a checkout of
the GitOps repo**:

```bash
# 1. explicit, self-contained, works anywhere
rightsizer --env prod-us1 --no-ca-discovery \
  --ca-arg scale-down-utilization-threshold=0.5 \
  --ca-arg ignore-daemonsets-utilization=true \
  --ca-arg scale-down-unneeded-time=5m run

# 2. read off the running deployment (default when --context is given)
rightsizer --env prod-us1 --context prod-us1 run
```

Anything unset falls back to the upstream default, and the configuration actually
used is recorded in the snapshot and printed at collection time — so a report
always says what it assumed.

The replay follows **Cluster Autoscaler v1.35.0** — the version the GitOps
chart deploys (`cluster-autoscaler-1.35.0`, chart 9.57.0) — read from its source
(`core/scaledown`, `simulator/drainability/rules`). One pass is one CA loop:

1. **Candidates** — every node at or below the threshold, utilisation being the
   larger of CPU and memory requests over allocatable. With
   `ignore-daemonsets-utilization` (and `ignore-mirror-pods-utilization`) those
   pods leave the sum *and* the allocatable. Order: empty nodes, then last pass's
   candidates, then node order. Nodes annotated `scale-down-disabled` are skipped.
2. **Simulated removal**, one candidate at a time. Each pod goes through CA's
   drain rules in upstream order — the first that decides wins:

   | Rule | Outcome |
   |---|---|
   | mirror / DaemonSet pod | not moved |
   | `safe-to-evict: "true"` | drainable — **overrides every rule below** |
   | no controller, or a controller other than ReplicaSet/StatefulSet/Job/RC | blocks (`skip-nodes-with-custom-controller-pods`) |
   | kube-system pod with no PDB | blocks, **until it is older than** `blocking-system-pod-distruption-timeout` (1h) |
   | `safe-to-evict: "false"` | blocks |
   | hostPath, or emptyDir not `medium: Memory`, not in `safe-to-evict-local-volumes` | blocks (`skip-nodes-with-local-storage`) |
   | PDB with **no** disruption left | blocks; more pods than the budget allows at once only makes the node *risky* (drained last) |

   Then every pod that must move is tried on the other nodes — in the node's pod
   order, each search starting after the node the previous pod landed on —
   against CPU, memory, max-pods, cordons, nodeSelector/affinity, taints,
   hostname anti-affinity, EBS volume zone and hard topology spread. A removal
   that works is kept for the rest of the pass, and the PDB budgets it used are
   spent.
3. **Deletion** — every empty unneeded node (up to `max-scale-down-parallelism`,
   10) and **one** node that needs draining (`max-drain-parallelism`), never
   below minSize.
4. **Rescheduling** — drained pods land where **kube-scheduler** puts them: the
   node scoring highest on least-allocated plus balanced-allocation. That is
   usually the *emptiest* node — often the next one CA wanted to remove — so
   consolidation stalls earlier than a packing suggests. A pod nothing fits goes
   Pending and CA adds a node.

The loop repeats until a pass removes nothing. Timing (`scale-down-unneeded-time`,
drain duration) changes when, not whether, and is not modelled. Pods only move
within their node group (nearly every workload here is pinned to one). Karpenter's
`do-not-disrupt` is not a CA rule and is ignored.

A request increase that leaves a pod larger than an empty node of the group makes
the option **unplaceable** — dropped, not priced with an extra node.

Facts the rules need, and where they come from: pod age (`rightsizer pod-ages`,
or any new `collect`); emptyDir medium, `safe-to-evict-local-volumes`, hard
topology spread and node `scale-down-disabled` (a `collect` with `--context`;
older snapshots count every emptyDir as local storage and skip spread).
