"""Recommendations: container requests, and the node count they permit.

The node-count math is closed-form because ASG node groups are homogeneous --
no bin-packer, no scheduler simulation. For a group with per-node allocatable
``A`` and per-node DaemonSet requests ``D``:

    N_capacity = ceil( max over {cpu, mem} of  sum(recommended requests) / (A - D) )

``D`` belongs in the denominator, not the numerator: DaemonSet pods run on every
node, so their requests shrink what each node has left rather than adding to the
total demand. Getting that backwards makes every number wrong.

The true floor is the max of several constraints; only some are visible from
Prometheus. What is not visible is reported as a blocker rather than assumed away.
"""

from __future__ import annotations

import math

from rightsizer.core.config import Tunables
from rightsizer.core.models import (
    Blocker,
    ContainerRecommendation,
    NodeGroupRecommendation,
    NodeGroupState,
    Snapshot,
    UsageStat,
)
from rightsizer.core.stats import recommend_cpu, recommend_memory, tail_adjusted

HOURS_PER_MONTH = 730.0


#: A container throttled for more than this share of its CFS periods is being
#: slowed measurably. Kubernetes' own guidance treats sustained throttling as a
#: signal to raise the limit; the number is low because the metric is a ratio of
#: periods, not of time, and even a few percent is a visible latency tail.
THROTTLE_THRESHOLD = 0.01
#: How far the measured basis must sit above the request before an increase is
#: worth proposing. Below this the change is noise against the round-up.
CPU_INCREASE_MARGIN = 1.10


def _cpu_increase_warranted(stat, tun) -> bool:
    """Is a CPU increase backed by measurement, or just by the floor?

    Two independent grounds, and either is enough:

    * the container is being **throttled** -- direct evidence, and the only
      signal usage cannot give, because usage looks fine precisely when the
      limit is holding it down;
    * its measured basis sits meaningfully above its request, so it is running
      on borrowed CPU that vanishes the moment the node is busy.

    The floor is deliberately excluded: `recommend_cpu` raises anything under
    `cpu_floor_millicores` to it, which would otherwise report every idle
    sidecar as needing more CPU.
    """
    if stat.limit > 0 and stat.throttle_pct >= THROTTLE_THRESHOLD:
        return True
    return tail_adjusted(stat, tun) > stat.request * CPU_INCREASE_MARGIN


def dominant_nodegroup(snapshot: Snapshot, stat: UsageStat) -> tuple[str, float]:
    shares = snapshot.workload_nodegroups.get(stat.workload_key, {})
    if not shares:
        return ("(unknown)", 0.0)
    name = max(shares, key=lambda k: shares[k])
    return (name, shares[name])


