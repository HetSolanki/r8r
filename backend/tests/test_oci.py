"""OCI and Karpenter for OCI, against representative Karpenter NodePool and
NodeClass definitions."""

from __future__ import annotations

import argparse

import pytest

from rightsizer.adapters import autoscalers, clouds
from rightsizer.adapters.autoscalers.karpenter_oci import KarpenterOCI
from rightsizer.adapters.clouds.oci import oke, pricing, shapes
from rightsizer.core.config import Scope
from rightsizer.core.models import ClusterFacts, Snapshot
from rightsizer.sources import queries as q
from rightsizer.sources.shell import ReadOnlyViolation, assert_read_only

# The price list as Oracle publishes it, E5 and A10 (B97384/B97385/B95909).
PRICES = {"B97384": 0.03, "B97385": 0.002, "B95909": 2.0}


@pytest.fixture(autouse=True)
def offline_prices(monkeypatch):
    monkeypatch.setattr(pricing, "_memo", dict(PRICES))


# -- shapes and prices ---------------------------------------------------------


def test_a_flex_shape_is_priced_by_its_size() -> None:
    oci = clouds.get("oci")
    assert oci.on_demand_price("VM.Standard.E5.Flex:4ocpu-32gb") == pytest.approx(4 * 0.03 + 32 * 0.002)
    assert oci.on_demand_price("VM.Standard.E5.Flex:2ocpu-16gb:preemptible") == pytest.approx(
        (2 * 0.03 + 16 * 0.002) / 2), "preemptible is half"
    assert oci.on_demand_price("VM.GPU.A10.1") == 2.0, "a GPU shape is priced per GPU"
    assert oci.on_demand_price("VM.Standard.E5.Flex") == 0.0, "a size-less Flex name says nothing"
    (t,) = oci.describe_instance_types(["VM.Standard.E5.Flex:8ocpu-64gb"]).values()
    assert (t.vcpu, t.memory_gib, t.storage_known, t.storage_gb) == (16, 64, True, 0), "1 OCPU = 2 vCPU"


def test_type_names_round_trip() -> None:
    for name in ("VM.Standard.E5.Flex:4ocpu-32gb", "VM.Standard.E5.Flex:2ocpu-16gb:preemptible",
                 "VM.GPU.A10.1", "VM.GPU.A10.1:preemptible", "VM.Standard.A1.Flex:2.5ocpu-15gb"):
        assert shapes.parse(name).name == name
    assert shapes.parse("m6i.xlarge") is None and shapes.parse("VM.GPU.A10.1:4ocpu-8gb") is None
    assert shapes.parse("VM.Standard.A1.Flex:4ocpu-24gb").vcpu == 4, "A1 is one vCPU per OCPU"


def test_the_paid_rate_is_read_past_a_free_tier() -> None:
    payload = {"items": [
        {"partNumber": "B1", "currencyCodeLocalizations": [{"currencyCode": "USD", "prices": [
            {"model": "PAY_AS_YOU_GO", "value": 0, "rangeMin": 0, "rangeMax": 3000},
            {"model": "PAY_AS_YOU_GO", "value": 0.01, "rangeMin": 3000, "rangeMax": 9e15}]}]},
        {"partNumber": "B2", "currencyCodeLocalizations": [{"currencyCode": "USD", "prices": [
            {"model": "PAY_AS_YOU_GO", "value": 0.03}]}]}]}
    assert pricing.parse_price_list(payload) == {"B1": 0.01, "B2": 0.03}


def test_the_ladder_keeps_the_shape_and_offers_other_memory_ratios() -> None:
    names = [i.name for i in clouds.get("oci").candidate_ladder("VM.Standard.E5.Flex:4ocpu-32gb")]
    assert "VM.Standard.E5.Flex:4ocpu-16gb" in names and "VM.Standard.E5.Flex:4ocpu-64gb" in names
    assert all(n.startswith("VM.Standard.E5.Flex:") for n in names)
    spot = clouds.get("oci").candidate_ladder("VM.Standard.E5.Flex:4ocpu-32gb:preemptible")
    assert all(i.name.endswith(":preemptible") for i in spot), "a spot group stays spot"
    assert [i.name for i in clouds.get("oci").candidate_ladder("VM.GPU.A10.1")] == ["VM.GPU.A10.1"]


def test_oci_is_recognised_from_its_nodes() -> None:
    assert clouds.detect(node_labels=[{"oci.oraclecloud.com/fault-domain": "FAULT-DOMAIN-1"}]) == "oci"
    assert clouds.detect(context="context-c4ydqzrgjqa") is None, "an OKE context says nothing"


