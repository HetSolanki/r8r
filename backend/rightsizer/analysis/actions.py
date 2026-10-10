"""Actions: what to actually do, ranked by money.

Every action carries a command you can run, the evidence behind it, and what
blocks it. An action with an unresolved ``unsafe`` or ``zero-savings`` blocker is
listed separately and contributes nothing to the total -- the point of the
blocker is that acting on it would not produce the saving.

Dollar figures are **ceilings**: list price less the enterprise discount, with no
Savings Plan modelling. Freeing a node-hour covered by a commitment reduces the
bill only if other uncovered usage absorbs that commitment.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from rightsizer.core.models import Blocker, NodeGroupRecommendation, Snapshot
from rightsizer.adapters import clouds, group_change, group_source
from rightsizer.adapters.clouds import HOURS_PER_MONTH, InstanceType  # noqa: F401


@dataclass
class Action:
    """One action, with a **signed** monthly delta.

    Negative is a reduction, positive an increase, zero a correctness fix that
    fits inside existing headroom. Signing matters: an OOM-risk workload that
    needs a *bigger* request is as much a finding as a saving, and a report that
    only counts reductions is not an action plan, it is a sales pitch.
    """

    kind: str          # scale-in | instance-downsize | memory-increase | annotate | requests
    subject: str
    summary: str
    monthly_delta: float           # signed: negative saves, positive costs
    command: str
    mechanism: str = ""
    severity: str = "routine"      # outage | no-headroom | correctness | routine
    surface: str = ""              # Chart | Terraform/ASG | HPA | Chart + Terraform
    prerequisite: str = "none"
    verified_by: str = ""
    evidence: list[str] = field(default_factory=list)
    blockers: list[Blocker] = field(default_factory=list)

    # A saving that is *structurally zero* is excluded from NET. A saving that
    # merely needs a deliberate step first is NOT excluded -- it is reported with
    # that step as a prerequisite. Conflating the two marks every action blocked
    # and reports $0, which is not caution, it is a broken report.
    #
    # Structurally zero (excluded):
    #   hpa-utilization-target  replicas x request is invariant; cutting saves nothing
    #   pod-density-bound       group is bound by maxPods, not CPU/memory
    #
    # Needs a prerequisite (included, with the step named):
    #   at-asg-min                 raise/lower minSize first
    #   repacking-required         lower desiredSize deliberately; CA will not defrag
    #   local-storage-blocks-drain annotate safe-to-evict, or drain in a window
    #   pdb-blocks-drain           relax the PDB for the maintenance window
    #   pdb-unhealthy-blocks-drain fix the unhealthy pods that used up the PDB's budget
    STRUCTURAL_ZERO: frozenset[str] = frozenset(
        {"hpa-utilization-target", "pod-density-bound"}
    )

    PREREQUISITE_TEXT: dict[str, str] = field(
        default_factory=lambda: {
            "at-asg-min": "lower minSize first",
            "repacking-required": "lower desiredSize deliberately (CA will not defragment)",
            "local-storage-blocks-drain": "annotate safe-to-evict, or drain in a window",
            "do-not-disrupt-blocks-drain": "remove karpenter.sh/do-not-disrupt, or drain in a window",
            "pdb-blocks-drain": "relax PDB for the maintenance window",
            "pdb-unhealthy-blocks-drain": "fix the unhealthy pods holding the PDB at zero budget",
            "flat-node-count": "confirm what is holding the group flat",
        },
        repr=False,
    )

    IRRELEVANT: dict[str, frozenset[str]] = field(
        default_factory=lambda: {
            "instance-downsize": frozenset(
                {"at-asg-min", "repacking-required", "pod-density-bound"}
            ),
            "memory-increase": frozenset(
                {
                    "at-asg-min", "repacking-required", "pod-density-bound",
                    "local-storage-blocks-drain", "pdb-blocks-drain",
                    "pdb-unhealthy-blocks-drain",
                }
            ),
            "annotate": frozenset(
                {
                    "at-asg-min", "repacking-required", "pod-density-bound",
                    "local-storage-blocks-drain", "pdb-blocks-drain",
                    "pdb-unhealthy-blocks-drain",
                }
            ),
        },
        repr=False,
    )

    @property
    def relevant_blockers(self) -> list[Blocker]:
        ignore = self.IRRELEVANT.get(self.kind, frozenset())
        return [
            b
            for b in self.blockers
            if b.severity in ("unsafe", "zero-savings") and b.code not in ignore
        ]

    @property
    def effective_blockers(self) -> list[Blocker]:
        """Only the blockers that make the saving structurally unreachable."""
        return [b for b in self.relevant_blockers if b.code in self.STRUCTURAL_ZERO]

    @property
    def prerequisites(self) -> list[str]:
        """Steps required before this action pays out. Not the same as blocked."""
        out = []
        for b in self.relevant_blockers:
            if b.code in self.STRUCTURAL_ZERO:
                continue
            text = self.PREREQUISITE_TEXT.get(b.code)
            if text and text not in out:
                out.append(text)
        return out

    @property
    def blocked(self) -> bool:
        return bool(self.effective_blockers)

    @property
    def category(self) -> str:
        if self.monthly_delta < 0:
            return "saving"
        if self.monthly_delta > 0:
            return "increase"
        return "neutral"

    @property
    def icon(self) -> str:
        return {"saving": "\U0001F4B0", "increase": "\U0001F53A", "neutral": "\u2796"}[
            self.category
        ]


def _node_price(
    snapshot: Snapshot, instance_type: str, region: str, profile: str | None, discount: float
) -> float:
    return clouds.for_snapshot(snapshot, region=region, profile=profile).monthly_price(instance_type, discount)


def scale_in_actions(
    snapshot: Snapshot,
    groups: list[NodeGroupRecommendation],
    *,
    cluster: str,
    region: str,
    profile: str | None,
    discount: float,
) -> list[Action]:
    out: list[Action] = []
    for g in groups:
        delta = g.nodes_avg_30d - g.nodes_target
        if delta <= 0.5:
            continue
        config = snapshot.facts.config_for(g.name) or {}
        itype = (config.get("instance_types") or [""])[0]
        monthly = _node_price(snapshot, itype, region, profile, discount) * delta if itype else 0.0
        owner = group_change(snapshot, config)
        cmd = owner.scale_in_command(cluster, config, g.nodes_target)
        out.append(
            Action(
                kind="scale-in",
                subject=g.name,
                summary=(
                    f"desired {config.get('desired_size', g.nodes_current)} -> {g.nodes_target} "
                    f"({delta:.1f} x {itype or 'node'})"
                ),
                monthly_delta=-monthly,
                mechanism=f"(a) node scale-in x{delta:.0f}",
                severity="routine",
                surface=owner.retype_plan(config)["surface"],
                verified_by=f"30d avg {g.nodes_avg_30d:.1f} nodes vs target {g.nodes_target}",
                command=cmd,
                evidence=[
                    f"30d nodes min {g.evidence.get('nodes_min_30d')} / "
                    f"avg {g.nodes_avg_30d:.1f} / peak {g.evidence.get('nodes_peak_30d')}",
                    f"binding constraint: {g.binding_constraint}",
                    f"demand {g.evidence.get('demand_cpu_cores')} cores / "
                    f"{g.evidence.get('demand_mem_gib')}Gi against "
                    f"{g.evidence.get('alloc_cpu_per_node')} cores/node",
                ],
                blockers=g.blockers,
            )
        )
    return out


def simulated_resize_actions(
    snapshot: Snapshot,
    groups: list[NodeGroupRecommendation],
    *,
    cluster: str,
    region: str,
    profile: str | None,
    discount: float,
    recommendations: list | None = None,
    ladder_for=None,
    price_for=None,
) -> tuple[list[Action], dict[str, list]]:
    """Pick each node group's cheapest *feasible* shape by simulation.

    Replaces the division-based estimate. Division cannot see fragmentation, cannot
    see that a pod is larger than the candidate node, and cannot see anti-affinity
    -- all three change the answer, and two of them change it in the unsafe
    direction. Every option returned here has been packed pod by pod.
    """
    from rightsizer.core.config import Tunables
    from rightsizer.engine.simulate import scenarios

    out: list[Action] = []
    sweeps: dict[str, list] = {}
    headroom = Tunables().pack_headroom

    def price_of(t: str) -> float:
        # Injectable, like ladder_for, so a test prices from its fixture rather than the cloud.
        return price_for(t) if price_for else _node_price(snapshot, t, region, profile, discount)

    for g in groups:
        state = next((n for n in snapshot.nodegroups if n.name == g.name), None)
        config = snapshot.facts.config_for(g.name) or {}
        itype = (config.get("instance_types") or [""])[0]
        if not state or not itype or "." not in itype or state.nodes_current == 0:
            continue
        # Injectable so a test can compare this against scenarios() over the same
        # search space, and so unit tests need no network.
        ladder = (
            ladder_for(itype)
            if ladder_for
            else clouds.ladder_for(snapshot, config, itype, region=region, profile=profile)
        )

        # The same function the Scenarios view uses. There were two of these, and
        # the second silently missed the headroom fix -- so the two tabs proposed
        # different instance types for the same node group. One implementation is
        # the fix; a second one only ever drifts.
        options = scenarios(
            snapshot, state, ladder, recommendations or [],
            current_type=itype, price_of=price_of,
            max_pods=snapshot.facts.max_pods_for(g.name), headroom=headroom,
            min_size=config.get("min_size"),
        )
        if not options:
            continue
        sweeps[g.name] = options

        asis = next((o for o in options if o.key == "asis"), None)
        # Same rule as the advisor: an option whose local disks cannot hold what
        # the nodes store today is not a saving.
        candidates = [o for o in options if o.key != "asis" and o.feasible and o.nodes > 0
                      and (o.storage or {}).get("verdict") != "unsafe"
                      and (o.network or {}).get("verdict") != "unsafe"
                      and (o.load or {}).get("verdict") != "unsafe"
                      # Same rules as the advisor: headroom kept, and not
                      # priced as if someone else's blockers were fixed.
                      and (o.margin or {}).get("ok", True)
                      and not o.blocked]
        if not asis or not candidates:
            continue
        best = min(candidates, key=lambda o: (o.monthly_usd, o.nodes))
        delta = best.monthly_usd - asis.monthly_usd
        if delta >= -1.0:
            continue

        same_type = best.instance_type == itype
        owner = group_change(snapshot, config)
        plan = owner.retype_plan(config)
        if same_type:
            cmd = owner.scale_in_command(cluster, config, best.nodes)
            mech = f"(a) node scale-in {state.nodes_current} -> {best.nodes}"
            summary = f"{state.nodes_current} -> {best.nodes} x {itype}"
        else:
            cmd = _disk_comment(best.storage) + owner.retype_command(cluster, config, best.instance_type, best.nodes)
            # A Karpenter pool is not resized: a listed size is consolidated onto,
            # an unlisted one is offered (added to shapeConfigs).
            offered = best.instance_type in (plan.get("offered") or [])
            verb = ("consolidate onto" if offered
                    else "offer" if group_source(snapshot, config) != "cloud" else "resize")
            mech = f"(b) {verb} {state.nodes_current}x{itype} -> {best.nodes}x{best.instance_type}"
            summary = f"{state.nodes_current} x {itype} -> {best.nodes} x {best.instance_type}"
        if getattr(best, "fleet", None) and getattr(asis, "fleet", None):
            # A Karpenter pool: what it runs before and after, by size.
            def fleet_text(f):
                return ", ".join(f"{e['nodes']} x {e['type'].split(':')[-1]}"
                                 for e in sorted(f, key=lambda e: -e["nodes"]))
            summary = f"{fleet_text(asis.fleet)} -> {fleet_text(best.fleet)}"

        rejected = ", ".join(
            f"{o.instance_type} ({o.note or 'infeasible'})"
            for o in options
            if o.key != "asis" and not o.feasible and o.monthly_usd < best.monthly_usd
        ) or "none"

        out.append(
            Action(
                kind="scale-in" if same_type else "instance-downsize",
                subject=g.name,
                summary=summary,
                monthly_delta=delta,
                mechanism=mech,
                severity="routine",
                surface=plan["surface"],
                verified_by=(
                    f"packed {best.nodes} node(s) at {headroom:.0%} headroom, "
                    f"{best.binding} binding"
                ),
                command=cmd,
                evidence=[
                    f"scenario: {best.label}",
                    f"utilisation after: cpu {best.cpu_utilization:.0%}, "
                    f"mem {best.mem_utilization:.0%}; binding constraint {best.binding}",
                    f"packed to {headroom:.0%} of allocatable, leaving room for rolling-update "
                    "surge and for a container exceeding its observed peak",
                    f"cheaper options rejected as infeasible: {rejected}",
                ],
                blockers=g.blockers,
            )
        )
    return out, sweeps


def annotation_actions(snapshot: Snapshot) -> list[Action]:
    """Zero-cost unblockers: workloads CA refuses to evict.

    These carry no dollar figure of their own, but they gate every scale-in on the
    groups that host them -- a node running one of these is never removed.

    A Cluster Autoscaler rule (``--skip-nodes-with-local-storage``): Karpenter
    drains such pods, so under it there is nothing to unblock.
    """
    from rightsizer.adapters.autoscalers import scale_down_settings

    if not scale_down_settings(snapshot).skips_local_storage:
        return []
    stuck = [
        w
        for w in snapshot.facts.workload_constraints
        if (w.get("local_storage") and w.get("safe_to_evict") is not True)
    ]
    if not stuck:
        return []
    sample = stuck[:3]
    cmds = "\n".join(
        f"kubectl -n {w['namespace']} annotate {w['kind'].lower()} {w['name']} \\\n"
        f"  cluster-autoscaler.kubernetes.io/safe-to-evict=true"
        for w in sample
    )
    return [
        Action(
            kind="annotate",
            subject=f"{len(stuck)} workloads",
            summary=(
                f"{len(stuck)} workload(s) use emptyDir/hostPath without safe-to-evict; "
                "Cluster Autoscaler will not drain their nodes"
            ),
            monthly_delta=0.0,
            mechanism="(c) unblock CA drain",
            severity="correctness",
            surface="Chart",
            verified_by=f"{len(stuck)} workloads with emptyDir/hostPath, safe-to-evict unset",
            command=cmds + ("\n# ... and more; see the full list in the JSON report"
                            if len(stuck) > 3 else ""),
            evidence=[
                "this is usually why a node group sits at a flat node count",
                "verify each workload tolerates eviction before annotating",
            ],
        )
    ]


def capacity_impact(
    snapshot: Snapshot,
    groups: list[NodeGroupRecommendation],
    recommendations: list,
    *,
    region: str,
    profile: str | None,
    discount: float,
) -> dict[str, dict]:
    """Re-pack each node group under three request sets and price the difference.

    A memory increase is free until it forces another node. Asserting $0 without
    packing it is a guess; this measures it. Three scenarios per group:
    current requests, memory increases only, and every actionable recommendation.
    """
    from rightsizer.engine.simulate import pack, pods_for_nodegroup

    out: dict[str, dict] = {}
    for g in groups:
        state = next((n for n in snapshot.nodegroups if n.name == g.name), None)
        config = snapshot.facts.config_for(g.name) or {}
        itype = (config.get("instance_types") or [""])[0]
        if not state or not itype or state.nodes_current == 0:
            continue
        ladder = clouds.ladder_for(snapshot, config, itype, region=region, profile=profile)
        current = next((i for i in ladder if i.name == itype), None)
        if not current:
            continue
        oh_cpu = max(0.0, current.vcpu - state.alloc_cpu_per_node)
        oh_mem = max(0.0, current.memory_gib * 1024**3 - state.alloc_mem_per_node)
        cpu_a = current.vcpu - oh_cpu - state.ds_cpu_per_node
        mem_a = current.memory_gib * 1024**3 - oh_mem - state.ds_mem_per_node
        mp = snapshot.facts.max_pods_for(g.name) or 110
        if cpu_a <= 0 or mem_a <= 0:
            continue

        counts, demand, oversized = {}, {}, {}
        for mode in ("current", "memory", "all"):
            pods = pods_for_nodegroup(
                snapshot, g.name, apply=mode, recommendations=recommendations
            )
            result = pack(
                pods, cpu_per_node=cpu_a, mem_per_node=mem_a, max_pods_per_node=mp
            )
            counts[mode] = result.nodes_required
            demand[mode] = sum(p.memory for p in pods)
            # A pod larger than one whole node is set aside by the packer, not
            # placed. Reporting the node count without it understates capacity and
            # can read as >100% utilisation -- which is the tell that the answer
            # is not a packing at all.
            # One entry per *workload*, not per replica: a 40-replica workload
            # that does not fit is one finding, not forty identical ones.
            seen: dict[str, float] = {}
            for pod in result.unplaceable:
                seen[pod.workload] = max(seen.get(pod.workload, 0.0), pod.memory)
            oversized[mode] = sorted(seen.items(), key=lambda kv: -kv[1])

        price = _node_price(snapshot, itype, region, profile, discount)
        out[g.name] = {
            "instance_type": itype,
            "nodes": counts,
            "mem_delta_gib": (demand["memory"] - demand["current"]) / 1024**3,
            "memory_cost": (counts["memory"] - counts["current"]) * price,
            "all_cost": (counts["all"] - counts["current"]) * price,
            "capacity_gib": mem_a * counts["current"] / 1024**3,
            "node_capacity_gib": mem_a / 1024**3,
            "oversized": oversized,
            "feasible": not oversized["memory"],
            "utilisation_after": (
                demand["memory"] / (mem_a * counts["memory"]) if counts["memory"] else 0.0
            ),
        }
    return out


def memory_increase_actions(
    snapshot: Snapshot, impact: dict[str, dict] | None = None
) -> list[Action]:
    """Workloads running above their memory request: increases, not savings.

    Memory overshoot is an OOMKill, not throttling. These are reported with a
    positive delta of $0 at the node level -- raising a request costs nothing
    until it forces an extra node -- but they carry the highest severity, because
    a workload at 300% of its request is an outage waiting for a traffic spike.
    """
    impact = impact or {}
    total_cost = sum(v["memory_cost"] for v in impact.values())
    absorbed = [
        f"{name}: +{v['mem_delta_gib']:.1f}Gi absorbed, {v['nodes']['memory']} node(s) "
        f"unchanged, {v['utilisation_after']:.0%} memory used after"
        for name, v in sorted(impact.items())
        if v["mem_delta_gib"] > 0.5 and v["memory_cost"] == 0 and v.get("feasible", True)
    ]
    # Deduplicated across groups and capped: this is context attached to every
    # memory action, so an unbounded list is repeated once per action and
    # dominates the payload.
    seen_infeasible: dict[str, str] = {}
    for name, v in sorted(impact.items()):
        for w, m in v.get("oversized", {}).get("memory", []):
            seen_infeasible.setdefault(
                w,
                f"{name}: {w.split('/')[1]} needs {m / 1024**3:.1f}Gi but a "
                f"{v['instance_type']} node has only {v['node_capacity_gib']:.1f}Gi allocatable "
                "-- this workload requires a larger instance type, not just a bigger request",
            )
    infeasible = list(seen_infeasible.values())[:5]
    forced = [
        f"{name}: +{v['mem_delta_gib']:.1f}Gi forces "
        f"{v['nodes']['current']} -> {v['nodes']['memory']} node(s) "
        f"(+${v['memory_cost']:,.2f}/mo)"
        for name, v in sorted(impact.items())
        if v["memory_cost"] > 0
    ]

    out: list[Action] = []
    worst: dict[str, tuple] = {}
    for s in snapshot.usage:
        if s.resource != "mem" or s.request <= 0 or s.peak <= 0:
            continue
        ratio = s.peak / s.request
        if ratio < 1.0:
            continue
        key = f"{s.namespace}/{s.workload}"
        if key not in worst or ratio > worst[key][0]:
            worst[key] = (ratio, s)
    for key, (ratio, s) in sorted(worst.items(), key=lambda kv: -kv[1][0])[:15]:
        out.append(
            Action(
                kind="memory-increase",
                subject=key,
                summary=(
                    f"{s.container}: peak {s.peak / 1024**3:.2f}Gi vs request "
                    f"{s.request / 1024**3:.2f}Gi ({ratio:.0%} of request)"
                ),
                monthly_delta=0.0,
                mechanism="(d) memory request increase",
                severity="outage" if ratio >= 2.0 else "no-headroom",
                surface="Chart",
                verified_by=f"30d peak {ratio:.0%} of request"
                + (", OOMKilled observed" if s.oom_killed else ""),
                command=(
                    f"# raise memory request AND limit for {key} ({s.container})\n"
                    f"# request: {s.request / 1024**3:.2f}Gi -> "
                    f"{s.peak * 1.5 / 1024**3:.2f}Gi"
                ),
                evidence=[
                    f"peak {s.peak / 1024**3:.2f}Gi, p95 {s.p95 / 1024**3:.2f}Gi, "
                    f"replicas {s.replicas}",
                    "memory overshoot is an OOMKill, not throttling: this is a stability fix",
                ]
                + (
                    ["**simulation incomplete**: some increases do not fit any node "
                     "in this group"] if infeasible else
                    [f"simulated capacity impact of all memory increases: "
                     f"${total_cost:,.2f}/mo"] if total_cost else
                    ["simulated: absorbed by existing headroom, no extra node required"]
                )
                + [f"  - {a}" for a in absorbed[:5]]
                + [f"  - {a}" for a in forced[:5]]
                + [f"  - \u26a0 {a}" for a in infeasible],
            )
        )
    return out


def build_actions(
    snapshot: Snapshot,
    groups: list[NodeGroupRecommendation],
    *,
    cluster: str,
    region: str,
    profile: str | None = None,
    discount: float = 0.12,
    recommendations: list | None = None,
) -> list[Action]:
    kwargs: dict[str, Any] = dict(
        cluster=cluster, region=region, profile=profile, discount=discount
    )
    resize, sweeps = simulated_resize_actions(
        snapshot, groups, recommendations=recommendations, **kwargs
    )
    impact = capacity_impact(
        snapshot, groups, recommendations or [],
        region=region, profile=profile, discount=discount,
    )
    actions = resize + annotation_actions(snapshot)
    actions += memory_increase_actions(snapshot, impact)
    build_actions.last_impact = impact  # type: ignore[attr-defined]
    build_actions.last_sweeps = sweeps  # type: ignore[attr-defined]
    for a in actions:
        if a.prerequisite == "none" and a.prerequisites:
            a.prerequisite = "; ".join(a.prerequisites)
    return sorted(actions, key=lambda a: (a.blocked, -abs(a.monthly_delta)))


def render(actions: list[Action], discount: float) -> list[str]:
    ready = [a for a in actions if not a.blocked]
    blocked = [a for a in actions if a.blocked]
    total = sum(a.monthly_usd for a in ready)

    lines = [
        "## Actions",
        "",
        f"**{len(ready)} ready · ${total:,.0f}/month ceiling · "
        f"{len(blocked)} blocked**",
        "",
        f"> Dollars are a ceiling: list price less {discount:.0%} enterprise discount, with no",
        "> Savings Plan modelling. A node-hour freed against a commitment reduces the bill",
        "> only if other uncovered usage absorbs that commitment.",
        "",
    ]
    for n, a in enumerate(ready, 1):
        lines += [
            f"### {n}. {a.subject} — ${a.monthly_usd:,.0f}/mo",
            f"*{a.kind}* · {a.summary}",
            "",
            "```bash",
            a.command,
            "```",
        ]
        lines += [f"- {e}" for e in a.evidence]
        lines.append("")
    if blocked:
        lines += ["### Blocked", ""]
        for a in blocked:
            reasons = "; ".join(f"`{b.code}`" for b in a.blockers if b.severity != "data")
            lines.append(
                f"- **{a.subject}** ({a.kind}, ${a.monthly_usd:,.0f}/mo unrealisable) "
                f"— {reasons or 'see node group blockers'}"
            )
        lines.append("")
    return lines


def _disk_comment(storage: dict | None) -> str:
    """One line on what the move does to the nodes' local disk."""
    if not storage:
        return ""
    line = f"# local disk: {storage.get('diskBefore')} -> {storage.get('diskAfter')}"
    before, after = storage.get("before") or {}, storage.get("after") or {}
    if before.get("hotShare") is not None and after.get("hotShare") is not None:
        line += f"; fullest node {before['hotShare']:.0%} -> {after['hotShare']:.0%} of its disk"
    elif storage.get("verdict") == "unknown":
        line += "; usage not measured"
    return line + "\n"
