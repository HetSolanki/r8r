"""Karpenter, replayed over the live cluster for one NodePool.

Nobody sets a NodePool's node count or picks its node sizes; Karpenter does,
from the pods' requests and the sizes the pool lists. So an option on a pool
is not "N nodes of type X" but "these requests, these listed sizes -- and
here is the fleet Karpenter arrives at". This module computes that fleet the
way Karpenter v1.14 (sigs.k8s.io/karpenter, as Karpenter Provider for OCI
vendors it) does, in two passes over the scheduler port
(:mod:`rightsizer.engine.kscheduler`):

**Two schedulers, as in a live cluster.** kube-scheduler binds pending pods to
existing nodes (:func:`rightsizer.engine.kscheduler.schedule`); Karpenter only
sees the pods it cannot place, and launches NodeClaims for them. Consolidation
is Karpenter's alone: it simulates moving a candidate's pods with its *own*
scheduler (``pkg/controllers/provisioning/scheduling``), which differs from
kube-scheduler in the ways that decide whether a node can go:

* **Preferences are requirements until relaxed** (scheduler.go trySchedule,
  preferences.go Relax). The heaviest preferred node affinity, every preferred
  pod affinity and anti-affinity term, and every ScheduleAnyway topology
  spread are enforced. Only when a pod fits nowhere -- existing nodes,
  in-flight claims *and* a new claim -- is one relaxed, in this order: pod
  affinity, pod anti-affinity, node affinity, spread; then it is tried again.
  A new node satisfies any hostname-level rule, so in practice a hostname
  preference is never relaxed: Karpenter launches a node rather than break it.
* **Hostname spread assumes a global minimum of zero** (topologygroup.go
  nextDomainTopologySpread): Karpenter can always create a new hostname, so a
  node may hold at most ``maxSkew`` matching pods, whatever the other nodes
  hold. ``maxSkew: 1`` means one replica per node.
* **First fit, not best score**: existing nodes are tried in name order
  (initialized first) and the first that takes the pod wins; then in-flight
  claims, fewest pods first; then a new claim (scheduler.go add).
* **Only the incoming pod's preferences count**: existing pods' *preferred*
  anti-affinity does not repel it (topology.go updateInverseAntiAffinity);
  their required anti-affinity does.

A claim launches as the cheapest size left to it (the OCI provider orders a
claim's options by price), in the zone its pods' volumes require, else where
the pool has fewest nodes. ``limits.cpu`` and ``limits.memory`` cap launches.

**Consolidation** (``pkg/controllers/disruption``): with
``WhenEmptyOrUnderutilized``, repeatedly -- delete empty nodes; then the
largest batch of candidates (binary search, up to 100) whose pods reschedule
onto the other nodes with at most one new node cheaper than the batch; then
single nodes, delete first, else replace with one strictly cheaper node.
Candidates are taken in descending savings ratio, price over the cost of
rescheduling their pods (types.go SavingsRatio). A node is not a candidate
while any pod on it carries ``karpenter.sh/do-not-disrupt`` or has no
PodDisruptionBudget allowance left; a pod with no controller does not block
(Karpenter evicts it). Spot-to-spot replacement of a single node needs fifteen
cheaper sizes, which a pool with a short ladder never has. ``WhenEmpty``
deletes empty nodes only.

**Disruption budgets** (``spec.disruption.budgets``, default ``nodes: 10%``):
``GetAllowedDisruptions`` rounds a percentage *up* over the pool's node count,
so a small pool allows one node at a time. Multi-node consolidation takes at
most that many candidates and needs two; single-node needs one; ``nodes: 0``
stops consolidation. Budgets with a schedule are out of their window for most
of the day and are left out: the steady state is the unscheduled budgets'.

**What a replay means.** Karpenter consolidates continuously, so the fleet
running today is already where its rules stop. A replay of today with nothing
edited that ends elsewhere has found a gap in this model, not a saving: the
caller measures every option against that replay, not against today.

What is not modelled: ``consolidateAfter`` (the fleet here is the steady
state once it has elapsed), drift, expiry, pod deletion cost, and the
price-capacity-optimised spot choice (spot launches the cheapest size, as
on-demand does).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, replace

from rightsizer.engine.kscheduler import Cluster, KNode, KPod, fits, schedule, static_ok

log = logging.getLogger(__name__)

GIB = 1024**3
MAX_PARALLEL_CANDIDATES = 100
MIN_SPOT_TO_SPOT_TYPES = 15
INSTANCE_TYPE_LABEL = "node.kubernetes.io/instance-type"
CAPACITY_TYPE_LABEL = "karpenter.sh/capacity-type"


@dataclass(frozen=True)
class Size:
    """One size a pool may launch, with what a node of it offers pods."""
    name: str
    vcpu: float
    alloc_cpu_m: int
    alloc_mem: int
    monthly_usd: float
    #: The label value Karpenter writes on a node of this size.
    label: str = ""
    #: Node capacity in bytes, what ``limits.memory`` counts; 0 for unknown.
    mem_bytes: int = 0


@dataclass
class Pool:
    name: str
    sizes: list[Size]
    policy: str = "WhenEmptyOrUnderutilized"
    spot: bool = False
    #: ``spec.limits.cpu`` in vCPU and ``limits.memory`` in bytes; None for no ceiling.
    limit_vcpu: float | None = None
    limit_mem: int | None = None
    #: Workload keys annotated karpenter.sh/do-not-disrupt.
    undisruptable: frozenset = frozenset()
    #: ``spec.disruption.budgets[].nodes`` of the budgets without a schedule.
    budgets: tuple[str, ...] = ("10%",)

    def allowed_disruptions(self, nodes: int) -> int:
        """nodepool.go GetAllowedDisruptions: the least of the budgets, a
        percentage rounded up over the pool's node count."""
        allowed = []
        for b in self.budgets:
            text = str(b).strip()
            try:
                if text.endswith("%"):
                    allowed.append(math.ceil(nodes * float(text[:-1]) / 100))
                else:
                    allowed.append(int(text))
            except ValueError:
                continue
        return min(allowed) if allowed else nodes