# -- OKE managed pools -----------------------------------------------------------


def test_oke_pools_are_read_with_read_only_commands(monkeypatch) -> None:
    calls = []

    def fake(argv, timeout=60):
        assert_read_only(argv)
        calls.append(argv[1:4])
        if argv[1:4] == ["ce", "cluster", "get"]:
            return {"data": {"compartment-id": "ocid1.compartment.oc1..x"}}
        return {"data": [{"name": "system", "node-shape": "VM.Standard.E5.Flex",
                          "node-shape-config": {"ocpus": 2.0, "memory-in-gbs": 8.0},
                          "node-config-details": {"size": 2}}]}

    monkeypatch.setattr(oke, "run_json", fake)
    (pool,) = clouds.get("oci", region="us-ashburn-1").collect_nodegroups("ocid1.cluster.oc1..abc")
    assert (pool.name, pool.instance_types, pool.min_size, pool.max_size) == (
        "system", ["VM.Standard.E5.Flex:2ocpu-8gb"], 2, 2)
    assert calls == [["ce", "cluster", "get"], ["ce", "node-pool", "list"]]
    assert clouds.get("oci").collect_nodegroups("my-cluster") == [], "an OCID or nothing"


@pytest.mark.parametrize("argv", [
    ["oci", "ce", "node-pool", "delete", "--node-pool-id", "x"],
    ["oci", "ce", "node-pool", "delete", "--unknown-flag", "list"],
    ["oci", "compute", "instance", "terminate", "--instance-id", "x"],
    ["oci", "compute", "instance", "action", "--action", "STOP", "--instance-id", "x"],
    ["oci", "raw-request", "--http-method", "DELETE", "--target-uri", "x"],
])
def test_oci_mutations_are_refused(argv) -> None:
    with pytest.raises(ReadOnlyViolation):
        assert_read_only(argv)


# -- Karpenter for OCI -------------------------------------------------------------

# karpenter.hcl `services` and `engine_adhoc`, at the developer row of capacity.hcl.
LADDER = [{"ocpus": 2, "memoryInGbs": 16}, {"ocpus": 4, "memoryInGbs": 32},
          {"ocpus": 8, "memoryInGbs": 64}, {"ocpus": 16, "memoryInGbs": 128}]


def _nodepool(name, cpu, spot=False):
    return {"metadata": {"name": name}, "spec": {
        "disruption": {"consolidationPolicy": "WhenEmptyOrUnderutilized", "consolidateAfter": "30m",
                       "budgets": []},
        "limits": {"cpu": str(cpu), "memory": f"{cpu * 4}Gi"},
        "template": {"spec": {"nodeClassRef": {"name": name},
                              "requirements": [{"key": "karpenter.sh/capacity-type", "operator": "In",
                                                "values": ["spot" if spot else "on-demand"]},
                                               {"key": "node.kubernetes.io/instance-type",
                                                "operator": "In", "values": ["VM.Standard.E5.Flex"]}]}}}}


NODEPOOLS = [_nodepool("services", 20), _nodepool("batch", 80, spot=True)]
NODECLASSES = [{"metadata": {"name": n}, "spec": {"shapeConfigs": LADDER}} for n in ("services", "batch")]


def _kubectl(argv, timeout=60):
    assert_read_only(argv)
    if "nodepools.karpenter.sh" in argv:
        return {"items": NODEPOOLS}
    if "ocinodeclasses" in argv:
        return {"items": NODECLASSES}
    raise RuntimeError("not installed")


def _node(name, pool, vcpu, gib):
    return {"name": name, "nodegroup": pool, "instance_type": "VM.Standard.E5.Flex",
            "cpu_capacity": vcpu, "mem_capacity_gib": gib, "labels": {"karpenter.sh/nodepool": pool}}


