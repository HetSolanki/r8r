"""Tests for the parts where a silent error would produce a wrong dollar figure."""

from __future__ import annotations

import math
import os
import pathlib

import pytest

from rightsizer.sources.collect import logical_workload
from rightsizer.core.config import NODEGROUP_LABEL, Scope, Tunables
from rightsizer.sources import queries as q
from rightsizer.core.models import HPASpec, PDBState, Snapshot, UsageStat
from rightsizer.core.stats import has_enough_data, percentile, recommend_cpu, recommend_memory

TUN = Tunables()
SCOPE = Scope(env="test-cluster")


# -- the invariant that matters most ---------------------------------------


def _all_queries() -> dict[str, str]:
    return {
        "owner_join": q.owner_join(SCOPE),
        "node_label_join": q.node_label_join(SCOPE),
        "cpu": q.cpu_per_container(SCOPE, 300),
        "memory": q.memory_per_container(SCOPE),
        "workload_usage": q.workload_usage(SCOPE, q.cpu_per_container(SCOPE, 300), "max"),
        "requests": q.workload_requests(SCOPE, "cpu"),
        "limits": q.workload_limits(SCOPE, "memory"),
        "pod_to_ng": q.pod_to_nodegroup(SCOPE),
        "workload_to_ng": q.workload_to_nodegroup(SCOPE),
        "ng_nodes": q.nodegroup_node_count(SCOPE),
        "ng_alloc": q.nodegroup_allocatable(SCOPE, "cpu"),
        "ng_ds": q.nodegroup_daemonset_requests(SCOPE, "cpu"),
        "ng_wl": q.nodegroup_workload_requests(SCOPE, "cpu"),
        "ng_pods": q.nodegroup_pod_count(SCOPE),
        "oom": q.oomkilled(SCOPE),
        "hpa_info": q.hpa_info(SCOPE),
        "hpa_target": q.hpa_target_metric(SCOPE),
        "pdb_desired": q.pdb_desired_healthy(SCOPE),
        "pdb_expected": q.pdb_expected_pods(SCOPE),
        "pdb_current": q.pdb_current_healthy(SCOPE),
        "pod_unready": q.unready_pods(SCOPE),
        "pod_waiting": q.container_waiting_reasons(SCOPE),
        "pod_init_waiting": q.init_container_waiting_reasons(SCOPE),
        "node_not_ready": q.node_not_ready(SCOPE),
        "node_cordoned": q.node_cordoned(SCOPE),
    }


@pytest.mark.parametrize("name,query", sorted(_all_queries().items()))
def test_every_selector_pins_env(name: str, query: str) -> None:
    """Thanos is multi-tenant with no cluster label.

    An unpinned selector silently aggregates across clusters -- no error, just
    wrong numbers. Every `{` that opens a selector must carry env.
    """
    selectors = query.count("{")
    pinned = query.count('env="test-cluster"')
    assert pinned == selectors, f"{name}: {selectors} selectors but only {pinned} pinned to env"


def test_daemonset_exclusion_uses_unless() -> None:
    """An inner join on kube_pod_owner silently drops bare pods."""
    assert "unless on(namespace,pod)" in q.nodegroup_workload_requests(SCOPE, "cpu")


def test_join_operands_are_deduplicated() -> None:
    """Thanos serves duplicate series per receive replica."""
    assert q.owner_join(SCOPE).startswith("max by (")
    # One branch per adapter's node-group label, each de-duplicated on its own.
    branches = q.node_label_join(SCOPE)[1:-1].split(" or on(node) ")
    assert branches and all(b.startswith("(max by (node,") for b in branches)


def test_no_percentile_in_promql() -> None:
    """Percentiles do not compose across chunks; they belong client-side."""
    for name, query in _all_queries().items():
        assert "quantile_over_time" not in query, name


def test_workload_usage_rejects_unknown_aggregation() -> None:
    with pytest.raises(ValueError):
        q.workload_usage(SCOPE, "x", "median")


# -- statistics -------------------------------------------------------------


def test_total_is_never_above_sum_of_peaks() -> None:
    """The concurrent footprint cannot exceed every replica peaking at once.

    If it does, the sum and max series disagree and the capacity math is unsafe.
    """
    stat = UsageStat(resource="cpu", namespace="n", workload="w", kind="Deployment",
                     container="c", replicas=10, peak=2.0, total_peak=7.5, samples=9000)
    assert stat.total_peak <= stat.peak * stat.replicas
    assert stat.coincidence == pytest.approx(0.375)


def test_coincidence_is_none_without_data() -> None:
    stat = UsageStat(resource="cpu", namespace="n", workload="w", kind="Deployment",
                     container="c")
    assert stat.coincidence is None


def test_coincidence_undefined_for_single_replica() -> None:
    """With one replica the sum across replicas IS that replica: the ratio is 1.0
    by construction. Reporting it as 100% reads as "replicas peak together", which
    is a claim the data cannot support."""
    for replicas in (0, 1):
        stat = UsageStat(resource="cpu", namespace="n", workload="w", kind="Deployment",
                         container="c", replicas=replicas, peak=0.25, total_peak=0.25)
        assert stat.coincidence is None, f"replicas={replicas} must not report coincidence"
    pair = UsageStat(resource="cpu", namespace="n", workload="w", kind="Deployment",
                     container="c", replicas=2, peak=0.25, total_peak=0.25)
    assert pair.coincidence == pytest.approx(0.5)


def test_percentile_interpolates() -> None:
    assert percentile([], 95) == 0.0
    assert percentile([7.0], 95) == 7.0
    assert percentile(list(range(1, 101)), 50) == pytest.approx(50.5)


def test_cpu_recommendation_rounds_up_and_floors() -> None:
    stat = UsageStat(resource="cpu", namespace="n", workload="w", kind="Deployment",
                     container="c", p95=0.111, samples=9000)
    assert recommend_cpu(stat, TUN) == pytest.approx(0.17)  # 0.111*1.5=0.1665 -> 170m
    tiny = UsageStat(resource="cpu", namespace="n", workload="w", kind="Deployment",
                     container="c", p95=0.0001, samples=9000)
    assert recommend_cpu(tiny, TUN) == pytest.approx(TUN.cpu_floor_millicores / 1000)


def test_memory_uses_peak_not_percentile_and_bumps_on_oom() -> None:
    """Memory overshoot is an OOMKill, so the peak is a floor, not a target."""
    gib = 1024**3
    stat = UsageStat(resource="mem", namespace="n", workload="w", kind="StatefulSet",
                     container="c", p95=1 * gib, peak=8 * gib, samples=9000)
    base = recommend_memory(stat, TUN)
    assert base >= 8 * gib * TUN.mem_headroom - 16 * 1024**2
    stat.oom_killed = True
    assert recommend_memory(stat, TUN) > base


def test_sufficiency_gate_rejects_sparse_series() -> None:
    """A workload alive for 3 of 30 days yields a confident-looking, useless p95."""
    sparse = UsageStat(resource="cpu", namespace="n", workload="w", kind="Deployment",
                       container="c", samples=99)
    assert has_enough_data(sparse, TUN)[0] is False
    partial = UsageStat(resource="cpu", namespace="n", workload="w", kind="Deployment",
                        container="c", samples=int(TUN.expected_samples() * 0.3))
    assert has_enough_data(partial, TUN)[0] is False
    full = UsageStat(resource="cpu", namespace="n", workload="w", kind="Deployment",
                     container="c", samples=TUN.expected_samples())
    assert has_enough_data(full, TUN)[0] is True


# -- workload identity ------------------------------------------------------


def test_replicaset_hash_and_job_stamp_are_stripped() -> None:
    assert logical_workload("ReplicaSet", "scaleops-admissions-6c86c994b8") == (
        "scaleops-admissions", "Deployment")
    assert logical_workload("Job", "backup-1758531600") == ("backup", "CronJob")
    assert logical_workload("StatefulSet", "elasticsearch-es-data") == (
        "elasticsearch-es-data", "StatefulSet")


# -- blockers ---------------------------------------------------------------


def test_hpa_on_utilization_is_zero_savings() -> None:
    """replicas x request is invariant when the HPA holds usage/request constant."""
    assert HPASpec("n", "h", targets=[("cpu", "utilization")]).scales_on_resource_utilization
    assert HPASpec("n", "h", targets=[("memory", "utilization")]).scales_on_resource_utilization
    assert not HPASpec("n", "h", targets=[("cpu", "average_value")]).scales_on_resource_utilization
    assert not HPASpec("n", "h", targets=[("rabbitmq_queue", "average_value")]
                       ).scales_on_resource_utilization


def test_pdb_blocking_drain_is_detected() -> None:
    assert PDBState("n", "p", desired_healthy=3, expected_pods=3).blocks_drain
    assert not PDBState("n", "p", desired_healthy=2, expected_pods=3).blocks_drain
    assert not PDBState("n", "p", desired_healthy=0, expected_pods=0).blocks_drain


def test_unhealthy_pods_can_use_up_a_pdb_budget() -> None:
    """The budget is current_healthy - desired_healthy, not expected - desired."""
    def pdb(desired: int, expected: int, current: int | None) -> PDBState:
        return PDBState("n", "p", desired_healthy=desired, expected_pods=expected,
                        current_healthy=current)

    # maxUnavailable 1 over 3 pods, one crash-looping: fine on paper, zero budget now.
    one_down = pdb(2, 3, 2)
    assert not one_down.blocks_drain
    assert one_down.disruptions_allowed == 0
    assert one_down.blocked_by_unhealthy
    assert not one_down.below_minimum

    # All healthy: one eviction allowed, nothing blocked.
    assert pdb(2, 3, 3).disruptions_allowed == 1
    assert not pdb(2, 3, 3).blocked_by_unhealthy

    # Below its minimum: the unhealthy pods are pinned too.
    assert pdb(1, 2, 0).blocked_by_unhealthy
    assert pdb(1, 2, 0).below_minimum

    # Nothing healthy and nothing required: nothing is being protected, and the
    # unhealthy pods remain evictable -- not a blocker.
    assert not pdb(0, 1, 0).blocked_by_unhealthy

    # A budget that permits nothing by construction is pdb-blocks-drain's case,
    # not this one; reporting it twice would double-count the same PDB.
    assert pdb(3, 3, 2).blocks_drain and not pdb(3, 3, 2).blocked_by_unhealthy

    # A snapshot that predates current_healthy: unknown, never "blocked".
    old = pdb(2, 3, None)
    assert old.disruptions_allowed is None and not old.blocked_by_unhealthy


def test_finished_pods_are_not_reported_unhealthy() -> None:
    """A completed Job pod is not-Ready forever; it is not a failing pod."""
    query = q.unready_pods(SCOPE)
    assert "unless on(namespace,pod)" in query
    assert 'phase=~"Succeeded|Failed"' in query


def test_starting_pods_are_not_reported_unhealthy() -> None:
    """ContainerCreating and PodInitializing are a rollout, not a failure."""
    unready = q.unready_pods(SCOPE)
    assert 'reason=~"ContainerCreating|PodInitializing"' in unready
    for query in (q.container_waiting_reasons(SCOPE), q.init_container_waiting_reasons(SCOPE)):
        assert 'reason!~"ContainerCreating|PodInitializing"' in query


def test_unhealthy_pods_keep_failures_and_drop_startup() -> None:
    from rightsizer.sources.collect import _build_unhealthy_pods

    def series(pod: str, container: str, reason: str) -> dict:
        return {"metric": {"namespace": "ns", "pod": pod, "container": container, "reason": reason}}

    instant = {
        "pod_owner": [],
        "pod_unready": [{"metric": {"namespace": "ns", "pod": "probe-failing"}}],
        "pod_waiting": [
            series("crashing", "app", "CrashLoopBackOff"),
            # Defensive: a startup reason that reaches the builder is still dropped.
            series("creating", "app", "ContainerCreating"),
        ],
        # Main containers of this pod only ever say PodInitializing.
        "pod_init_waiting": [series("init-crashing", "migrate", "CrashLoopBackOff")],
    }
    got = {p["pod"]: p["reasons"] for p in _build_unhealthy_pods(instant)}
    assert got == {
        "crashing": ["app: CrashLoopBackOff"],
        "init-crashing": ["migrate (init): CrashLoopBackOff"],
        "probe-failing": ["not Ready"],
    }


def _unhealthy_pdb_snapshot(with_facts: bool) -> Snapshot:
    from rightsizer.core.models import ClusterFacts

    facts = ClusterFacts()
    if with_facts:
        facts = ClusterFacts(
            collected=True,
            workload_constraints=[
                {"namespace": "ns", "name": "api", "kind": "Deployment", "labels": {"app": "api"}},
                {"namespace": "ns", "name": "web", "kind": "Deployment", "labels": {"app": "web"}},
            ],
            pdb_specs=[{"namespace": "ns", "name": "api-pdb", "selector": {"app": "api"},
                        "max_unavailable": "1"}],
        )
    return Snapshot(
        env="e", collected_at="t", window_days=30, tunables={},
        pdbs=[PDBState("ns", "api-pdb", desired_healthy=2, expected_pods=3, current_healthy=2)],
        workload_nodegroups={"ns/api/Deployment": {"g-api": 1.0},
                             "ns/web/Deployment": {"g-web": 1.0}},
        unhealthy_pods=[{"namespace": "ns", "pod": "api-7f9c-x1",
                         "workload_key": "ns/api/Deployment",
                         "reasons": ["api: CrashLoopBackOff"]}],
        facts=facts,
    )


def test_unhealthy_pdb_blocker_names_the_cause_on_the_right_group() -> None:
    from rightsizer.analysis.blockers import nodegroup_blockers
    from rightsizer.core.models import NodeGroupState

    snap = _unhealthy_pdb_snapshot(with_facts=True)
    here = [b for b in nodegroup_blockers(NodeGroupState("g-api"), snap, TUN, 1.0)
            if b.code == "pdb-unhealthy-blocks-drain"]
    assert len(here) == 1 and here[0].severity == "unsafe"
    detail = here[0].detail
    assert "ns/api-pdb" in detail and "2 of 3 pods healthy" in detail and "0 evictions" in detail
    assert "api-7f9c-x1" in detail and "CrashLoopBackOff" in detail, detail
    assert "listed on every group" not in detail

    # The PDB's pods do not run on g-web: blocking it there would repeat the
    # cluster-wide mistake that marks every group unsafe for one workload.
    elsewhere = [b for b in nodegroup_blockers(NodeGroupState("g-web"), snap, TUN, 1.0)
                 if b.code == "pdb-unhealthy-blocks-drain"]
    assert not elsewhere


def test_unmappable_unhealthy_pdb_is_reported_everywhere_and_says_so() -> None:
    """Without the API facts the PDB cannot be placed; hiding it would be optimistic."""
    from rightsizer.analysis.blockers import nodegroup_blockers
    from rightsizer.core.models import NodeGroupState

    snap = _unhealthy_pdb_snapshot(with_facts=False)
    for group in ("g-api", "g-web"):
        found = [b for b in nodegroup_blockers(NodeGroupState(group), snap, TUN, 1.0)
                 if b.code == "pdb-unhealthy-blocks-drain"]
        assert len(found) == 1, group
        assert "listed on every group" in found[0].detail
        assert "--context" in found[0].detail


def test_unhealthy_pdb_is_a_prerequisite_not_a_zeroed_saving() -> None:
    """A transient budget shortfall delays a saving; it does not remove it."""
    from rightsizer.analysis.actions import Action
    from rightsizer.core.models import Blocker

    a = Action("scale-in", "g", "s", -100.0, "cmd",
               blockers=[Blocker("pdb-unhealthy-blocks-drain", "unsafe", "g", "d")])
    assert not a.blocked
    assert any("unhealthy pods" in p for p in a.prerequisites)


def test_collect_records_current_healthy_and_why_pods_are_unhealthy() -> None:
    from rightsizer.sources.collect import _build_pdbs, _build_unhealthy_pods

    def row(value, **labels):
        return {"metric": labels, "value": [0, str(value)]}

    instant = {
        "pdb_desired": [row(2, namespace="ns", poddisruptionbudget="api-pdb"),
                        row(1, namespace="ns", poddisruptionbudget="old-pdb")],
        "pdb_expected": [row(3, namespace="ns", poddisruptionbudget="api-pdb"),
                         row(2, namespace="ns", poddisruptionbudget="old-pdb")],
        "pdb_current": [row(2, namespace="ns", poddisruptionbudget="api-pdb")],
        "pod_owner": [row(1, namespace="ns", pod="api-7f9c8d6b5-x1",
                          owner_kind="ReplicaSet", owner_name="api-7f9c8d6b5"),
                      row(1, namespace="ns", pod="web-5b4d7c9f2-y2",
                          owner_kind="ReplicaSet", owner_name="web-5b4d7c9f2")],
        "pod_unready": [row(1, namespace="ns", pod="api-7f9c8d6b5-x1"),
                        row(1, namespace="ns", pod="web-5b4d7c9f2-y2")],
        "pod_waiting": [row(1, namespace="ns", pod="api-7f9c8d6b5-x1",
                            container="api", reason="CrashLoopBackOff")],
    }
    pdbs = {p.name: p for p in _build_pdbs(instant)}
    assert pdbs["api-pdb"].current_healthy == 2
    assert pdbs["old-pdb"].current_healthy is None, "a missing series must stay unknown"

    pods = {p["pod"]: p for p in _build_unhealthy_pods(instant)}
    assert pods["api-7f9c8d6b5-x1"]["workload_key"] == "ns/api/Deployment"
    assert pods["api-7f9c8d6b5-x1"]["reasons"] == ["api: CrashLoopBackOff"]
    # Not Ready with no waiting reason: a failing readiness probe, still listed.
    assert pods["web-5b4d7c9f2-y2"]["reasons"] == ["not Ready"]


def test_snapshot_without_pdb_health_still_loads(tmp_path) -> None:
    import json

    path = tmp_path / "old.json"
    path.write_text(json.dumps({
        "env": "e", "collected_at": "t", "window_days": 30, "tunables": {},
        "usage": [], "nodegroups": [], "hpas": [],
        "pdbs": [{"namespace": "ns", "name": "p", "desired_healthy": 1, "expected_pods": 2}],
    }))
    snap = Snapshot.read(path)
    assert snap.pdbs[0].current_healthy is None
    assert snap.unhealthy_pods == []
    assert not snap.pdbs[0].blocked_by_unhealthy


def test_blocked_workload_contributes_no_savings() -> None:
    """A recommendation that cannot be applied must not create a node removal."""
    from rightsizer.analysis.recommend import container_recommendations, nodegroup_recommendations
    from rightsizer.core.models import NodeGroupState

    gib = 1024**3
    stats = []
    for res, req, peak in (("cpu", 2.0, 0.05), ("mem", 4 * gib, 0.2 * gib)):
        stats.append(UsageStat(resource=res, namespace="apps", workload="api",
                               kind="Deployment", container="app", replicas=10,
                               request=req, peak=peak, p95=peak, samples=9000))
    group = NodeGroupState(name="services-1", nodes_current=10, nodes_min_30d=2,
                           nodes_avg_30d=10.0, nodes_peak_30d=10,
                           allocatable_cpu=80.0, allocatable_mem=320 * gib,
                           workload_cpu_requests=20.0, workload_mem_requests=40 * gib)
    snap = Snapshot(env="e", collected_at="t", window_days=30, tunables={},
                    usage=stats, nodegroups=[group],
                    hpas=[HPASpec("apps", "api-hpa", target_kind="Deployment",
                                  target_name="api", targets=[("cpu", "utilization")])],
                    workload_nodegroups={"apps/api/Deployment": {"services-1": 1.0}})
    recs = container_recommendations(snap, TUN)
    assert recs and not recs[0].actionable, "HPA-blocked workload must not be actionable"
    groups = nodegroup_recommendations(snap, TUN, recs)
    assert groups[0].node_hours_saved_month == 0.0


def test_group_with_no_workloads_is_not_sized() -> None:
    """Missing evidence must never read as an empty group."""
    from rightsizer.core.models import NodeGroupState
    from rightsizer.analysis.recommend import nodegroup_recommendations

    group = NodeGroupState(name="engine-1", nodes_current=20, nodes_avg_30d=20.0,
                           allocatable_cpu=160.0, allocatable_mem=640 * 1024**3)
    snap = Snapshot(env="e", collected_at="t", window_days=30, tunables={}, nodegroups=[group])
    out = nodegroup_recommendations(snap, TUN, [])
    assert out[0].node_hours_saved_month == 0.0
    assert any(b.code == "no-workload-data" for b in out[0].blockers)


def test_never_recommends_more_nodes_than_running() -> None:
    from rightsizer.core.models import NodeGroupState
    from rightsizer.analysis.recommend import nodegroup_recommendations

    group = NodeGroupState(name="tiny", nodes_current=1, nodes_avg_30d=1.0, nodes_min_30d=1,
                           allocatable_cpu=4.0, allocatable_mem=16 * 1024**3,
                           workload_cpu_requests=3.0, workload_mem_requests=8 * 1024**3)
    stat = UsageStat(resource="cpu", namespace="n", workload="w", kind="Deployment",
                     container="c", replicas=1, request=3.0, p95=2.9, peak=3.0, samples=9000)
    snap = Snapshot(env="e", collected_at="t", window_days=30, tunables={}, usage=[stat],
                    nodegroups=[group],
                    workload_nodegroups={"n/w/Deployment": {"tiny": 1.0}})
    out = nodegroup_recommendations(snap, TUN, [])
    assert out[0].nodes_target <= math.ceil(group.nodes_avg_30d)