@dataclass
class PoolOutcome:
    nodes: int
    #: size name -> node count: the fleet Karpenter arrives at.
    fleet: dict[str, int]
    placement: dict[str, list[tuple[str, float, float, bool]]]
    unplaceable: int = 0
    monthly_usd: float = 0.0
    #: Why nodes that could have gone stayed, node -> reason.
    held: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


HOSTNAME = "kubernetes.io/hostname"


def _value(node: KNode, key: str) -> str | None:
    """The node's domain for a topology key; a node's hostname is its name."""
    if key == HOSTNAME:
        return node.labels.get(HOSTNAME) or node.name
    return node.labels.get(key)


@dataclass
class _Prefs:
    """A pod's preferences, enforced as requirements until relaxed (preferences.go)."""
    pod_aff: list
    pod_anti: list
    node_pref: list
    spread: list

    @classmethod
    def of(cls, pod: KPod) -> _Prefs:
        def heaviest_first(terms):
            return sorted(terms or [], key=lambda t: -int(t.get("weight", 0)))
        return cls(
            [t["term"] for t in heaviest_first(pod.spec.pod_affinity_pref)],
            [t["term"] for t in heaviest_first(pod.spec.pod_anti_pref)],
            # requirements.go newPodRequirements: only the heaviest node preference.
            [t["term"] for t in heaviest_first(pod.spec.node_preferred)][:1],
            [c for c in pod.spec.spread if c.get("whenUnsatisfiable") == "ScheduleAnyway"],
        )

    def any(self) -> bool:
        return bool(self.pod_aff or self.pod_anti or self.node_pref or self.spread)

    def kinds(self) -> list[str]:
        return [k for k, v in (("preferred pod affinity", self.pod_aff),
                               ("preferred pod anti-affinity", self.pod_anti),
                               ("preferred node affinity", self.node_pref),
                               ("ScheduleAnyway topology spread", self.spread)) if v]

    def relax(self) -> bool:
        """Drop the next preference, in preferences.go Relax order."""
        for terms in (self.pod_aff, self.pod_anti, self.node_pref, self.spread):
            if terms:
                terms.pop(0)
                return True
        return False


