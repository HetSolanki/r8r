"""Constraint-aware bin-packing simulation.

Answers the question the arithmetic cannot: *given these pods and these
constraints, how many nodes of a given instance type are actually needed?*

The closed-form floor -- ``ceil(sum(requests) / allocatable)`` -- assumes capacity
is perfectly divisible. It is not. Real placement is defeated by three things the
division cannot express:

* **Fragmentation.** Ten 600m pods do not fit into six 1-core slots. A node with
  400m left cannot take another 600m pod, and that remainder is stranded.
* **Anti-affinity.** A workload with required anti-affinity on hostname needs one
  node per replica no matter how small the pods are.
* **Pod density.** The ENI-derived maxPods ceiling binds before CPU or memory on
  clusters running many small pods.

Algorithm is **First-Fit Decreasing**, the standard bin-packing heuristic: sort
pods largest-first, place each in the first node that fits. FFD is guaranteed
within 11/9 of optimal, and it mirrors how a scheduler behaves in practice better
than an exact solver would -- kube-scheduler is itself greedy, so an optimal
packing would be an answer the cluster never reaches.

Determinism is a requirement, not a nicety: the sort is fully ordered including a
name tiebreak, so the same snapshot always yields the same node count.
"""

from __future__ import annotations

import math

from dataclasses import dataclass, field

GIB = 1024**3


@dataclass(frozen=True)
class Pod:
    name: str
    workload: str
    cpu: float          # cores -- the footprint used for packing
    memory: float       # bytes -- ditto
    antiaffinity: str = ""   # non-empty: at most one such pod per node
    # True when observed usage, not the declared request, set the footprint.
    # A container with no limit can burst well past its request; packing such a
    # pod by its request alone fills a node that then cannot hold it.
    usage_driven: bool = False

    @property
    def sort_key(self) -> tuple:
        # Largest first by the more binding dimension, then a stable name tiebreak.
        return (-max(self.cpu, self.memory / GIB / 4), -self.cpu, self.name)


@dataclass
class Node:
    cpu_free: float
    mem_free: float
    pods_free: int
    antiaffinity_present: set[str] = field(default_factory=set)
    pods: list[Pod] = field(default_factory=list)

    def fits(self, pod: Pod) -> bool:
        if self.pods_free <= 0:
            return False
        if pod.cpu > self.cpu_free or pod.memory > self.mem_free:
            return False
        if pod.antiaffinity and pod.antiaffinity in self.antiaffinity_present:
            return False
        return True

    def place(self, pod: Pod) -> None:
        self.cpu_free -= pod.cpu
        self.mem_free -= pod.memory
        self.pods_free -= 1
        if pod.antiaffinity:
            self.antiaffinity_present.add(pod.antiaffinity)
        self.pods.append(pod)


@dataclass
class PackResult:
    nodes_required: int
    unplaceable: list[Pod]
    cpu_utilization: float
    mem_utilization: float
    pod_utilization: float
    binding: str
    # Where each pod actually landed. Kept because a node count is a claim and
    # the placement is the evidence for it -- without it nobody can see whether
    # the nodes come out evenly filled or one is carrying a single large pod.
    placement: list[Node] = field(default_factory=list)

    @property
    def feasible(self) -> bool:
        return not self.unplaceable


def pack(
    pods: list[Pod],
    *,
    cpu_per_node: float,
    mem_per_node: float,
    max_pods_per_node: int,
    headroom: float = 1.0,
    node_limit: int = 2000,
) -> PackResult:
    """First-Fit Decreasing onto identical nodes. Capacities are *allocatable*.

    ``headroom`` is the share of allocatable the packer is allowed to fill, and it
    is not optional in practice. Packing to 100% produces a node with nowhere to
    put a pod during a rolling update's surge, nothing spare when a container
    exceeds the peak it happened to reach in the observed window, and no slack for
    kubelet eviction thresholds. A recommendation that leaves a node at 100% is
    arithmetically valid and operationally useless.

    It applies to the *recommendation* path only. Cluster Autoscaler keeps no
    headroom of its own, so :func:`simulate_ca_scale_down` deliberately does not
    use this -- that function predicts behaviour rather than proposing a target.

    Nodes are opened on demand; the count returned is how many were needed. A pod
    larger than one whole node is reported as unplaceable rather than silently
    dropped -- that is a real condition (an oversized request) and hiding it would
    produce a node count that cannot run the workload.
    """
    if cpu_per_node <= 0 or mem_per_node <= 0 or max_pods_per_node <= 0:
        return PackResult(0, list(pods), 0.0, 0.0, 0.0, "no capacity")

    # Fill only up to the headroom target. Utilisation is still reported against
    # full allocatable, so a reader sees how full the node really is rather than
    # how full it is relative to a self-imposed ceiling.
    usable_cpu = cpu_per_node * headroom
    usable_mem = mem_per_node * headroom
    usable_pods = max(1, int(max_pods_per_node * headroom))

    nodes: list[Node] = []
    unplaceable: list[Pod] = []

    for pod in sorted(pods, key=lambda p: p.sort_key):
        # Oversized is measured against full allocatable: a pod that fits the node
        # but not the headroom budget is a packing constraint, not a broken pod.
        if pod.cpu > cpu_per_node or pod.memory > mem_per_node:
            unplaceable.append(pod)
            continue
        for node in nodes:
            if node.fits(pod):
                node.place(pod)
                break
        else:
            if len(nodes) >= node_limit:
                unplaceable.append(pod)
                continue
            fresh = Node(usable_cpu, usable_mem, usable_pods)
            fresh.place(pod)
            nodes.append(fresh)

    if not nodes:
        return PackResult(0, unplaceable, 0.0, 0.0, 0.0, "no pods")

    n = len(nodes)
    cpu_used = sum(usable_cpu - x.cpu_free for x in nodes)
    mem_used = sum(usable_mem - x.mem_free for x in nodes)
    pods_used = sum(usable_pods - x.pods_free for x in nodes)

    cpu_u = cpu_used / (cpu_per_node * n)
    mem_u = mem_used / (mem_per_node * n)
    pod_u = pods_used / (max_pods_per_node * n)
    binding = max(
        (cpu_u, "cpu"), (mem_u, "memory"), (pod_u, "pod density"), key=lambda t: t[0]
    )[1]
    return PackResult(n, unplaceable, cpu_u, mem_u, pod_u, binding, nodes)


def theoretical_floor(
    pods: list[Pod], *, cpu_per_node: float, mem_per_node: float, max_pods_per_node: int
) -> int:
    """The division-based floor, for comparison against the packed result.

    The gap between this and :func:`pack` is fragmentation -- capacity that exists
    but cannot be used because it is spread across nodes in unusable remainders.
    """
    if not pods or cpu_per_node <= 0 or mem_per_node <= 0:
        return 0
    return max(
        math.ceil(sum(p.cpu for p in pods) / cpu_per_node),
        math.ceil(sum(p.memory for p in pods) / mem_per_node),
        math.ceil(len(pods) / max_pods_per_node),
        1,
    )


# --------------------------------------------------------------------------
# building a pod set from a snapshot, and the what-if sweep
# --------------------------------------------------------------------------


def _daemonset_pods_per_node(group_state) -> int:
    """DaemonSet pods a node carries. One per DaemonSet, so it does not change
    with the node count -- which is why it is overhead rather than load."""
    if group_state.nodes_current <= 0:
        return 0
    return round(group_state.daemonset_pods / group_state.nodes_current)


@dataclass
class Option:
    """One candidate instance type for a node group, fully simulated."""

    instance_type: str
    nodes: int
    monthly_usd: float
    delta_usd: float
    cpu_utilization: float
    mem_utilization: float
    pod_utilization: float
    binding: str
    feasible: bool
    theoretical_nodes: int
    note: str = ""

    @property
    def fragmentation_nodes(self) -> int:
        """Nodes lost to remainders the division-based floor cannot see."""
        return max(0, self.nodes - self.theoretical_nodes)


def pods_for_nodegroup(
    snapshot,
    group_name: str,
    *,
    apply: str = "current",
    recommendations: list | None = None,
    group_state=None,
    plan_targets: dict | None = None,
) -> list[Pod]:
    """The pod set on one node group.

    Built from **observed placement** -- which pod is on which node, right now --
    when the snapshot has it. That replaces three approximations at once:
    attributing a workload to its majority node group, multiplying by a 30-day
    peak replica count, and then scaling the result back down to reconcile. Each
    of those was a source of error, and none is needed once the real placement is
    available.

    DaemonSet pods are excluded: they run one per node, so they are per-node
    overhead subtracted from capacity rather than items to place.

    ``apply`` selects what to change: ``current`` as configured, ``memory`` for
    memory increases only, ``all`` for every actionable request change, and
    ``replicas`` to drop the surplus replicas an over-set HPA floor is holding.
    A blocked recommendation is never applied.

    ``plan_targets`` overrides all of that with explicit per-container requests,
    keyed ``(namespace, workload, container)`` -> ``{"cpu": cores, "mem": bytes}``.
    Containers it does not name keep what they have, which is the point: a plan
    is a few edits, not a sweep.
    """
    placed = (snapshot.placements or {}).get(group_name)
    if placed:
        return _pods_from_placement(snapshot, placed, apply, recommendations, plan_targets)
    if plan_targets:
        raise ValueError(
            f"node group {group_name!r} has no recorded placement; a plan cannot be "
            "simulated against attributed pods -- re-run `collect` to record placement"
        )
    return _pods_from_attribution(snapshot, group_name, apply, recommendations, group_state)


def _recommendation_index(recommendations: list | None) -> dict[tuple, object]:
    return {
        (r.namespace, r.workload, r.kind, r.container): r
        for r in (recommendations or [])
        if r.actionable
    }


def _peak_index(snapshot) -> dict[tuple, dict[str, float]]:
    """Observed peak per replica, summed over a workload's containers."""
    peak: dict[tuple, dict[str, float]] = {}
    for stat in snapshot.usage:
        entry = peak.setdefault(
            (stat.namespace, stat.workload, stat.kind), {"cpu": 0.0, "mem": 0.0}
        )
        if stat.resource == "cpu":
            entry["cpu"] += stat.peak
        else:
            entry["mem"] += stat.peak
    return peak


def plan_deltas(snapshot, plan_targets: dict) -> dict[str, dict[str, float]]:
    """Per-pod request change for each workload a plan touches.

    A plan names containers; a placement row is a whole pod. The change is
    summed over the workload's planned containers and applied to the pod. One
    definition, shared by the packer and the Cluster Autoscaler replay, so the
    two can never simulate different edits.
    """
    stats = {(u.namespace, u.workload, u.container): u
             for u in snapshot.usage if u.resource == "cpu"}
    mem_stats = {(u.namespace, u.workload, u.container): u
                 for u in snapshot.usage if u.resource == "mem"}
    deltas: dict[str, dict[str, float]] = {}
    for tkey, target in plan_targets.items():
        cur_cpu = getattr(stats.get(tkey), "request", None)
        cur_mem = getattr(mem_stats.get(tkey), "request", None)
        kind = getattr(stats.get(tkey), "kind", None) or getattr(
            mem_stats.get(tkey), "kind", ""
        )
        d = deltas.setdefault(f"{tkey[0]}/{tkey[1]}/{kind}", {"cpu": 0.0, "mem": 0.0})
        if target.get("cpu") is not None and cur_cpu is not None:
            d["cpu"] += target["cpu"] - cur_cpu
        if target.get("mem") is not None and cur_mem is not None:
            d["mem"] += target["mem"] - cur_mem
    return deltas