def test_kpo_pools_become_node_groups_with_their_sizes_and_ceiling(monkeypatch) -> None:
    import rightsizer.sources.shell as shell

    monkeypatch.setattr(shell, "run_json", _kubectl)
    found = autoscalers.discover(context="ctx")
    assert found.kind == "karpenter-oci", "the OCI form wins over plain Karpenter"

    facts = ClusterFacts(node_facts=[
        _node("a", "services", 8, 30.6), _node("b", "services", 8, 30.9),
        _node("c", "services", 16, 61.8),
        _node("d", "batch", 4, 15.2)])
    warnings: list[str] = []
    groups = {g.name: g for g in KarpenterOCI().nodegroup_configs(found, facts, warnings)}

    svc = groups["services"]
    assert svc.instance_types[0] == "VM.Standard.E5.Flex:4ocpu-32gb", "the size most nodes are"
    assert len(svc.instance_types) == 4, "every size the NodeClass offers"
    assert (svc.min_size, svc.desired_size, svc.max_size) == (0, 3, 2), "20 vCPU / 8 per node"
    assert svc.limits == {"cpu": "20", "memory": "80Gi"}
    assert any("services runs mixed sizes" in w for w in warnings)

    adhoc = groups["batch"]
    assert adhoc.instance_types[0] == "VM.Standard.E5.Flex:2ocpu-16gb:preemptible"
    assert adhoc.capacity_type == "preemptible" and adhoc.max_size == 20
    assert clouds.get("oci").monthly_price(adhoc.instance_types[0], 0) == pytest.approx(
        (2 * 0.03 + 16 * 0.002) / 2 * 730)

    settings = KarpenterOCI().scale_down_settings(found)
    assert settings.get("scale-down-unneeded-time") == "1800s", "consolidateAfter = 30m"
    assert not KarpenterOCI.models_scale_down


def test_plain_karpenter_without_node_classes_is_not_kpo(monkeypatch) -> None:
    import rightsizer.sources.shell as shell

    def no_classes(argv, timeout=60):
        if "nodepools.karpenter.sh" in argv:
            return {"items": NODEPOOLS}
        raise RuntimeError("the server doesn't have a resource type ocinodeclasses")

    monkeypatch.setattr(shell, "run_json", no_classes)
    assert autoscalers.discover(context="ctx").kind == "karpenter"


# -- one cluster's own Prometheus --------------------------------------------------


def test_a_single_cluster_prometheus_pins_no_env() -> None:
    scope = Scope(env="oke-dev1", env_label="")
    assert scope.node_matchers() == "" and scope.matchers('pod!=""') == 'pod!=""'
    join = q.node_label_join(scope)
    assert "env=" not in join and "{label_karpenter_sh_nodepool" in join
    assert "env=" not in q.owner_join(scope)
    assert Scope(env="x").node_matchers() == 'env="x"', "Thanos still pins it"


def test_the_metrics_source_follows_the_snapshot(monkeypatch) -> None:
    from rightsizer.cli import _metrics

    def ns(**kw):
        return argparse.Namespace(**{"prometheus_url": None, "thanos_url": None, "env_label": None, **kw})

    assert _metrics(ns(prometheus_url="http://localhost:9090")) == ("http://localhost:9090", "")
    assert _metrics(ns(prometheus_url="http://p:9090", env_label="cluster")) == ("http://p:9090", "cluster")
    snap = Snapshot(env="x", collected_at="t", window_days=14, tunables={},
                    meta={"metrics": {"url": "http://localhost:9090", "env_label": ""}})
    assert _metrics(ns(), snap) == ("http://localhost:9090", ""), "a later command asks the same store"
    assert Scope.for_snapshot(snap).env_label == ""
    assert _metrics(ns())[1] == "env"


class FakeClient:
    """``since``: the first timestamp the store holds history for."""

    def __init__(self, *, groups=True, history=True, since=0):
        self.groups, self.history, self.since = groups, history, since
        self.probes = 0

    def query_many(self, queries, at=None):
        return {k: [{"value": [0, "1"]}] for k in queries}

    def query_instant(self, query, at=None):
        if "kube_node_labels" in query:
            return [{"value": [0, "3"]}] if self.groups else []
        self.probes += 1
        held = self.history and (at is None or at >= self.since)
        return [{"value": [0, "1"]}] if held else []


def test_preflight_names_a_missing_group_label_and_a_window_past_retention() -> None:
    from rightsizer.sources.collect import PreflightError, preflight

    scope = Scope(env="x", env_label="")
    preflight(FakeClient(), scope, at=30 * 86400, start=16 * 86400)
    with pytest.raises(PreflightError, match="metric-labels-allowlist"):
        preflight(FakeClient(groups=False), scope, at=30 * 86400, start=16 * 86400)
    with pytest.raises(PreflightError, match="nothing to size from"):
        preflight(FakeClient(history=False), scope, at=30 * 86400, start=0)


