"""Karpenter replayed on a pool: the fleet it arrives at, not a node count."""

from __future__ import annotations

from dataclasses import replace

from rightsizer.engine.careplay import Budget, in_zone, launch_zone, zone_ids, zone_of
from rightsizer.engine.karpenter import INSTANCE_TYPE_LABEL, Pool, PoolSimulation, Size
from rightsizer.engine.kscheduler import Cluster, KNode, KPod, PodSpec

GI = 1024**3
SMALL = Size("small", 2, 1800, 6 * GI, 50.0, label="small")
MEDIUM = Size("medium", 4, 3800, 14 * GI, 100.0, label="medium")
LARGE = Size("large", 8, 7800, 30 * GI, 200.0, label="large")


def node(name, size, zone="a"):
    return KNode(name=name, cpu=size.alloc_cpu_m, mem=size.alloc_mem, pods=110,
                 labels={"kubernetes.io/hostname": name, "topology.kubernetes.io/zone": zone,
                         "karpenter.sh/nodepool": "svc", INSTANCE_TYPE_LABEL: size.label}, group="svc")


def pod(name, node_name, cpu, mem_gi, owner="ReplicaSet", wkey="ns/web/Deployment", **kw):
    return KPod(ns="ns", name=name, node=node_name, spec=kw.pop("spec", PodSpec()), owner_kind=owner,
                owner_name="web", wkey=wkey, cpu=cpu, mem=int(mem_gi * GI), nz_cpu=cpu or 100,
                nz_mem=int(mem_gi * GI) or 1, labels={"app": wkey.split("/")[1]}, **kw)


def ds(node_name):
    return pod(f"ds-{node_name}", node_name, 100, 0.25, owner="DaemonSet", wkey="kube-system/ds/DaemonSet")


def sim_for(nodes, pods, pool=None, budgets=()):
    cl = Cluster(nodes, pods, {})
    group_of = {n.name: "svc" for n in nodes}
    pool = pool or Pool("svc", [SMALL, MEDIUM, LARGE])
    by_label = {s.label: s for s in pool.sizes}
    size_of = lambda n: by_label.get(n.labels.get(INSTANCE_TYPE_LABEL, ""))  # noqa: E731
    launch = lambda c, g, grp: launch_zone(c, g, grp, None)  # noqa: E731
    daemons = [replace(p) for p in pods if p.is_daemonset and p.node == nodes[0].name]
    return PoolSimulation(cl, group_of, pool, nodes[0], daemons, list(budgets), None,
                          size_of, zone_of, in_zone, launch, zone_ids(cl))


def fleet(sim):
    out = {}
    for n in sim.nodes():
        s = sim.size_of_node(sim.cl.nodes[n])
        out[s.name] = out.get(s.name, 0) + 1
    return out


def test_provisioning_packs_largest_first_and_launches_the_cheapest_size_that_fits() -> None:
    n = node("n1", LARGE)
    sim = sim_for([n], [ds("n1")] + [pod(f"p{i}", "n1", 3800, 10) for i in range(2)])
    # n1 is full (2 x 3.8 cores + the DaemonSet of 7.8); three new pods: 1500m + 1500m + 400m.
    pending = [pod("q1", "", 1500, 2), pod("q2", "", 1500, 2), pod("q3", "", 400, 1)]
    new, left = sim.provision(pending, sim.cl)
    assert left == [] and len(new) == 1
    # 3400m + 100m DaemonSet does not fit small (1800m): medium is the cheapest that does.
    assert sim.size_of_node(sim.cl.nodes[new[0]]).name == "medium"
    assert sorted(p.name for p in sim.cl.on[new[0]] if not p.is_daemonset) == ["q1", "q2", "q3"]


