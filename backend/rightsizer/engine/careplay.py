"""Cluster Autoscaler v1.35.0's scale-down, replayed over the whole cluster.

The real controller looks at every node of every node group in one loop, moves
pods across groups when their constraints allow, drains one node at a time
cluster-wide, and leaves where the evicted pods land to kube-scheduler. This
replays exactly that, on the live objects (:mod:`rightsizer.sources.kfacts`), with the
scheduler's own filters and scores (:mod:`rightsizer.engine.kscheduler`):

one **pass** = one CA loop (``StaticAutoscaler.RunOnce`` -> ``scaleDown``):

1. **Gate** -- no scale-down while the cluster is unhealthy (unready nodes above
   both ``ok-total-unready-count`` and ``max-total-unready-percentage``).
2. **Candidates** (``PreFilteringScaleDownNodeProcessor``, ``eligibility``) --
   autoscaled nodes of groups above their ASG minSize, not annotated
   ``scale-down-disabled``, at or below the utilisation threshold. Utilisation
   is ``max(cpu, mem)`` of requests over allocatable with DaemonSet / mirror
   pods out of both sides when configured; GPU nodes use their GPU utilisation
   and threshold. Per-ASG option tags override the cluster flags. Order: empty
   nodes, then last pass's unneeded nodes, then node order.
3. **Simulated removal** (``Planner.categorizeNodes``) -- the drainability
   rules on each pod, in upstream order; then every pod to move placed on the
   other nodes with the scheduler's filters, trying last pass's hint first and
   otherwise rotating from where the previous pod landed. Removals that work
   are kept for the rest of the pass, and their PDB disruptions are spent.
   At most ``unneededNodesLimit`` nodes are found per pass.
4. **Deletion** (``NodesToDelete`` + ``CropNodes``) -- empty nodes up to
   ``max-scale-down-parallelism``, at most ``max-drain-parallelism`` drained
   ones, non-risky first, never below a group's minSize.
5. **Rescheduling** -- the drained pods recreated and bound by kube-scheduler,
   every other unneeded node carrying CA's soft ``DeletionCandidate`` taint.
   A pod nothing can hold goes Pending and CA adds a node to its group.

Not reproducible from a snapshot: the order Kubernetes lists nodes in (taken
as name order), kube-scheduler's random tie-break (first by name), and time --
the unneeded/unready timers, cooldowns and drain durations change when a node
goes, not whether.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

from rightsizer.adapters import gpu_label_keys
from rightsizer.engine.kscheduler import (DELETE_TAINT, SOFT_TAINT, Cluster, KNode, KPod, fits, label_selector_matches,
                         schedule, static_ok)

#: ``replicated.replicatedKind``.
REPLICATED_KINDS = frozenset({"ReplicationController", "Job", "ReplicaSet", "StatefulSet"})
#: ``drain.PodLongTerminatingExtraThreshold``.
LONG_TERMINATING_EXTRA = 30


@dataclass
class Group:
    name: str
    min: int = 0
    max: int = 0
    #: ASG option tags (``k8s.io/cluster-autoscaler/node-template/autoscaling-options/*``).
    options: dict = field(default_factory=dict)
    #: The zones the ASG launches into, in its order. An ASG keeps its
    #: instances balanced: a new one lands in the zone with the fewest, ties
    #: in this order. Empty when unknown (the template's zone is kept).
    zones: list[str] = field(default_factory=list)


#: Node labels that name the zone; every one is rewritten when a node is
#: launched elsewhere, or a PV's node affinity (``topology.ebs.csi.aws.com/zone``)
#: would still see the template's zone.
ZONE_LABELS = ("topology.kubernetes.io/zone", "failure-domain.beta.kubernetes.io/zone",
               "topology.ebs.csi.aws.com/zone")
ZONE_ID_LABEL = "topology.k8s.aws/zone-id"


def zone_of(node: KNode) -> str:
    return node.labels.get(ZONE_LABELS[0], "") or node.labels.get(ZONE_LABELS[1], "")


def launch_zone(cluster: Cluster, group_of: dict[str, str], group: str, info: Group | None) -> str:
    """Where the ASG puts the group's next instance: the zone with the fewest
    of its nodes, ties in the ASG's order. "" when the zones are unknown."""
    zones = list(info.zones) if info and info.zones else []
    if not zones:
        return ""
    count = {z: 0 for z in zones}
    for n in cluster.order:
        if group_of.get(n) == group:
            z = zone_of(cluster.nodes[n])
            if z in count:
                count[z] += 1
    return min(zones, key=lambda z: (count[z], zones.index(z)))