def test_a_window_past_retention_shrinks_to_the_whole_days_that_exist() -> None:
    from rightsizer.sources.collect import preflight

    day, scope = 86400, Scope(env="x", env_label="")
    end = 100 * day
    # Asked for 15 days, the store holds the last 7 and a bit: 7 whole days.
    client = FakeClient(since=end - 7 * day - 5 * 3600)
    assert preflight(client, scope, at=end, start=end - 15 * day) == end - 7 * day
    assert client.probes <= 6, "a binary search, not a probe per day"
    # The whole window held: the start is returned unchanged.
    assert preflight(FakeClient(), scope, at=end, start=end - 15 * day) == end - 15 * day
    # Exactly one day held is enough; less than one is not.
    assert preflight(FakeClient(since=end - day), scope, at=end, start=end - 15 * day) == end - day
    with pytest.raises(Exception, match="less than 1 day"):
        preflight(FakeClient(since=end - 3600), scope, at=end, start=end - 15 * day)


def test_a_snapshot_says_when_it_holds_less_than_was_asked(tmp_path) -> None:
    snap = Snapshot(env="x", collected_at="t", window_days=7, tunables={}, requested_window_days=15)
    assert snap.window_shortened
    back = Snapshot.read(snap.write(tmp_path / "s.json"))
    assert (back.window_days, back.requested_window_days) == (7, 15)
    # Older snapshots never recorded a request and always got what they asked.
    assert not Snapshot(env="x", collected_at="t", window_days=30, tunables={}).window_shortened


def test_an_estate_label_names_groups_no_adapter_does(monkeypatch) -> None:
    from rightsizer.adapters import node_group_of

    monkeypatch.setenv("RIGHTSIZER_GROUP_LABELS", "example.com/node-pool")
    assert node_group_of({"example.com/node-pool": "system"}) == "system"
    assert node_group_of({"karpenter.sh/nodepool": "services",
                          "example.com/node-pool": "services"}) == "services"
    assert "label_example_com_node_pool" in q.node_label_join(Scope(env="x", env_label=""))


def test_a_context_names_its_cluster_region_and_profile(monkeypatch) -> None:
    """An OKE kubeconfig's token command already says which cluster: no --cluster."""
    import rightsizer.sources.kube as kube
    from rightsizer import cli

    ocid = "ocid1.cluster.oc1.phx.aaaaaaaay3jp5berk"
    view = {"contexts": [{"name": "context-c4abc", "context": {"cluster": "cluster-c4abc"}},
                         {"name": "c", "context": {"cluster": "c"}}],
            "users": [{"name": "user-c", "user": {"exec": {
        "command": "oci",
        "args": ["ce", "cluster", "generate-token", "--cluster-id", ocid, "--region", "us-phoenix-1"],
        "env": [{"name": "OCI_CLI_PROFILE", "value": "DEFAULT"}]}}}]}

    def fake(argv, timeout=30):
        assert_read_only(argv)
        assert argv[1:3] == ["config", "view"] or "config" in argv
        return view

    monkeypatch.setattr(kube, "run_json", fake)
    args = argparse.Namespace(context="context-c4abc", kubeconfig="/k", cloud=None, cluster=None,
                              region=None, profile=None)
    cli._apply_kubeconfig(args)
    assert (args.cloud, args.cluster, args.region, args.profile) == ("oci", ocid, "us-phoenix-1", "DEFAULT")

    explicit = argparse.Namespace(context="c", kubeconfig=None, cloud=None, cluster="mine",
                                  region="us-ashburn-1", profile=None)
    cli._apply_kubeconfig(explicit)
    assert (explicit.cluster, explicit.region, explicit.profile) == ("mine", "us-ashburn-1", "DEFAULT"), \
        "a flag given wins"

    eks = {"command": "aws", "args": ["--region", "us-east-1", "eks", "get-token", "--cluster-name", "x"]}
    assert clouds.from_kubeconfig(eks) == ("aws", {"cluster": "x", "region": "us-east-1"})


def test_a_cluster_entry_passed_as_the_context_is_named(monkeypatch) -> None:
    import rightsizer.sources.kube as kube

    view = {"contexts": [{"name": "context-oke-dev2-x", "context": {"cluster": "cluster-oke-dev2-x"}}]}
    monkeypatch.setattr(kube, "run_json", lambda argv, timeout=30: view)
    assert "--context context-oke-dev2-x" in kube.context_hint("cluster-oke-dev2-x")
    assert kube.context_hint("context-oke-dev2-x") is None
    assert "contexts: context-oke-dev2-x" in kube.context_hint("nope")


# -- the Karpenter pool as a ladder of sizes ---------------------------------------