def container_recommendations(
    snapshot: Snapshot, tun: Tunables
) -> list[ContainerRecommendation]:
    from rightsizer.analysis.blockers import _hpa_index, workload_blockers

    hpa_index = _hpa_index(snapshot.hpas)
    by_key: dict[tuple, dict[str, UsageStat]] = {}
    for stat in snapshot.usage:
        by_key.setdefault(stat.key, {})[stat.resource] = stat

    out: list[ContainerRecommendation] = []
    for key, pair in sorted(by_key.items()):
        cpu, mem = pair.get("cpu"), pair.get("mem")
        anchor = cpu or mem
        if anchor is None:
            continue
        blockers: list[Blocker] = []
        for stat in (cpu, mem):
            if stat is not None:
                blockers.extend(workload_blockers(stat, snapshot, tun, hpa_index))

        blocked_codes = {b.code for b in blockers}
        hard_block = blocked_codes & {"insufficient-data", "hpa-utilization-target"}

        hpa = hpa_index.get((anchor.namespace, anchor.workload))
        # Size to the HPA's own target only when the replica floor -- not the HPA --
        # is what currently holds the replica count.
        hpa_target = (
            hpa.cpu_target
            if hpa and "hpa-floor-bound" in blocked_codes and hpa.cpu_target
            else None
        )

        cpu_rec = None
        if cpu and cpu.request > 0 and not hard_block:
            candidate = recommend_cpu(cpu, tun, hpa_target)
            if candidate < cpu.request:
                cpu_rec = candidate
            elif candidate > cpu.request and _cpu_increase_warranted(cpu, tun):
                # A right-sizer that only ever cuts CPU is a cost tool. An
                # under-set CPU request is a real performance exposure -- the
                # container is not guaranteed what it measurably uses, so it
                # loses first when the node is contended, and a throttled
                # container is being slowed down right now.
                #
                # Gated, because most "increases" here are the millicore floor
                # talking: a 10m request whose p95 is 1m gets a 50m candidate
                # from `cpu_floor_millicores` alone, and raising it buys
                # nothing. Only a *measured* need counts.
                cpu_rec = candidate

        mem_rec = None
        if mem and mem.request > 0 and "insufficient-data" not in blocked_codes:
            candidate = recommend_memory(mem, tun)
            # An increase is always emitted: memory overshoot is an OOMKill, and a
            # correctness fix is as much a finding as a saving.
            if candidate != mem.request:
                mem_rec = candidate

        nodegroup, share = dominant_nodegroup(snapshot, anchor)
        out.append(
            ContainerRecommendation(
                namespace=anchor.namespace,
                workload=anchor.workload,
                kind=anchor.kind,
                container=anchor.container,
                replicas=anchor.replicas,
                cpu_request=cpu.request if cpu else 0.0,
                cpu_recommended=cpu_rec,
                mem_request=mem.request if mem else 0.0,
                mem_recommended=mem_rec,
                nodegroup=nodegroup,
                nodegroup_share=share,
                cpu_total_peak=cpu.total_peak if cpu else 0.0,
                mem_total_peak=mem.total_peak if mem else 0.0,
                cpu_coincidence=cpu.coincidence if cpu else None,
                basis=(
                    f"cpu p95 {cpu.p95:.3f} peak {cpu.peak:.3f} x{tun.cpu_headroom} | "
                    f"mem peak {(mem.peak / 1024**3):.2f}Gi x{tun.mem_headroom}"
                    if cpu and mem
                    else "partial data"
                ),
                blockers=blockers,
            )
        )
    return out


def _request_coverage(snapshot: Snapshot, group: str) -> float:
    on_group = [
        s
        for s in snapshot.usage
        if s.resource == "cpu" and dominant_nodegroup(snapshot, s)[0] == group
    ]
    if not on_group:
        return 1.0
    declared = sum(1 for s in on_group if s.request > 0)
    return declared / len(on_group)