# -- simulator hardening ----------------------------------------------------


def _snap_with(usage, nodegroups, constraints=(), shares=None):
    from rightsizer.core.models import ClusterFacts

    return Snapshot(
        env="e", collected_at="t", window_days=30, tunables={},
        usage=list(usage), nodegroups=list(nodegroups),
        facts=ClusterFacts(collected=True, workload_constraints=list(constraints)),
        workload_nodegroups=shares or {},
    )


def _stat(res, wl, req, peak, replicas, kind="Deployment"):
    return UsageStat(resource=res, namespace="apps", workload=wl, kind=kind,
                     container="app", replicas=replicas, request=req, peak=peak,
                     p95=peak, samples=9000)


def test_pod_footprint_uses_usage_when_it_exceeds_the_request() -> None:
    """A container with no limit can burst far past its request.

    Packing it by the request fills a node that cannot then hold it, so the
    footprint is the larger of the two and the pod is marked usage-driven.
    """
    from rightsizer.core.models import NodeGroupState
    from rightsizer.engine.simulate import pods_for_nodegroup

    gib = 1024**3
    snap = _snap_with(
        [_stat("cpu", "hungry", 0.1, 2.0, 3), _stat("mem", "hungry", 1 * gib, 1 * gib, 3)],
        [NodeGroupState(name="ng", nodes_current=3, allocatable_cpu=24.0)],
        shares={"apps/hungry/Deployment": {"ng": 1.0}},
    )
    pods = pods_for_nodegroup(snap, "ng")
    assert len(pods) == 3
    assert all(p.cpu == pytest.approx(2.0) for p in pods), "request must not win over larger usage"
    assert all(p.usage_driven for p in pods)


def test_antiaffinity_replicas_survive_reconciliation() -> None:
    """A one-per-node workload's replica count is a scheduling floor.

    Scaling it down to reconcile a bursty group would delete the constraint that
    sets the node count.
    """
    from rightsizer.core.models import NodeGroupState
    from rightsizer.engine.simulate import pack, pods_for_nodegroup

    gib = 1024**3
    usage = [_stat("cpu", "sts", 1.0, 1.0, 4, "StatefulSet"),
             _stat("mem", "sts", 1 * gib, 1 * gib, 4, "StatefulSet"),
             _stat("cpu", "bursty", 1.0, 1.0, 400), _stat("mem", "bursty", 1 * gib, 1 * gib, 400)]
    group = NodeGroupState(name="ng", nodes_current=4, allocatable_cpu=32.0,
                           allocatable_mem=128 * gib, workload_cpu_requests=8.0)
    snap = _snap_with(
        usage, [group],
        constraints=[{"namespace": "apps", "name": "sts", "kind": "StatefulSet",
                      "hard_antiaffinity_hostname": True, "replicas": 4}],
        shares={"apps/sts/StatefulSet": {"ng": 1.0}, "apps/bursty/Deployment": {"ng": 1.0}},
    )
    pods = pods_for_nodegroup(snap, "ng", group_state=group)
    anti = [p for p in pods if p.antiaffinity]
    assert len(anti) == 4, "anti-affinity replicas must not be scaled down"
    result = pack(pods, cpu_per_node=8.0, mem_per_node=32 * gib, max_pods_per_node=110)
    assert result.nodes_required >= 4, "one-per-node must force at least one node per replica"


def test_reduction_is_unverified_without_cluster_constraints() -> None:
    """Missing evidence is not an absent constraint."""
    from rightsizer.core.models import ClusterFacts, NodeGroupState
    from rightsizer.adapters.clouds import InstanceType
    from rightsizer.engine.simulate import scenarios

    gib = 1024**3
    group = NodeGroupState(name="ng", nodes_current=8, nodes_avg_30d=8.0,
                           allocatable_cpu=56.0, allocatable_mem=224 * gib,
                           workload_cpu_requests=4.0, workload_mem_requests=8 * gib)
    snap = Snapshot(
        env="e", collected_at="t", window_days=30, tunables={},
        usage=[_stat("cpu", "w", 0.5, 0.1, 8), _stat("mem", "w", 1 * gib, 0.2 * gib, 8)],
        nodegroups=[group], facts=ClusterFacts(collected=True),  # no constraints
        workload_nodegroups={"apps/w/Deployment": {"ng": 1.0}},
    )
    # A ladder with a smaller size, so there is a real alternative to find.
    ladder = [InstanceType("m6id.xlarge", 4, 16.0, 0.2373),
              InstanceType("m6id.2xlarge", 8, 32.0, 0.4746)]
    price = {"m6id.xlarge": 152.44, "m6id.2xlarge": 304.88}
    rows = scenarios(snap, group, ladder, [], current_type="m6id.2xlarge",
                     price_of=lambda t: price[t], max_pods=110)
    shrinking = [s for s in rows if s.key != "asis" and 0 < s.nodes < group.nodes_current]
    assert shrinking, "expected at least one reduction in this fixture"
    assert all(s.unverified for s in shrinking)
    assert all(any("anti-affinity" in r for r in s.requires) for s in shrinking)


# -- cluster autoscaler scale-down loop --------------------------------------


def _pods(spec):
    """spec: {node: [(name, cpu, mem_gib, owner, workload)]}"""
    from rightsizer.engine.autoscaler import PlacedPod

    out = []
    for node, entries in spec.items():
        for name, cpu, mem, owner, wl in entries:
            out.append(PlacedPod(namespace="apps", name=name, node=node, cpu=cpu,
                                 memory=mem * 1024**3, owner_kind=owner, workload_key=wl))
    return out


def _ca(**overrides):
    from rightsizer.engine.autoscaler import parse_ca_args

    return parse_ca_args([f"{k}={v}" for k, v in overrides.items()])


def test_ca_removes_empty_nodes_in_bulk() -> None:
    """Nodes holding only DaemonSet pods are deleted together, not one by one."""
    from rightsizer.engine.autoscaler import simulate_ca_scale_down

    nodes = [f"n{i}" for i in range(6)]
    pods = _pods({n: [(f"ds-{n}", 0.1, 0.2, "DaemonSet", "kube/ds/DaemonSet")] for n in nodes})
    run = simulate_ca_scale_down(nodes=nodes, pods=pods, alloc_cpu=8.0,
                                 alloc_mem=32 * 1024**3, config=_ca(), min_size=1)
    assert len(run.removed_empty) == 5
    assert run.floor_reached
    assert run.iterations <= 3, "empty nodes should go in one bulk pass, not one per loop"


def test_ca_will_not_relocate_a_pod_that_does_not_fit_elsewhere() -> None:
    """CA moves pods onto nodes that remain; it never opens a new one.

    Two half-full nodes can consolidate. Two nodes each holding a pod larger than
    the other's free space cannot, however tidy a global repack would look.
    """
    from rightsizer.engine.autoscaler import simulate_ca_scale_down

    fits = _pods({"a": [("p1", 2.0, 4, "ReplicaSet", "apps/x/Deployment")],
                  "b": [("p2", 2.0, 4, "ReplicaSet", "apps/x/Deployment")]})
    run = simulate_ca_scale_down(nodes=["a", "b"], pods=fits, alloc_cpu=8.0,
                                 alloc_mem=32 * 1024**3, config=_ca())
    assert run.total == 1

    stuck = _pods({"a": [("p1", 5.0, 4, "ReplicaSet", "apps/x/Deployment")],
                   "b": [("p2", 5.0, 4, "ReplicaSet", "apps/x/Deployment")]})
    run = simulate_ca_scale_down(nodes=["a", "b"], pods=stuck, alloc_cpu=8.0,
                                 alloc_mem=32 * 1024**3, config=_ca(
                                     **{"scale-down-utilization-threshold": 0.8}))
    assert run.total == 0
    assert run.not_evacuable


def test_ca_never_evacuates_onto_a_cordoned_or_not_ready_node() -> None:
    """The scheduler places nothing there, so its free space absorbs nothing.

    `a` fits onto `c` -- unless `c` is cordoned, when the only other node, `b`,
    is too full and `a` stays.
    """
    from rightsizer.engine.autoscaler import simulate_ca_scale_down

    pods = _pods({"a": [("p1", 3.0, 4, "ReplicaSet", "apps/x/Deployment")],
                  "b": [("p2", 7.5, 4, "ReplicaSet", "apps/y/Deployment")],
                  "c": [("p3", 4.5, 4, "ReplicaSet", "apps/z/Deployment")]})
    kwargs = dict(nodes=["a", "b", "c"], pods=pods, alloc_cpu=8.0,
                  alloc_mem=32 * 1024**3, config=_ca())

    assert simulate_ca_scale_down(**kwargs).removed == ["a"]

    run = simulate_ca_scale_down(**kwargs, unschedulable=frozenset({"c"}))
    assert run.total == 0
    assert "a" in run.not_evacuable


def test_replay_uses_the_groups_unschedulable_nodes_and_says_so() -> None:
    from rightsizer.engine.eviction import rules_from_snapshot, simulate_group
    from rightsizer.core.models import NodeGroupState

    rows = [
        {"namespace": "apps", "pod": p, "node": n, "cpu": cpu, "mem": 4 * 1024**3,
         "owner_kind": "ReplicaSet", "workload_key": f"apps/{p}/Deployment"}
        for p, n, cpu in (("x", "a", 3.0), ("y", "b", 7.5), ("z", "c", 4.5))
    ]
    snap = Snapshot(env="e", collected_at="t", window_days=30, tunables={},
                    placements={"g": rows})
    group = NodeGroupState("g", nodes_current=3, allocatable_cpu=24.0,
                           allocatable_mem=96 * 1024**3, not_ready_nodes=[],
                           cordoned_nodes=["c"])
    run, checked = simulate_group(snap, group, _ca(), rules_from_snapshot(snap))
    assert run.total == 0, "evacuated onto the cordoned node"
    assert "cordoned / NotReady nodes excluded as destinations (1 here)" in checked

    # A snapshot that predates node health must not claim the check ran.
    old = NodeGroupState("g", nodes_current=3, allocatable_cpu=24.0,
                         allocatable_mem=96 * 1024**3)
    run, checked = simulate_group(snap, old, _ca(), rules_from_snapshot(snap))
    assert run.removed == ["a"]
    assert not any("cordoned" in c for c in checked)


def test_collect_separates_no_unhealthy_nodes_from_not_measured() -> None:
    from rightsizer.sources.collect import _build_nodegroups
    from rightsizer.sources.thanos import SeriesSet

    def row(value, **labels):
        return {"metric": labels, "value": [0, str(value)]}

    g = {NODEGROUP_LABEL: "g"}
    base = {k: [row(v, **g)] for k, v in (("alloc_cpu", 24), ("alloc_mem", 96), ("ds_cpu", 0),
                                          ("ds_mem", 0), ("wl_cpu", 1), ("wl_mem", 1),
                                          ("pods", 3), ("nodes_now", 3))}
    coarse = {"ng_nodes": SeriesSet(values={("g",): [3.0, 3.0]})}

    measured = {**base, "node_not_ready": [row(1, node="n1", **g)], "node_cordoned": []}
    (state,) = _build_nodegroups(measured, coarse, {})
    assert state.not_ready_nodes == ["n1"] and state.nodes_not_ready == 1
    assert state.cordoned_nodes == [] and state.nodes_cordoned == 0, "none is a finding"

    (old,) = _build_nodegroups(base, coarse, {})
    assert old.not_ready_nodes is None and old.nodes_cordoned is None, "unknown is not zero"


def test_phase2_gaps_warn_but_absent_limits_do_not() -> None:
    """A cluster with no CPU limits has no limit series in any chunk: not a failure."""
    from rightsizer.sources.collect import COARSE_IMPACT, OPTIONAL_COARSE, _coverage_warnings
    from rightsizer.sources.thanos import SeriesSet

    sets = {
        "req_cpu": SeriesSet(chunks_total=6, chunks_ok=5),
        "lim_cpu": SeriesSet(chunks_total=6, chunks_ok=0),      # no limits anywhere
        "lim_mem": SeriesSet(chunks_total=6, chunks_ok=3),      # present, but gappy
        "ng_nodes": SeriesSet(chunks_total=6, chunks_ok=6),
    }
    found = _coverage_warnings(sets, impact=COARSE_IMPACT, optional=OPTIONAL_COARSE)
    assert len(found) == 2, found
    assert any(w.startswith("req_cpu: only 5/6") and "requests" in w for w in found)
    assert any(w.startswith("lim_mem: only 3/6") for w in found)
    assert not any(w.startswith("lim_cpu") for w in found)

    # A required set that returned nothing at all is the loudest failure.
    empty = {"ng_nodes": SeriesSet(chunks_total=6, chunks_ok=0)}
    assert _coverage_warnings(empty, impact=COARSE_IMPACT, optional=OPTIONAL_COARSE)


def test_thin_node_history_is_flagged_even_when_every_chunk_answered() -> None:
    """A partial response passes the chunk check and still drops samples."""
    from rightsizer.sources.collect import _history_warnings
    from rightsizer.sources.thanos import SeriesSet

    history = SeriesSet(chunks_total=6, chunks_ok=6,
                        values={("thin",): [3.0] * 102, ("full",): [3.0] * 126})
    found = _history_warnings(history, expected=120)
    assert len(found) == 1 and "thin has 102 of ~120" in found[0]


def test_ca_vetoes_local_storage_bare_and_system_pods() -> None:
    """A veto protects the node holding the pod, not the whole group.

    CA is still free to remove other nodes by moving their pods *onto* the
    protected one, which is why each case asserts on node "a" specifically.
    """
    from rightsizer.engine.autoscaler import simulate_ca_scale_down

    common = dict(alloc_cpu=8.0, alloc_mem=32 * 1024**3, config=_ca())
    spare = ("spare", 0.1, 0.2, "ReplicaSet", "apps/spare/Deployment")

    bare = _pods({"a": [("orphan", 0.1, 0.2, "", "")], "b": [spare]})
    run = simulate_ca_scale_down(nodes=["a", "b"], pods=bare, **common)
    assert "a" not in run.removed
    assert "no controller" in run.vetoed["a"]

    system = _pods({"a": [("coredns", 0.1, 0.2, "ReplicaSet", "kube-system/coredns/Deployment")],
                    "b": [spare]})
    for pod in system:
        if pod.name == "coredns":
            pod.namespace = "kube-system"
    run = simulate_ca_scale_down(nodes=["a", "b"], pods=system, **common)
    assert "a" not in run.removed
    assert "kube-system" in run.vetoed["a"]

    local = _pods({"a": [("cache", 0.1, 0.2, "ReplicaSet", "apps/cache/Deployment")],
                   "b": [spare]})
    run = simulate_ca_scale_down(nodes=["a", "b"], pods=local,
                                 drain_blocked=frozenset({"apps/cache/Deployment"}), **common)
    assert "a" not in run.removed
    assert "local storage" in run.vetoed["a"]

    # And with skip-nodes-with-local-storage off, the same node is removable.
    permissive = simulate_ca_scale_down(
        nodes=["a", "b"], pods=local, drain_blocked=frozenset({"apps/cache/Deployment"}),
        alloc_cpu=8.0, alloc_mem=32 * 1024**3,
        config=_ca(**{"skip-nodes-with-local-storage": "false"}),
    )
    assert "a" not in permissive.vetoed


def test_ca_respects_the_node_group_floor_and_utilisation_threshold() -> None:
    from rightsizer.engine.autoscaler import simulate_ca_scale_down

    nodes = ["a", "b", "c"]
    pods = _pods({n: [(f"p-{n}", 0.2, 0.5, "ReplicaSet", "apps/x/Deployment")] for n in nodes})
    run = simulate_ca_scale_down(nodes=nodes, pods=pods, alloc_cpu=8.0,
                                 alloc_mem=32 * 1024**3, config=_ca(), min_size=2)
    assert len(run.removed) == 1 and run.floor_reached

    # Well above the threshold: not a candidate at all.
    busy = _pods({n: [(f"p-{n}", 7.0, 28, "ReplicaSet", "apps/x/Deployment")] for n in nodes})
    run = simulate_ca_scale_down(nodes=nodes, pods=busy, alloc_cpu=8.0,
                                 alloc_mem=32 * 1024**3, config=_ca())
    assert run.total == 0


def test_ignore_daemonsets_utilization_changes_candidacy() -> None:
    """The flag lowers measured utilisation, so more nodes become candidates."""
    from rightsizer.engine.autoscaler import simulate_ca_scale_down

    spec = {n: [(f"app-{n}", 3.0, 8, "ReplicaSet", "apps/x/Deployment"),
                (f"ds-{n}", 2.0, 4, "DaemonSet", "kube/ds/DaemonSet")] for n in ("a", "b")}
    common = dict(nodes=["a", "b"], pods=_pods(spec), alloc_cpu=8.0, alloc_mem=64 * 1024**3)

    counted = simulate_ca_scale_down(**common, config=_ca(
        **{"ignore-daemonsets-utilization": "false"}))
    ignored = simulate_ca_scale_down(**common, config=_ca(
        **{"ignore-daemonsets-utilization": "true"}))
    assert counted.total == 0, "5/8 cores counted is above the 0.5 threshold"
    assert ignored.total == 1, "3/8 cores once DaemonSets are excluded is below it"


def test_packer_leaves_headroom() -> None:
    """A node filled to 100% has nowhere to put a surge pod or a burst.

    Headroom is applied to the packing budget but utilisation is still reported
    against full allocatable, so the figure shown is how full the node really is.
    """
    from rightsizer.engine.simulate import Pod, pack

    gib = 1024**3
    pods = [Pod(f"p{i}", "apps/w/Deployment", 1.0, 2 * gib) for i in range(8)]
    common = dict(cpu_per_node=8.0, mem_per_node=32 * gib, max_pods_per_node=110)

    full = pack(pods, headroom=1.0, **common)
    assert full.nodes_required == 1
    assert full.cpu_utilization == pytest.approx(1.0)

    spaced = pack(pods, headroom=0.85, **common)
    assert spaced.nodes_required == 2, "8 cores cannot fit in an 6.8-core budget"
    # Reported against full allocatable, not against the budget.
    assert spaced.cpu_utilization < 0.85


def test_headroom_does_not_reject_a_pod_that_fits_the_node() -> None:
    """Oversized is measured against allocatable, not against the budget.

    A pod larger than the headroom budget but smaller than the node is a packing
    constraint -- it gets a node of its own -- not an unplaceable pod.
    """
    from rightsizer.engine.simulate import Pod, pack

    gib = 1024**3
    big = [Pod("big", "apps/w/Deployment", 7.5, 4 * gib)]
    result = pack(big, cpu_per_node=8.0, mem_per_node=32 * gib,
                  max_pods_per_node=110, headroom=0.85)
    assert result.feasible, "7.5 cores fits an 8-core node even past the 6.8-core budget"
    assert result.nodes_required == 1


def test_reported_fill_matches_what_was_packed() -> None:
    """A node's reported usage must equal the sum of the pods on it.

    Free space starts at the headroom budget, so measuring usage against full
    allocatable silently adds the untouched reserve to every node and makes a
    correctly packed node read as completely full.
    """
    from rightsizer.engine.simulate import Pod, pack

    gib = 1024**3
    pods = [Pod(f"p{i}", "apps/w/Deployment", 1.0, 2 * gib) for i in range(4)]
    result = pack(pods, cpu_per_node=8.0, mem_per_node=32 * gib,
                  max_pods_per_node=110, headroom=0.85)
    budget_cpu, budget_mem = 8.0 * 0.85, 32 * gib * 0.85
    for node in result.placement:
        used_cpu = budget_cpu - node.cpu_free
        used_mem = budget_mem - node.mem_free
        assert used_cpu == pytest.approx(sum(p.cpu for p in node.pods))
        assert used_mem == pytest.approx(sum(p.memory for p in node.pods))


def test_scenarios_keep_up_sizes_and_rank_savings_first() -> None:
    """Every feasible shape is returned, tagged by direction.

    A group whose nodes are already tight has no cheaper answer; its useful
    answer is a larger one. Filtering to savings would leave such a group looking
    as though nothing could be said about it. Savings still lead the ranking.
    """
    from rightsizer.core.models import ClusterFacts, NodeGroupState
    from rightsizer.adapters.clouds import InstanceType
    from rightsizer.engine.simulate import scenarios

    gib = 1024**3
    group = NodeGroupState(name="ng", nodes_current=2, nodes_avg_30d=2.0,
                           allocatable_cpu=16.0, allocatable_mem=64 * gib,
                           workload_cpu_requests=14.0, workload_mem_requests=56 * gib)
    snap = Snapshot(
        env="e", collected_at="t", window_days=30, tunables={},
        usage=[_stat("cpu", "w", 7.0, 6.5, 2), _stat("mem", "w", 28 * gib, 27 * gib, 2)],
        nodegroups=[group], facts=ClusterFacts(collected=True),
        workload_nodegroups={"apps/w/Deployment": {"ng": 1.0}},
    )
    ladder = [InstanceType("m6id.2xlarge", 8, 32.0, 0.4746),
              InstanceType("m6id.4xlarge", 16, 64.0, 0.9492)]
    price = {"m6id.2xlarge": 304.88, "m6id.4xlarge": 609.77}
    rows = scenarios(snap, group, ladder, [], current_type="m6id.2xlarge",
                     price_of=lambda t: price[t], max_pods=110)

    assert rows[0].key == "asis", "the baseline is always first"
    assert rows[0].direction == "neutral"
    assert not any(r.key.startswith("rightsize") for r in rows), \
        "no actionable recommendations, so no request-change row"

    options = rows[1:]
    assert options, "a bigger instance is a real option even though it costs more"
    assert {o.direction for o in options} <= {"saves", "costs", "neutral"}
    # Savings lead, increases trail.
    directions = [o.direction for o in options]
    assert directions == sorted(directions, key=lambda d: {"saves": 0, "neutral": 1, "costs": 2}[d])
    for o in options:
        assert o.rationale, "every option says why it is on the list"
        if o.direction == "costs":
            assert o.delta_usd > 0
        elif o.direction == "saves":
            assert o.delta_usd < 0


