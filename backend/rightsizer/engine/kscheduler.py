"""kube-scheduler v1.34, as the replay needs it: which nodes a pod may go to,
and which of those it goes to.

Cluster Autoscaler asks the first question when it simulates a node's removal
(``RunFiltersUntilPassingNode``: the scheduler's filter plugins); kube-scheduler
answers the second when the drained pods are recreated (filters, then the
weighted score plugins). Both are ported here from the v1.34.6 source
(``pkg/scheduler/framework/plugins``), the version the EKS control plane runs.

Filters: NodeUnschedulable, TaintToleration, NodeAffinity (nodeSelector and
required affinity), NodePorts, NodeResourcesFit (cpu, memory, ephemeral
storage, GPU, pod count), NodeVolumeLimits (CSI attach limits), VolumeBinding
(bound PVs' node affinity), PodTopologySpread (DoNotSchedule) and
InterPodAffinity (required affinity, anti-affinity both ways).

Scores, with the default profile's weights: TaintToleration x3, NodeAffinity
x2, PodTopologySpread x2 (the pod's own soft constraints, else the system
defaults: hostname maxSkew 3, zone maxSkew 5), InterPodAffinity x2,
NodeResourcesFit (LeastAllocated) x1, NodeResourcesBalancedAllocation x1,
ImageLocality x1; each normalised as its plugin does.

The one step not reproduced: kube-scheduler breaks a tie between equally
scored nodes at random (``selectHost``); here the first node by name wins.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from rightsizer.adapters import node_group_of

MAX_NODE_SCORE = 100
DEFAULT_MILLI_CPU = 100
DEFAULT_MEMORY = 200 * 1024 * 1024
HOSTNAME = "kubernetes.io/hostname"
ZONE = "topology.kubernetes.io/zone"
UNSCHEDULABLE_TAINT = {"key": "node.kubernetes.io/unschedulable", "value": "", "effect": "NoSchedule"}
#: CA marks unneeded nodes with this (``DeletionCandidateOfClusterAutoscaler``),
#: and nodes it is deleting with ``ToBeDeletedByClusterAutoscaler``.
SOFT_TAINT = {"key": "DeletionCandidateOfClusterAutoscaler", "value": "", "effect": "PreferNoSchedule"}
DELETE_TAINT = {"key": "ToBeDeletedByClusterAutoscaler", "value": "", "effect": "NoSchedule"}
SYSTEM_SPREAD = [{"maxSkew": 3, "topologyKey": HOSTNAME}, {"maxSkew": 5, "topologyKey": ZONE}]
MIN_IMAGE = 23 * 1024 * 1024
MAX_IMAGE_PER_CONTAINER = 1000 * 1024 * 1024
WEIGHTS = {"TaintToleration": 3, "NodeAffinity": 2, "PodTopologySpread": 2,
           "InterPodAffinity": 2, "NodeResourcesFit": 1, "NodeResourcesBalancedAllocation": 1,
           "ImageLocality": 1}


# -- selectors ---------------------------------------------------------------------

def label_selector_matches(sel: dict | None, labels: dict) -> bool:
    """``metav1.LabelSelectorAsSelector``: nil selects nothing, {} everything."""
    if sel is None:
        return False
    for k, v in (sel.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False
    for e in sel.get("matchExpressions") or []:
        if not _requirement(e, labels):
            return False
    return True


def _requirement(e: dict, labels: dict) -> bool:
    key, op, values = e.get("key", ""), e.get("operator", ""), e.get("values") or []
    has = key in labels
    if op == "In":
        return has and labels[key] in values
    if op == "NotIn":
        return not has or labels[key] not in values
    if op == "Exists":
        return has
    if op == "DoesNotExist":
        return not has
    if op in ("Gt", "Lt"):
        if not has or not values:
            return False
        try:
            a, b = int(labels[key]), int(values[0])
        except ValueError:
            return False
        return a > b if op == "Gt" else a < b
    return False


def node_term_matches(term: dict, node: KNode) -> bool:
    """One NodeSelectorTerm: every expression and field must hold; empty matches nothing."""
    exprs, fields = term.get("matchExpressions") or [], term.get("matchFields") or []
    if not exprs and not fields:
        return False
    if not all(_requirement(e, node.labels) for e in exprs):
        return False
    return all(_requirement(f, {"metadata.name": node.name}) for f in fields)


def required_node_affinity(spec: PodSpec, node: KNode) -> bool:
    """``nodeaffinity.GetRequiredNodeAffinity(pod).Match``: nodeSelector AND any term."""
    for k, v in spec.node_selector.items():
        if node.labels.get(k) != v:
            return False
    if spec.node_required:
        return any(node_term_matches(t, node) for t in spec.node_required)
    return True


def tolerates(tol: dict, taint: dict) -> bool:
    """``v1.Toleration.ToleratesTaint``."""
    if tol.get("effect") and tol["effect"] != taint.get("effect"):
        return False
    if tol.get("key") and tol["key"] != taint.get("key"):
        return False
    op = tol.get("operator") or "Equal"
    if op == "Exists":
        return True
    if op == "Equal":
        return (tol.get("value") or "") == (taint.get("value") or "")
    return False


def untolerated(taints: list[dict], tolerations: list[dict], effects: tuple[str, ...]) -> bool:
    return any(t.get("effect") in effects and not any(tolerates(o, t) for o in tolerations)
               for t in taints)


# -- the objects -------------------------------------------------------------------------

@dataclass(eq=False)
class PodSpec:
    """What a pod asks of its node -- shared by every replica of a workload."""
    node_selector: dict = field(default_factory=dict)
    node_required: list = field(default_factory=list)
    node_preferred: list = field(default_factory=list)
    tolerations: list = field(default_factory=list)
    pod_affinity: list = field(default_factory=list)
    pod_anti: list = field(default_factory=list)
    pod_affinity_pref: list = field(default_factory=list)
    pod_anti_pref: list = field(default_factory=list)
    spread: list = field(default_factory=list)
    #: The default spread selector (Services + owner), None when it is empty.
    default_selector: dict | None = None


@dataclass(eq=False)
class KPod:
    ns: str
    name: str
    node: str
    spec: PodSpec
    owner_kind: str = ""
    owner_name: str = ""
    wkey: str = ""
    labels: dict = field(default_factory=dict)
    created: int | None = None
    deleting: int | None = None
    grace: int | None = None
    priority: int | None = None
    safe_to_evict: str | None = None
    #: The pod carries ``karpenter.sh/do-not-disrupt: "true"``.
    do_not_disrupt: bool = False
    #: Volumes CA counts as local storage (after tmpfs and the annotation).
    local_volumes: tuple = ()
    #: PodRequests: millicores, bytes, GPUs, ephemeral bytes.
    cpu: int = 0
    mem: int = 0
    gpu: int = 0
    eph: int = 0
    #: The same with kube-scheduler's stand-in for missing requests (scoring).
    nz_cpu: int = 0
    nz_mem: int = 0
    images: tuple = ()
    ports: tuple = ()           # (port, protocol, hostIP)
    #: Bound CSI volumes: ((driver, handle), ...) and their PVs' node terms.
    csi: tuple = ()
    pv_terms: tuple = ()
    n_containers: int = 1

    @property
    def is_daemonset(self) -> bool:
        return self.owner_kind == "DaemonSet"

    @property
    def is_mirror(self) -> bool:
        return self.owner_kind == "Node"

    @property
    def is_bare(self) -> bool:
        return not self.owner_kind or self.owner_kind == "Unknown"

    @property
    def subject(self) -> str:
        if self.is_bare or not self.wkey:
            return f"{self.ns}/{self.name}"
        return self.wkey


@dataclass(eq=False)
class KNode:
    name: str
    labels: dict = field(default_factory=dict)
    taints: list = field(default_factory=list)
    cpu: int = 0
    mem: int = 0
    pods: int = 110
    gpu: int = 0
    eph: int = 0
    unschedulable: bool = False
    ready: bool = True
    scale_down_disabled: bool = False
    group: str = ""
    images: dict = field(default_factory=dict)   # normalised name -> size
    csi_limits: dict = field(default_factory=dict)


def normalized_image(name: str) -> str:
    if name.rfind(":") <= name.rfind("/"):
        name = name + ":latest"
    return name


class Cluster:
    """Nodes and the pods on them, with the sums the plugins read."""

    def __init__(self, nodes: list[KNode], pods: list[KPod], namespaces: dict | None = None):
        self.nodes: dict[str, KNode] = {n.name: n for n in nodes}
        self.order: list[str] = sorted(self.nodes)
        self.on: dict[str, list[KPod]] = {n: [] for n in self.order}
        for p in pods:
            if p.node in self.on:
                self.on[p.node].append(p)
        self.namespaces = namespaces or {}
        self.extra_taints: dict[str, list[dict]] = {}

    # sums ----------------------------------------------------------------------
    def requested(self, node: str) -> tuple[int, int, int, int]:
        c = m = g = e = 0
        for p in self.on[node]:
            c += p.cpu
            m += p.mem
            g += p.gpu
            e += p.eph
        return c, m, g, e

    def nonzero_requested(self, node: str) -> tuple[int, int]:
        c = m = 0
        for p in self.on[node]:
            c += p.nz_cpu
            m += p.nz_mem
        return c, m

    def taints(self, node: str) -> list[dict]:
        return self.nodes[node].taints + self.extra_taints.get(node, [])

    # mutation ------------------------------------------------------------------
    def place(self, pod: KPod, node: str) -> None:
        pod.node = node
        self.on[node].append(pod)

    def unplace(self, pod: KPod) -> None:
        self.on[pod.node].remove(pod)

    def remove_node(self, node: str) -> list[KPod]:
        pods = self.on.pop(node, [])
        self.nodes.pop(node, None)
        self.order.remove(node)
        self.extra_taints.pop(node, None)
        return pods

    def add_node(self, node: KNode) -> None:
        self.nodes[node.name] = node
        self.on[node.name] = []
        self.order = sorted(self.nodes)

    def fork(self) -> Cluster:
        c = Cluster.__new__(Cluster)
        c.nodes = dict(self.nodes)
        c.order = list(self.order)
        c.on = {n: list(v) for n, v in self.on.items()}
        c.namespaces = self.namespaces
        c.extra_taints = {n: list(v) for n, v in self.extra_taints.items()}
        return c


# -- affinity terms -------------------------------------------------------------------------

def _term_namespaces(term: dict, owner_ns: str) -> set[str]:
    if not term.get("namespaces") and term.get("namespaceSelector") is None:
        return {owner_ns}
    return set(term.get("namespaces") or [])


_TERM_CACHE: dict[tuple, tuple] = {}


def term_matches(term: dict, owner_ns: str, target: KPod, namespaces: dict) -> bool:
    """``AffinityTerm.Matches``: the target's namespace is named or selected, and its
    labels match the term's selector.

    Memoised on the term and label objects themselves (replicas share both), and
    the cached entry holds them, so a key can never be reused by another object.
    """
    key = (id(term), owner_ns, target.ns, id(target.labels), id(namespaces))
    hit = _TERM_CACHE.get(key)
    if hit is not None and hit[0] is term and hit[1] is target.labels:
        return hit[2]
    result = _term_matches(term, owner_ns, target, namespaces)
    if len(_TERM_CACHE) > 2_000_000:
        _TERM_CACHE.clear()
    _TERM_CACHE[key] = (term, target.labels, result)
    return result


def _term_matches(term: dict, owner_ns: str, target: KPod, namespaces: dict) -> bool:
    in_ns = target.ns in _term_namespaces(term, owner_ns) or label_selector_matches(
        term.get("namespaceSelector"), namespaces.get(target.ns, {}))
    return in_ns and label_selector_matches(term.get("labelSelector"), target.labels)


# -- filters ---------------------------------------------------------------------------------

def static_ok(pod: KPod, node: KNode) -> bool:
    """The filters that depend on the node alone, not on what else runs there."""
    taints = node.taints
    if node.unschedulable and not any(tolerates(t, UNSCHEDULABLE_TAINT) for t in pod.spec.tolerations):
        return False
    if untolerated(taints, pod.spec.tolerations, ("NoSchedule", "NoExecute")):
        return False
    if not required_node_affinity(pod.spec, node):
        return False
    for terms in pod.pv_terms:
        if terms and not any(node_term_matches(t, node) for t in terms):
            return False
    return True


def fits(pod: KPod, node: str, cluster: Cluster, *, static: bool | None = None) -> bool:
    """Every filter plugin, against the cluster as it stands."""
    n = cluster.nodes[node]
    if static is None:
        static = static_ok(pod, n)
    if not static:
        return False
    extra = cluster.extra_taints.get(node)
    if extra and untolerated(extra, pod.spec.tolerations, ("NoSchedule", "NoExecute")):
        return False
    here = cluster.on[node]
    # NodeResourcesFit
    if len(here) + 1 > n.pods:
        return False
    if pod.cpu or pod.mem or pod.gpu or pod.eph:
        c, m, g, e = cluster.requested(node)
        if pod.cpu > 0 and pod.cpu > n.cpu - c:
            return False
        if pod.mem > 0 and pod.mem > n.mem - m:
            return False
        if pod.eph > 0 and pod.eph > n.eph - e:
            return False
        if pod.gpu > 0 and pod.gpu > n.gpu - g:
            return False
    # NodePorts
    if pod.ports:
        used = [q for p in here for q in p.ports]
        for port, proto, ip in pod.ports:
            for uport, uproto, uip in used:
                if port == uport and proto == uproto and (ip == uip or "0.0.0.0" in (ip, uip)):
                    return False
    # NodeVolumeLimits (CSI)
    if pod.csi:
        attached: dict[str, set] = {}
        for p in here:
            for drv, handle in p.csi:
                attached.setdefault(drv, set()).add(handle)
        for drv in {d for d, _ in pod.csi}:
            limit = n.csi_limits.get(drv)
            if limit is None:
                continue
            new = {h for d, h in pod.csi if d == drv} - attached.get(drv, set())
            if new and len(attached.get(drv, set())) + len(new) > limit:
                return False
    # InterPodAffinity
    if not _pod_affinity_ok(pod, n, cluster):
        return False
    # PodTopologySpread (DoNotSchedule)
    hard = [c for c in pod.spec.spread if c.get("whenUnsatisfiable") == "DoNotSchedule"]
    return not (hard and not _spread_ok(pod, n, cluster, hard))


def _pod_affinity_ok(pod: KPod, node: KNode, cluster: Cluster) -> bool:
    ns = cluster.namespaces
    # Existing pods' required anti-affinity against the incoming pod.
    for other_name, pods in cluster.on.items():
        other = cluster.nodes[other_name]
        for e in pods:
            for t in e.spec.pod_anti:
                key = t.get("topologyKey", "")
                if key in node.labels and other.labels.get(key) == node.labels[key] \
                        and term_matches(t, e.ns, pod, ns):
                    return False
    for t in pod.spec.pod_anti:
        key = t.get("topologyKey", "")
        if key not in node.labels:
            continue
        for other_name, pods in cluster.on.items():
            if cluster.nodes[other_name].labels.get(key) != node.labels[key]:
                continue
            if any(term_matches(t, pod.ns, e, ns) for e in pods):
                return False
    for t in pod.spec.pod_affinity:
        key = t.get("topologyKey", "")
        if key not in node.labels:
            return False
        found = any(term_matches(t, pod.ns, e, ns)
                    for o, pods in cluster.on.items() if cluster.nodes[o].labels.get(key) == node.labels[key]
                    for e in pods)
        if not found:
            # The first pod of a group that selects itself may go anywhere.
            anywhere = any(term_matches(t, pod.ns, e, ns) for pods in cluster.on.values() for e in pods)
            if anywhere or not term_matches(t, pod.ns, pod, ns):
                return False
    return True


def _spread_selector(c: dict, pod: KPod) -> dict | None:
    sel = c.get("labelSelector")
    keys = c.get("matchLabelKeys") or []
    if keys and sel is not None:
        extra = {k: pod.labels[k] for k in keys if k in pod.labels}
        if extra:
            sel = {"matchLabels": {**(sel.get("matchLabels") or {}), **extra},
                   "matchExpressions": list(sel.get("matchExpressions") or [])}
    return sel


def _count(pods: list[KPod], sel: dict | None, ns: str) -> int:
    """``countPodsMatchSelector``: an empty selector (Everything) counts nothing,
    as does nil (Nothing)."""
    if sel is None or not (sel.get("matchLabels") or sel.get("matchExpressions")):
        return 0
    return sum(1 for p in pods if p.deleting is None and p.ns == ns
               and label_selector_matches(sel, p.labels))


def _included(c: dict, pod: KPod, node: KNode) -> bool:
    if (c.get("nodeAffinityPolicy") or "Honor") == "Honor" and not required_node_affinity(pod.spec, node):
        return False
    return not ((c.get("nodeTaintsPolicy") or "Ignore") == "Honor"
                and untolerated(node.taints, pod.spec.tolerations, ("NoSchedule", "NoExecute")))


def _spread_ok(pod: KPod, node: KNode, cluster: Cluster, hard: list[dict]) -> bool:
    for c in hard:
        key = c.get("topologyKey", "")
        if key not in node.labels:
            return False
        sel = _spread_selector(c, pod)
        counts: dict[str, int] = {}
        for name in cluster.order:
            n = cluster.nodes[name]
            if key not in n.labels or not _included(c, pod, n):
                continue
            counts[n.labels[key]] = counts.get(n.labels[key], 0) + _count(cluster.on[name], sel, pod.ns)
        domains = len(counts)
        min_match = min(counts.values()) if counts else 0
        if domains < int(c.get("minDomains") or 1):
            min_match = 0
        self_match = 1 if label_selector_matches(sel, pod.labels) else 0
        if counts.get(node.labels[key], 0) + self_match - min_match > int(c.get("maxSkew", 1)):
            return False
    return True


# -- scores ------------------------------------------------------------------------------------

def _least(requested: int, capacity: int) -> int:
    if capacity == 0 or requested > capacity:
        return 0
    return ((capacity - requested) * MAX_NODE_SCORE) // capacity


def _fit_score(pod: KPod, node: KNode, cluster: Cluster) -> int:
    c, m = cluster.nonzero_requested(node.name)
    total = weight = 0
    for req, cap in ((c + pod.nz_cpu, node.cpu), (m + pod.nz_mem, node.mem)):
        if cap == 0:
            continue
        total += _least(req, cap)
        weight += 1
    return total // weight if weight else 0


def _balanced_score(pod: KPod, node: KNode, cluster: Cluster) -> int:
    c, m, _, _ = cluster.requested(node.name)
    fractions = []
    for req, cap in ((c + pod.cpu, node.cpu), (m + pod.mem, node.mem)):
        if cap == 0:
            continue
        fractions.append(min(1.0, req / cap))
    std = 0.0
    if len(fractions) == 2:
        std = abs((fractions[0] - fractions[1]) / 2)
    return int((1 - std) * MAX_NODE_SCORE)


def _default_normalize(scores: dict[str, int], reverse: bool) -> dict[str, int]:
    top = max(scores.values(), default=0)
    if top == 0:
        return {k: (MAX_NODE_SCORE if reverse else v) for k, v in scores.items()}
    out = {}
    for k, v in scores.items():
        s = MAX_NODE_SCORE * v // top
        out[k] = MAX_NODE_SCORE - s if reverse else s
    return out


def _spread_scores(pod: KPod, feasible: list[str], cluster: Cluster) -> dict[str, int] | None:
    own = [c for c in pod.spec.spread if c.get("whenUnsatisfiable") == "ScheduleAnyway"]
    if own:
        constraints = [{"topologyKey": c["topologyKey"], "maxSkew": int(c.get("maxSkew", 1)),
                        "selector": _spread_selector(c, pod),
                        "nodeAffinityPolicy": c.get("nodeAffinityPolicy") or "Honor",
                        "nodeTaintsPolicy": c.get("nodeTaintsPolicy") or "Ignore"} for c in own]
        require_all = True
    elif pod.spec.spread:
        return None  # only hard constraints: nothing soft to score
    else:
        if pod.spec.default_selector is None:
            return None
        constraints = [{"topologyKey": c["topologyKey"], "maxSkew": c["maxSkew"],
                        "selector": pod.spec.default_selector, "nodeAffinityPolicy": "Honor",
                        "nodeTaintsPolicy": "Ignore"} for c in SYSTEM_SPREAD]
        require_all = False
    ignored: set[str] = set()
    tp_counts: list[dict[str, int]] = [{} for _ in constraints]
    topo_size = [0] * len(constraints)
    for name in feasible:
        n = cluster.nodes[name]
        if require_all and not all(c["topologyKey"] in n.labels for c in constraints):
            ignored.add(name)
            continue
        for i, c in enumerate(constraints):
            if c["topologyKey"] == HOSTNAME:
                continue
            value = n.labels.get(c["topologyKey"])
            if value not in tp_counts[i]:
                tp_counts[i][value] = 0
                topo_size[i] += 1
    weights = []
    for i, c in enumerate(constraints):
        size = len(feasible) - len(ignored) if c["topologyKey"] == HOSTNAME else topo_size[i]
        weights.append(math.log(size + 2))
    for name in cluster.order:
        n = cluster.nodes[name]
        if require_all and not all(c["topologyKey"] in n.labels for c in constraints):
            continue
        for i, c in enumerate(constraints):
            if not _included(c, pod, n):
                continue
            value = n.labels.get(c["topologyKey"])
            if value not in tp_counts[i]:
                continue
            tp_counts[i][value] += _count(cluster.on[name], c["selector"], pod.ns)
    raw: dict[str, int] = {}
    for name in feasible:
        n = cluster.nodes[name]
        if name in ignored:
            raw[name] = 0
            continue
        score = 0.0
        for i, c in enumerate(constraints):
            key = c["topologyKey"]
            if key in n.labels:
                if key == HOSTNAME:
                    cnt = _count(cluster.on[name], c["selector"], pod.ns)
                else:
                    cnt = tp_counts[i][n.labels[key]]
                score += cnt * weights[i] + (c["maxSkew"] - 1)
        raw[name] = int(_round_half_away(score))
    lo = min((v for k, v in raw.items() if k not in ignored), default=0)
    hi = max((v for k, v in raw.items() if k not in ignored), default=0)
    out = {}
    for k, v in raw.items():
        if k in ignored:
            out[k] = 0
        elif hi == 0:
            out[k] = MAX_NODE_SCORE
        else:
            out[k] = MAX_NODE_SCORE * (hi + lo - v) // hi
    return out


def _round_half_away(x: float) -> float:
    """Go's math.Round: half away from zero."""
    return math.floor(x + 0.5) if x >= 0 else -math.floor(-x + 0.5)


