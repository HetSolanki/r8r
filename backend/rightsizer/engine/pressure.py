"""What the scheduler cannot see: the load a node really carries.

Kubernetes places pods by their requests, and Cluster Autoscaler removes nodes
by their requests. A pod with no request -- BestEffort -- or one running above
its request is invisible to both. The scheduler puts it on a node with no room
for what it actually uses; CA removes the node that was quietly absorbing it.
Nothing reports the result until the kubelet runs short of memory and starts
evicting, BestEffort pods first.

This module does not change how placement is predicted: predicting CA with
anything but requests would be predicting a different controller. It checks the
consequence instead. Given where pods end up, does the load they are measured
using fit the node -- and if not, which pods suffer first?

Load is *measured use*, never the request: a request is a scheduling promise and
consumes nothing, while the kubelet evicts on what containers actually hold.
"Busy" use is the p95 over the window of the *mean* replica, summed over a pod's
containers -- not the hottest replica's, because a node holds many replicas and
adding the worst one's figure for each of them invents load nobody saw. Summing
a p95 across every pod on a node still assumes they are busy together, so the
check errs towards warning: the right direction when the failure is an eviction.
"""

from __future__ import annotations

from dataclasses import dataclass, field

GIB = 1024**3

#: Busy memory above this share of allocatable is an eviction waiting to happen.
MEM_CRITICAL = 1.0
MEM_WARNING = 0.9
#: CPU is compressible: over-commit slows pods rather than killing them.
CPU_WARNING = 1.0


@dataclass(frozen=True)
class WorkloadLoad:
    """One replica of a workload: what it reserves and what it uses, per pod."""

    cpu_request: float = 0.0
    mem_request: float = 0.0
    cpu_busy: float = 0.0
    mem_busy: float = 0.0
    qos: str = "Burstable"
    #: Containers with neither request nor limit on either resource.
    unrequested: tuple[str, ...] = ()


def _qos(containers: dict[str, dict[str, float]]) -> str:
    """Kubernetes QoS class from per-container requests and limits."""
    values = [v for c in containers.values() for v in c.values()]
    if not any(values):
        return "BestEffort"
    guaranteed = all(
        c["cpu_limit"] > 0 and c["mem_limit"] > 0
        and c["cpu_request"] in (0.0, c["cpu_limit"])
        and c["mem_request"] in (0.0, c["mem_limit"])
        for c in containers.values()
    )
    return "Guaranteed" if guaranteed else "Burstable"


def workload_loads(snapshot) -> dict[str, WorkloadLoad]:
    """{workload key: per-replica load}, built from the snapshot's usage stats."""
    per: dict[str, dict[str, dict[str, float]]] = {}
    busy: dict[str, dict[str, float]] = {}
    for u in snapshot.usage:
        key = f"{u.namespace}/{u.workload}/{u.kind}"
        c = per.setdefault(key, {}).setdefault(u.container, {
            "cpu_request": 0.0, "mem_request": 0.0, "cpu_limit": 0.0, "mem_limit": 0.0,
        })
        c[f"{u.resource}_request"] = u.request
        c[f"{u.resource}_limit"] = u.limit
        b = busy.setdefault(key, {"cpu": 0.0, "mem": 0.0})
        # Mean replica at the busy 5% of the window; an older snapshot without
        # the ladder falls back to the hottest replica's p95, which overstates.
        b[u.resource] += float((u.avg_ladder or {}).get("p95") or u.p95)

    out = {}
    for key, containers in per.items():
        out[key] = WorkloadLoad(
            cpu_request=sum(c["cpu_request"] for c in containers.values()),
            mem_request=sum(c["mem_request"] for c in containers.values()),
            cpu_busy=busy[key]["cpu"],
            mem_busy=busy[key]["mem"],
            qos=_qos(containers),
            unrequested=tuple(sorted(n for n, c in containers.items() if not any(c.values()))),
        )
    return out


@dataclass
class ExposedPod:
    """A pod using more than it reserves -- first in line under node pressure."""

    pod: str
    workload: str
    qos: str
    mem_unreserved: float
    cpu_unreserved: float


@dataclass
class NodePressure:
    node: str
    cpu_alloc: float
    mem_alloc: float
    cpu_reserved: float
    mem_reserved: float
    cpu_busy: float
    mem_busy: float
    pods: int
    exposed: list[ExposedPod] = field(default_factory=list)

    @property
    def mem_ratio(self) -> float:
        return self.mem_busy / self.mem_alloc if self.mem_alloc else 0.0

    @property
    def cpu_ratio(self) -> float:
        return self.cpu_busy / self.cpu_alloc if self.cpu_alloc else 0.0

    @property
    def severity(self) -> str:
        if self.mem_ratio > MEM_CRITICAL:
            return "critical"
        if self.mem_ratio > MEM_WARNING or self.cpu_ratio > CPU_WARNING:
            return "warning"
        return "ok"

    @property
    def besteffort(self) -> list[ExposedPod]:
        return [p for p in self.exposed if p.qos == "BestEffort"]

    def describe(self) -> str:
        """One line a reviewer can act on."""
        parts = [
            f"memory {self.mem_busy / GIB:.1f} of {self.mem_alloc / GIB:.1f} GiB busy "
            f"({self.mem_ratio:.0%}; {self.mem_reserved / GIB:.1f} GiB reserved)",
            f"cpu {self.cpu_busy:.2f} of {self.cpu_alloc:.2f} cores ({self.cpu_ratio:.0%})",
        ]
        return f"{self.node}: " + ", ".join(parts)