class _Claim:
    """An in-flight NodeClaim: its pods, the sizes still able to hold them, and
    a stand-in node carrying its labels for the topology checks."""

    def __init__(self, options: list[Size], zone: str, probe: KNode) -> None:
        self.pods: list[KPod] = []
        self.options = list(options)
        self.zone = zone
        self.probe = probe


def _pod_order(p: KPod) -> tuple:
    # queue.go byCPUAndMemoryDescending: cpu desc, memory desc, created, name.
    return (-p.cpu, -p.mem, p.created or 0, p.name)


def _required_zone(p: KPod) -> str:
    for terms in p.pv_terms:
        for t in terms:
            for e in t.get("matchExpressions") or []:
                if e.get("key", "").endswith("/zone") and e.get("operator") == "In" and e.get("values"):
                    return e["values"][0]
    return ""


class _Run:
    """One pass of Karpenter's scheduler: existing nodes, then in-flight claims,
    then a new claim, each with the pod's preferences enforced until relaxed."""

    def __init__(self, sim: PoolSimulation, cl: Cluster, existing: list[str]) -> None:
        self.sim = sim
        self.cl = cl
        self.existing = existing
        self.claims: list[_Claim] = []

    def _places(self):
        for n in self.cl.order:
            yield self.cl.nodes[n], self.cl.on[n]
        for c in self.claims:
            yield c.probe, c.pods

    def _pods_in(self, key: str, value: str, node: KNode, here: list[KPod]):
        if key == HOSTNAME:
            return here
        return [q for n, pods in self._places() if _value(n, key) == value for q in pods]

    def soft_ok(self, pod: KPod, prefs: _Prefs, node: KNode, here: list[KPod]) -> bool:
        """The preferences still held, and every spread the way Karpenter counts it."""
        from rightsizer.engine.kscheduler import (
            _count, _included, _spread_selector, label_selector_matches, node_term_matches, term_matches)

        ns = self.cl.namespaces
        if prefs.node_pref and not node_term_matches(prefs.node_pref[0], node):
            return False
        for term in prefs.pod_anti:
            key = term.get("topologyKey", "")
            v = _value(node, key)
            if v is not None and any(q is not pod and q.deleting is None and term_matches(term, pod.ns, q, ns)
                                     for q in self._pods_in(key, v, node, here)):
                return False
        for term in prefs.pod_aff:
            key = term.get("topologyKey", "")
            v = _value(node, key)
            anywhere = any(q is not pod and term_matches(term, pod.ns, q, ns)
                           for _, pods in self._places() for q in pods)
            if anywhere and (v is None or not any(q is not pod and term_matches(term, pod.ns, q, ns)
                                                  for q in self._pods_in(key, v, node, here))):
                return False
        hard = [c for c in pod.spec.spread if c.get("whenUnsatisfiable", "DoNotSchedule") == "DoNotSchedule"]
        for c in prefs.spread + hard:
            key = c.get("topologyKey", "")
            v = _value(node, key)
            if v is None:
                return False
            sel = _spread_selector(c, pod)
            me = 1 if label_selector_matches(sel, pod.labels) else 0
            count = _count([q for q in self._pods_in(key, v, node, here) if q is not pod], sel, pod.ns)
            if key == HOSTNAME:
                # A new hostname can always be made: the global minimum is zero.
                if count + me > int(c.get("maxSkew", 1)):
                    return False
                continue
            counts: dict[str, int] = {}
            for n, pods in self._places():
                d = _value(n, key)
                if d is None or not _included(c, pod, n):
                    continue
                counts[d] = counts.get(d, 0) + _count([q for q in pods if q is not pod], sel, pod.ns)
            if count + me - (min(counts.values()) if counts else 0) > int(c.get("maxSkew", 1)):
                return False
        return True

    def _committed(self, except_claim: _Claim | None) -> float:
        return sum(min(s.vcpu for s in c.options) for c in self.claims if c is not except_claim and c.options)

    def place(self, pod: KPod) -> bool:
        """scheduler.go trySchedule: add, and relax one preference after each failure."""
        prefs = _Prefs.of(pod)
        while True:
            for n in self.existing:
                if fits(pod, n, self.cl) and self.soft_ok(pod, prefs, self.cl.nodes[n], self.cl.on[n]):
                    self.cl.place(pod, n)
                    return True
            for claim in sorted(self.claims, key=lambda c: len(c.pods)):
                options = self.sim._can_add(claim, pod, self.cl, self._committed(claim))
                if options and self.soft_ok(pod, prefs, claim.probe, claim.pods):
                    claim.pods.append(pod)
                    claim.options = options
                    claim.zone = claim.zone or _required_zone(pod)
                    return True
            claim = self.sim.new_claim(_required_zone(pod), self.cl)
            options = self.sim._can_add(claim, pod, self.cl, self._committed(None))
            if options and self.soft_ok(pod, prefs, claim.probe, []):
                claim.pods.append(pod)
                claim.options = options
                self.claims.append(claim)
                return True
            if not prefs.relax():
                return False

    def schedule_all(self, pods: list[KPod]) -> list[KPod]:
        """scheduler.go Solve: largest first; a pod that fails is retried while
        others still land, which settles affinity between pods of one batch."""
        queue, left = sorted(pods, key=_pod_order), []
        while queue:
            failed = [p for p in queue if not self.place(p)]
            if len(failed) == len(queue):
                left = failed
                break
            queue = failed
        return left


