"""What happens if I change *these* containers, to *these* values.

The recommender proposes a number for every container it can size. Nobody
applies all of them at once, and nobody applies them exactly: a request goes out
in a PR covering a handful of workloads, usually a little more generous than the
suggestion. The question that actually gets asked before merging is "does this
particular set of edits free a node, and does it break anything?" -- and neither
half of that is answerable from a list of per-container recommendations.

This module answers it. A plan is an explicit set of target requests; it is
packed through the same engine that produces the recommendations, so the node
count here and the node count there cannot drift.

**The failure this exists to catch.** A container with no limit can use far more
than it reserves. The scheduler only ever sees requests, so cutting a request
makes room that the scheduler will fill -- and the new neighbour arrives on a
node whose existing occupants are already, in practice, using the space. Nothing
in the cluster reports that gap. :func:`review` does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

GIB = 1024**3

_CPU_SUFFIX = {"m": 0.001, "": 1.0}
_MEM_SUFFIX = {
    "": 1.0, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12,
    "Ki": 1024.0, "Mi": 1024.0**2, "Gi": 1024.0**3, "Ti": 1024.0**4,
}

#: A value meaning "use whatever the recommender said for this container".
RECOMMENDED = "recommended"


class PlanError(ValueError):
    """A plan that cannot be read. Raised rather than guessed at."""


def parse_cpu(text: str) -> float:
    """Kubernetes CPU quantity to cores. ``100m`` -> 0.1, ``1.5`` -> 1.5."""
    m = re.fullmatch(r"\s*([0-9.]+)\s*(m?)\s*", str(text))
    if not m:
        raise PlanError(f"not a CPU quantity: {text!r}")
    return float(m.group(1)) * _CPU_SUFFIX[m.group(2)]


def parse_memory(text: str) -> float:
    """Kubernetes memory quantity to bytes. ``2Gi`` -> 2147483648."""
    m = re.fullmatch(r"\s*([0-9.]+)\s*([EPTGMk]i?|)\s*", str(text))
    if not m or m.group(2) not in _MEM_SUFFIX:
        raise PlanError(f"not a memory quantity: {text!r}")
    return float(m.group(1)) * _MEM_SUFFIX[m.group(2)]


def format_cpu(cores: float) -> str:
    return f"{round(cores * 1000)}m" if cores < 1 else f"{cores:.3g}"


def format_memory(b: float) -> str:
    return f"{b / GIB:.3g}Gi" if b >= GIB else f"{b / 1024**2:.3g}Mi"


@dataclass(frozen=True)
class Change:
    """One container's target requests. ``None`` leaves that resource alone."""

    namespace: str
    workload: str
    container: str
    cpu: float | str | None = None
    memory: float | str | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.namespace, self.workload, self.container)

    def __str__(self) -> str:
        bits = []
        if self.cpu is not None:
            bits.append(f"cpu={self.cpu if isinstance(self.cpu, str) else format_cpu(self.cpu)}")
        if self.memory is not None:
            bits.append(
                f"mem={self.memory if isinstance(self.memory, str) else format_memory(self.memory)}"
            )
        return f"{self.namespace}/{self.workload}/{self.container} {' '.join(bits)}"


