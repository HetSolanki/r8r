"""Turning a search into a recommendation.

The scenario search answers "what shapes are possible". That is an input to a
decision, not the decision. A list of thirty options ranked by dollars asks the
reader to do the reasoning the tool should have done -- and quietly buries the
one-step config change under a three-step migration worth 20% more.

This module decides. For each node group it works out what is actually wrong --
if anything -- and names the single thing to do about it, with the reasoning
visible and the runners-up kept only where they represent a genuinely different
trade-off.

Two principles:

* **Effort is part of the answer.** Lowering a node group's minimum is one line
  of config. Replacing a node group means creating it, draining the old one and
  deleting it. A recommendation that ignores the difference is not ranking
  options, it is ranking numbers.
* **Stability findings are not optional and are not savings.** A container
  running above its request is a pending OOMKill; it belongs at the top of the
  group's report whether or not anything can be saved there.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# A cheaper option must beat the simpler one by this much to be worth the extra
# work. Below it, the difference is not worth a migration.
MATERIAL_GAIN = 0.20
# Savings below this share of a group's spend are noise, not a recommendation.
MIN_RELEVANT_SHARE = 0.02
# How far a group's demand ratio must sit from its instance's before the family
# is considered the wrong shape rather than merely imperfect.
SHAPE_MISMATCH = 1.6


@dataclass
class Problem:
    kind: str          # oom-risk | exceeds-request | no-request | drain-blocked
    subject: str
    detail: str
    severity: str      # critical | serious | warning


KIND_SUMMARY = {
    "oom-risk": "OOMKilled",
    "exceeds-request": "over their memory request",
    "no-request": "with no requests declared",
    "drain-blocked": "blocking node drain",
}
# How many findings to name. Beyond this they are a count, not a list: a group
# with ninety-seven of them has one systemic problem, and printing every row
# makes that harder to see rather than easier.
NAMED_FINDINGS = 3


@dataclass
class Advice:
    group: str
    verdict: str           # act | watch | healthy | blocked
    headline: str
    reason: str
    scenario_key: str | None = None
    monthly_delta: float = 0.0
    steps: list[str] = field(default_factory=list)
    alternatives: list[dict] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    # {kind: count} across every finding, so the shape of the problem is visible
    # without reading the list.
    finding_counts: dict = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)

    def summarise_findings(self) -> str:
        """One line: what is wrong here, by kind and count."""
        parts = [
            f"{n} {KIND_SUMMARY.get(kind, kind)}"
            for kind, n in sorted(self.finding_counts.items(), key=lambda kv: -kv[1])
            if n
        ]
        return " · ".join(parts)


def _problems(snapshot, group_name: str, recommendations) -> list[Problem]:
    """Correctness and stability findings on this group, worst first.

    These are surfaced whether or not the group has any saving available, because
    "nothing to save here" and "nothing wrong here" are different statements and
    conflating them is how an OOM risk goes unread.
    """
    out: list[Problem] = []
    for r in recommendations:
        if r.nodegroup != group_name:
            continue
        stat_mem = next(
            (s for s in snapshot.usage
             if s.resource == "mem" and (s.namespace, s.workload, s.kind, s.container)
             == (r.namespace, r.workload, r.kind, r.container)),
            None,
        )
        stat_cpu = next(
            (s for s in snapshot.usage
             if s.resource == "cpu" and (s.namespace, s.workload, s.kind, s.container)
             == (r.namespace, r.workload, r.kind, r.container)),
            None,
        )
        subject = f"{r.namespace}/{r.workload}:{r.container}"

        # Performance findings, which cost money to fix rather than save it.
        # A tool that reports only the savings is a tool whose recommendations
        # you stop trusting the first time one of these bites.
        if stat_cpu and stat_cpu.limit > 0 and stat_cpu.throttle_pct >= 0.01:
            out.append(Problem(
                "cpu-throttled", subject,
                f"throttled in {stat_cpu.throttle_pct:.1%} of its CFS periods against a "
                f"{stat_cpu.limit * 1000:.0f}m limit -- the kernel is clipping this "
                "container, and its usage looks healthy only because of that",
                "serious" if stat_cpu.throttle_pct >= 0.05 else "warning",
            ))
        elif (stat_cpu and r.cpu_request > 0
              and r.cpu_recommended is not None and r.cpu_recommended > r.cpu_request):
            out.append(Problem(
                "cpu-below-use", subject,
                f"p95 is {stat_cpu.p95 * 1000:.0f}m against a {r.cpu_request * 1000:.0f}m "
                f"request -- it runs on CPU borrowed from neighbours, and loses it "
                "first when the node is contended",
                "warning",
            ))

        if stat_mem and stat_mem.oom_killed:
            out.append(Problem(
                "oom-risk", subject,
                f"OOMKilled in the window; peak {stat_mem.peak / 1024**3:.2f}Gi against a "
                f"{r.mem_request / 1024**3:.2f}Gi request",
                "critical",
            ))
        elif stat_mem and r.mem_request > 0 and stat_mem.peak > r.mem_request:
            out.append(Problem(
                "exceeds-request", subject,
                f"memory peaked at {stat_mem.peak / r.mem_request:.0%} of its request "
                f"({stat_mem.peak / 1024**3:.2f}Gi vs {r.mem_request / 1024**3:.2f}Gi) -- "
                "the next spike is an OOMKill, not a throttle",
                "serious",
            ))
        if r.cpu_request <= 0 and r.mem_request <= 0:
            out.append(Problem(
                "no-request", subject,
                "no requests declared: invisible to the scheduler and to the autoscaler, "
                "which may remove the node while it is working",
                "warning",
            ))
    rank = {"critical": 0, "serious": 1, "warning": 2}
    # One finding per subject: a workload appears once per container, and the
    # same container can produce both a memory finding and a no-request one.
    seen: dict[str, Problem] = {}
    for problem in sorted(out, key=lambda p: rank[p.severity]):
        seen.setdefault(problem.subject, problem)
    return sorted(seen.values(), key=lambda p: rank[p.severity])


def _verdict_for(problems: list[Problem], has_action: bool) -> str:
    """A group with nothing to save is not therefore healthy.

    Cost and correctness are separate axes. A group that is priced correctly and
    running containers above their memory requests needs attention, and calling
    it healthy is how that goes unread.
    """
    worst = {p.severity for p in problems}
    if has_action:
        return "act"
    if "critical" in worst or "serious" in worst:
        return "watch"
    return "healthy"


def _shape(group_state, scenarios) -> tuple[str, float, float]:
    """Is the instance family the right shape for what this group runs?

    Compares GiB-per-core of actual demand against GiB-per-vCPU of the instance.
    A memory-heavy group on a general-purpose family is paying for cores it
    cannot use, and the reverse for a CPU-heavy one.
    """
    asis = next((s for s in scenarios if s.key == "asis"), None)
    if not asis or not asis.vcpu:
        return "", 0.0, 0.0
    cpu = group_state.workload_cpu_requests
    mem = group_state.workload_mem_requests / 1024**3
    if cpu <= 0:
        return "", 0.0, 0.0
    demand = mem / cpu
    instance = (asis.mem_gib or 0) / asis.vcpu
    if instance <= 0:
        return "", demand, 0.0
    if demand > instance * SHAPE_MISMATCH:
        return "memory-heavy", demand, instance
    if demand * SHAPE_MISMATCH < instance:
        return "cpu-heavy", demand, instance
    return "", demand, instance


def advise(snapshot, group_state, scenarios, recommendations, *, min_size=None,
           edits: dict[str, int] | None = None) -> Advice:
    """The one thing to do about this node group, why -- and the other routes.

    ``edits`` is how many individual changes each option makes (requests, HPA
    floors, minSize, a node-group replacement), by option key. It breaks ties
    between options of equal saving and steps: 8 HPA floors is less to ship
    than 25 request edits, though both are "two steps".
    """
    edits = edits or {}
    name = group_state.name
    all_problems = _problems(snapshot, name, recommendations or [])
    counts: dict[str, int] = {}
    for problem in all_problems:
        counts[problem.kind] = counts.get(problem.kind, 0) + 1
    # Keep a few worst examples; the counts carry the rest.
    problems = all_problems[:NAMED_FINDINGS]
    asis = next((s for s in scenarios if s.key == "asis"), None)
    # An option whose disks cannot hold what the nodes store today is not a
    # saving: kubelet evicts pods once the disk passes 85%, and losing the local
    # NVMe moves images and logs onto a root volume sized for the OS.
    options = [s for s in scenarios if s.key != "asis" and s.direction == "saves"
               and (getattr(s, "storage", None) or {}).get("verdict") != "unsafe"
               # Nor one whose nodes cannot carry today's traffic past their credits.
               and (getattr(s, "network", None) or {}).get("verdict") != "unsafe"
               # Nor one whose nodes would run out of memory at measured load.
               and (getattr(s, "load", None) or {}).get("verdict") != "unsafe"]
    # An option priced as if CA's drain blockers were fixed is not the pick --
    # the fix is a change to someone else's workload, and some of those
    # annotations are there on purpose -- but it is offered beside the pick
    # whenever it saves more, so the saving the blockers hold back is visible.
    # Nor one that leaves the nodes without headroom: fitting at 96% of the
    # memory is not a recommendation, whatever it saves. The search offers the
    # same change on enough nodes beside it.
    options = [s for s in options if (getattr(s, "margin", None) or {}).get("ok", True)]
    unblockable = [s for s in options if getattr(s, "blocked", None)]
    options = [s for s in options if not getattr(s, "blocked", None)]
    shape, demand_ratio, instance_ratio = _shape(group_state, scenarios)
    floor = int(min_size or 0)
    floor_bound = any("minsize" in s.label.lower() for s in options)

    diagnostics = {
        "binding": asis.binding if asis else "",
        "gibPerCoreDemanded": round(demand_ratio, 2),
        "gibPerCoreOffered": round(instance_ratio, 2),
        "shapeMismatch": shape,
        "floorBound": floor_bound,
        "minSize": floor,
        "tight": bool(asis and "tight" in (asis.rationale or "")),
    }

    if not options:
        verdict = _verdict_for(problems, has_action=False)
        held = _best_unblocked(unblockable, None, spend=asis.monthly_usd if asis else 0.0,
                               edits=edits)
        headline = (
            "Priced correctly; stability findings need attention"
            if verdict == "watch" else "Correctly sized"
        )
        reason = (
            f"Every alternative costs more than the {group_state.nodes_current} "
            f"{asis.instance_type if asis else 'node'}s it runs today"
            + (", and the nodes are already above the headroom target"
               if diagnostics["tight"] else "")
            + "."
        )
        if held:
            reason += (f" Cluster Autoscaler's drain blockers hold back "
                       f"{_money(held['monthlyDelta']).lstrip('−+')} -- see the option below.")
        return Advice(name, verdict, headline, reason, problems=problems,
                      finding_counts=counts, diagnostics=diagnostics,
                      alternatives=[held] if held else [])

    spend = asis.monthly_usd if asis else 0.0
    relevant = [
        s for s in options
        if spend <= 0 or abs(s.delta_usd) / spend >= MIN_RELEVANT_SHARE
    ]
    if not relevant:
        verdict = _verdict_for(problems, has_action=False)
        held = _best_unblocked(unblockable, None, spend=spend, edits=edits)
        return Advice(
            name, verdict,
            "Priced correctly; stability findings need attention"
            if verdict == "watch" else "Correctly sized",
            f"The best alternative saves under {MIN_RELEVANT_SHARE:.0%} of this "
            "group's spend, which is not worth the change."
            + (f" {sum(counts.values())} workload(s) need a request change regardless."
               if counts else ""),
            problems=problems, finding_counts=counts, diagnostics=diagnostics,
            alternatives=[held] if held else [],
        )

    # Best option at each effort level, so the comparison is "what does one more
    # step buy" rather than "the cheapest-to-do thing versus the best thing" --
    # which produces nonsense like "1321% more" when the one-step option happened
    # to be a poor one.
    by_tier: dict[int, object] = {}
    for option in sorted(relevant, key=lambda s: (s.delta_usd, edits.get(s.key, 0))):
        by_tier.setdefault(len(option.requires), option)
    tiers = [by_tier[k] for k in sorted(by_tier)]
    by_effort = tiers
    simplest = tiers[0]
    richest = min(relevant, key=lambda s: (s.delta_usd, edits.get(s.key, 0)))

    chosen, reason = simplest, ""
    if richest.key != simplest.key:
        gain = (abs(richest.delta_usd) - abs(simplest.delta_usd)) / max(abs(simplest.delta_usd), 1)
        if gain >= MATERIAL_GAIN:
            chosen = richest
            reason = (
                f"Worth the extra work: it saves {gain:.0%} more than the simpler "
                f"option ({_money(simplest.delta_usd)} in {len(simplest.requires)} step"
                f"{'' if len(simplest.requires) == 1 else 's'})."
            )
        else:
            reason = (
                f"The cheapest option to carry out. Saving more is possible "
                f"({_money(richest.delta_usd)}) but only {gain:.0%} better for "
                f"{len(richest.requires)} steps, which does not justify a migration."
            )

    if not reason:
        reason = "The only option worth acting on."
    # Only explain the shape when the recommendation actually acts on it. Saying
    # a group is memory-heavy and then proposing a compute-optimised node reads
    # as a contradiction, and one of the two statements is wrong.
    # Judged by GiB per vCPU, which is what a memory-heavy group needs more
    # of: on EC2 that is the r family, on a Flex shape a higher GB-per-OCPU
    # rung. The pick must move the ratio the way the demand says.
    chosen_ratio = (chosen.mem_gib or 0) / chosen.vcpu if chosen.vcpu else 0.0
    if shape == "memory-heavy" and chosen_ratio <= instance_ratio * 1.05:
        shape = ""
    if shape == "cpu-heavy" and chosen_ratio >= instance_ratio / 1.05:
        shape = ""
    if shape == "memory-heavy":
        reason += (
            f" This group demands {demand_ratio:.1f}GiB per core against the "
            f"{instance_ratio:.0f}GiB its instance offers, so a shape with more memory "
            "per core fits it better."
        )
    elif shape == "cpu-heavy":
        reason += (
            f" This group demands only {demand_ratio:.1f}GiB per core against the "
            f"{instance_ratio:.0f}GiB its instance offers -- it is paying for memory "
            "it does not use."
        )

    # The other routes, one per kind of change: a reader rarely ships the pick
    # exactly, and "what if we only touch the HPAs" deserves an answer rather
    # than a missing row. The best safe option on each route, never the pick.
    alternatives = []
    for family, best in _best_per_family(relevant, edits):
        if best.key == chosen.key:
            continue
        alternatives.append({
            "key": best.key, "label": best.label, "monthlyDelta": best.monthly_usd - spend,
            "steps": len(best.requires), "family": family,
            "why": _why_family(best, chosen, edits),
        })
    # Compared with the pick, and with the best that stays on today's nodes:
    # when the pick is a migration, fixing a blocker is often the alternative
    # to it, and worth seeing even if it saves less than moving.
    staying = min((s for s in relevant if family_of(s) != "newtype"),
                  key=lambda s: s.delta_usd, default=None)
    held = _best_unblocked(unblockable, chosen, spend=spend, edits=edits, staying=staying)
    if held:
        alternatives.append(held)

    # A cost ranking cannot see that the cheapest row leaves a stability problem
    # standing. On services-1 the recommendation is "reduce HPA minReplicas" --
    # correct, cheapest, one step -- and it applies none of the memory increases
    # the OOM findings on that group exist to fix. The option that does both is
    # more expensive and takes more steps, so it loses on every axis the ranking
    # measures, and would never appear. It is surfaced here for the reason it is
    # actually worth seeing.
    at_risk = counts.get("oom-risk", 0) + counts.get("exceeds-request", 0)
    if at_risk and not _applies_requests(chosen):
        # Searched over every scenario, not `options`. That list keeps only
        # those whose direction is "saves", and the fix for an at-risk container
        # is a memory *increase* -- free at best, and often a cost. On
        # prod-eu1/db-1 it is exactly $0, which is the option a reader most wants
        # to see: five containers made safe for nothing.
        candidates = [
            s for s in scenarios
            if s.key != "asis" and s.key != chosen.key
            and s.feasible and _applies_requests(s)
            # Priced "once unblocked": a second decision, offered on its own.
            and not getattr(s, "blocked", None)
            and (getattr(s, "margin", None) or {}).get("ok", True)
        ]
        # Prefer the option that changes requests and nothing else. Every
        # request-applying scenario on prod-eu1/storage-1 also migrates the
        # instance type, and taking the cheapest of those offered +$6,033/mo to
        # make one container safe -- a migration priced as a stability fix.
        same_shape = [s for s in candidates if s.instance_type == (asis.instance_type if asis else "")]
        fix = min(same_shape or candidates, key=lambda s: s.delta_usd, default=None)
        # A "fix" that costs a quarter of the group's spend is a different
        # decision, not a footnote on this one.
        if fix is not None and spend > 0 and fix.delta_usd > spend * 0.25:
            fix = None
        if fix is not None:
            premium = fix.delta_usd - chosen.delta_usd
            # The same row may already be offered as a route; say why once.
            alternatives = [x for x in alternatives if x["key"] != fix.key]
            alternatives.insert(0, {
                "key": fix.key,
                "label": fix.label,
                "family": family_of(fix),
                "monthlyDelta": fix.monthly_usd - spend,
                "steps": len(fix.requires),
                "why": (
                    f"also fixes the {at_risk} container(s) at risk, "
                    + (f"for ${premium:,.0f}/mo more" if premium >= 0.5 else
                       f"and saves ${-premium:,.0f}/mo more" if premium <= -0.5 else
                       "at the same cost")
                ),
            })
            alternatives = alternatives[:FAMILY_LIMIT + 1]

    return Advice(
        name, "act", chosen.label, reason,
        scenario_key=chosen.key, monthly_delta=chosen.delta_usd,
        steps=list(chosen.requires), alternatives=alternatives,
        problems=problems, finding_counts=counts, diagnostics=diagnostics,
    )


#: Routes offered beside the pick, in the order a reader weighs them: the
#: changes that stay on today's nodes first, then a new node group.
FAMILIES = (
    ("requests", "requests only"),
    ("hpa", "HPA floors only"),
    ("both", "requests and HPA floors"),
    ("minsize", "lower minSize"),
    ("newtype", "new instance type"),
    ("unblock", "fix drain blockers"),
)
FAMILY_LIMIT = len(FAMILIES)


def family_of(scenario) -> str:
    """Which kind of change an option is, from the steps it takes."""
    req = scenario.requires or []
    if any("will not drain" in r for r in req):
        return "unblock"
    if getattr(scenario, "retype", False) or any("replace the node group" in r for r in req):
        return "newtype"
    if any("minSize" in r for r in req):
        return "minsize"
    requests = _applies_requests(scenario)
    hpa = any("minReplicas" in r for r in req)
    return "both" if requests and hpa else "requests" if requests else "hpa" if hpa else "minsize"


def _best_unblocked(unblockable, chosen, *, spend: float, edits: dict[str, int],
                    staying=None) -> dict | None:
    """The option that saves most once CA's drain blockers are fixed.

    Offered when it saves more than the pick, or -- when the pick replaces the
    node group -- more than anything that keeps today's nodes, or when there is
    no pick at all. One that saves less than both is the pick plus extra work.
    """
    best = min(unblockable, key=lambda s: (s.delta_usd, len(s.requires), edits.get(s.key, 0)),
               default=None)
    if best is None:
        return None
    beats_pick = chosen is None or best.delta_usd < chosen.delta_usd - 1.0
    pick_moves = chosen is not None and family_of(chosen) == "newtype"
    beats_staying = pick_moves and (staying is None or best.delta_usd < staying.delta_usd - 1.0)
    if not (beats_pick or beats_staying):
        return None
    held = best.blocked or {}
    n = len(held.get("nodes") or {})
    if chosen is None:
        extra = f"saves {_money(best.delta_usd).lstrip('−+')}"
    elif beats_pick:
        extra = (f"saves {_money(best.delta_usd - chosen.delta_usd).lstrip('−+')} more than "
                 "the recommendation")
    else:
        extra = (f"keeps today's instance type for {_money(best.delta_usd - chosen.delta_usd).lstrip('−+')} "
                 "less than the migration"
                 + (f", {_money(best.delta_usd - staying.delta_usd).lstrip('−+')} more than "
                    "anything else that stays" if staying is not None else ""))
    return {
        "key": best.key, "label": best.label, "monthlyDelta": best.monthly_usd - spend,
        "steps": len(best.requires), "family": "unblock",
        "why": (f"{extra} -- blocked today: fix {len(held.get('lift') or []) or n} workload(s) "
                f"on the {n} node(s) that go, or drain them by hand once; the nodes that stay "
                f"need nothing (Cluster Autoscaler stops at {held.get('nodesIfNot')} nodes "
                "without it)"),
    }


def _best_per_family(options, edits: dict[str, int]) -> list[tuple[str, object]]:
    """The cheapest option on each route; fewer edits breaks a tie."""
    best: dict[str, object] = {}
    for s in sorted(options, key=lambda s: (s.delta_usd, len(s.requires), edits.get(s.key, 0))):
        best.setdefault(family_of(s), s)
    return [(f, best[f]) for f, _ in FAMILIES if f in best]


def _why_family(candidate, chosen, edits: dict[str, int]) -> str:
    """What this route trades against the pick, in one clause."""
    gap = candidate.delta_usd - chosen.delta_usd
    ce, he = edits.get(candidate.key, 0), edits.get(chosen.key, 0)
    work = (f"{ce} change{'' if ce == 1 else 's'} instead of {he}" if ce and he and ce != he
            else f"{len(candidate.requires)} step{'' if len(candidate.requires) == 1 else 's'}")
    if abs(gap) < 1:
        return f"same saving by a different route -- {work}"
    if gap < 0:
        return f"saves {_money(gap).lstrip('−+')} more -- {work}"
    return f"saves {_money(gap).lstrip('−+')} less -- {work}"


def _money(delta: float) -> str:
    return f"{'−' if delta < 0 else '+'}${abs(delta):,.0f}/mo"


def _is_distinct(candidate, chosen) -> bool:
    """Keep an alternative only if it trades something real against the choice.

    Fewer steps, or a materially different saving. A row that is slightly worse
    in every dimension is not an alternative, it is clutter.
    """
    fewer_steps = len(candidate.requires) < len(chosen.requires)
    better = candidate.delta_usd < chosen.delta_usd * 1.1
    return fewer_steps or better


def _applies_requests(scenario) -> bool:
    """Does this option actually change any container request?"""
    return any("container request changes" in r for r in scenario.requires)


def _why_instead(candidate, chosen) -> str:
    if len(candidate.requires) < len(chosen.requires):
        return (
            f"{len(candidate.requires)} step"
            f"{'' if len(candidate.requires) == 1 else 's'} instead of "
            f"{len(chosen.requires)}, for {_money(candidate.delta_usd)}"
        )
    if candidate.delta_usd < chosen.delta_usd:
        return f"saves more ({_money(candidate.delta_usd)}) for more work"
    return "a different shape at a similar price"