def test_consolidation_deletes_a_node_whose_pods_fit_elsewhere_and_replaces_with_a_cheaper_one() -> None:
    a, b, c = node("n1", LARGE), node("n2", LARGE), node("n3", MEDIUM)
    pods = [ds("n1"), ds("n2"), ds("n3"),
            pod("p1", "n1", 1000, 2), pod("p2", "n2", 1000, 2), pod("p3", "n3", 500, 1)]
    sim = sim_for([a, b, c], pods)
    sim.consolidate()
    # 2.5 cores and 5 GiB in all: one medium (3.8 cores, 14 GiB) holds everything,
    # cheaper than any two of today's nodes.
    assert fleet(sim) == {"medium": 1}, fleet(sim)
    assert sim.cl.on and sum(1 for n in sim.nodes() for p in sim.cl.on[n] if not p.is_daemonset) == 3


def test_a_do_not_disrupt_pod_or_an_exhausted_pdb_holds_its_node() -> None:
    a, b = node("n1", LARGE), node("n2", LARGE)
    pods = [ds("n1"), ds("n2"), pod("p1", "n1", 500, 1, wkey="ns/db/StatefulSet", owner="StatefulSet"),
            pod("p2", "n2", 500, 1)]
    sim = sim_for([a, b], pods, pool=Pool("svc", [SMALL, MEDIUM, LARGE], undisruptable=frozenset({"ns/db/StatefulSet"})))
    sim.consolidate()
    assert "n1" in sim.nodes() and "n1" in sim.held and "do-not-disrupt" in sim.held["n1"]
    assert "n2" not in sim.nodes(), "the other node's pod moves onto n1 and n2 goes"

    pods = [ds("n1"), ds("n2"), pod("p1", "n1", 500, 1), pod("p2", "n2", 500, 1)]
    sim = sim_for([a, b], pods, budgets=[Budget("ns", "web-pdb", {"matchLabels": {"app": "web"}}, 0)])
    sim.consolidate()
    assert sorted(sim.nodes()) == ["n1", "n2"] and all("PDB" in w for w in sim.held.values())


def test_the_pools_cpu_limit_caps_what_may_be_launched() -> None:
    n = node("n1", SMALL)
    sim = sim_for([n], [ds("n1"), pod("p1", "n1", 1500, 2)], pool=Pool("svc", [SMALL, MEDIUM, LARGE], limit_vcpu=4))
    new, left = sim.provision([pod("q1", "", 1500, 2), pod("q2", "", 1500, 2)], sim.cl)
    # 2 vCPU in use; a small (2) fits the limit, a medium (4) would not.
    assert len(new) == 1 and sim.size_of_node(sim.cl.nodes[new[0]]).name == "small"
    assert [p.name for p in left] == ["q2"], "the second pod has no size left under the limit"


def test_spot_single_node_replacement_needs_fifteen_cheaper_sizes() -> None:
    a = node("n1", LARGE)
    sim = sim_for([a], [ds("n1"), pod("p1", "n1", 500, 1)], pool=Pool("svc", [SMALL, MEDIUM, LARGE], spot=True))
    sim.consolidate()
    assert fleet(sim) == {"large": 1}, "spot-to-spot replacement is refused with a short ladder"
    sim = sim_for([a], [ds("n1"), pod("p1", "n1", 500, 1)])
    sim.consolidate()
    assert fleet(sim) == {"small": 1}, "on-demand: replaced by the cheapest size that holds the pod"


def test_a_volume_pins_a_claim_to_its_zone() -> None:
    a = node("n1", LARGE, zone="b")
    terms = (({"matchExpressions": [{"key": "topology.ebs.csi.aws.com/zone", "operator": "In", "values": ["c"]}]},),)
    p = pod("db-0", "", 500, 1, owner="StatefulSet", wkey="ns/db/StatefulSet", pv_terms=terms)
    sim = sim_for([a], [ds("n1")] + [pod(f"p{i}", "n1", 3800, 10) for i in range(2)])
    new, left = sim.provision([p], sim.cl)
    assert left == [] and zone_of(sim.cl.nodes[new[0]]) == "c"
    assert sim.cl.nodes[new[0]].labels["topology.ebs.csi.aws.com/zone"] == "c"