class PoolSimulation:
    def __init__(self, cluster: Cluster, group_of: dict[str, str], pool: Pool, template: KNode,
                 daemons: list[KPod], budgets: list, now: float | None,
                 size_of_node, zone_of, in_zone, launch_zone, zone_ids: dict[str, str]) -> None:
        self.cl = cluster
        self.group_of = group_of
        self.pool = pool
        self.template = template
        self.daemons = daemons
        self.budgets = budgets
        self.now = now
        self.size_of_node = size_of_node      # KNode -> Size | None
        self.zone_of = zone_of
        self.in_zone = in_zone
        self.launch_zone = launch_zone
        self.zone_ids = zone_ids
        self.launched = 0
        self.probes = 0
        self.held: dict[str, str] = {}

    # -- the pool's nodes ---------------------------------------------------------

    def nodes(self, cl: Cluster | None = None) -> list[str]:
        cl = cl or self.cl
        return [n for n in cl.order if self.group_of.get(n) == self.pool.name]

    def price(self, node: str, cl: Cluster | None = None) -> float:
        s = self.size_of_node((cl or self.cl).nodes[node])
        return s.monthly_usd if s else 0.0

    def vcpu_in_use(self, cl: Cluster) -> float:
        return sum((self.size_of_node(cl.nodes[n]) or Size("", 0, 0, 0, 0)).vcpu for n in self.nodes(cl))

    def mem_in_use(self, cl: Cluster) -> int:
        return sum((self.size_of_node(cl.nodes[n]) or Size("", 0, 0, 0, 0)).mem_bytes for n in self.nodes(cl))

    def node_for(self, size: Size, zone: str, cl: Cluster) -> KNode:
        self.launched += 1
        name = f"({size.name} {self.launched})"
        labels = {**self.template.labels, INSTANCE_TYPE_LABEL: size.label or size.name,
                  "beta.kubernetes.io/instance-type": size.label or size.name,
                  HOSTNAME: name}
        if self.pool.spot:
            labels[CAPACITY_TYPE_LABEL] = "spot"
        n = replace(self.template, name=name, labels=labels, cpu=size.alloc_cpu_m, mem=size.alloc_mem,
                    images={}, unschedulable=False, scale_down_disabled=False)
        return self.in_zone(n, zone or self.launch_zone(cl, self.group_of, self.pool.name), self.zone_ids)

    def new_claim(self, zone: str, cl: Cluster) -> _Claim:
        """A claim not yet launched, with a stand-in node for its labels."""
        self.probes += 1
        name = f"(claim {self.probes})"
        probe = replace(self.template, name=name, labels={**self.template.labels, HOSTNAME: name})
        probe = self.in_zone(probe, zone or self.launch_zone(cl, self.group_of, self.pool.name), self.zone_ids)
        return _Claim(self.pool.sizes, zone, probe)

    # -- provisioning ------------------------------------------------------------

    def _daemon_overhead(self) -> tuple[int, int]:
        return sum(d.cpu for d in self.daemons), sum(d.mem for d in self.daemons)

    def _can_add(self, claim: _Claim, pod: KPod, cl: Cluster, inflight_vcpu: float = 0.0) -> list[Size] | None:
        """nodeclaim.go CanAdd: taints, affinity and volume zone against the
        pool's node, then the sizes that still hold every pod with the
        DaemonSets' overhead, under the pool's CPU and memory ceilings."""
        zone = _required_zone(pod)
        if claim.zone and zone and zone != claim.zone:
            return None
        probe = self.in_zone(self.template, zone or claim.zone, self.zone_ids)
        if not static_ok(pod, probe):
            return None
        pods = claim.pods + [pod]
        # Required hostname anti-affinity among the claim's own pods: one per node.
        for q in claim.pods:
            if _anti_hostname(pod, q) or _anti_hostname(q, pod):
                return None
        dc, dm = self._daemon_overhead()
        cpu = sum(p.cpu for p in pods) + dc
        mem = sum(p.mem for p in pods) + dm
        count = len(pods) + len(self.daemons)
        options = [s for s in claim.options
                   if cpu <= s.alloc_cpu_m and mem <= s.alloc_mem and count <= self.template.pods]
        if self.pool.limit_vcpu is not None:
            # filterByRemainingResources: what runs plus what the other claims
            # will launch at the least, against the pool's ceiling.
            used = self.vcpu_in_use(cl) + inflight_vcpu
            options = [s for s in options if used + s.vcpu <= self.pool.limit_vcpu + 1e-9]
        if self.pool.limit_mem is not None:
            used_mem = self.mem_in_use(cl)
            options = [s for s in options if not s.mem_bytes or used_mem + s.mem_bytes <= self.pool.limit_mem]
        return options or None

    def _launch(self, claims: list[_Claim], cl: Cluster) -> tuple[list[str], list[KPod]]:
        new: list[str] = []
        left: list[KPod] = []
        for claim in claims:
            size = min(claim.options, key=lambda s: (s.monthly_usd, s.vcpu, s.name))
            node = self.node_for(size, claim.zone, cl)
            cl.add_node(node)
            self.group_of[node.name] = self.pool.name
            new.append(node.name)
            for d in self.daemons:
                cl.place(replace(d, node=node.name), node.name)
            for p in claim.pods:
                if fits(p, node.name, cl):
                    cl.place(p, node.name)
                else:
                    left.append(p)
        return new, left

    def provision(self, pending: list[KPod], cl: Cluster, only: list[str] | None = None) -> tuple[list[str], list[KPod]]:
        """Pending pods: kube-scheduler binds what an existing node takes; Karpenter
        launches claims for the rest. Returns (new node names, pods nothing took)."""
        run = _Run(self, cl, existing=[])
        rest: list[KPod] = []
        for pod in sorted(pending, key=_pod_order):
            target = schedule(pod, cl, candidates=only)
            if target is not None:
                cl.place(pod, target)
            else:
                rest.append(pod)
        left = run.schedule_all(rest)
        new, more = self._launch(run.claims, cl)
        return new, left + more

    # -- consolidation ------------------------------------------------------------

    def _reschedulable(self, node: str, cl: Cluster) -> list[KPod]:
        return [p for p in cl.on[node] if not (p.is_daemonset or p.is_mirror)]

    def _disruptable(self, node: str, cl: Cluster) -> str:
        """"" when Karpenter may disrupt this node, else why not
        (statenode.go ValidatePodsDisruptable)."""
        from rightsizer.engine.careplay import matching_budgets

        for p in cl.on[node]:
            # Every pod counts here, DaemonSet and mirror pods included.
            if p.do_not_disrupt or p.wkey in self.pool.undisruptable:
                return f"{p.subject} is do-not-disrupt"
        left = {(b.ns, b.name): b.allowed for b in self.budgets}
        for p in self._reschedulable(node, cl):
            for b in matching_budgets(p, self.budgets):
                a = left.get((b.ns, b.name))
                if a is not None and a < 1:
                    return f"PDB {b.ns}/{b.name} allows no disruption"
        return ""

    def _savings_ratio(self, node: str, cl: Cluster) -> float:
        # types.go SavingsRatio: price over (1 + one eviction per reschedulable pod).
        return self.price(node, cl) / (1.0 + len(self._reschedulable(node, cl)))

    def _why_new_node(self, run: _Run, sim: Cluster) -> str:
        """Which pod forced a new node, and whether its own preferences are why."""
        for claim in run.claims:
            for p in claim.pods:
                prefs = _Prefs.of(p)
                if not prefs.any():
                    return f"no room on the other nodes for {p.subject}"
                if any(fits(p, n, sim) for n in run.existing):
                    return (f"{p.subject}: its {' and '.join(prefs.kinds())} keep it off the other "
                            "nodes, and Karpenter treats preferences as required")
                return f"no room on the other nodes for {p.subject}"
        return ""

    def _try(self, cands: list[str], cl: Cluster) -> tuple[tuple[Cluster, list[str]] | None, str]:
        """consolidation.go computeConsolidation on `cands`: their pods onto the
        other nodes with Karpenter's scheduler, at most one new node, cheaper
        than what goes. Returns (result or None, why not)."""
        sim = cl.fork()
        group_of = dict(self.group_of)
        pending: list[KPod] = []
        for n in cands:
            pending += [replace(p, node="") for p in self._reschedulable(n, sim)]
            sim.remove_node(n)
            group_of.pop(n, None)
        saved = self.group_of, self.launched
        self.group_of = group_of
        run = _Run(self, sim, existing=list(sim.order))
        left = run.schedule_all(pending)
        why = self._why_new_node(run, sim)
        if left:
            self.group_of, self.launched = saved
            return None, f"{left[0].subject} fits nowhere else"
        if len(run.claims) > 1:
            self.group_of, self.launched = saved
            return None, f"can't remove without {len(run.claims)} new nodes ({why})"
        new, more = self._launch(run.claims, sim)
        self.group_of = saved[0]
        if more:
            self.launched = saved[1]
            return None, f"{more[0].subject} fits nowhere else"
        if new:
            price_new = self.price(new[0], sim)
            price_old = sum(self.price(n, cl) for n in cands)
            # filterOutSameInstanceType: not the same size at the same price.
            same = any(self.size_of_node(cl.nodes[n]) == self.size_of_node(sim.nodes[new[0]]) for n in cands)
            if price_new >= price_old or (same and len(cands) == 1):
                self.launched = saved[1]
                return None, f"can't replace with a cheaper node ({why})"
            if self.pool.spot and len(cands) == 1:
                cheaper = [s for s in self.pool.sizes if s.monthly_usd < price_old]
                if len(cheaper) < MIN_SPOT_TO_SPOT_TYPES:
                    self.launched = saved[1]
                    return None, "spot-to-spot replacement needs 15 cheaper sizes"
            self.group_of[new[0]] = self.pool.name
        return (sim, new), ""

    def _apply(self, sim: Cluster, cands: list[str]) -> None:
        self.cl = sim
        for n in cands:
            self.group_of.pop(n, None)
            self.held.pop(n, None)

    def consolidate(self) -> None:
        if self.pool.policy not in ("WhenEmptyOrUnderutilized", "WhenUnderutilized", "WhenEmpty"):
            return
        for _ in range(500):
            cl = self.cl
            # Emptiness.
            empty = [n for n in self.nodes() if not self._reschedulable(n, cl) and not self._disruptable(n, cl)]
            if empty:
                for n in empty:
                    cl.remove_node(n)
                    self.group_of.pop(n, None)
                    self.held.pop(n, None)
                continue
            if self.pool.policy == "WhenEmpty":
                break
            allowed = self.pool.allowed_disruptions(len(self.nodes()))
            if allowed < 1:
                for n in self.nodes():
                    self.held[n] = f"the pool's disruption budget ({', '.join(self.pool.budgets)}) allows no node"
                break
            cands = []
            for n in self.nodes():
                why = self._disruptable(n, cl)
                if why:
                    self.held[n] = why
                else:
                    cands.append(n)
            # Highest savings per unit of disruption first (consolidation.go sortCandidates).
            cands.sort(key=lambda n: (-self._savings_ratio(n, cl), n))
            # Multi-node: the largest batch that works (multinodeconsolidation.go,
            # binary search) among as many candidates as the budget allows at once.
            lo, hi, best = 2, min(len(cands), allowed, MAX_PARALLEL_CANDIDATES), None
            while lo <= hi:
                mid = (lo + hi) // 2
                r, _why = self._try(cands[:mid], cl)
                if r is not None:
                    best, lo = (cands[:mid], r), mid + 1
                else:
                    hi = mid - 1
            if best is not None:
                self._apply(best[1][0], best[0])
                continue
            # Single-node.
            done = False
            for n in cands:
                r, why = self._try([n], cl)
                if r is not None:
                    self._apply(r[0], [n])
                    done = True
                    break
                # Only a reason someone can act on is kept: a node that stays
                # because the others are full is simply needed.
                if "keep it off" in why or "cheaper" in why:
                    self.held[n] = why
            if not done:
                break