def _pods_from_placement(
    snapshot, placed, apply, recommendations, plan_targets: dict | None = None
) -> list[Pod]:
    recs = _recommendation_index(recommendations)
    # `apply` is a set of levers joined by "+", because they are not
    # alternatives in reality: a team cutting an over-set HPA floor generally
    # fixes the requests in the same PR. Searching them only separately hid the
    # combination entirely -- on services-1 right-sizing alone is *worse* than
    # doing nothing, so the only row that looked good was the replica one.
    levers = set(apply.split("+"))
    # When this option also changes requests, the floor has to be computed
    # against those requests -- otherwise the two halves of one option
    # contradict each other.
    replicas = (
        replica_running(snapshot, recommendations if levers & {"all", "memory"} else None)
        if "replicas" in levers else {}
    )
    kept: dict[str, int] = {}
    peaks = _peak_index(snapshot)
    antiaffinity = {
        f"{w.get('namespace')}/{w.get('name')}/{w.get('kind')}"
        for w in snapshot.facts.workload_constraints
        if w.get("hard_antiaffinity_hostname")
    }

    # Recommendations are per container; a placement row is a whole pod. Sum the
    # per-container deltas for the workload and apply them to the pod's request.
    deltas: dict[str, dict[str, float]] = {}
    if plan_targets:
        deltas = plan_deltas(snapshot, plan_targets)
    else:
        wants_all = "all" in levers
        wants_mem = wants_all or "memory" in levers
        for (ns, wl, kind, _container), rec in recs.items():
            key = f"{ns}/{wl}/{kind}"
            d = deltas.setdefault(key, {"cpu": 0.0, "mem": 0.0})
            if rec.cpu_recommended is not None and wants_all:
                d["cpu"] += rec.cpu_recommended - rec.cpu_request
            if rec.mem_recommended is not None and wants_mem:
                if wants_all or rec.mem_recommended > rec.mem_request:
                    d["mem"] += rec.mem_recommended - rec.mem_request

    pods: list[Pod] = []
    for row in placed:
        if row.get("owner_kind") == "DaemonSet":
            continue
        wkey = row.get("workload_key", "")
        # Under the replica lever, keep only as many pods of a workload as its
        # suggested floor allows. Which pods go is arbitrary -- for capacity the
        # count is what matters, and the scheduler picks in practice.
        if wkey in replicas:
            seen = kept.get(wkey, 0)
            if seen >= replicas[wkey]:
                continue
            kept[wkey] = seen + 1
        delta = deltas.get(wkey, {"cpu": 0.0, "mem": 0.0})
        cpu = max(0.0, row.get("cpu", 0.0) + delta["cpu"])
        mem = max(0.0, row.get("mem", 0.0) + delta["mem"])

        # A pod's real footprint is the larger of what it reserves and what it
        # has been measured using -- a container with no limit can burst well
        # past its request, and packing it by the request fills a node that then
        # cannot hold it.
        ns, _, rest = wkey.partition("/")
        wl, _, kind = rest.rpartition("/")
        observed = peaks.get((ns, wl, kind), {"cpu": 0.0, "mem": 0.0})
        usage_driven = observed["cpu"] > cpu * 1.01 or observed["mem"] > mem * 1.01
        pods.append(
            Pod(
                name=f"{row.get('namespace')}/{row.get('pod')}",
                workload=wkey,
                cpu=max(cpu, observed["cpu"]),
                memory=max(mem, observed["mem"]),
                antiaffinity=wkey if wkey in antiaffinity else "",
                usage_driven=usage_driven,
            )
        )
    return pods


def _pods_from_attribution(
    snapshot, group_name: str, apply: str, recommendations, group_state
) -> list[Pod]:
    """Fallback for a snapshot collected before placement was recorded."""
    from rightsizer.analysis.recommend import dominant_nodegroup

    rec_index: dict[tuple, object] = {}
    for r in recommendations or []:
        if r.actionable:
            rec_index[(r.namespace, r.workload, r.kind, r.container)] = r

    per_workload: dict[tuple, dict] = {}
    for stat in snapshot.usage:
        key = (stat.namespace, stat.workload, stat.kind)
        if stat.kind == "DaemonSet":
            continue
        if dominant_nodegroup(snapshot, stat)[0] != group_name:
            continue
        entry = per_workload.setdefault(
            key, {"cpu": 0.0, "mem": 0.0, "replicas": max(stat.replicas, 1)}
        )
        entry["replicas"] = max(entry["replicas"], max(stat.replicas, 1))

        rec = rec_index.get((stat.namespace, stat.workload, stat.kind, stat.container))
        value = stat.request
        if rec is not None and apply != "current":
            if stat.resource == "mem" and rec.mem_recommended is not None:
                # under "memory", only raise -- never bank a memory cut as savings
                if apply == "all" or rec.mem_recommended > stat.request:
                    value = rec.mem_recommended
            elif stat.resource == "cpu" and apply == "all" and rec.cpu_recommended is not None:
                value = rec.cpu_recommended

        if stat.resource == "cpu":
            entry["cpu"] += value
        else:
            entry["mem"] += value

    antiaffinity = {
        f"{w.get('namespace')}/{w.get('name')}/{w.get('kind')}"
        for w in snapshot.facts.workload_constraints
        if w.get("hard_antiaffinity_hostname")
    }

    # Observed peak per replica. A request is what the scheduler reserves; peak
    # usage is what the pod actually needs. Where a container has no limit the
    # second can be many times the first, and a node packed to its requests then
    # has no room for the reality. The footprint is therefore the larger of the
    # two -- reserving less than a pod demonstrably uses is not a saving, it is
    # an incident.
    peak: dict[tuple, dict[str, float]] = {}
    for stat in snapshot.usage:
        key = (stat.namespace, stat.workload, stat.kind)
        entry = peak.setdefault(key, {"cpu": 0.0, "mem": 0.0})
        if stat.resource == "cpu":
            entry["cpu"] += stat.peak
        else:
            entry["mem"] += stat.peak

    # `replicas` is the 30-day MAXIMUM each workload reached, so summing it across
    # workloads reconstructs a pod set that never existed at one instant. On a
    # bursty group -- engine jobs churning through the month -- that inflates the
    # fleet by an order of magnitude.
    #
    # Reconcile against the observed pod count. Scaling by CPU instead, as this
    # did, made the factor depend on which request set was being applied: the
    # right-sized pass has smaller requests, so it scaled less, and the same
    # physical pods came out at different counts in different scenarios. Pod
    # count is a property of the group, not of the scenario.
    scale = 1.0
    if group_state is not None and group_state.schedulable_pods > 0:
        reconstructed = sum(e["replicas"] for e in per_workload.values())
        target = group_state.schedulable_pods
        if reconstructed > target * 1.05:
            scale = target / reconstructed

    pods: list[Pod] = []
    for key, entry in sorted(per_workload.items()):
        namespace, workload, kind = key
        wkey = f"{namespace}/{workload}/{kind}"
        observed = peak.get(key, {"cpu": 0.0, "mem": 0.0})
        # A pod with no declared request still occupies a maxPods slot and still
        # uses CPU and memory. Dropping it -- as this did -- removes most of the
        # fleet on a group with low request coverage and makes every density
        # calculation wrong. Keep it, sized by what it actually uses.
        if entry["cpu"] <= 0 and entry["mem"] <= 0 and observed["cpu"] <= 0 and observed["mem"] <= 0:
            continue
        cpu = max(entry["cpu"], observed["cpu"])
        mem = max(entry["mem"], observed["mem"])
        driven = cpu > entry["cpu"] * 1.01 or mem > entry["mem"] * 1.01

        is_anti = wkey in antiaffinity
        # A workload pinned one-per-node has a replica count that is a hard
        # scheduling floor, not a statistic. Scaling it down to reconcile a
        # bursty group would quietly delete the very constraint that sets the
        # node count.
        if scale < 1.0 and not is_anti:
            count = max(1, round(entry["replicas"] * scale))
        else:
            count = entry["replicas"]

        for i in range(count):
            pods.append(
                Pod(
                    name=f"{wkey}#{i}",
                    workload=wkey,
                    cpu=cpu,
                    memory=mem,
                    antiaffinity=wkey if is_anti else "",
                    usage_driven=driven,
                )
            )
    return pods


def whatif(
    snapshot,
    group_state,
    ladder,
    *,
    current_type: str,
    price_of,
    max_pods: int,
    pods: list[Pod] | None = None,
    headroom: float = 0.85,
) -> list[Option]:
    """Simulate every same-family instance size and price each outcome.

    System overhead (kubelet + system reserved) is carried across as an
    **absolute** amount measured on the running nodes, not a proportion: a smaller
    instance does not get a smaller kube-reserved.
    """
    pods = pods if pods is not None else pods_for_nodegroup(
        snapshot, group_state.name, group_state=group_state
    )
    current = next((i for i in ladder if i.name == current_type), None)
    if not current or not pods:
        return []

    overhead_cpu = max(0.0, current.vcpu - group_state.alloc_cpu_per_node)
    overhead_mem = max(0.0, current.memory_gib * GIB - group_state.alloc_mem_per_node)
    ds_cpu = group_state.ds_cpu_per_node
    ds_mem = group_state.ds_mem_per_node
    current_cost = price_of(current_type) * group_state.nodes_current

    options: list[Option] = []
    for cand in ladder:
        cpu_alloc = cand.vcpu - overhead_cpu - ds_cpu
        mem_alloc = cand.memory_gib * GIB - overhead_mem - ds_mem
        if cpu_alloc <= 0 or mem_alloc <= 0:
            continue
        result = pack(
            pods,
            cpu_per_node=cpu_alloc,
            mem_per_node=mem_alloc,
            max_pods_per_node=max_pods or 110,
        )
        floor = theoretical_floor(
            pods,
            cpu_per_node=cpu_alloc,
            mem_per_node=mem_alloc,
            max_pods_per_node=max_pods or 110,
        )
        cost = price_of(cand.name) * result.nodes_required
        note = ""
        if not result.feasible:
            note = f"{len(result.unplaceable)} pod(s) exceed one node"
        elif result.nodes_required > floor:
            note = f"+{result.nodes_required - floor} node(s) lost to fragmentation"
        options.append(
            Option(
                instance_type=cand.name,
                nodes=result.nodes_required,
                monthly_usd=cost,
                delta_usd=cost - current_cost,
                cpu_utilization=result.cpu_utilization,
                mem_utilization=result.mem_utilization,
                pod_utilization=result.pod_utilization,
                binding=result.binding,
                feasible=result.feasible,
                theoretical_nodes=floor,
                note=note,
            )
        )
    return options


# --------------------------------------------------------------------------
# scenario matrix
# --------------------------------------------------------------------------