def test_kpo_node_labels_name_the_size_exactly() -> None:
    t = shapes.parse("VM.Standard.E5.Flex.4o.32g.1_1b")
    assert (t.shape, t.ocpus, t.memory_gb, t.vcpu, t.name) == (
        "VM.Standard.E5.Flex", 4, 32, 8, "VM.Standard.E5.Flex:4ocpu-32gb")
    assert shapes.kpo_name(t) == "VM.Standard.E5.Flex.4o.32g.1_1b"
    assert shapes.parse("VM.Standard.E5.Flex.4o.32g.1_1b:preemptible").preemptible


def test_allocatable_follows_kpo_formula_and_grows_with_the_node() -> None:
    """vm_memory_overhead.go + kubeReservedResources in karpenter-provider-oci."""
    cpu, mem = shapes.allocatable(shapes.parse("VM.Standard.E5.Flex:2ocpu-16gb"))
    # 4 vCPU - (85m kube + 100m system); 16384 MiB - 904 MiB hypervisor - 2.6 GiB kube - 100 MiB
    assert (round(cpu, 3), round(mem, 2)) == (3.815, 12.42)
    cpu, mem = shapes.allocatable(shapes.parse("VM.Standard.E5.Flex:16ocpu-128gb"))
    assert round(mem, 1) == 115.6 and round(cpu, 2) == 31.74
    assert shapes.allocatable(shapes.parse("VM.Standard.E5.Flex:16ocpu-128gb"))[1] / 128 > \
        shapes.allocatable(shapes.parse("VM.Standard.E5.Flex:2ocpu-16gb"))[1] / 16, \
        "the reservation is a smaller share of a bigger node"
    (t,) = clouds.get("oci").describe_instance_types(["VM.Standard.E5.Flex:2ocpu-16gb"]).values()
    assert (round(t.alloc_vcpu, 3), round(t.alloc_mem_gib, 2)) == (3.815, 12.42)


def test_the_ladder_spans_memory_ratios_and_the_pools_own_rungs() -> None:
    snap = Snapshot(env="x", collected_at="t", window_days=14, tunables={}, meta={"cloud": "oci"})
    config = {"source": "karpenter-oci", "instance_types": [
        "VM.Standard.E5.Flex:2ocpu-16gb", "VM.Standard.E5.Flex:6ocpu-48gb"]}
    names = [i.name for i in clouds.ladder_for(snap, config, "VM.Standard.E5.Flex:2ocpu-16gb")]
    assert "VM.Standard.E5.Flex:6ocpu-48gb" in names, "a rung the pool lists is searched"
    ratios = {int(n.split("ocpu-")[1][:-2]) // int(n.split(":")[1].split("ocpu")[0]) for n in names
              if "6ocpu" not in n}
    assert {2, 4, 8, 16, 32} <= ratios, "CPU-heavy to memory-heavy sizes of the same shape"
    assert all(n.startswith("VM.Standard.E5.Flex:") for n in names)


def test_a_type_change_is_in_the_owners_words() -> None:
    from rightsizer.adapters import retype_steps

    oci = Snapshot(env="x", collected_at="t", window_days=14, tunables={}, meta={"cloud": "oci"})
    pool = {"name": "services", "source": "karpenter-oci", "limits": {"cpu": "20"},
            "instance_types": ["VM.Standard.E5.Flex:2ocpu-16gb", "VM.Standard.E5.Flex:4ocpu-32gb"]}
    (offered,) = retype_steps(oci, pool, "VM.Standard.E5.Flex:4ocpu-32gb")
    assert offered.startswith("already offered to Karpenter") and "nothing to change" in offered
    (new,) = retype_steps(oci, pool, "VM.Standard.E5.Flex:4ocpu-16gb")
    assert new.startswith("add { ocpus = 4, memoryInGbs = 16 } to the pool's shapeConfigs")
    assert "keeping the existing sizes" in new, "removing a size is drift: say to keep them"

    kpo = autoscalers.get("karpenter-oci")
    cmd = kpo.retype_command("c", pool, "VM.Standard.E5.Flex:4ocpu-16gb", 1)
    assert "karpenter_nodepools.services.shapeConfigs" in cmd
    assert "    { ocpus = 2, memoryInGbs = 16 }," in cmd and "+   { ocpus = 4, memoryInGbs = 16 }," in cmd
    assert cmd.index("+   { ocpus = 4, memoryInGbs = 16 }") < cmd.index("{ ocpus = 4, memoryInGbs = 32 }"), "in ladder order"
    assert "20 vCPU ceiling is 2 node(s)" in cmd
    assert "eksctl" not in cmd
    assert "consolidates to 1" in kpo.scale_in_command("c", pool, 1)

    managed = {"name": "system", "source": "cloud", "id": "ocid1.nodepool.oc1..x",
               "instance_types": ["VM.Standard.E5.Flex:2ocpu-8gb"]}
    (step,) = retype_steps(oci, managed, "VM.Standard.E5.Flex:4ocpu-16gb")
    assert step.startswith("update the node pool's shape config to 4 OCPU / 16 GB")
    cmd = clouds.get("oci").retype_command("c", managed, "VM.Standard.E5.Flex:4ocpu-16gb", 2)
    assert 'oci ce node-pool update --node-pool-id ocid1.nodepool.oc1..x --node-shape-config' in cmd
    assert '"ocpus": 4, "memoryInGBs": 16' in cmd

    aws = Snapshot(env="x", collected_at="t", window_days=30, tunables={})
    (step,) = retype_steps(aws, {"name": "g", "min_size": 1, "max_size": 3}, "m6i.xlarge")
    assert step == "replace the node group (instance type is immutable)", "EKS reads as before"
    assert "eksctl create nodegroup" in clouds.get("aws").retype_command("c", {"name": "g"}, "m6i.xlarge", 2)


