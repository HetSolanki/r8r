"""What production would actually do with an option, rather than an ideal repack.

The as-is baseline is the real cluster. Options used to be priced as a perfect
First-Fit-Decreasing repack filled to an 85% ceiling of our own: every pod
moved at once, in the best order. Neither controller behaves like that, so the
number could be wrong in both directions -- services-2 "needed" 11 nodes for
the pods it runs on 10, and prod-eu1 db-1 was offered 3 -> 1 on nodes Cluster
Autoscaler will never drain.

This models the two controllers that decide a node count:

* **Same instance type** -- a request edit, an HPA floor, a minSize. Nothing
  repacks the group. The change lands on today's placement: requests resize,
  surplus replicas go (a ReplicaSet removes pods from the nodes holding the
  most of its replicas first), and pods that no longer fit are rescheduled onto
  free space or a new node. Then Cluster Autoscaler's scale-down loop runs with
  its real threshold, PDBs, affinity and eviction vetoes
  (:func:`rightsizer.engine.eviction.simulate_group`).
* **A new instance type** -- a replacement node group, which the pods move into
  as the old one drains. The scheduler places by request against *full*
  allocatable and Cluster Autoscaler adds a node only when a pod fits nowhere,
  so the count is a by-request packing at 100%. It keeps no headroom.

The 85% ceiling stood in for "do not overload the nodes". This replaces it with
the check it approximated: the load each node would really carry, from measured
use, with a workload's load spread over the replicas it keeps
(:func:`load_check`). An option whose nodes would run out of memory is unsafe.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rightsizer.engine.pressure import MEM_CRITICAL, MEM_WARNING, CPU_WARNING, workload_loads

GIB = 1024**3


@dataclass
class Outcome:
    nodes: int
    #: "autoscaler" (replayed on today's placement) or "scheduler" (new nodes)
    mode: str
    #: node -> [(workload_key, cpu request, mem request, is_daemonset)]
    placement: dict[str, list[tuple[str, float, float, bool]]] = field(default_factory=dict)
    added: int = 0
    vetoed: dict[str, str] = field(default_factory=dict)
    #: node -> every (subject, why) blocking it; see ``ScaleDownRun.blockers``.
    blockers: dict[str, list] = field(default_factory=dict)
    unplaceable: int = 0
    load: dict = field(default_factory=dict)


def lever_changes(snapshot, apply: str, recommendations, plan_targets: dict | None = None
                  ) -> tuple[dict[str, dict[str, float]], dict[str, int]]:
    """Per-pod request deltas and replica floors for a set of levers.

    One definition for the packer, the Cluster Autoscaler replay and the load
    check, so they can never simulate different edits.
    """
    from rightsizer.engine.simulate import _recommendation_index, plan_deltas, replica_running

    levers = set(apply.split("+"))
    # Priced on what the HPA runs at the new floor, never on the floor itself.
    keep = (replica_running(snapshot, recommendations if levers & {"all", "memory"} else None)
            if "replicas" in levers else {})
    if plan_targets:
        return plan_deltas(snapshot, plan_targets), keep
    deltas: dict[str, dict[str, float]] = {}
    wants_all = "all" in levers
    wants_mem = wants_all or "memory" in levers
    for (ns, wl, kind, _c), rec in _recommendation_index(recommendations).items():
        d = deltas.setdefault(f"{ns}/{wl}/{kind}", {"cpu": 0.0, "mem": 0.0})
        if rec.cpu_recommended is not None and wants_all:
            d["cpu"] += rec.cpu_recommended - rec.cpu_request
        if rec.mem_recommended is not None and wants_mem:
            if wants_all or rec.mem_recommended > rec.mem_request:
                d["mem"] += rec.mem_recommended - rec.mem_request
    return deltas, keep


def lever_requests(snapshot, apply: str, recommendations, plan_targets: dict | None = None
                   ) -> dict[tuple[str, str, str], tuple[int | None, int | None]]:
    """The same edits as :func:`lever_changes`, per container, for the replay on
    live pods: (namespace, workload, container) -> (cpu millicores | None,
    memory bytes | None). A container gains or loses kube-scheduler's stand-in
    request depending on whether it ends up with one, so the change has to land
    on the container, not on the pod's total.
    """
    from rightsizer.engine.simulate import _recommendation_index

    def m(cores):
        return None if cores is None else int(round(cores * 1000))

    def b(bytes_):
        return None if bytes_ is None else int(round(bytes_))

    if plan_targets:
        return {k: (m(t.get("cpu")), b(t.get("mem"))) for k, t in plan_targets.items()}
    levers = set(apply.split("+"))
    wants_all = "all" in levers
    wants_mem = wants_all or "memory" in levers
    out = {}
    for (ns, wl, _kind, c), rec in _recommendation_index(recommendations).items():
        cpu = m(rec.cpu_recommended) if wants_all and rec.cpu_recommended is not None else None
        mem = None
        if rec.mem_recommended is not None and wants_mem and (
                wants_all or rec.mem_recommended > rec.mem_request):
            mem = b(rec.mem_recommended)
        if cpu is not None or mem is not None:
            out[(ns, wl, c)] = (cpu, mem)
    return out


def apply_keep(pods: list, keep: dict[str, int]) -> list:
    """Drop surplus replicas the way a ReplicaSet scaling down does.

    It removes pods from the nodes holding the most of its replicas first; ties
    go to the pod name sorting last, so the choice is deterministic and the
    browser's port makes the same one.
    """
    if not keep:
        return pods
    drop: set[int] = set()
    by_wl: dict[str, list] = {}
    for p in pods:
        if p.workload_key in keep and not p.is_daemonset:
            by_wl.setdefault(p.workload_key, []).append(p)
    for wkey, mine in by_wl.items():
        for _ in range(max(0, len(mine) - max(0, keep[wkey]))):
            count: dict[str, int] = {}
            for p in mine:
                if id(p) not in drop:
                    count[p.node] = count.get(p.node, 0) + 1
            victim = max((p for p in mine if id(p) not in drop),
                         key=lambda p: (count.get(p.node, 0), p.name))
            drop.add(id(victim))
    return [p for p in pods if id(p) not in drop]


def placed_pods(snapshot, group_name: str, deltas: dict, keep: dict) -> list:
    """Today's placement with the edits applied, DaemonSets included."""
    from rightsizer.engine.autoscaler import PlacedPod

    pods = []
    for r in (snapshot.placements or {}).get(group_name) or []:
        d = deltas.get(r.get("workload_key", ""), {}) if r.get("owner_kind") != "DaemonSet" else {}
        pods.append(PlacedPod(
            namespace=r.get("namespace", ""), name=r.get("pod", ""), node=r.get("node", ""),
            cpu=max(0.0, float(r.get("cpu", 0.0)) + d.get("cpu", 0.0)),
            memory=max(0.0, float(r.get("mem", 0.0)) + d.get("mem", 0.0)),
            owner_kind=r.get("owner_kind", ""), workload_key=r.get("workload_key", ""),
        ))
    return apply_keep(pods, keep)