def _interpod_scores(pod: KPod, feasible: list[str], cluster: Cluster) -> dict[str, int] | None:
    ns = cluster.namespaces
    has_own = bool(pod.spec.pod_affinity_pref or pod.spec.pod_anti_pref)
    topo: dict[str, dict[str, int]] = {}

    def add(key: str, value: str, w: int) -> None:
        topo.setdefault(key, {})
        topo[key][value] = topo[key].get(value, 0) + w

    any_term = False
    for name in cluster.order:
        node = cluster.nodes[name]
        if not node.labels:
            continue
        for e in cluster.on[name]:
            es = e.spec
            if not has_own and not (es.pod_affinity or es.pod_anti or es.pod_affinity_pref or es.pod_anti_pref):
                continue
            for wt in pod.spec.pod_affinity_pref:
                t = wt["term"]
                if term_matches(t, pod.ns, e, ns) and t.get("topologyKey") in node.labels:
                    add(t["topologyKey"], node.labels[t["topologyKey"]], wt["weight"])
                    any_term = True
            for wt in pod.spec.pod_anti_pref:
                t = wt["term"]
                if term_matches(t, pod.ns, e, ns) and t.get("topologyKey") in node.labels:
                    add(t["topologyKey"], node.labels[t["topologyKey"]], -wt["weight"])
                    any_term = True
            for t in es.pod_affinity:  # hardPodAffinityWeight 1
                if term_matches(t, e.ns, pod, ns) and t.get("topologyKey") in node.labels:
                    add(t["topologyKey"], node.labels[t["topologyKey"]], 1)
                    any_term = True
            for wt in es.pod_affinity_pref:
                t = wt["term"]
                if term_matches(t, e.ns, pod, ns) and t.get("topologyKey") in node.labels:
                    add(t["topologyKey"], node.labels[t["topologyKey"]], wt["weight"])
                    any_term = True
            for wt in es.pod_anti_pref:
                t = wt["term"]
                if term_matches(t, e.ns, pod, ns) and t.get("topologyKey") in node.labels:
                    add(t["topologyKey"], node.labels[t["topologyKey"]], -wt["weight"])
                    any_term = True
    if not any_term and not topo:
        return None
    raw = {}
    for name in feasible:
        labels = cluster.nodes[name].labels
        raw[name] = sum(vals.get(labels[k], 0) for k, vals in topo.items() if k in labels)
    if not topo:
        return raw
    lo, hi = min(raw.values(), default=0), max(raw.values(), default=0)
    diff = hi - lo
    return {k: int(MAX_NODE_SCORE * ((v - lo) / diff)) if diff > 0 else 0 for k, v in raw.items()}