@dataclass
class Scenario:
    """One way of changing a node group, fully simulated and priced.

    The four scenarios are deliberately *not* alternatives to choose between by
    eye -- they decompose one decision. Right-sizing and re-typing interact:
    smaller requests change which instance size packs best, so "both" is rarely
    the sum of the two done separately. Reporting only the combined figure hides
    which half earned it; reporting only one hides the interaction.
    """

    key: str              # asis | rightsize | retype | both
    label: str
    instance_type: str
    nodes: int
    monthly_usd: float
    delta_usd: float      # signed, against as-is
    cpu_utilization: float
    mem_utilization: float
    binding: str
    feasible: bool
    note: str = ""
    requires: list[str] = field(default_factory=list)
    # Per-node allocatable after system and DaemonSet overhead, so a reader can
    # see what "a node" means in this scenario rather than trusting the count.
    node_cpu: float = 0.0
    node_mem_gib: float = 0.0
    node_max_pods: int = 0
    # Share of allocatable the packer was allowed to fill.
    headroom: float = 1.0
    # True when the placement shown is observed rather than simulated.
    measured: bool = False
    # "saves" | "costs" | "neutral" -- an option that costs more is still an
    # option. When the current shape is short of capacity, the useful answer is
    # an up-size, and filtering to savings would hide the finding entirely.
    direction: str = "neutral"
    # Why this shape is worth looking at even though it is not the cheapest.
    rationale: str = ""
    # True when scheduling constraints were unavailable and this scenario reduces
    # the node count anyway -- the figure is an upper bound, not a recommendation.
    unverified: bool = False
    # Pods whose footprint came from observed usage rather than their request.
    usage_driven_pods: int = 0
    # A different instance type from today's, and the steps that reach it --
    # a subset of `requires`, in the group owner's words (replace the node
    # group / update the pool's shape config / add a Karpenter rung).
    retype: bool = False
    retype_steps: list[str] = field(default_factory=list)
    #: For a Karpenter pool: the fleet Karpenter arrives at, [{type, nodes}],
    #: which may mix sizes. `instance_type` is then the commonest size and
    #: `nodes` the total; neither is something an operator sets.
    fleet: list[dict] = field(default_factory=list)
    #: On the baseline of a Karpenter pool: what consolidation does with no
    #: edits at all -- the fleet after, its cost, and the nodes it keeps and why.
    karpenter: dict | None = None
    vcpu: int = 0
    mem_gib: float = 0.0
    hourly_usd: float = 0.0
    # Local disk before and after, from :func:`rightsizer.sources.storage.assess`.
    storage: dict = field(default_factory=dict)
    # Network bandwidth before and after, from :func:`rightsizer.sources.network.assess`.
    network: dict = field(default_factory=dict)
    # Set on an option priced as if Cluster Autoscaler's drain blockers were
    # fixed: {"nodes": {node: why}, "nodesIfNot": count without the fix}.
    # Never the pick -- the fix is a change to someone's workload.
    blocked: dict = field(default_factory=dict)
    # Headroom against :data:`rightsizer.engine.realistic.HEADROOM`, from
    # :func:`rightsizer.engine.realistic.margin`. An option without it is never the
    # pick; the search offers the same change on enough nodes instead.
    margin: dict = field(default_factory=dict)
    # Measured busy load on the resulting nodes, from
    # :func:`rightsizer.engine.realistic.load_check`.
    load: dict = field(default_factory=dict)
    # "autoscaler" (CA replayed on today's placement) or "scheduler" (new nodes).
    model: str = ""
    # [{cpu, mem, pods}] per node, for the packing visualisation.
    placement: list[dict] = field(default_factory=list)
    #: The node count this option never goes below: the group's minSize, or
    #: none for an option that lowers it. Carried so a consumer repacking a
    #: different selection holds the same floor rather than re-deriving it.
    min_nodes: int = 0


# Burst headroom kept above an HPA's equilibrium replica count, and the floor
# below which a replica floor is never proposed. An HPA at equilibrium has no
# spare capacity for the next spike, and a floor of one is a single point of
# failure whatever the arithmetic says.
REPLICA_HEADROOM = 1.25
MIN_REPLICA_FLOOR = 2


#: How much of the window a proposed floor must cover on its own. The HPA can
#: always scale above it, so the floor is not a cap -- it is the level below
#: which the workload never sits. Covering the 95th percentile means the HPA
#: does the work in the busiest 5% of the window, which is its job.
FLOOR_COVERAGE = "p99"
#: Kubernetes ignores a deviation under 10% before acting.
HPA_TOLERANCE = 0.10
#: Distance the HPA must cover on its own, above which the change is worth
#: staging. Reported, never applied: a cap on the size of the cut would make the
#: recommendation a function of the current value rather than of demand -- a
#: floor of 400 would "recommend" 200, justified by nothing -- and phasing a
#: rollout is the operator's decision, not the arithmetic's.
SCALE_UP_NOTE_RATIO = 3.0


@dataclass(frozen=True)
class FloorPlan:
    """What a 30-day HPA replay says about one workload's minReplicas."""

    key: str
    hpa: str
    current_min: int
    suggested: int
    #: Where the replay puts the floor. Same as `suggested` unless the
    #: two-replica minimum raised it.
    floor_at_coverage: int
    #: True when the busiest sample wanted several times the suggested floor, so
    #: the HPA has a long way to travel and the change is worth staging.
    long_scale_up: bool
    #: Replicas the HPA would have wanted at each quantile of the window.
    desired: dict
    #: What each replica was actually working at, as a share of its request --
    #: the number the HPA steers on. Measured against ``current_min`` because
    #: these workloads are floor-bound by definition: the floor sits above what
    #: the HPA wants, so the replica count is the floor for the whole window.
    util_avg: float
    util_max: float
    #: Replicas wanted at the average, for symmetry with ``peak_desired``.
    desired_avg: int
    #: Replicas actually running, averaged over the window. Derived rather than
    #: assumed: `total / mean-across-replicas` is the count that was live, which
    #: is 92 for a workload floored at 100 for three weeks and 75 for one. The
    #: peak alone (104) reads as a spike; this shows it was the steady state.
    avg_replicas: float
    #: The target the HPA is steering toward, for comparison with the above.
    target: float
    #: Share of the window the suggested floor covers without the HPA acting.
    coverage: float
    #: Replicas at the busiest sample -- what the HPA must still be able to reach.
    peak_desired: int
    max_replicas: int
    basis: str
    #: The window does not describe one configuration. Either the request moved
    #: (utilisation is usage/request, so the percentages blend two regimes) or
    #: the workload averaged materially more replicas than its floor allows,
    #: which means the floor itself came down during the window.
    #: api-server averaged 92 against a floor of 75 -- it was 100 until
    #: 16 September.
    mixed_window: str = ""
    #: The per-replica CPU request the replay sized against: the recommended one
    #: when the option also cuts requests, otherwise today's. Pod-level (every
    #: container) when the minute replay ran, since that is what the HPA divides by.
    request: float = 0.0
    #: "replay-1m" when the floor came from the per-minute controller replay,
    #: "equilibrium" when only the 5-minute quantiles were available.
    method: str = "equilibrium"
    #: Today's pod request, for the replay's "now" side.
    request_now: float = 0.0
    #: Replay summaries (see ``hpa_replay.ReplayResult.summary``) for today's
    #: floor and request, and for the suggested ones.
    replay_now: dict | None = None
    replay_after: dict | None = None
    #: How closely the replay of today tracked the real HPA.
    validation: dict | None = None
    behavior_source: str = ""
    lag_minutes: float | None = None
    #: Replicas the HPA wants by usage alone at ``request``, minutes per count,
    #: from a replay with no floor: {replicas: minutes}. What a floor actually
    #: runs is read off this (:func:`running_replicas`), not assumed to be the
    #: floor -- 5 replicas of 100m under 1 core of load run 15, not 5.
    demand: dict | None = None


#: Share of the window's minutes the priced replica count must cover. The
#: rest are scale-ups the HPA makes and Cluster Autoscaler follows for a few
#: minutes; pricing on them counts a node for a spike, pricing on the floor
#: counts none for load that runs all month.
RUNNING_COVERAGE = 0.90


def running_replicas(demand: dict | None, floor: int, ratio: float = 1.0) -> int:
    """Replicas an HPA with this floor runs, from its demand histogram.

    Each minute the HPA runs ``max(floor, wanted)``. The count priced is the
    one that covers :data:`RUNNING_COVERAGE` of the minutes -- what the node
    group holds nearly all the time. 5 replicas of 100m under 1 core of load
    run 15, whatever the floor says. ``ratio`` is the request the demand was
    replayed at over the request in force: a smaller request makes the same
    load want proportionally more replicas.

    The same arithmetic runs in the console (``runningReplicas``), so a floor
    typed in the dialog is priced exactly as the option's own.
    """
    if not demand:
        return floor
    runs: dict[int, int] = {}
    for wanted, n in demand.items():
        w = int(wanted)
        if ratio != 1.0:
            w = math.ceil(w * ratio - 1e-9)
        r = max(floor, w)
        runs[r] = runs.get(r, 0) + n
    minutes = sum(runs.values())
    covered = 0
    for r in sorted(runs):
        covered += runs[r]
        if covered >= RUNNING_COVERAGE * minutes - 1e-9:
            return r
    return floor


def _desired_at(total: float, request: float, target: float) -> int:
    """Replicas the HPA would ask for, given a concurrent total.

    Kubernetes computes ``ceil(replicas x current/target)`` on the *average*
    utilisation across pods, which for CPU reduces to ``ceil(total / (request x
    target))`` -- the replica count cancels. The 10% tolerance is applied the
    way the controller applies it: no action inside the band.
    """
    if request <= 0 or target <= 0:
        return 0
    ratio = total / (request * target)
    if abs(ratio - 1.0) <= HPA_TOLERANCE:
        return 1
    return max(1, math.ceil(ratio))


def _mixed_window(stat, hpa) -> str:
    """Say when the window spans more than one configuration."""
    notes = []
    if getattr(stat, "request_varied", False):
        notes.append("the CPU request changed during the window, so utilisation "
                     "percentages blend two regimes")
    avg_replicas = (stat.total_avg / stat.avg) if stat.avg else 0.0
    if hpa.min_replicas and avg_replicas > hpa.min_replicas * 1.1:
        notes.append(
            f"averaged {avg_replicas:.0f} replicas against a floor of "
            f"{hpa.min_replicas}, so the floor was higher earlier in the window"
        )
    return "; ".join(notes)


#: Extra minutes above request a lower floor may add over a 30-day window --
#: about one short burst a week absorbed a little more slowly.
REPLAY_ALLOWANCE_MINUTES_30D = 30


def _replay_inputs(snapshot, hpa, stats: list, recommended: dict, recommended_mem: dict | None = None):
    """What a replay of this HPA needs, or None when it cannot be modelled.

    CPU and/or memory utilization targets (the controller takes the larger
    proposal of the two); anything else -- a custom metric -- is left alone.
    Returns (history, cpu series, common replay args, memory metric builder,
    request now, request after).
    """
    from rightsizer.engine.hpa_replay import Behavior, effective_lag, memory_metric, rle_decode

    hist = (snapshot.hpa_history or {}).get(f"{hpa.namespace}/{hpa.name}")
    targets = hpa.resource_targets
    if not hist or not stats or not targets:
        return None
    if "memory" in targets and not hist.get("mem_mib"):
        return None  # a memory HPA without its memory history cannot be replayed
    request_now = sum(s.request for s in stats)
    request_after = sum(recommended.get((s.namespace, s.workload, s.container), s.request)
                        for s in stats)
    if request_now <= 0 or request_after <= 0:
        return None
    cpu = [None if v is None else v / 1000 for v in hist["cpu_m"]]
    behavior = Behavior.from_dict(hist["behavior"])
    lag = effective_lag(hist.get("lag_minutes"))
    common = dict(target=targets.get("cpu", 0.0), max_replicas=hpa.max_replicas or 0,
                  behavior=behavior, lag_minutes=lag)

    def mem(after: bool):
        if "memory" not in targets:
            return None
        mstats = [s for s in snapshot.usage if s.resource == "memory"
                  and (s.namespace, s.workload) == (hpa.namespace, hpa.target_name)]
        req = sum(((recommended_mem or {}).get((s.namespace, s.workload, s.container), s.request)
                   if after else s.request) for s in mstats)
        if req <= 0:
            return None
        series = [None if v is None else v * 2**20 for v in hist["mem_mib"]]
        return memory_metric(series, rle_decode(hist["ready"]), req, targets["memory"])

    return hist, cpu, common, mem, request_now, request_after


