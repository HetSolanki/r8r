"""Data model shared by collect -> analyze -> report.

A snapshot is written to disk between collection and analysis so that analysis is
re-runnable against frozen data: methodology changes can be replayed over the same
window without re-querying Thanos, and two runs over one snapshot are identical.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

Millicores = float  # CPU, cores
Bytes = float       # memory


@dataclass
class UsageStat:
    """Observed usage for one (workload, container) pair over the window.

    ``peak`` and ``p95`` are taken over the *max-across-replicas* series: requests
    are configured per replica, so the replica that matters is the hottest one at
    each instant. Averaging across replicas hides hot shards in sharded workloads.
    """

    resource: str          # "cpu" | "mem"
    namespace: str
    workload: str
    kind: str
    container: str
    replicas: int = 0
    request: float = 0.0
    limit: float = 0.0
    # Per-replica: the worst single replica at each instant. Sizes the request.
    avg: float = 0.0
    p95: float = 0.0
    peak: float = 0.0
    # Across all replicas concurrently. Sizes capacity. Always <= peak x replicas,
    # and usually well below it, because replicas do not peak together.
    total_avg: float = 0.0
    total_p95: float = 0.0
    total_peak: float = 0.0
    #: Quantiles of the concurrent-total series, kept so an HPA can be replayed
    #: against the whole window rather than judged from one scalar. Six numbers
    #: per stat; the series itself would be 8,640 points.
    total_ladder: dict = field(default_factory=dict)
    #: Quantiles of the *per-replica* series -- the hottest replica at each
    #: instant, which is the distribution a request is sized against. Without it
    #: the only answerable question is "what is p95"; with it a reader can ask
    #: how often a container would sit above whatever request is proposed.
    ladder: dict = field(default_factory=dict)
    #: Quantiles of the *mean across replicas* at each instant. That mean is
    #: exactly what an HPA steers on, and it is computed against the replica
    #: count that was live at the time -- so a workload whose minReplicas moved
    #: mid-window is measured correctly, which dividing by today's floor is not.
    avg_ladder: dict = field(default_factory=dict)
    #: True when the request changed during the window. Utilisation is
    #: usage/request, so a replay against today's value mixes two regimes and
    #: every percentage derived from it is part fiction. Reported rather than
    #: silently averaged over.
    request_varied: bool = False
    #: CPU only: the worst share of CFS periods the container was throttled in,
    #: 0..1. The kernel can only throttle against a **limit**, so this is
    #: meaningless without one -- read it beside `limit`, never alone. It is the
    #: one performance signal usage cannot give: a clipped container's usage
    #: looks healthy precisely because the limit is holding it down.
    throttle_pct: float = 0.0
    samples: int = 0

    def at(self, quantile: str) -> float:
        """A point on the per-replica ladder, falling back to p95.

        An older snapshot has no ladder, so asking for p99 on one returns the
        p95 it does have rather than zero -- a smaller number than requested,
        never a larger one, and the caller can check `ladder` to see which.
        """
        if quantile == "max":
            return self.peak
        return float((self.ladder or {}).get(quantile) or self.p95)
    coverage: float = 0.0
    oom_killed: bool = False

    @property
    def util_pct(self) -> float | None:
        return (self.peak / self.request * 100.0) if self.request > 0 else None

    @property
    def total_reserved(self) -> float:
        """What the fleet holds: per-replica request x replicas."""
        return self.request * max(self.replicas, 1)

    @property
    def coincidence(self) -> float | None:
        """total_peak / (peak x replicas) -- how much replicas peak together.

        1.0 means every replica peaked at the same instant; 0.3 means the fleet
        never used more than 30% of what sizing from the worst replica implies.
        The gap is how much a sum-of-peaks estimate over-provisions.

        Undefined below two replicas: with one replica the sum across replicas *is*
        that replica, so the ratio is 1.0 by construction and says nothing about
        anything. Returning None keeps a tautology from reading as a finding.
        """
        if self.replicas < 2:
            return None
        naive = self.peak * self.replicas
        return (self.total_peak / naive) if naive > 0 else None

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.namespace, self.workload, self.kind, self.container)

    @property
    def workload_key(self) -> str:
        return f"{self.namespace}/{self.workload}/{self.kind}"


@dataclass
class NodeGroupState:
    """Capacity and occupancy of one EKS node group."""

    name: str
    nodes_current: int = 0
    nodes_min_30d: int = 0
    nodes_avg_30d: float = 0.0
    nodes_peak_30d: int = 0
    allocatable_cpu: float = 0.0     # total across the group
    allocatable_mem: float = 0.0
    daemonset_cpu: float = 0.0       # total across the group
    daemonset_mem: float = 0.0
    workload_cpu_requests: float = 0.0
    workload_mem_requests: float = 0.0
    pod_count: int = 0
    # DaemonSet pods, which are overhead rather than packable items.
    daemonset_pods: int = 0
    # Observed occupancy per node: [{cpu, mem, pods}], one entry per real node.
    # Distinct from any simulated packing -- this is what is running.
    occupancy: list[dict[str, float]] = field(default_factory=list)
    #: Nodes whose Ready condition was false/unknown, and nodes cordoned, at
    #: collection time. None when the snapshot predates them: unknown, which a
    #: caller must not read as "every node is schedulable".
    not_ready_nodes: list[str] | None = None
    cordoned_nodes: list[str] | None = None

    @property
    def nodes_not_ready(self) -> int | None:
        return None if self.not_ready_nodes is None else len(self.not_ready_nodes)

    @property
    def nodes_cordoned(self) -> int | None:
        return None if self.cordoned_nodes is None else len(self.cordoned_nodes)

    @property
    def unschedulable_nodes(self) -> frozenset[str]:
        """Nodes that accept no new pods: NotReady or cordoned."""
        return frozenset((self.not_ready_nodes or []) + (self.cordoned_nodes or []))

    @property
    def schedulable_pods(self) -> int:
        """Observed pods the scheduler actually places, excluding DaemonSets."""
        return max(0, self.pod_count - self.daemonset_pods)

    def per_node(self, total: float) -> float:
        return total / self.nodes_current if self.nodes_current else 0.0

    @property
    def alloc_cpu_per_node(self) -> float:
        return self.per_node(self.allocatable_cpu)

    @property
    def alloc_mem_per_node(self) -> float:
        return self.per_node(self.allocatable_mem)

    @property
    def ds_cpu_per_node(self) -> float:
        return self.per_node(self.daemonset_cpu)

    @property
    def ds_mem_per_node(self) -> float:
        return self.per_node(self.daemonset_mem)


@dataclass
class HPASpec:
    namespace: str
    name: str
    target_kind: str = ""
    target_name: str = ""
    min_replicas: int = 0
    max_replicas: int = 0
    # (metric_name, metric_target_type) pairs, e.g. ("cpu", "utilization")
    targets: list[tuple[str, str]] = field(default_factory=list)
    # Target utilization as a fraction, e.g. 0.90 for a 90% CPU target. The value
    # matters as much as the type: an HPA only governs replica count while
    # measured utilization is near its target. Far below it, minReplicas governs.
    cpu_target: float | None = None
    # The same for a memory utilization target: working set over request.
    mem_target: float | None = None

    @property
    def resource_targets(self) -> dict[str, float] | None:
        """{"cpu": 0.7, "memory": 0.8} when every target is a cpu/memory
        utilization one with a known value -- what the replay can model --
        else None."""
        if not self.targets:
            # Snapshots that recorded only the value: that is a CPU target.
            return {"cpu": self.cpu_target} if self.cpu_target else None
        out: dict[str, float] = {}
        for name, ttype in self.targets:
            if ttype != "utilization" or name not in ("cpu", "memory"):
                return None
            value = self.cpu_target if name == "cpu" else self.mem_target
            if not value:
                return None
            out[name] = value
        return out or None

    @property
    def scales_on_resource_utilization(self) -> bool:
        """True when this HPA scales on a cpu/memory *utilization* target."""
        return any(
            name in ("cpu", "memory") and ttype == "utilization" for name, ttype in self.targets
        )

    def governs(self, per_replica_p95: float, request: float, replicas: int) -> bool:
        """Is the HPA actually deciding the replica count right now?

        An HPA holds utilization (usage/request) at its target, so while it is in
        control ``replicas x request`` is invariant and cutting a request saves
        nothing -- the HPA simply scales out. That is only true while the HPA is
        the binding constraint. When measured utilization sits far below the
        target, ``minReplicas`` is what holds the replica count up, the request
        is pure over-reservation, and cutting it is a real saving.

        Returns True when the HPA binds (do not recommend a cut), False when the
        replica floor binds (a cut is safe and saves money).
        """
        if not self.scales_on_resource_utilization or not self.cpu_target or request <= 0:
            return True  # unknown target: stay conservative
        equilibrium = (per_replica_p95 * max(replicas, 1)) / self.cpu_target
        reserved = request * max(replicas, 1)
        # 10% margin so a workload sitting right at its target is left alone.
        return reserved <= equilibrium * 1.1


@dataclass
class PDBState:
    namespace: str
    name: str
    desired_healthy: int = 0
    expected_pods: int = 0
    #: Pods counted as healthy (Ready) at collection time. None when the
    #: snapshot predates it -- unknown, which is not the same as all healthy.
    current_healthy: int | None = None

    @property
    def blocks_drain(self) -> bool:
        """desired_healthy == expected_pods means no pod may be evicted."""
        return self.expected_pods > 0 and self.desired_healthy >= self.expected_pods

    @property
    def disruptions_allowed(self) -> int | None:
        """Evictions the budget permits right now: current_healthy - desired_healthy."""
        if self.current_healthy is None:
            return None
        return max(0, self.current_healthy - self.desired_healthy)

    @property
    def unhealthy(self) -> int:
        """Covered pods not currently counted as healthy."""
        if self.current_healthy is None:
            return 0
        return max(0, self.expected_pods - self.current_healthy)

    @property
    def below_minimum(self) -> bool:
        """Fewer healthy pods than the budget requires.

        Under the default ``unhealthyPodEvictionPolicy: IfHealthyBudget`` even
        the unhealthy pods may not be evicted then, so every pod of the set
        pins its node.
        """
        return self.current_healthy is not None and self.current_healthy < self.desired_healthy

    @property
    def blocked_by_unhealthy(self) -> bool:
        """Would allow an eviction if its pods were healthy, but unhealthy ones used it up.

        Distinct from :attr:`blocks_drain`, which is a budget that permits
        nothing by construction. This one is transient: it clears when the
        failing pods recover. With zero evictions allowed the healthy replicas
        cannot be moved; when none are healthy there is nothing being protected
        -- unless the budget is below its minimum, which pins the unhealthy
        pods too.
        """
        if self.blocks_drain or not self.unhealthy or self.disruptions_allowed:
            return False
        return bool(self.current_healthy) or self.below_minimum


@dataclass
class Blocker:
    """A reason a recommendation is not worth acting on, or is unsafe."""

    code: str
    severity: str          # "zero-savings" | "unsafe" | "data"
    subject: str           # workload or node group the blocker applies to
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ContainerRecommendation:
    namespace: str
    workload: str
    kind: str
    container: str
    replicas: int
    cpu_request: float
    cpu_recommended: float | None
    mem_request: float
    mem_recommended: float | None
    nodegroup: str
    nodegroup_share: float
    basis: str
    cpu_total_peak: float = 0.0
    mem_total_peak: float = 0.0
    cpu_coincidence: float | None = None
    blockers: list[Blocker] = field(default_factory=list)

    # "advisory" is guidance attached to a recommendation, not a reason to drop it.
    BLOCKING_SEVERITIES = frozenset({"unsafe", "zero-savings", "data"})

    @property
    def actionable(self) -> bool:
        blocking = [b for b in self.blockers if b.severity in self.BLOCKING_SEVERITIES]
        return not blocking and (
            self.cpu_recommended is not None or self.mem_recommended is not None
        )


@dataclass
class NodeGroupRecommendation:
    name: str
    nodes_current: int
    nodes_avg_30d: float
    nodes_floor: int
    nodes_target: int
    binding_constraint: str
    node_hours_saved_month: float
    blockers: list[Blocker] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def delta_nodes(self) -> float:
        return max(0.0, self.nodes_avg_30d - self.nodes_target)


@dataclass
class ClusterFacts:
    """Constraints that only the Kubernetes and AWS APIs expose.

    Prometheus cannot see pod anti-affinity, topology spread, a PDB's *spec*, or a
    node group's configured minimum -- and those are routinely the real floor on a
    node group, well above what capacity alone implies. Collected optionally: when
    absent, every node count is reported as an upper bound.
    """

    collected: bool = False
    nodegroup_configs: list[dict[str, Any]] = field(default_factory=list)
    workload_constraints: list[dict[str, Any]] = field(default_factory=list)
    pdb_specs: list[dict[str, Any]] = field(default_factory=list)
    node_facts: list[dict[str, Any]] = field(default_factory=list)
    #: The live pods, nodes, budgets and Auto Scaling groups, as the two
    #: controllers read them (:mod:`rightsizer.sources.kfacts`). Empty when not collected.
    kube: dict[str, Any] = field(default_factory=dict)

    def config_for(self, nodegroup: str) -> dict[str, Any] | None:
        return next((c for c in self.nodegroup_configs if c.get("name") == nodegroup), None)

    def max_pods_for(self, nodegroup: str) -> int:
        pods = [
            int(n.get("max_pods", 0))
            for n in self.node_facts
            if n.get("nodegroup") == nodegroup and n.get("max_pods")
        ]
        return min(pods) if pods else 0

    def antiaffinity_floor(self, workload_keys: set[str]) -> tuple[int, str]:
        """Highest replica count among workloads pinned one-per-node.

        A required podAntiAffinity on kubernetes.io/hostname means no two replicas
        share a node, so the group can never hold fewer nodes than that workload
        has replicas -- regardless of how much CPU or memory is free.
        """
        best, who = 0, ""
        for w in self.workload_constraints:
            if not w.get("hard_antiaffinity_hostname"):
                continue
            key = f"{w.get('namespace')}/{w.get('name')}/{w.get('kind')}"
            if workload_keys and key not in workload_keys:
                continue
            replicas = int(w.get("replicas", 0) or 0)
            if replicas > best:
                best, who = replicas, key
        return best, who


@dataclass
class Snapshot:
    """Frozen collection output. Analysis reads only this."""

    env: str
    collected_at: str
    window_days: int
    tunables: dict[str, Any]
    usage: list[UsageStat] = field(default_factory=list)
    nodegroups: list[NodeGroupState] = field(default_factory=list)
    hpas: list[HPASpec] = field(default_factory=list)
    pdbs: list[PDBState] = field(default_factory=list)
    # (namespace, workload, kind) -> {nodegroup: share}
    workload_nodegroups: dict[str, dict[str, float]] = field(default_factory=dict)
    facts: ClusterFacts = field(default_factory=ClusterFacts)
    # Cluster Autoscaler flags as deployed: {source, args}. Kept for snapshots
    # and readers from before `autoscaler`; written only for Cluster Autoscaler.
    ca: dict[str, Any] = field(default_factory=dict)
    #: The node autoscaler found running: {kind, source, settings}. It is what
    #: actually removes nodes, so it bounds every scale-in claim. Read through
    #: :func:`rightsizer.adapters.autoscalers.for_snapshot`.
    autoscaler: dict[str, Any] = field(default_factory=dict)
    # Observed pod -> node placement, per node group. Cluster Autoscaler
    # evacuates specific nodes onto the nodes that remain, so simulating it needs
    # real placement rather than an aggregate.
    placements: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: Pods not Ready at collection time: [{namespace, pod, workload_key,
    #: reasons}]. Explains why a PDB has no disruption budget left.
    unhealthy_pods: list[dict[str, Any]] = field(default_factory=list)
    #: Where this was collected from: {cloud, cluster, region, profile, account,
    #: context}, whatever was given. Lets the exporter place a snapshot without a
    #: hand-maintained table of clusters.
    meta: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    #: Per-minute CPU, desired and ready replicas for each CPU HPA, keyed
    #: ``namespace/hpa``, plus its behavior and readiness lag -- what the HPA
    #: replay runs on. See :mod:`rightsizer.sources.hpa_history`.
    hpa_history: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Per node group: the disk kubelet writes to -- size, peak use over the
    #: window, ephemeral-storage requests. See :mod:`rightsizer.sources.storage`.
    storage: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Per node group: the busiest and a typical node's network traffic, per
    #: direction, in Gbps. See :mod:`rightsizer.sources.network`.
    network: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: ``namespace/pod`` -> the PersistentVolumeClaims it mounts, with storage
    #: class and whether the volume is zonal. See :func:`rightsizer.sources.storage.collect_volumes`.
    volumes: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: Per node group: 6-hourly CPU, memory and disk used and available over
    #: the window. See :mod:`rightsizer.sources.ngtrends`.
    nodegroup_trends: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: The window asked for (``--days``). ``window_days`` is what the store
    #: actually held and what everything was sized from; the two differ when
    #: retention, or the cluster's age, is shorter than the request. None in
    #: snapshots from before this was recorded, which always got what they asked.
    requested_window_days: int | None = None

    @property
    def window_shortened(self) -> bool:
        return bool(self.requested_window_days) and self.requested_window_days > self.window_days

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)

    def write(self, path: Path) -> Path:
        from rightsizer.core.layout import write_atomic

        # Atomic: commands that add to a snapshot rewrite it in place, and an
        # interrupted rewrite must not leave a 16 MB file cut in half.
        return write_atomic(Path(path), self.to_json())

    @classmethod
    def read(cls, path: Path) -> Snapshot:
        raw = json.loads(Path(path).read_text())
        return cls(
            env=raw["env"],
            collected_at=raw["collected_at"],
            window_days=raw["window_days"],
            requested_window_days=raw.get("requested_window_days"),
            tunables=raw["tunables"],
            usage=[UsageStat(**u) for u in raw["usage"]],
            nodegroups=[NodeGroupState(**n) for n in raw["nodegroups"]],
            hpas=[
                HPASpec(**{**h, "targets": [tuple(t) for t in h.get("targets", [])]})
                for h in raw["hpas"]
            ],
            pdbs=[PDBState(**p) for p in raw["pdbs"]],
            workload_nodegroups=raw.get("workload_nodegroups", {}),
            facts=ClusterFacts(**raw.get("facts", {})),
            ca=raw.get("ca", {}),
            autoscaler=raw.get("autoscaler", {}),
            placements=raw.get("placements", {}),
            unhealthy_pods=raw.get("unhealthy_pods", []),
            meta=raw.get("meta", {}),
            warnings=raw.get("warnings", []),
            hpa_history=raw.get("hpa_history", {}),
            storage=raw.get("storage", {}),
            network=raw.get("network", {}),
            volumes=raw.get("volumes", {}),
            nodegroup_trends=raw.get("nodegroup_trends", {}),
        )