def _image_score(pod: KPod, node: KNode, cluster: Cluster, spread: dict[str, int]) -> int:
    total_nodes = len(cluster.nodes)
    s = 0
    for img in pod.images:
        size = node.images.get(img)
        if size is not None:
            s += int(size * (spread.get(img, 0) / total_nodes))
    hi = MAX_IMAGE_PER_CONTAINER * max(len(pod.images), 1)
    s = min(max(s, MIN_IMAGE), hi)
    return MAX_NODE_SCORE * (s - MIN_IMAGE) // (hi - MIN_IMAGE)


def score(pod: KPod, feasible: list[str], cluster: Cluster) -> dict[str, int]:
    """Weighted, normalised total per feasible node."""
    total = dict.fromkeys(feasible, 0)
    if not feasible:
        return total
    # TaintToleration (reversed count of intolerable PreferNoSchedule taints)
    prefer = [t for t in pod.spec.tolerations if not t.get("effect") or t["effect"] == "PreferNoSchedule"]
    tt = {n: sum(1 for t in cluster.taints(n) if t.get("effect") == "PreferNoSchedule"
                 and not any(tolerates(o, t) for o in prefer)) for n in feasible}
    for n, v in _default_normalize(tt, True).items():
        total[n] += v * WEIGHTS["TaintToleration"]
    # NodeAffinity (preferred terms; skipped without any)
    if pod.spec.node_preferred:
        na = {n: sum(t["weight"] for t in pod.spec.node_preferred
                     if node_term_matches(t["term"], cluster.nodes[n])) for n in feasible}
        for n, v in _default_normalize(na, False).items():
            total[n] += v * WEIGHTS["NodeAffinity"]
    # PodTopologySpread
    sp = _spread_scores(pod, feasible, cluster)
    if sp is not None:
        for n, v in sp.items():
            total[n] += v * WEIGHTS["PodTopologySpread"]
    # InterPodAffinity
    ip = _interpod_scores(pod, feasible, cluster)
    if ip is not None:
        for n, v in ip.items():
            total[n] += v * WEIGHTS["InterPodAffinity"]
    # NodeResourcesFit, BalancedAllocation (skipped for a BestEffort pod)
    best_effort = pod.cpu == 0 and pod.mem == 0
    spread_count: dict[str, int] = {}
    for n in cluster.nodes.values():
        for img in n.images:
            spread_count[img] = spread_count.get(img, 0) + 1
    for n in feasible:
        node = cluster.nodes[n]
        total[n] += _fit_score(pod, node, cluster) * WEIGHTS["NodeResourcesFit"]
        if not best_effort:
            total[n] += _balanced_score(pod, node, cluster) * WEIGHTS["NodeResourcesBalancedAllocation"]
        total[n] += _image_score(pod, node, cluster, spread_count) * WEIGHTS["ImageLocality"]
    return total