def same_type(snapshot, group, *, deltas, keep, min_nodes: int, ca, rules,
              lift: bool | frozenset = False, requests: dict | None = None) -> Outcome | None:
    """Replay Cluster Autoscaler on today's placement after the edits.

    ``lift`` replays it as if the pods it refuses to evict were fixed, which is
    what those blockers cost: ``True`` for every one, or a set of subjects
    (workload keys) for only those.
    """
    import dataclasses

    from rightsizer.engine.eviction import simulate_group

    if requests is not None and (snapshot.facts.kube or {}).get("pods"):
        return kube_same_type(snapshot, group.name, requests=requests, keep=keep,
                              min_nodes=min_nodes, lift=lift)
    if lift is True:
        rules = dataclasses.replace(rules, lifted=True)
    elif lift:
        rules = dataclasses.replace(rules, lift=frozenset(lift))

    run, _ = simulate_group(snapshot, group, ca, rules, min_nodes,
                            request_delta=deltas, keep=keep)
    if run is None:
        return None
    placement = {
        n: [(p.workload_key, p.cpu, p.memory, p.is_daemonset) for p in pods]
        for n, pods in run.placement.items()
    }
    start = len({r.get("node") for r in snapshot.placements.get(group.name, []) if r.get("node")})
    nodes = max(start + run.added - run.total, min_nodes, 1)
    # A pod no node of this type can hold never schedules: the option is not
    # one this group can take, however many nodes it adds.
    return Outcome(nodes=nodes, mode="autoscaler", placement=placement, added=run.added,
                   vetoed=dict(run.vetoed), blockers=dict(run.blockers),
                   unplaceable=len(run.unplaceable))