def test_the_report_header_names_the_cloud_store_and_autoscaler() -> None:
    from rightsizer.output.report import to_markdown

    snap = Snapshot(env="oke-dev1", collected_at="t", window_days=14, tunables={},
                    meta={"cloud": "oci", "metrics": {"url": "http://p:9090", "env_label": ""}},
                    autoscaler={"kind": "karpenter-oci", "source": "12 nodepools", "settings": {
                        "nodepools": [{"name": "services", "consolidationPolicy": "WhenEmptyOrUnderutilized",
                                       "consolidateAfter": "30m"}]}})
    md = to_markdown(snap, [], [], [], 0.0, region="us-phoenix-1", profile=None, account="")
    head = md.split("## ")[0]
    assert "Oracle Cloud Infrastructure" in head and "Prometheus PromQL" in head
    assert "Karpenter (OCI provider) (scale-down approximated)" in head
    assert "EKS" not in head and "AWS" not in head and "Thanos" not in head


def test_local_storage_blocks_drain_only_under_cluster_autoscaler() -> None:
    """Karpenter drains emptyDir/hostPath pods; Cluster Autoscaler refuses them."""
    from rightsizer.analysis.actions import annotation_actions
    from rightsizer.analysis.blockers import nodegroup_blockers
    from rightsizer.core.config import Tunables
    from rightsizer.core.models import NodeGroupState

    def blockers(snapshot):
        return nodegroup_blockers(group, snapshot, Tunables(), 0.95)

    w = {"namespace": "apps", "name": "app", "kind": "Deployment", "local_storage": True,
         "safe_to_evict": None}
    pinned = {**w, "name": "db", "do_not_disrupt": True}
    facts = ClusterFacts(collected=True, workload_constraints=[w, pinned],
                         node_facts=[{"name": "n", "nodegroup": "services", "labels": {}}])
    group = NodeGroupState(name="services", nodes_current=2)

    def snap(**kw):
        return Snapshot(env="x", collected_at="t", window_days=14, tunables={}, facts=facts,
                        nodegroups=[group],
                        workload_nodegroups={"apps/app/Deployment": {"services": 1.0},
                                             "apps/db/Deployment": {"services": 1.0}}, **kw)

    ca = snap(ca={"source": "live", "args": {}})
    (act,) = annotation_actions(ca)
    assert "Cluster Autoscaler will not drain" in act.summary
    codes = {b.code: b.detail for b in blockers(ca) if "drain" in b.code}
    assert "local-storage-blocks-drain" in codes and "2 workload(s)" in codes["local-storage-blocks-drain"]

    karp = snap(autoscaler={"kind": "karpenter-oci", "source": "t", "settings": {"nodepools": [
        {"name": "services", "consolidationPolicy": "WhenEmptyOrUnderutilized", "consolidateAfter": "30m"}]}})
    assert annotation_actions(karp) == [], "nothing to annotate: Karpenter drains emptyDir pods"
    codes = {b.code: b.detail for b in blockers(karp) if "drain" in b.code}
    assert list(codes) == ["do-not-disrupt-blocks-drain"]
    assert "1 workload(s) Karpenter (OCI provider) will not evict (apps/db)" in codes["do-not-disrupt-blocks-drain"]