def test_exported_pool_options_are_fleets_karpenter_arrives_at():
    """A Karpenter pool's rows carry the fleet, its total matches `nodes`, the
    panel is the replay rather than the Cluster Autoscaler mapping, and the
    cost is the fleet's cost -- not one type times a count nobody sets."""
    import pathlib

    import pytest

    from rightsizer.core import layout

    fleet = layout.assemble_fleet(pathlib.Path("../frontend/public/data"))
    if fleet is None:
        pytest.skip("no exported payload")
    pools = 0
    for period in fleet["periods"]:
        for c in period["clusters"]:
            if not (c.get("autoscaler") or {}).get("kind", "").startswith("karpenter"):
                continue
            for g in c.get("nodegroups") or []:
                if g.get("empty") or g.get("source") == "cloud":
                    continue
                est = g.get("autoscaler") or {}
                assert est.get("replayed"), \
                    f"{c['env']}/{g['name']}: panel is not Karpenter's own replay"
                assert g.get("fleet"), f"{c['env']}/{g['name']}: no fleet today"
                assert sum(f["nodes"] for f in g["fleet"]) == g["nodes"]
                for s in g.get("scenarios") or []:
                    if s["key"] == "asis":
                        continue
                    pools += 1
                    assert s.get("fleet"), \
                        f"{c['env']}/{g['name']} {s['key']}: not a replayed fleet"
                    assert sum(f["nodes"] for f in s["fleet"]) == s["nodes"]
                    # Short, like an AWS label: the fleet is shown beside it.
                    assert "Karpenter runs" not in s["label"] and len(s["label"]) <= 60, s["label"]
                    assert s.get("retype") == any("shapeConfigs" in r for r in s["requires"])
    if not pools:
        pytest.skip("no Karpenter pool with options in the export")


def test_the_disruption_budget_limits_how_many_nodes_go_at_once() -> None:
    """Four large nodes of small pods: all fit one medium, but the default
    budget (10% of 4, rounded up) is one node at a time, so multi-node
    consolidation never has its two candidates. Single-node deletes then run
    one by one until the pods no longer fit the rest; nothing cheaper than a
    large exists to replace with, so it stops at larges. `nodes: "0"` stops
    everything."""
    nodes = [node(f"n{i}", LARGE) for i in range(1, 5)]
    pods = [ds(n.name) for n in nodes] + [pod(f"p{i}", f"n{i}", 500, 1) for i in range(1, 5)]
    unbudgeted = sim_for(nodes, pods, pool=Pool("svc", [SMALL, MEDIUM, LARGE], budgets=("100%",)))
    unbudgeted.consolidate()
    assert fleet(unbudgeted) == {"medium": 1}, fleet(unbudgeted)

    default = sim_for(nodes, pods)
    default.consolidate()
    # 4 x 600m = 2.4 cores fits one large (7.8), so deletes run down to one node;
    # a replacement needs a cheaper size that holds 2.0 cores + 100m: medium does.
    assert fleet(default) == {"medium": 1}, fleet(default)

    frozen = sim_for(nodes, pods, pool=Pool("svc", [SMALL, MEDIUM, LARGE], budgets=("0",)))
    frozen.consolidate()
    assert fleet(frozen) == {"large": 4} and len(frozen.held) == 4


def test_a_budget_of_one_cannot_merge_two_nodes_into_a_bigger_one() -> None:
    """Two mediums whose pods only fit together on a large: that is a
    multi-node move (two candidates, one replacement), and a budget of one
    node at a time never allows it."""
    a, b = node("n1", MEDIUM), node("n2", MEDIUM)
    pods = [ds("n1"), ds("n2"), pod("p1", "n1", 3000, 4), pod("p2", "n2", 3000, 4)]
    big = Size("large", 8, 7800, 30 * GI, 150.0, label="large")  # cheaper than two mediums
    sim = sim_for([a, b], pods, pool=Pool("svc", [SMALL, MEDIUM, big], budgets=("100%",)))
    sim.consolidate()
    assert fleet(sim) == {"large": 1}
    sim = sim_for([a, b], pods, pool=Pool("svc", [SMALL, MEDIUM, big], budgets=("10%",)))
    sim.consolidate()
    assert fleet(sim) == {"medium": 2}