def zone_ids(cluster: Cluster) -> dict[str, str]:
    """zone -> AWS zone-id, from the nodes standing in each zone. Learned
    before a replay removes any, since the one node in a zone may be what goes."""
    out: dict[str, str] = {}
    for n in cluster.nodes.values():
        if (z := zone_of(n)) and ZONE_ID_LABEL in n.labels:
            out.setdefault(z, n.labels[ZONE_ID_LABEL])
    return out


def in_zone(template: KNode, zone: str, ids: dict[str, str] | None = None) -> KNode:
    """``template`` as launched in ``zone``: its zone labels rewritten, the
    AWS zone-id from ``ids`` (it cannot be derived; dropped when unknown)."""
    if not zone or zone == zone_of(template):
        return template
    labels = dict(template.labels)
    # Every zone label, present or not: a PV's node affinity may name the
    # storage driver's key (topology.ebs.csi.aws.com/zone) and the launched
    # node carries it whether or not the template node did.
    for key in ZONE_LABELS:
        labels[key] = zone
    if (ids or {}).get(zone):
        labels[ZONE_ID_LABEL] = ids[zone]
    else:
        labels.pop(ZONE_ID_LABEL, None)
    return replace(template, labels=labels)


@dataclass
class Budget:
    """One PDB: its namespace, selector, and disruptions allowed right now."""
    ns: str
    name: str
    selector: dict | None
    allowed: int | None


@dataclass
class Settings:
    """The Cluster Autoscaler flags the replay reads (live deployment's values)."""
    threshold: float = 0.5
    gpu_threshold: float = 0.5
    ignore_daemonsets: bool = False
    ignore_mirror: bool = False
    skip_local_storage: bool = True
    skip_system_pods: bool = True
    skip_custom_controllers: bool = True
    system_timeout: float = 3600.0
    parallelism: int = 10
    drain_parallelism: int = 1
    expendable_cutoff: int = -10
    ok_unready: int = 3
    max_unready_pct: float = 45.0
    unneeded_time: float = 600.0
    scan_interval: float = 10.0
    enabled: bool = True

    @classmethod
    def from_args(cls, args: dict) -> Settings:
        from rightsizer.engine.autoscaler import _truthy, duration_seconds

        def get(k, d):
            v = args.get(k)
            return d if v is None else v
        return cls(
            threshold=float(get("scale-down-utilization-threshold", 0.5)),
            gpu_threshold=float(get("scale-down-gpu-utilization-threshold", 0.5)),
            ignore_daemonsets=_truthy(get("ignore-daemonsets-utilization", False)),
            ignore_mirror=_truthy(get("ignore-mirror-pods-utilization", False)),
            skip_local_storage=_truthy(get("skip-nodes-with-local-storage", True)),
            skip_system_pods=_truthy(get("skip-nodes-with-system-pods", True)),
            skip_custom_controllers=_truthy(get("skip-nodes-with-custom-controller-pods", True)),
            system_timeout=duration_seconds(get("blocking-system-pod-distruption-timeout", "1h"), 3600.0),
            parallelism=max(1, int(get("max-scale-down-parallelism", 10))),
            drain_parallelism=max(1, int(get("max-drain-parallelism", 1))),
            expendable_cutoff=int(get("expendable-pods-priority-cutoff", -10)),
            ok_unready=int(get("ok-total-unready-count", 3)),
            max_unready_pct=float(get("max-total-unready-percentage", 45)),
            unneeded_time=duration_seconds(get("scale-down-unneeded-time", "10m"), 600.0),
            scan_interval=duration_seconds(get("scan-interval", "10s"), 10.0),
            enabled=_truthy(get("scale-down-enabled", True)),
        )


@dataclass
class Result:
    removed: list[str] = field(default_factory=list)          # drained
    removed_empty: list[str] = field(default_factory=list)
    added: dict[str, int] = field(default_factory=dict)       # group -> nodes CA added
    vetoed: dict[str, str] = field(default_factory=dict)
    blockers: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    not_evacuable: list[str] = field(default_factory=list)
    at_min: list[str] = field(default_factory=list)
    risky: list[str] = field(default_factory=list)
    iterations: int = 0
    healthy: bool = True
    cluster: Cluster | None = None
    group_of: dict[str, str] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.removed) + len(self.removed_empty)

    def nodes_in(self, group: str) -> list[str]:
        return [n for n in (self.cluster.order if self.cluster else []) if self.group_of.get(n) == group]