def test_a_tight_group_says_so_on_its_baseline() -> None:
    """When observed nodes exceed the headroom target, the baseline names it."""
    from rightsizer.core.models import ClusterFacts, NodeGroupState
    from rightsizer.adapters.clouds import InstanceType
    from rightsizer.engine.simulate import scenarios

    gib = 1024**3
    group = NodeGroupState(
        name="ng", nodes_current=2, nodes_avg_30d=2.0,
        allocatable_cpu=16.0, allocatable_mem=64 * gib,
        workload_cpu_requests=15.0, workload_mem_requests=60 * gib,
        occupancy=[{"cpu": 7.6, "mem": 30.0, "pods": 4, "dsCpu": 0.2, "dsMem": 0.5, "dsPods": 2}] * 2,
    )
    snap = Snapshot(
        env="e", collected_at="t", window_days=30, tunables={},
        usage=[_stat("cpu", "w", 7.5, 7.0, 2), _stat("mem", "w", 30 * gib, 29 * gib, 2)],
        nodegroups=[group], facts=ClusterFacts(collected=True),
        workload_nodegroups={"apps/w/Deployment": {"ng": 1.0}},
    )
    rows = scenarios(snap, group, [InstanceType("m6id.2xlarge", 8, 32.0, 0.4746),
                                   InstanceType("m6id.4xlarge", 16, 64.0, 0.9492)],
                     [], current_type="m6id.2xlarge",
                     price_of=lambda t: 304.88 if "2x" in t else 609.77, max_pods=110)
    assert "tight" in rows[0].rationale


def test_actions_and_scenarios_agree() -> None:
    """The Actions tab and the Scenarios tab must not disagree about a node group.

    They once had separate implementations of the same search, and only one of
    them received the headroom fix -- so the two views proposed different instance
    types for the same group. This asserts they share a single source.
    """
    from rightsizer.analysis.actions import simulated_resize_actions
    from rightsizer.core.config import Tunables
    from rightsizer.core.models import ClusterFacts, NodeGroupState
    from rightsizer.adapters.clouds import InstanceType
    from rightsizer.analysis.recommend import nodegroup_recommendations
    from rightsizer.engine.simulate import scenarios

    gib = 1024**3
    group = NodeGroupState(name="ng", nodes_current=8, nodes_avg_30d=8.0,
                           allocatable_cpu=64.0, allocatable_mem=256 * gib,
                           workload_cpu_requests=12.0, workload_mem_requests=24 * gib,
                           pod_count=16, daemonset_pods=8)
    placements = {"ng": [
        {"namespace": "apps", "pod": f"p{i}", "node": f"n{i // 2}", "cpu": 1.5,
         "mem": 3 * gib, "owner_kind": "ReplicaSet", "workload_key": "apps/w/Deployment"}
        for i in range(8)
    ]}
    snap = Snapshot(
        env="e", collected_at="t", window_days=30, tunables={},
        usage=[_stat("cpu", "w", 1.5, 0.4, 8), _stat("mem", "w", 3 * gib, 1 * gib, 8)],
        nodegroups=[group],
        # The action path reads the instance type from the AWS node-group config,
        # so a fixture without it exercises nothing.
        facts=ClusterFacts(
            collected=True,
            nodegroup_configs=[{"name": "ng", "instance_types": ["m6id.2xlarge"],
                                "min_size": 1, "max_size": 20, "desired_size": 8}],
        ),
        placements=placements,
        workload_nodegroups={"apps/w/Deployment": {"ng": 1.0}},
    )
    ladder = [InstanceType("m6id.xlarge", 4, 16.0, 0.2373),
              InstanceType("m6id.2xlarge", 8, 32.0, 0.4746),
              InstanceType("m6id.4xlarge", 16, 64.0, 0.9492)]
    price = {i.name: i.on_demand_hourly * 730 for i in ladder}
    tun = Tunables()
    monkey_price = {k: round(v, 2) for k, v in price.items()}
    assert monkey_price  # prices come from the fixture, never the network

    rows = scenarios(snap, group, ladder, [], current_type="m6id.2xlarge",
                     price_of=lambda t: price[t], max_pods=110,
                     headroom=tun.pack_headroom, min_size=1)
    asis = next(r for r in rows if r.key == "asis")
    feasible = [r for r in rows if r.key != "asis" and r.feasible and r.nodes > 0]
    best = min(feasible, key=lambda r: (r.monthly_usd, r.nodes))

    recs = nodegroup_recommendations(snap, tun, [])
    # Same ladder and same prices, so any disagreement is the two code paths
    # diverging rather than them searching different spaces.
    actions, _ = simulated_resize_actions(
        snap, recs, cluster="c", region="us-east-1", profile=None, discount=0.0,
        recommendations=[], ladder_for=lambda _t: ladder, price_for=lambda t: price[t],
    )
    saving = [a for a in actions if a.subject == "ng" and a.monthly_delta < 0]
    if best.monthly_usd < asis.monthly_usd - 1:
        assert saving, "scenarios found a cheaper shape but actions proposed nothing"
        assert saving[0].monthly_delta == pytest.approx(best.delta_usd, abs=1.0)
        assert str(best.nodes) in saving[0].summary
    else:
        assert not saving, "actions proposed a saving that scenarios does not support"


# -- trend collection ------------------------------------------------------


def test_write_trends_skips_files_already_on_disk(tmp_path, monkeypatch):
    """Widening --limit must collect the newcomers, not re-pay for the set.

    Each container costs two range queries against Thanos, so a second run
    over an overlapping selection is the difference between a minute and
    half an hour.
    """
    from rightsizer.sources import trends

    calls: list[str] = []

    def fake_collect(settings, key, resource):
        calls.append(f"{key.slug}:{resource}")
        return {"bands": {}, "sample": [], "podIdentities": 0}

    monkeypatch.setattr(trends, "collect_trend", fake_collect)

    class _S:
        scope = SCOPE

    keys = [trends.TrendKey("ns", "a", "c"), trends.TrendKey("ns", "b", "c")]
    written, skipped, empty = trends.write_trends(_S(), keys, tmp_path)
    assert (written, skipped) == (2, 0)
    # The stub returns no series, which is exactly the failure the empty list
    # exists to surface.
    assert len(empty) == 2
    assert len(calls) == 4  # cpu + memory for each

    calls.clear()
    keys.append(trends.TrendKey("ns", "d", "c"))
    written, skipped, _ = trends.write_trends(_S(), keys, tmp_path)
    assert (written, skipped) == (1, 2)
    assert {c.split(":")[0] for c in calls} == {"ns__d__c"}

    calls.clear()
    written, skipped, _ = trends.write_trends(_S(), keys, tmp_path, refresh=True)
    assert (written, skipped) == (3, 0)


def test_trend_slug_matches_the_console(tmp_path):
    """The filename the collector writes is the path the UI fetches."""
    from rightsizer.sources.trends import TrendKey

    assert TrendKey("apps", "web-api/v2", "api").slug == "apps__web-api-v2__api"


# -- the console payload ---------------------------------------------------


def _snapshot(env: str):
    """A collected snapshot in this checkout, in either layout."""
    from rightsizer.core import layout

    path = layout.find_snapshot(pathlib.Path("out"), env)
    if not path.exists():
        pytest.skip(f"no collected snapshot for {env}")
    return path


def _exported_fleet() -> dict:
    """The console's data, assembled from public/data/<env>/ as the console does."""
    from rightsizer.core import layout

    fleet = layout.assemble_fleet(pathlib.Path("../frontend/public/data"))
    if fleet is None:
        pytest.skip("no exported payload")
    return fleet


def test_container_rows_carry_both_resources():
    """A missing resource key is silent: `_usage_fields` returns {} and the
    console renders "peak 0.00 GiB" for every container. Snapshots spell it
    "mem"; asking for "memory" shipped exactly that for 354 rows."""
    from rightsizer.output import console as export_ui
    from rightsizer.core.models import Snapshot

    snap = Snapshot.read(_snapshot("prod-us1"))
    idx = export_ui._usage_index(snap)
    keyed = {k[4] for k in idx}
    assert keyed <= {"cpu", "mem"}, f"unexpected resource spellings: {keyed}"

    with pytest.raises(ValueError):
        export_ui._usage_fields(idx, object(), "memory")

    from rightsizer.core.config import Tunables
    from rightsizer.analysis.recommend import container_recommendations

    tun = Tunables(**snap.tunables) if snap.tunables else Tunables()
    recs = container_recommendations(snap, tun)
    with_cpu = with_mem = 0
    for r in recs:
        cpu = export_ui._usage_fields(idx, r, "cpu")
        mem = export_ui._usage_fields(idx, r, "mem", 1024**3)
        if "cpuPeak" in cpu:
            with_cpu += 1
            if "memPeak" in mem:
                with_mem += 1
    assert with_cpu > 100, f"only {with_cpu} recommendations carried CPU usage"
    assert with_mem == with_cpu, (
        f"{with_cpu - with_mem} of {with_cpu} rows have CPU usage but no memory usage"
    )


# -- plan simulation -------------------------------------------------------


def test_plan_parsing_rejects_what_it_cannot_represent():
    from rightsizer.analysis.plan import Plan, PlanError, parse_cpu, parse_memory

    assert parse_cpu("250m") == pytest.approx(0.25)
    assert parse_cpu("1.5") == pytest.approx(1.5)
    assert parse_memory("2Gi") == 2 * 1024**3
    assert parse_memory("512Mi") == 512 * 1024**2

    p = Plan.parse(["# c", "ns/wl/c cpu=100m mem=1Gi", "", "ns/wl2/c2 cpu=recommended"])
    assert len(p.changes) == 2
    assert p.changes[0].cpu == pytest.approx(0.1)
    assert p.changes[1].cpu == "recommended"

    for bad in (["wl/c cpu=1"], ["ns/wl/c"], ["ns/wl/c cpu"], ["ns/wl/c disk=1"],
                ["ns/wl/c cpu=banana"], ["# only a comment"]):
        with pytest.raises(PlanError):
            Plan.parse(bad)


def test_plan_below_measured_use_frees_nothing_and_is_flagged():
    """The failure this feature exists for.

    A request cut below what a container is measured using does not shrink its
    footprint -- the node still has to hold the usage -- so the node count must
    not move, and the plan must say the pod is now running unreserved.
    """
    from rightsizer.analysis.plan import Plan, resolve, review, simulate
    from rightsizer.core.config import Tunables
    from rightsizer.core.models import Snapshot
    from rightsizer.analysis.recommend import container_recommendations

    snap = Snapshot.read(_snapshot("prod-us1"))
    tun = Tunables(**snap.tunables) if snap.tunables else Tunables()
    recs = container_recommendations(snap, tun)

    # A container measured well above whatever we are about to reserve for it.
    stat = max(
        (u for u in snap.usage if u.resource == "mem" and u.peak > 2 * 1024**3),
        key=lambda u: u.peak,
    )
    target = stat.peak / 8
    plan = Plan.parse([
        f"{stat.namespace}/{stat.workload}/{stat.container} mem={int(target)}"
    ])
    resolved, missing = resolve(snap, plan, recs)
    assert not missing and resolved

    findings = review(resolved)
    assert any(f.code == "memory-below-peak" and f.severity == "unsafe" for f in findings), (
        "cutting memory to an eighth of the measured peak was not flagged unsafe"
    )

    out = simulate(snap, plan, resolved, tun, price_of=lambda _t: 100.0)
    assert out, "the plan touched no node group"
    for o in out:
        assert o.nodes_after >= o.nodes_before, (
            f"{o.group}: freed {o.nodes_freed} node(s) by under-reserving; the pods "
            "still use what they use"
        )
        assert o.absorbed_pods > 0


def test_plan_above_recommendation_costs_nodes():
    """Being generous has to show up as a cost, not round to zero."""
    from rightsizer.analysis.plan import Plan, resolve, simulate
    from rightsizer.core.config import Tunables
    from rightsizer.core.models import Snapshot
    from rightsizer.analysis.recommend import container_recommendations

    snap = Snapshot.read(_snapshot("prod-us1"))
    tun = Tunables(**snap.tunables) if snap.tunables else Tunables()
    recs = container_recommendations(snap, tun)
    big = max(
        (r for r in recs if r.replicas > 20 and r.cpu_request > 0),
        key=lambda r: r.replicas * r.cpu_request,
    )
    plan = Plan.parse([
        f"{big.namespace}/{big.workload}/{big.container} cpu={int(big.cpu_request * 2000)}m"
    ])
    resolved, _ = resolve(snap, plan, recs)
    out = simulate(snap, plan, resolved, tun, price_of=lambda _t: 100.0)
    assert out
    worst = max(out, key=lambda o: o.nodes_after - o.nodes_before)
    assert worst.nodes_after > worst.nodes_before, "doubling a request freed or held nodes"
    assert worst.delta_usd > 0, "more nodes did not cost more"


def test_plan_and_scenarios_pack_the_same_baseline():
    """Both go through the same packer, so their untouched baselines must agree."""
    from rightsizer.analysis.plan import Plan, resolve, simulate
    from rightsizer.core.config import Tunables
    from rightsizer.core.models import Snapshot
    from rightsizer.analysis.recommend import container_recommendations
    from rightsizer.engine.simulate import pack, pods_for_nodegroup

    snap = Snapshot.read(_snapshot("prod-us1"))
    tun = Tunables(**snap.tunables) if snap.tunables else Tunables()
    recs = container_recommendations(snap, tun)
    r = next(x for x in recs if x.actionable and x.cpu_recommended is not None)
    plan = Plan.parse([f"{r.namespace}/{r.workload}/{r.container} cpu=recommended"])
    resolved, _ = resolve(snap, plan, recs)
    out = simulate(snap, plan, resolved, tun, price_of=lambda _t: 100.0)

    for o in out:
        g = next(x for x in snap.nodegroups if x.name == o.group)
        pods = pods_for_nodegroup(snap, g.name, apply="current",
                                  recommendations=[], group_state=g)
        direct = pack(pods, cpu_per_node=g.alloc_cpu_per_node - g.ds_cpu_per_node,
                      mem_per_node=g.alloc_mem_per_node - g.ds_mem_per_node,
                      max_pods_per_node=snap.facts.max_pods_for(g.name) or 110,
                      headroom=tun.pack_headroom)
        expected = max(direct.nodes_required, o.min_size) if o.min_size else direct.nodes_required
        assert o.nodes_before == expected, (
            f"{o.group}: plan baseline {o.nodes_before} vs packer {expected}"
        )


def test_two_targets_below_the_peak_pack_identically():
    """Why 300m and 400m gave the same answer, asserted.

    Footprint is max(request, measured peak). Two proposals on the same side of
    the peak are the same proposal as far as capacity is concerned; one above it
    is not. If this ever stops holding, the "did not shrink" explanation in the
    report is lying.
    """
    from rightsizer.analysis.plan import Plan, resolve, simulate
    from rightsizer.core.config import Tunables
    from rightsizer.core.models import Snapshot
    from rightsizer.analysis.recommend import container_recommendations

    snap = Snapshot.read(_snapshot("prod-us1"))
    tun = Tunables(**snap.tunables) if snap.tunables else Tunables()
    recs = container_recommendations(snap, tun)

    stat = max(
        (u for u in snap.usage
         if u.resource == "cpu" and u.replicas > 30 and u.peak > 0.3),
        key=lambda u: u.replicas * u.peak,
    )
    target = f"{stat.namespace}/{stat.workload}/{stat.container}"
    peak_m = stat.peak * 1000

    def footprint(millicores):
        plan = Plan.parse([f"{target} cpu={int(millicores)}m"])
        resolved, _ = resolve(snap, plan, recs)
        out = simulate(snap, plan, resolved, tun, price_of=lambda _t: 100.0)
        o = max(out, key=lambda x: x.touched_pods)
        return round(o.cpu_footprint_after, 3), o.nodes_after, o.absorbed_detail

    low, low_nodes, low_detail = footprint(peak_m * 0.4)
    mid, mid_nodes, mid_detail = footprint(peak_m * 0.8)
    high, high_nodes, _ = footprint(peak_m * 1.6)

    assert low == mid, "two requests below the measured peak packed differently"
    assert low_nodes == mid_nodes
    assert high > mid, "a request above the measured peak did not grow the footprint"
    assert high_nodes >= mid_nodes

    # And the report must name the number that overrode the request, or the
    # identical results above look like a bug to whoever reads them.
    assert low_detail and mid_detail, "absorbed cuts were not explained"
    assert any(abs(row[3] - stat.peak) < 1e-6 for row in low_detail), (
        f"the binding peak was not reported: {low_detail}"
    )