def _replay_floor(snapshot, hpa, stats: list, recommended: dict, recommended_mem: dict | None = None):
    """The floor from the per-minute controller replay, or None without history.

    Cached on the snapshot per (HPA, request in force): the exporter asks for
    the same floors once per option, and a replay is not free.
    """
    from rightsizer.engine.hpa_replay import choose_floor, replay, rle_decode, validate

    if hpa.min_replicas <= MIN_REPLICA_FLOOR:
        return None  # nothing lower is ever proposed; no replay needed
    got = _replay_inputs(snapshot, hpa, stats, recommended, recommended_mem)
    if got is None:
        return None
    hist, cpu, common, mem, request_now, request_after = got
    behavior = common["behavior"]
    lag = common["lag_minutes"]
    mem_now, mem_after = mem(False), mem(True)
    cache = snapshot.__dict__.setdefault("_hpa_replay_cache", {})
    ck = (hpa.namespace, hpa.name, round(request_after, 6),
          round(mem_after.pod_request) if mem_after else None)
    if ck in cache:
        return cache[ck]

    # "Today" is today's floor *and* today's request: the exposure a change is
    # judged against is what runs now, not a hybrid.
    today = replay(cpu, pod_request=request_now, min_replicas=hpa.min_replicas, memory=mem_now, **common)
    allowance = round(REPLAY_ALLOWANCE_MINUTES_30D * snapshot.window_days / 30)
    chosen = choose_floor(
        lambda f: replay(cpu, pod_request=request_after, min_replicas=f, memory=mem_after, **common),
        hpa.min_replicas, allowance_minutes=allowance, base=today)
    result = None
    if chosen:
        floor, after, _ = chosen
        # The same controller with no floor: what the load alone asks for at
        # the new request, minute by minute.
        free = replay(cpu, pod_request=request_after, min_replicas=1, memory=mem_after, **common)
        demand: dict[int, int] = {}
        for r in free.replicas:
            if r is not None:
                demand[int(r)] = demand.get(int(r), 0) + 1
        # Validated against the floor that was in force each minute, when the
        # history has it: api-server's was 100 for three weeks and 75
        # after, and a replay at 75 throughout could not track it.
        as_was = (replay(cpu, pod_request=request_now, min_replicas=rle_decode(hist["min"]),
                         memory=mem_now, **common) if hist.get("min") else today)
        result = {
            "floor": floor, "after": after.summary(), "now": today.summary(),
            "validation": validate(as_was.replicas, rle_decode(hist["desired"])),
            "behavior_source": behavior.source, "lag": lag,
            "request_now": request_now, "request_after": request_after,
            "demand": dict(sorted(demand.items())),
        }
    cache[ck] = result
    return result


#: Minutes held at the ceiling, over a 30-day window, before raising it is
#: suggested: a few minutes is a blip the stabilization window absorbs; more is
#: load the workload was denied.
CEILING_MINUTES_30D = 10
#: Headroom over the most the load asked for, so the next busier day fits too.
CEILING_HEADROOM = 1.25
#: The peak a ceiling is sized on must have lasted this long: one noisy
#: 1-minute sample is not load the HPA needed to follow.
SUSTAINED_MINUTES = 5


@dataclass
class CeilingPlan:
    """An HPA whose maxReplicas, not its floor, is the risk.

    Pinned at the ceiling, an HPA stops adding pods while load keeps rising:
    the ready pods run hotter, CPU queues and memory climbs toward eviction --
    nothing pages until latency or an OOM does. Measured two ways: minutes the
    real HPA sat at maxReplicas, and minutes the replay wanted more than it and
    was held -- today, and at the recommended requests, where a cut makes the
    same load ask for more replicas.
    """
    key: str
    hpa: str
    current_max: int
    suggested_max: int
    #: Minutes the real HPA's desired count was at maxReplicas.
    pinned_minutes: int
    #: Replay: minutes the controller wanted more than maxReplicas, and the most.
    capped_now: int
    peak_wanted_now: int
    #: The same at the recommended requests (0 when they change nothing).
    capped_after: int = 0
    peak_wanted_after: int = 0
    #: "now" -- the ceiling binds today; "after" -- only once requests are cut.
    when: str = "now"
    basis: str = ""
    #: Minutes the replay is still held at the suggested ceiling: the spikes
    #: shorter than a few minutes it deliberately does not chase.
    capped_at_suggested: int = 0


def replica_ceilings(snapshot, recommendations: list | None = None) -> list[CeilingPlan]:
    """HPAs whose ceiling the load reached or would reach, with a higher one.

    Cached on the snapshot per recommendation set: every option of every
    group asks, and each answer is three month-long replays per HPA.
    """
    cache = snapshot.__dict__.setdefault("_ceiling_cache", {})
    ck = id(recommendations) if recommendations else None
    hit = cache.get(ck)
    if hit is None or hit[0] is not recommendations:
        hit = cache[ck] = (recommendations, _replica_ceilings(snapshot, recommendations))
    return hit[1]


def _replica_ceilings(snapshot, recommendations: list | None = None) -> list[CeilingPlan]:
    from rightsizer.engine.hpa_replay import replay, rle_decode

    stats_by: dict[tuple[str, str], list] = {}
    for s in snapshot.usage:
        if s.resource == "cpu":
            stats_by.setdefault((s.namespace, s.workload), []).append(s)
    recommended = {(r.namespace, r.workload, r.container): r.cpu_recommended
                   for r in (recommendations or []) if r.actionable and r.cpu_recommended is not None}
    recommended_mem = {(r.namespace, r.workload, r.container): r.mem_recommended
                       for r in (recommendations or []) if r.actionable and r.mem_recommended is not None}
    threshold = max(1, round(CEILING_MINUTES_30D * snapshot.window_days / 30))
    out: list[CeilingPlan] = []
    for hpa in snapshot.hpas:
        if not hpa.target_name or not hpa.max_replicas:
            continue
        stats = stats_by.get((hpa.namespace, hpa.target_name), [])
        got = _replay_inputs(snapshot, hpa, stats, recommended, recommended_mem)
        if got is None:
            continue
        hist, cpu, common, mem, request_now, request_after = got
        floors = rle_decode(hist["min"]) if hist.get("min") else hpa.min_replicas
        now = replay(cpu, pod_request=request_now, min_replicas=floors, memory=mem(False), **common)
        desired = [d for d in rle_decode(hist["desired"]) if d is not None]
        pinned = sum(1 for d in desired if d >= hpa.max_replicas)
        after = None
        if recommendations and (request_after != request_now or mem(True) is not None):
            after = replay(cpu, pod_request=request_after, min_replicas=floors, memory=mem(True), **common)
        binds_now = max(now.capped_minutes, pinned) >= threshold
        binds_after = after is not None and after.capped_minutes >= threshold
        if not (binds_now or binds_after):
            continue
        # Sized on the case that binds: today's load, or -- only when today's
        # ceiling holds -- the load at the recommended requests.
        basis_run = now if binds_now else after
        peak = max(basis_run.sustained_wanted(SUSTAINED_MINUTES), hpa.max_replicas + 1)
        suggested = max(hpa.max_replicas + 1, math.ceil(peak * CEILING_HEADROOM - 1e-9))
        why = []
        if hpa.max_replicas <= hpa.min_replicas:
            why.append(f"maxReplicas equals minReplicas ({hpa.max_replicas}): this HPA cannot scale at all")
        if pinned >= threshold:
            why.append(f"the HPA sat at maxReplicas {hpa.max_replicas} for {pinned} minutes")
        if now.capped_minutes >= threshold:
            why.append(f"the replay wanted up to {now.peak_wanted} for {now.capped_minutes} minutes")
        if binds_after and not binds_now:
            why.append(f"at the recommended requests the same load wants up to {after.peak_wanted} "
                       f"for {after.capped_minutes} minutes")
        at_new = replay(cpu, pod_request=request_now if binds_now else request_after,
                        min_replicas=floors, memory=mem(not binds_now),
                        **{**common, "max_replicas": suggested})
        out.append(CeilingPlan(
            capped_at_suggested=at_new.capped_minutes,
            key=f"{hpa.namespace}/{hpa.target_name}/{stats[0].kind if stats else hpa.target_kind}",
            hpa=hpa.name, current_max=hpa.max_replicas, suggested_max=suggested,
            pinned_minutes=pinned, capped_now=now.capped_minutes, peak_wanted_now=now.peak_wanted,
            capped_after=after.capped_minutes if after else 0,
            peak_wanted_after=after.peak_wanted if after else 0,
            when="now" if binds_now else "after",
            basis="; ".join(why) + (f" -- sized at {CEILING_HEADROOM:.0%} of the {peak} replicas "
                                    f"it wanted for {SUSTAINED_MINUTES}+ minutes in a row"),
        ))
    return out


def _with_history(snapshot) -> set[tuple[str, str]]:
    """Workloads whose HPA has a minute history: judged by the replay alone."""
    names = set((snapshot.hpa_history or {}))
    return {(h.namespace, h.target_name) for h in snapshot.hpas
            if f"{h.namespace}/{h.name}" in names and h.target_name}


def replica_floors(snapshot, recommendations: list | None = None) -> list[FloorPlan]:
    """:func:`_replica_floors`, cached on the snapshot per recommendation set.

    Every option of every group asks for the same floors; each answer is a
    handful of month-long replays per HPA. The set is kept beside its answer,
    so its id cannot be reused while the entry lives.
    """
    cache = snapshot.__dict__.setdefault("_floor_cache", {})
    ck = id(recommendations) if recommendations else None
    hit = cache.get(ck)
    if hit is None or hit[0] is not recommendations:
        hit = cache[ck] = (recommendations, _replica_floors(snapshot, recommendations))
    return hit[1]


