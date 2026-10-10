"""Cluster Autoscaler configuration, and what it implies about scale-down.

The simulator answers "how few nodes could hold these pods". Cluster Autoscaler
answers a narrower and more consequential question: "which nodes will I actually
remove?" Those differ, and the gap is why a tidy packing can produce a bill that
does not move.

CA's scale-down rule, as implemented upstream:

1. A node's **utilisation** is the sum of its pods' requests over its allocatable,
   taken as the max of the CPU and memory ratios. With
   ``ignore-daemonsets-utilization=true`` DaemonSet requests are left out of that
   sum -- which raises the bar, because the remaining pods must be sparse enough
   on their own.
2. A node is *unneeded* while utilisation stays below
   ``scale-down-utilization-threshold`` for ``scale-down-unneeded-time``.
3. An unneeded node is removed only if **every** pod on it can be rescheduled
   elsewhere -- CA never relocates pods to create that condition.
4. Several classes of pod veto removal outright: local storage without
   ``safe-to-evict``, kube-system pods with no PDB, and anything annotated
   ``safe-to-evict: false``.
5. The node group cannot go below its configured ``minSize``.

Values are read from the GitOps chart that deploys CA, so the model reflects what
is deployed rather than upstream defaults.
"""

from __future__ import annotations

import dataclasses
import logging
import math
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Upstream defaults, used for any flag the chart leaves unset.
DEFAULTS: dict[str, object] = {
    "scale-down-utilization-threshold": 0.5,
    "scale-down-unneeded-time": "10m",
    "scale-down-delay-after-add": "10m",
    "scale-down-enabled": True,
    "skip-nodes-with-local-storage": True,
    "skip-nodes-with-system-pods": True,
    "ignore-daemonsets-utilization": False,
    "max-graceful-termination-sec": 600,
    "expander": "random",
    # v1.35.0 flags that change which nodes go and in what order.
    "ignore-mirror-pods-utilization": False,
    "skip-nodes-with-custom-controller-pods": True,
    "blocking-system-pod-distruption-timeout": "1h",
    "max-scale-down-parallelism": 10,
    "max-drain-parallelism": 1,
}


def duration_seconds(value: object, default: float = 0.0) -> float:
    """A Go duration ("1h", "5m", "1h0m0s", "90s") in seconds."""
    import re

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value or "").strip()
    parts = re.findall(r"([0-9.]+)(h|ms|m|s)", text)
    if not parts:
        return default
    unit = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}
    return sum(float(n) * unit[u] for n, u in parts)


@dataclass
class CAConfig:
    source: str = "defaults"
    args: dict[str, object] = field(default_factory=lambda: dict(DEFAULTS))

    def get(self, key: str) -> object:
        return self.args.get(key, DEFAULTS.get(key))

    @property
    def utilization_threshold(self) -> float:
        return float(self.get("scale-down-utilization-threshold") or 0.5)

    @property
    def ignores_daemonsets(self) -> bool:
        return _truthy(self.get("ignore-daemonsets-utilization"))

    @property
    def skips_local_storage(self) -> bool:
        return _truthy(self.get("skip-nodes-with-local-storage"))

    @property
    def skips_system_pods(self) -> bool:
        return _truthy(self.get("skip-nodes-with-system-pods"))

    @property
    def enabled(self) -> bool:
        return _truthy(self.get("scale-down-enabled"))

    @property
    def ignores_mirror_pods(self) -> bool:
        return _truthy(self.get("ignore-mirror-pods-utilization"))

    @property
    def skips_custom_controllers(self) -> bool:
        return _truthy(self.get("skip-nodes-with-custom-controller-pods"))

    @property
    def system_pod_timeout(self) -> float:
        """Seconds after which a kube-system pod without a PDB stops blocking."""
        return duration_seconds(self.get("blocking-system-pod-distruption-timeout"), 3600.0)

    @property
    def parallelism(self) -> int:
        return max(1, int(self.get("max-scale-down-parallelism") or 10))

    @property
    def drain_parallelism(self) -> int:
        return max(1, int(self.get("max-drain-parallelism") or 1))

    def non_default(self) -> dict[str, object]:
        """Flags this deployment changes that affect scale-down.

        Live discovery also sees operational flags -- logging verbosity, the cloud
        provider, the auto-discovery tag -- which change nothing about whether a
        node is removed. Reporting them as "non-default" buries the four that
        matter.
        """
        return {
            k: v
            for k, v in self.args.items()
            if k in DEFAULTS and DEFAULTS[k] != v
        }


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