def test_trend_bands_are_bucketed_not_sampled():
    """The chart must read the same samples the recommendation does.

    A 6-hourly instant read of a 5-minute series plots 1 point in 72 and misses
    every spike between them: measured on api-server, instant sampling showed
    a 194m peak against a true 529m. The chart then contradicts the number it
    exists to justify, so every band is bucketed over its step.
    """
    from rightsizer.sources import trends
    from rightsizer.core.config import Scope, Settings, Tunables

    captured: dict[str, str] = {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def query_range(self, expr, *a, **k):
            captured[expr] = expr
            return []

    original = trends.ThanosClient
    trends.ThanosClient = _Client
    try:
        st = Settings(scope=Scope(env="test-cluster"), tunables=Tunables())
        for resource in ("cpu", "mem"):
            trends.collect_trend(st, trends.TrendKey("ns", "wl", "c"), resource=resource)
    finally:
        trends.ThanosClient = original

    assert captured, "no queries were issued"
    bucket = f"[{trends.TREND_STEP_SECONDS}s:{trends.BUCKET_RESOLUTION}s]"
    unbucketed = [e for e in captured if bucket not in e]
    assert not unbucketed, f"these trend queries sample instead of bucket: {unbucketed}"

    # The envelope edges must keep their meaning across the bucket -- a `max`
    # band reduced with avg_over_time is not a maximum.
    assert any(e.startswith("max_over_time((max(") for e in captured)
    assert any(e.startswith("min_over_time((min(") for e in captured)

    # And the bucket resolution must track the resolution the snapshot's own
    # statistics are computed at, or the two drift apart again.
    assert trends.BUCKET_RESOLUTION == Tunables().step_seconds


def test_trend_selects_only_its_own_workload():
    """Namespace + container name is not a workload.

    Three postgres StatefulSets in one namespace each run a container called
    `database`. Selecting on namespace and container alone plotted all three on
    one chart, and the hottest line was a different database with 2.4GiB
    against this one's 1.09GiB -- which read as the recommendation being wrong.
    """
    from rightsizer.sources import trends

    dep = trends.owner_selector("Deployment", "api-server")
    assert 'owner_kind="ReplicaSet"' in dep
    # Anchored, and a hash carries no dash, so a name that is a prefix of
    # another workload's cannot swallow it.
    import re as _re

    pattern = _re.search(r'owner_name=~"([^"]+)"', dep).group(1)
    assert _re.fullmatch(pattern, r"api\-server-5dcfdd95df".replace("\\", ""))
    assert not _re.fullmatch(pattern, "api-server-worker-5dcfdd95df")

    assert trends.owner_selector("StatefulSet", "pg-a") == (
        'owner_kind="StatefulSet", owner_name="pg-a"'
    )

    captured: list[str] = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def query_range(self, expr, *a, **k):
            captured.append(expr)
            return []

    from rightsizer.core.config import Scope, Settings, Tunables

    original = trends.ThanosClient
    trends.ThanosClient = _Client
    try:
        st = Settings(scope=Scope(env="test-cluster"), tunables=Tunables())
        key = trends.TrendKey("apps", "postgres-main", "database",
                              "StatefulSet")
        for resource in ("cpu", "mem"):
            trends.collect_trend(st, key, resource=resource)
    finally:
        trends.ThanosClient = original

    assert captured
    unscoped = [e for e in captured if "kube_pod_owner" not in e]
    assert not unscoped, f"these trend queries match any owner's pods: {unscoped}"
    assert all('owner_name="postgres-main"' in e for e in captured)


def test_replica_count_band_is_not_scaled_to_bytes():
    """A pod count divided by 1024**3 reads as zero replicas."""
    from rightsizer.sources import trends
    from rightsizer.core.config import Scope, Settings, Tunables

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def query_range(self, expr, *a, **k):
            # One value for every query, so the only difference in the output
            # is the scaling each band applies.
            return [{"metric": {}, "values": [[1700000000, str(2 * 1024**3)]]}]

    original = trends.ThanosClient
    trends.ThanosClient = _Client
    try:
        st = Settings(scope=Scope(env="test-cluster"), tunables=Tunables())
        doc = trends.collect_trend(
            st, trends.TrendKey("ns", "wl", "c", "Deployment"), resource="mem"
        )
    finally:
        trends.ThanosClient = original

    assert doc["bands"]["max"][0][1] == pytest.approx(2.0), "memory band lost its GiB scaling"
    assert doc["bands"]["replicas"][0][1] == 2 * 1024**3, (
        f"replica count was divided by the byte scale: {doc['bands']['replicas'][:1]}"
    )


# -- eviction constraints --------------------------------------------------


def test_safe_to_evict_false_blocks_drain_on_its_own():
    """CA's veto list names the annotation without qualification.

    Treating `safe-to-evict: "false"` as an emptyDir modifier means a stateless
    workload carrying it looks movable, and the tool predicts a node removal
    Cluster Autoscaler will never perform.
    """
    from rightsizer.sources.kube import blocks_drain

    assert blocks_drain(local_storage=False, safe_to_evict=False, do_not_disrupt=False)
    assert blocks_drain(local_storage=True, safe_to_evict=None, do_not_disrupt=False)
    # Karpenter's annotation is not a Cluster Autoscaler rule (v1.35 has none).
    assert not blocks_drain(local_storage=False, safe_to_evict=True, do_not_disrupt=True)
    assert not blocks_drain(local_storage=True, safe_to_evict=True, do_not_disrupt=False)
    assert not blocks_drain(local_storage=False, safe_to_evict=None, do_not_disrupt=False)

    # The dict form the snapshot stores must agree with the dataclass form.
    from rightsizer.analysis.blockers import _blocks_drain
    from rightsizer.sources.kube import WorkloadConstraints

    for local, evict, dnd in [(a, b, c) for a in (True, False)
                              for b in (True, False, None) for c in (True, False)]:
        w = WorkloadConstraints(namespace="n", name="w", kind="Deployment",
                                local_storage=local, safe_to_evict=evict,
                                do_not_disrupt=dnd)
        assert w.blocks_drain == _blocks_drain(
            {"local_storage": local, "safe_to_evict": evict, "do_not_disrupt": dnd}
        ), f"forms disagree for {local}/{evict}/{dnd}"


def test_cluster_facts_are_not_called_collected_when_empty():
    """`collected` gates the "constraints were never checked" warning.

    Setting it because kubectl did not raise -- rather than because it returned
    something -- made a cluster with zero facts look fully evaluated and dropped
    that warning from every node group in it.
    """
    from rightsizer.core.models import ClusterFacts, Snapshot

    empty = ClusterFacts()
    assert not empty.collected

    for env in ("prod-us1", "prod-eu1"):
        f = Snapshot.read(_snapshot(env)).facts
        has_any = bool(f.workload_constraints or f.pdb_specs or f.node_facts)
        assert f.collected == has_any, (
            f"{env}: collected={f.collected} but "
            f"{len(f.workload_constraints)} constraints, {len(f.pdb_specs)} pdbs, "
            f"{len(f.node_facts)} nodes"
        )


# -- the five classes that stop CA removing a node -------------------------


def _rules(**kw):
    from rightsizer.engine.eviction import EvictionRules

    return EvictionRules(**kw)


def _pod(ns, name, node, wl="ns/wl/Deployment", kind="ReplicaSet", cpu=1.0, mem=1.0):
    from rightsizer.engine.autoscaler import PlacedPod

    return PlacedPod(namespace=ns, name=name, node=node, cpu=cpu,
                     memory=mem * 1024**3, owner_kind=kind, workload_key=wl)


def test_pdb_blocks_a_node_only_when_the_budget_allows_no_disruption():
    """Cluster Autoscaler's ``pdb`` rule: ``disruptionsAllowed < 1`` blocks.

    Two pods of a workload on one node against a budget of one is *not* a
    blocker upstream -- the node is drained last (risky) and the eviction API
    lets the pods go one at a time. Only a budget with nothing left blocks.
    """
    from rightsizer.engine.eviction import Pdb

    pdb = Pdb("ns", "wl-pdb", {"app": "wl"}, max_unavailable="1")
    rules = _rules(pdb_for={"ns/wl/Deployment": pdb}, replicas={"ns/wl/Deployment": 6})

    one = [_pod("ns", "a", "n1")]
    two = [_pod("ns", "a", "n1"), _pod("ns", "b", "n1")]
    assert rules.pdb_veto(one) is None
    assert rules.pdb_veto(two) is None, "more pods than the budget is risky, not blocked"

    # minAvailable is the other spelling of the same budget.
    tight = Pdb("ns", "p", {"app": "wl"}, min_available="6")
    rules.pdb_for = {"ns/wl/Deployment": tight}
    veto = rules.pdb_veto(one)
    assert veto and "allows no disruption" in veto, "minAvailable == replicas permits none"

    zero = Pdb("ns", "p", {"app": "wl"}, max_unavailable="0")
    rules.pdb_for = {"ns/wl/Deployment": zero}
    assert rules.pdb_veto(one), "maxUnavailable 0 permits none"

    # A percentage resolves against the running count.
    pct = Pdb("ns", "p", {"app": "wl"}, max_unavailable="50%")
    rules.pdb_for = {"ns/wl/Deployment": pct}
    assert rules.pdb_veto(two) is None, "3 of 6 allowed"


def test_kube_system_pod_is_evictable_only_with_a_pdb():
    """CA's exemption is the PDB, not the namespace."""
    from rightsizer.engine.eviction import Pdb

    pod = _pod("kube-system", "coredns-x", "n1", wl="kube-system/coredns/Deployment")
    veto = _rules().system_veto(pod, None)
    assert veto, "kube-system with no PDB must block"
    assert veto.startswith("kube-system/coredns/Deployment"), (
        f"the reason must lead with the workload so callers can group on it: {veto}"
    )

    covered = _rules(pdb_for={"kube-system/coredns/Deployment": Pdb(
        "kube-system", "coredns", {"k8s-app": "kube-dns"}, max_unavailable="1")})
    assert covered.system_veto(pod, None) is None, "a PDB exempts a kube-system pod"

    # A DaemonSet pod is deleted with the node, never evacuated.
    ds = _pod("kube-system", "kube-proxy-x", "n1", kind="DaemonSet")
    assert _rules().system_veto(ds, None) is None


def test_node_selector_taints_and_affinity_restrict_where_a_pod_may_go():
    """The fifth class: a destination the scheduler would refuse."""
    wl = "ns/wl/Deployment"
    rules = _rules(
        node_selector={wl: {"pool": "gpu"}},
        node_labels={"gpu-1": {"pool": "gpu"}, "cpu-1": {"pool": "cpu"}},
    )
    pod = _pod("ns", "a", "gpu-1", wl=wl)
    assert rules.can_host(pod, "gpu-1", set())
    assert not rules.can_host(pod, "cpu-1", set()), "nodeSelector was ignored"
    assert not rules.can_host(pod, "unknown-node", set()), (
        "a node with no known labels must not be assumed to fit"
    )

    tainted = _rules(
        node_labels={"t-1": {}},
        node_taints={"t-1": [{"key": "dedicated", "value": "ml", "effect": "NoSchedule"}]},
    )
    plain = _pod("ns", "a", "x", wl=wl)
    assert not tainted.can_host(plain, "t-1", set()), "an untolerated taint was ignored"

    tainted.tolerations = {wl: [{"key": "dedicated", "operator": "Equal",
                                 "value": "ml", "effect": "NoSchedule"}]}
    assert tainted.can_host(plain, "t-1", set())

    # PreferNoSchedule does not stop scheduling.
    soft = _rules(node_labels={"s-1": {}},
                  node_taints={"s-1": [{"key": "x", "value": "", "effect": "PreferNoSchedule"}]})
    assert soft.can_host(plain, "s-1", set())

    # Required node affinity, In and Exists.
    aff = _rules(node_affinity={wl: [{"zone": ["a", "b"]}]},
                 node_labels={"z-a": {"zone": "a"}, "z-c": {"zone": "c"}})
    assert aff.can_host(plain, "z-a", set())
    assert not aff.can_host(plain, "z-c", set())


def test_scale_down_consults_the_rules_for_every_pod_on_the_node():
    """End to end: a PDB breach on one node keeps that node."""
    from rightsizer.engine.autoscaler import CAConfig, simulate_ca_scale_down
    from rightsizer.engine.eviction import Pdb

    cfg = CAConfig({"scale-down-utilization-threshold": "0.5"})
    pods = [
        _pod("ns", "a1", "n1", cpu=0.5, mem=0.5),
        _pod("ns", "a2", "n1", cpu=0.5, mem=0.5),
        _pod("ns", "b1", "n2", cpu=0.5, mem=0.5),
    ]
    common = dict(nodes=["n1", "n2"], pods=pods, alloc_cpu=8.0,
                  alloc_mem=32 * 1024**3, config=cfg)

    free = simulate_ca_scale_down(**common)
    assert free.total, "baseline removes nothing"

    # Two pods of a one-disruption budget on n1: drained anyway, as risky.
    risky = simulate_ca_scale_down(**common, rules=_rules(
        pdb_for={"ns/wl/Deployment": Pdb("ns", "p", {"a": "b"}, max_unavailable="1")},
        replicas={"ns/wl/Deployment": 3},
    ))
    assert risky.total == 1 and risky.risky == risky.removed

    # A budget with nothing left keeps every node holding one of its pods.
    guarded = simulate_ca_scale_down(**common, rules=_rules(
        pdb_for={"ns/wl/Deployment": Pdb("ns", "p", {"a": "b"}, max_unavailable="0")},
        replicas={"ns/wl/Deployment": 3},
    ))
    assert guarded.total == 0
    assert "allows no disruption" in guarded.vetoed["n1"]


def test_cluster_total_equals_what_the_recommendations_say():
    """The headline must be reachable by doing what the tool says.

    Summing the best option per group while the Recommendations tab showed the
    advised option made the two disagree by $137/mo on one cluster -- and the
    bigger number was the one the advice explicitly rejects as not worth the
    steps.
    """
    import json
    import pathlib

    payload_fleet = _exported_fleet()
    fleet = payload_fleet
    for period in fleet["periods"]:
        for cluster in period["clusters"]:
            groups = cluster.get("nodegroups") or []
            advised = sum(
                abs((g.get("advice") or {}).get("monthlyDelta") or 0.0) for g in groups
            )
            shown = sum(g.get("saving") or 0.0 for g in groups)
            assert shown == pytest.approx(advised, abs=1.0), (
                f"{cluster['env']}: group savings sum to {shown:.2f} but the advice "
                f"on those groups sums to {advised:.2f}"
            )
            assert -cluster["net"] == pytest.approx(shown, abs=1.0), (
                f"{cluster['env']}: headline net {cluster['net']:.2f} against "
                f"{shown:.2f} of group savings"
            )
            # The best-case figure must still be carried, and never be smaller.
            for g in groups:
                best = g.get("savingIfEverything")
                if best is not None:
                    assert best >= (g.get("saving") or 0) - 1.0, (
                        f"{g['name']}: best option {best} below the advised "
                        f"{g.get('saving')}"
                    )


def test_if_the_floor_were_lowered_respects_the_veto_classes():
    """That column offered $4,726/mo across prod-us1 for lowering minSize.

    It was `unit x nodes below the utilisation threshold` -- a count that never
    asked whether CA could drain them. Every candidate node on db-1 and seven of
    nine on resources-1 host a pod CA will not evict, so lowering the floor
    frees nothing there. The figure must come from the same replay with the
    floor removed, not from a node count.
    """
    import json
    import pathlib

    payload_fleet = _exported_fleet()
    fleet = payload_fleet
    seen = 0
    for period in fleet["periods"]:
        for cluster in period["clusters"]:
            for g in cluster.get("nodegroups") or []:
                a = g.get("autoscaler")
                if not a or not a.get("simulated"):
                    continue
                seen += 1
                # Lowering a floor can never free more than removing every
                # constraint, and never less than what CA already removes.
                assert a["monthlyIfRemoved"] <= a["monthlyIfFloorLowered"] + 0.01
                assert a["monthlyIfFloorLowered"] <= a["monthlyIfNothingBlocked"] + 0.01
                # A group whose every candidate is vetoed cannot gain from the
                # floor being lowered.
                if a.get("vetoes") and len(a["vetoes"]) >= g["nodes"]:
                    assert a["monthlyIfFloorLowered"] == pytest.approx(0.0, abs=0.01), (
                        f"{g['name']}: all {g['nodes']} nodes vetoed yet the floor "
                        f"column offers ${a['monthlyIfFloorLowered']}"
                    )
    assert seen, "no simulated autoscaler estimate in the payload"


def test_lowering_a_floor_is_not_offered_as_a_saving_ca_cannot_deliver():
    """resources-1 offered "lower minSize 9 -> 5" as a one-step $1,220/mo saving.

    Seven of its nine nodes host a pod Cluster Autoscaler will not evict, so
    lowering the floor only grants permission CA then declines to use. A
    scenario that leans on CA must say what else has to happen; one that
    replaces the node group moves the pods itself and does not.
    """
    from rightsizer.output import console as E
    from rightsizer.engine.autoscaler import CAConfig
    from rightsizer.core.config import Tunables
    from rightsizer.engine.eviction import rules_from_snapshot, simulate_group
    from rightsizer.core.models import Snapshot
    from rightsizer.analysis.recommend import container_recommendations

    snap = Snapshot.read(_snapshot("prod-us1"))
    if not snap.ca:
        pytest.skip("no autoscaler config in this snapshot")
    ca = CAConfig(**snap.ca)
    rules = rules_from_snapshot(snap)
    tun = Tunables(**snap.tunables) if snap.tunables else Tunables()
    recs = container_recommendations(snap, tun)

    checked = 0
    for g in snap.nodegroups:
        cfg = snap.facts.config_for(g.name) or {}
        itype = (cfg.get("instance_types") or [""])[0]
        if not itype or g.nodes_current == 0:
            continue
        run, _ = simulate_group(snap, g, ca, rules, 0)
        if run is None or not run.vetoed:
            continue
        scenarios = E._scenarios(snap, g, itype, recs, lambda _t: 100.0,
                                 None, "us-east-1", cfg)
        E._annotate_drain(snap, g, scenarios, ca, rules, cfg)
        for sc in scenarios:
            requires = sc.get("requires") or []
            leans_on_ca = (
                sc.get("key") != "asis"
                and (sc.get("nodes") or 0) < g.nodes_current
                and any("minSize" in r for r in requires)
                and not any("replace the node group" in r for r in requires)
            )
            if not leans_on_ca:
                continue
            checked += 1
            # Below what CA reaches only when priced "once unblocked", and
            # then it says what is blocking it and what CA stops at without.
            if sc.get("blocked"):
                assert sc.get("drainBlocked") and sc.get("drainBlockers"), sc["key"]
                assert any("Cluster Autoscaler will not evict" in r for r in requires)
                assert sc["blocked"]["nodesIfNot"] > sc["nodes"]
    # Since options are priced by replaying CA rather than repacking, a
    # floor-only option can no longer claim the nodes CA declines: it stops
    # where the replay stops. That is the stronger property, so assert it.
    for g in snap.nodegroups:
        cfg = snap.facts.config_for(g.name) or {}
        itype = (cfg.get("instance_types") or [""])[0]
        if not itype or g.nodes_current == 0:
            continue
        run, _ = simulate_group(snap, g, ca, rules, 0)
        if run is None or not run.vetoed:
            continue
        reachable = g.nodes_current + run.added - run.total
        for sc in E._scenarios(snap, g, itype, recs, lambda _t: 100.0, None, "us-east-1", cfg):
            requires = sc.get("requires") or []
            if (sc.get("key") != "asis" and not sc.get("blocked")
                    and any("minSize" in r for r in requires)
                    and not any("replace the node group" in r for r in requires)
                    and not any("request changes" in r or "minReplicas" in r for r in requires)):
                checked += 1
                assert sc["nodes"] >= reachable, (
                    f"{g.name}/{sc['key']}: claims {sc['nodes']} nodes by lowering the floor; "
                    f"Cluster Autoscaler reaches {reachable}")


def test_pdb_suggestion_refuses_to_pretend_one_replica_is_safe():
    """A PDB is what makes CA willing to touch a kube-system pod.

    With two or more replicas `maxUnavailable: 1` both unblocks the eviction and
    keeps the workload serving. With one it cannot do both: `minAvailable: 1`
    permits nothing and leaves CA exactly as blocked, `maxUnavailable: 1`
    permits it at the cost of a gap. Suggesting one without saying so would read
    as a safe fix.
    """
    from rightsizer.output import console as E

    labels = {"app.kubernetes.io/name": "svc", "app.kubernetes.io/instance": "svc-1",
              "helm.sh/chart": "svc-1.2.3", "pod-template-hash": "abc123"}

    many = E._pdb_suggestion("kube-system/svc/Deployment", 3, labels)
    assert many["safe"] and many["yaml"]
    assert "maxUnavailable: 1" in many["yaml"]
    assert "keeps 2 serving" in many["note"]
    # A selector built from a chart version stops matching on the next upgrade.
    assert "helm.sh/chart" not in many["yaml"]
    assert "pod-template-hash" not in many["yaml"]
    assert "app.kubernetes.io/name: svc" in many["yaml"]

    one = E._pdb_suggestion("kube-system/svc/Deployment", 1, labels)
    assert not one["safe"], "a single-replica PDB was presented as a safe fix"
    assert "1 replica" in one["note"] and "Scale to 2" in one["note"]

    batch = E._pdb_suggestion("kube-system/nightly/CronJob", 0, labels)
    assert not batch["safe"] and batch["yaml"] is None, (
        "a PDB was suggested for a batch pod that is going to exit anyway"
    )
    assert "CronJob" in batch["note"]

    unknown = E._pdb_suggestion("kube-system/svc/Deployment", 3, {})
    assert unknown["yaml"] is None and "selector cannot be suggested" in unknown["note"]


def test_every_no_pdb_blocker_carries_a_suggestion():
    """The fix says 'add a PodDisruptionBudget'; it has to say which one."""
    import json
    import pathlib

    payload_fleet = _exported_fleet()
    fleet = payload_fleet
    seen = 0
    for period in fleet["periods"]:
        for cluster in period["clusters"]:
            for g in cluster.get("nodegroups") or []:
                for sc in g.get("scenarios") or []:
                    for b in sc.get("drainBlockers") or []:
                        if "no PDB" not in b["reason"]:
                            continue
                        seen += 1
                        assert b.get("pdb"), f"{b['subject']}: no PDB suggested"
                        assert b["pdb"]["note"], f"{b['subject']}: no explanation"
    assert seen, "no 'no PDB' blocker in the payload; the check was vacuous"


def test_levers_are_searched_in_combination_not_only_alone():
    """Requests and HPA floors are not alternatives.

    A team cutting an over-set floor fixes the requests in the same PR, and the
    two interact: fewer pods each holding a larger memory reservation is a
    different shape from either change alone. Searching them only separately
    made the combination unreachable.
    """
    from rightsizer.core.config import Tunables
    from rightsizer.core.models import Snapshot
    from rightsizer.analysis.recommend import container_recommendations
    from rightsizer.engine.simulate import pack, pods_for_nodegroup, replica_plan

    snap = Snapshot.read(_snapshot("prod-us1"))
    tun = Tunables(**snap.tunables) if snap.tunables else Tunables()
    recs = container_recommendations(snap, tun)
    if not replica_plan(snap):
        pytest.skip("no HPA floor to reduce in this snapshot")

    g = next(x for x in snap.nodegroups if x.name == "services-1")
    kw = dict(recommendations=recs, group_state=g)
    shapes = {}
    for mode in ("current", "all", "replicas", "all+replicas"):
        pods = pods_for_nodegroup(snap, g.name, apply=mode, **kw)
        shapes[mode] = (
            len(pods), round(sum(p.cpu for p in pods), 1),
            round(sum(p.memory for p in pods) / 1024**3),
        )

    # The combination is a genuinely different pod set from either lever.
    assert shapes["all+replicas"] not in (shapes["replicas"], shapes["all"])
    assert shapes["all+replicas"][0] < shapes["current"][0], (
        "combining did not reduce any replica floor"
    )
    assert shapes["all+replicas"][2] > shapes["replicas"][2], (
        "combining did not apply the memory increases"
    )

    # And the floor is recomputed against the requests the same option sets.
    # Utilisation is usage/request, so cutting the request raises it and the HPA
    # answers by scaling out: computing the floor against today's request gives
    # one the HPA immediately overruns. Fewer workloads qualify once that is
    # accounted for, never more.
    at_current = replica_plan(snap)
    after_cuts = replica_plan(snap, recs)
    assert set(after_cuts) <= set(at_current)
    assert len(after_cuts) < len(at_current), (
        "no workload dropped out once the floor was computed against the cut "
        "requests; the two levers were not reconciled"
    )
    assert shapes["all+replicas"][0] > shapes["replicas"][0], (
        "the combined option kept floor reductions that right-sizing invalidates"
    )




def test_a_recommendation_that_changes_no_requests_offers_the_one_that_does():
    """The cheapest row can leave every OOM finding standing.

    services-1 is recommended "reduce HPA minReplicas" -- one step, cheapest,
    and it applies none of the memory increases its findings call for. The
    option that does both loses on cost and on steps, so a ranking on those two
    axes would never surface it.

    The offer is suppressed when the only request-applying option bundles a
    migration: on prod-eu1/storage-1 that priced one container's safety at
    +$6,033/mo, which is a different decision, not a footnote on this one.
    """
    import json
    import pathlib

    payload_fleet = _exported_fleet()
    fleet = payload_fleet

    offered = {}
    for period in fleet["periods"]:
        for cluster in period["clusters"]:
            for g in cluster.get("nodegroups") or []:
                a = g.get("advice") or {}
                for alt in a.get("alternatives") or []:
                    if "at risk" not in alt.get("why", ""):
                        continue
                    offered[f"{cluster['env']}/{g['name']}"] = alt
                    # Never priced as a footnote when it is really a migration.
                    if g.get("monthlyCost"):
                        assert alt["monthlyDelta"] <= g["monthlyCost"] * 0.25, (
                            f"{g['name']}: offers a ${alt['monthlyDelta']:.0f}/mo "
                            f"'fix' against ${g['monthlyCost']:.0f}/mo of spend"
                        )

    assert offered, "no group offered the option that fixes its at-risk containers"

    # The case this exists for: a group whose recommendation is the replica
    # lever alone must be shown the option that also fixes the requests.
    key = "prod-us1/services-1"
    if key in offered:
        alt = offered[key]
        assert "request" in alt["label"].lower(), alt["label"]
        assert "hpa" in alt["label"].lower() or "replica" in alt["label"].lower(), (
            f"the offered option drops the replica lever the recommendation relies on: "
            f"{alt['label']}"
        )


def test_hpa_floor_is_replayed_over_the_window_not_a_single_equilibrium():
    """A floor derived from one scalar cannot say what it would have covered.

    `desired(t) = ceil(total(t) / (request x target))` is the HPA's own formula
    with the replica count cancelled. Evaluated across the stored quantiles it
    yields a distribution -- what the HPA wanted at the median, at p99, at the
    busiest sample -- and the floor is set from that, with the peak carried so a
    reader can see what the HPA must still be able to reach.
    """
    from rightsizer.engine.simulate import (
        FLOOR_COVERAGE, MIN_REPLICA_FLOOR, _desired_at, replica_floors,
    )
    from rightsizer.core.models import HPASpec, Snapshot, UsageStat

    # ceil(total / (request * target)); the replica count cancels out.
    assert _desired_at(10.0, 0.5, 0.8) == 25
    assert _desired_at(0.0, 0.5, 0.8) == 1
    # Inside the 10% tolerance the controller does not act.
    assert _desired_at(0.42, 0.5, 0.8) == 1
    assert _desired_at(1.0, 0.0, 0.8) == 0, "a zero request must not divide"

    snap = Snapshot.read(_snapshot("prod-us1"))
    floors = replica_floors(snap)
    if not floors:
        pytest.skip("no reducible HPA floor in this snapshot")

    replayed = [f for f in floors if f.desired]
    if not replayed:
        pytest.skip("snapshot predates the total-series ladder")

    for f in replayed:
        assert f.suggested >= MIN_REPLICA_FLOOR, f"{f.key}: floor below two replicas"
        assert f.suggested < f.current_min
        # The suggestion is the formula's own answer, not a fraction of the
        # current value. A cap would make it depend on how wrong the current
        # setting happens to be -- a floor of 400 would "recommend" 200.
        assert f.suggested == max(f.floor_at_coverage, MIN_REPLICA_FLOOR), (
            f"{f.key}: suggested {f.suggested} is not the replay's {f.floor_at_coverage}"
        )
        # A long climb is reported, never silently absorbed into the number.
        if f.peak_desired > f.suggested * 3:
            assert f.long_scale_up, (
                f"{f.key}: floor {f.suggested} against a peak of {f.peak_desired} "
                "with no staging note"
            )
        # Demand is monotonic across the ladder, so the floor must be too.
        assert f.desired["p50"] <= f.desired[FLOOR_COVERAGE] <= f.peak_desired
        # A floor above what the HPA can reach is not a saving, it is an outage.
        if f.max_replicas:
            assert f.peak_desired <= f.max_replicas
        assert str(f.desired.get("n", "")) not in f.basis or f.basis


def test_a_floor_is_not_proposed_when_the_hpa_could_not_reach_the_peak():
    """Lowering a floor on a workload already hitting maxReplicas deepens the
    shortfall rather than saving anything."""
    from rightsizer.engine.simulate import replica_floors
    from rightsizer.core.models import HPASpec, Snapshot, UsageStat

    snap = Snapshot.read(_snapshot("prod-us1"))
    stat = UsageStat(resource="cpu", namespace="ns", workload="wl", kind="Deployment",
                     container="c", replicas=10, request=0.5, p95=0.4,
                     total_ladder={"p50": 4.0, "p75": 5.0, "p90": 6.0, "p95": 7.0,
                                   "p99": 8.0, "max": 40.0, "n": 100})
    snap.usage = [stat]
    snap.hpas = [HPASpec(namespace="ns", name="wl-hpa", target_name="wl",
                         min_replicas=30, max_replicas=40, cpu_target=0.8)]
    # Peak demand is ceil(40 / (0.5*0.8)) = 100, well past maxReplicas 40.
    assert not replica_floors(snap), (
        "proposed a lower floor for a workload whose peak already exceeds its max"
    )


def test_vetoes_are_reported_even_when_the_floor_binds_first():
    """A group held at minSize recorded zero blockers while seven were real.

    The scale-down loop exits at `len(remaining) <= min_size` before evaluating
    a single candidate, so vetoes collected from the floor-aware replay are
    empty for exactly the groups where the blockers matter most. They must come
    from the replay with the floor removed.
    """
    import json
    import pathlib

    payload_fleet = _exported_fleet()
    fleet = payload_fleet

    from rightsizer.engine.autoscaler import CAConfig
    from rightsizer.engine.eviction import rules_from_snapshot, simulate_group
    from rightsizer.core.models import Snapshot

    snap = Snapshot.read(_snapshot("prod-us1"))
    if not snap.ca:
        pytest.skip("no autoscaler config")
    ca = CAConfig(**snap.ca)
    rules = rules_from_snapshot(snap)

    cluster = next(c for p in fleet["periods"] for c in p["clusters"]
                   if c["env"] == "prod-us1")
    shipped = {g["name"]: g.get("autoscaler") for g in cluster["nodegroups"]}

    checked = 0
    for g in snap.nodegroups:
        a = shipped.get(g.name)
        if not a or not a.get("simulated"):
            continue
        run, _ = simulate_group(snap, g, ca, rules, 0)
        if run is None or not run.vetoed:
            continue
        checked += 1
        assert len(a.get("vetoes") or []) == len(run.vetoed), (
            f"{g.name}: ships {len(a.get('vetoes') or [])} blocker(s) but the "
            f"unfloored replay finds {len(run.vetoed)}"
        )
    assert checked, "no group had a veto to report; the check was vacuous"


def test_running_now_is_not_the_thirty_day_peak():
    """The column was headed "Running" and held a 30-day maximum.

    `stat.replicas` counts both sides of a rolling update, so
    api-server read 104 against 75 actually running -- above its floor
    and above anything the HPA ever asked for, which makes the row look like the
    HPA is scaling when it is sitting on its minimum.
    """
    import collections
    import json
    import pathlib

    payload_fleet = _exported_fleet()
    from rightsizer.core.models import Snapshot

    snap = Snapshot.read(_snapshot("prod-us1"))
    fleet = payload_fleet
    checked = 0
    for period in fleet["periods"]:
        for cluster in period["clusters"]:
            if cluster["env"] != "prod-us1":
                continue
            for g in cluster["nodegroups"]:
                rows = g.get("replicaChanges") or []
                if not rows:
                    continue
                placed = collections.Counter(
                    r.get("workload_key")
                    for r in (snap.placements or {}).get(g["name"], [])
                    if r.get("owner_kind") != "DaemonSet"
                )
                for r in rows:
                    key = f"{r['ns']}/{r['workload']}/Deployment"
                    if key not in placed:
                        continue
                    checked += 1
                    assert r.get("runningNow") == placed[key], (
                        f"{r['workload']}: row says {r.get('runningNow')} running, "
                        f"placement has {placed[key]}"
                    )
                    # The floor cannot claim to free capacity that is not there.
                    assert r["suggestedMin"] <= r["minReplicas"]
                    if r.get("peakDesired") is not None:
                        # The floor never drops below two replicas whatever the
                        # replay says, so a workload the HPA only ever wanted
                        # one of still gets two.
                        from rightsizer.engine.simulate import MIN_REPLICA_FLOOR

                        assert r["replayFloor"] <= max(
                            r["peakDesired"], MIN_REPLICA_FLOOR
                        ), (
                            f"{r['workload']}: replay floor {r['replayFloor']} above "
                            f"peak demand {r['peakDesired']}"
                        )
    assert checked, "no replica row cross-checked against placement"


def test_sim_payload_lets_the_console_reproduce_the_engine():
    """The console packs pods itself, which is the money math existing twice.

    Every group ships reference cases packed by this engine. If the exported
    payload cannot reproduce them here, the fixtures are wrong and the console's
    check against them proves nothing.
    """
    import json
    import pathlib

    payload_fleet = _exported_fleet()
    from rightsizer.engine.simulate import Pod, pack

    fleet = payload_fleet
    cases = 0
    for period in fleet["periods"]:
        for cluster in period["clusters"]:
            for g in cluster.get("nodegroups") or []:
                sim = g.get("sim")
                if not sim:
                    continue
                floors = {
                    w["key"]: w["hpa"]["suggested"]
                    for w in sim["workloads"] if w.get("hpa")
                }
                for f in sim["packFixtures"]:
                    cases += 1
                    kept: dict[str, int] = {}
                    pods = []
                    for i, p in enumerate(sim["pods"]):
                        w = sim["workloads"][p["w"]]
                        if f.get("applyFloors") and w["key"] in floors:
                            seen = kept.get(w["key"], 0)
                            if seen >= floors[w["key"]]:
                                continue
                            kept[w["key"]] = seen + 1
                        pods.append(Pod(
                            name=str(i), workload=w["key"],
                            cpu=max(p["c"] * f["scaleCpu"], w["peakCpu"]),
                            memory=max(p["m"] * f["scaleMem"], w["peakMem"]),
                            antiaffinity=w["key"] if w["antiaffinity"] else "",
                        ))
                    got = pack(pods, cpu_per_node=sim["nodeCpu"],
                               mem_per_node=sim["nodeMem"],
                               max_pods_per_node=sim["maxPods"],
                               headroom=sim["headroom"]).nodes_required
                    assert got == f["nodes"], (
                        f"{cluster['env']}/{g['name']} cpu x{f['scaleCpu']} "
                        f"mem x{f['scaleMem']}"
                        f"{' +floors' if f.get('applyFloors') else ''}: "
                        f"fixture says {f['nodes']}, engine packs {got}"
                    )
    assert cases >= 12, f"only {cases} fixture cases in the payload"


def test_hpa_rows_carry_the_utilisation_that_explains_them():
    """A floor recommendation without the utilisation behind it is an assertion.

    The HPA steers on utilisation; the reason a floor is doing the work instead
    is that utilisation sits far below the target. media-server targets
    80% and its replicas averaged 0.1% -- that number is the argument, and it
    was not on the row.
    """
    import json
    import pathlib

    payload_fleet = _exported_fleet()
    fleet = payload_fleet
    checked = 0
    for period in fleet["periods"]:
        for cluster in period["clusters"]:
            for g in cluster.get("nodegroups") or []:
                for r in g.get("replicaChanges") or []:
                    checked += 1
                    for field in ("utilAvgPct", "utilMaxPct", "targetPct",
                                  "neededAvg", "peakDesired"):
                        assert r.get(field) is not None, f"{r['workload']}: no {field}"
                    assert r["utilAvgPct"] <= r["utilMaxPct"] + 1e-6, (
                        f"{r['workload']}: average utilisation above the maximum"
                    )
                    assert r["neededAvg"] <= r["peakDesired"], (
                        f"{r['workload']}: needs more replicas on average "
                        f"({r['neededAvg']}) than at peak ({r['peakDesired']})"
                    )
                    # A workload only qualifies for a floor cut because the HPA
                    # is not the thing deciding its replica count.
                    assert r["utilAvgPct"] < r["targetPct"], (
                        f"{r['workload']}: average utilisation {r['utilAvgPct']}% is at "
                        f"or above its {r['targetPct']}% target, so the HPA is governing "
                        "and the floor is not the lever"
                    )
    assert checked, "no HPA rows in the payload"


def test_a_combined_option_lowers_fewer_floors_than_the_replica_only_one():
    """Both levers pull on the same quantity, so they cannot both be taken fully.

    Utilisation is usage/request. Cutting the request raises it, and the HPA
    answers by wanting more replicas -- so an option that also cuts requests can
    lower each floor less, and some workloads not at all. Two options on the
    same group showing different floors is the trade-off, not a bug.
    """
    import json
    import pathlib

    payload_fleet = _exported_fleet()
    fleet = payload_fleet
    compared = 0
    for period in fleet["periods"]:
        for cluster in period["clusters"]:
            for g in cluster.get("nodegroups") or []:
                scenarios = g.get("scenarios") or []
                def floors(sc):
                    req = sc.get("requires") or []
                    if not any("minReplicas" in r for r in req):
                        return None, False
                    rows = sc.get("replicaChanges") or []
                    cuts = any("container request changes" in r for r in req)
                    return {r["workload"]: r["suggestedMin"] for r in rows}, cuts

                plain = next((floors(s)[0] for s in scenarios
                              if floors(s)[0] is not None and not floors(s)[1]), None)
                combined = next((floors(s)[0] for s in scenarios
                                 if floors(s)[0] is not None and floors(s)[1]), None)
                if not plain or not combined:
                    continue
                compared += 1
                assert set(combined) <= set(plain), (
                    f"{g['name']}: the combined option lowers a floor the "
                    "requests-unchanged option does not"
                )
                for wl, floor in combined.items():
                    assert floor >= plain[wl], (
                        f"{g['name']}/{wl}: combined option proposes {floor}, below the "
                        f"{plain[wl]} of the option that leaves requests alone -- cutting a "
                        "request cannot let the floor go lower"
                    )
    if not compared:
        pytest.skip("no group ships both a plain and a combined replica option")


def test_rate_window_is_not_tied_to_the_query_step():
    """Matching the two smooths away the bursts a request has to cover.

    `rate(...[5m])` averages a 15-second spike across five minutes. Measured on
    this fleet: p95 per replica was 193m at [5m] against 320m at [1m] for
    api-server, and 53m against 178m for search-server -- so the tool
    recommended 220m and 70m requests for containers whose real p95 exceeds
    those. Sizing a request below its own p95 guarantees throttling.
    """
    from rightsizer.core.config import Tunables
    from rightsizer.sources import queries as q

    tun = Tunables()
    assert tun.rate_window_seconds < tun.step_seconds, (
        "the rate window must be finer than the sampling step, or the samples "
        "are smoothed to the step and the peaks are gone"
    )
    # rate() needs several scrapes inside its window; this fleet scrapes at 15s.
    assert tun.rate_window_seconds >= 60, "below 60s a 15s scrape leaves too few samples"

    expr = q.cpu_per_container(Scope(env="test-cluster"), tun.rate_window_seconds)
    assert f"[{tun.rate_window_seconds}s]" in expr
    assert f"[{tun.step_seconds}s]" not in expr, (
        "the step leaked back into the rate window"
    )


def test_request_coverage_is_a_choice_with_a_safe_fallback():
    """p95 leaves 5% of samples above the request.

    That is fine while a container has no CPU limit and its node has room, and
    an exposure when either changes. The coverage is therefore a setting, and a
    snapshot collected before the ladder existed must degrade to the p95 it
    does have -- never to zero, which would recommend the floor for everything.
    """
    from dataclasses import replace

    from rightsizer.core.config import Tunables
    from rightsizer.core.models import UsageStat
    from rightsizer.core.stats import recommend_cpu

    base = dict(resource="cpu", namespace="n", workload="w", kind="Deployment",
                container="c", request=0.8)
    rich = UsageStat(**base, p95=0.198, peak=2.66,
                     ladder={"p50": 0.04, "p95": 0.198, "p99": 0.9, "max": 2.66})
    bare = UsageStat(**base, p95=0.198, peak=2.66)

    assert rich.at("p95") == pytest.approx(0.198)
    assert rich.at("p99") == pytest.approx(0.9)
    assert rich.at("max") == pytest.approx(2.66)
    # No ladder: fall back to p95, which is smaller than asked for, never larger.
    assert bare.at("p99") == pytest.approx(0.198)
    assert bare.at("max") == pytest.approx(2.66), "max always has `peak` to fall back on"

    # Raising the coverage must raise the request, never lower it. Measured
    # with the tail guard off, so this tests the coverage setting alone --
    # core-trigger's p99 is 4.5x its p95, so the guard is active on it.
    plain = Tunables(tail_lift=0.0)
    at95 = recommend_cpu(rich, replace(plain, request_coverage="p95"), hpa_target=0.8)
    at99 = recommend_cpu(rich, replace(plain, request_coverage="p99"), hpa_target=0.8)
    assert at99 > at95, f"p99 coverage produced {at99} against p95's {at95}"
    # p95 of 198m against an 80% target is the 250m core-trigger was given
    # before the tail guard existed.
    assert at95 == pytest.approx(0.25, abs=0.011)

    # And with the guard on, that same container is raised -- it is the case
    # the guard exists for.
    assert recommend_cpu(rich, Tunables(request_coverage="p95"), hpa_target=0.8) > at95

    # A snapshot without a ladder must still produce the p95 answer, unchanged.
    assert recommend_cpu(bare, replace(plain, request_coverage="p99"), hpa_target=0.8) == (
        pytest.approx(at95)
    )


def test_utilisation_uses_the_live_replica_count_not_todays_floor():
    """api-server ran at 100 replicas for three of four weeks.

    Dividing the concurrent total by today's minReplicas of 75 reported 12.3%
    against a true 10.0%. `stat.avg` is `avg by (...)` across pods at each
    instant, so it is already divided by the count that was live then -- the
    only correct denominator when a floor moves mid-window.
    """
    from rightsizer.core.models import HPASpec, Snapshot, UsageStat
    from rightsizer.engine.simulate import replica_floors

    # 8 cores across 100 replicas early, across 75 later; request 0.5.
    stat = UsageStat(
        resource="cpu", namespace="ns", workload="wl", kind="Deployment",
        container="c", replicas=100, request=0.5, p95=0.1,
        avg=0.05,          # mean across replicas, per instant
        total_avg=4.6,     # concurrent total -> implies ~92 replicas running
        peak=0.4,
        total_ladder={"p50": 4.0, "p95": 5.0, "p99": 6.0, "max": 8.0, "n": 100},
    )
    snap = Snapshot.read(_snapshot("prod-us1"))
    snap.usage = [stat]
    snap.hpas = [HPASpec(namespace="ns", name="wl-hpa", target_name="wl",
                         min_replicas=75, max_replicas=150, cpu_target=0.8)]

    plan = replica_floors(snap)[0]
    assert plan.util_avg == pytest.approx(0.05 / 0.5), "utilisation is avg/request"
    # The stale form would have been total_avg / (75 * request) = 12.3%.
    assert plan.util_avg != pytest.approx(4.6 / (75 * 0.5), abs=1e-4)
    assert plan.avg_replicas == pytest.approx(92.0, abs=0.5)
    # And the mismatch against the floor must be reported, not left to be spotted.
    assert "floor was higher earlier" in plan.mixed_window


def test_a_request_that_moved_mid_window_is_flagged():
    """Utilisation is usage/request; replaying against one value blends two."""
    from rightsizer.sources.collect import _varied

    assert not _varied([0.5, 0.5, 0.5])
    assert not _varied([0.5, 0.505])          # scrape noise, not an edit
    assert not _varied([]) and not _varied([0.5])
    assert _varied([0.5, 0.25]), "a halved request must be flagged"
    assert _varied([0.2, 0.2, 0.8])


def test_tail_lift_is_graduated_and_saturates():
    """A flat percentile describes a tight distribution well and a bursty one
    badly, and a flat *guard* over-reserves for the extremes.

    The lift grows with p99/p95 and saturates, because the instinct is
    backwards at the top end: job-controller sits at 20m for 95% of the window
    and needs 734m at p99. Reserving half of that would be 367m on every
    replica for something that happens 1% of the time. A rare, enormous burst
    is the worst case to reserve for -- unreserved node headroom absorbs it.
    """
    from rightsizer.core.config import Tunables
    from rightsizer.core.models import UsageStat
    from rightsizer.core.stats import tail_adjusted

    tun = Tunables()

    def stat(p95, p99):
        return UsageStat(resource="cpu", namespace="n", workload="w", kind="Deployment",
                         container="c", request=0.8, p95=p95, peak=9.0,
                         ladder={"p95": p95, "p99": p99})

    # Below the trigger nothing moves -- the median container here is 1.24.
    for ratio in (1.0, 1.24, 1.5):
        assert tail_adjusted(stat(0.2, 0.2 * ratio), tun) == pytest.approx(0.2)

    # Above it, strictly increasing in the ratio.
    lifts = [tail_adjusted(stat(0.2, 0.2 * r), tun) for r in (2, 3, 5, 10, 40)]
    assert all(a < b for a, b in zip(lifts, lifts[1:])), "the lift must be graduated"

    # And saturating: 40x the gap is nowhere near 40x the reservation.
    assert lifts[-1] <= 0.2 * (1 + tun.tail_lift) + 1e-9, "the lift exceeded its ceiling"
    assert lifts[-1] < 2 * lifts[0], "the curve did not saturate"

    # Never above p99 -- covering more than the tail is what `max` does.
    for p95, p99 in ((0.02, 0.734), (0.006, 0.174), (0.1, 0.11)):
        assert tail_adjusted(stat(p95, p99), tun) <= p99 + 1e-9

    # Never below the basis it starts from.
    for p95, p99 in ((0.2, 0.2), (0.2, 0.9), (0.05, 3.0)):
        assert tail_adjusted(stat(p95, p99), tun) >= p95 - 1e-9

    # Disabled leaves every case at the plain basis.
    off = Tunables(tail_lift=0.0)
    for p95, p99 in ((0.2, 0.9), (0.02, 0.734)):
        assert tail_adjusted(stat(p95, p99), off) == pytest.approx(p95)


# -- performance, not only cost --------------------------------------------


def test_cpu_increases_are_emitted_but_only_when_measured():
    """A right-sizer that only ever cuts CPU is a cost tool.

    The rule used to discard every candidate above the request, so CPU moved in
    one direction only. It now moves both -- but most raw "increases" are the
    millicore floor talking: a 10m request whose p95 is 1m gets a 50m candidate
    from `cpu_floor_millicores` alone, and acting on it buys nothing. 39
    containers clear the naive test on this snapshot; 10 clear the measured one.
    """
    from rightsizer.core.config import Tunables
    from rightsizer.core.models import Snapshot, UsageStat
    from rightsizer.analysis.recommend import (
        CPU_INCREASE_MARGIN, THROTTLE_THRESHOLD, _cpu_increase_warranted,
        container_recommendations,
    )

    tun = Tunables()

    def stat(**kw):
        base = dict(resource="cpu", namespace="ns", workload="wl", kind="Deployment",
                    container="c", replicas=3, samples=1000)
        return UsageStat(**{**base, **kw})

    # The floor case: idle, tiny request, no grounds for an increase.
    assert not _cpu_increase_warranted(
        stat(request=0.010, p95=0.001, peak=0.002, avg=0.001), tun
    )
    # Measurably over its request.
    assert _cpu_increase_warranted(
        stat(request=0.100, p95=0.400, peak=0.900, avg=0.300), tun
    )
    # Throttled: the ground usage cannot show, because usage looks healthy
    # precisely when the limit is holding it down.
    assert _cpu_increase_warranted(
        stat(request=0.100, p95=0.050, peak=0.090, avg=0.040,
             limit=0.100, throttle_pct=THROTTLE_THRESHOLD), tun
    )
    # Throttle reported without a limit is meaningless -- the kernel cannot
    # throttle a container that has none.
    assert not _cpu_increase_warranted(
        stat(request=0.100, p95=0.050, peak=0.090, avg=0.040,
             limit=0.0, throttle_pct=0.9), tun
    )
    assert CPU_INCREASE_MARGIN > 1.0

    snap = Snapshot.read(_snapshot("prod-us1"))
    tun = Tunables(**snap.tunables) if snap.tunables else Tunables()
    recs = container_recommendations(snap, tun)
    ups = [r for r in recs
           if r.cpu_recommended is not None and r.cpu_recommended > r.cpu_request]
    downs = [r for r in recs
             if r.cpu_recommended is not None and r.cpu_recommended < r.cpu_request]
    assert ups, "no CPU increase on a snapshot with under-provisioned containers"
    assert downs, "CPU cuts disappeared"

    # Every increase must be justified by measurement, never by the floor.
    by_key = {(u.namespace, u.workload, u.kind, u.container): u
              for u in snap.usage if u.resource == "cpu"}
    for r in ups:
        u = by_key[(r.namespace, r.workload, r.kind, r.container)]
        assert _cpu_increase_warranted(u, tun), (
            f"{r.workload}/{r.container}: increase to "
            f"{r.cpu_recommended * 1000:.0f}m with p95 {u.p95 * 1000:.0f}m and no throttling"
        )


def test_performance_findings_are_reported_alongside_savings():
    """"Nothing to save here" and "nothing wrong here" are different claims."""
    from rightsizer.analysis.advise import _problems
    from rightsizer.core.config import Tunables
    from rightsizer.core.models import Snapshot
    from rightsizer.analysis.recommend import container_recommendations

    snap = Snapshot.read(_snapshot("prod-us1"))
    tun = Tunables(**snap.tunables) if snap.tunables else Tunables()
    recs = container_recommendations(snap, tun)
    kinds = {p.kind for g in snap.nodegroups for p in _problems(snap, g.name, recs)}
    assert "cpu-below-use" in kinds or "cpu-throttled" in kinds, (
        f"no CPU performance finding reported; saw {sorted(kinds)}"
    )


def test_throttle_share_can_never_exceed_the_periods_it_counts():
    """It reported 242.9% of CFS periods throttled, which does not exist.

    The ratio is two independently-extrapolated rates: a container restart
    resets both counters, and `rate()` can carry the numerator across the reset
    differently from the denominator. Taking `max` over 30 days then selects
    exactly those artefacts.
    """
    from rightsizer.core.config import Scope
    from rightsizer.sources import queries as q
    from rightsizer.core.models import Snapshot

    assert "clamp_max(" in q.cpu_throttling(Scope(env="test-cluster")), (
        "the throttling query no longer clamps its ratio"
    )

    snap = Snapshot.read(_snapshot("prod-us1"))
    bad = [u for u in snap.usage if u.throttle_pct > 1.0]
    assert not bad, (
        f"{len(bad)} stat(s) report more throttled periods than periods: "
        f"{[(u.workload, u.throttle_pct) for u in bad[:3]]}"
    )
    # And the signal is only meaningful beside a limit.
    for u in snap.usage:
        if u.throttle_pct > 0:
            assert u.resource == "cpu", "throttling recorded on a memory stat"


# -- load the scheduler cannot see ------------------------------------------


def _load_stat(resource, workload, container, *, request=0.0, limit=0.0, busy=0.0, peak=None):
    """A usage stat whose mean-replica p95 -- what pressure sums -- is `busy`."""
    return UsageStat(resource=resource, namespace="ns", workload=workload, kind="Deployment",
                     container=container, replicas=2, request=request, limit=limit,
                     p95=busy, peak=peak if peak is not None else busy, samples=8000,
                     avg_ladder={"p95": busy})


def test_qos_class_follows_kubernetes() -> None:
    from rightsizer.engine.pressure import _qos

    none = {"cpu_request": 0.0, "mem_request": 0.0, "cpu_limit": 0.0, "mem_limit": 0.0}
    assert _qos({"a": dict(none)}) == "BestEffort"
    assert _qos({"a": {**none, "mem_request": 1.0}}) == "Burstable"
    full = {"cpu_request": 1.0, "mem_request": 2.0, "cpu_limit": 1.0, "mem_limit": 2.0}
    assert _qos({"a": full}) == "Guaranteed"
    # One container with nothing makes the pod Burstable, not Guaranteed.
    assert _qos({"a": full, "sidecar": dict(none)}) == "Burstable"


def test_pressure_sums_measured_use_of_the_mean_replica() -> None:
    """A request consumes nothing; the hottest replica's figure is not every replica's."""
    from rightsizer.engine.pressure import workload_loads

    hot = UsageStat(resource="mem", namespace="ns", workload="w", kind="Deployment",
                    container="c", request=4.0, p95=9.0, avg_ladder={"p95": 3.0})
    snap = Snapshot(env="e", collected_at="t", window_days=30, tunables={}, usage=[hot])
    assert workload_loads(snap)["ns/w/Deployment"].mem_busy == 3.0

    old = UsageStat(resource="mem", namespace="ns", workload="w", kind="Deployment",
                    container="c", request=4.0, p95=9.0)
    snap = Snapshot(env="e", collected_at="t", window_days=30, tunables={}, usage=[old])
    assert workload_loads(snap)["ns/w/Deployment"].mem_busy == 9.0, "fallback: hottest p95"


def test_a_big_request_is_not_load_but_an_unrequested_pod_is() -> None:
    from rightsizer.engine.autoscaler import PlacedPod
    from rightsizer.engine.pressure import WorkloadLoad, assess

    loads = {"ns/reserved/Deployment": WorkloadLoad(cpu_request=4, mem_request=8 * GIB_T,
                                                    cpu_busy=0.5, mem_busy=1 * GIB_T),
             "ns/be/Deployment": WorkloadLoad(cpu_busy=0.2, mem_busy=9.5 * GIB_T,
                                              qos="BestEffort")}
    node = [PlacedPod("ns", "reserved-1", "n", 4, 8 * GIB_T, "ReplicaSet",
                      "ns/reserved/Deployment"),
            PlacedPod("ns", "be-1", "n", 0, 0, "ReplicaSet", "ns/be/Deployment")]
    (p,) = assess({"n": node}, cpu_alloc=8, mem_alloc=10 * GIB_T, loads=loads)
    assert p.mem_reserved == 8 * GIB_T
    assert p.mem_busy == 10.5 * GIB_T, "load is use (1 + 9.5), not the 8 GiB request"
    assert p.severity == "critical"
    assert [e.pod for e in p.exposed] == ["ns/be-1"], "the reserved pod uses less than it holds"
    assert p.exposed[0].qos == "BestEffort"


GIB_T = 1024**3


def _besteffort_snapshot() -> Snapshot:
    """Two nodes of 8 cores / 10 GiB. `api` holds 6 GiB requested but uses 3.

    Node b also runs `be`, which requests nothing and uses 5 GiB. Today CA sees
    both nodes 60% full by memory requests and removes neither. Cut `api` to
    2 GiB and node a looks 20% full: CA moves its api pod onto b -- by requests
    it fits -- and b now carries 3 + 3 + 5 = 11 GiB of real use in 10.
    """
    from rightsizer.core.models import NodeGroupState

    usage = [
        _load_stat("cpu", "api", "app", request=1.0, busy=0.3),
        _load_stat("mem", "api", "app", request=6 * GIB_T, busy=3 * GIB_T),
        _load_stat("cpu", "be", "worker", busy=0.2, peak=0.4),
        _load_stat("mem", "be", "worker", busy=5 * GIB_T, peak=5.5 * GIB_T),
    ]
    row = lambda pod, node, cpu, mem, wl: {  # noqa: E731
        "namespace": "ns", "pod": pod, "node": node, "cpu": cpu, "mem": mem,
        "owner_kind": "ReplicaSet", "workload_key": f"ns/{wl}/Deployment"}
    return Snapshot(
        env="e", collected_at="t", window_days=30, tunables={}, usage=usage,
        nodegroups=[NodeGroupState("g", nodes_current=2, allocatable_cpu=16.0,
                                   allocatable_mem=20 * GIB_T, pod_count=3)],
        placements={"g": [row("api-1", "a", 1.0, 6 * GIB_T, "api"),
                          row("api-2", "b", 1.0, 6 * GIB_T, "api"),
                          row("be-1", "b", 0.0, 0.0, "be")]},
        workload_nodegroups={"ns/api/Deployment": {"g": 1.0}, "ns/be/Deployment": {"g": 1.0}},
    )


def test_a_request_cut_that_lets_ca_starve_a_besteffort_pod_is_unsafe() -> None:
    from rightsizer.analysis.plan import Plan, pressure_findings, resolve, simulate
    from rightsizer.analysis.recommend import container_recommendations

    snap = _besteffort_snapshot()
    plan = Plan.parse("ns/api/app mem=2Gi")
    resolved, missing = resolve(snap, plan, container_recommendations(snap, TUN))
    assert not missing
    (o,) = simulate(snap, plan, resolved, TUN, price_of=lambda t: 0.0)

    assert o.ca_removed_today == [], "60% by requests: CA leaves both nodes alone today"
    assert o.ca_removed_plan == ["a"], "at 20% node a is unneeded and its pod fits on b"
    assert not o.pressure_today
    (b,) = o.pressure_plan
    assert b.node == "b" and b.severity == "critical"
    assert b.mem_busy == 11 * GIB_T

    findings = pressure_findings(snap, [o], TUN)
    unsafe = [f for f in findings if f.severity == "unsafe"]
    assert [f.code for f in unsafe] == ["memory-overcommit-after-scale-down"]
    assert "ns/be-1 (BestEffort" in unsafe[0].detail, unsafe[0].detail
    assert "+1 vs today" in unsafe[0].detail

    # And it says what to do: give the BestEffort workload requests.
    advice = [f for f in findings if f.code == "unreserved-load"]
    assert advice and "ns/be/worker cpu=" in advice[0].detail and "mem=" in advice[0].detail


def test_the_same_cut_is_quiet_when_nothing_unrequested_is_there() -> None:
    """The finding is about the no-request pod, not about the cut itself."""
    from rightsizer.analysis.plan import Plan, pressure_findings, resolve, simulate
    from rightsizer.analysis.recommend import container_recommendations

    snap = _besteffort_snapshot()
    snap.placements["g"] = [r for r in snap.placements["g"] if r["pod"] != "be-1"]
    plan = Plan.parse("ns/api/app mem=2Gi")
    resolved, _ = resolve(snap, plan, container_recommendations(snap, TUN))
    (o,) = simulate(snap, plan, resolved, TUN, price_of=lambda t: 0.0)
    assert o.ca_removed_plan == ["a"]
    assert not o.pressure_plan, "3 + 3 GiB of real use fits a 10 GiB node"
    assert not [f for f in pressure_findings(snap, [o], TUN) if f.severity == "unsafe"]


def test_ca_replay_reports_where_every_pod_lands() -> None:
    from rightsizer.engine.autoscaler import simulate_ca_scale_down

    pods = _pods({"a": [("p1", 2.0, 4, "ReplicaSet", "apps/x/Deployment")],
                  "b": [("p2", 2.0, 4, "ReplicaSet", "apps/x/Deployment")]})
    run = simulate_ca_scale_down(nodes=["a", "b"], pods=pods, alloc_cpu=8.0,
                                 alloc_mem=32 * 1024**3, config=_ca())
    (kept,) = run.placement
    assert kept not in run.removed
    assert sorted(p.name for p in run.placement[kept]) == ["p1", "p2"]


def test_plan_text_explains_what_the_cluster_would_do() -> None:
    from rightsizer.analysis.plan import Plan, pressure_findings, resolve, simulate, to_text
    from rightsizer.analysis.recommend import container_recommendations

    snap = _besteffort_snapshot()
    plan = Plan.parse("ns/api/app mem=2Gi")
    resolved, missing = resolve(snap, plan, container_recommendations(snap, TUN))
    outcomes = simulate(snap, plan, resolved, TUN, price_of=lambda t: 0.0)
    text = to_text(plan, resolved, missing, outcomes, pressure_findings(snap, outcomes, TUN))
    assert "packing by requests only" in text
    assert "today it could remove 0 node(s); with this plan 1: a" in text
    assert "load it cannot see: 1 pod(s) with no request or limit" in text
    assert "[critical] b:" in text
    assert "[unsafe] g/b" in text


# -- console export: discovered, not listed ---------------------------------


def test_export_discovers_every_snapshot_and_names_what_it_skipped(tmp_path) -> None:
    from rightsizer.output import console as export_ui

    for name in ("snapshot-a.json", "snapshot-b.json", "snapshot-a-2026-08-06.json",
                 "snapshot-a.5m.json", "report-a.json"):
        (tmp_path / name).write_text("{}")
    found, skipped = export_ui.discover(tmp_path)
    assert found == {None: ["a", "b"], "2026-08-06": ["a"]}, "a new cluster needs no code change"
    assert skipped == ["snapshot-a.5m.json"], "an odd file is reported, not silently dropped"


def test_cluster_metadata_comes_from_config_then_snapshot_then_inference() -> None:
    from rightsizer.output import console as export_ui
    from rightsizer.core.models import ClusterFacts

    def snap(meta=None, region_label=None):
        facts = ClusterFacts(node_facts=[{"name": "n", "labels": (
            {"topology.kubernetes.io/region": region_label} if region_label else {})}])
        return Snapshot(env="x", collected_at="t", window_days=30, tunables={},
                        facts=facts, meta=meta or {})

    recorded = snap({"region": "eu-west-2", "profile": "p", "account": "123", "cluster": "eks-x"})
    m = export_ui.resolve_meta("x", recorded, {})
    assert (m["region"], m["profile"], m["account"], m["cluster"], m["source"]) == (
        "eu-west-2", "p", "123", "eks-x", "snapshot")
    assert not m["regionAssumed"]

    m = export_ui.resolve_meta("x", recorded, {"clusters": {"x": {"region": "ap-south-1"}}})
    assert m["region"] == "ap-south-1" and m["source"] == "clusters.json"
    assert m["profile"] == "p", "an override fills only what it names"

    m = export_ui.resolve_meta("x", snap(region_label="ca-central-1"), {})
    assert (m["region"], m["cluster"], m["source"]) == ("ca-central-1", "x", "inferred")

    m = export_ui.resolve_meta("x", snap(), {})
    assert m["region"] == export_ui.DEFAULT_REGION and m["regionAssumed"], "assumed, and said so"


def test_snapshot_records_where_it_came_from(tmp_path) -> None:
    snap = Snapshot(env="x", collected_at="t", window_days=30, tunables={},
                    meta={"region": "eu-west-2", "profile": "main"})
    loaded = Snapshot.read(snap.write(tmp_path / "s.json"))
    assert loaded.meta == {"region": "eu-west-2", "profile": "main"}


def test_console_export_says_what_the_report_says(tmp_path, monkeypatch) -> None:
    """The console is what decision makers read: it must carry the report's
    disposition and cost summary, computed by the same functions, and every
    container -- including the ones left alone, with why."""
    from rightsizer.output import console as export_ui
    import rightsizer.adapters.clouds.aws.ec2 as pricing
    import rightsizer.sources.shell as shell
    from rightsizer.output import report
    from rightsizer.analysis.recommend import container_recommendations, nodegroup_recommendations

    def offline(*_a, **_k):
        raise RuntimeError("no AWS in tests")

    monkeypatch.setattr(shell, "run_json", offline)
    monkeypatch.setattr(pricing, "run_json", offline)

    snap = _besteffort_snapshot()
    snap.usage.append(_load_stat("mem", "new", "c", request=GIB_T, busy=0.1 * GIB_T))
    snap.usage[-1].samples = 3  # too little data: held back, and must say so
    snap.write(tmp_path / "snapshot-e.json")

    out = export_ui.build("e", tmp_path, config={}, discount=0.1)
    assert out is not None

    recs = container_recommendations(snap, TUN)
    groups = {g.name: g for g in nodegroup_recommendations(snap, TUN, recs)}
    (g,) = out["nodegroups"]
    verdict, scale_in, why = report.disposition(snap.nodegroups[0], groups["g"], None)
    assert (g["verdict"], g["scaleIn"], g["why"]) == (verdict, scale_in, why)
    assert {b["code"] for b in g["blockers"]} == {b.code for b in groups["g"].blockers}

    assert set(out["summary"]) >= {"net", "projectedRunRate", "blocked", "reductionsCount"}
    assert out["metaSource"] == "inferred" and out["regionAssumed"]
    assert any("no instance prices" in w for w in out["exportWarnings"]), "an unpriced $0 is flagged"

    held = [c for c in out["containers"] if c["actionable"] is False]
    assert any(b["code"] == "insufficient-data" for c in held for b in c["blockers"]), \
        "a container left alone ships with its reason"


def test_no_request_option_for_a_group_whose_pods_have_no_change() -> None:
    """A cut on another group must not produce a "Right-size requests" row here.

    The request levers were chosen from the whole cluster's recommendations, so
    a group with nothing to right-size still got the row. Its saving was pure
    repacking and it listed no containers -- prod-eu1's batch-aux-1 showed
    18 -> 5 nodes, -$4,595, and an empty table.
    """
    from rightsizer.core.models import ClusterFacts, NodeGroupState
    from rightsizer.adapters.clouds import InstanceType
    from rightsizer.analysis.recommend import container_recommendations
    from rightsizer.engine.simulate import scenarios

    gib = 1024**3
    groups = [
        NodeGroupState(name=n, nodes_current=2, nodes_avg_30d=2.0,
                       allocatable_cpu=16.0, allocatable_mem=64 * gib,
                       workload_cpu_requests=8.0, workload_mem_requests=16 * gib)
        for n in ("ng", "other")
    ]
    snap = Snapshot(
        env="e", collected_at="t", window_days=30, tunables={},
        usage=[_stat("cpu", "steady", 2.0, 1.9, 2), _stat("mem", "steady", 5.859375 * gib, 3.9 * gib, 2),
               _stat("cpu", "idle", 4.0, 0.05, 2), _stat("mem", "idle", 4 * gib, 3.9 * gib, 2)],
        nodegroups=groups, facts=ClusterFacts(collected=True),
        workload_nodegroups={"apps/steady/Deployment": {"ng": 1.0},
                             "apps/idle/Deployment": {"other": 1.0}},
    )
    recs = container_recommendations(snap, TUN)
    assert any(r.workload == "idle" and r.actionable and r.cpu_recommended < r.cpu_request
               for r in recs), "fixture: the other group has a real cut"
    steady = [r for r in recs if r.workload == "steady"]
    assert all(r.cpu_recommended in (None, r.cpu_request) and r.mem_recommended in (None, r.mem_request)
               for r in steady), "fixture: nothing on ng changes"

    ladder = [InstanceType("m6id.2xlarge", 8, 32.0, 0.4746)]
    run = lambda g: scenarios(snap, g, ladder, recs, current_type="m6id.2xlarge",  # noqa: E731
                              price_of=lambda t: 304.88, max_pods=110)
    assert not any(r.key.startswith("rightsize") for r in run(groups[0]))
    assert any(r.key.startswith("rightsize") for r in run(groups[1])), \
        "the group that owns the cut keeps its option"


# -- node-local storage -------------------------------------------------------


def _disk(**over):
    base = {"usage_measured": True, "nodes_measured": 10, "fs_gb": 473.6, "root_gb": 161.0,
            "on_root": False, "eph_capacity_gb": 473.6, "eph_allocatable_gb": 425.2,
            "hot_peak_gb": 100.0, "hot_peak_share": 0.21, "total_peak_gb": 600.0,
            "total_avg_gb": 500.0, "avg_node_peak_gb": 60.0, "eph_requests_gb": 0.0, "eph_request_max_gb": 0.0,
            "eph_request_pods": 0}
    return {**base, **over}


def _itype(name, vcpu, mem, store=0.0, kind="ssd"):
    from rightsizer.adapters.clouds import InstanceType

    return InstanceType(name, vcpu, mem, 0.5, storage_gb=store, storage_disks=1 if store else 0,
                        storage_type=kind if store else "", storage_nvme="required" if store else "",
                        storage_known=True)


def test_losing_the_nvme_kubelet_lives_on_is_unsafe() -> None:
    """m6id -> m6i looks free on CPU and memory; the disk moves to a 161 GB root."""
    from rightsizer.sources.storage import assess

    out = assess(_disk(), _itype("m6id.2xlarge", 8, 32, 474), _itype("m6i.2xlarge", 8, 32),
                 nodes_now=10, nodes_after=10)
    assert out["kubeletOnInstanceStore"] is True
    assert out["diskBefore"] == "1 × 474 GB NVMe SSD" and out["diskAfter"] == "none (EBS only)"
    assert out["verdict"] == "unsafe"
    assert any("EBS root" in n for n in out["notes"])


def test_fewer_smaller_disks_are_checked_against_what_the_nodes_hold() -> None:
    """The same bytes on fewer, smaller disks: fine at 10 -> 8, an eviction storm at 10 -> 3."""
    from rightsizer.sources.storage import UNSAFE_SHARE, assess

    cur, small = _itype("m6id.2xlarge", 8, 32, 474), _itype("c6id.2xlarge", 8, 16, 474)
    ok = assess(_disk(), cur, small, nodes_now=10, nodes_after=8)
    assert ok["verdict"] == "ok"
    assert ok["after"]["avgShare"] > ok["before"]["avgShare"], "fewer nodes, fuller disks"

    tiny = _itype("c6id.large", 2, 4, 118)
    bad = assess(_disk(), cur, tiny, nodes_now=10, nodes_after=3)
    assert bad["verdict"] == "unsafe"
    assert bad["after"]["hotShare"] >= UNSAFE_SHARE
    assert bad["perNodeGbAfter"] < 120


def test_ephemeral_requests_that_cannot_be_scheduled_are_unsafe() -> None:
    from rightsizer.sources.storage import assess

    out = assess(_disk(avg_node_peak_gb=1, hot_peak_gb=2, eph_requests_gb=150, eph_request_max_gb=150),
                 _itype("m6id.2xlarge", 8, 32, 474), _itype("m6id.large", 2, 8, 118),
                 nodes_now=10, nodes_after=2)
    assert out["verdict"] == "unsafe"
    assert any("request" in n for n in out["notes"])


def test_unmeasured_disk_is_unknown_not_fine() -> None:
    """A failed usage query once read as 0 GB used: every option looked safe."""
    from rightsizer.sources.storage import assess

    cur = _itype("m6id.2xlarge", 8, 32, 474)
    assert assess(None, cur, cur, nodes_now=5, nodes_after=2)["verdict"] == "unknown"
    out = assess(_disk(usage_measured=False, hot_peak_gb=0, total_peak_gb=0), cur, cur,
                 nodes_now=5, nodes_after=2)
    assert out["verdict"] == "unknown" and out["after"] is None


def test_unsafe_disk_option_is_never_the_advice() -> None:
    """Cheaper on CPU and memory, but it cannot hold the disk: not recommended."""
    from rightsizer.analysis.advise import advise
    from rightsizer.core.models import ClusterFacts, NodeGroupState
    from rightsizer.engine.simulate import scenarios

    gib = 1024**3
    group = NodeGroupState(name="ng", nodes_current=6, nodes_avg_30d=6.0,
                           allocatable_cpu=48.0, allocatable_mem=192 * gib,
                           workload_cpu_requests=6.0, workload_mem_requests=24 * gib)
    snap = Snapshot(
        env="e", collected_at="t", window_days=30, tunables={},
        usage=[_stat("cpu", "w", 1.0, 0.9, 6), _stat("mem", "w", 4 * gib, 3.9 * gib, 6)],
        nodegroups=[group], facts=ClusterFacts(collected=True),
        workload_nodegroups={"apps/w/Deployment": {"ng": 1.0}},
        storage={"ng": _disk(nodes_measured=6, avg_node_peak_gb=300, hot_peak_gb=320,
                             hot_peak_share=0.68)},
    )
    ladder = [_itype("m6id.large", 2, 8, 118), _itype("m6id.2xlarge", 8, 32, 474)]
    price = {"m6id.large": 80.0, "m6id.2xlarge": 300.0}
    rows = scenarios(snap, group, ladder, [], current_type="m6id.2xlarge",
                     price_of=lambda t: price[t], max_pods=110)
    small = [r for r in rows if r.instance_type == "m6id.large"]
    assert small and all(r.storage["verdict"] == "unsafe" for r in small)
    assert all(any(x.startswith("local storage:") for x in r.requires) for r in small)
    assert rows[0].storage["before"]["hotShare"] == 0.68
    a = advise(snap, group, rows, [])
    assert a.scenario_key not in {r.key for r in small}


def test_an_autoscaled_pools_peak_total_is_not_packed_onto_todays_nodes() -> None:
    """prod-eu1's engine pool held 2.4 TB when scaled out to 30-odd nodes.

    Dividing that by the 5 nodes today's pods pack onto read as 103% full. What
    those pods hold is the per-node average at its peak, on today's 18 nodes.
    """
    from rightsizer.sources.storage import assess

    disk = _disk(nodes_measured=18, total_peak_gb=2449.5, avg_node_peak_gb=35.0,
                 hot_peak_gb=109.8, hot_peak_share=0.22)
    out = assess(disk, _itype("m6id.2xlarge", 8, 32, 474), _itype("c6id.2xlarge", 8, 16, 474),
                 nodes_now=18, nodes_after=5)
    assert out["verdict"] == "ok"
    assert out["totalUsedPeakGb"] == 18 * 35.0


# -- finding a cluster's credentials by name ---------------------------------


def _cluster_dir(root, env, *, envrc=None, context=None):
    d = root / env
    d.mkdir(parents=True)
    kc = d / f"kubeconfig_{env}.yaml"
    ctx = context or f"arn:aws:eks:eu-west-2:123456789012:cluster/{env}"
    kc.write_text(f"apiVersion: v1\ncontexts:\n- context:\n    cluster: {ctx}\n  name: {ctx}\n"
                  f"current-context: {ctx}\n")
    (d / ".envrc").write_text(envrc if envrc is not None else
                              f'export KUBECONFIG="$PWD/kubeconfig_{env}.yaml"\n'
                              "export AWS_PROFILE=main\nexport AWS_REGION=eu-west-2\n")
    return d


def test_a_cluster_is_found_by_name_with_everything_collect_needs(tmp_path, monkeypatch) -> None:
    from rightsizer.sources import local

    _cluster_dir(tmp_path / "prod", "cluster-x1")
    monkeypatch.setenv(local.ROOTS_VAR, f"{tmp_path / 'prod'}:{tmp_path / 'stage'}")
    c = local.find("cluster-x1")
    assert c is not None
    assert c.kubeconfig == tmp_path / "prod" / "cluster-x1" / "kubeconfig_cluster-x1.yaml", "$PWD expanded"
    assert (c.context, c.cluster, c.profile, c.region, c.account) == (
        "arn:aws:eks:eu-west-2:123456789012:cluster/cluster-x1", "cluster-x1", "main", "eu-west-2",
        "123456789012")
    assert local.find("nope") is None
    assert [x.env for x in local.discover()] == ["cluster-x1"]


def test_envrc_is_read_never_executed(tmp_path, monkeypatch) -> None:
    """A shell file is code. Only `export NAME=value` lines are taken."""
    from rightsizer.sources import local

    marker = tmp_path / "ran"
    _cluster_dir(tmp_path, "cluster-x2", envrc=(
        f"touch {marker}\nexport AWS_PROFILE=p  # comment\n"
        f"export AWS_REGION=$(touch {marker})\nexport KUBECONFIG='$PWD/kubeconfig_cluster-x2.yaml'\n"))
    monkeypatch.setenv(local.ROOTS_VAR, str(tmp_path))
    c = local.find("cluster-x2")
    assert not marker.exists(), "something in the .envrc was executed"
    assert c.profile == "p"
    assert c.region == "eu-west-2", "a command substitution is not a value; fall back to the ARN"


def test_govcloud_arns_carry_their_account(tmp_path, monkeypatch) -> None:
    from rightsizer.sources import local

    _cluster_dir(tmp_path, "gov-cluster", context="arn:aws-us-gov:eks:us-gov-west-1:210987654321:cluster/gov-cluster",
                 envrc="export AWS_PROFILE=gov\n")
    monkeypatch.setenv(local.ROOTS_VAR, str(tmp_path))
    c = local.find("gov-cluster")
    assert (c.account, c.region, c.cluster) == ("210987654321", "us-gov-west-1", "gov-cluster")


def test_cli_fills_from_the_directory_but_flags_win(tmp_path, monkeypatch) -> None:
    from rightsizer import cli
    from rightsizer.sources import local

    _cluster_dir(tmp_path, "cluster-x3")
    monkeypatch.setenv(local.ROOTS_VAR, str(tmp_path))
    monkeypatch.delenv("KUBECONFIG", raising=False)

    args = cli.build_parser().parse_args(["--env", "cluster-x3", "analyze"])
    cli._apply_local(args)
    assert (args.profile, args.region, args.cluster) == ("main", "eu-west-2", "cluster-x3")
    assert args.context.endswith("cluster/cluster-x3")
    assert os.environ["KUBECONFIG"].endswith("kubeconfig_cluster-x3.yaml")

    monkeypatch.delenv("KUBECONFIG", raising=False)
    args = cli.build_parser().parse_args(
        ["--env", "cluster-x3", "--region", "us-east-1", "--context", "mine", "analyze"])
    cli._apply_local(args)
    assert (args.region, args.context) == ("us-east-1", "mine")
    assert "KUBECONFIG" not in os.environ, "an explicit context keeps the caller's kubeconfig"


def test_several_clusters_each_get_their_own_kubeconfig(tmp_path, monkeypatch) -> None:
    from rightsizer import cli
    from rightsizer.sources import local

    for env in ("cluster-a", "cluster-b"):
        _cluster_dir(tmp_path, env)
    monkeypatch.setenv(local.ROOTS_VAR, str(tmp_path))
    monkeypatch.setenv("KUBECONFIG", "/original")
    seen = []

    def record(args):
        seen.append((args.env, os.environ.get("KUBECONFIG")))
        return 0

    parser = cli.build_parser()
    for p in parser._subparsers._group_actions[0].choices.values():  # noqa: SLF001
        p.set_defaults(func=record)
    monkeypatch.setattr(cli, "build_parser", lambda: parser)
    assert cli.main(["--env", "all", "--out", str(tmp_path / "out"), "analyze"]) == 0
    assert [e for e, _ in seen] == ["cluster-a", "cluster-b"]
    assert all(k.endswith(f"kubeconfig_{e}.yaml") for e, k in seen)
    assert os.environ["KUBECONFIG"] == "/original", "one cluster's kubeconfig leaked into the shell"


def test_the_using_line_reports_what_the_run_uses_not_what_was_found(tmp_path, monkeypatch,
                                                                     capsys) -> None:
    """An override printed as the discovered value it replaced reads as ignored."""
    from rightsizer import cli
    from rightsizer.sources import local

    _cluster_dir(tmp_path, "cluster-x4")   # its .envrc says AWS_PROFILE=main
    monkeypatch.setenv(local.ROOTS_VAR, str(tmp_path))
    monkeypatch.delenv("KUBECONFIG", raising=False)
    cli._apply_local(cli.build_parser().parse_args(
        ["--env", "cluster-x4", "--profile", "shared", "analyze"]))
    line = capsys.readouterr().err
    assert "profile shared," in line, line
    assert "profile main" not in line


# --------------------------------------------------------------------------
# network bandwidth
# --------------------------------------------------------------------------

def _net(hot_peak=1.0, hot_sus=0.3, avg_peak=0.5, avg_sus=0.1):
    return {"rx_hot_peak": hot_peak, "rx_hot_sustained": hot_sus,
            "rx_avg_peak": avg_peak, "rx_avg_sustained": avg_sus,
            "tx_hot_peak": 0.01, "tx_hot_sustained": 0.01,
            "tx_avg_peak": 0.01, "tx_avg_sustained": 0.01, "measured": True}


def _nic(name, base, peak):
    from rightsizer.adapters.clouds import InstanceType

    return InstanceType(name, 8, 32, 0.5, net_baseline_gbps=base, net_peak_gbps=peak)


def test_traffic_that_fits_the_baseline_is_ok() -> None:
    from rightsizer.sources.network import assess

    out = assess(_net(), _nic("m6i.2xlarge", 3.125, 12.5), _nic("m6i.2xlarge", 3.125, 12.5),
                 nodes_now=10, nodes_after=10)
    assert out["verdict"] == "ok", out
    assert out["bandwidthAfter"] == "3.125 Gbps, burst to 12.5"


def test_a_smaller_size_whose_baseline_the_busiest_hour_exceeds_is_unsafe() -> None:
    """Same "up to 12.5" headline, a quarter the baseline: credits run out."""
    from rightsizer.sources.network import assess

    out = assess(_net(hot_peak=2.0, hot_sus=1.2), _nic("m6i.2xlarge", 3.125, 12.5),
                 _nic("m6i.large", 0.781, 12.5), nodes_now=10, nodes_after=10)
    assert out["verdict"] == "unsafe", out
    assert any("throttles it to the baseline" in n for n in out["notes"])
    assert any("drops 3.125 → 0.781" in n for n in out["notes"])


def test_consolidation_spreads_the_same_traffic_over_fewer_nodes() -> None:
    """A typical node's hour at 0.5 Gbps, 20 nodes onto 4: 2.5 Gbps each."""
    from rightsizer.sources.network import assess

    out = assess(_net(hot_peak=1.0, hot_sus=0.6, avg_peak=0.8, avg_sus=0.5),
                 _nic("m6i.2xlarge", 3.125, 12.5), _nic("m6i.2xlarge", 3.125, 12.5),
                 nodes_now=20, nodes_after=4)
    assert out["after"]["hotSustainedGbps"] == 2.5
    assert out["verdict"] == "warn", out   # 80% of the baseline, peaks above it


def test_peaks_above_the_baseline_depend_on_credits() -> None:
    from rightsizer.sources.network import assess

    out = assess(_net(hot_peak=4.9, hot_sus=0.9), _nic("r6id.4xlarge", 6.25, 12.5),
                 _nic("r6id.2xlarge", 3.125, 12.5), nodes_now=2, nodes_after=1)
    assert out["verdict"] == "warn"
    assert any("depend on network credits" in n for n in out["notes"])


def test_an_undescribed_type_falls_back_to_the_published_table() -> None:
    """Caches written before bandwidth was recorded still get a verdict."""
    from rightsizer.sources.network import assess, bandwidth
    from rightsizer.adapters.clouds import InstanceType

    old = InstanceType("m6id.xlarge", 4, 16, 0.2)
    assert bandwidth(old) == (1.562, 12.5, "published")
    out = assess(_net(), old, old, nodes_now=3, nodes_after=3)
    assert out["source"] == "published" and out["verdict"] == "ok"
    unknown = assess(_net(), old, InstanceType("x9z.large", 2, 8, 0.1), nodes_now=3, nodes_after=3)
    assert unknown["verdict"] == "unknown"



def test_lifting_drain_blockers_prices_what_they_hold_back() -> None:
    """A node CA refuses to drain is freed in the "once unblocked" replay.

    Scheduling rules still apply: the lift says whether a pod may *leave*, not
    where it may go.
    """
    import dataclasses

    from rightsizer.engine.autoscaler import CAConfig, simulate_ca_scale_down

    pods = [_pod("ns", "a", "n1", wl="ns/keep/Deployment"), _pod("ns", "b", "n2")]
    rules = _rules(drain_blocked={"ns/keep/Deployment": "ns/keep/Deployment is annotated "
                                                        "safe-to-evict=false"})
    kw = dict(nodes=["n1", "n2"], alloc_cpu=8.0, alloc_mem=32 * 1024**3, config=CAConfig())
    held = simulate_ca_scale_down(pods=list(pods), rules=rules, **kw)
    freed = simulate_ca_scale_down(pods=list(pods), rules=dataclasses.replace(rules, lifted=True),
                                   **kw)
    assert "n1" in held.vetoed and held.total == 1
    assert not freed.vetoed and freed.total == 1 and len(freed.placement) == 1


def test_an_option_that_fits_without_headroom_is_flagged() -> None:
    """96% of the memory at the window's peak fits, and is not a recommendation."""
    from rightsizer.engine.realistic import margin

    tight = margin({"peakMem": 0.96, "peakCpu": 0.3, "hotMem": 0.5, "hotCpu": 0.2}, {})
    assert not tight["ok"] and "peakMem" in tight["over"]
    assert "96%" in tight["notes"][0] and "80% target" in tight["notes"][0]
    # Already that hot today: a finding about today, not caused by the option.
    assert margin({"peakMem": 0.96}, {"peakMem": 0.95})["ok"]
    # Disk and network judge their own margin.
    assert not margin({}, {}, network={"verdict": "warn", "notes": ["peaks need credits"]})["ok"]


def test_every_shipped_option_keeps_headroom() -> None:
    """The pick and every alternative leave the margin; the tight version of a
    change is replaced by the same change on enough nodes."""
    import json
    import pathlib

    payload_fleet = _exported_fleet()
    fleet = payload_fleet
    offered = 0
    for period in fleet["periods"]:
        for cluster in period["clusters"]:
            for g in cluster.get("nodegroups") or []:
                a = g.get("advice") or {}
                keys = {a.get("scenarioKey")} | {x["key"] for x in a.get("alternatives") or []}
                for s in g.get("scenarios") or []:
                    if s["key"] in keys and s.get("margin"):
                        offered += 1
                        assert s["margin"]["ok"], (
                            f"{cluster['env']}/{g['name']} {s['label']}: {s['margin']['notes']}")
    assert offered


def test_batched_trends_group_by_workload_and_container() -> None:
    """One query covers every container; pods are tied to their workload the
    way ``owner_selector`` ties them, so a band means the same either way."""
    from rightsizer.core.config import Scope
    from rightsizer.sources.trends import _batch_exprs

    exprs = _batch_exprs(Scope(env="cluster-x"), "memory")
    assert set(exprs) == {"max", "p95", "median", "min", "total", "replicas"}
    for e in exprs.values():
        assert "namespace,workload,wkind,container" in e
        assert 'owner_kind="ReplicaSet"' in e and '"(.+)-[a-z0-9]+"' in e
        assert 'owner_kind="Job"' in e and '"(.+)-[0-9]+"' in e
        assert "[5m:1m]" in e  # the snapshot's own memory series


def test_short_lived_containers_do_not_fail_the_trend_run(tmp_path, monkeypatch) -> None:
    """A CronJob pod that finishes between two scrapes has no series: that is
    data, written as an empty file, not a broken query. A detailed container
    coming back empty still is."""
    from rightsizer.sources import trends as T
    from rightsizer.core.config import Scope, Settings

    monkeypatch.setattr(T, "collect_batched", lambda s, keys: {
        keys[0].slug: {r: {"bands": {"max": [[0, 1.0]]}, "sample": []} for r in ("cpu", "memory")}})
    monkeypatch.setattr(T, "collect_trend", lambda s, k, resource: {"bands": {"max": []}, "sample": []})
    keys = [T.TrendKey("ns", "busy", "c", "Deployment"), T.TrendKey("ns", "blip", "c", "CronJob"),
            T.TrendKey("ns", "", "c", "Unknown"), T.TrendKey("ns", "touched", "c", "Deployment")]
    written, skipped, empty = T.write_trends(
        Settings(scope=Scope(env="x")), keys, tmp_path, detailed={keys[3].slug})
    assert written == 3                      # no file for a pod without a controller
    assert empty == [keys[3].slug]           # only the detailed one is a failure
    import json
    assert json.loads((tmp_path / f"{keys[1].slug}.json").read_text())["batched"] is True


def test_every_blocker_on_a_node_is_recorded() -> None:
    """CA stops at the first reason; the reader needs all of them. db-2's third
    node held a postgres StatefulSet *behind* an argocd controller, and only the
    controller was shown."""
    from rightsizer.engine.autoscaler import CAConfig, simulate_ca_scale_down

    blocked = {"ns/argo/Deployment": "ns/argo/Deployment is annotated safe-to-evict=false",
               "ns/pg/StatefulSet": "ns/pg/StatefulSet uses emptyDir/hostPath without safe-to-evict=true"}
    pods = [_pod("ns", "argo-1", "n1", wl="ns/argo/Deployment"),
            _pod("ns", "pg-0", "n1", wl="ns/pg/StatefulSet"), _pod("ns", "x", "n2")]
    run = simulate_ca_scale_down(nodes=["n1", "n2"], pods=pods, alloc_cpu=8.0,
                                 alloc_mem=32 * 1024**3, config=CAConfig(),
                                 rules=_rules(drain_blocked=blocked))
    assert {s for s, _ in run.blockers["n1"]} == set(blocked)


def test_only_the_nodes_that_go_need_unblocking() -> None:
    """Lifting one node's blockers is enough when CA can keep the other."""
    import dataclasses

    from rightsizer.engine.autoscaler import CAConfig, simulate_ca_scale_down

    blocked = {f"ns/pg{i}/StatefulSet": f"ns/pg{i}/StatefulSet is annotated safe-to-evict=false"
               for i in (1, 2)}
    pods = [_pod("ns", "pg1-0", "n1", wl="ns/pg1/StatefulSet"),
            _pod("ns", "pg2-0", "n2", wl="ns/pg2/StatefulSet")]
    kw = dict(nodes=["n1", "n2"], alloc_cpu=8.0, alloc_mem=32 * 1024**3, config=CAConfig())
    rules = _rules(drain_blocked=blocked)
    one = simulate_ca_scale_down(pods=list(pods), **kw,
                                 rules=dataclasses.replace(rules, lift=frozenset({"ns/pg1/StatefulSet"})))
    assert one.total == 1 and set(one.placement) == {"n2"}


def test_a_pod_on_an_ebs_volume_never_leaves_its_zone() -> None:
    """Three databases in three zones cannot share one node, however small."""
    from rightsizer.engine.autoscaler import PlacedPod

    rules = _rules(node_labels={"a": {"topology.kubernetes.io/zone": "z1"},
                                "b": {"topology.kubernetes.io/zone": "z2"}},
                   zone_bound={"ns/pg-0": "z1"})
    pod = PlacedPod("ns", "pg-0", "a", 0.1, 1.0, "StatefulSet", "ns/pg/StatefulSet")
    assert rules.can_host(pod, "a", set())
    assert not rules.can_host(pod, "b", set())
    from rightsizer.engine.realistic import volume_zones
    pods = [pod, PlacedPod("ns", "pg2-0", "b", 0.1, 1.0, "StatefulSet", "ns/pg2/StatefulSet")]
    assert volume_zones(pods, {"ns/pg-0": "z1", "ns/pg2-0": "z2"}) == ["z1", "z2"]


def test_a_floor_is_priced_on_what_the_hpa_runs_not_on_the_floor() -> None:
    """1 core of load on 100m pods at a 70% target wants 15 replicas every
    minute. A floor of 5 does not make it run 5: it runs 15, and the option
    must be priced on 15."""
    from rightsizer.engine.simulate import running_replicas

    busy = {15: 43_200}                          # 30 days of minutes at 15
    assert running_replicas(busy, 5) == 15
    assert running_replicas(busy, 20) == 20      # a floor above demand holds
    # Mostly at the floor with a few minutes' spike: priced on the floor.
    spiky = {2: 42_900, 3: 300}
    assert running_replicas(spiky, 2) == 2
    # Busy for a quarter of the month: more than the 10% it may exceed.
    shifts = {2: 30_000, 6: 13_200}
    assert running_replicas(shifts, 2) == 6
    # Replayed at 200m wanting 8; the same load at 100m wants twice as many.
    assert running_replicas({8: 43_200}, 5, ratio=2.0) == 16
    assert running_replicas(None, 4) == 4        # no replay: the floor as before


def test_the_autoscaler_replay_never_places_past_max_pods() -> None:
    """services-2 runs max-pods 110; the replay put 168 on one node, because
    only CPU and memory were checked. DaemonSet pods count against it too."""
    from rightsizer.engine.autoscaler import CAConfig, PlacedPod, simulate_ca_scale_down

    ds = [PlacedPod("kube-system", f"ds-{n}", n, 0.01, 1.0, "DaemonSet", "kube-system/ds/DaemonSet")
          for n in ("a", "b")]
    small = [_pod("ns", f"p{i}", "a", cpu=0.01, mem=0.01) for i in range(3)] + \
            [_pod("ns", f"q{i}", "b", cpu=0.01, mem=0.01) for i in range(3)]
    kw = dict(nodes=["a", "b"], alloc_cpu=8.0, alloc_mem=32 * 1024**3, config=CAConfig())
    free = simulate_ca_scale_down(pods=ds + small, **kw)
    assert free.total == 1                                   # plenty of room: one goes
    capped = simulate_ca_scale_down(pods=ds + small, max_pods=6, **kw)
    assert capped.total == 0                                 # 4 + 3 > 6 on either node
    assert all(len(p) <= 6 for p in capped.placement.values())


def test_no_published_option_places_past_max_pods() -> None:
    import json
    import pathlib

    payload_fleet = _exported_fleet()
    over = []
    for period in payload_fleet["periods"]:
        for cluster in period["clusters"]:
            for g in cluster.get("nodegroups") or []:
                for s in g.get("scenarios") or []:
                    if s.get("key") == "asis" or not s.get("nodeMaxPods"):
                        continue
                    worst = max((p["pods"] + p.get("dsPods", 0) for p in s.get("placement") or []), default=0)
                    if worst > s["nodeMaxPods"]:
                        over.append(f"{cluster['env']}/{g['name']} {s['key']}: {worst} > {s['nodeMaxPods']}")
    assert not over, over


# --------------------------------------------------------------------------
# memory HPAs and maxReplicas
# --------------------------------------------------------------------------

def _behavior():
    from rightsizer.engine.hpa_replay import assumed_behavior
    return assumed_behavior()


def test_a_memory_hpa_follows_memory_and_the_controller_takes_the_larger_ask() -> None:
    """CPU is idle, memory is busy: the replicas follow memory."""
    from rightsizer.engine.hpa_replay import MemoryMetric, replay

    GI = 2**30
    n = 600
    mem = MemoryMetric(variable=[6 * GI] * n, base=0.2 * GI, pod_request=1 * GI, target=0.8)
    r = replay([0.1] * n, pod_request=0.5, target=0.7, min_replicas=2, max_replicas=50,
               behavior=_behavior(), memory=mem)
    # 6 GiB of load at 0.8 GiB usable per pod beyond a 0.2 GiB footprint: ~10 pods.
    assert 9 <= r.replicas[-1] <= 11, r.replicas[-1]
    cpu_only = replay([0.1] * n, pod_request=0.5, target=0.7, min_replicas=2, max_replicas=50,
                      behavior=_behavior())
    assert cpu_only.replicas[-1] == 2


def test_a_memory_footprint_above_the_target_pins_the_hpa_at_its_ceiling() -> None:
    """Each pod idles at 90% of its request against an 80% target: adding pods
    never brings utilisation down, so the HPA runs to maxReplicas and stays."""
    from rightsizer.engine.hpa_replay import MemoryMetric, replay

    GI = 2**30
    mem = MemoryMetric(variable=[0.0] * 600, base=0.9 * GI, pod_request=1 * GI, target=0.8)
    r = replay([0.1] * 600, pod_request=0.5, target=0.7, min_replicas=2, max_replicas=8,
               behavior=_behavior(), memory=mem)
    assert r.replicas[-1] == 8 and r.capped_minutes > 500


def test_memory_metric_splits_footprint_from_load() -> None:
    from rightsizer.engine.hpa_replay import memory_metric

    GI = 2**30
    # 4 pods idling at 0.5 GiB each, plus 2 GiB of load.
    m = memory_metric([4 * 0.5 * GI + 2 * GI] * 50 + [4 * 0.5 * GI] * 50, [4] * 100, 1 * GI, 0.8)
    assert abs(m.base - 0.5 * GI) < 1e-6 * GI
    assert abs(m.variable[0] - 2 * GI) < 1e-6 * GI and m.variable[-1] == 0.0


def test_a_ceiling_is_sized_on_load_that_lasted_not_one_sample() -> None:
    from rightsizer.engine.hpa_replay import replay

    load = [1.0] * 300 + [20.0] + [1.0] * 100 + [6.0] * 30 + [1.0] * 100
    r = replay(load, pod_request=0.5, target=0.7, min_replicas=2, max_replicas=5,
               behavior=_behavior())
    assert r.peak_wanted >= 50              # the one-minute spike
    assert r.sustained_wanted(5) == 18      # 6 cores for 30 minutes: ceil(6 / 0.35)
    assert r.capped_minutes >= 30


def test_resource_targets_cover_cpu_and_memory_only() -> None:
    from rightsizer.core.models import HPASpec

    both = HPASpec("ns", "h", targets=[("cpu", "utilization"), ("memory", "utilization")],
                   cpu_target=0.7, mem_target=0.8)
    assert both.resource_targets == {"cpu": 0.7, "memory": 0.8}
    assert HPASpec("ns", "h", targets=[("aggregated:pvc_usage", "value")]).resource_targets is None
    assert HPASpec("ns", "h", cpu_target=0.6).resource_targets == {"cpu": 0.6}


# -- Cluster Autoscaler v1.35.0 rules ----------------------------------------------

def _ds(node, cpu=2.0, mem=1.0):
    from rightsizer.engine.autoscaler import PlacedPod

    return PlacedPod(namespace="kube-system", name=f"ds-{node}", node=node, cpu=cpu,
                     memory=mem * 1024**3, owner_kind="DaemonSet", workload_key="kube-system/ds/DaemonSet")


def test_utilisation_leaves_daemonsets_out_of_the_allocatable_too() -> None:
    """``utilization.Calculate``: with ignore-daemonsets-utilization, DaemonSet
    requests leave the sum *and* the allocatable. 3.2 of 8 cores beside a 2-core
    DaemonSet is 3.2/6 = 53%, above 50% -- not the 40% that 3.2/8 reads."""
    from rightsizer.engine.autoscaler import simulate_ca_scale_down

    pods = [_ds("a"), _pod("ns", "p", "a", cpu=3.2), _ds("b"), _pod("ns", "q", "b", cpu=0.1)]
    run = simulate_ca_scale_down(nodes=["a", "b"], pods=pods, alloc_cpu=8.0, alloc_mem=64 * 1024**3,
                                 config=_ca(**{"ignore-daemonsets-utilization": "true"}))
    assert run.removed == ["b"], "a is above the threshold once DaemonSets leave the allocatable"


def test_a_node_exactly_at_the_threshold_is_a_candidate() -> None:
    """``isNodeAtOrBelowUtilizationThreshold``: at or below."""
    from rightsizer.engine.autoscaler import simulate_ca_scale_down

    pods = [_pod("ns", "p", "a", cpu=4.0), _pod("ns", "q", "b", cpu=1.0)]
    run = simulate_ca_scale_down(nodes=["a", "b"], pods=pods, alloc_cpu=8.0, alloc_mem=64 * 1024**3,
                                 config=_ca())
    assert run.total == 1


def test_kube_system_pods_stop_blocking_after_the_disruption_timeout() -> None:
    """``--blocking-system-pod-distruption-timeout`` (1h): an older kube-system
    pod without a PDB is drainable; a younger one, or one of unknown age, blocks."""
    import dataclasses

    from rightsizer.engine.autoscaler import simulate_ca_scale_down

    now = 1_000_000
    sys_pod = _pod("kube-system", "dns", "a", wl="kube-system/dns/Deployment")
    kw = dict(nodes=["a", "b"], alloc_cpu=8.0, alloc_mem=64 * 1024**3, config=_ca(), now=now,
              rules=_rules())
    other = _pod("ns", "q", "b")
    old = simulate_ca_scale_down(pods=[dataclasses.replace(sys_pod, created=now - 7200), other], **kw)
    young = simulate_ca_scale_down(pods=[dataclasses.replace(sys_pod, created=now - 600), other], **kw)
    unknown = simulate_ca_scale_down(pods=[sys_pod, other], **kw)
    assert old.total == 1
    assert young.total == 1 and young.removed == ["b"] and "has no PDB" in young.vetoed["a"]
    assert "age unknown" in unknown.vetoed["a"]
    short = simulate_ca_scale_down(
        pods=[dataclasses.replace(sys_pod, created=now - 600), other],
        **{**kw, "config": _ca(**{"blocking-system-pod-distruption-timeout": "5m"})})
    assert "a" not in short.vetoed


def test_safe_to_evict_true_overrides_every_later_rule() -> None:
    """``safetoevict`` runs before the system, local-storage and PDB rules."""
    from rightsizer.engine.autoscaler import simulate_ca_scale_down
    from rightsizer.engine.eviction import Pdb

    key = "kube-system/tool/Deployment"
    pods = [_pod("kube-system", "tool", "a", wl=key), _pod("ns", "q", "b")]
    rules = _rules(safe_to_evict=frozenset({key}), local_storage=frozenset({key}),
                   pdb_for={key: Pdb("kube-system", "p", {}, max_unavailable="0")})
    run = simulate_ca_scale_down(nodes=["a", "b"], pods=pods, alloc_cpu=8.0,
                                 alloc_mem=64 * 1024**3, config=_ca(), rules=rules)
    assert run.total == 1 and not run.vetoed


def test_a_custom_controller_blocks_unless_ca_is_told_otherwise() -> None:
    from rightsizer.engine.autoscaler import simulate_ca_scale_down

    pods = [_pod("ns", "pg-1", "a", wl="ns/pg/Cluster", kind="Cluster"), _pod("ns", "q", "b")]
    kw = dict(nodes=["a", "b"], pods=pods, alloc_cpu=8.0, alloc_mem=64 * 1024**3, rules=_rules())
    held = simulate_ca_scale_down(**kw, config=_ca())
    assert "custom controller (Cluster)" in held.vetoed["a"]
    freed = simulate_ca_scale_down(**kw, config=_ca(**{"skip-nodes-with-custom-controller-pods": "false"}))
    assert "a" not in freed.vetoed


def test_one_drained_node_per_pass_and_the_budget_is_spent_within_it() -> None:
    """``max-drain-parallelism`` 1, and ``RemainingPdbTracker``: two nodes each
    holding one pod of a one-disruption budget cannot both be removed in one
    pass -- but the budget is back once the first pod is rescheduled."""
    from rightsizer.engine.autoscaler import simulate_ca_scale_down
    from rightsizer.engine.eviction import Pdb

    pods = [_pod("ns", "w1", "a"), _pod("ns", "w2", "b"), _pod("ns", "x", "c", wl="ns/x/Deployment")]
    rules = _rules(pdb_for={"ns/wl/Deployment": Pdb("ns", "p", {}, max_unavailable="1")},
                   replicas={"ns/wl/Deployment": 2})
    run = simulate_ca_scale_down(nodes=["a", "b", "c"], pods=pods, alloc_cpu=8.0,
                                 alloc_mem=64 * 1024**3, config=_ca(), rules=rules)
    assert run.total == 2 and run.iterations >= 3


def test_drained_pods_land_where_the_scheduler_puts_them() -> None:
    """CA's simulation rotates through nodes; kube-scheduler picks the least
    allocated. The pods of a drained node end on the emptier of two."""
    from rightsizer.engine.autoscaler import simulate_ca_scale_down

    pods = [_pod("ns", "p", "a", cpu=0.5), _pod("ns", "busy", "b", cpu=4.5, wl="ns/b/Deployment"),
            _pod("ns", "calm", "c", cpu=4.2, wl="ns/c/Deployment")]
    run = simulate_ca_scale_down(nodes=["a", "b", "c"], pods=pods, alloc_cpu=8.0,
                                 alloc_mem=64 * 1024**3, config=_ca())
    assert run.removed == ["a"]
    assert [p.name for p in run.placement["c"]] == ["calm", "p"]


def test_a_node_annotated_scale_down_disabled_is_kept() -> None:
    from rightsizer.engine.autoscaler import simulate_ca_scale_down

    pods = [_pod("ns", "p", "a"), _pod("ns", "q", "b")]
    run = simulate_ca_scale_down(nodes=["a", "b"], pods=pods, alloc_cpu=8.0, alloc_mem=64 * 1024**3,
                                 config=_ca(), rules=_rules(scale_down_disabled=frozenset({"a", "b"})))
    assert run.total == 0 and "scale-down-disabled" in run.vetoed["a"]


def test_a_pod_bigger_than_a_node_is_unplaceable_not_a_new_node() -> None:
    """prod-us1 resources-1: Prometheus recommended at 41.6 GiB on 29.3 GiB nodes.
    No node of this type ever holds it, so no scale-up is counted."""
    from types import SimpleNamespace

    from rightsizer.engine.eviction import _reschedule_overflow

    group = SimpleNamespace(alloc_cpu_per_node=8.0, alloc_mem_per_node=29.3 * 1024**3)
    pods = [_pod("ns", "prom-0", "a", wl="ns/prom/StatefulSet", kind="StatefulSet", mem=41.6),
            _pod("ns", "grow", "b", mem=20.0), _pod("ns", "grow2", "b", mem=20.0)]
    added, unplaceable = _reschedule_overflow(pods, ["a", "b"], group, _ca())
    assert [p.name for p in unplaceable] == ["prom-0"] and not unplaceable[0].node
    # 20 + 20 GiB overfills b; the one moved lands on a, which prom-0 left empty.
    assert added == 0 and sorted(p.node for p in pods if p.name.startswith("grow")) == ["a", "b"]


def test_trend_charts_read_cpu_at_the_snapshots_rate_window() -> None:
    """The chart and the "peak" figure must read the same series: a 5-minute rate
    drew api-server's 640m one-minute burst as 133m, and no peak was visible."""
    from rightsizer.core.config import Scope, Tunables
    from rightsizer.sources.trends import _batch_exprs

    window = f"[{Tunables().rate_window_seconds}s]"
    for band, expr in _batch_exprs(Scope(env="test-cluster"), "cpu").items():
        assert window in expr, band
        assert "[5m])" not in expr, band


# -- layout: one directory per cluster ------------------------------------------


def test_discovery_reads_both_layouts_and_the_per_cluster_one_wins(tmp_path) -> None:
    from rightsizer.core import layout

    (tmp_path / "snapshot-a.json").write_text("{}")
    (tmp_path / "snapshot-b.json").write_text("{}")
    for rel in ("a/snapshot.json", "a/snapshot-2026-08-06.json", "c/snapshot.json", "c/snapshot.5m.json"):
        (tmp_path / rel).parent.mkdir(exist_ok=True)
        (tmp_path / rel).write_text("{}")
    found, skipped = layout.discover_snapshots(tmp_path)
    assert found[None] == {"a": tmp_path / "a/snapshot.json", "b": tmp_path / "snapshot-b.json",
                           "c": tmp_path / "c/snapshot.json"}
    assert found["2026-08-06"] == {"a": tmp_path / "a/snapshot-2026-08-06.json"}
    assert skipped == ["c/snapshot.5m.json"]
    assert layout.find_snapshot(tmp_path, "b") == tmp_path / "snapshot-b.json", "unmigrated still read"
    assert layout.find_snapshot(tmp_path, "z") == tmp_path / "z/snapshot.json", "new goes per cluster"
    with pytest.raises(ValueError):
        layout.snapshot_path(tmp_path, "../etc")


def test_one_cluster_file_lost_or_broken_costs_only_that_cluster(tmp_path) -> None:
    """The reason for the layout: the fleet is never one file away from gone."""
    import json

    from rightsizer.core import layout

    current = {"id": "current", "label": "Current", "end": None}
    for env, rate in (("a", 100.0), ("b", 300.0)):
        layout.write_period_file(tmp_path, env, current, {"env": env, "currentRunRate": rate,
                                                          "collectedAt": f"t-{env}"}, schema=12, discount=0.1)
    layout.write_period_file(tmp_path, "a", {"id": "2026-08-06", "label": "", "end": "2026-08-06"},
                             {"env": "a", "currentRunRate": 90.0, "collectedAt": "t0"}, schema=12, discount=0.1)
    index = layout.write_index(tmp_path, known=["a", "b", "c"])
    assert [p["id"] for p in index["periods"]] == ["current", "2026-08-06"]
    assert [c["env"] for c in index["periods"][0]["clusters"]] == ["b", "a"], "largest spend first"
    assert index["uncollected"] == ["c"]

    # Re-exporting one cluster rewrites its file alone, and the index still lists both.
    before = (tmp_path / "b/current.json").read_text()
    layout.write_period_file(tmp_path, "a", current, {"env": "a", "currentRunRate": 50.0,
                                                      "collectedAt": "t-a2"}, schema=12, discount=0.1)
    assert (tmp_path / "b/current.json").read_text() == before
    assert {c["env"] for c in layout.write_index(tmp_path)["periods"][0]["clusters"]} == {"a", "b"}

    # The index holds nothing the cluster files do not: lose it, and it comes back.
    layout.index_path(tmp_path).unlink()
    assert layout.write_index(tmp_path)["periods"][0]["clusters"][1]["collectedAt"] == "t-a2"

    # A broken file is named, not silently dropped, and the rest are unaffected.
    (tmp_path / "a/current.json").write_text('{"cluster": ')
    index = layout.write_index(tmp_path)
    assert [c["env"] for c in index["periods"][0]["clusters"]] == ["b"]
    assert [u["file"] for u in index["unreadable"]] == ["a/current.json"]
    fleet = layout.assemble_fleet(tmp_path)
    assert [c["env"] for c in fleet["periods"][0]["clusters"]] == ["b"]
    assert json.loads((tmp_path / "b/current.json").read_text())["cluster"]["currentRunRate"] == 300.0
    assert not list(tmp_path.rglob("*.tmp")), "atomic writes leave no temp files behind"
