"""Blockers: the reasons a recommendation is not worth acting on.

A blocker is a first-class output, not a footnote. Most tools silently skip the
cases below and report the aggregate saving anyway, which is how a projection
realises at a fraction of its headline. Here, a blocked recommendation is shown
with the reason and contributes **zero** to the savings total.

Severities:
  ``zero-savings`` -- the action is safe but provably cannot reduce the bill.
  ``unsafe``       -- the action risks availability.
  ``data``         -- the evidence does not support a recommendation.
"""

from __future__ import annotations

from rightsizer.core.config import Tunables
from rightsizer.core.models import Blocker, HPASpec, NodeGroupState, PDBState, Snapshot, UsageStat
from rightsizer.core.stats import has_enough_data

MULTI_GROUP_SHARE = 0.9


def _hpa_index(hpas: list[HPASpec]) -> dict[tuple[str, str], HPASpec]:
    return {(h.namespace, h.target_name): h for h in hpas if h.target_name}


def workload_blockers(
    stat: UsageStat,
    snapshot: Snapshot,
    tun: Tunables,
    hpa_index: dict[tuple[str, str], HPASpec] | None = None,
) -> list[Blocker]:
    """Every reason not to act on one (workload, container, resource)."""
    hpa_index = hpa_index if hpa_index is not None else _hpa_index(snapshot.hpas)
    subject = f"{stat.namespace}/{stat.workload}:{stat.container}"
    out: list[Blocker] = []

    ok, why = has_enough_data(stat, tun)
    if not ok:
        out.append(Blocker("insufficient-data", "data", subject, why))

    if stat.request <= 0:
        out.append(
            Blocker(
                "no-request-declared",
                "data",
                subject,
                "no resource request set: the Cluster Autoscaler sees this pod as free and "
                "may remove a node while it is working. Setting a request is a correctness "
                "fix, not a saving.",
            )
        )

    hpa = hpa_index.get((stat.namespace, stat.workload))
    if hpa and hpa.scales_on_resource_utilization and stat.resource == "cpu":
        if hpa.governs(stat.p95, stat.request, stat.replicas):
            targets = ", ".join(f"{n}/{t}" for n, t in hpa.targets)
            out.append(
                Blocker(
                    "hpa-utilization-target",
                    "zero-savings",
                    subject,
                    f"HPA {hpa.name} targets {targets} and is the binding constraint on replica "
                    "count. Utilization is usage/request, so lowering the request raises it, the "
                    "HPA scales out, and replicas x request stays invariant. Savings are zero.",
                )
            )
        else:
            reserved = stat.request * max(stat.replicas, 1)
            equilibrium = (stat.p95 * max(stat.replicas, 1)) / (hpa.cpu_target or 1)
            out.append(
                Blocker(
                    "hpa-floor-bound",
                    "advisory",
                    subject,
                    f"HPA {hpa.name} targets {hpa.cpu_target:.0%} CPU but measured utilization is "
                    f"{stat.p95 / stat.request:.1%}, so minReplicas={hpa.min_replicas} holds the "
                    f"replica count, not the HPA. {reserved:.1f} cores are reserved against a "
                    f"{equilibrium:.1f}-core equilibrium: the excess is over-reservation and "
                    "cutting it is safe. Re-check the HPA target afterwards.",
                )
            )

    if stat.resource == "mem" and stat.oom_killed:
        out.append(
            Blocker(
                "oom-killed",
                "unsafe",
                subject,
                "container was OOMKilled in the window: this needs an increase, not a cut.",
            )
        )

    shares = snapshot.workload_nodegroups.get(stat.workload_key, {})
    if shares and max(shares.values()) < MULTI_GROUP_SHARE:
        spread = ", ".join(f"{ng} {share:.0%}" for ng, share in sorted(shares.items()))
        out.append(
            Blocker(
                "spans-node-groups",
                "data",
                subject,
                f"workload ran across several node groups ({spread}); these are one logical "
                "tier and must be sized together, never in isolation.",
            )
        )

    return out