def parse_ca_args(pairs: list[str] | None) -> CAConfig:
    """Build a config from ``key=value`` flags.

    The deployed flags are the input to this model, and a tool that can only read
    them from a checkout of the GitOps repo cannot run anywhere the repo is not.
    Passing them explicitly keeps the tool self-contained and makes the
    configuration it assumed part of its own output.
    """
    args: dict[str, object] = dict(DEFAULTS)
    for pair in pairs or []:
        key, _, raw = pair.partition("=")
        key = key.strip().lstrip("-")
        if not key:
            continue
        args[key] = _coerce(raw.strip())
    return CAConfig(source="--ca-arg flags", args=args)


def _coerce(raw: str) -> object:
    lowered = raw.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        return float(raw) if "." in raw else int(raw)
    except ValueError:
        return raw


def read_ca_from_cluster(context: str | None, namespace: str = "kube-system") -> CAConfig | None:
    """Read the flags off the running Cluster Autoscaler deployment.

    More authoritative than any chart: it is what is actually running, it needs
    no repository checkout, and it uses the kubectl context already supplied.
    Read-only, and allowlisted like every other cluster call.
    """
    from rightsizer.sources.shell import run_json

    argv = ["kubectl"]
    if context:
        argv += ["--context", context]
    argv += ["get", "deployments", "-n", namespace, "-o", "json", "--request-timeout=45s"]
    try:
        payload = run_json(argv, timeout=60)
    except Exception as exc:  # noqa: BLE001 - optional enrichment
        log.warning("could not read cluster-autoscaler from the cluster: %s", exc)
        return None

    args: dict[str, object] = dict(DEFAULTS)
    found = ""
    for item in payload.get("items", []):  # type: ignore[union-attr]
        name = item.get("metadata", {}).get("name", "")
        if "cluster-autoscaler" not in name:
            continue
        for container in item["spec"]["template"]["spec"].get("containers", []):
            for token in list(container.get("command", [])) + list(container.get("args", [])):
                if not token.startswith("--"):
                    continue
                key, _, raw = token[2:].partition("=")
                args[key] = _coerce(raw) if raw else True
        found = name
    if not found:
        log.warning("no cluster-autoscaler deployment found in namespace %s", namespace)
        return None
    return CAConfig(source=f"live deployment {found}", args=args)


@dataclass
class ScaleDownEstimate:
    """What CA would remove from a node group, given its observed occupancy."""

    removable: int
    unneeded: int            # below the utilisation threshold
    floor_blocked: int       # unneeded but held by minSize
    threshold: float
    reasons: list[str] = field(default_factory=list)


def estimate_scale_down(group, config: CAConfig, min_size: int | None = None) -> ScaleDownEstimate:
    """How many nodes CA would remove on its own, right now.

    Deliberately conservative. It counts nodes below the utilisation threshold and
    then subtracts what the replica floor and the veto classes hold back. It does
    **not** claim the pods would find homes -- CA checks that per node against
    live cluster state, and asserting it from a snapshot would overstate.
    """
    reasons: list[str] = []
    if not config.enabled:
        return ScaleDownEstimate(0, 0, 0, config.utilization_threshold,
                                 ["scale-down is disabled on this cluster"])
    if not group.occupancy:
        return ScaleDownEstimate(0, 0, 0, config.utilization_threshold,
                                 ["no observed per-node occupancy"])

    alloc_cpu = group.alloc_cpu_per_node
    alloc_mem = group.alloc_mem_per_node
    ds_cpu = group.ds_cpu_per_node if config.ignores_daemonsets else 0.0
    ds_mem = group.ds_mem_per_node if config.ignores_daemonsets else 0.0

    unneeded = 0
    for node in group.occupancy:
        # utilization.Calculate: DaemonSet requests leave both the sum and the
        # allocatable it is divided by.
        cpu = max(0.0, node.get("cpu", 0.0) - ds_cpu)
        mem = max(0.0, node.get("mem", 0.0) * 1024**3 - ds_mem)
        util = max(
            cpu / (alloc_cpu - ds_cpu) if alloc_cpu > ds_cpu else 0.0,
            mem / (alloc_mem - ds_mem) if alloc_mem > ds_mem else 0.0,
        )
        if util <= config.utilization_threshold:
            unneeded += 1

    if config.ignores_daemonsets:
        reasons.append(
            "DaemonSet requests are excluded from utilisation "
            "(ignore-daemonsets-utilization=true), which makes a node look emptier"
        )

    floor = int(min_size) if min_size is not None else 0
    headroom = max(0, len(group.occupancy) - floor)
    removable = min(unneeded, headroom)
    floor_blocked = unneeded - removable
    if floor_blocked:
        reasons.append(f"{floor_blocked} unneeded node(s) held by minSize={floor}")
    if config.skips_local_storage:
        reasons.append(
            "nodes hosting emptyDir/hostPath pods without safe-to-evict are never removed "
            "(skip-nodes-with-local-storage defaults to true)"
        )
    if config.skips_system_pods:
        reasons.append(
            "nodes hosting kube-system pods without a PDB are never removed "
            "(skip-nodes-with-system-pods defaults to true)"
        )
    return ScaleDownEstimate(removable, unneeded, floor_blocked,
                             config.utilization_threshold, reasons)