def _replica_floors(snapshot, recommendations: list | None = None) -> list[FloorPlan]:
    """Replay each HPA against the 30-day window and size its floor from that.

    The previous model asked one question -- what would the HPA settle at, given
    p95 usage -- and answered it with a single number. That is an equilibrium,
    not a behaviour: it says nothing about how often the workload was busier
    than that, and a floor set from it is either wasteful or too tight with no
    way to tell which.

    This replays instead. ``desired(t) = ceil(total(t) / (request x target))``
    is evaluated across the stored quantiles of the concurrent-total series, so
    the result is a distribution: what the HPA wanted at the median, at p95, at
    the busiest sample. The floor is set at the quantile named by
    :data:`FLOOR_COVERAGE` -- **soft**, because a floor is not a ceiling. Below
    it the workload never sits; above it the HPA scales, which is what it is for.

    A workload whose peak demand exceeds its configured ``maxReplicas`` is left
    alone: its floor is not the thing to change.
    """
    hpas = {(h.namespace, h.target_name): h for h in snapshot.hpas if h.target_name}
    usage = {
        (s.namespace, s.workload): s for s in snapshot.usage if s.resource == "cpu"
    }
    # Every container of a pod: the HPA divides pod usage by pod request.
    containers: dict[tuple[str, str], list] = {}
    for s in snapshot.usage:
        if s.resource == "cpu":
            containers.setdefault((s.namespace, s.workload), []).append(s)
    recommended = {
        (r.namespace, r.workload, r.container): r.cpu_recommended
        for r in (recommendations or [])
        if r.actionable and r.cpu_recommended is not None
    }
    recommended_mem = {
        (r.namespace, r.workload, r.container): r.mem_recommended
        for r in (recommendations or [])
        if r.actionable and r.mem_recommended is not None
    }

    out: list[FloorPlan] = []
    for (namespace, workload), hpa in hpas.items():
        stat = usage.get((namespace, workload))
        targets = hpa.resource_targets
        if not stat or stat.request <= 0 or not targets or hpa.min_replicas <= 0:
            continue
        # A memory target is modelled only by the minute replay, from the
        # memory history; the quantile fallback below is CPU arithmetic.
        memory_hpa = "memory" in targets
        # The request that will be in force. Utilisation is usage/request, so an
        # option that cuts the request raises what the HPA wants.
        request = recommended.get((namespace, workload, stat.container), stat.request)
        if request <= 0:
            continue

        ladder = dict(stat.total_ladder or {})
        replayed = _replay_floor(snapshot, hpa, containers.get((namespace, workload), []),
                                 recommended, recommended_mem)
        if memory_hpa and replayed is None and (namespace, workload) not in _with_history(snapshot):
            continue
        if replayed is not None or (namespace, workload) in _with_history(snapshot):
            if replayed is None:
                continue  # replayed, and no lower floor holds burst exposure
            floor = replayed["floor"]
            util_avg = (stat.avg / request) if request else 0.0
            peak_avg = (stat.avg_ladder or {}).get("max")
            util_max = ((peak_avg / request) if peak_avg is not None
                        else (stat.p95 / request)) if request else 0.0
            desired = {q: _desired_at(v, request, hpa.cpu_target or 0.0)
                       for q, v in ladder.items() if q != "n"}
            peak = replayed["after"]["peak_replicas"]
            v = replayed["validation"] or {}
            out.append(FloorPlan(
                key=f"{namespace}/{workload}/{stat.kind}", hpa=hpa.name,
                current_min=hpa.min_replicas, suggested=floor, floor_at_coverage=floor,
                long_scale_up=peak > floor * SCALE_UP_NOTE_RATIO,
                desired=desired, coverage=0.0, peak_desired=peak,
                util_avg=round(util_avg, 4), util_max=round(util_max, 4),
                avg_replicas=round(stat.total_avg / stat.avg, 1) if stat.avg else 0.0,
                mixed_window=_mixed_window(stat, hpa),
                desired_avg=round(replayed["after"]["avg_replicas"]),
                target=hpa.cpu_target, max_replicas=hpa.max_replicas or 0,
                basis=(f"per-minute HPA replay ({replayed['behavior_source']} behavior, "
                       f"{replayed['lag']:.0f}-min readiness): lowest floor adding at most "
                       f"{round(REPLAY_ALLOWANCE_MINUTES_30D * snapshot.window_days / 30)} minutes "
                       f"above request"
                       + (f"; replay of today within ±1 replica {v['within_one']:.0%} of minutes"
                          if v else "")),
                request=replayed["request_after"], request_now=replayed["request_now"],
                method="replay-1m", replay_now=replayed["now"], replay_after=replayed["after"],
                validation=replayed["validation"], behavior_source=replayed["behavior_source"],
                lag_minutes=replayed["lag"], demand=replayed.get("demand"),
            ))
            continue

        if not ladder:
            # No series kept: fall back to the single-point equilibrium, and say
            # so, rather than silently reporting a replay that did not happen.
            equilibrium = (stat.p95 * max(stat.replicas, 1)) / (request * hpa.cpu_target)
            suggested = max(MIN_REPLICA_FLOOR, math.ceil(equilibrium * REPLICA_HEADROOM))
            if suggested >= hpa.min_replicas:
                continue
            out.append(FloorPlan(
                key=f"{namespace}/{workload}/{stat.kind}", hpa=hpa.name,
                current_min=hpa.min_replicas, suggested=suggested,
                floor_at_coverage=suggested, long_scale_up=False,
                desired={}, coverage=0.0, peak_desired=0,
                util_avg=0.0, util_max=0.0, desired_avg=0, avg_replicas=0.0,
                target=hpa.cpu_target or 0.0,
                max_replicas=hpa.max_replicas or 0,
                basis="p95 equilibrium (no series in this snapshot)",
                request=request,
            ))
            continue

        desired = {
            q: _desired_at(v, request, hpa.cpu_target or 0.0)
            for q, v in ladder.items() if q not in ("n",)
        }
        peak = desired.get("max", 0)
        if hpa.max_replicas and peak > hpa.max_replicas:
            # The HPA cannot reach what the window demanded; the floor is not
            # the problem here and lowering it would only deepen the shortfall.
            continue

        # The formula's own answer, floored at two replicas for availability.
        at_coverage = max(MIN_REPLICA_FLOOR, desired.get(FLOOR_COVERAGE, 0))
        suggested = at_coverage
        if suggested >= hpa.min_replicas:
            continue

        # Utilisation as the HPA sees it: the mean across replicas, over what
        # each one reserved.
        #
        # `stat.avg` is `avg by (...)` across pods at each instant, so it is
        # already divided by the replica count that was live then. Dividing the
        # concurrent total by today's `minReplicas` instead -- which this did --
        # is wrong whenever that floor moved during the window:
        # api-server ran at 100 replicas for three of four weeks and 75
        # for the last, and the stale constant reported 12.3% against a true
        # 10.0%.
        util_avg = (stat.avg / request) if request else 0.0
        peak_avg = (stat.avg_ladder or {}).get("max")
        util_max = ((peak_avg / request) if peak_avg is not None
                    else (stat.p95 / request)) if request else 0.0
        covered = sum(1 for q, d in desired.items()
                      if q != "max" and d <= suggested) / max(len(desired) - 1, 1)
        out.append(FloorPlan(
            key=f"{namespace}/{workload}/{stat.kind}", hpa=hpa.name,
            current_min=hpa.min_replicas, suggested=suggested,
            floor_at_coverage=at_coverage,
            long_scale_up=peak > suggested * SCALE_UP_NOTE_RATIO,
            desired=desired, coverage=round(covered, 3), peak_desired=peak,
            util_avg=round(util_avg, 4), util_max=round(util_max, 4),
            avg_replicas=round(stat.total_avg / stat.avg, 1) if stat.avg else 0.0,
            mixed_window=_mixed_window(stat, hpa),
            desired_avg=_desired_at(stat.total_avg, request, hpa.cpu_target),
            target=hpa.cpu_target,
            max_replicas=hpa.max_replicas or 0,
            basis=f"HPA replayed over {ladder.get('n', 0)} samples; floor at "
                  f"{FLOOR_COVERAGE} of demand, HPA scales above it",
            request=request,
        ))
    return out


def replica_plan(snapshot, recommendations: list | None = None) -> dict[str, int]:
    """{workload key: suggested minReplicas}, from :func:`replica_floors`."""
    return {f.key: f.suggested for f in replica_floors(snapshot, recommendations)}


def replica_running(snapshot, recommendations: list | None = None) -> dict[str, int]:
    """{workload key: replicas the HPA runs at its suggested floor}.

    What the node count is priced on. The floor is a minimum, not a count: at
    a request cut this far the load alone can hold the HPA above it, and
    pricing the option at the floor would claim nodes the HPA refills.
    """
    return {f.key: running_replicas(f.demand, f.suggested)
            for f in replica_floors(snapshot, recommendations)}


def _request_sets(recommendations: list | None) -> list[tuple[str, str, str]]:
    """Which request changes are worth simulating for this group.

    Derived from what the recommendations actually contain rather than a fixed
    list. A group with no memory increases never gets a memory scenario, and a
    group with nothing actionable gets none at all -- an empty option is noise,
    not caution.

    Returns (apply-mode, key, label).
    """
    recs = [r for r in (recommendations or []) if r.actionable]
    cuts = any(r.cpu_recommended is not None and r.cpu_recommended < r.cpu_request for r in recs)
    raises = any(r.mem_recommended is not None and r.mem_recommended > r.mem_request for r in recs)
    mem_cuts = any(r.mem_recommended is not None and r.mem_recommended < r.mem_request for r in recs)

    out: list[tuple[str, str, str]] = []
    if cuts or mem_cuts or raises:
        out.append(("all", "rightsize", "Right-size requests"))
    # Memory-only is worth its own row when memory moves on its own: it is a
    # stability change whose cost should be visible separately from any saving.
    if raises and not (cuts or mem_cuts):
        out.append(("memory", "memory", "Raise memory requests"))
    return out


