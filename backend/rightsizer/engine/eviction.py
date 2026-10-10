"""Whether Cluster Autoscaler may evict a pod, and where it could go.

Five independent classes stop CA removing a node. They are gathered here rather
than inside the scale-down loop because each needs different cluster facts, each
degrades differently when a fact is missing, and a node count that ignores any
one of them is optimistic in a way nobody can see:

1. **Bare pods** -- no controller, so nothing would recreate them.
2. **kube-system pods with no PDB** -- ``--skip-nodes-with-system-pods``.
3. **Local storage** -- hostPath, or an emptyDir not backed by memory,
   ``--skip-nodes-with-local-storage``. Explicit ``safe-to-evict: "false"``
   vetoes on its own; ``safe-to-evict: "true"`` overrides every class here.
   Karpenter's ``do-not-disrupt`` is not a Cluster Autoscaler rule; it vetoes
   only when the snapshot's autoscaler is Karpenter (its adapter's ``drain_veto``).
4. **PDB exhausted** -- the workload's budget allows no disruption at all.
5. **Nowhere to reschedule** -- nodeSelector, required node affinity, taints,
   hostname anti-affinity or topology spread leave no node that can take it.

Every check answers "unknown" rather than "fine" when the fact behind it was not
collected, and :attr:`EvictionRules.unknown` lists what was missing, so a run
against a cluster with no API access cannot quietly report the permissive
answer.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field


def _quantity(value: str | None, total: int) -> int | None:
    """A PDB quantity, which is either an integer or a percentage of total."""
    if value is None:
        return None
    text = str(value).strip()
    try:
        if text.endswith("%"):
            pct = float(text[:-1])
            # Kubernetes rounds minAvailable percentages up and maxUnavailable
            # down; the ceiling is the safe reading for a blocker.
            return int(-(-total * pct // 100))
        return int(float(text))
    except ValueError:
        return None


@dataclass(frozen=True)
class Pdb:
    namespace: str
    name: str
    selector: dict
    min_available: str | None = None
    max_unavailable: str | None = None
    has_expressions: bool = False

    def allowed_disruptions(self, healthy: int) -> int | None:
        """How many pods of this set may be down at once. None when unreadable."""
        if self.max_unavailable is not None:
            n = _quantity(self.max_unavailable, healthy)
            return None if n is None else max(0, n)
        if self.min_available is not None:
            n = _quantity(self.min_available, healthy)
            return None if n is None else max(0, healthy - n)
        return None

    def covers(self, labels: dict) -> bool:
        if self.has_expressions or not self.selector:
            return False
        return all(labels.get(k) == v for k, v in self.selector.items())


@dataclass
class EvictionRules:
    """The cluster facts CA consults, indexed for the scale-down loop."""

    #: workload_key -> reason it can never be drained
    drain_blocked: dict[str, str] = field(default_factory=dict)
    #: workload_key -> the PDB governing it, if any
    pdb_for: dict[str, Pdb] = field(default_factory=dict)
    #: workload_key -> total running replicas, for the disruption arithmetic
    replicas: dict[str, int] = field(default_factory=dict)
    #: workload_key -> pod-template labels, so a suggested PDB can carry a
    #: selector that actually matches the pods it is meant to govern.
    labels: dict[str, dict] = field(default_factory=dict)
    #: workload_key -> nodeSelector / affinity / tolerations
    node_selector: dict[str, dict] = field(default_factory=dict)
    node_affinity: dict[str, list] = field(default_factory=dict)
    tolerations: dict[str, list] = field(default_factory=dict)
    #: node -> labels and taints
    node_labels: dict[str, dict] = field(default_factory=dict)
    node_taints: dict[str, list] = field(default_factory=dict)
    antiaffinity: frozenset[str] = frozenset()
    #: ``namespace/pod`` -> the zone its zonal volume (EBS) lives in. The pod
    #: can restart only on a node in that zone, whatever else allows.
    zone_bound: dict[str, str] = field(default_factory=dict)
    #: Facts that were not collected, so the caller can say which checks were
    #: skipped instead of presenting a partial answer as a complete one.
    unknown: tuple[str, ...] = ()
    #: Replay as if every drain blocker were fixed -- annotations corrected,
    #: PDBs added or relaxed, bare pods given a controller. Scheduling
    #: constraints still hold: they are where a pod may go, not whether it may
    #: leave. Prices what the blockers cost; never what CA does today.
    lifted: bool = False
    #: Lift only these blockers -- workload keys, or ``namespace/pod`` for a
    #: pod with no controller -- and keep every other veto. The smallest set
    #: of fixes that still reaches an option's node count is found with this.
    lift: frozenset[str] = frozenset()
    #: Workloads annotated ``safe-to-evict: "true"``: CA drains them whatever
    #: else applies -- no controller, kube-system without a PDB, local storage
    #: or an exhausted PDB (the ``safetoevict`` rule runs before all of those).
    safe_to_evict: frozenset[str] = frozenset()
    #: ``safe-to-evict: "false"``: blocks on its own.
    not_safe_to_evict: frozenset[str] = frozenset()
    #: Local storage as CA counts it (``--skip-nodes-with-local-storage``).
    local_storage: frozenset[str] = frozenset()
    #: Hard topology spread per workload: [{"key", "maxSkew"}].
    hard_spread: dict[str, list] = field(default_factory=dict)
    #: Nodes annotated ``cluster-autoscaler.kubernetes.io/scale-down-disabled``.
    scale_down_disabled: frozenset[str] = frozenset()

    # -- class 1-4: may this pod be evicted at all? ------------------------

    def veto(self, pod, node_pods: list) -> str | None:
        """Why CA would refuse to drain this pod, or None."""
        if pod.is_daemonset:
            return None
        if pod.is_bare:
            return f"{pod.namespace}/{pod.name} has no controller"
        reason = self.drain_blocked.get(pod.workload_key)
        if reason:
            return reason
        return None

    def system_veto(self, pod, labels_of) -> str | None:
        """kube-system pods block unless a PDB governs them."""
        if pod.namespace != "kube-system" or pod.is_daemonset:
            return None
        if self.pdb_for.get(pod.workload_key):
            return None
        # Led by the workload rather than the namespace, so callers grouping on
        # the subject get one entry per controller instead of lumping every
        # kube-system add-on together.
        subject = pod.workload_key or f"kube-system/{pod.name}"
        return f"{subject} has no PDB (kube-system pod {pod.name})"

    def pdb_veto(self, node_pods: list) -> str | None:
        """Would draining this node's pods breach a disruption budget?

        CA drains a whole node at once, so the question is per workload: how
        many of its pods live here, against how many disruptions its PDB allows.
        """
        found = self.pdb_vetoes(node_pods)
        return found[0][1] if found else None

    def pdb_vetoes(self, node_pods: list) -> list[tuple[str, str]]:
        """Every budget that stops this node being drained, as (workload, why).

        Cluster Autoscaler's ``pdb`` rule blocks a pod only when its budget has
        **no** disruption left (``disruptionsAllowed < 1``). A node holding more
        of a workload's pods than the budget allows at once is still removed --
        CA marks it risky and drains it last, and the eviction API lets the
        pods go one at a time as their replacements become ready.
        """
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for pod in node_pods:
            if pod.is_daemonset or pod.workload_key in seen:
                continue
            pdb = self.pdb_for.get(pod.workload_key)
            if pdb is None:
                continue
            allowed = self.pdb_allowed(pdb)
            if allowed is not None and allowed < 1:
                seen.add(pod.workload_key)
                out.append((pod.workload_key, (
                    f"PDB {pdb.namespace}/{pdb.name} allows no disruption "
                    f"({pod.workload_key})")))
        return out

    def pdb_allowed(self, pdb: Pdb) -> int | None:
        """``status.disruptionsAllowed`` for a budget, from its spec and the
        replicas of every workload it covers. None when unreadable."""
        covered = [w for w, p in self.pdb_for.items() if p is pdb]
        healthy = sum(self.replicas.get(w, 0) for w in covered)
        return pdb.allowed_disruptions(healthy)

    # -- class 5: is there anywhere for it to go? --------------------------

    def can_host(self, pod, node: str, present: set) -> bool:
        """Could the scheduler place this pod on this node?"""
        labels = self.node_labels.get(node)
        selector = self.node_selector.get(pod.workload_key) or {}
        if selector:
            if labels is None:
                return False  # unknown labels: do not assume it fits
            if any(labels.get(k) != v for k, v in selector.items()):
                return False
        terms = self.node_affinity.get(pod.workload_key) or []
        if terms:
            if labels is None:
                return False
            if not any(_term_matches(term, labels) for term in terms):
                return False
        taints = self.node_taints.get(node) or []
        if taints and not _tolerates(self.tolerations.get(pod.workload_key) or [], taints):
            return False
        if pod.workload_key in self.antiaffinity and pod.workload_key in present:
            return False
        zone = self.zone_bound.get(f"{pod.namespace}/{pod.name}")
        if zone and (labels or {}).get(ZONE_LABEL) != zone:
            return False  # its volume cannot follow it to another zone
        return True


ZONE_LABEL = "topology.kubernetes.io/zone"


def _term_matches(term: dict, labels: dict) -> bool:
    for key, values in term.items():
        if key not in labels:
            return False
        if values and labels[key] not in values:
            return False
    return True


def _tolerates(tolerations: list, taints: list) -> bool:
    """Every NoSchedule/NoExecute taint must be tolerated."""
    for taint in taints:
        if taint.get("effect") not in ("NoSchedule", "NoExecute"):
            continue
        if not any(_tolerates_one(t, taint) for t in tolerations):
            return False
    return True


def _tolerates_one(tol: dict, taint: dict) -> bool:
    effect = tol.get("effect") or ""
    if effect and effect != taint.get("effect"):
        return False
    key = tol.get("key") or ""
    if not key:
        # An empty key with Exists tolerates every taint.
        return tol.get("operator") == "Exists"
    if key != taint.get("key"):
        return False
    if tol.get("operator") == "Exists":
        return True
    return (tol.get("value") or "") == (taint.get("value") or "")


def rules_from_snapshot(snapshot) -> EvictionRules:
    """Index a snapshot's cluster facts for the scale-down loop.

    Each fact that is absent disables exactly the check that needs it and is
    named in :attr:`EvictionRules.unknown`, because a check that silently
    passes is indistinguishable from one that was never run.
    """
    facts = snapshot.facts
    unknown: list[str] = []

    constraints = list(facts.workload_constraints or [])
    if not constraints:
        unknown.append("workload constraints (local storage, safe-to-evict, "
                       "nodeSelector, tolerations, anti-affinity)")

    from rightsizer.adapters.autoscalers import for_snapshot

    autoscaler, _ = for_snapshot(snapshot)
    drain_blocked: dict[str, str] = {}
    safe: set[str] = set()
    not_safe: set[str] = set()
    local: set[str] = set()
    hard_spread: dict[str, list] = {}
    labels_of: dict[str, dict] = {}
    node_selector: dict[str, dict] = {}
    node_affinity: dict[str, list] = {}
    tolerations: dict[str, list] = {}
    antiaffinity: set[str] = set()
    for w in constraints:
        key = f"{w.get('namespace')}/{w.get('name')}/{w.get('kind')}"
        # Cluster Autoscaler's own annotations, then whatever the autoscaler
        # actually running adds (Karpenter: do-not-disrupt).
        if (veto := autoscaler.drain_veto(w)):
            not_safe.add(key)
            drain_blocked[key] = veto
        elif w.get("safe_to_evict") is True:
            safe.add(key)
        elif w.get("safe_to_evict") is False:
            not_safe.add(key)
            drain_blocked[key] = f"{key} is annotated safe-to-evict=false"
        if w.get("local_storage") and w.get("safe_to_evict") is not True:
            local.add(key)
            drain_blocked.setdefault(
                key, f"{key} uses emptyDir/hostPath without safe-to-evict=true")
        if w.get("hard_spread"):
            hard_spread[key] = list(w["hard_spread"])
        labels_of[key] = dict(w.get("labels") or {})
        if w.get("node_selector"):
            node_selector[key] = dict(w["node_selector"])
        if w.get("node_affinity_terms"):
            node_affinity[key] = list(w["node_affinity_terms"])
        if w.get("tolerations"):
            tolerations[key] = list(w["tolerations"])
        if w.get("hard_antiaffinity_hostname"):
            antiaffinity.add(key)

    # A snapshot collected before these fields existed has constraints but no
    # labels/selectors in them. That is not "no PDB covers anything" -- it is
    # "the question was never asked", and the two must not look alike.
    if constraints and not any(w.get("local_storage_exact") for w in constraints):
        unknown.append("emptyDir medium and safe-to-evict-local-volumes (snapshot predates them -- "
                       "every emptyDir counts as local storage, which can report a blocker CA "
                       "does not have; re-run `collect` with --context)")
    if constraints and not any(w.get("spread_known") for w in constraints):
        unknown.append("hard topology spread (snapshot predates whenUnsatisfiable -- not checked)")
    if constraints and not any("labels" in w for w in constraints):
        unknown.append("pod-template labels, nodeSelector, node affinity and "
                       "tolerations (snapshot predates them -- re-run `collect` "
                       "with --context to enable the PDB and scheduling checks)")

    specs = list(facts.pdb_specs or [])
    if not specs:
        unknown.append("PodDisruptionBudget specs")
    elif not any("selector" in p for p in specs):
        unknown.append("PDB selectors (snapshot predates them -- no PDB can be "
                       "attributed to the pods it governs)")
    pdbs = [
        Pdb(
            namespace=p.get("namespace", ""),
            name=p.get("name", ""),
            selector=dict(p.get("selector") or {}),
            min_available=p.get("min_available"),
            max_unavailable=p.get("max_unavailable"),
            has_expressions=bool(p.get("has_expressions")),
        )
        for p in specs
    ]
    if any(p.has_expressions for p in pdbs):
        unknown.append("PDBs whose selector uses matchExpressions "
                       "(not evaluated; treated as covering nothing)")

    pdb_for: dict[str, Pdb] = {}
    for key, labels in labels_of.items():
        ns = key.split("/", 1)[0]
        for pdb in pdbs:
            if pdb.namespace == ns and pdb.covers(labels):
                pdb_for[key] = pdb
                break

    node_labels: dict[str, dict] = {}
    node_taints: dict[str, list] = {}
    no_scale_down: set[str] = set()
    for n in facts.node_facts or []:
        node_labels[n.get("name", "")] = dict(n.get("labels") or {})
        node_taints[n.get("name", "")] = list(n.get("taint_specs") or [])
        if n.get("scale_down_disabled"):
            no_scale_down.add(n.get("name", ""))
    if not node_labels:
        unknown.append("node labels and taints")
    elif not any(node_labels.values()):
        unknown.append("node labels (snapshot predates them -- nodeSelector and "
                       "node affinity cannot be checked against a destination)")

    # A pod on a zonal volume stays in the zone of the node it runs on now.
    volumes = getattr(snapshot, "volumes", None) or {}
    if not volumes:
        unknown.append("persistent volumes (re-run `rightsizer storage` -- without them a "
                       "pod on an EBS volume may be moved out of its volume's zone)")
    zone_bound: dict[str, str] = {}
    for rows in (snapshot.placements or {}).values():
        for r in rows:
            key = f"{r.get('namespace', '')}/{r.get('pod', '')}"
            if any(v.get("zonal") for v in volumes.get(key, [])):
                zone = (node_labels.get(r.get("node", "")) or {}).get(ZONE_LABEL)
                if zone:
                    zone_bound[key] = zone

    replicas: dict[str, int] = {}
    for stat in snapshot.usage:
        key = f"{stat.namespace}/{stat.workload}/{stat.kind}"
        replicas[key] = max(replicas.get(key, 0), stat.replicas or 0)

    return EvictionRules(
        drain_blocked=drain_blocked,
        labels=labels_of,
        pdb_for=pdb_for,
        replicas=replicas,
        node_selector=node_selector,
        node_affinity=node_affinity,
        tolerations=tolerations,
        node_labels=node_labels,
        node_taints=node_taints,
        antiaffinity=frozenset(antiaffinity),
        zone_bound=zone_bound,
        unknown=tuple(unknown),
        safe_to_evict=frozenset(safe),
        not_safe_to_evict=frozenset(not_safe),
        local_storage=frozenset(local),
        hard_spread=hard_spread,
        scale_down_disabled=frozenset(no_scale_down),
    )


def simulate_group(snapshot, group, config, rules: EvictionRules, min_size: int = 0,
                   request_delta: dict[str, dict[str, float]] | None = None,
                   keep: dict[str, int] | None = None):
    """Replay CA's loop over one node group's observed placement.

    Returns ``(run, checked)`` where ``checked`` names the veto classes that
    were actually evaluated. A class whose facts are missing is absent from that
    list, so a caller can report "three of five checked" instead of presenting a
    partial answer as a whole one.

    ``request_delta`` ({workload key: {"cpu": cores, "mem": bytes}}, per pod)
    replays CA as it would behave after a request change: smaller requests make
    nodes look emptier, so CA removes more of them. ``run.placement`` then says
    where every pod lands, which is what the pressure check needs.

    ``keep`` ({workload key: replicas}) replays a lowered replica floor: that
    many of the workload's pods stay. A ReplicaSet scaling down removes pods
    from the nodes holding the most of its replicas first, so that is the order
    used here. Pods that no longer fit where they run after a request *increase*
    are rescheduled the way the scheduler would -- onto a node with room, else
    onto a new node, which is Cluster Autoscaler's scale-up -- before the
    scale-down loop runs. ``run.added`` counts those new nodes.
    """
    from rightsizer.engine.autoscaler import PlacedPod, simulate_ca_scale_down

    rows = (snapshot.placements or {}).get(group.name) or []
    if not rows:
        return None, ()

    pods = []
    for r in rows:
        d = (request_delta or {}).get(r.get("workload_key", ""), {})
        pods.append(PlacedPod(
            namespace=r.get("namespace", ""),
            name=r.get("pod", ""),
            node=r.get("node", ""),
            cpu=max(0.0, float(r.get("cpu", 0.0)) + d.get("cpu", 0.0)),
            memory=max(0.0, float(r.get("mem", 0.0)) + d.get("mem", 0.0)),
            owner_kind=r.get("owner_kind", ""),
            workload_key=r.get("workload_key", ""),
            created=r.get("created"),
        ))
    if keep:
        from rightsizer.engine.realistic import apply_keep
        pods = apply_keep(pods, keep)

    nodes = sorted({p.node for p in pods if p.node})
    if not nodes:
        return None, ()

    max_pods = snapshot.facts.max_pods_for(group.name) if hasattr(snapshot.facts, "max_pods_for") else None
    added, unplaceable = _reschedule_overflow(pods, nodes, group, config, max_pods, rules)
    pods = [p for p in pods if p.node]
    nodes = sorted({p.node for p in pods})
    now = _observed_at(snapshot)

    # Full allocatable. The placement includes each node's DaemonSet pods, so
    # free space already accounts for them, and utilisation leaves them out
    # when CA is told to. Subtracting them from allocatable as well counted
    # them twice and made every node look fuller than CA sees it.
    # Cordoned or NotReady nodes accept nothing. Both sources count: the metrics
    # (always collected now) and the API's spec.unschedulable (with --context).
    unschedulable = set(group.unschedulable_nodes) | {
        n.get("name", "") for n in (snapshot.facts.node_facts or [])
        if n.get("unschedulable") and n.get("name") in nodes
    }
    run = simulate_ca_scale_down(
        nodes=nodes,
        pods=pods,
        alloc_cpu=max(0.0, group.alloc_cpu_per_node),
        alloc_mem=max(0.0, group.alloc_mem_per_node),
        config=config,
        min_size=min_size or 0,
        drain_blocked=frozenset(rules.local_storage),
        antiaffinity=rules.antiaffinity,
        rules=rules,
        unschedulable=frozenset(unschedulable),
        max_pods=max_pods,
        now=now,
    )
    run.added += added
    run.unplaceable = unplaceable
    if not run.placement and not run.total:
        # Scale-down disabled or nothing to evaluate: the pods stay where they are.
        for pod in pods:
            if pod.node:
                run.placement.setdefault(pod.node, []).append(pod)

    checked = ["bare pods"]
    if group.not_ready_nodes is not None or snapshot.facts.node_facts:
        checked.append(
            "cordoned / NotReady nodes excluded as destinations"
            + (f" ({len(unschedulable)} here)" if unschedulable else "")
        )
    if config.skips_system_pods:
        checked.append("kube-system without a PDB" if rules.pdb_for
                       else "kube-system (PDB exemption not evaluated)")
    if rules.drain_blocked or not rules.unknown:
        checked.append("local storage / safe-to-evict / do-not-disrupt")
    if rules.pdb_for:
        checked.append("disruption budgets")
    if rules.node_labels and any(rules.node_labels.values()):
        checked.append("nodeSelector, node affinity and taints")
    if rules.antiaffinity:
        checked.append("hostname anti-affinity")
    return run, tuple(checked)


def _observed_at(snapshot) -> float | None:
    """When the placement was observed (unix seconds), or None if unreadable."""
    import datetime as _dt

    try:
        return _dt.datetime.fromisoformat(snapshot.collected_at).timestamp()
    except (TypeError, ValueError):
        return None


def _reschedule_overflow(pods, nodes, group, config, max_pods: int | None = None,
                         rules: EvictionRules | None = None) -> tuple[int, list]:
    """Move pods off nodes that a request increase has overfilled.

    A pod whose request grows is recreated by its rollout; kube-scheduler places
    it on the feasible node it scores highest (see
    :func:`rightsizer.engine.autoscaler.scheduler_score`), and when none is feasible it
    stays Pending until Cluster Autoscaler adds a node. A pod that would not fit
    even on an empty node of this group is **unplaceable**: no scale-up helps,
    and it is returned rather than given a node of its own. Returns (nodes
    added, unplaceable pods); an unplaceable pod is left with ``node = ""``.
    """
    from rightsizer.engine.autoscaler import scheduler_score

    alloc_cpu = max(0.0, group.alloc_cpu_per_node)
    alloc_mem = max(0.0, group.alloc_mem_per_node)
    if alloc_cpu <= 0 or alloc_mem <= 0:
        return 0, []
    on: dict[str, list] = {n: [] for n in nodes}
    for p in pods:
        if p.node in on:
            on[p.node].append(p)
    # A new node starts with the DaemonSet pods the busiest node carries.
    template = max((sorted((d for d in on[n] if d.is_daemonset), key=lambda d: d.name) for n in on),
                   key=len, default=[])
    ds_cpu = sum(d.cpu for d in template)
    ds_mem = sum(d.memory for d in template)
    cap = max_pods or 10**9

    def over(n: str) -> bool:
        return (sum(p.cpu for p in on[n]) > alloc_cpu + 1e-9
                or sum(p.memory for p in on[n]) > alloc_mem + 1e-9)

    pending = []
    for n in nodes:
        here = sorted((p for p in on[n] if not p.is_daemonset),
                      key=lambda p: (p.cpu / alloc_cpu + p.memory / alloc_mem))
        while here and over(n):
            p = here.pop()
            on[n].remove(p)
            pending.append(p)

    def feasible(p, n: str) -> bool:
        if len(on[n]) + 1 > cap:
            return False
        if p.cpu > alloc_cpu - sum(q.cpu for q in on[n]) + 1e-9:
            return False
        if p.memory > alloc_mem - sum(q.memory for q in on[n]) + 1e-9:
            return False
        if rules is None:
            return True
        present = {q.workload_key for q in on[n] if q.workload_key in rules.antiaffinity}
        return rules.can_host(p, n, present) if not n.startswith("(new") else True

    added = 0
    unplaceable = []
    for p in sorted(pending, key=lambda p: (-p.cpu, -p.memory)):
        if p.cpu > alloc_cpu - ds_cpu + 1e-9 or p.memory > alloc_mem - ds_mem + 1e-9:
            p.node = ""
            unplaceable.append(p)
            continue
        best, best_score = None, -1
        for n in on:
            if not feasible(p, n):
                continue
            score = scheduler_score(on[n], p, alloc_cpu, alloc_mem)
            if score > best_score:
                best, best_score = n, score
        if best is None:
            added += 1
            best = f"(new node {added})"
            on[best] = [dataclasses.replace(d, node=best) for d in template]
            pods.extend(on[best])
        on[best].append(p)
        p.node = best
    return added, unplaceable