# --------------------------------------------------------------------------
# scale-down simulation, following Cluster Autoscaler v1.35.0
# --------------------------------------------------------------------------
#
# One pass of the loop below is one CA iteration (``StaticAutoscaler.scaleDown``):
#
# 1. **Candidates** -- every node at or below the utilisation threshold
#    (``eligibility.Checker``), ordered empty first, then the nodes that were
#    unneeded last pass, then node order (``emptycandidates`` and
#    ``previouscandidates`` sorting).
# 2. **Simulated removal**, one candidate at a time (``Planner.categorizeNodes``):
#    each pod through the drainability rules in upstream order; then every pod
#    that must move tried on the other nodes (``RemovalSimulator.findPlaceFor``),
#    in the node's pod order, each search starting after the node the last pod
#    landed on. A removal that works is kept -- the next candidate sees its pods
#    already moved, cannot send pods to it, and finds the PDB budgets it used
#    spent.
# 3. **Deletion** (``NodesToDelete`` and ``budgets.CropNodes``): every empty
#    unneeded node, up to ``max-scale-down-parallelism``, and at most
#    ``max-drain-parallelism`` (one, by default) that needs draining -- risky
#    ones (more pods of a workload than its PDB lets go at once) last -- never
#    below the group's minSize.
# 4. **Rescheduling** -- the drained pods are recreated and placed by
#    kube-scheduler, not where CA's simulation put them: the node that scores
#    highest on its default resource plugins (``NodeResourcesFit`` least
#    allocated plus ``NodeResourcesBalancedAllocation``).
#
# and the loop runs again until a pass removes nothing. The minutes in between
# (``scale-down-unneeded-time``, the drain itself) change when, not whether.

#: ``replicated.replicatedKind``: with ``--skip-nodes-with-custom-controller-pods``
#: (the default) any other owner blocks the drain.
REPLICATED_KINDS = frozenset({"ReplicationController", "Job", "ReplicaSet", "StatefulSet"})
#: kube-scheduler's stand-in request for a pod that declares none, used when it
#: scores nodes (``schedutil.DefaultMilliCPURequest`` / ``DefaultMemoryRequest``).
DEFAULT_MILLI_CPU = 100
DEFAULT_MEMORY = 200 * 1024 * 1024
MAX_NODE_SCORE = 100
HOSTNAME_KEYS = ("kubernetes.io/hostname", "topology.kubernetes.io/hostname")


@dataclass
class PlacedPod:
    namespace: str
    name: str
    node: str
    cpu: float
    memory: float
    owner_kind: str = ""
    workload_key: str = ""
    #: Unix seconds, when known.
    created: int | None = None

    @property
    def is_daemonset(self) -> bool:
        return self.owner_kind == "DaemonSet"

    @property
    def is_mirror(self) -> bool:
        """A static pod's API mirror: owned by its Node, never evicted."""
        return self.owner_kind == "Node"

    @property
    def is_bare(self) -> bool:
        """No controller, so nothing would recreate it after eviction."""
        return not self.owner_kind or self.owner_kind == "Unknown"

    @property
    def subject(self) -> str:
        """What a fix is attached to: the workload, or the pod when it has none."""
        if self.is_bare or not self.workload_key:
            return f"{self.namespace}/{self.name}"
        return self.workload_key