def scenarios(
    snapshot,
    group_state,
    ladder,
    recommendations,
    *,
    current_type: str,
    price_of,
    max_pods: int,
    headroom: float = 1.0,
    min_size: int | None = None,
    ca=None,
    rules=None,
) -> list[Scenario]:
    """Search the shapes this node group could take, and keep the ones that pay.

    The dimensions are the request set and the instance type; every feasible
    combination is packed and priced, then reduced to the distinct outcomes worth
    a decision. Nothing is hard-coded to a fixed set of cases -- a group with no
    actionable requests simply has fewer rows, and a group where nothing helps
    has none beyond its baseline.

    "As-is" is always returned, marked as the baseline rather than a proposal,
    because a reader needs to know what is running before judging what to change.

    Every option's node count is what production would reach, not an ideal
    repack (:mod:`rightsizer.engine.realistic`): Cluster Autoscaler replayed on today's
    placement for the same node type, by-request placement at 100% for a new
    one, and a measured-load check on the result. ``ca`` is the Cluster
    Autoscaler config (the snapshot's, else upstream defaults); ``rules`` its
    eviction rules (from the snapshot when not given). ``headroom`` is kept for
    callers and reported, and is 1.0: neither controller keeps any.
    """
    from rightsizer.adapters.autoscalers import scale_down_settings
    from rightsizer.engine import realistic as R
    from rightsizer.engine.eviction import rules_from_snapshot
    from rightsizer.sources.network import assess as network_assess
    from rightsizer.sources.storage import assess as storage_assess

    if ca is None:
        ca = scale_down_settings(snapshot, group_state.name)
    if rules is None:
        rules = rules_from_snapshot(snapshot)

    current = next((i for i in ladder if i.name == current_type), None)
    if not current or group_state.nodes_current == 0:
        return []
    measured_disk = (getattr(snapshot, "storage", None) or {}).get(group_state.name)
    measured_net = (getattr(snapshot, "network", None) or {}).get(group_state.name)

    overhead_cpu = max(0.0, current.vcpu - group_state.alloc_cpu_per_node)
    overhead_mem = max(0.0, current.memory_gib * GIB - group_state.alloc_mem_per_node)
    mp = max_pods or 110

    def capacity(inst) -> tuple[float, float]:
        # The running type as measured; a candidate by its cloud's own
        # allocatable formula when it has one (OCI: Karpenter's), since the
        # kubelet's reservation grows with the node and the running node's
        # overhead would overstate every larger candidate.
        if inst.name != current.name and getattr(inst, "alloc_vcpu", 0) and getattr(inst, "alloc_mem_gib", 0):
            return (
                inst.alloc_vcpu - group_state.ds_cpu_per_node,
                inst.alloc_mem_gib * GIB - group_state.ds_mem_per_node,
            )
        return (
            inst.vcpu - overhead_cpu - group_state.ds_cpu_per_node,
            inst.memory_gib * GIB - overhead_mem - group_state.ds_mem_per_node,
        )

    pod_sets: dict[str, list[Pod]] = {
        "current": pods_for_nodegroup(snapshot, group_state.name, apply="current",
                                      recommendations=recommendations, group_state=group_state)
    }
    if not pod_sets["current"]:
        return []
    modes = list(_request_sets(recommendations))
    # Replica floors are their own lever, and often the cheaper one -- but they
    # are not an *alternative* to fixing requests. A team cutting an over-set
    # HPA floor fixes the requests in the same PR, and the two interact: fewer
    # pods each holding a larger memory reservation is a different shape from
    # either change alone. Searching them only separately made the combination
    # invisible, and on services-1 that hid the fact that the cheapest row
    # (replicas alone, 16 nodes) still carries the OOM risk the memory increases
    # exist to remove -- which the combination fixes for two more nodes.
    if replica_plan(snapshot):
        modes.append(("replicas", "replicas", "Reduce HPA minReplicas"))
        for mode, key, label in list(_request_sets(recommendations)):
            modes.append((
                f"{mode}+replicas",
                f"{key}-replicas",
                f"{label} and reduce HPA minReplicas",
            ))
    for mode, _key, _label in modes:
        pod_sets[mode] = pods_for_nodegroup(
            snapshot, group_state.name, apply=mode,
            recommendations=recommendations, group_state=group_state,
        )
    # `_request_sets` sees the whole cluster's recommendations, so a group none
    # of whose pods has a request change still got a "Right-size requests" row.
    # Its pods are the baseline's, so its saving was pure repacking -- on
    # prod-eu1's batch-aux-1, 18 -> 5 nodes with nothing to right-size --
    # and the option listed no containers. Keep a request lever only where it
    # changes a pod on this group.
    def without_requests(mode: str) -> str:
        return "replicas" if "replicas" in mode.split("+") else "current"

    modes = [
        m for m in modes
        if not (set(m[0].split("+")) & {"all", "memory"})
        or pod_sets[m[0]] != pod_sets.get(without_requests(m[0]))
    ]

    rows = (snapshot.placements or {}).get(group_state.name) or []
    if not rows:
        # A snapshot collected before placement was recorded: stand in for it
        # with the attributed pods dealt round-robin over today's nodes, largest
        # first, so the same controllers can be replayed. Approximate by nature.
        from dataclasses import replace as _replace
        n = max(group_state.nodes_current, 1)
        rows = [
            {"namespace": p.workload.split("/", 1)[0], "pod": p.name, "node": f"node-{i % n}",
             "cpu": p.cpu, "mem": p.memory, "owner_kind": "ReplicaSet", "workload_key": p.workload}
            for i, p in enumerate(sorted(pod_sets["current"], key=lambda p: p.sort_key))
        ]
        snapshot = _replace(snapshot, placements={**(snapshot.placements or {}),
                                                  group_state.name: rows})
    loads = R.busy_loads(snapshot)
    replicas_now: dict[str, int] = {}
    for r in rows:
        if r.get("owner_kind") != "DaemonSet":
            k = r.get("workload_key", "")
            replicas_now[k] = replicas_now.get(k, 0) + 1
    antiaffinity = set(rules.antiaffinity)

    def full_alloc(inst) -> tuple[float, float]:
        return inst.vcpu - overhead_cpu, inst.memory_gib * GIB - overhead_mem

    # Today's placement exactly as it runs: the baseline for the load check.
    today_out = R.Outcome(
        nodes=group_state.nodes_current, mode="measured",
        placement={},
    )
    ds_nodes: dict[str, list[float]] = {}
    for r in rows:
        node = r.get("node", "")
        if not node:
            continue
        today_out.placement.setdefault(node, []).append((
            r.get("workload_key", ""), float(r.get("cpu", 0.0)), float(r.get("mem", 0.0)),
            r.get("owner_kind") == "DaemonSet"))
        if r.get("owner_kind") == "DaemonSet":
            load = loads.get(r.get("workload_key", ""))
            acc = ds_nodes.setdefault(node, [0.0, 0.0])
            acc[0] += load.cpu_busy if load else float(r.get("cpu", 0.0))
            acc[1] += load.mem_busy if load else float(r.get("mem", 0.0))
    ds_busy = (
        (sum(v[0] for v in ds_nodes.values()) / len(ds_nodes),
         sum(v[1] for v in ds_nodes.values()) / len(ds_nodes)) if ds_nodes else (0.0, 0.0)
    )
    today_load = R.load_check(today_out, alloc_cpu=group_state.alloc_cpu_per_node,
                              alloc_mem=group_state.alloc_mem_per_node, loads=loads,
                              replicas_now=replicas_now, replicas_kept={}, ds_busy=ds_busy)

    trend_today = R.trend_check(
        (getattr(snapshot, "nodegroup_trends", None) or {}).get(group_state.name),
        nodes_now=group_state.nodes_current, nodes_after=group_state.nodes_current,
        min_nodes=group_state.nodes_current, cpu_scale=1.0, mem_scale=1.0)
    today_marks = {**today_load, **{k: v for k, v in trend_today.items() if k.startswith("peak")}}

    realized: dict[tuple, R.Outcome | None] = {}

    def realize(mode: str, inst, min_nodes: int, lift: bool | frozenset = False) -> R.Outcome | None:
        key = (mode, inst.name, min_nodes, lift)
        if key in realized:
            return realized[key]
        deltas, keep = R.lever_changes(snapshot, mode, recommendations)
        # The same edits per container, for the exact replay on live pods
        # (used when the snapshot carries them -- `rightsizer kube`).
        requests = R.lever_requests(snapshot, mode, recommendations)
        if inst.name == current_type:
            out = R.same_type(snapshot, group_state, deltas=deltas, keep=keep,
                              min_nodes=min_nodes, ca=ca, rules=rules, lift=lift,
                              requests=requests)
        else:
            cpu_a, mem_a = capacity(inst)
            live = bool((snapshot.facts.kube or {}).get("pods"))
            out = None if cpu_a <= 0 or mem_a <= 0 else R.new_type(
                snapshot, group_state, deltas=deltas, keep=keep, cpu_per_node=cpu_a,
                # max-pods counts DaemonSet pods, which every new node runs too;
                # the live replay places those DaemonSet pods itself.
                mem_per_node=mem_a, max_pods=mp if live else max(1, mp - ds_pods),
                min_nodes=min_nodes, antiaffinity=antiaffinity,
                zone_bound=rules.zone_bound, requests=requests, full_alloc=full_alloc(inst),
                inst=inst, current=current)
        if out is not None:
            fa = full_alloc(inst)
            kept = {w: min(n, replicas_now.get(w, n)) for w, n in keep.items()}
            pods_load = R.load_check(out, alloc_cpu=fa[0], alloc_mem=fa[1], loads=loads,
                                     replicas_now=replicas_now, replicas_kept=kept,
                                     ds_busy=ds_busy, today=today_load)
            month = R.trend_check(
                (getattr(snapshot, "nodegroup_trends", None) or {}).get(group_state.name),
                nodes_now=group_state.nodes_current, nodes_after=out.nodes,
                min_nodes=min_nodes, cpu_scale=inst.vcpu / current.vcpu,
                mem_scale=inst.memory_gib / current.memory_gib)
            out.load = R.combine(pods_load, month)
        realized[key] = out
        return out

    base_cost = price_of(current_type) * group_state.nodes_current
    ds_pods = _daemonset_pods_per_node(group_state)
    constraints_known = bool(snapshot.facts.workload_constraints)
    implausible = max(4 * group_state.nodes_current, group_state.nodes_current + 20)
    floor = int(min_size or 0)

    def utilisation(inst, out: R.Outcome) -> tuple[float, float, str]:
        cpu_a, mem_a = capacity(inst)
        n = max(out.nodes, 1)
        cpu = sum(c for pods in out.placement.values() for _w, c, _m, ds in pods if not ds)
        mem = sum(m for pods in out.placement.values() for _w, _c, m, ds in pods if not ds)
        count = sum(1 for pods in out.placement.values() for p in pods if not p[3])
        cu, mu, pu = cpu / (cpu_a * n), mem / (mem_a * n), count / (mp * n)
        return cu, mu, max((cu, "cpu"), (mu, "memory"), (pu, "pod density"), key=lambda t: t[0])[1]

    def summarise(out: R.Outcome) -> list[dict]:
        return [
            {
                "cpu": round(sum(c for _w, c, _m, ds in pods if not ds), 3),
                "mem": round(sum(m for _w, _c, m, ds in pods if not ds) / GIB, 2),
                "pods": sum(1 for p in pods if not p[3]),
                "dsCpu": round(group_state.ds_cpu_per_node, 3),
                "dsMem": round(group_state.ds_mem_per_node / GIB, 2),
                "dsPods": ds_pods,
            }
            for _node, pods in sorted(out.placement.items())[:80]
        ]

    def build(key, label, inst, pods, out: R.Outcome, requires, effective_floor=None,
              cost: float | None = None) -> Scenario | None:
        if out is None or out.unplaceable or out.nodes <= 0:
            return None
        if out.nodes > implausible:
            return None
        nodes = out.nodes
        cost = price_of(inst.name) * nodes if cost is None else cost
        cpu_a, mem_a = capacity(inst)
        note = ""
        if out.mode == "autoscaler" and out.vetoed:
            note = (f"Cluster Autoscaler stops at {nodes}: {len(out.vetoed)} node(s) hold pods "
                    "it will not evict")
        elif out.added:
            note = f"+{out.added} node(s) added for pods the larger requests push out"
        unverified = not constraints_known and nodes < group_state.nodes_current
        if unverified:
            # Scheduling constraints were unavailable, so the packing could not
            # see a workload pinned one replica per node. Say so as a step, not
            # only as a badge.
            requires = requires + [
                "verify pod anti-affinity and topology spread against the Kubernetes API"
            ]
        disk = storage_assess(measured_disk, current, inst,
                              nodes_now=group_state.nodes_current, nodes_after=nodes)
        if disk["verdict"] == "unsafe":
            requires = requires + [f"local storage: {n}" for n in disk["notes"]]
        net = network_assess(measured_net, current, inst,
                             nodes_now=group_state.nodes_current, nodes_after=nodes)
        if net["verdict"] == "unsafe":
            requires = requires + [f"network: {n}" for n in net["notes"]]
        if out.load.get("verdict") == "unsafe":
            requires = requires + [f"node load: {n}" for n in out.load.get("notes", [])]
        driven = sum(1 for p in pods if p.usage_driven)
        if driven and nodes < group_state.nodes_current:
            # Placement is by request, as the scheduler and Cluster Autoscaler
            # place; the load check reads what these pods really use.
            requires = requires + [
                f"give requests to the {driven} pod(s) here that use more than they request"
            ]
        room = R.margin(out.load or {}, today_marks, storage=disk, network=net)
        cu, mu, binding = utilisation(inst, out)
        return Scenario(
            key=key, label=label, instance_type=inst.name, nodes=nodes,
            monthly_usd=cost, delta_usd=cost - base_cost,
            cpu_utilization=cu, mem_utilization=mu,
            binding=binding, feasible=True, note=note, requires=requires,
            retype=inst.name != current.name,
            retype_steps=[r for r in requires if r in type_steps],
            unverified=unverified,
            usage_driven_pods=driven,
            node_cpu=round(cpu_a, 3), node_mem_gib=round(mem_a / GIB, 2),
            node_max_pods=mp, headroom=1.0,
            vcpu=inst.vcpu, mem_gib=inst.memory_gib, hourly_usd=inst.on_demand_hourly,
            placement=summarise(out),
            min_nodes=floor if effective_floor is None else effective_floor,
            storage=disk, network=net, load=out.load, model=out.mode, margin=room,
        )

    def floor_options() -> list[tuple[int, str]]:
        """Node-count floors worth simulating.

        The configured minSize is a constraint the tool can propose changing, not
        a law. A group held at its floor has no cheaper shape to offer while the
        floor is treated as fixed -- which is exactly the case where the useful
        answer is "lower the floor".
        """
        if floor <= 1:
            return [(floor, "")]
        return [(floor, ""), (0, "lower minSize")]

    total_cpu = sum(float(r.get("cpu", 0.0)) for r in rows if r.get("owner_kind") != "DaemonSet")
    total_mem = sum(float(r.get("mem", 0.0)) for r in rows if r.get("owner_kind") != "DaemonSet")
    base_cu = total_cpu / (capacity(current)[0] * max(group_state.nodes_current, 1))
    base_mu = total_mem / (capacity(current)[1] * max(group_state.nodes_current, 1))
    baseline = Scenario(
        key="asis", label="As-is", instance_type=current_type,
        nodes=group_state.nodes_current, monthly_usd=base_cost, delta_usd=0.0,
        cpu_utilization=base_cu, mem_utilization=base_mu,
        binding="cpu" if base_cu >= base_mu else "memory", feasible=True, requires=[],
        node_cpu=round(capacity(current)[0], 3),
        node_mem_gib=round(capacity(current)[1] / GIB, 2),
        node_max_pods=mp, headroom=1.0, vcpu=current.vcpu,
        mem_gib=current.memory_gib, hourly_usd=current.on_demand_hourly,
        placement=(group_state.occupancy[:80] or summarise(today_out)),
        measured=bool(group_state.occupancy),
        min_nodes=floor,
        storage=storage_assess(measured_disk, current, current,
                               nodes_now=group_state.nodes_current,
                               nodes_after=group_state.nodes_current),
        network=network_assess(measured_net, current, current,
                               nodes_now=group_state.nodes_current,
                               nodes_after=group_state.nodes_current),
        load=today_load, model="measured",
    )

    # Zones holding this group's EBS volumes: a replacement group needs a node
    # in each, or those pods stay Pending.
    group_zones = sorted({
        z for r in rows
        if (z := rules.zone_bound.get(f"{r.get('namespace', '')}/{r.get('pod', '')}"))
    })

    # HPAs in this group that the recommended request cut pushes past their
    # ceiling: an option applying that cut has to raise them too.
    def _home(key: str) -> str:
        shares = (snapshot.workload_nodegroups or {}).get(key) or {}
        return max(shares, key=shares.get) if shares else ""
    raise_after = [c for c in (replica_ceilings(snapshot, recommendations) if recommendations else [])
                   if c.when == "after" and _home(c.key) == group_state.name]

    def requires_for(mode, same_type, inst, floor_note, out) -> list[str]:
        requires: list[str] = []
        levers = set(mode.split("+"))
        if levers & {"all", "memory"}:
            requires.append("apply the container request changes")
            for c in raise_after:
                requires.append(
                    f"raise maxReplicas on {c.key.rsplit('/', 1)[0]} from {c.current_max} to "
                    f"{c.suggested_max} -- at the new requests the same load wants up to "
                    f"{c.peak_wanted_after} replicas")
        if "replicas" in levers:
            requires.append("lower minReplicas on the affected HPAs")
        if not same_type:
            # In the words of whoever owns the group's shape: a managed pool is
            # replaced or updated, a Karpenter pool gains a size or has it already.
            from rightsizer.adapters import retype_steps

            steps = retype_steps(snapshot, snapshot.facts.config_for(group_state.name), inst.name)
            if len(group_zones) > 1:
                steps.append(
                    f"span the new group across {', '.join(group_zones)} with a node in each -- "
                    "pods on zonal volumes can only restart in their volume's zone")
            type_steps.update(steps)
            requires.extend(steps)
        if floor_note:
            requires.append(f"lower the node group minSize from {floor} to {max(out.nodes, 1)}")
        return requires

    # The steps that came from a type change, so a reader (and the console's
    # re-pricing) can tell them from the request and floor steps.
    type_steps: set[str] = set()

    def name_for(label, same_type, inst, floor_note, out) -> str:
        floor_label = f"lower minSize to {max(out.nodes, 1)}" if floor_note else None
        parts = [p for p in (label, None if same_type else f"move to {inst.name}",
                             floor_label) if p]
        return ", ".join(parts).capitalize() if parts else f"Move to {inst.name}"

    def key_for(key, same_type, inst, floor_note) -> str:
        return "-".join(p for p in (key, "" if same_type else inst.name,
                                    "floor" if floor_note else "") if p)

    def fewest_fixes(mode, inst, min_nodes: int, held: R.Outcome, full: R.Outcome
                     ) -> tuple[R.Outcome, dict[str, list], bool, list[str]]:
        """The fewest blockers to fix for the node count every fix would reach.

        Fixing everything CA refuses is rarely needed: CA only has to empty
        the nodes it removes, and the ones that stay can keep whatever pins
        them. On prod-us2 db-2 the three nodes each hold a postgres StatefulSet;
        going to one node means fixing the two nodes that go, and the third --
        with the argocd controllers and the kube-system pods beside its
        database -- needs nothing. So fix one node's blockers at a time, the
        one that frees the most for the least, until the count is reached.
        Returns the outcome with only those fixed (CA still honours the rest),
        {node: [(subject, why)]} for the nodes that must be unblocked, and
        whether nothing short of fixing every blocker reaches the count.
        """
        lift: set[str] = set()
        cur = held
        for _ in range(len(held.blockers) + 1):
            if cur.nodes <= full.nodes:
                break
            best = None
            for node, reasons in sorted(cur.blockers.items()):
                new = {subj for subj, _ in reasons} - lift
                if not new:
                    continue
                o = realize(mode, inst, min_nodes, lift=frozenset(lift | new))
                if o is None or o.unplaceable:
                    continue
                # Fewer nodes, then fewer fixes, then fewer StatefulSets --
                # a database is the last thing to make evictable.
                score = (o.nodes, len(new), sum(s.endswith("/StatefulSet") for s in new))
                if best is None or score < best[0]:
                    best = (score, new, o)
            if best is None or best[2].nodes >= cur.nodes:
                break
            lift |= best[1]
            cur = best[2]
        # One node at a time can stall where two must go together -- moving
        # pods can itself create a blocker (two coredns replicas landing on
        # one node breach its PDB). Then fix everything on the nodes the
        # unrestricted replay removes, and whatever that uncovers, until the
        # count is reached; the published fix list is always one that does.
        seen: dict[str, dict[str, str]] = {n: dict(rs) for n, rs in held.blockers.items()}
        for _ in range(6):
            if cur.nodes <= full.nodes:
                break
            for n, rs in cur.blockers.items():
                seen.setdefault(n, {}).update(dict(rs))
            wider = lift | {s for n, rs in seen.items() if n not in full.placement for s in rs}
            if wider == lift:
                break
            lift = wider
            o = realize(mode, inst, min_nodes, lift=frozenset(lift))
            if o is None or o.unplaceable:
                break
            cur = o
        if cur.nodes > full.nodes:
            # Nothing short of fixing every blocker reaches it: say so.
            return full, {n: sorted(rs.items()) for n, rs in seen.items()}, True, []
        for n, rs in cur.blockers.items():
            seen.setdefault(n, {}).update(dict(rs))
        # A node that goes needs every blocker on it fixed; list them all.
        fixes = {
            n: sorted(rs.items())
            for n, rs in seen.items()
            if n not in cur.placement and any(s in lift for s in rs)
        }
        # A fix the replay needed whose veto only showed on a node that stays
        # (a PDB breached by pods landing there) is still a fix: list it there.
        listed = {s for rs in fixes.values() for s, _ in rs}
        for n, rs in seen.items():
            extra = [(s, why) for s, why in rs.items() if s in lift and s not in listed]
            if extra and n in cur.placement:
                fixes.setdefault(n, []).extend(extra)
                listed |= {s for s, _ in extra}
        # Exactly the set the replay lifted, so the console replays the same.
        return cur, fixes, False, sorted(lift)

    def with_headroom(tight: Scenario | None, mode, key, label, inst, pods, same_type: bool,
                      lift: bool, vetoed: dict) -> Scenario | None:
        """The same change on the fewest nodes that leave the headroom target.

        An option can fit and still be a bad recommendation: prod-us2's
        resources-2 "lower minSize to 1" peaked at 96% of its memory over the
        window. The saving comes from the change, not from running the nodes
        to the edge, so offer the change with a floor that keeps the margin --
        usually one more node, and most of the saving.
        """
        import math

        if tight is None or (tight.margin or {}).get("ok", True):
            return None
        if same_type:
            k_max = group_state.nodes_current
        else:
            ratio = max(current.vcpu / max(inst.vcpu, 1), current.memory_gib / max(inst.memory_gib, 1))
            k_max = min(implausible, math.ceil(group_state.nodes_current * ratio) + 1)
        for k in range(tight.nodes + 1, k_max + 1):
            fixes: dict[str, list] = {}
            if lift:
                full = realize(mode, inst, k, lift=True)
                held_k = realize(mode, inst, k)
                if full is None or held_k is None or held_k.nodes <= full.nodes:
                    continue  # nothing to unblock at this size: the plain option covers it
                o, fixes, every, lifted = fewest_fixes(mode, inst, k, held_k, full)
            else:
                o = realize(mode, inst, k)
            if o is None or o.unplaceable:
                continue
            if same_type and o.nodes >= group_state.nodes_current:
                break  # no fewer nodes than today: nothing left to offer
            requires = requires_for(mode, same_type, inst, False, o)
            if not same_type:
                requires.append(f"set the new node group's minSize to {k} to keep headroom")
                name = name_for(label, same_type, inst, False, o) + f", keep {k} nodes for headroom"
            elif k < floor:
                requires.append(f"lower the node group minSize from {floor} to {k}")
                name = name_for(label, same_type, inst, True, o)
            elif k > floor:
                requires.append(f"raise the node group minSize from {floor} to {k} to keep headroom")
                name = name_for(label, same_type, inst, False, o) + f", keep {k} nodes for headroom"
            else:
                name = name_for(label, same_type, inst, False, o)
            if lift:
                requires.append(f"unblock {len(fixes)} node(s) Cluster Autoscaler will not drain")
                name += ", once unblocked"
            roomy = build(key_for(key, same_type, inst, False) + f"-room{k}"
                          + ("-unblocked" if lift else ""), name, inst, pods, o, requires, k)
            if roomy is None or not roomy.margin.get("ok", True):
                continue
            if lift:
                roomy.blocked = {
                    "nodes": fixes, "nodesIfNot": tight.blocked.get("nodesIfNot"),
                    "lift": lifted,
                    "keep": {n: rs for n, rs in held_k.blockers.items() if n not in fixes},
                    "stays": sorted(n for n in o.placement if not n.startswith("(new")),
                }
            roomy.note = (f"{roomy.nodes - tight.nodes} node(s) more than the change needs, to "
                          f"keep the headroom: {'; '.join(tight.margin['notes'])} on "
                          f"{tight.nodes}")
            return roomy
        return None

    # Search: every request set against every instance size.
    found: list[Scenario] = []
    request_sets = [("current", "retype", "")] + modes

    # A Karpenter pool has no node count to set and no type to pin: Karpenter
    # chooses both from the requests and the sizes the pool lists. So each
    # lever set is replayed through Karpenter itself (engine/karpenter.py),
    # once with the pool's own sizes and once per extra size worth offering,
    # and the row is the fleet it arrives at.
    from rightsizer.adapters import autoscalers as _autoscalers
    from rightsizer.adapters import group_source
    from rightsizer.adapters import retype_steps as _rsteps

    pool_cfg = snapshot.facts.config_for(group_state.name) or {}
    pool_owner = group_source(snapshot, pool_cfg)
    pool_info = (_autoscalers.get(pool_owner).pool(pool_cfg, snapshot)
                 if pool_owner != "cloud" and (snapshot.facts.kube or {}).get("pods") else None)
    if pool_info is not None:
        from rightsizer.engine import karpenter as K

        adapter = _autoscalers.get(pool_owner)
        by_name = {i.name: i for i in ladder}
        offered = [by_name[t] for t in (pool_cfg.get("instance_types") or []) if t in by_name]
        if offered:
            lo_v, hi_v = min(i.vcpu for i in offered) / 2, max(i.vcpu for i in offered) * 2
            extras = [i for i in ladder if i.name not in {o.name for o in offered} and lo_v <= i.vcpu <= hi_v]

            def size_for(inst) -> K.Size:
                fc, fm = full_alloc(inst)
                return K.Size(inst.name, inst.vcpu, int(round(fc * 1000)), int(round(fm)),
                              price_of(inst.name), label=adapter.node_label_for(inst.name),
                              mem_bytes=int(inst.memory_gib * GIB))

            def size_of_node(sizes_by_name):
                return lambda n: sizes_by_name.get(adapter.node_type_name(n.labels) or "")

            # Today's fleet, from the nodes' own labels.
            live = R._kube_state(snapshot, {}, {}).cluster
            today_fleet: dict[str, int] = {}
            for n in live.order:
                if live.nodes[n].group == group_state.name:
                    t = adapter.node_type_name(live.nodes[n].labels) or current_type
                    today_fleet[t] = today_fleet.get(t, 0) + 1
            baseline.fleet = [{"type": t, "nodes": n} for t, n in sorted(today_fleet.items())]

            stuck = ""
            gap = 0.0   # today's cost less the no-edit replay's, when the two disagree
            for mode, key, label in request_sets:
                pods = pod_sets.get(mode)
                if pods is None:
                    continue
                requests = R.lever_requests(snapshot, mode, recommendations)
                _deltas, keep = R.lever_changes(snapshot, mode, recommendations)
                for extra in [None, *extras]:
                    sizes = [size_for(i) for i in offered + ([extra] if extra else [])]
                    pool = K.Pool(group_state.name, sizes, policy=pool_info["policy"], spot=pool_info["spot"],
                                  limit_vcpu=pool_info["limit_vcpu"], limit_mem=pool_info.get("limit_mem"),
                                  budgets=tuple(pool_info.get("budgets") or ("10%",)),
                                  undisruptable=pool_info["undisruptable"])
                    out = K.simulate_pool(snapshot, pool, requests=requests, keep=keep,
                                          size_of_node=size_of_node({s.name: s for s in sizes}))
                    if out is None or out.unplaceable or out.nodes <= 0:
                        continue
                    if mode == "current" and extra is None:
                        # Karpenter consolidates continuously, so today's fleet is
                        # already where its rules stop: "let Karpenter
                        # consolidate" is never an option. This replay is the
                        # calibration. When it ends elsewhere the model has a
                        # gap -- something it does not see holds those nodes --
                        # and every option is measured against this replay, so
                        # only what an option's own edits change is counted.
                        if out.fleet != today_fleet:
                            gap = (baseline.monthly_usd or 0.0) - out.monthly_usd
                            stuck = (f"the replay of today runs {out.nodes} node(s); Karpenter runs "
                                     f"{group_state.nodes_current}, so savings here are measured "
                                     "against the replay")
                        baseline.karpenter = {
                            "fleetAfter": [{"type": t, "nodes": n} for t, n in sorted(out.fleet.items())],
                            "nodesAfter": out.nodes, "monthlyAfter": round(out.monthly_usd, 2),
                            "held": [{"node": n, "reason": r} for n, r in sorted(out.held.items())],
                            "notes": list(out.notes) + ([stuck] if stuck else []),
                            "stuck": bool(stuck),
                        }
                        continue
                    dominant = max(out.fleet.items(), key=lambda kv: (kv[1], kv[0]))[0]
                    inst = by_name.get(dominant, current)
                    o = R.Outcome(nodes=out.nodes, mode="karpenter", placement=out.placement,
                                  unplaceable=out.unplaceable)
                    fa = full_alloc(inst)
                    kept = {w: min(n, replicas_now.get(w, n)) for w, n in keep.items()}
                    o.load = R.combine(
                        R.load_check(o, alloc_cpu=fa[0], alloc_mem=fa[1], loads=loads,
                                     replicas_now=replicas_now, replicas_kept=kept, ds_busy=ds_busy,
                                     today=today_load),
                        R.trend_check((getattr(snapshot, "nodegroup_trends", None) or {}).get(group_state.name),
                                      nodes_now=group_state.nodes_current, nodes_after=o.nodes, min_nodes=0,
                                      cpu_scale=inst.vcpu / current.vcpu, mem_scale=inst.memory_gib / current.memory_gib))
                    requires = requires_for(mode, True, inst, False, o)
                    rung_steps = _rsteps(snapshot, pool_cfg, extra.name) if extra else []
                    requires = requires + rung_steps
                    # Like "Move to c6id.xlarge": what to do, in a few words. The
                    # fleet Karpenter then runs is shown beside it, not in it.
                    size = extra.name.split(":")[-1] if extra else ""
                    if label and extra:
                        text = f"{label}, add {size} to the pool"
                    elif extra:
                        text = f"Add {size} to the pool"
                    else:
                        text = label
                    text = text[0].upper() + text[1:]
                    s = build((key if mode != "current" else "karpenter") + (f"-{extra.name}" if extra else ""),
                              text, inst, pods, o, requires, 0, cost=out.monthly_usd + gap)
                    if s is None:
                        continue
                    s.fleet = [{"type": t, "nodes": n} for t, n in sorted(out.fleet.items())]
                    s.retype = extra is not None
                    s.retype_steps = rung_steps
                    notes = []
                    if out.held:
                        reasons = sorted(set(out.held.values()))
                        notes.append(f"{len(out.held)} node{'s' if len(out.held) > 1 else ''} kept: "
                                     + "; ".join(reasons[:2]))
                    if stuck:
                        notes.append(stuck)
                    s.note = "; ".join(notes)
                    found.append(s)
            request_sets = []  # the search below is for groups someone sizes by hand
    for mode, key, label in request_sets:
        pods = pod_sets.get(mode)
        if pods is None:
            continue
        for inst in ladder:
            for effective_floor, floor_note in floor_options():
                same_type = inst.name == current_type
                same_floor = effective_floor == floor
                if mode == "current" and same_type and same_floor:
                    continue  # that is the baseline
                out = realize(mode, inst, effective_floor)
                if out is None:
                    continue
                # What the same edits reach once CA's drain blockers are fixed.
                # The engine stops where CA stops, so without this the saving
                # those blockers hold back is invisible -- and it is often the
                # largest one on the group, for a one-line annotation.
                vetoed = {n: r for n, r in out.vetoed.items() if n != "*"}
                freed = realize(mode, inst, effective_floor, lift=True) if (
                    same_type and vetoed) else None
                if freed is not None and (freed.nodes >= out.nodes or freed.unplaceable):
                    freed = None
                if freed is not None:
                    freed, fixes, every, lifted = fewest_fixes(mode, inst, effective_floor, out, freed)
                if freed is not None and not (floor_note and freed.nodes >= floor):
                    vetoed = {n: fixes[n] for n in fixes}
                    unblock = f"unblock {len(vetoed)} node(s) Cluster Autoscaler will not drain"
                    f_requires = [*requires_for(mode, same_type, inst, floor_note, freed), unblock]
                    f_name = name_for(label, same_type, inst, floor_note, freed) + ", once unblocked"
                    f_key = key_for(key, same_type, inst, floor_note) + "-unblocked"
                    s_freed = build(f_key, f_name, inst, pods, freed, f_requires, effective_floor)
                    if s_freed:
                        s_freed.blocked = {
                            "nodes": vetoed, "nodesIfNot": out.nodes,
                            # Empty: every blocker must go, and the console replays it so.
                            "lift": lifted,
                            # Blocked too, but they stay: no fix needed there.
                            "keep": {n: rs for n, rs in out.blockers.items() if n not in vetoed},
                            "stays": sorted(n for n in freed.placement if not n.startswith("(new")),
                        }
                        s_freed.note = (f"{out.nodes - freed.nodes} node(s) more than Cluster "
                                        f"Autoscaler removes today, once {len(vetoed)} node(s) "
                                        "holding pods it will not evict are unblocked")
                        found.append(s_freed)
                        roomy = with_headroom(s_freed, mode, key, label, inst, pods, same_type,
                                              True, vetoed)
                        if roomy:
                            found.append(roomy)
                # "Lower minSize" only means something when the option ends up
                # below today's floor; otherwise its step reads "from 3 to 3"
                # and it duplicates the same option without the step.
                if floor_note and out.nodes >= floor:
                    continue

                requires = requires_for(mode, same_type, inst, floor_note, out)
                name = name_for(label, same_type, inst, floor_note, out)
                shape_key = key_for(key, same_type, inst, floor_note)
                scenario = build(shape_key, name, inst, pods, out, requires, effective_floor)
                if scenario:
                    found.append(scenario)
                    roomy = with_headroom(scenario, mode, key, label, inst, pods, same_type,
                                          False, vetoed)
                    if roomy:
                        found.append(roomy)

    # Keep every distinct feasible shape, not only the ones that save. A group
    # whose nodes are already tight has no cheaper answer -- its useful answer is
    # a larger one, and dropping cost increases would leave it looking as though
    # nothing could be said about it.
    # Dedupe by outcome, preferring the route that needs the least work. Two
    # routes can land on the same shape at the same price -- one needing three
    # container edits, one needing none -- and showing whichever sorted first
    # hides a real difference in what it costs to carry out.
    #
    # One per *route*, though, not only per shape: cutting requests and lowering
    # HPA floors can land on the same node count by different changes, and which
    # one a team can actually ship is theirs to judge. Collapsing them hid the
    # HPA route on prod-us2 services-2 behind a request option of equal cost.
    best_by_shape: dict[tuple, Scenario] = {}
    for s in sorted(found, key=lambda s: (s.monthly_usd, len(s.requires), s.nodes)):
        best_by_shape.setdefault((s.instance_type, s.nodes, route(s)), s)

    # Is the group short of capacity as it stands? If so an up-size is the
    # headline rather than a curiosity.
    tight = _tightness(group_state, None, TIGHT_SHARE)

    ranked: list[Scenario] = []
    for s in best_by_shape.values():
        if s.delta_usd < -1.0:
            s.direction = "saves"
            s.rationale = "costs less than the current shape"
        elif s.delta_usd > 1.0:
            s.direction = "costs"
            s.rationale = (
                "more capacity than the group runs today"
                + (" -- and it is currently tight" if tight else "")
            )
        else:
            s.direction = "neutral"
            s.rationale = "same cost, different shape"
        ranked.append(s)

    # Savings first, cheapest first; then neutral; then increases, smallest
    # first, so the least expensive way to add capacity leads that section.
    order = {"saves": 0, "neutral": 1, "costs": 2}
    ranked.sort(key=lambda s: (order[s.direction], s.delta_usd if s.direction != "costs" else abs(s.delta_usd)))
    baseline.direction = "neutral"
    baseline.rationale = (
        "measured: what the group runs today"
        + (" -- nodes are tight, see the up-size options" if tight else "")
    )
    return [baseline, *ranked]