def assess(by_node: dict[str, list], *, cpu_alloc: float, mem_alloc: float,
           loads: dict[str, WorkloadLoad]) -> list[NodePressure]:
    """Busy load against allocatable, for every node in a placement.

    ``by_node`` maps node -> pods carrying ``namespace``, ``name``,
    ``workload_key``, ``cpu`` and ``memory``, where cpu/memory are the requests
    *in force* -- a plan's, when simulating one. A request edit changes what is
    reserved, not what is used, so busy load always comes from measurement; only
    a pod with no measurement at all is counted at its request.
    """
    out = []
    for node, pods in sorted(by_node.items()):
        res = NodePressure(node=node, cpu_alloc=cpu_alloc, mem_alloc=mem_alloc,
                           cpu_reserved=0.0, mem_reserved=0.0, cpu_busy=0.0,
                           mem_busy=0.0, pods=len(pods))
        for p in pods:
            load = loads.get(p.workload_key)
            cpu_use = load.cpu_busy if load else p.cpu
            mem_use = load.mem_busy if load else p.memory
            res.cpu_reserved += p.cpu
            res.mem_reserved += p.memory
            res.cpu_busy += cpu_use
            res.mem_busy += mem_use
            if load and (mem_use > p.memory * 1.01 or cpu_use > p.cpu * 1.01):
                qos = load.qos
                if qos == "BestEffort" and (p.cpu or p.memory):
                    qos = "Burstable"  # a plan that gives it requests changes its class
                res.exposed.append(ExposedPod(
                    pod=f"{p.namespace}/{p.name}", workload=p.workload_key, qos=qos,
                    mem_unreserved=mem_use - p.memory, cpu_unreserved=cpu_use - p.cpu,
                ))
        # Kubelet eviction order: BestEffort first, then by usage above request.
        res.exposed.sort(key=lambda e: (e.qos != "BestEffort", -e.mem_unreserved, e.pod))
        out.append(res)
    return out


def flagged(pressure: list[NodePressure]) -> list[NodePressure]:
    return [p for p in pressure if p.severity != "ok"]


@dataclass
class Unreserved:
    """Load on a node group that the scheduler and Cluster Autoscaler cannot see."""

    besteffort_pods: int = 0
    besteffort_cpu: float = 0.0
    besteffort_mem: float = 0.0
    over_request_pods: int = 0
    over_request_mem: float = 0.0
    #: (workload key, pods, busy memory per pod), heaviest first
    top_besteffort: list[tuple[str, int, float]] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.besteffort_pods or self.over_request_pods)

    def describe(self) -> str:
        bits = []
        if self.besteffort_pods:
            bits.append(
                f"{self.besteffort_pods} pod(s) with no request or limit using "
                f"~{self.besteffort_mem / GIB:.1f} GiB and {self.besteffort_cpu:.2f} cores "
                "busy, 0 reserved"
            )
        if self.over_request_pods:
            bits.append(
                f"{self.over_request_pods} pod(s) using ~{self.over_request_mem / GIB:.1f} GiB "
                "more memory than they request"
            )
        return "; ".join(bits)


def unreserved(rows: list[dict], loads: dict[str, WorkloadLoad]) -> Unreserved:
    """Summarise unreserved load across a group's placement rows."""
    out = Unreserved()
    by_wl: dict[str, int] = {}
    for r in rows:
        load = loads.get(r.get("workload_key", ""))
        if load is None:
            continue
        cpu, mem = float(r.get("cpu", 0.0)), float(r.get("mem", 0.0))
        if load.qos == "BestEffort" and not (cpu or mem):
            out.besteffort_pods += 1
            out.besteffort_cpu += load.cpu_busy
            out.besteffort_mem += load.mem_busy
            by_wl[r["workload_key"]] = by_wl.get(r["workload_key"], 0) + 1
        elif mem and load.mem_busy > mem * 1.01:
            out.over_request_pods += 1
            out.over_request_mem += load.mem_busy - mem
    out.top_besteffort = sorted(
        ((k, n, loads[k].mem_busy) for k, n in by_wl.items()),
        key=lambda t: (-t[1] * t[2], t[0]),
    )
    return out


def by_node_from_rows(rows: list[dict]) -> dict[str, list]:
    """Group placement rows into PlacedPods per node, requests as recorded."""
    from rightsizer.engine.autoscaler import PlacedPod

    out: dict[str, list] = {}
    for r in rows:
        if not r.get("node"):
            continue
        out.setdefault(r["node"], []).append(PlacedPod(
            namespace=r.get("namespace", ""), name=r.get("pod", ""), node=r["node"],
            cpu=float(r.get("cpu", 0.0)), memory=float(r.get("mem", 0.0)),
            owner_kind=r.get("owner_kind", ""), workload_key=r.get("workload_key", ""),
        ))
    return out
