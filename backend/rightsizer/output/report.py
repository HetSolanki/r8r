"""Report rendering, following the established action-plan contract.

Three properties the layout enforces:

* **Every action appears exactly once, signed.** Reductions negative, increases
  positive, correctness fixes zero. The subtotals must sum to NET, and NET must
  equal current run-rate minus projected.
* **Every node group appears in exactly one row** of the disposition table,
  including groups with no action and groups with zero nodes. A group that is
  silently absent reads as overlooked, and the reasons for *not* touching
  something are as valuable as the reasons for touching it.
* **Dollars are labelled as a ceiling.** List price less the enterprise discount,
  no Savings Plan modelling.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from rightsizer.analysis.actions import Action
from rightsizer.core.models import ContainerRecommendation, NodeGroupRecommendation, Snapshot
from rightsizer.adapters import clouds
from rightsizer.core.stats import fmt_bytes, fmt_cores

SEVERITY_ICON = {
    "outage": "\U0001F534 **outage now**",
    "no-headroom": "\U0001F7E0 no headroom",
    "correctness": "\U0001F7E1 correctness",
    "routine": "\U0001F7E2 routine",
}


def _node_monthly(snapshot: Snapshot, itype: str, region: str, profile: str | None,
                  discount: float) -> float:
    if not itype:
        return 0.0
    return clouds.for_snapshot(snapshot, region=region, profile=profile).monthly_price(itype, discount)


def fleet_runrate(
    snapshot: Snapshot, region: str, profile: str | None, discount: float
) -> float:
    total = 0.0
    for group in snapshot.nodegroups:
        config = snapshot.facts.config_for(group.name) or {}
        itype = (config.get("instance_types") or [""])[0]
        total += _node_monthly(snapshot, itype, region, profile, discount) * group.nodes_current
    return total


STRUCTURAL_ZERO = frozenset({"hpa-utilization-target", "pod-density-bound"})


def disposition(
    group, rec: NodeGroupRecommendation | None, action=None
) -> tuple[str, str, str]:
    """(verdict, scale-in: "yes" | "no" | "n/a", why) for one node group.

    Must agree with the cost summary: a group carrying a ready action is scale-in
    recommended, whatever prerequisites that action has. Only a *structurally
    zero* blocker makes a group un-actionable. Deriving this independently from
    the action list is how the two tables drifted apart and contradicted each
    other -- which is also why the console export calls this rather than
    re-deriving it.
    """
    if action is not None and not action.blocked:
        prereq = "; ".join(action.prerequisites)
        return (
            "OVERPROVISIONED",
            "yes",
            f"{action.summary} → ${abs(action.monthly_delta):,.0f}/mo"
            + (f". Prerequisite: {prereq}" if prereq else ""),
        )
    if group.nodes_current == 0:
        return ("—", "n/a", "Empty; costs $0. Latent spend if max_size is large.")

    cpu_util = (
        group.workload_cpu_requests / group.allocatable_cpu * 100
        if group.allocatable_cpu else 0.0
    )
    mem_util = (
        group.workload_mem_requests / group.allocatable_mem * 100
        if group.allocatable_mem else 0.0
    )
    if rec is None:
        return ("UNKNOWN", "n/a", "No recommendation produced.")

    hard = [b for b in rec.blockers if b.code in STRUCTURAL_ZERO]
    if rec.nodes_target < rec.nodes_avg_30d and not hard:
        return (
            "OVERPROVISIONED",
            "yes",
            f"Target {rec.nodes_target}; binding constraint {rec.binding_constraint}",
        )
    if hard:
        codes = ", ".join(b.code for b in hard[:2])
        return (
            "REDUCIBLE_BUT_BLOCKED" if rec.nodes_target < rec.nodes_avg_30d else "CORRECTLY_SIZED",
            "no",
            f"Blocked: {codes}",
        )
    if max(cpu_util, mem_util) >= 50:
        return ("CORRECTLY_SIZED", "no", "CA blocked: utilization ≥ 50% threshold")
    return ("CORRECTLY_SIZED", "no", f"At floor ({rec.binding_constraint})")


_MARKER = {"yes": "✅ **Yes**", "no": "❌ **No**", "n/a": "➖ **N/A**"}


def _verdict(group, rec: NodeGroupRecommendation | None, action=None) -> tuple[str, str, str]:
    """:func:`disposition`, formatted for the markdown table."""
    verdict, scale_in, why = disposition(group, rec, action)
    if why.startswith("Blocked: "):
        why = "Blocked: " + ", ".join(f"`{c}`" for c in why[len("Blocked: "):].split(", "))
    return verdict, _MARKER[scale_in], why


def summarise(actions: list[Action], current: float) -> dict[str, float | int]:
    """The cost summary, computed once for the markdown report and the console.

    Ready actions only: a blocked action contributes nothing to NET, because
    acting on it would not produce the saving. It is totalled separately so the
    amount set aside is visible rather than silently dropped.
    """
    ready = [a for a in actions if not a.blocked]
    savings = [a for a in ready if a.category == "saving"]
    increases = [a for a in ready if a.category == "increase"]
    blocked = [a for a in actions if a.blocked]
    net = sum(a.monthly_delta for a in savings) + sum(a.monthly_delta for a in increases)
    return {
        "reductions": round(sum(a.monthly_delta for a in savings), 2),
        "reductionsCount": len(savings),
        "increases": round(sum(a.monthly_delta for a in increases), 2),
        "increasesCount": len(increases),
        "neutralCount": sum(1 for a in ready if a.category == "neutral"),
        "net": round(net, 2),
        "netAnnual": round(net * 12, 2),
        "currentRunRate": round(current, 2),
        "projectedRunRate": round(current + net, 2),
        "netPctOfSpend": round(net / current * 100, 1) if current else 0.0,
        "blocked": round(sum(abs(a.monthly_delta) for a in blocked), 2),
        "blockedCount": len(blocked),
    }


def to_markdown(
    snapshot: Snapshot,
    groups: list[NodeGroupRecommendation],
    recs: list[ContainerRecommendation],
    actions: list[Action] | None = None,
    discount: float = 0.12,
    *,
    region: str = "us-east-1",
    profile: str | None = None,
    account: str = "",
) -> str:
    actions = actions or []
    savings = [a for a in actions if a.category == "saving" and not a.blocked]
    increases = [a for a in actions if a.category == "increase" and not a.blocked]
    neutral = [a for a in actions if a.category == "neutral" and not a.blocked]
    blocked = [a for a in actions if a.blocked]

    s = summarise(actions, fleet_runrate(snapshot, region, profile, discount))
    sav_mo, inc_mo, net_mo = s["reductions"], s["increases"], s["net"]
    current, projected, pct = s["currentRunRate"], s["projectedRunRate"], s["netPctOfSpend"]

    from rightsizer.adapters import autoscalers

    cloud = clouds.for_snapshot(snapshot, region=region, profile=profile)
    autoscaler, found = autoscalers.for_snapshot(snapshot)
    metrics = (snapshot.meta or {}).get("metrics") or {}
    store = "Prometheus" if metrics.get("env_label") == "" else "Thanos"
    threshold = autoscaler.scale_down_settings(found).utilization_threshold
    L: list[str] = [
        f"# Right-Sizing & Capacity Action Plan (`{snapshot.env}`)",
        "",
        f"> **Cluster:** `{snapshot.env}` · **Cloud:** {cloud.title} · **Region:** `{region}`"
        + (f" · **Account:** `{account}`" if account else ""),
        f"> **Lookback:** {snapshot.window_days} days ({store} PromQL) + point-in-time snapshot "
        f"(`kubectl` + {cloud.title} API)"
        + (f" · **only {snapshot.window_days} of the {snapshot.requested_window_days} requested "
           "days of history exist; every recommendation is sized from that shorter window**"
           if snapshot.window_shortened else ""),
        f"> **Generated:** {snapshot.collected_at} · **Autoscaler:** {autoscaler.title}"
        + ("" if autoscaler.models_scale_down else " (scale-down approximated)")
        + f" · **scale-down threshold:** `{threshold:.1%}` · **Discount applied:** `{discount:.0%}`",
        f"> **NET CHANGE:** **${net_mo:,.2f}/month (${net_mo * 12:,.2f}/year)** — "
        f"reductions **${sav_mo:,.2f}**, increases **+${inc_mo:,.2f}**",
        "> **Execution Mode:** `STRICT READ-ONLY ANALYSIS` — no cluster mutations were performed.",
        "> **Savings Plans are not modelled:** these are list-price ceilings. A node-hour freed",
        "> against a commitment reduces the bill only if other uncovered usage absorbs it.",
        "",
        "---",
        "",
        "## Cost Summary — every action, signed",
        "",
        "Negative = reduction, positive = increase, `$0.00` = corrected inside existing headroom.",
        "Ordered by absolute impact.",
        "",
        "| # | Action | Target | Type | Mechanism | Monthly Δ | Annual Δ |",
        "| ---: | --- | --- | :---: | --- | ---: | ---: |",
    ]
    ranked = sorted(
        [a for a in actions if not a.blocked], key=lambda a: -abs(a.monthly_delta)
    )
    for n, a in enumerate(ranked, 1):
        d = a.monthly_delta
        money = "$0.00" if d == 0 else f"{'−' if d < 0 else '+'}${abs(d):,.2f}"
        annual = "$0.00" if d == 0 else f"{'−' if d < 0 else '+'}${abs(d) * 12:,.2f}"
        L.append(
            f"| {n} | {a.summary} | `{a.subject}` | {a.icon} | {a.mechanism} | "
            f"**{money}** | **{annual}** |"
        )

    L += [
        "",
        "| | Monthly | Annual |",
        "| --- | ---: | ---: |",
        f"| **Total reductions** (×{len(savings)}) | **−${abs(sav_mo):,.2f}** | "
        f"**−${abs(sav_mo) * 12:,.2f}** |",
        f"| **Total increases** (×{len(increases)}) | **+${inc_mo:,.2f}** | "
        f"**+${inc_mo * 12:,.2f}** |",
        f"| **Neutral / correctness** (×{len(neutral)}) | $0.00 | $0.00 |",
        f"| **NET CHANGE** | **${net_mo:,.2f}** | **${net_mo * 12:,.2f}** |",
        f"| Current fleet run-rate | ${current:,.2f} | — |",
        f"| **Projected fleet run-rate** | **${projected:,.2f}** | — |",
        f"| Net as % of current spend | **{pct:.1f}%** | — |",
        "",
    ]
    if blocked:
        lost = sum(abs(a.monthly_delta) for a in blocked)
        L += [
            f"**Excluded: ${lost:,.2f}/month identified but blocked** "
            f"({len(blocked)} action(s)). Listed under *Blocked* below; these contribute "
            "nothing to NET because acting on them would not produce the saving.",
            "",
        ]

    L += ["---", "", "## Action Summary — what to do, in what order", ""]
    L += [
        "| # | Action | Target | Type | Severity | Surface | Prerequisite | Verified by |",
        "| ---: | --- | --- | :---: | :---: | --- | --- | --- |",
    ]
    for n, a in enumerate(ranked, 1):
        L.append(
            f"| {n} | {a.summary} | `{a.subject}` | {a.icon} | "
            f"{SEVERITY_ICON.get(a.severity, a.severity)} | {a.surface} | "
            f"{a.prerequisite} | {a.verified_by} |"
        )
    L.append("")

    if blocked:
        L += ["### Blocked", "", "| Action | Target | Would save | Blocked by |",
              "| --- | --- | ---: | --- |"]
        for a in blocked:
            codes = ", ".join(f"`{b.code}`" for b in a.effective_blockers) or "see node group"
            L.append(
                f"| {a.summary} | `{a.subject}` | ${abs(a.monthly_delta):,.2f}/mo | {codes} |"
            )
        L.append("")

    # Node groups with zero nodes never appear in kube_node_labels, so Prometheus
    # cannot see them at all. They must still be reconciled against the cloud's list:
    # a group that is silently absent reads as overlooked, and an empty group with
    # a large max_size is latent spend, not nothing.
    seen = {g.name for g in snapshot.nodegroups}
    configured = {c["name"] for c in snapshot.facts.nodegroup_configs}
    empty_only = sorted(configured - seen)
    total_groups = len(seen | configured)

    L += ["---", "", "## Complete Node Group Disposition", "",
          f"{cloud.title} and the autoscaler list **{len(configured) or 'unknown'}** group(s); "
          f"Prometheus sees **{len(seen)}** (groups with zero nodes have no node labels). "
          f"This table has **{total_groups}** rows — every group appears exactly once.", "",
          "| Node Group | Nodes | min/des/max | CA util (CPU/RAM) | 30d verdict | Scale-In? | "
          "Disposition & Why |",
          "| --- | ---: | ---: | ---: | --- | :---: | --- |"]
    by_name = {g.name: g for g in groups}
    for group in sorted(snapshot.nodegroups, key=lambda g: -g.nodes_current):
        rec = by_name.get(group.name)
        config = snapshot.facts.config_for(group.name) or {}
        bounds = (
            f"{config.get('min_size', '?')}/{config.get('desired_size', '?')}/"
            f"{config.get('max_size', '?')}"
        )
        cpu_util = (
            group.workload_cpu_requests / group.allocatable_cpu * 100
            if group.allocatable_cpu else 0.0
        )
        mem_util = (
            group.workload_mem_requests / group.allocatable_mem * 100
            if group.allocatable_mem else 0.0
        )
        action = next(
            (a for a in actions if a.subject == group.name and a.monthly_delta < 0), None
        )
        verdict, marker, why = _verdict(group, rec, action)
        # Nodes that accept no pods still count as capacity above; say so beside it.
        counts = ((group.nodes_not_ready, "NotReady"), (group.nodes_cordoned, "cordoned"))
        health = [f"{n} {label}" for n, label in counts if n]
        nodes = f"{group.nodes_current} ({', '.join(health)})" if health else group.nodes_current
        L.append(
            f"| `{group.name}` | {nodes} | {bounds} | "
            f"{cpu_util:.1f}% / {mem_util:.1f}% | {verdict} | {marker} | {why} |"
        )
    latent_total = 0.0
    for name in empty_only:
        config = next(c for c in snapshot.facts.nodegroup_configs if c["name"] == name)
        itype = (config.get("instance_types") or [""])[0]
        maxs = int(config.get("max_size", 0))
        ceiling = _node_monthly(snapshot, itype, region, profile, discount) * maxs
        latent_total += ceiling
        L.append(
            f"| `{name}` | 0 | {config.get('min_size')}/{config.get('desired_size')}/{maxs} "
            f"| — | EMPTY | \u2796 **N/A** | Costs $0 today. `max_size={maxs}` x `{itype}` "
            f"= **${ceiling:,.0f}/mo** latent if anything schedules here "
            f"({config.get('capacity_type', '')}) |"
        )

    L += ["", "**Legend:** ✅ scale-in recommended · ❌ cannot or should not · "
          "➖ no nodes.", ""]
    if empty_only:
        L += [
            f"### Empty node groups ({len(empty_only)})",
            "",
            f"These cost **$0 today** and are therefore not savings. They are not nothing "
            f"either: their combined `max_size` ceiling is **${latent_total:,.0f}/month** of "
            "latent spend if anything schedules onto them. Review the ceilings rather than "
            "the current cost.",
            "",
        ]

    L += _ceiling_section(snapshot, recs)

    if snapshot.warnings:
        L += ["---", "", "## Data warnings", ""] + [f"- {w}" for w in snapshot.warnings] + [""]

    L += ["---", "", "## Commands", ""]
    for n, a in enumerate(ranked, 1):
        L += [f"**{n}. {a.subject}** — {a.summary}", "", "```bash", a.command, "```"]
        L += [f"- {e}" for e in a.evidence] + [""]

    L += _container_appendix(recs)
    return "\n".join(L)


def _container_appendix(recs: list[ContainerRecommendation]) -> list[str]:
    actionable = [r for r in recs if r.actionable]
    if not actionable:
        return []
    L = ["---", "", "## Appendix — container requests", "",
         "Per-replica columns size the request; **fleet** columns are all replicas together.", "",
         "| Workload | Container | Repl | CPU req/rep | → | Fleet reserved | Mem req/rep | → |",
         "| :-- | :-- | --: | --: | --: | --: | --: | --: |"]
    for m in sorted(actionable, key=lambda r: -(r.cpu_request * max(r.replicas, 1)))[:40]:
        n = max(m.replicas, 1)
        L.append(
            f"| `{m.namespace}/{m.workload}` | {m.container} | {m.replicas} | "
            f"{fmt_cores(m.cpu_request)} | "
            f"{fmt_cores(m.cpu_recommended) if m.cpu_recommended is not None else '—'} | "
            f"{fmt_cores(m.cpu_request * n)} | {fmt_bytes(m.mem_request)} | "
            f"{fmt_bytes(m.mem_recommended) if m.mem_recommended is not None else '—'} |"
        )
    return L + [""]


def to_json(
    snapshot: Snapshot,
    groups: list[NodeGroupRecommendation],
    recs: list[ContainerRecommendation],
    actions: list[Action] | None = None,
) -> str:
    actions = actions or []
    ready = [a for a in actions if not a.blocked]
    return json.dumps(
        {
            "env": snapshot.env,
            "collected_at": snapshot.collected_at,
            "window_days": snapshot.window_days,
            "requested_window_days": snapshot.requested_window_days or snapshot.window_days,
            "totals": {
                "reductions_monthly": round(
                    sum(a.monthly_delta for a in ready if a.monthly_delta < 0), 2
                ),
                "increases_monthly": round(
                    sum(a.monthly_delta for a in ready if a.monthly_delta > 0), 2
                ),
                "net_monthly": round(sum(a.monthly_delta for a in ready), 2),
                "blocked_monthly": round(
                    sum(abs(a.monthly_delta) for a in actions if a.blocked), 2
                ),
                "containers_actionable": sum(1 for r in recs if r.actionable),
                "containers_blocked": sum(1 for r in recs if r.blockers),
            },
            "actions": [asdict(a) for a in actions],
            "nodegroups": [asdict(g) for g in groups],
            "containers": [asdict(r) for r in recs],
            "warnings": snapshot.warnings,
        },
        indent=2,
        default=str,
    )


def _ceiling_section(snapshot, recs) -> list[str]:
    """HPAs held at maxReplicas: the risk a floor recommendation does not show."""
    from rightsizer.engine.simulate import replica_ceilings

    ceilings = replica_ceilings(snapshot, recs)
    if not ceilings:
        return []
    L = ["---", "", "## HPA ceilings — maxReplicas the load reaches", "",
         "Pinned at its ceiling, an HPA stops adding pods while load keeps rising: the "
         "ready pods run hotter until latency or an OOM says so. Raising `maxReplicas` "
         "costs nothing until the load needs it.", "",
         "| HPA | maxReplicas | Suggested | When | Why |", "|---|--:|--:|---|---|"]
    for c in sorted(ceilings, key=lambda c: (c.when != "now", -max(c.pinned_minutes, c.capped_now))):
        when = "today" if c.when == "now" else "after the request cut"
        L.append(f"| `{c.key.rsplit('/', 1)[0]}` ({c.hpa}) | {c.current_max} | **{c.suggested_max}** "
                 f"| {when} | {c.basis} |")
    return L + [""]