def route(scenario) -> tuple[bool, bool, bool, bool]:
    """What an option changes: (requests, HPA floors, minSize, drain blockers).

    The instance type is the fourth dimension and is carried by the option
    itself. Two options on the same route and shape are the same decision.
    """
    req = scenario.requires or []
    return (
        any("container request changes" in r for r in req),
        any("minReplicas" in r for r in req),
        any("minSize" in r for r in req),
        any("will not drain" in r for r in req),
    )


#: A node above this share of allocatable by request is reported as tight. A
#: description of today's cluster, not a packing limit.
TIGHT_SHARE = 0.85


def _tightness(group_state, asis_pack: PackResult | None, headroom: float) -> bool:
    """Is the group already running above the headroom it should keep?

    Read from observed occupancy where available, so the answer describes the
    cluster rather than a simulation of it.
    """
    alloc_cpu = group_state.alloc_cpu_per_node
    alloc_mem = group_state.alloc_mem_per_node
    if group_state.occupancy and alloc_cpu > 0 and alloc_mem > 0:
        for node in group_state.occupancy:
            cpu = (node.get("cpu", 0.0) + node.get("dsCpu", 0.0)) / alloc_cpu
            mem = (node.get("mem", 0.0) * GIB + node.get("dsMem", 0.0) * GIB) / alloc_mem
            if max(cpu, mem) > headroom:
                return True
        return False
    if asis_pack is None:
        return False
    return max(asis_pack.cpu_utilization, asis_pack.mem_utilization) > headroom