@dataclass(frozen=True)
class Plan:
    changes: tuple[Change, ...] = ()

    def index(self) -> dict[tuple[str, str, str], Change]:
        return {c.key: c for c in self.changes}

    @staticmethod
    def parse(lines: list[str] | str) -> "Plan":
        """One change per line::

            # comments and blank lines are ignored
            apps/media-server/media-server  cpu=100m  mem=3Gi
            apps/web-api/api                          cpu=recommended

        A plain-text format on purpose: it goes in the PR beside the manifest
        change it describes, and a reviewer can read it without tooling.
        """
        if isinstance(lines, str):
            lines = lines.splitlines()
        changes: list[Change] = []
        for n, raw in enumerate(lines, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            target = parts[0]
            bits = target.split("/")
            if len(bits) != 3 or not all(bits):
                raise PlanError(
                    f"line {n}: expected namespace/workload/container, got {target!r}"
                )
            cpu = mem = None
            for token in parts[1:]:
                if "=" not in token:
                    raise PlanError(f"line {n}: expected key=value, got {token!r}")
                k, _, v = token.partition("=")
                k = k.lower()
                if k in ("cpu", "cpurequest"):
                    cpu = RECOMMENDED if v == RECOMMENDED else parse_cpu(v)
                elif k in ("mem", "memory", "memrequest"):
                    mem = RECOMMENDED if v == RECOMMENDED else parse_memory(v)
                else:
                    raise PlanError(f"line {n}: unknown key {k!r}; use cpu= or mem=")
            if cpu is None and mem is None:
                raise PlanError(f"line {n}: {target} sets neither cpu nor mem")
            changes.append(Change(bits[0], bits[1], bits[2], cpu, mem))
        if not changes:
            raise PlanError("plan is empty")
        return Plan(tuple(changes))

    @staticmethod
    def read(path: Path) -> "Plan":
        return Plan.parse(Path(path).read_text().splitlines())


# -- resolving a plan against a snapshot -----------------------------------


@dataclass
class Resolved:
    """A change with its current state and the numbers needed to judge it."""

    change: Change
    kind: str
    replicas: int
    cpu_request: float
    mem_request: float
    cpu_limit: float
    mem_limit: float
    cpu_peak: float
    mem_peak: float
    cpu_p95: float
    mem_p95: float
    cpu_target: float | None = None
    mem_target: float | None = None
    recommended_cpu: float | None = None
    recommended_mem: float | None = None
    blockers: list = field(default_factory=list)


def resolve(snapshot, plan: Plan, recommendations: list) -> tuple[list[Resolved], list[str]]:
    """Match each change to the container it names.

    Returns the resolved changes and the targets that matched nothing --
    reported rather than skipped, because a typo in a workload name would
    otherwise produce a confident simulation of doing nothing.
    """
    stats: dict[tuple, dict[str, object]] = {}
    for s in snapshot.usage:
        e = stats.setdefault((s.namespace, s.workload, s.container), {"kind": s.kind})
        e[s.resource] = s
        e["replicas"] = s.replicas

    recs = {(r.namespace, r.workload, r.container): r for r in recommendations}

    out: list[Resolved] = []
    missing: list[str] = []
    for change in plan.changes:
        entry = stats.get(change.key)
        if entry is None:
            missing.append(f"{change.namespace}/{change.workload}/{change.container}")
            continue
        cpu = entry.get("cpu")
        mem = entry.get("mem")
        rec = recs.get(change.key)
        r = Resolved(
            change=change,
            kind=str(entry["kind"]),
            replicas=int(entry.get("replicas") or 0),
            cpu_request=getattr(cpu, "request", 0.0),
            mem_request=getattr(mem, "request", 0.0),
            cpu_limit=getattr(cpu, "limit", 0.0),
            mem_limit=getattr(mem, "limit", 0.0),
            cpu_peak=getattr(cpu, "peak", 0.0),
            mem_peak=getattr(mem, "peak", 0.0),
            cpu_p95=getattr(cpu, "p95", 0.0),
            mem_p95=getattr(mem, "p95", 0.0),
            recommended_cpu=getattr(rec, "cpu_recommended", None),
            recommended_mem=getattr(rec, "mem_recommended", None),
            blockers=list(getattr(rec, "blockers", []) or []),
        )
        r.cpu_target = _target(change.cpu, r.recommended_cpu, r.cpu_request)
        r.mem_target = _target(change.memory, r.recommended_mem, r.mem_request)
        out.append(r)
    return out, missing


def _target(value, recommended, current) -> float | None:
    if value is None:
        return None
    if value == RECOMMENDED:
        # "recommended" on a container the tool declined to size is a no-op, not
        # an error -- the plan asked for its number and there isn't one.
        return recommended if recommended is not None else current
    return float(value)


# -- safety review ---------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    severity: str        # "unsafe" | "risk" | "advisory"
    code: str
    subject: str
    detail: str

    ORDER = {"unsafe": 0, "risk": 1, "advisory": 2}


def review(resolved: list[Resolved]) -> list[Finding]:
    """Everything about this plan that could bite, per container.

    The checks are deliberately about the *gap between what a container reserves
    and what it uses*, because that gap is invisible to the scheduler and is the
    mechanism behind "it fit on paper and fell over in practice".
    """
    out: list[Finding] = []
    for r in resolved:
        name = f"{r.change.namespace}/{r.change.workload}/{r.change.container}"

        # A request above its own limit is rejected by the API server. Catching
        # it here costs nothing and saves a failed rollout.
        if r.cpu_target is not None and r.cpu_limit and r.cpu_target > r.cpu_limit:
            out.append(Finding(
                "unsafe", "request-above-limit", name,
                f"cpu request {format_cpu(r.cpu_target)} exceeds the container's limit "
                f"{format_cpu(r.cpu_limit)}; the API server will reject the pod spec",
            ))
        if r.mem_target is not None and r.mem_limit and r.mem_target > r.mem_limit:
            out.append(Finding(
                "unsafe", "request-above-limit", name,
                f"memory request {format_memory(r.mem_target)} exceeds the container's limit "
                f"{format_memory(r.mem_limit)}; the API server will reject the pod spec",
            ))

        # Memory below the observed peak. Memory is not compressible: the
        # container will still take what it takes, it just stops being covered
        # by its reservation, and under node pressure a Burstable pod over its
        # request is what the kubelet evicts first.
        if r.mem_target is not None and r.mem_peak > r.mem_target:
            over = r.mem_peak - r.mem_target
            qos = "no memory limit set, so it is Burstable" if not r.mem_limit else "Burstable"
            out.append(Finding(
                "unsafe" if r.mem_peak > r.mem_target * 1.25 else "risk",
                "memory-below-peak", name,
                f"peaked at {format_memory(r.mem_peak)} against a proposed request of "
                f"{format_memory(r.mem_target)} -- {format_memory(over)} of its real use "
                f"would be unreserved ({qos}, so it is evicted before anything Guaranteed)",
            ))

        # CPU below p95 throttles rather than dies, so it is a different class of
        # problem and said differently.
        if r.cpu_target is not None and r.cpu_p95 > r.cpu_target:
            out.append(Finding(
                "risk" if r.cpu_p95 > r.cpu_target * 1.5 else "advisory",
                "cpu-below-p95", name,
                f"p95 is {format_cpu(r.cpu_p95)} against a proposed request of "
                f"{format_cpu(r.cpu_target)}; CPU is compressible so this throttles "
                f"rather than fails, but latency will move",
            ))

        # No limit at all: this is the container that can take a node down, and
        # it is worth naming even when the plan's numbers are fine.
        if not r.mem_limit and r.mem_peak:
            headroom = (r.mem_target if r.mem_target is not None else r.mem_request)
            if r.mem_peak > headroom:
                out.append(Finding(
                    "advisory", "no-memory-limit", name,
                    f"has no memory limit and has already exceeded its request "
                    f"({format_memory(r.mem_peak)} against {format_memory(headroom)}); "
                    f"nothing caps what one replica can take from its node",
                ))

        for b in r.blockers:
            sev = getattr(b, "severity", "advisory")
            out.append(Finding(
                "unsafe" if sev == "unsafe" else "advisory",
                getattr(b, "code", "blocker"), name,
                getattr(b, "detail", str(b)),
            ))

    out.sort(key=lambda f: (Finding.ORDER.get(f.severity, 3), f.code, f.subject))
    return out


def burst_exposure(nodes: list) -> list[dict]:
    """Per node: how much use is not covered by a reservation.

    The scheduler places on requests alone. When the pods on a node collectively
    use more than they reserve, that surplus is real load the scheduler cannot
    see, and it will keep adding pods to the node as though the space were free.
    This is the number behind "it fit, and then it fell over".
    """
    rows = []
    for i, node in enumerate(nodes):
        pods = getattr(node, "pods", [])
        if not pods:
            continue
        unreserved_cpu = sum(1 for p in pods if p.usage_driven)
        rows.append({
            "node": i + 1,
            "pods": len(pods),
            "usage_driven": unreserved_cpu,
            "cpu_free": round(node.cpu_free, 3),
            "mem_free_gib": round(node.mem_free / GIB, 2),
        })
    return rows


# -- simulating the plan ---------------------------------------------------


@dataclass
class GroupOutcome:
    """One node group, packed twice.

    Three node counts, not two, because they answer different questions and
    conflating them is how a simulation starts flattering itself:

    ``nodes_now``     what the cluster is actually running.
    ``nodes_before``  what the packer makes of today's pods, unchanged.
    ``nodes_after``   the same packer with the plan applied.

    The saving is ``before - after``: both sides come from the same model, so
    the difference is attributable to the plan. ``model_gap`` carries the
    disagreement between the model's baseline and reality, which is a fact about
    the model's accuracy and has no business being folded into a dollar figure.
    """

    group: str
    instance_type: str
    nodes_now: int
    nodes_before: int
    nodes_after: int
    price_per_node: float
    feasible: bool
    unplaceable: int
    binding_before: str
    binding_after: str
    touched_pods: int
    absorbed_pods: int
    usage_driven_before: int
    usage_driven_after: int
    cpu_footprint_before: float
    cpu_footprint_after: float
    mem_footprint_before: float
    mem_footprint_after: float
    min_size: int = 0
    nodes: list = field(default_factory=list)
    # (resource, container, proposed request, measured peak, current request)
    # for each planned container whose peak, not its request, sets the footprint.
    absorbed_detail: list = field(default_factory=list)
    # -- what the cluster itself would do -----------------------------------
    # The packer above sizes pods by what they use. Cluster Autoscaler and the
    # scheduler size them by what they request, and that is what will actually
    # happen: these fields replay it and check the load it leaves behind.
    ca_source: str = ""
    ca_removed_today: list = field(default_factory=list)
    ca_removed_plan: list = field(default_factory=list)
    #: Nodes whose busy load does not fit -- today, and after CA acts on the plan.
    pressure_today: list = field(default_factory=list)
    pressure_plan: list = field(default_factory=list)
    #: :class:`rightsizer.engine.pressure.Unreserved` for the group.
    unreserved: object = None

    @property
    def nodes_freed(self) -> int:
        return self.nodes_before - self.nodes_after

    @property
    def delta_usd(self) -> float:
        return -self.nodes_freed * self.price_per_node

    @property
    def model_gap(self) -> int:
        return self.nodes_before - self.nodes_now

    @property
    def floor_bound(self) -> bool:
        return bool(self.min_size) and self.nodes_after <= self.min_size


def _absorbed_detail(resolved: list[Resolved], on_group: set) -> list:
    """Which planned containers have their footprint set by use, not request.

    Without this the report can only say a cut "did not shrink" anything, which
    is true and useless -- the number that overrode the request is the whole
    explanation.
    """
    rows = []
    for r in resolved:
        if (r.change.namespace, r.change.workload) not in on_group:
            continue
        name = f"{r.change.workload}/{r.change.container}"
        if r.cpu_target is not None and r.cpu_peak > r.cpu_target:
            rows.append(("cpu", name, r.cpu_target, r.cpu_peak, r.cpu_request))
        if r.mem_target is not None and r.mem_peak > r.mem_target:
            rows.append(("mem", name, r.mem_target, r.mem_peak, r.mem_request))
    return rows


def simulate(snapshot, plan: Plan, resolved: list[Resolved], tun, *,
             price_of) -> list[GroupOutcome]:
    """Pack every node group twice -- as it runs, and with the plan applied.

    Both packs go through :func:`rightsizer.engine.simulate.pack`, the same function
    behind the published recommendations, so a node count here and a node count
    there cannot disagree.
    """
    from rightsizer.adapters.autoscalers import for_snapshot
    from rightsizer.engine import pressure as pr
    from rightsizer.engine.eviction import rules_from_snapshot, simulate_group
    from rightsizer.engine.simulate import pack, plan_deltas, pods_for_nodegroup

    targets: dict[tuple, dict] = {}
    for r in resolved:
        t: dict[str, float] = {}
        if r.cpu_target is not None:
            t["cpu"] = r.cpu_target
        if r.mem_target is not None:
            t["mem"] = r.mem_target
        if t:
            targets[r.change.key] = t
    touched = {(r.change.namespace, r.change.workload) for r in resolved}

    # Shared by every group: the same edits, the same CA flags, the same facts.
    deltas = plan_deltas(snapshot, targets)
    loads = pr.workload_loads(snapshot)
    rules = rules_from_snapshot(snapshot)
    autoscaler, found = for_snapshot(snapshot)
    ca_cfg = autoscaler.scale_down_settings(found)
    ca_source = (ca_cfg.source if found
                 else "upstream defaults (no CA configuration in this snapshot)")

    out: list[GroupOutcome] = []
    for g in snapshot.nodegroups:
        if not (snapshot.placements or {}).get(g.name) or g.nodes_current == 0:
            continue
        cfg = snapshot.facts.config_for(g.name) or {}
        itype = (cfg.get("instance_types") or [""])[0]
        cpu_a = g.alloc_cpu_per_node - g.ds_cpu_per_node
        mem_a = g.alloc_mem_per_node - g.ds_mem_per_node
        if cpu_a <= 0 or mem_a <= 0:
            continue
        mp = snapshot.facts.max_pods_for(g.name) or 110

        def run(plan_targets):
            pods = pods_for_nodegroup(
                snapshot, g.name, apply="current", recommendations=[],
                group_state=g, plan_targets=plan_targets,
            )
            return pods, pack(pods, cpu_per_node=cpu_a, mem_per_node=mem_a,
                              max_pods_per_node=mp, headroom=tun.pack_headroom)

        pods_before, before = run(None)
        pods_after, after = run(targets)
        on_group = {
            (p.workload.split("/")[0], "/".join(p.workload.split("/")[1:-1]))
            for p in pods_after
        } & touched
        hit = sum(
            1 for p in pods_after
            if (p.workload.split("/")[0], "/".join(p.workload.split("/")[1:-1])) in touched
        )
        if hit == 0:
            continue
        floor = int(cfg.get("min_size") or 0)

        # What CA would do on its own, before and after the plan. It packs by
        # requests, so a cut makes nodes look emptier and it removes more --
        # and moves their pods, requested or not, onto the nodes that stay.
        rows = snapshot.placements[g.name]
        run_today, _ = simulate_group(snapshot, g, ca_cfg, rules, floor)
        run_plan, _ = simulate_group(snapshot, g, ca_cfg, rules, floor, request_delta=deltas)

        def assess(by_node):
            return pr.flagged(pr.assess(by_node, cpu_alloc=g.alloc_cpu_per_node,
                                        mem_alloc=g.alloc_mem_per_node, loads=loads))

        out.append(GroupOutcome(
            group=g.name,
            instance_type=itype or "",
            nodes_now=g.nodes_current,
            nodes_before=max(before.nodes_required, floor) if floor else before.nodes_required,
            nodes_after=max(after.nodes_required, floor) if floor else after.nodes_required,
            price_per_node=price_of(itype) if itype else 0.0,
            feasible=after.feasible,
            unplaceable=len(after.unplaceable),
            binding_before=before.binding,
            binding_after=after.binding,
            touched_pods=hit,
            # Planned pods whose footprint is still set by what they measured,
            # not by the request the plan gives them.
            absorbed_pods=sum(
                1 for p in pods_after
                if p.usage_driven
                and (p.workload.split("/")[0], "/".join(p.workload.split("/")[1:-1])) in touched
            ),
            absorbed_detail=_absorbed_detail(resolved, on_group),
            usage_driven_before=sum(1 for p in pods_before if p.usage_driven),
            usage_driven_after=sum(1 for p in pods_after if p.usage_driven),
            cpu_footprint_before=sum(p.cpu for p in pods_before),
            cpu_footprint_after=sum(p.cpu for p in pods_after),
            mem_footprint_before=sum(p.memory for p in pods_before),
            mem_footprint_after=sum(p.memory for p in pods_after),
            min_size=floor,
            nodes=after.placement,
            ca_source=ca_source,
            ca_removed_today=(run_today.removed + run_today.removed_empty) if run_today else [],
            ca_removed_plan=(run_plan.removed + run_plan.removed_empty) if run_plan else [],
            pressure_today=assess(pr.by_node_from_rows(rows)),
            pressure_plan=assess(run_plan.placement) if run_plan else [],
            unreserved=pr.unreserved(rows, loads),
        ))
    return out


#: Heaviest BestEffort workloads offered a suggested request, per group.
SUGGEST_TOP = 5


def pressure_findings(snapshot, outcomes: list[GroupOutcome], tun) -> list[Finding]:
    """What the plan does to pods the scheduler cannot see.

    Three kinds, kept apart because they call for different responses:

    * **unsafe**: a node that fits today would, once CA acts on the plan, carry
      more busy memory than it has. The kubelet evicts to recover, BestEffort
      pods first -- the plan causes an outage for pods it never mentions.
    * **risk**: CPU over-commit or memory above 90% after the plan (slowdown,
      not eviction), and pressure that already exists today -- not caused by
      the plan, but the plan should not make it worse.
    * **advisory**: unreserved load on a group the plan tightens, with requests
      to give those pods so the scheduler and CA can see them.
    """
    from rightsizer.core.stats import recommend_cpu, recommend_memory

    stats = {(u.namespace, u.workload, u.kind, u.container, u.resource): u
             for u in snapshot.usage}
    out: list[Finding] = []
    for o in outcomes:
        today = {p.node: p for p in o.pressure_today}
        extra = len(o.ca_removed_plan) - len(o.ca_removed_today)
        cause = (
            f"once Cluster Autoscaler removes {len(o.ca_removed_plan)} node(s) under this plan "
            f"({extra:+d} vs today)"
        )
        for p in o.pressure_plan:
            before = today.get(p.node)
            if before and before.severity == p.severity:
                continue  # unchanged by the plan; reported below as pre-existing
            victims = _victims(p)
            if p.severity == "critical":
                out.append(Finding(
                    "unsafe", "memory-overcommit-after-scale-down", f"{o.group}/{p.node}",
                    f"{cause}, {p.describe()}. The kubelet will evict to recover memory, "
                    f"BestEffort pods first{victims}.",
                ))
            else:
                out.append(Finding(
                    "risk", "load-exceeds-capacity-after-scale-down", f"{o.group}/{p.node}",
                    f"{cause}, {p.describe()}. Pods without requests get the smallest CPU "
                    f"share and slow down first{victims}.",
                ))
        for p in o.pressure_today:
            out.append(Finding(
                "risk", "node-pressure-today", f"{o.group}/{p.node}",
                f"already today, before any change: {p.describe()}{_victims(p)}. Not caused "
                "by this plan -- but any scale-down here makes it worse.",
            ))

        u = o.unreserved
        if u is not None and u.any and (extra > 0 or o.pressure_plan):
            lines = []
            for wkey, pods, _mem in u.top_besteffort[:SUGGEST_TOP]:
                ns, _, rest = wkey.partition("/")
                wl, _, kind = rest.rpartition("/")
                for (sns, swl, skind, container, res), st in sorted(stats.items()):
                    if (sns, swl, skind, res) != (ns, wl, kind, "cpu"):
                        continue
                    mem = stats.get((ns, wl, kind, container, "mem"))
                    cpu_r = format_cpu(recommend_cpu(st, tun))
                    mem_r = format_memory(recommend_memory(mem, tun)) if mem else None
                    lines.append(f"{ns}/{wl}/{container} cpu={cpu_r}"
                                 + (f" mem={mem_r}" if mem_r else "") + f"   # x{pods}")
            suggestion = (
                " Give the heaviest ones requests so the scheduler and CA can see them"
                " (plan lines, sized from p95 and peak):\n        " + "\n        ".join(lines)
                if lines else ""
            )
            out.append(Finding(
                "advisory", "unreserved-load", o.group,
                f"{u.describe()}. Cluster Autoscaler counts none of it, so each node this "
                f"plan lets it remove pushes that load onto fewer nodes.{suggestion}",
            ))
    return out


def _victims(p) -> str:
    """The pods first in line, named -- a count alone is not actionable."""
    if not p.exposed:
        return ""
    names = ", ".join(
        f"{e.pod} ({e.qos}, +{e.mem_unreserved / GIB:.2f} GiB unreserved)" for e in p.exposed[:4]
    )
    more = f" and {len(p.exposed) - 4} more" if len(p.exposed) > 4 else ""
    be = len(p.besteffort)
    lead = f": {be} BestEffort pod(s) here" if be else ": pods above their request here"
    return f"{lead} -- {names}{more}"


# -- report ----------------------------------------------------------------


def _what_the_cluster_does(w, o: GroupOutcome) -> None:
    """The requests-only view: what CA does, and the real load it leaves behind.

    The packing above is usage-aware and therefore kinder than the cluster. This
    is the cluster: CA removes nodes by requests, the scheduler refills by
    requests, and pods without requests go wherever there is nominal room.
    """
    if not o.ca_source:
        return
    w(f"      cluster autoscaler ({o.ca_source}), packing by requests only:")
    w(f"        today it could remove {len(o.ca_removed_today)} node(s); "
      f"with this plan {len(o.ca_removed_plan)}"
      + (f": {', '.join(o.ca_removed_plan[:4])}" + (" ..." if len(o.ca_removed_plan) > 4 else "")
         if o.ca_removed_plan else ""))
    if o.unreserved is not None and o.unreserved.any:
        w(f"        load it cannot see: {o.unreserved.describe()}")
    if o.pressure_plan:
        w("        busy load on the nodes that remain (measured use, p95 of the mean replica):")
        for p in o.pressure_plan[:6]:
            be = f", {len(p.besteffort)} BestEffort" if p.besteffort else ""
            w(f"          [{p.severity}] {p.describe()}{be}")
        if len(o.pressure_plan) > 6:
            w(f"          ... and {len(o.pressure_plan) - 6} more")
    else:
        w("        every remaining node holds its busy load within allocatable")


def to_text(plan: Plan, resolved: list[Resolved], missing: list[str],
            outcomes: list[GroupOutcome], findings: list[Finding]) -> str:
    """The plan as something a reviewer can read in a PR."""
    L: list[str] = []
    w = L.append
    w("PLAN")
    for r in resolved:
        c = r.change
        bits = []
        if r.cpu_target is not None:
            bits.append(f"cpu {format_cpu(r.cpu_request)} -> {format_cpu(r.cpu_target)}")
        if r.mem_target is not None:
            bits.append(f"mem {format_memory(r.mem_request)} -> {format_memory(r.mem_target)}")
        w(f"  {c.namespace}/{c.workload}/{c.container}  x{r.replicas}")
        w(f"      {' · '.join(bits)}")
        # A plan that differs from the recommendation is the normal case, so the
        # difference is stated rather than treated as an error.
        for label, target, rec, fmt in (
            ("cpu", r.cpu_target, r.recommended_cpu, format_cpu),
            ("mem", r.mem_target, r.recommended_mem, format_memory),
        ):
            if target is not None and rec is not None and abs(target - rec) > rec * 0.01:
                w(f"      {label}: {fmt(target)} vs recommended {fmt(rec)} "
                  f"({(target / rec - 1) * 100:+.0f}%)")
    if missing:
        w("")
        w("NOT FOUND -- these matched no container in the snapshot:")
        for m in missing:
            w(f"  {m}")

    w("")
    w("CAPACITY")
    if not outcomes:
        w("  no node group holds a pod this plan touches")
    total = 0.0
    for o in outcomes:
        total += o.delta_usd
        w(f"  {o.group}  ({o.instance_type}, running {o.nodes_now})")
        # "-3 nodes" for a plan that needs three more is exactly backwards, so
        # the direction is spelled out rather than left to a sign.
        moved = o.nodes_after - o.nodes_before
        verdict = (
            f"{-moved} node(s) freed" if moved < 0
            else f"{moved} more node(s) needed" if moved > 0
            else "no change in node count"
        )
        w(f"      packed {o.nodes_before} -> {o.nodes_after} nodes"
          f"   {verdict}   ${o.delta_usd:+,.0f}/mo")
        w(f"      cpu {o.cpu_footprint_before:.1f} -> {o.cpu_footprint_after:.1f} cores"
          f" · mem {o.mem_footprint_before / GIB:.0f} -> {o.mem_footprint_after / GIB:.0f} GiB"
          f" · binding {o.binding_before} -> {o.binding_after}")
        w(f"      {o.touched_pods} pod(s) of the planned workloads on this group")
        if o.absorbed_pods:
            # The single most useful line in this report: a request cut below
            # what a container is measured using does not free anything, because
            # the node still has to hold the usage. The saving is imaginary and
            # the eviction risk is not. Naming the number that overrode the
            # request is what makes it actionable rather than mysterious.
            w(f"      {o.absorbed_pods} of them did not shrink -- packed by measured "
              f"use, not by the request this plan gives them:")
            for res, name, target, peak, current in o.absorbed_detail:
                fmt = format_cpu if res == "cpu" else format_memory
                w(f"        {name} {res}: you propose {fmt(target)}, "
                  f"peak is {fmt(peak)} -- the peak binds")
                if peak >= current:
                    w(f"          it already peaks above its current {fmt(current)} request, "
                      f"so no cut frees capacity here; the node is holding real usage")
                else:
                    w(f"          anything below {fmt(peak)} is wasted effort -- "
                      f"cutting to {fmt(peak)} frees the same and keeps the reservation")
        if o.usage_driven_after != o.usage_driven_before:
            w(f"      pods whose size is set by measured use, not their request: "
              f"{o.usage_driven_before} -> {o.usage_driven_after}")
        if o.model_gap:
            w(f"      note: the packer makes {o.nodes_before} of today's pods against "
              f"{o.nodes_now} actually running ({o.model_gap:+d}); the saving above is "
              f"model-to-model, so this gap is not double-counted")
        if not o.feasible:
            w(f"      INFEASIBLE: {o.unplaceable} pod(s) fit on no node of this shape")
        if o.floor_bound:
            w(f"      minSize is {o.min_size}, so the group cannot drop below it")
        _what_the_cluster_does(w, o)
    w("")
    w(f"  net ${total:+,.0f}/mo")

    w("")
    w("SAFETY")
    if not findings:
        w("  nothing flagged")
    for f in findings:
        w(f"  [{f.severity}] {f.subject}")
        w(f"      {f.detail}")
    return "\n".join(L)