def _anti_hostname(a: KPod, b: KPod) -> bool:
    from rightsizer.engine.kscheduler import label_selector_matches

    for term in a.spec.pod_anti or []:
        if term.get("topologyKey") != HOSTNAME:
            continue
        if label_selector_matches(term.get("labelSelector"), b.labels) and \
                (not term.get("namespaces") or b.ns in term["namespaces"] or b.ns == a.ns):
            return True
    return False


def simulate_pool(snapshot, pool: Pool, *, requests: dict, keep: dict, size_of_node,
                  template_eph: int | None = None) -> PoolOutcome | None:
    """The fleet Karpenter arrives at for ``pool`` after the edits.

    ``size_of_node`` maps a node (its labels) to one of the pool's sizes, or
    None for a node of a size the pool no longer lists (it is priced by its
    own size where that is known).
    """
    from rightsizer.engine import realistic as R
    from rightsizer.engine.careplay import budgets_from_kube, in_zone, launch_zone, zone_ids, zone_of

    base = R._kube_state(snapshot, requests, keep)
    st = R._fork_state(base)
    cl = st.cluster
    mine = [n for n in cl.order if st.group_of.get(n) == pool.name]
    if not mine:
        return None
    tmpl = cl.nodes[mine[0]]
    if template_eph is not None:
        tmpl = replace(tmpl, eph=template_eph)
    daemons = [replace(p) for p in cl.on[mine[0]] if p.is_daemonset]
    budgets = budgets_from_kube(snapshot.facts.kube, [p for n in cl.order for p in cl.on[n]])
    ids = zone_ids(cl)

    def launch(cluster, group_of, group):
        from rightsizer.engine.careplay import Group
        info = st.groups.get(group) or Group(name=group)
        return launch_zone(cluster, group_of, group, info)

    sim = PoolSimulation(cl, st.group_of, pool, tmpl, daemons, budgets, st.now,
                         size_of_node, zone_of, in_zone, launch, ids)
    # Pods the rollout left Pending on this pool: Karpenter provisions for them.
    pending = [p for p in st.unplaceable if p.wkey in R.group_workloads(snapshot, pool.name)[0]]
    st.unplaceable = [p for p in st.unplaceable if p not in pending]
    _, left = sim.provision(pending, cl)
    sim.consolidate()
    cl = sim.cl
    names = sim.nodes(cl)
    fleet: dict[str, int] = {}
    cost = 0.0
    for n in names:
        s = size_of_node(cl.nodes[n])
        key = s.name if s else cl.nodes[n].labels.get(INSTANCE_TYPE_LABEL, "?")
        fleet[key] = fleet.get(key, 0) + 1
        cost += s.monthly_usd if s else 0.0
    placement = {n: [(p.wkey, p.cpu / 1000, float(p.mem), p.is_daemonset) for p in cl.on[n]] for n in names}
    notes = []
    if left:
        notes.append(f"{len(left)} pod(s) fit no size the pool lists")
    return PoolOutcome(nodes=len(names), fleet=fleet, placement=placement, unplaceable=len(left),
                       monthly_usd=cost, held={n: r for n, r in sim.held.items() if n in names},
                       notes=notes)