def matching_budgets(pod: KPod, budgets: list[Budget]) -> list[Budget]:
    return [b for b in budgets if b.ns == pod.ns and label_selector_matches(b.selector, pod.labels)]


def drain_verdict(pod: KPod, *, s: Settings, budgets: list[Budget], left: dict[tuple[str, str], int | None],
                  now: float | None, lifted: bool, lift: frozenset[str]) -> tuple[str, str]:
    """``rules.Default(...).Drainable`` for one pod: (skip|ds|move|block, why)."""
    if pod.is_mirror:
        return "skip", ""
    if pod.deleting is not None and now is not None and \
            now > pod.deleting + (pod.grace if pod.grace is not None else 30) + LONG_TERMINATING_EXTRA:
        return "skip", ""
    if pod.is_daemonset:
        return "ds", ""
    if pod.safe_to_evict == "true":
        return "move", ""
    subject = pod.subject
    if lifted or subject in lift:
        return "move", ""
    if pod.is_bare:
        return "block", f"{subject} has no controller"
    if s.skip_custom_controllers and pod.owner_kind not in REPLICATED_KINDS:
        return "block", f"{subject} is owned by a custom controller ({pod.owner_kind})"
    mine = matching_budgets(pod, budgets)
    if s.skip_system_pods and pod.ns == "kube-system" and not mine:
        age = None if pod.created is None or now is None else now - pod.created
        if age is not None and age > s.system_timeout:
            return "move", ""
        return "block", f"{subject} has no PDB (kube-system pod {pod.name}" + (
            ", age unknown)" if age is None else ")")
    if pod.safe_to_evict == "false":
        return "block", f"{subject} is annotated safe-to-evict=false"
    if s.skip_local_storage and pod.local_volumes:
        return "block", f"{subject} uses local storage without safe-to-evict ({', '.join(pod.local_volumes)})"
    for b in mine if left is not None else ():
        a = left.get((b.ns, b.name))
        if a is not None and a < 1:
            return "block", f"PDB {b.ns}/{b.name} allows no disruption ({subject})"
    return "move", ""


