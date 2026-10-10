"""The exact replay: kube-scheduler v1.34 (kscheduler) and Cluster Autoscaler v1.35 (careplay)."""

from __future__ import annotations

from dataclasses import replace
from rightsizer.engine.careplay import Budget, Group, Settings, estimate, replay
from rightsizer.engine.kscheduler import (SOFT_TAINT, Cluster, KNode, KPod, PodSpec, fits, label_selector_matches,
                                   pod_requests, schedule, score)

GI = 2**30


def node(name, cpu=4000, mem=16 * GI, group="g", **kw):
    labels = {"kubernetes.io/hostname": name, "topology.kubernetes.io/zone": kw.pop("zone", "a"),
              "eks.amazonaws.com/nodegroup": group, **kw.pop("labels", {})}
    return KNode(name=name, cpu=cpu, mem=mem, labels=labels, group=group, **kw)


def pod(name, node_name, cpu=100, mem=GI, owner="ReplicaSet", wkey="ns/web/Deployment", spec=None, **kw):
    kw.setdefault("nz_cpu", cpu or 100)
    kw.setdefault("nz_mem", mem or 200 * 1024 * 1024)
    return KPod(ns=kw.pop("ns", "ns"), name=name, node=node_name, spec=spec or PodSpec(), owner_kind=owner,
                owner_name="web-abc", wkey=wkey, cpu=cpu, mem=mem, **kw)


def ds(node_name):
    return pod(f"ds-{node_name}", node_name, cpu=100, mem=GI // 4, owner="DaemonSet", wkey="kube-system/ds/DaemonSet",
               ns="kube-system")


def run(nodes, pods, **kw):
    cl = Cluster(nodes, pods)
    groups = kw.pop("groups", {"g": Group("g", min=0)})
    return replay(cl, settings=kw.pop("settings", Settings()), group_of={n.name: n.group for n in nodes},
                  groups=groups, budgets=kw.pop("budgets", []), now=kw.pop("now", 10_000), **kw)


# -- kube-scheduler ---------------------------------------------------------------------------------

def test_label_selectors_follow_apimachinery() -> None:
    assert not label_selector_matches(None, {"a": "1"}), "nil selects nothing"
    assert label_selector_matches({"matchLabels": {}, "matchExpressions": []}, {}), "{} selects everything"
    sel = {"matchLabels": {"app": "x"}, "matchExpressions": [{"key": "tier", "operator": "NotIn", "values": ["db"]}]}
    assert label_selector_matches(sel, {"app": "x"}), "NotIn matches an absent key"
    assert not label_selector_matches(sel, {"app": "x", "tier": "db"})


def test_pod_requests_take_the_largest_init_container_and_add_sidecars() -> None:
    containers = [{"cpu": 100, "mem": 100}, {"cpu": None, "mem": None}]
    init = [{"cpu": 500, "mem": 50}, {"cpu": 10, "mem": 10, "sidecar": True}]
    assert pod_requests(containers, init, {}, nonzero=False) == (500, 110, 0, 0)
    # The scheduler scores a container without a request as 100m / 200Mi.
    cpu, mem, _, _ = pod_requests(containers, [], {}, nonzero=True)
    assert (cpu, mem) == (200, 100 + 200 * 1024 * 1024)


def test_filters_refuse_taints_affinity_ports_and_resources() -> None:
    n = node("n1", taints=[{"key": "dedicated", "value": "db", "effect": "NoSchedule"}])
    cl = Cluster([n, node("n2")], [])
    p = pod("p", "")
    assert not fits(p, "n1", cl), "untolerated NoSchedule taint"
    tol = PodSpec(tolerations=[{"key": "dedicated", "operator": "Equal", "value": "db", "effect": "NoSchedule"}])
    assert fits(pod("p", "", spec=tol), "n1", cl)
    pinned = PodSpec(node_required=[{"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In",
                                                            "values": ["n2"]}], "matchFields": []}])
    assert not fits(pod("p", "", spec=pinned), "n1", cl) and fits(pod("p", "", spec=pinned), "n2", cl)
    cl.place(pod("held", "n2", ports=((9100, "TCP", "0.0.0.0"),)), "n2")
    assert not fits(pod("p", "", ports=((9100, "TCP", "10.0.0.1"),)), "n2", cl), "host port taken"
    assert not fits(pod("big", "", cpu=4000), "n2", cl), "100m already requested there"


def test_scores_add_least_allocated_and_balanced_allocation() -> None:
    """Fit: ((4000-2000)*100/4000 + (16Gi-8Gi)*100/16Gi)/2 = 50. Balanced:
    fractions .5 and .5 -> std 0 -> 100. Image locality: none -> 0."""
    cl = Cluster([node("n1")], [pod("a", "n1", cpu=1900, mem=8 * GI - GI, nz_cpu=1900)])
    s = score(pod("p", "", cpu=100, mem=GI), ["n1"], cl)
    # TaintToleration (no taints): 100 x 3; spread skipped (no selector).
    assert s["n1"] == 300 + 50 + 100