def schedule(pod: KPod, cluster: Cluster, candidates: list[str] | None = None,
             static: dict[str, bool] | None = None) -> str | None:
    """Where kube-scheduler binds ``pod``: the best-scoring feasible node."""
    names = cluster.order if candidates is None else candidates
    feasible = [n for n in names if fits(pod, n, cluster, static=(static or {}).get(n))]
    if not feasible:
        return None
    totals = score(pod, feasible, cluster)
    best = max(totals.values())
    return next(n for n in feasible if totals[n] == best)


# -- building from collected facts ---------------------------------------------------------------

def pod_requests(containers: list[dict], init: list[dict], overhead: dict, *, nonzero: bool
                 ) -> tuple[int, int, int, int]:
    """``resourcehelper.PodRequests``: containers summed, sidecars added, the
    largest init container as a floor, plus overhead. ``nonzero`` substitutes
    100m / 200Mi for a container that sets no request (scoring only)."""
    def req(c: dict) -> list[int]:
        cpu, mem = c.get("cpu"), c.get("mem")
        if nonzero:
            cpu = DEFAULT_MILLI_CPU if cpu is None else cpu
            mem = DEFAULT_MEMORY if mem is None else mem
        return [cpu or 0, mem or 0, c.get("gpu") or 0, c.get("eph") or 0]

    total = [0, 0, 0, 0]
    for c in containers:
        total = [a + b for a, b in zip(total, req(c), strict=True)]
    sidecars = [0, 0, 0, 0]
    init_max = [0, 0, 0, 0]
    for c in init:
        r = req(c)
        if c.get("sidecar"):
            total = [a + b for a, b in zip(total, r, strict=True)]
            sidecars = [a + b for a, b in zip(sidecars, r, strict=True)]
            r = list(sidecars)
        else:
            r = [a + b for a, b in zip(r, sidecars, strict=True)]
        init_max = [max(a, b) for a, b in zip(init_max, r, strict=True)]
    total = [max(a, b) for a, b in zip(total, init_max, strict=True)]
    total[0] += (overhead or {}).get("cpu", 0)
    total[1] += (overhead or {}).get("mem", 0)
    return total[0], total[1], total[2], total[3]