def replay(cluster: Cluster, *, settings: Settings, group_of: dict[str, str], groups: dict[str, Group],
           budgets: list[Budget], now: float | None, lift: frozenset[str] = frozenset(),
           lifted: bool = False, max_iterations: int = 500) -> Result:
    """Run CA's scale-down to quiescence over ``cluster`` (mutated in place)."""
    s = settings
    res = Result(cluster=cluster, group_of=group_of)
    ids = zone_ids(cluster)
    # Expendable pods are not in CA's snapshot at all.
    for name in list(cluster.order):
        cluster.on[name] = [p for p in cluster.on[name]
                            if p.priority is None or p.priority >= s.expendable_cutoff]
    if not s.enabled:
        res.vetoed["*"] = "scale-down is disabled"
        return res
    unready = sum(1 for n in cluster.nodes.values() if not n.ready)
    if unready > s.ok_unready and unready > s.max_unready_pct / 100.0 * len(cluster.nodes):
        res.healthy = False
        res.vetoed["*"] = f"cluster unhealthy: {unready} of {len(cluster.nodes)} nodes unready"
        return res

    static: dict[tuple[int, str], bool] = {}

    def ok(pod: KPod, node: str, where: Cluster) -> bool:
        k = (id(pod.spec), node)
        if k not in static:
            static[k] = static_ok(pod, where.nodes[node])
        return fits(pod, node, where, static=static[k])

    def opt(group: str, key: str, default):
        return (groups.get(group).options if groups.get(group) else {}).get(key, default)

    # The cloud's GPU label is CA's GPULabel(); any registered cloud's counts.
    gpu_keys = gpu_label_keys()

    def is_gpu(n: KNode) -> bool:
        return n.gpu > 0 or any(k in n.labels for k in gpu_keys)

    def utilisation(node: str) -> float:
        n = cluster.nodes[node]
        g = group_of.get(node, "")
        ignore_ds = str(opt(g, "ignoredaemonsetsutilization", s.ignore_daemonsets)).lower() in ("true", "1")
        if is_gpu(n):
            used = skip = 0
            for p in cluster.on[node]:
                if (ignore_ds and p.is_daemonset) or (s.ignore_mirror and p.is_mirror):
                    skip += p.gpu
                    continue
                if _long_terminating(p, now):
                    continue
                used += p.gpu
            return used / (n.gpu - skip) if n.gpu - skip > 0 else 0.0
        c = m = sc = sm = 0
        for p in cluster.on[node]:
            if (ignore_ds and p.is_daemonset) or (s.ignore_mirror and p.is_mirror):
                sc += p.cpu
                sm += p.mem
                continue
            if _long_terminating(p, now):
                continue
            c += p.cpu
            m += p.mem
        cu = c / (n.cpu - sc) if n.cpu - sc > 0 else 0.0
        mu = m / (n.mem - sm) if n.mem - sm > 0 else 0.0
        return max(cu, mu)

    def threshold(node: str) -> float:
        g = group_of.get(node, "")
        if is_gpu(cluster.nodes[node]):
            return float(opt(g, "scaledowngpuutilizationthreshold", s.gpu_threshold))
        return float(opt(g, "scaledownutilizationthreshold", s.threshold))

    def budgets_now() -> dict[tuple[str, str], int | None]:
        return {(b.ns, b.name): b.allowed for b in budgets}

    def verdict(pod: KPod, left) -> tuple[str, str]:
        return drain_verdict(pod, s=s, budgets=budgets, left=left, now=now, lifted=lifted, lift=lift)

    def group_size(g: str) -> int:
        return sum(1 for n in cluster.order if group_of.get(n) == g)

    cursor = [0]
    hints_old: dict[int, str] = {}
    hints_cur: dict[int, str] = {}
    fresh: set[str] = set()
    previous: list[str] = []
    n_par = s.parallelism
    loops_per_unneeded = max(1, int(s.unneeded_time // s.scan_interval)) if s.scan_interval > 0 else 1

    def find_place(node: str, movers: list[KPod], sim: Cluster, gone: set[str]) -> bool:
        """``findPlaceFor``: take ``node`` out of ``sim`` (as v1.35 does before
        scheduling), then each pod onto another node -- its hint if that still
        fits, else the first fitting node rotating from the last match. On
        success the node stays out and the pods stay where they landed; on
        failure ``sim`` is restored. Pod objects are never mutated."""
        held_node, held = sim.nodes[node], sim.remove_node(node)
        listing = list(sim.order)
        placed: list[tuple[str, KPod]] = []
        for pod in movers:
            target = None
            hint = hints_cur.get(id(pod)) or hints_old.get(id(pod))
            if hint and hint in sim.nodes and hint not in gone and ok(pod, hint, sim):
                target = hint
            else:
                for i in range(len(listing)):
                    idx = (cursor[0] + i) % len(listing)
                    if listing[idx] not in gone and ok(pod, listing[idx], sim):
                        target = listing[idx]
                        cursor[0] = (idx + 1) % len(listing)
                        break
            if target is None:
                for t, q in placed:
                    sim.on[t].remove(q)
                sim.add_node(held_node)
                sim.on[node] = held
                return False
            sim.on[target].append(pod)
            placed.append((target, pod))
            hints_cur[id(pod)] = target
        return True

    while res.iterations < max_iterations:
        res.iterations += 1
        at_min = {g for g, info in groups.items() if group_size(g) <= info.min}

        def is_empty(node: str) -> bool:
            return all(verdict(p, None)[0] in ("ds", "skip") for p in cluster.on[node])

        candidates = [
            n for n in cluster.order
            if group_of.get(n) in groups and group_of[n] not in at_min and n not in fresh
            and (not cluster.nodes[n].scale_down_disabled or lifted or f"node/{n}" in lift)
            and utilisation(n) <= threshold(n)]
        prev = set(previous)
        candidates.sort(key=lambda n: (not is_empty(n), n not in prev, cluster.order.index(n)))

        sim = cluster.fork()
        left = budgets_now()
        unneeded: list[str] = []
        movers_of: dict[str, list[KPod]] = {}
        risky: set[str] = set()
        limit = min(len(previous) + 2 * n_par, n_par * loops_per_unneeded + n_par)
        for node in candidates:
            if len(unneeded) >= limit:
                break
            movers, blocked = [], False
            for pod in sim.on[node]:
                v, _ = verdict(pod, left)
                if v == "block":
                    blocked = True
                    break
                if v == "move":
                    movers.append(pod)
            if blocked:
                continue
            if not find_place(node, movers, sim, set(unneeded)):
                continue
            count: dict[tuple[str, str], int] = {}
            for pod in movers:
                for b in matching_budgets(pod, budgets):
                    count[(b.ns, b.name)] = count.get((b.ns, b.name), 0) + 1
            for k, c in count.items():
                a = left.get(k)
                if a is not None:
                    if a < c:
                        risky.add(node)
                    left[k] = a - c
            unneeded.append(node)
            movers_of[node] = movers
        hints_old, hints_cur = hints_cur, {}
        previous = list(unneeded)
        if not unneeded:
            break

        sizes = {g: group_size(g) for g in groups}
        empty_ok, drain_ok = [], []
        for node in [n for n in unneeded if not movers_of[n]] + [n for n in unneeded if movers_of[n]]:
            g = group_of.get(node)
            if sizes.get(g, 0) - 1 < groups[g].min:
                continue
            (drain_ok if movers_of[node] else empty_ok).append(node)
            sizes[g] -= 1
        drain_ok = [n for n in drain_ok if n not in risky] + [n for n in drain_ok if n in risky]
        delete_empty = empty_ok[:n_par]
        delete_drain = drain_ok[:max(0, min(s.drain_parallelism, n_par - len(delete_empty)))]
        if not delete_empty and not delete_drain:
            break
        for node in delete_empty:
            cluster.remove_node(node)
            res.removed_empty.append(node)
        # Taint everything being deleted, soft-taint the other unneeded nodes,
        # then let the scheduler place the evicted pods.
        for node in delete_drain:
            cluster.extra_taints[node] = [DELETE_TAINT]
        for node in unneeded:
            if node in cluster.nodes and node not in delete_drain:
                cluster.extra_taints[node] = [SOFT_TAINT]
        evicted: list[tuple[KPod, str]] = []
        templates: dict[str, tuple[KNode, list[KPod]]] = {}
        for node in delete_drain:
            for pod in cluster.on[node]:
                if pod.is_daemonset or pod.is_mirror:
                    continue
                if verdict(pod, None)[0] == "skip":
                    continue
                evicted.append((pod, node))
            templates[group_of.get(node, "")] = (cluster.nodes[node],
                                                 [p for p in cluster.on[node] if p.is_daemonset])
        for pod, node in evicted:
            cluster.unplace(pod)
            if pod.is_bare:
                continue  # nothing recreates a pod without a controller
            new = replace(pod, node="")
            target = schedule(new, cluster, candidates=[n for n in cluster.order if n not in delete_drain])
            if target is None:
                g = group_of.get(node, "")
                res.added[g] = res.added.get(g, 0) + 1
                tmpl, daemons = templates[g]
                name = f"(new {g} {res.added[g]})"
                tmpl = in_zone(tmpl, launch_zone(cluster, group_of, g, groups.get(g)), ids)
                cluster.add_node(replace(tmpl, name=name, images={},
                                         labels={**tmpl.labels, "kubernetes.io/hostname": name}))
                group_of[name] = g
                fresh.add(name)
                for d in daemons:
                    cluster.place(replace(d, node=name), name)
                target = name
            cluster.place(new, target)
        for node in delete_drain:
            cluster.remove_node(node)
            res.removed.append(node)
            if node in risky:
                res.risky.append(node)
        for node in list(cluster.extra_taints):
            if cluster.extra_taints[node] == [SOFT_TAINT]:
                del cluster.extra_taints[node]

    # Why each candidate that stays is kept.
    at_min = {g for g, info in groups.items() if group_size(g) <= info.min}
    left = budgets_now()
    for node in cluster.order:
        g = group_of.get(node)
        if g not in groups or node in fresh or utilisation(node) > threshold(node):
            continue
        if g in at_min:
            res.at_min.append(node)
            continue
        if cluster.nodes[node].scale_down_disabled and not (lifted or f"node/{node}" in lift):
            why = f"node {node} is annotated cluster-autoscaler.kubernetes.io/scale-down-disabled"
            res.vetoed[node] = why
            res.blockers[node] = [(f"node/{node}", why)]
            continue
        found: dict[str, str] = {}
        movers = []
        for pod in cluster.on[node]:
            v, why = verdict(pod, left)
            if v == "block":
                found.setdefault(pod.subject, why)
            elif v == "move":
                movers.append(pod)
        if found:
            res.vetoed[node] = next(iter(found.values()))
            res.blockers[node] = list(found.items())
            continue
        saved, saved_hints = cursor[0], dict(hints_cur)
        if not find_place(node, movers, cluster.fork(), set()):
            res.not_evacuable.append(node)
        cursor[0], hints_cur = saved, saved_hints
    return res


def _long_terminating(p: KPod, now: float | None) -> bool:
    return p.deleting is not None and now is not None and \
        now > p.deleting + (p.grace if p.grace is not None else 30) + LONG_TERMINATING_EXTRA


def budgets_from_kube(kube: dict, pods: list[KPod]) -> list[Budget]:
    """Each PDB with ``disruptionsAllowed`` as it would be for ``pods``.

    The live status is exact for the cluster as it is. When an option changes
    how many pods a budget covers, ``currentHealthy`` moves with it and the
    allowance is recomputed the way the disruption controller does.
    """
    out = []
    for b in kube.get("pdbs") or []:
        sel = b.get("selector")
        now = sum(1 for p in pods if p.ns == b["ns"] and label_selector_matches(sel, p.labels))
        status = b.get("disruptionsAllowed")
        expected = b.get("expectedPods")
        healthy = b.get("currentHealthy")
        if status is not None and expected is not None and healthy is not None and now == expected:
            allowed = int(status)
        else:
            healthy = (healthy if healthy is not None else now) - ((expected or now) - now)
            expected = now
            desired = _desired_healthy(b, expected)
            allowed = None if desired is None else max(0, healthy - desired)
        out.append(Budget(ns=b["ns"], name=b["name"], selector=sel, allowed=allowed))
    return out


def _desired_healthy(b: dict, expected: int) -> int | None:
    def scaled(v: str) -> int | None:
        v = str(v).strip()
        try:
            if v.endswith("%"):
                return math.ceil(expected * float(v[:-1]) / 100)
            return int(float(v))
        except ValueError:
            return None
    if b.get("maxUnavailable") is not None:
        mu = scaled(b["maxUnavailable"])
        return None if mu is None else max(0, expected - mu)
    if b.get("minAvailable") is not None:
        return scaled(b["minAvailable"])
    return None


# -- the cluster an option leaves ----------------------------------------------------------------

@dataclass
class State:
    cluster: Cluster
    group_of: dict[str, str]
    groups: dict[str, Group]
    budgets: list[Budget]
    settings: Settings
    now: float | None
    #: Pods a rollout could not place on any node, even a new one of their group.
    unplaceable: list = field(default_factory=list)
    #: Nodes the rollout itself made CA add (Pending pods), per group.
    added: dict[str, int] = field(default_factory=dict)


def _workload_of(p: dict) -> tuple[str, str]:
    from rightsizer.sources.collect import logical_workload

    if not p.get("owner_kind"):
        return "", ""
    return logical_workload(p.get("owner_kind", ""), p.get("owner_name", ""))


def state_from_kube(kube: dict, *, recommended: dict | None = None, keep: dict | None = None,
                    min_override: dict[str, int] | None = None, now: float | None = None) -> State:
    """The cluster after an option's edits, ready for :func:`replay`.

    ``keep`` ({workload key: replicas}) lowers HPA floors first: the ReplicaSet
    removes pods from the nodes holding most of its replicas. ``recommended``
    ({(ns, workload, container): (cpu m | None, mem bytes | None)}) is then
    rolled out: each edited Deployment pod is replaced by kube-scheduler
    (surge: the new pod is placed while the old one still runs), each
    StatefulSet pod deleted then recreated; DaemonSet and Job pods change in
    place. A replacement nothing can hold goes Pending and CA adds a node of
    its group; one larger than an empty node of its group is unplaceable.
    """
    from rightsizer.engine.kscheduler import cluster_from_kube

    base_nodes, base_pods = cluster_from_kube(kube, workload_of=_workload_of)
    _, new_pods = cluster_from_kube(kube, recommended=recommended, workload_of=_workload_of) \
        if recommended else (None, base_pods)
    group_of = {n.name: n.group for n in base_nodes if n.group}
    groups: dict[str, Group] = {}
    node_zones: dict[str, list[str]] = {}
    for n in base_nodes:
        if n.group and (z := zone_of(n)) and z not in node_zones.setdefault(n.group, []):
            node_zones[n.group].append(z)
    for a in kube.get("asgs") or []:
        g = a.get("nodegroup") or a.get("name")
        if g:
            groups[g] = Group(name=g, min=int(a.get("min") or 0), max=int(a.get("max") or 0),
                              options=dict(a.get("options") or {}),
                              # Recorded since ASG zones were collected; before
                              # that, the zones its nodes stand in.
                              zones=list(a.get("zones") or sorted(node_zones.get(g, []))))
    for g, zs in node_zones.items():
        groups.setdefault(g, Group(name=g, zones=sorted(zs)))
    for g, m in (min_override or {}).items():
        if g in groups:
            groups[g] = replace(groups[g], min=m)
    settings = Settings.from_args((kube.get("ca") or {}).get("args") or {})
    namespaces = kube.get("namespaces") or {}
    state = State(cluster=Cluster(base_nodes, base_pods, namespaces), group_of=group_of, groups=groups,
                  budgets=[], settings=settings, now=now)
    cl = state.cluster
    ids = zone_ids(cl)
    # HPA floors: drop surplus replicas, nodes holding most of them first.
    if keep:
        by_wl: dict[str, list[KPod]] = {}
        for name in cl.order:
            for p in cl.on[name]:
                if p.wkey in keep and not p.is_daemonset:
                    by_wl.setdefault(p.wkey, []).append(p)
        for wkey, mine in sorted(by_wl.items()):
            for _ in range(max(0, len(mine) - max(0, keep[wkey]))):
                count: dict[str, int] = {}
                for p in mine:
                    count[p.node] = count.get(p.node, 0) + 1
                victim = max(mine, key=lambda p: (count[p.node], p.name))
                mine.remove(victim)
                cl.unplace(victim)
    # Request rollouts.
    if recommended:
        updated = {(p.ns, p.name): p for p in new_pods}
        changed = sorted({p.wkey for p in base_pods
                          if (q := updated.get((p.ns, p.name))) and (q.cpu, q.mem) != (p.cpu, p.mem)})
        templates = {state.group_of.get(n): cl.nodes[n] for n in cl.order}
        daemons_of = {g: [p for p in cl.on[n] if p.is_daemonset] for n in reversed(cl.order)
                      for g in [state.group_of.get(n)]}
        for wkey in changed:
            pods = sorted((p for n in cl.order for p in cl.on[n] if p.wkey == wkey), key=lambda p: p.name)
            for old in pods:
                new = updated.get((old.ns, old.name))
                if new is None:
                    continue
                if old.is_daemonset or old.owner_kind in ("Job", "Node", ""):
                    cl.unplace(old)
                    cl.place(replace(new, node=old.node), old.node)
                    continue
                surge = old.owner_kind == "ReplicaSet"
                if not surge:
                    cl.unplace(old)
                fresh = replace(new, node="")
                target = schedule(fresh, cl)
                if target is None:
                    g = state.group_of.get(old.node, "")
                    tmpl = templates.get(g)
                    if tmpl is None or fresh.cpu > tmpl.cpu - sum(d.cpu for d in daemons_of.get(g, [])) \
                            or fresh.mem > tmpl.mem - sum(d.mem for d in daemons_of.get(g, [])):
                        state.unplaceable.append(fresh)
                        if surge:
                            cl.unplace(old)
                        continue
                    state.added[g] = state.added.get(g, 0) + 1
                    name = f"(scale-up {g} {state.added[g]})"
                    tmpl = in_zone(tmpl, launch_zone(cl, state.group_of, g, groups.get(g)), ids)
                    cl.add_node(replace(tmpl, name=name, images={},
                                        labels={**tmpl.labels, "kubernetes.io/hostname": name}))
                    state.group_of[name] = g
                    for d in daemons_of.get(g, []):
                        cl.place(replace(d, node=name), name)
                    target = name
                if surge:
                    cl.unplace(old)
                cl.place(fresh, target)
    state.budgets = budgets_from_kube(kube, [p for n in cl.order for p in cl.on[n]])
    return state


def replay_state(state: State, *, lift: frozenset[str] = frozenset(), lifted: bool = False) -> Result:
    res = replay(state.cluster, settings=state.settings, group_of=state.group_of, groups=state.groups,
                 budgets=state.budgets, now=state.now, lift=lift, lifted=lifted)
    for g, n in state.added.items():
        res.added[g] = res.added.get(g, 0) + n
    return res


# -- scale-up: CA's binpacking estimator (estimator/binpacking_estimator.go) -----------------------

def estimate(pending: list[KPod], template: KNode, daemons: list[KPod], cluster: Cluster,
             max_new: int, group: str, group_of: dict[str, str]) -> int:
    """How many template nodes CA adds for ``pending`` (``BinpackingNodeEstimator``).

    Equivalence groups (same controller and spec) in decreasing order of
    cpu/allocatable + memory/allocatable; each pod first onto a node this
    estimation added (rotating), else onto the last added node, else onto a
    fresh one -- unless the last one stayed empty, which ends the estimation.
    """
    groups: dict[tuple, list[KPod]] = {}
    for p in pending:
        key = (p.ns, p.owner_kind, p.owner_name, id(p.spec), p.cpu, p.mem, p.gpu, p.ports)
        groups.setdefault(key, []).append(p)

    def podscore(ps: list[KPod]) -> float:
        p = ps[0]
        return (p.cpu / template.cpu if template.cpu else 0.0) + (p.mem / template.mem if template.mem else 0.0)

    ordered = sorted(groups.values(), key=lambda ps: (-podscore(ps), ps[0].name))
    sim = cluster.fork()
    new_names: list[str] = []
    with_pods: set[str] = set()
    last = ""
    cursor = 0
    available = True

    def add() -> str:
        name = f"(estimate {group} {len(new_names) + 1})"
        sim.add_node(replace(template, name=name, images={},
                             labels={**template.labels, "kubernetes.io/hostname": name}))
        for d in daemons:
            sim.on[name].append(replace(d, node=name))
        new_names.append(name)
        group_of.setdefault(name, group)
        return name

    for ps in ordered:
        rest = list(ps)
        # tryToScheduleOnExistingNodes: only the nodes this estimation added.
        while rest and new_names:
            pod = rest[0]
            hit = None
            for i in range(len(new_names)):
                n = new_names[(cursor + i) % len(new_names)]
                if fits(pod, n, sim):
                    hit, cursor = n, (cursor + i + 1) % len(new_names)
                    break
            if hit is None:
                break
            sim.on[hit].append(pod)
            with_pods.add(hit)
            rest.pop(0)
        if not available:
            continue
        for pod in rest:
            if last and fits(pod, last, sim):
                sim.on[last].append(pod)
                with_pods.add(last)
                continue
            if last and last not in with_pods:
                break
            if len(new_names) >= max_new:
                available = False
                break
            last = add()
            if not fits(pod, last, sim):
                break
            sim.on[last].append(pod)
            with_pods.add(last)
    return len(with_pods)


def migrate(state: State, group: str, template: KNode, *, min_size: int, max_size: int = 1000) -> None:
    """Replace ``group``'s nodes with ``template`` nodes, the way it happens:
    the old nodes are drained (kubectl drain), kube-scheduler places what fits
    elsewhere, CA scales the group up for the rest by its estimator, and the
    scheduler binds the pods to the new nodes. Bare pods are deleted by the
    drain and not recreated. Mutates ``state``; leaves ``min_size`` nodes at least.
    """
    cl = state.cluster
    ids = zone_ids(cl)  # before the old nodes go: one of them may be a zone's only node
    old = [n for n in cl.order if state.group_of.get(n) == group]
    daemons = [replace(p) for p in (cl.on[old[0]] if old else []) if p.is_daemonset]
    pending: list[KPod] = []
    for n in old:
        for p in cl.on[n]:
            if not (p.is_daemonset or p.is_mirror or p.is_bare):
                pending.append(replace(p, node=""))
        cl.remove_node(n)
        state.group_of.pop(n, None)
    groups = state.groups
    groups[group] = replace(groups.get(group) or Group(name=group), min=min_size, max=max_size)
    added = 0

    def add_node() -> str:
        """One instance, where the ASG launches it: the least-populated of
        its zones, with that zone's labels."""
        nonlocal added
        added += 1
        name = f"({template.name} {added})"
        tmpl = in_zone(template, launch_zone(cl, state.group_of, group, groups[group]), ids)
        cl.add_node(replace(tmpl, name=name, images={},
                            labels={**tmpl.labels, "kubernetes.io/hostname": name}))
        state.group_of[name] = group
        for d in daemons:
            cl.place(replace(d, node=name), name)
        return name

    def ca_template() -> KNode:
        """What CA believes a new node looks like: a running node of the group
        (it samples one), so one zone. A pod pinned to another zone does not
        fit it, and CA does not scale up for that pod -- the ASG may still put
        the next instance there, but only when something else asks for one."""
        mine = [n for n in cl.order if state.group_of.get(n) == group]
        return in_zone(template, zone_of(cl.nodes[mine[0]]), ids) if mine else template

    for _ in range(min_size):
        add_node()
    rounds = 0
    while pending and rounds < 50:
        rounds += 1
        still = []
        for p in pending:
            t = schedule(p, cl)
            if t is None:
                still.append(p)
            else:
                cl.place(p, t)
        pending = still
        if not pending:
            break
        need = estimate(pending, ca_template(), daemons, cl, max_size - added, group, dict(state.group_of))
        if need <= 0:
            break
        for _ in range(need):
            add_node()
    state.unplaceable.extend(pending)