def nodegroup_blockers(
    group: NodeGroupState,
    snapshot: Snapshot,
    tun: Tunables,
    request_coverage: float,
    max_pods_per_node: int | None = None,
) -> list[Blocker]:
    out: list[Blocker] = []

    if request_coverage < tun.request_coverage_threshold:
        out.append(
            Blocker(
                "low-request-coverage",
                "data",
                group.name,
                f"only {request_coverage:.0%} of workloads here declare requests. Request-based "
                "sizing would report this group as nearly empty and recommend deleting most of "
                "it. Sized from measured usage instead; never lower this group's floor from "
                "request data.",
            )
        )

    if group.nodes_peak_30d and group.nodes_min_30d == group.nodes_peak_30d:
        out.append(
            Blocker(
                "flat-node-count",
                "data",
                group.name,
                f"node count was flat at {group.nodes_min_30d} for the whole window: the "
                "autoscaler is not moving this group, so something else (min size, a floor, or "
                "an unschedulable workload) is holding it. Confirm before scaling in.",
            )
        )

    if max_pods_per_node and group.nodes_current:
        ceiling = max_pods_per_node * group.nodes_current
        if ceiling and group.pod_count / ceiling > 0.9:
            out.append(
                Blocker(
                    "pod-density-bound",
                    "zero-savings",
                    group.name,
                    f"{group.pod_count} pods against an ENI-derived ceiling of ~{ceiling}. This "
                    "group is bound by pod density, not CPU or memory, so cutting requests frees "
                    "nothing.",
                )
            )

    blocking = [p for p in snapshot.pdbs if p.blocks_drain]
    if blocking:
        names = ", ".join(f"{p.namespace}/{p.name}" for p in blocking[:5])
        out.append(
            Blocker(
                "pdb-blocks-drain",
                "unsafe",
                group.name,
                f"{len(blocking)} PodDisruptionBudget(s) permit zero evictions ({names}). Any "
                "node hosting these cannot be drained, so the scale-in will stall.",
            )
        )

    unhealthy = _unhealthy_pdb_blocker(group, snapshot)
    if unhealthy:
        out.append(unhealthy)

    facts = snapshot.facts
    if facts.collected:
        config = facts.config_for(group.name)
        if config and int(config.get("desired_size", 0)) <= int(config.get("min_size", 0)):
            out.append(
                Blocker(
                    "at-asg-min",
                    "zero-savings",
                    group.name,
                    f"desired={config.get('desired_size')} is already at minSize="
                    f"{config.get('min_size')}. The autoscaler cannot go lower; realising any "
                    "saving here means changing the node group's minimum, not right-sizing.",
                )
            )

        blocking = [p for p in facts.pdb_specs if str(p.get("max_unavailable")) == "0"]
        if blocking:
            names = ", ".join(f"{p['namespace']}/{p['name']}" for p in blocking[:5])
            out.append(
                Blocker(
                    "pdb-blocks-drain",
                    "unsafe",
                    group.name,
                    f"{len(blocking)} PodDisruptionBudget(s) set maxUnavailable: 0 ({names}). "
                    "No pod may be evicted, so any node hosting them can never be drained.",
                )
            )

        # Attribute drain-blockers to the group that actually hosts them. The
        # workload -> node group mapping already exists from Prometheus, so this
        # needs no extra API call. Applying a cluster-wide count to every group
        # blocks everything and makes the whole report read as "no savings".
        # Which pods the autoscaler that is running refuses to evict: Cluster
        # Autoscaler's local-storage rule applies only under it; Karpenter
        # drains those pods and refuses only karpenter.sh/do-not-disrupt.
        from rightsizer.adapters.autoscalers import for_snapshot

        adapter, found = for_snapshot(snapshot)
        settings = adapter.scale_down_settings(found)
        stuck = []
        for w in facts.workload_constraints:
            if not _blocks_drain(w, adapter, settings):
                continue
            key = f"{w.get('namespace')}/{w.get('name')}/{w.get('kind')}"
            shares = snapshot.workload_nodegroups.get(key)
            if shares is None:
                continue  # never observed on any group: cannot attribute, do not block
            if shares.get(group.name, 0.0) > 0.0:
                stuck.append(w)
        if stuck:
            names = ", ".join(f"{w['namespace']}/{w['name']}" for w in stuck[:4])
            if settings.skips_local_storage:
                code, why = "local-storage-blocks-drain", (
                    "emptyDir/hostPath without cluster-autoscaler.kubernetes.io/safe-to-evict=true, "
                    "or an explicit safe-to-evict=false")
            else:
                code, why = "do-not-disrupt-blocks-drain", (
                    "annotated karpenter.sh/do-not-disrupt or safe-to-evict=false (local storage "
                    f"does not block {adapter.title}: it drains emptyDir/hostPath pods)")
            out.append(
                Blocker(
                    code,
                    "unsafe",
                    group.name,
                    f"{len(stuck)} workload(s) {adapter.title} will not evict ({names}): {why}. "
                    "Nodes hosting them are never removed, which is usually why a group sits flat.",
                )
            )
    else:
        out.append(
            Blocker(
                "topology-not-evaluated",
            "data",
            group.name,
            "pod anti-affinity and topology spread constraints are not visible in Prometheus and "
            "were not evaluated. They frequently set the real floor above the capacity floor, so "
            "treat this node count as an upper bound on savings until checked against the "
                "Kubernetes API.",
            )
        )

    return out