HOST = "kubernetes.io/hostname"


def _gateway_spec(kind: str) -> PodSpec:
    """llm-gateway's two soft rules, one at a time."""
    if kind == "anti":
        return PodSpec(pod_anti_pref=[{"weight": 100, "term": {
            "labelSelector": {"matchLabels": {"app": "gw"}}, "namespaces": [], "namespaceSelector": None,
            "topologyKey": HOST, "matchLabelKeys": [], "mismatchLabelKeys": []}}])
    return PodSpec(spread=[{"maxSkew": 1, "topologyKey": HOST, "whenUnsatisfiable": "ScheduleAnyway",
                            "labelSelector": {"matchLabels": {"app": "gw"}}}])


def _gateway_cluster(kind: str):
    """Three nodes of the pool's cheapest size, as on dev2's services pool,
    one gateway replica on each: by requests alone two nodes hold everything,
    and no replacement can be cheaper."""
    nodes = [node(n, SMALL) for n in ("n1", "n2", "n3")]
    spec = _gateway_spec(kind)
    pods = [ds(n.name) for n in nodes] + [
        pod(f"gw-{i}", n.name, 500, 1, wkey="ns/gw/Deployment", spec=spec) for i, n in enumerate(nodes)]
    return sim_for(nodes, pods)


def test_preferred_anti_affinity_keeps_a_node_karpenter_would_otherwise_remove() -> None:
    """Karpenter enforces preferred anti-affinity while it consolidates and only
    relaxes it when a pod fits nowhere -- a new node included, which always
    fits a hostname rule. So it keeps three nodes, as live Karpenter does on
    dev2 ("Can't remove without creating 2 candidates")."""
    sim = _gateway_cluster("anti")
    sim.consolidate()
    assert fleet(sim) == {"small": 3}
    assert any("preferred pod anti-affinity" in r for r in sim.held.values())


def test_hostname_spread_counts_from_zero_whatever_the_other_nodes_hold() -> None:
    """topologygroup.go: a new hostname can always be made, so the global minimum
    is zero and maxSkew 1 allows one matching pod per node. kube-scheduler would
    compare against the least-loaded node and allow the move."""
    sim = _gateway_cluster("spread")
    sim.consolidate()
    assert fleet(sim) == {"small": 3}
    assert any("ScheduleAnyway topology spread" in r for r in sim.held.values())


def test_without_the_soft_rules_the_same_pool_consolidates() -> None:
    nodes = [node(n, SMALL) for n in ("n1", "n2", "n3")]
    pods = [ds(n.name) for n in nodes] + [
        pod(f"gw-{i}", n.name, 500, 1, wkey="ns/gw/Deployment") for i, n in enumerate(nodes)]
    sim = sim_for(nodes, pods)
    sim.consolidate()
    assert sum(fleet(sim).values()) < 3


def test_do_not_disrupt_on_the_pod_holds_its_node_and_a_bare_pod_does_not() -> None:
    """Karpenter reads do-not-disrupt from the pod itself, DaemonSets included;
    a pod with no controller is evicted, not protected."""
    a, b = node("n1", LARGE), node("n2", LARGE)
    held = [ds("n1"), ds("n2"), pod("p1", "n1", 500, 1), pod("pin", "n2", 500, 1, do_not_disrupt=True)]
    sim = sim_for([a, b], held)
    sim.consolidate()
    assert "n2" in sim.nodes() and "do-not-disrupt" in sim.held.get("n2", "")
    bare = [ds("n1"), ds("n2"), pod("p1", "n1", 500, 1), pod("loose", "n2", 500, 1, owner="")]
    sim = sim_for([node("n1", LARGE), node("n2", LARGE)], bare)
    sim.consolidate()
    assert len(sim.nodes()) == 1