def nodegroup_recommendations(
    snapshot: Snapshot,
    tun: Tunables,
    recs: list[ContainerRecommendation],
    price_per_node_hour: dict[str, float] | None = None,
) -> list[NodeGroupRecommendation]:
    from rightsizer.analysis.blockers import nodegroup_blockers

    price_per_node_hour = price_per_node_hour or {}
    by_group: dict[str, list[ContainerRecommendation]] = {}
    for rec in recs:
        by_group.setdefault(rec.nodegroup, []).append(rec)

    out: list[NodeGroupRecommendation] = []
    for group in snapshot.nodegroups:
        members = by_group.get(group.name, [])
        coverage = _request_coverage(snapshot, group.name)
        blockers = nodegroup_blockers(group, snapshot, tun, coverage)

        alloc_cpu = group.alloc_cpu_per_node - group.ds_cpu_per_node
        alloc_mem = group.alloc_mem_per_node - group.ds_mem_per_node
        if alloc_cpu <= 0 or alloc_mem <= 0 or group.nodes_current == 0:
            continue
        if not members:
            # No workload could be attributed to this group. That is missing
            # evidence, not an empty group -- sizing it from nothing would
            # recommend scaling to the minimum and delete a live tier.
            out.append(
                NodeGroupRecommendation(
                    name=group.name,
                    nodes_current=group.nodes_current,
                    nodes_avg_30d=group.nodes_avg_30d,
                    nodes_floor=group.nodes_current,
                    nodes_target=group.nodes_current,
                    binding_constraint="no attributable workloads (not sized)",
                    node_hours_saved_month=0.0,
                    blockers=[
                        *blockers,
                        Blocker(
                            "no-workload-data",
                            "data",
                            group.name,
                            "no workload usage could be attributed to this group, so it was not "
                            "sized. Either the pods are short-lived, or the metrics needed to map "
                            "them are missing.",
                        ),
                    ],
                    evidence={"pods": group.pod_count, "nodes_min_30d": group.nodes_min_30d},
                )
            )
            continue

        # Demand is anchored on what this group *measurably* carries today
        # (sum of non-DaemonSet requests of the pods actually scheduled here),
        # not on re-deriving it from workload attribution. A workload's replicas
        # are spread across groups, so attributing all of them to whichever group
        # wins the dominance vote inflates demand by the number of groups it spans.
        #
        # Recommendations then apply as a *reduction* against that measured
        # baseline, prorated by the workload's share of this group. A blocked
        # recommendation contributes nothing: it cannot be applied, so it must
        # not manufacture a node removal.
        cut_cpu = 0.0
        cut_mem = 0.0
        for r in members:
            if not r.actionable:
                continue
            here = max(r.replicas, 1) * (r.nodegroup_share or 1.0)
            if r.cpu_recommended is not None and r.cpu_recommended < r.cpu_request:
                cut_cpu += (r.cpu_request - r.cpu_recommended) * here
            if r.mem_recommended is not None and r.mem_recommended < r.mem_request:
                cut_mem += (r.mem_request - r.mem_recommended) * here

        demand_cpu = max(0.0, group.workload_cpu_requests - cut_cpu)
        demand_mem = max(0.0, group.workload_mem_requests - cut_mem)

        def floor_for(cpu_total: float, mem_total: float) -> tuple[int, int, int]:
            n_c = math.ceil(cpu_total / alloc_cpu) if cpu_total else 0
            n_m = math.ceil(mem_total / alloc_mem) if mem_total else 0
            return max(n_c, n_m, 1), n_c, n_m

        # Two different floors, because they are realised by different mechanisms.
        #
        #   n_today  -- what the group's CURRENT requests would fit into under
        #               perfect packing. Reaching it requires *defragmentation*:
        #               moving running pods onto fewer nodes.
        #   n_after  -- the same after applying the actionable request cuts.
        #
        # Cluster Autoscaler does not defragment. It removes a node only when that
        # node's requests fall below its utilisation threshold AND every pod on it
        # already fits elsewhere; it will never relocate pods to create that
        # condition. So the gap between avg nodes and n_today is real capacity but
        # it is NOT self-realising -- it needs a deliberate ASG change or a drain.
        # Reporting the two together as one number is how a projection that looks
        # like a scale-in turns into a bill that does not move.
        n_today, _, _ = floor_for(group.workload_cpu_requests, group.workload_mem_requests)
        n_capacity, n_cpu, n_mem = floor_for(demand_cpu, demand_mem)
        binding = "cpu capacity" if n_cpu >= n_mem else "memory capacity"

        # Sanity rail: the group must still hold today's pods. If the computed
        # floor exceeds the current node count, the inputs disagree with reality --
        # report it rather than emitting a scale-*up* dressed as right-sizing.
        # The true floor is the max of several constraints, most of which are not
        # capacity. Capacity is simply the one Prometheus can see.
        floors: list[tuple[int, str]] = [(n_capacity, binding)]

        facts = snapshot.facts
        if facts.collected:
            members_keys = {f"{m.namespace}/{m.workload}/{m.kind}" for m in members}
            aa_floor, aa_who = facts.antiaffinity_floor(members_keys)
            if aa_floor:
                floors.append((aa_floor, f"anti-affinity: {aa_who} needs one node per replica"))

            max_pods = facts.max_pods_for(group.name)
            if max_pods and group.pod_count:
                floors.append(
                    (
                        math.ceil(group.pod_count / max_pods),
                        f"pod density ({group.pod_count} pods / {max_pods} maxPods per node)",
                    )
                )

            config = facts.config_for(group.name)
            if config and int(config.get("min_size", 0)):
                floors.append(
                    (int(config["min_size"]), f"node group minSize={config['min_size']}")
                )

        n_floor, binding = max(floors, key=lambda f: f[0])
        n_floor = max(n_floor, 1)
        if n_floor > group.nodes_current:
            blockers.append(
                Blocker(
                    "demand-exceeds-capacity",
                    "data",
                    group.name,
                    f"computed floor ({n_floor}) is above the current node count "
                    f"({group.nodes_current}): measured requests do not fit the measured "
                    "allocatable capacity. Do not act on this group until reconciled.",
                )
            )
        n_target = max(1, math.ceil(n_floor / tun.target_utilization))
        # Never propose more nodes than the group already runs. Headroom is a
        # safety margin on a scale-in, not a licence to recommend a scale-out:
        # growth decisions need demand forecasting this tool does not do.
        n_target = min(n_target, max(1, math.ceil(group.nodes_avg_30d)))
        # Never recommend below the observed 30d floor: the group held that many
        # nodes for a reason the metrics may not explain.
        if group.nodes_min_30d:
            if n_target < group.nodes_min_30d:
                binding = f"30d minimum node count ({group.nodes_min_30d})"
            n_target = max(n_target, group.nodes_min_30d)

        def target_from(floor: int) -> int:
            t = max(1, math.ceil(floor / tun.target_utilization))
            t = min(t, max(1, math.ceil(group.nodes_avg_30d)))
            return max(t, group.nodes_min_30d) if group.nodes_min_30d else t

        target_today = target_from(n_today)
        # Savings attributable to the request cuts themselves: the further the
        # group can shrink *because* requests came down.
        cut_hours = max(0.0, target_today - n_target) * HOURS_PER_MONTH
        # Savings available only by defragmenting what is already running.
        repack_hours = max(0.0, group.nodes_avg_30d - target_today) * HOURS_PER_MONTH
        node_hours = cut_hours

        if repack_hours > 0:
            blockers.append(
                Blocker(
                    "repacking-required",
                    "zero-savings",
                    group.name,
                    f"a further {repack_hours / HOURS_PER_MONTH:.1f} nodes' worth of capacity is "
                    "free at current requests, but reaching it means moving running pods onto "
                    "fewer nodes. Cluster Autoscaler does not defragment -- it only removes a "
                    "node whose pods already fit elsewhere. This is reachable by lowering the "
                    "ASG desired count deliberately, not by right-sizing, and it is excluded "
                    "from the headline saving.",
                )
            )

        out.append(
            NodeGroupRecommendation(
                name=group.name,
                nodes_current=group.nodes_current,
                nodes_avg_30d=group.nodes_avg_30d,
                nodes_floor=n_floor,
                nodes_target=n_target,
                binding_constraint=binding,
                node_hours_saved_month=node_hours,
                blockers=blockers,
                evidence={
                    "node_hours_from_request_cuts": round(cut_hours, 1),
                    "node_hours_from_repacking": round(repack_hours, 1),
                    "target_at_current_requests": target_today,
                    "alloc_cpu_per_node": round(alloc_cpu, 3),
                    "alloc_mem_per_node_gib": round(alloc_mem / 1024**3, 2),
                    "daemonset_cpu_per_node": round(group.ds_cpu_per_node, 3),
                    "daemonset_mem_per_node_gib": round(group.ds_mem_per_node / 1024**3, 2),
                    "measured_cpu_requests": round(group.workload_cpu_requests, 2),
                    "measured_mem_requests_gib": round(group.workload_mem_requests / 1024**3, 2),
                    "cut_cpu_cores": round(cut_cpu, 2),
                    "cut_mem_gib": round(cut_mem / 1024**3, 2),
                    "demand_cpu_cores": round(demand_cpu, 2),
                    "demand_mem_gib": round(demand_mem / 1024**3, 2),
                    "n_cpu": n_cpu,
                    "n_mem": n_mem,
                    "request_coverage": round(coverage, 3),
                    "workloads": len(members),
                    "pods": group.pod_count,
                    "nodes_min_30d": group.nodes_min_30d,
                    "nodes_peak_30d": group.nodes_peak_30d,
                },
            )
        )
    return sorted(out, key=lambda r: -r.node_hours_saved_month)