def _pdb_workloads(snapshot: Snapshot) -> dict[tuple[str, str], set[str]]:
    """(namespace, PDB name) -> workload keys its selector matches.

    Needs the PDB specs and pod-template labels from the Kubernetes API. A PDB
    that cannot be matched -- no facts, a matchExpressions selector, or no known
    workload carrying its labels -- is left out, and the caller treats it as
    unattributed rather than as covering nothing.
    """
    from rightsizer.engine.eviction import Pdb

    facts = snapshot.facts
    labels = {
        f"{w.get('namespace')}/{w.get('name')}/{w.get('kind')}": dict(w.get("labels") or {})
        for w in facts.workload_constraints
    }
    out: dict[tuple[str, str], set[str]] = {}
    for spec in facts.pdb_specs:
        pdb = Pdb(
            namespace=spec.get("namespace", ""),
            name=spec.get("name", ""),
            selector=dict(spec.get("selector") or {}),
            has_expressions=bool(spec.get("has_expressions")),
        )
        matched = {
            key for key, lab in labels.items()
            if key.split("/", 1)[0] == pdb.namespace and lab and pdb.covers(lab)
        }
        if matched:
            out[(pdb.namespace, pdb.name)] = matched
    return out


def _unhealthy_pdb_blocker(group: NodeGroupState, snapshot: Snapshot) -> Blocker | None:
    """PDBs whose budget unhealthy pods have used up, on the groups hosting them.

    Transient, unlike ``pdb-blocks-drain``: the budget is fine on paper and
    returns once the failing pods recover. It still stalls a drain today, and
    the fix is different -- repair the pods, not the PDB -- so it is reported
    separately and names the pods and why they are failing.
    """
    stuck = [p for p in snapshot.pdbs if p.blocked_by_unhealthy]
    if not stuck:
        return None

    covered = _pdb_workloads(snapshot)
    failing: dict[str, list[dict]] = {}
    for pod in snapshot.unhealthy_pods:
        failing.setdefault(pod.get("workload_key", ""), []).append(pod)

    lines: list[str] = []
    unattributed = False
    for p in stuck:
        workloads = covered.get((p.namespace, p.name))
        if workloads is None:
            unattributed = True
        elif not any(
            snapshot.workload_nodegroups.get(w, {}).get(group.name, 0.0) > 0.0 for w in workloads
        ):
            continue  # its pods run on other groups

        line = (
            f"{p.namespace}/{p.name}: {p.current_healthy} of {p.expected_pods} pods healthy, "
            f"needs {p.desired_healthy}, so 0 evictions allowed"
        )
        if p.below_minimum:
            line += " -- below its minimum, so even the unhealthy pods cannot be evicted"
        pods = [x for w in sorted(workloads or ()) for x in failing.get(w, [])]
        if pods:
            shown = ", ".join(f"{x['pod']} ({'; '.join(x['reasons'])})" for x in pods[:3])
            more = f" and {len(pods) - 3} more" if len(pods) > 3 else ""
            line += f". Unhealthy: {shown}{more}"
        elif workloads is None:
            line += (
                ". Its pods could not be identified -- re-run collect with --context to map "
                "the PDB to its workload"
            )
        lines.append(line)

    if not lines:
        return None
    where = (
        " Not every PDB could be mapped to a node group, so it is listed on every group."
        if unattributed else ""
    )
    return Blocker(
        "pdb-unhealthy-blocks-drain",
        "unsafe",
        group.name,
        f"{len(lines)} PodDisruptionBudget(s) have no disruption budget left because some of "
        f"their pods are unhealthy: {'; '.join(lines[:5])}. Nodes running their healthy replicas "
        "cannot be drained until the failing pods recover or are removed -- fix those pods, "
        f"then re-collect.{where}",
    )


def _blocks_drain(w: dict, adapter=None, settings=None) -> bool:
    """Whether the autoscaler running refuses to evict this workload's pods.

    Without an adapter: Cluster Autoscaler's rules, as before adapters.
    """
    from rightsizer.sources.kube import blocks_drain

    if adapter is None:
        from rightsizer.adapters.autoscalers import get

        adapter = get()
    if settings is None:
        settings = adapter.scale_down_settings(None)
    if adapter.drain_veto(w) is not None:
        return True
    if w.get("safe_to_evict") is False:
        return True
    if not settings.skips_local_storage:
        return False
    return blocks_drain(
        local_storage=bool(w.get("local_storage")),
        safe_to_evict=w.get("safe_to_evict"),
        do_not_disrupt=False,
    )


def summarize(blockers: list[Blocker]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for b in blockers:
        counts[b.code] = counts.get(b.code, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