def test_ca_soft_taint_steers_pods_away_from_nodes_it_will_remove() -> None:
    cl = Cluster([node("n1"), node("n2")], [pod("a", "n2", cpu=1500)])
    p = pod("p", "")
    assert schedule(p, cl) == "n1", "least allocated"
    cl.extra_taints["n1"] = [SOFT_TAINT]
    assert schedule(p, cl) == "n2", "PreferNoSchedule outweighs least-allocated (x3)"


# -- Cluster Autoscaler ------------------------------------------------------------------------------

def test_utilisation_threshold_and_one_drain_per_pass() -> None:
    nodes = [node(f"n{i}") for i in range(4)]
    pods = [ds(n.name) for n in nodes] + [pod(f"p{i}", f"n{i}", cpu=400) for i in range(4)]
    res = run(nodes, pods)
    assert res.total == 3 and res.iterations >= 3, "one drained node per pass"


def test_safe_to_evict_true_overrides_no_controller_and_bare_pods_vanish() -> None:
    nodes = [node("n1"), node("n2")]
    bare = pod("eng", "n1", owner="", wkey="", safe_to_evict="true")
    res = run(nodes, [bare, pod("x", "n2", cpu=3000)])
    assert res.removed == ["n1"]
    assert all(p.name != "eng" for n in res.cluster.order for p in res.cluster.on[n]), "not recreated"
    held = run(nodes, [pod("eng", "n1", owner="", wkey=""), pod("x", "n2", cpu=3000)])
    assert "has no controller" in held.vetoed["n1"]


def test_kube_system_pod_without_pdb_blocks_for_an_hour() -> None:
    nodes = [node("n1"), node("n2")]
    young = pod("dns", "n1", ns="kube-system", wkey="kube-system/dns/Deployment", created=10_000 - 600)
    old = pod("dns", "n1", ns="kube-system", wkey="kube-system/dns/Deployment", created=10_000 - 7200)
    assert "n1" in run(nodes, [young, pod("x", "n2", cpu=3000)]).vetoed
    assert run(nodes, [old, pod("x", "n2", cpu=3000)]).removed == ["n1"]


def test_pdb_with_no_disruption_left_blocks() -> None:
    nodes = [node("n1"), node("n2")]
    b = Budget(ns="ns", name="web", selector={"matchLabels": {"app": "web"}, "matchExpressions": []}, allowed=0)
    p = pod("w", "n1", labels={"app": "web"})
    res = run(nodes, [p, pod("x", "n2", cpu=3000)], budgets=[b])
    assert "allows no disruption" in res.vetoed["n1"]


def test_group_at_min_size_is_not_a_candidate() -> None:
    nodes = [node("n1"), node("n2")]
    res = run(nodes, [pod("a", "n1"), pod("b", "n2")], groups={"g": Group("g", min=2)})
    assert res.total == 0 and set(res.at_min) == {"n1", "n2"}


def test_unhealthy_cluster_does_not_scale_down() -> None:
    nodes = [node(f"n{i}", ready=i > 4) for i in range(8)]
    res = run(nodes, [])
    assert not res.healthy and res.total == 0


def test_gpu_nodes_use_gpu_utilisation() -> None:
    gpu = node("g1", gpu=1, labels={"k8s.amazonaws.com/accelerator": "t4"})
    busy = pod("infer", "g1", cpu=100, gpu=1)
    res = run([gpu, node("n2")], [busy])
    assert "g1" in res.cluster.nodes and res.removed_empty == ["n2"], "GPU 100% used, whatever the CPU says"


def test_expendable_pods_are_ignored() -> None:
    nodes = [node("n1"), node("n2")]
    filler = pod("placeholder", "n1", cpu=3500, priority=-11)
    res = run(nodes, [filler, pod("x", "n2", cpu=3000)])
    assert res.removed_empty == ["n1"], "below the cutoff: as if it were not there"


def test_asg_option_tags_override_the_threshold() -> None:
    nodes = [node("n1"), node("n2")]
    pods = [pod("a", "n1", cpu=2400), pod("b", "n2", cpu=500)]
    tagged = {"g": Group("g", options={"scaledownutilizationthreshold": "0.7"})}
    assert run(nodes, pods).total == 1
    assert run(nodes, pods, groups=tagged).total == 1


def test_estimator_packs_decreasing_and_stops_on_an_empty_node() -> None:
    tmpl = node("t", cpu=4000, mem=16 * GI)
    pending = [pod(f"p{i}", "", cpu=1500, mem=GI) for i in range(5)]
    n = estimate(pending, tmpl, [], Cluster([], []), 100, "g", {})
    assert n == 3, "two 1.5-core pods per 4-core node"
    huge = [pod("x", "", cpu=9000)]
    assert estimate(huge, tmpl, [], Cluster([], []), 100, "g", {}) == 0, "fits no template node"


# -- where a new instance lands: the ASG's zones, and CA's one-zone template ----------