@dataclass
class ScaleDownRun:
    removed: list[str] = field(default_factory=list)
    removed_empty: list[str] = field(default_factory=list)
    vetoed: dict[str, str] = field(default_factory=dict)      # node -> why
    #: Every blocker on each vetoed node: node -> [(subject, why)]. ``vetoed``
    #: keeps the first, which is where CA stopped looking.
    blockers: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    not_evacuable: list[str] = field(default_factory=list)
    floor_reached: bool = False
    iterations: int = 0
    #: Where every pod sits once the loop stops: node -> pods, remaining nodes
    #: only. CA packs by requests, so this is the placement whose *real* load
    #: :mod:`rightsizer.engine.pressure` checks.
    placement: dict[str, list] = field(default_factory=dict)
    #: Nodes Cluster Autoscaler had to add: for pods a request increase left
    #: with nowhere to fit (set by :func:`rightsizer.engine.eviction.simulate_group`),
    #: and for drained pods kube-scheduler could not place.
    added: int = 0
    #: Pods larger than an empty node of this group: they never schedule, and
    #: no number of nodes of this type changes that.
    unplaceable: list = field(default_factory=list)
    #: Nodes CA drained although they held more of a workload's pods than its
    #: PDB lets go at once (``IsRisky``): the eviction API releases them one by
    #: one as replacements become ready.
    risky: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.removed) + len(self.removed_empty)


def drain_verdict(pod: PlacedPod, *, config: CAConfig, rules=None, now: float | None = None,
                  pdb_left: dict | None = None,
                  local_storage: frozenset[str] = frozenset(),
                  lifted: bool = False,
                  lift: frozenset[str] = frozenset()) -> tuple[str, str]:
    """Cluster Autoscaler's drainability rules for one pod, in upstream order.

    Returns (outcome, why): ``skip`` (a mirror pod: stays, nothing to move),
    ``ds`` (a DaemonSet pod: goes with the node), ``move`` (evicted and
    rescheduled) or ``block`` (the node cannot be removed). The first rule that
    decides wins, as in ``rules.Rules.Drainable``:

    mirror -> DaemonSet -> ``safe-to-evict: "true"`` (overrides everything
    after it) -> no controller or a custom one -> kube-system without a PDB,
    unless older than ``--blocking-system-pod-distruption-timeout`` ->
    ``safe-to-evict: "false"`` -> local storage -> PDB with no disruption left.

    ``lifted`` or a subject in ``lift`` prices the blocker as fixed.
    """
    if pod.is_mirror:
        return "skip", ""
    if pod.is_daemonset:
        return "ds", ""
    if rules is not None and pod.workload_key in rules.safe_to_evict:
        return "move", ""
    subject = pod.subject
    if lifted or subject in lift:
        return "move", ""
    if pod.is_bare:
        return "block", f"{subject} has no controller"
    if config.skips_custom_controllers and pod.owner_kind not in REPLICATED_KINDS:
        return "block", f"{subject} is owned by a custom controller ({pod.owner_kind})"
    if config.skips_system_pods and pod.namespace == "kube-system" and not (
            rules is not None and rules.pdb_for.get(pod.workload_key)):
        age = None if pod.created is None or now is None else now - pod.created
        if age is not None and age > config.system_pod_timeout:
            return "move", ""  # the system rule itself declares it drainable
        return "block", (f"{subject} has no PDB (kube-system pod {pod.name}"
                         + (", age unknown)" if age is None else ")"))
    if rules is not None:
        if pod.workload_key in rules.not_safe_to_evict:
            return "block", f"{subject} is annotated safe-to-evict=false"
        # Rules built by hand name blockers in ``drain_blocked`` alone; local
        # storage among them blocks only under --skip-nodes-with-local-storage.
        if pod.workload_key in rules.drain_blocked and pod.workload_key not in rules.local_storage:
            return "block", rules.drain_blocked[pod.workload_key]
    if config.skips_local_storage and (
            pod.workload_key in local_storage
            or (rules is not None and pod.workload_key in rules.local_storage)):
        return "block", f"{subject} uses local storage without safe-to-evict"
    if rules is not None and pdb_left is not None:
        pdb = rules.pdb_for.get(pod.workload_key)
        if pdb is not None:
            left = pdb_left.get((pdb.namespace, pdb.name))
            if left is not None and left < 1:
                return "block", (f"PDB {pdb.namespace}/{pdb.name} allows no disruption "
                                 f"({subject})")
    return "move", ""