# -- the exact replay on live objects (careplay + kscheduler) --------------------------------------

def _kube_now(snapshot) -> float | None:
    import datetime as _dt

    try:
        return _dt.datetime.fromisoformat(snapshot.facts.kube["collected_at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None


def _kube_state(snapshot, requests: dict, keep: dict):
    """The cluster after the edits (HPA floors, request rollouts), cached per
    edit set: every group, floor and lift of an option replays from it."""
    from rightsizer.engine.careplay import state_from_kube

    cache = snapshot.__dict__.setdefault("_kube_states", {})
    key = (tuple(sorted(requests.items())), tuple(sorted((keep or {}).items())))
    if key not in cache:
        cache[key] = state_from_kube(snapshot.facts.kube, recommended=requests or None,
                                     keep=keep or None, now=_kube_now(snapshot))
    return cache[key]


def group_workloads(snapshot, group: str) -> tuple[set[str], set[tuple[str, str]]]:
    """Workloads with a pod on ``group``'s nodes: workload keys and (ns, name).
    An option for a group edits these, as the console lists them."""
    from rightsizer.adapters import node_group_of
    from rightsizer.engine.careplay import _workload_of

    cache = snapshot.__dict__.setdefault("_kube_group_workloads", {})
    if group not in cache:
        kube = snapshot.facts.kube or {}
        nodes = {n["name"] for n in kube.get("nodes") or []
                 if node_group_of(n.get("labels")) == group}
        keys, names = set(), set()
        for p in kube.get("pods") or []:
            if p.get("node") in nodes and p.get("owner_kind") not in ("DaemonSet", ""):
                name, kind = _workload_of(p)
                keys.add(f"{p['ns']}/{name}/{kind}")
                names.add((p["ns"], name))
        cache[group] = (keys, names)
    return cache[group]


def scoped(snapshot, group: str, requests: dict, keep: dict) -> tuple[dict, dict]:
    keys, names = group_workloads(snapshot, group)
    return ({k: v for k, v in (requests or {}).items() if (k[0], k[1]) in names},
            {k: v for k, v in (keep or {}).items() if k in keys})


def _fork_state(state):
    import dataclasses as _dc

    return _dc.replace(state, cluster=state.cluster.fork(), group_of=dict(state.group_of),
                       groups=dict(state.groups), unplaceable=list(state.unplaceable),
                       added=dict(state.added))


def kube_baseline(snapshot) -> dict[str, int]:
    """Nodes per group once CA has done what it would do anyway, with nothing
    changed. An option is credited only with the nodes it frees beyond this."""
    from rightsizer.engine.careplay import replay_state

    cache = snapshot.__dict__.get("_kube_baseline")
    if cache is None:
        st = _fork_state(_kube_state(snapshot, {}, {}))
        res = replay_state(st)
        cache = snapshot.__dict__["_kube_baseline"] = {g: len(res.nodes_in(g)) for g in st.groups}
    return cache


def _outcome_for(res, state, group: str, nodes: int, mode: str) -> Outcome:
    names = res.nodes_in(group)
    placement = {
        n: [(p.wkey, p.cpu / 1000, float(p.mem), p.is_daemonset) for p in res.cluster.on[n]]
        for n in names}
    mine = set(names)
    return Outcome(nodes=nodes, mode=mode, placement=placement, added=res.added.get(group, 0),
                   vetoed={n: v for n, v in res.vetoed.items() if n in mine},
                   blockers={n: v for n, v in res.blockers.items() if n in mine},
                   unplaceable=len(state.unplaceable))


def kube_same_type(snapshot, group: str, *, requests: dict, keep: dict, min_nodes: int,
                   lift: bool | frozenset = False) -> Outcome | None:
    """Cluster Autoscaler v1.35.0 over the whole cluster after the edits, on the
    live pods, with kube-scheduler placing every pod that moves."""
    from rightsizer.engine.careplay import Group, replay_state

    requests, keep = scoped(snapshot, group, requests, keep)
    base = _kube_state(snapshot, requests, keep)
    if not any(state_group == group for state_group in base.group_of.values()):
        return None
    st = _fork_state(base)
    g = st.groups.get(group) or Group(name=group)
    st.groups[group] = __import__("dataclasses").replace(g, min=min_nodes)
    res = replay_state(st, lift=frozenset(lift) if lift and lift is not True else frozenset(),
                       lifted=lift is True)
    today = sum(1 for n in base.cluster.order if base.group_of.get(n) == group)
    left = len(res.nodes_in(group))
    background = max(0, today - kube_baseline(snapshot).get(group, today))
    return _outcome_for(res, st, group, max(left + background, min_nodes, 1), "autoscaler")


def _ephemeral_after(tmpl_eph: int, current, inst, measured: dict | None) -> int:
    """The new node's ephemeral storage, in bytes.

    On a type with an instance store the kubelet lives on it (that is what the
    running nodes do, when their capacity matches their store); a type with
    none puts it on the root volume, whose size the launch template sets. The
    same reading :mod:`rightsizer.sources.storage` makes, here for the
    scheduler's ephemeral-storage filter.
    """
    if inst is None or current is None or not getattr(inst, "storage_known", False):
        return tmpl_eph
    store_now = float(getattr(current, "storage_gb", 0) or 0) * 1e9
    on_store = store_now > 0 and tmpl_eph > 0.5 * store_now
    if not on_store:
        return tmpl_eph
    if getattr(inst, "storage_gb", 0):
        return int(tmpl_eph * (inst.storage_gb * 1e9 / store_now))
    root_gb = (measured or {}).get("root_gb")
    return int(root_gb * 1e9 * 0.9) if root_gb else tmpl_eph


def kube_new_type(snapshot, group: str, *, requests: dict, keep: dict, alloc: tuple[float, float],
                  max_pods: int, min_nodes: int, inst=None, current=None) -> Outcome | None:
    """The group moved to another instance type the way it happens: drained,
    re-placed by kube-scheduler, scaled up by CA's estimator, then scaled down
    by CA like the rest of the cluster."""
    import dataclasses as _dc

    from rightsizer.engine.careplay import migrate, replay_state

    requests, keep = scoped(snapshot, group, requests, keep)
    base = _kube_state(snapshot, requests, keep)
    old = [n for n in base.cluster.order if base.group_of.get(n) == group]
    if not old:
        return None
    st = _fork_state(base)
    tmpl = st.cluster.nodes[old[0]]
    labels = dict(tmpl.labels)
    if inst is not None:
        labels["node.kubernetes.io/instance-type"] = inst.name
        labels["beta.kubernetes.io/instance-type"] = inst.name
    # The launch template's node: where it lands (zone) is migrate's to decide,
    # instance by instance, as the ASG does.
    template = _dc.replace(tmpl, name=getattr(inst, "name", "new"), labels=labels,
                           cpu=int(round(alloc[0] * 1000)), mem=int(round(alloc[1])),
                           pods=max_pods or tmpl.pods, gpu=int(getattr(inst, "gpus", 0) or 0),
                           eph=_ephemeral_after(tmpl.eph, current, inst,
                                                (getattr(snapshot, "storage", None) or {}).get(group)),
                           images={}, unschedulable=False, scale_down_disabled=False)
    migrate(st, group, template, min_size=min_nodes)
    res = replay_state(st)
    return _outcome_for(res, st, group, max(len(res.nodes_in(group)), min_nodes, 1), "scheduler")


def volume_zones(pods, zone_bound: dict[str, str]) -> list[str]:
    """The zones a new group needs a node in: one per zone holding a pod's EBS volume."""
    return sorted({z for p in pods if (z := zone_bound.get(f"{p.namespace}/{p.name}"))})


def new_type(snapshot, group, *, deltas, keep, cpu_per_node: float, mem_per_node: float,
             max_pods: int, min_nodes: int, antiaffinity: set[str],
             zone_bound: dict[str, str] | None = None, requests: dict | None = None,
             full_alloc: tuple[float, float] | None = None, inst=None, current=None) -> Outcome:
    """Place by request onto fresh nodes, the way the scheduler fills a new group.

    ``cpu_per_node`` / ``mem_per_node`` are allocatable after DaemonSets. Largest
    first, 100% of allocatable: the scheduler keeps no headroom. A pod on an
    EBS volume needs a node in its volume's zone, so the group keeps at least
    one node in every such zone -- three databases in three zones do not fit
    on one node, however small they are.
    """
    from rightsizer.engine.simulate import Pod, pack

    if requests is not None and full_alloc and (snapshot.facts.kube or {}).get("pods"):
        return kube_new_type(snapshot, group.name, requests=requests, keep=keep,
                             alloc=full_alloc, max_pods=max_pods, min_nodes=min_nodes, inst=inst,
                             current=current)
    pods = [p for p in placed_pods(snapshot, group.name, deltas, keep) if not p.is_daemonset]
    packed = pack(
        [Pod(name=f"{p.namespace}/{p.name}", workload=p.workload_key, cpu=p.cpu, memory=p.memory,
             antiaffinity=p.workload_key if p.workload_key in antiaffinity else "")
         for p in pods],
        cpu_per_node=cpu_per_node, mem_per_node=mem_per_node,
        max_pods_per_node=max_pods, headroom=1.0,
    )
    placement = {
        f"(new {i + 1})": [(p.workload, p.cpu, p.memory, False) for p in node.pods]
        for i, node in enumerate(packed.placement)
    }
    zones = volume_zones(pods, zone_bound or {})
    return Outcome(nodes=max(packed.nodes_required, min_nodes, 1 if pods else 0, len(zones)),
                   mode="scheduler",
                   placement=placement, unplaceable=len(packed.unplaceable))


def busy_loads(snapshot) -> dict:
    return workload_loads(snapshot)


def load_check(outcome: Outcome, *, alloc_cpu: float, alloc_mem: float, loads: dict,
               replicas_now: dict[str, int], replicas_kept: dict[str, int],
               ds_busy: tuple[float, float] = (0.0, 0.0), today: dict | None = None) -> dict:
    """The load each node would really carry, from measured use.

    A request is a scheduling promise and consumes nothing; the kubelet evicts
    on what containers actually hold. Busy use is the p95 of the mean replica
    (see :mod:`rightsizer.engine.pressure`). When replicas are cut, a workload's load
    is conserved, not dropped: the replicas it keeps each carry
    ``now / kept`` times as much. DaemonSets are counted from the placement
    where it has them, else as a constant per node (``ds_busy``).
    """
    if not outcome.placement or alloc_cpu <= 0 or alloc_mem <= 0:
        return {"verdict": "unknown", "notes": ["no placement to check"]}
    hot_cpu = hot_mem = 0.0
    over_mem = over_cpu = 0
    for pods in outcome.placement.values():
        cpu = mem = 0.0
        has_ds = False
        for wkey, cpu_req, mem_req, is_ds in pods:
            has_ds = has_ds or is_ds
            load = loads.get(wkey)
            scale = 1.0
            if not is_ds and replicas_kept.get(wkey):
                scale = max(1.0, replicas_now.get(wkey, 0) / replicas_kept[wkey])
            cpu += (load.cpu_busy if load else cpu_req) * scale
            mem += (load.mem_busy if load else mem_req) * scale
        if not has_ds:
            cpu += ds_busy[0]
            mem += ds_busy[1]
        c, m = cpu / alloc_cpu, mem / alloc_mem
        hot_cpu, hot_mem = max(hot_cpu, c), max(hot_mem, m)
        over_mem += m >= MEM_CRITICAL
        over_cpu += c >= CPU_WARNING
    base_mem = (today or {}).get("hotMem", 0.0)
    notes: list[str] = []
    verdict = "ok"
    # Worse than today, not merely high: a node already running hot is a
    # finding about today, and an option that leaves it no worse is not the
    # cause of it.
    if hot_mem >= MEM_CRITICAL and hot_mem > base_mem + 0.02:
        verdict = "unsafe"
        notes.append(f"{over_mem} node(s) would need {hot_mem:.0%} of their memory at busy load "
                     "-- the kubelet evicts pods before that")
    elif hot_mem >= MEM_WARNING and hot_mem > base_mem + 0.02:
        verdict = "warn"
        notes.append(f"the busiest node would use {hot_mem:.0%} of its memory at busy load")
    # CPU is compressible, but a node past 100% at *busy* load -- a p95, not a
    # spike -- queues every request on it for as long as the load lasts. That
    # is an outage measured in latency, and an HPA reading that utilisation
    # adds replicas the option did not count.
    if hot_cpu >= CPU_WARNING and hot_cpu > (today or {}).get("hotCpu", 0.0) + 0.02:
        verdict = "unsafe"
        notes.append(f"{over_cpu} node(s) would need {hot_cpu:.0%} of their CPU at busy load")
    return {"verdict": verdict, "hotCpu": round(hot_cpu, 4), "hotMem": round(hot_mem, 4),
            "nodesOverMem": over_mem, "notes": notes}


def trend_check(trend: dict | None, *, nodes_now: int, nodes_after: int, min_nodes: int,
                cpu_scale: float, mem_scale: float) -> dict:
    """Would the option's nodes have held what the group's nodes used all month?

    The placement is one moment; a pool that scales with load looks tiny at a
    quiet one -- prod-eu1's engine pool packed onto a single node while the month
    ran it on 18 or more. This reads node-exporter's 6-hourly peaks instead,
    and keeps the group's own scaling pattern: where it ran N nodes the option
    runs ``ceil(N x nodes_after / nodes_now)``, never below its floor. The
    same arithmetic the console's utilisation chart draws, so the verdict and
    the chart cannot disagree.

    ``cpu_scale`` / ``mem_scale`` are the new node's size over today's.
    """
    import math

    if not trend or not trend.get("t"):
        return {"verdict": "unknown", "notes": []}
    ratio = nodes_after / max(nodes_now, 1)
    floor = max(1, min_nodes)
    over = {"cpu": 0, "mem": 0}
    peak = {"cpu": 0.0, "mem": 0.0}
    points = 0
    for i in range(len(trend["t"])):
        n = trend["nodes"][i]
        if not n:
            continue
        after = max(floor, math.ceil(n * ratio - 1e-9))
        seen = False
        for k, scale in (("cpu", cpu_scale), ("mem", mem_scale)):
            used, cap = trend[k][i], trend[f"{k}Cap"][i]
            if used is None or not cap:
                continue
            seen = True
            share = used / (cap / n * scale * after)
            peak[k] = max(peak[k], share)
            over[k] += share > 1.0
        points += seen
    if not points:
        return {"verdict": "unknown", "notes": []}
    notes: list[str] = []
    verdict = "ok"
    # Memory past the nodes is an OOM kill the first time it happens.
    if over["mem"]:
        verdict = "unsafe"
        notes.append(f"the nodes used more memory than this option has in {over['mem']} of "
                     f"{points} 6-hour periods (peak {peak['mem']:.0%})")
    # CPU past the nodes for more than an occasional burst is sustained queueing.
    if over["cpu"] > max(1, points // 20):
        verdict = "unsafe"
        notes.append(f"the nodes used more CPU than this option has in {over['cpu']} of "
                     f"{points} 6-hour periods (peak {peak['cpu']:.0%})")
    elif over["cpu"] and verdict == "ok":
        verdict = "warn"
        notes.append(f"CPU would saturate in {over['cpu']} of {points} 6-hour periods")
    return {"verdict": verdict, "peakCpu": round(peak["cpu"], 4), "peakMem": round(peak["mem"], 4),
            "notes": notes}


def combine(*checks: dict) -> dict:
    """The worst verdict of several checks, with every note that led to it."""
    rank = {"unsafe": 3, "warn": 2, "ok": 1, "unknown": 0}
    out = dict(max(checks, key=lambda c: rank.get(c.get("verdict", "unknown"), 0)))
    out["notes"] = [n for c in checks for n in c.get("notes", [])]
    for c in checks:
        for k, v in c.items():
            if k not in ("verdict", "notes"):
                out.setdefault(k, v)
    return out


#: Headroom an option must leave to be recommended. Fitting is not enough: a
#: node group that peaked at 96% of its memory last month has one busy day, one
#: new replica or one leaking container between it and the OOM killer. Memory
#: is the tighter one because it does not compress; CPU past its target means
#: throttling, not kills. Peaks are the busiest moment of each 6-hour bucket
#: over the whole window (``trend_check``); busy load is the p95 of each
#: node's pods (``load_check``), which sits under the peak.
HEADROOM = {"peakMem": 0.80, "peakCpu": 0.90, "hotMem": 0.85, "hotCpu": 0.90}
_HEADROOM_TEXT = {
    "peakMem": "the nodes' memory peaks at {v:.0%} of this option over the window",
    "peakCpu": "the nodes' CPU peaks at {v:.0%} of this option over the window",
    "hotMem": "the busiest node would use {v:.0%} of its memory at busy load",
    "hotCpu": "the busiest node would use {v:.0%} of its CPU at busy load",
}


def margin(load: dict, today: dict, *, storage: dict | None = None,
           network: dict | None = None) -> dict:
    """Does an option leave the headroom a recommendation needs?

    Judged against today, as the load check is: a group already running above
    a target is a finding about today, and an option that leaves it no worse
    is not the cause. Local disk and network count too -- their own "tight"
    verdicts (70% of the disk; peaks that need burst credits) are the same
    question for those resources.
    """
    notes: list[str] = []
    over: dict[str, float] = {}
    for key, target in HEADROOM.items():
        v = load.get(key)
        if v is None or v <= target or v <= (today.get(key) or 0.0) + 0.02:
            continue
        over[key] = v
        notes.append(_HEADROOM_TEXT[key].format(v=v) + f" -- above the {target:.0%} target")
    for name, check in (("local disk", storage), ("network", network)):
        if (check or {}).get("verdict") == "warn":
            over[name] = 1.0
            notes.append(f"{name}: " + "; ".join((check or {}).get("notes") or ["tight"]))
    return {"ok": not notes, "notes": notes, "over": over, "targets": HEADROOM}