def _pinned(name, node_name, zone, **kw):
    """A StatefulSet pod whose EBS volume lives in `zone`."""
    terms = (({"matchExpressions": [{"key": "topology.ebs.csi.aws.com/zone", "operator": "In",
                                     "values": [zone]}], "matchFields": []},),)
    return pod(name, node_name, owner="StatefulSet", wkey=f"ns/{name}/StatefulSet", pv_terms=terms, **kw)


def _db_cluster():
    """db-2 in miniature: one node per zone, one database pinned to each."""
    from rightsizer.engine.careplay import State
    nodes, pods, group_of = [], [], {}
    for z in ("a", "b", "c"):
        n = node(f"n-{z}", cpu=64000, mem=256 * GI, group="db", zone=z,
                 labels={"topology.ebs.csi.aws.com/zone": z, "failure-domain.beta.kubernetes.io/zone": z,
                         "topology.k8s.aws/zone-id": f"use1-az-{z}"})
        nodes.append(n); group_of[n.name] = "db"
        pods += [_pinned(f"pg-{z}", n.name, z, cpu=200, mem=2 * GI), ds(n.name)]
    state = State(cluster=Cluster(nodes, pods, {}), group_of=group_of,
                  groups={"db": Group(name="db", min=3, max=10, zones=["a", "b", "c"])},
                  budgets=[], settings=Settings.from_args({}), now=None)
    return state


def test_a_replacement_group_launches_one_node_per_zone_and_every_database_follows_its_volume() -> None:
    from rightsizer.engine.careplay import migrate, replay_state, zone_of
    st = _db_cluster()
    small = replace(st.cluster.nodes["n-a"], name="m6id.xlarge", cpu=2000, mem=5 * GI)
    migrate(st, "db", small, min_size=3)
    res = replay_state(st)
    mine = res.nodes_in("db")
    assert len(mine) == 3 and st.unplaceable == []
    assert sorted(zone_of(st.cluster.nodes[n]) for n in mine) == ["a", "b", "c"], "balanced across the ASG's zones"
    for n in mine:
        labels = st.cluster.nodes[n].labels
        z = zone_of(st.cluster.nodes[n])
        assert labels["topology.ebs.csi.aws.com/zone"] == z and labels["topology.k8s.aws/zone-id"] == f"use1-az-{z}"
        assert [p.name for p in st.cluster.on[n] if not p.is_daemonset] == [f"pg-{z}"], "each database on its zone's node"


def test_ca_does_not_scale_up_for_a_pod_pinned_outside_its_template_zone() -> None:
    """CA sizes a scale-up with a running node as the template -- one zone --
    so a pod whose volume is elsewhere does not fit it and triggers nothing.
    The ASG would launch the next instance in the emptiest zone, but only
    when something asks for one. One node, two databases stranded: exact."""
    from rightsizer.engine.careplay import migrate, replay_state, zone_of
    st = _db_cluster()
    small = replace(st.cluster.nodes["n-a"], name="m6id.xlarge", cpu=2000, mem=5 * GI)
    migrate(st, "db", small, min_size=1)
    res = replay_state(st)
    mine = res.nodes_in("db")
    assert len(mine) == 1 and zone_of(st.cluster.nodes[mine[0]]) == "a", "the first launch: fewest, ties in ASG order"
    assert sorted(p.name for p in st.unplaceable) == ["pg-b", "pg-c"]


def test_the_asg_fills_its_emptiest_zone_first() -> None:
    from rightsizer.engine.careplay import launch_zone, in_zone
    st = _db_cluster()
    assert launch_zone(st.cluster, st.group_of, "db", st.groups["db"]) == "a", "all equal: the ASG's first"
    st.cluster.remove_node("n-b")
    assert launch_zone(st.cluster, st.group_of, "db", st.groups["db"]) == "b"
    assert launch_zone(st.cluster, st.group_of, "db", Group(name="db")) == "", "zones unknown: keep the template's"
    from rightsizer.engine.careplay import zone_ids
    moved = in_zone(st.cluster.nodes["n-a"], "c", zone_ids(st.cluster))
    assert moved.labels["topology.kubernetes.io/zone"] == "c" and moved.labels["topology.k8s.aws/zone-id"] == "use1-az-c"


def test_group_zones_come_from_the_asg_or_else_its_nodes() -> None:
    from rightsizer.engine.careplay import state_from_kube
    kube = {"nodes": [{"name": f"n-{z}", "labels": {"eks.amazonaws.com/nodegroup": "db", "topology.kubernetes.io/zone": z},
                       "alloc": {"cpu": 4000, "mem": GI, "pods": 110}} for z in ("b", "a")],
            "pods": [], "pdbs": [], "namespaces": {}, "pvcs": {}, "pvs": {}, "csinodes": {}, "services": [], "owners": {},
            "asgs": [{"name": "asg", "nodegroup": "db", "min": 1, "max": 5, "zones": ["c", "a", "b"]}]}
    assert state_from_kube(kube).groups["db"].zones == ["c", "a", "b"], "the ASG's, in its order"
    kube["asgs"][0].pop("zones")
    assert state_from_kube(kube).groups["db"].zones == ["a", "b"], "an older snapshot: where its nodes stand"