def _nonzero(pod: PlacedPod) -> tuple[float, float]:
    """(millicores, bytes) kube-scheduler scores a pod at."""
    cpu = pod.cpu * 1000
    return (cpu if cpu > 0 else DEFAULT_MILLI_CPU, pod.memory if pod.memory > 0 else DEFAULT_MEMORY)


def scheduler_score(pods_on_node: list, pod: PlacedPod, alloc_cpu: float, alloc_mem: float) -> int:
    """kube-scheduler's resource score for placing ``pod`` here, 0..200.

    ``NodeResourcesFit`` (LeastAllocated, cpu and memory weighted equally, on
    non-zero requests) plus ``NodeResourcesBalancedAllocation`` (one minus the
    spread of the two fractions, on actual requests), both weight 1 in the
    default profile. The other scoring plugins -- preferred affinity, soft
    topology spread, image locality -- are not modelled.
    """
    alloc_m = alloc_cpu * 1000
    req_c, req_m = _nonzero(pod)
    act_c, act_m = pod.cpu * 1000, pod.memory
    for p in pods_on_node:
        c, m = _nonzero(p)
        req_c += c
        req_m += m
        act_c += p.cpu * 1000
        act_m += p.memory
    least = 0
    for req, cap in ((req_c, alloc_m), (req_m, alloc_mem)):
        if cap > 0 and req <= cap:
            least += math.floor((cap - req) * MAX_NODE_SCORE / cap)
    least = least // 2
    fc = min(1.0, act_c / alloc_m) if alloc_m > 0 else 1.0
    fm = min(1.0, act_m / alloc_mem) if alloc_mem > 0 else 1.0
    balanced = math.floor((1 - abs(fc - fm) / 2) * MAX_NODE_SCORE)
    return least + balanced