def default_selector(p: dict, kube: dict) -> dict | None:
    """``helper.DefaultSelector``: the labels of every Service selecting the pod,
    AND the owner's selector. None when that is empty."""
    labels = p.get("labels") or {}
    merged: dict[str, str] = {}
    for s in kube.get("services") or []:
        if s["ns"] == p["ns"] and all(labels.get(k) == v for k, v in s["selector"].items()):
            merged.update(s["selector"])
    exprs: list[dict] = []
    owner = (kube.get("owners") or {}).get(f"{p['ns']}/{p.get('owner_kind')}/{p.get('owner_name')}")
    if owner:
        merged.update(owner.get("matchLabels") or {})
        exprs = list(owner.get("matchExpressions") or [])
    if not merged and not exprs:
        return None
    return {"matchLabels": merged, "matchExpressions": exprs}


def cluster_from_kube(kube: dict[str, Any], *, recommended: dict | None = None,
                      workload_of=None) -> tuple[list[KNode], list[KPod]]:
    """KNodes and KPods from :func:`rightsizer.sources.kfacts.collect_kube` output.

    ``recommended`` maps (namespace, workload, container) to (cpu millicores or
    None, memory bytes or None): a container it names takes those requests,
    which is how an option's request changes reach the replay -- per container,
    so a container that gains a request also loses the scheduler's stand-in.
    ``workload_of(pod) -> (name, kind)`` maps a pod to its workload.
    """
    nodes = []
    for n in kube.get("nodes") or []:
        images: dict[str, int] = {}
        for img in n.get("images") or []:
            for name in img.get("names") or []:
                images[normalized_image(name)] = img.get("size", 0)
        a = n.get("alloc") or {}
        limits = {d: c for d, c in ((kube.get("csinodes") or {}).get(n["name"]) or {}).items() if c is not None}
        nodes.append(KNode(
            name=n["name"], labels=n.get("labels") or {}, taints=list(n.get("taints") or []),
            cpu=a.get("cpu", 0), mem=a.get("mem", 0), pods=a.get("pods", 110), gpu=a.get("gpu", 0),
            eph=a.get("eph", 0), unschedulable=bool(n.get("unschedulable")), ready=bool(n.get("ready", True)),
            scale_down_disabled=bool(n.get("scale_down_disabled")),
            group=node_group_of(n.get("labels")),
            images=images, csi_limits=limits))
    pvcs, pvs = kube.get("pvcs") or {}, kube.get("pvs") or {}
    specs: dict[str, PodSpec] = {}
    pods = []
    for p in kube.get("pods") or []:
        if not p.get("node"):
            continue
        wname, wkind = workload_of(p) if workload_of else (p.get("owner_name", ""), p.get("owner_kind", ""))
        wkey = f"{p['ns']}/{wname}/{wkind}" if wname else ""
        containers = [dict(c) for c in p.get("containers") or []]
        if recommended:
            for c in containers:
                rec = recommended.get((p["ns"], wname, c.get("name", "")))
                if rec:
                    if rec[0] is not None:
                        c["cpu"] = rec[0]
                    if rec[1] is not None:
                        c["mem"] = rec[1]
        cpu, mem, gpu, eph = pod_requests(containers, p.get("init") or [], p.get("overhead") or {},
                                          nonzero=False)
        nz_cpu, nz_mem, _, _ = pod_requests(containers, p.get("init") or [], p.get("overhead") or {},
                                            nonzero=True)
        sig = repr((p["ns"], p.get("owner_kind"), p.get("owner_name"), p.get("node_selector"),
                    p.get("node_required"), p.get("node_preferred"), p.get("tolerations"),
                    p.get("pod_affinity"), p.get("pod_anti"), p.get("pod_affinity_pref"),
                    p.get("pod_anti_pref"), p.get("spread")))
        spec = specs.get(sig)
        if spec is None:
            spec = specs[sig] = PodSpec(
                node_selector=p.get("node_selector") or {}, node_required=p.get("node_required") or [],
                node_preferred=p.get("node_preferred") or [], tolerations=p.get("tolerations") or [],
                pod_affinity=p.get("pod_affinity") or [], pod_anti=p.get("pod_anti") or [],
                pod_affinity_pref=p.get("pod_affinity_pref") or [],
                pod_anti_pref=p.get("pod_anti_pref") or [], spread=p.get("spread") or [],
                default_selector=default_selector(p, kube))
        csi, terms = [], []
        for v in p.get("volumes") or []:
            if v.get("kind") != "pvc":
                continue
            pv = pvs.get((pvcs.get(f"{p['ns']}/{v.get('claim', '')}") or {}).get("volume", ""))
            if pv:
                if pv.get("driver"):
                    csi.append((pv["driver"], pv["handle"]))
                terms.append(tuple(pv.get("node_terms") or ()))
        exempt = {x.strip() for x in (p.get("safe_local_volumes") or "").split(",") if x.strip()}
        local = tuple(v["name"] for v in p.get("volumes") or []
                      if (v.get("kind") == "hostPath" or (v.get("kind") == "emptyDir" and v.get("medium") != "Memory"))
                      and v["name"] not in exempt)
        images = tuple(normalized_image(c.get("image", "")) for c in (p.get("init") or []) + containers)
        pods.append(KPod(
            ns=p["ns"], name=p["name"], node=p["node"], spec=spec, owner_kind=p.get("owner_kind", ""),
            owner_name=p.get("owner_name", ""), wkey=wkey, labels=p.get("labels") or {},
            created=p.get("created"), deleting=p.get("deleting"), grace=p.get("grace"),
            priority=p.get("priority"), safe_to_evict=p.get("safe_to_evict"), local_volumes=local,
            do_not_disrupt=bool(p.get("do_not_disrupt")),
            cpu=cpu, mem=mem, gpu=gpu, eph=eph, nz_cpu=nz_cpu, nz_mem=nz_mem, images=images,
            ports=tuple((pt["port"], pt["proto"], pt["ip"]) for c in containers for pt in c.get("ports") or []),
            csi=tuple(csi), pv_terms=tuple(terms), n_containers=len(containers)))
    return nodes, pods