def simulate_ca_scale_down(
    *,
    nodes: list[str],
    pods: list[PlacedPod],
    alloc_cpu: float,
    alloc_mem: float,
    config: CAConfig,
    min_size: int = 0,
    drain_blocked: frozenset[str] = frozenset(),
    antiaffinity: frozenset[str] = frozenset(),
    rules=None,
    max_iterations: int = 500,
    unschedulable: frozenset[str] = frozenset(),
    max_pods: int | None = None,
    now: float | None = None,
) -> ScaleDownRun:
    """Replay Cluster Autoscaler v1.35.0's scale-down against observed placement.

    See the section comment above for the loop. ``rules`` is an
    :class:`~rightsizer.engine.eviction.EvictionRules`; without it only the classes
    expressible from placement alone are checked -- bare pods, custom
    controllers, kube-system, and ``drain_blocked`` as local storage -- and
    disruption budgets and scheduling constraints are not evaluated at all.
    ``now`` (unix seconds) is the moment the placement was observed, which is
    what a kube-system pod's age is measured against.
    """
    run = ScaleDownRun()
    if not config.enabled:
        run.vetoed["*"] = "scale-down is disabled"
        return run
    if alloc_cpu <= 0 or alloc_mem <= 0 or not nodes:
        return run

    lifted = bool(rules is not None and getattr(rules, "lifted", False))
    lift = frozenset(getattr(rules, "lift", frozenset()) if rules is not None else frozenset())
    no_scale_down = frozenset(getattr(rules, "scale_down_disabled", frozenset())
                              if rules is not None else frozenset())
    hard_spread = (getattr(rules, "hard_spread", None) or {}) if rules is not None else {}
    local_storage = frozenset(drain_blocked)
    threshold = config.utilization_threshold

    remaining = sorted(nodes)
    by_node: dict[str, list[PlacedPod]] = {n: [] for n in remaining}
    # Copies: the replay moves pods, and the caller's placement must not move with it.
    for pod in sorted(pods, key=lambda p: (p.namespace, p.name)):
        if pod.node in by_node:
            by_node[pod.node].append(dataclasses.replace(pod))
    # Nodes CA added itself: kept out of the candidates, as
    # scale-down-delay-after-add keeps them in practice.
    fresh: set[str] = set()

    def verdict(pod: PlacedPod, pdb_left: dict | None) -> tuple[str, str]:
        return drain_verdict(pod, config=config, rules=rules, now=now, pdb_left=pdb_left,
                             local_storage=local_storage, lifted=lifted, lift=lift)

    def utilisation(node: str) -> float:
        # utilization.Calculate: skipped pods leave the sum *and* the
        # allocatable it is divided by.
        cpu = mem = skip_cpu = skip_mem = 0.0
        for p in by_node[node]:
            if ((config.ignores_daemonsets and p.is_daemonset)
                    or (config.ignores_mirror_pods and p.is_mirror)):
                skip_cpu += p.cpu
                skip_mem += p.memory
                continue
            cpu += p.cpu
            mem += p.memory
        cap_cpu, cap_mem = alloc_cpu - skip_cpu, alloc_mem - skip_mem
        return max(cpu / cap_cpu if cap_cpu > 0 else 1.0,
                   mem / cap_mem if cap_mem > 0 else 1.0)

    def budgets() -> dict:
        """Each PDB's disruptions allowed, fresh for a pass."""
        if rules is None:
            return {}
        out = {}
        for pdb in {id(p): p for p in rules.pdb_for.values()}.values():
            out[(pdb.namespace, pdb.name)] = rules.pdb_allowed(pdb)
        return out

    Where = dict[str, list[PlacedPod]]

    def spread_ok(pod: PlacedPod, target: str, where: Where, among: list[str]) -> bool:
        """PodTopologySpread's filter for a hard (DoNotSchedule) constraint."""
        def domain(n: str, key: str) -> str | None:
            if key in HOSTNAME_KEYS:
                return n
            return ((rules.node_labels.get(n) or {}) if rules is not None else {}).get(key)

        for c in hard_spread.get(pod.workload_key, []):
            key, skew = c.get("key", ""), int(c.get("maxSkew", 1))
            counts: dict[str, int] = {}
            for n in among:
                if rules is not None and not rules.can_host(pod, n, set()):
                    continue
                d = domain(n, key)
                if d is None:
                    continue
                mine = sum(1 for p in where[n] if p.workload_key == pod.workload_key)
                counts[d] = counts.get(d, 0) + mine
            d = domain(target, key)
            if d is None or not counts:
                continue
            if counts.get(d, 0) + 1 - min(counts.values()) > skew:
                return False
        return True

    def fits(pod: PlacedPod, target: str, where: Where, among: list[str]) -> bool:
        if target in unschedulable:
            return False
        here = where[target]
        if max_pods and len(here) + 1 > max_pods:
            return False
        if pod.cpu > alloc_cpu - sum(p.cpu for p in here):
            return False
        if pod.memory > alloc_mem - sum(p.memory for p in here):
            return False
        anti = rules.antiaffinity if rules is not None else antiaffinity
        present = {p.workload_key for p in here if p.workload_key in anti}
        if rules is not None:
            if not rules.can_host(pod, target, present):
                return False
        elif pod.workload_key in antiaffinity and pod.workload_key in present:
            return False
        return spread_ok(pod, target, where, among)

    # The scheduler plugin runner's rotation: each search starts after the node
    # the previous pod landed on, for the life of the process.
    cursor = [0]

    def find_place(node: str, movers: list[PlacedPod], where: dict[str, list[PlacedPod]],
                   gone: list[str]) -> dict[int, str] | None:
        """``findPlaceFor``: every pod onto a remaining node, or None."""
        listing = [n for n in remaining if n != node and n not in gone]
        if not listing and movers:
            return None
        plan: dict[int, str] = {}
        placed: list[tuple[str, PlacedPod]] = []
        ok = True
        for pod in movers:
            found = None
            for i in range(len(listing)):
                idx = (cursor[0] + i) % len(listing)
                if fits(pod, listing[idx], where, listing):
                    found = idx
                    break
            if found is None:
                ok = False
                break
            target = listing[found]
            cursor[0] = (found + 1) % len(listing)
            where[target].append(pod)
            placed.append((target, pod))
            plan[id(pod)] = target
        if not ok:
            for target, pod in placed:
                where[target].remove(pod)
            return None
        return plan

    def schedule(pod: PlacedPod) -> str | None:
        """Where kube-scheduler puts a recreated pod: the best-scoring node."""
        best, best_score = None, -1
        for n in remaining:
            if not fits(pod, n, by_node, remaining):
                continue
            score = scheduler_score(by_node[n], pod, alloc_cpu, alloc_mem)
            if score > best_score:
                best, best_score = n, score
        return best

    previous: set[str] = set()
    while run.iterations < max_iterations:
        run.iterations += 1
        if len(remaining) <= min_size:
            run.floor_reached = True
            break

        def is_empty(n: str) -> bool:
            return all(p.is_daemonset or p.is_mirror for p in by_node[n])

        candidates = [n for n in remaining if n not in fresh and utilisation(n) <= threshold
                      and (n not in no_scale_down or lifted or f"node/{n}" in lift)]
        candidates.sort(key=lambda n: (not is_empty(n), n not in previous, remaining.index(n)))

        sim = {n: list(v) for n, v in by_node.items()}
        pdb_left = budgets()
        unneeded: list[str] = []
        moves: dict[str, list[PlacedPod]] = {}
        risky: set[str] = set()
        for node in candidates:
            movers, blocked = [], False
            for pod in sim[node]:
                outcome, _ = verdict(pod, pdb_left)
                if outcome == "block":
                    blocked = True
                    break
                if outcome == "move":
                    movers.append(pod)
            if blocked:
                continue
            if find_place(node, movers, sim, unneeded) is None:
                continue
            sim[node] = []
            # RemainingPdbTracker: CanRemovePods, then RemovePods.
            if rules is not None:
                count: dict[tuple[str, str], int] = {}
                for pod in movers:
                    pdb = rules.pdb_for.get(pod.workload_key)
                    if pdb is not None:
                        k = (pdb.namespace, pdb.name)
                        count[k] = count.get(k, 0) + 1
                for k, c in count.items():
                    left = pdb_left.get(k)
                    if left is not None:
                        if left < c:
                            risky.add(node)
                        pdb_left[k] = left - c
            unneeded.append(node)
            moves[node] = movers
        previous = set(unneeded)
        if not unneeded:
            break

        size = len(remaining)
        empty_ok, drain_ok = [], []
        for node in [n for n in unneeded if not moves[n]] + [n for n in unneeded if moves[n]]:
            if size - 1 < min_size:
                continue
            (drain_ok if moves[node] else empty_ok).append(node)
            size -= 1
        drain_ok = [n for n in drain_ok if n not in risky] + [n for n in drain_ok if n in risky]
        delete_empty = empty_ok[:config.parallelism]
        room = config.parallelism - len(delete_empty)
        delete_drain = drain_ok[:max(0, min(config.drain_parallelism, room))]
        if not delete_empty and not delete_drain:
            run.floor_reached = True
            break
        for node in delete_empty:
            remaining.remove(node)
            run.removed_empty.append(node)
            del by_node[node]
        # Every node being deleted is tainted before any is drained, so none
        # receives another's pods.
        evicted, daemons = [], []
        for node in delete_drain:
            evicted += [p for p in by_node[node] if not (p.is_daemonset or p.is_mirror)]
            daemons = daemons or [p for p in by_node[node] if p.is_daemonset]
            remaining.remove(node)
            run.removed.append(node)
            if node in risky:
                run.risky.append(node)
            del by_node[node]
        if evicted:
            for pod in evicted:
                target = schedule(pod)
                if target is None:
                    # Pending: Cluster Autoscaler scales the group up for it.
                    run.added += 1
                    target = f"(new node {run.added})"
                    remaining.append(target)
                    fresh.add(target)
                    by_node[target] = [dataclasses.replace(d, node=target)
                                       for d in daemons]
                pod.node = target
                by_node[target].append(pod)

    # Why each node that stays is kept: a fresh look at the final state, every
    # blocker listed (CA reports the first; fixing it alone may not be enough).
    pdb_left = budgets()
    for node in remaining:
        if node in fresh or utilisation(node) > threshold:
            continue
        if node in no_scale_down and not (lifted or f"node/{node}" in lift):
            why = (f"node {node} is annotated "
                   "cluster-autoscaler.kubernetes.io/scale-down-disabled")
            run.vetoed[node] = why
            run.blockers[node] = [(f"node/{node}", why)]
            continue
        found: dict[str, str] = {}
        movers = []
        for pod in by_node[node]:
            outcome, why = verdict(pod, pdb_left)
            if outcome == "block":
                found.setdefault(pod.subject, why)
            elif outcome == "move":
                movers.append(pod)
        if found:
            run.vetoed[node] = next(iter(found.values()))
            run.blockers[node] = list(found.items())
            continue
        saved = cursor[0]
        probe = {n: list(v) for n, v in by_node.items()}
        if find_place(node, movers, probe, []) is None:
            run.not_evacuable.append(node)
        cursor[0] = saved

    run.placement = {n: list(by_node[n]) for n in remaining}
    return run
