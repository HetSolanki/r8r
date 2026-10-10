"""The adapter seams: a new cloud or autoscaler plugs in without touching the engine."""

from __future__ import annotations

import pytest

from rightsizer.adapters import autoscalers, clouds, gpu_label_keys, node_group_of
from rightsizer.adapters.autoscalers.base import AutoscalerConfig
from rightsizer.adapters.clouds import CloudProvider, InstanceType, NodeGroupConfig
from rightsizer.core.config import Scope
from rightsizer.core.models import ClusterFacts, Snapshot
from rightsizer.sources import queries as q


class FakeCloud(CloudProvider):
    name = "fake"
    title = "Fake Cloud"
    cli = "fakectl"
    default_region = "moon-1"
    nodegroup_label_keys = ("fake.example.com/pool",)
    gpu_label_keys = ("fake.example.com/gpu",)

    @classmethod
    def matches_context(cls, context):
        return bool(context and context.startswith("fake://"))

    def collect_nodegroups(self, cluster):
        return [NodeGroupConfig(name="p1", min_size=1, max_size=5, desired_size=2,
                                instance_types=["f1.large"])]

    def describe_instance_types(self, names):
        return {n: InstanceType(name=n, vcpu=2, memory_gib=8) for n in names}

    def on_demand_price(self, instance_type, region=None):
        return 0.10

    def family_ladder(self, family):
        return [InstanceType(name=f"{family}.large", vcpu=2, memory_gib=8, on_demand_hourly=0.1)]


@pytest.fixture
def fake_cloud():
    saved = dict(clouds.registry._entries)
    clouds.registry.register("fake", FakeCloud)
    yield FakeCloud
    clouds.registry._entries = saved
    clouds.registry.version += 1


def _snap(meta=None, labels=None, **kw) -> Snapshot:
    facts = ClusterFacts(node_facts=[{"name": "n", "labels": labels or {}}])
    return Snapshot(env="x", collected_at="t", window_days=30, tunables={}, facts=facts,
                    meta=meta or {}, **kw)


def test_a_registered_cloud_reaches_every_seam(fake_cloud) -> None:
    assert clouds.detect(context="fake://c1") == "fake"
    assert clouds.detect(node_labels=[{"fake.example.com/pool": "p1"}]) == "fake"
    provider = clouds.for_snapshot(_snap(meta={"cloud": "fake"}))
    assert isinstance(provider, FakeCloud) and provider.region == "moon-1"
    assert provider.monthly_price("f1.large", 0.5) == pytest.approx(0.10 * 0.5 * 730)
    assert provider.candidate_ladder("f1.xlarge")[0].name == "f1.large", "default ladder: same family"
    # Recognised from the nodes when the snapshot does not say.
    assert clouds.for_snapshot(_snap(labels={"fake.example.com/pool": "p1"})).name == "fake"
    # The engine and the queries see its labels without knowing it exists.
    assert node_group_of({"fake.example.com/pool": "p1"}) == "p1"
    assert "fake.example.com/gpu" in gpu_label_keys()
    assert "label_fake_example_com_pool" in q.node_label_join(Scope(env="x"))


def test_unrecorded_snapshots_are_aws_and_cluster_autoscaler() -> None:
    """Everything collected before adapters existed reads exactly as before."""
    snap = _snap(ca={"source": "live deployment ca", "args": {"scale-down-utilization-threshold": 0.7}})
    assert clouds.for_snapshot(snap).name == "aws"
    adapter, config = autoscalers.for_snapshot(snap)
    assert adapter.name == "cluster-autoscaler" and adapter.models_scale_down
    assert autoscalers.scale_down_settings(snap).utilization_threshold == 0.7
    assert autoscalers.scale_down_settings(_snap()).source == "defaults"


def test_a_managed_node_group_wins_over_an_autoscaler_pool() -> None:
    labels = {"eks.amazonaws.com/nodegroup": "ng", "karpenter.sh/nodepool": "pool"}
    assert node_group_of(labels) == "ng"
    assert node_group_of({"karpenter.sh/nodepool": "pool"}) == "pool"
    assert node_group_of({}) == ""


NODEPOOL = {"metadata": {"name": "general"}, "spec": {
    "disruption": {"consolidationPolicy": "WhenEmptyOrUnderutilized", "consolidateAfter": "30s",
                   "budgets": [{"nodes": "10%"}]},
    "limits": {"cpu": "1000"},
    "template": {"spec": {"requirements": [
        {"key": "node.kubernetes.io/instance-type", "operator": "In", "values": ["m6i.xlarge", "m6i.2xlarge"]}]}}}}
PROVISIONER = {"metadata": {"name": "old"}, "spec": {"ttlSecondsAfterEmpty": 60}}


def test_karpenter_is_discovered_and_mapped_but_never_passed_off_as_modelled(monkeypatch) -> None:
    import rightsizer.sources.shell as shell

    seen = []

    def fake_run_json(argv, timeout=120):
        shell.assert_read_only(argv)
        seen.append(argv)
        if "nodepools.karpenter.sh" in argv:
            return {"items": [NODEPOOL]}
        raise RuntimeError("not installed")

    monkeypatch.setattr(shell, "run_json", fake_run_json)
    karpenter = autoscalers.get("karpenter")
    found = karpenter.discover(context="ctx")
    assert found.kind == "karpenter" and found.settings["nodepools"][0]["instanceTypes"] == [
        "m6i.2xlarge", "m6i.xlarge"]
    assert all("--context" in a and "get" in a for a in seen), "read-only, against the given context"

    ca = karpenter.scale_down_settings(found)
    assert ca.utilization_threshold == 1.0, "consolidating underused nodes has no threshold"
    assert ca.get("scale-down-unneeded-time") == "30s"
    assert not karpenter.models_scale_down
    assert karpenter.notes(found) == [], "a described configuration is context, not a caveat"
    summary = karpenter.summary(found)
    assert "1 NodePools" in summary and "replayed" in summary
    (bare,) = karpenter.notes(None)
    assert "approximation" in bare and "general" in bare

    # Round-trips through a snapshot, and every reader then picks Karpenter.
    snap = _snap(autoscaler=found.to_dict())
    adapter, config = autoscalers.for_snapshot(snap)
    assert adapter.name == "karpenter" and config == found


def test_karpenter_reads_v1alpha5_provisioners(monkeypatch) -> None:
    import rightsizer.sources.shell as shell

    def fake_run_json(argv, timeout=120):
        if "provisioners.karpenter.sh" in argv:
            return {"items": [PROVISIONER]}
        return {"items": []}

    monkeypatch.setattr(shell, "run_json", fake_run_json)
    found = autoscalers.get("karpenter").discover(context=None)
    (pool,) = found.settings["nodepools"]
    assert (pool["consolidationPolicy"], pool["consolidateAfter"]) == ("WhenEmpty", "60s")
    assert autoscalers.get("karpenter").scale_down_settings(found).utilization_threshold < 0.01


def test_discovery_tries_each_autoscaler_in_order(monkeypatch) -> None:
    from rightsizer.adapters.autoscalers.cluster_autoscaler import ClusterAutoscaler
    from rightsizer.adapters.autoscalers.karpenter import Karpenter

    monkeypatch.setattr(ClusterAutoscaler, "discover", lambda self, **kw: None)
    monkeypatch.setattr(Karpenter, "discover",
                        lambda self, **kw: AutoscalerConfig(kind="karpenter", source="test"))
    assert autoscalers.discover(context="c").kind == "karpenter"
    assert autoscalers.discover(context="c", only="cluster-autoscaler") is None


def test_ca_flags_become_cluster_autoscaler_config() -> None:
    cfg = autoscalers.get().from_flags(["scale-down-utilization-threshold=0.6"])
    assert cfg.kind == "cluster-autoscaler" and cfg.source == "--ca-arg flags"
    assert autoscalers.get().scale_down_settings(cfg).utilization_threshold == 0.6
    with pytest.raises(NotImplementedError):
        autoscalers.get("karpenter").from_flags(["x=1"])


def test_only_named_clis_may_run() -> None:
    """A cloud adapter cannot widen the read-only guard by being registered."""
    from rightsizer.sources.shell import READONLY_CLIS, ReadOnlyViolation, assert_read_only

    assert set(READONLY_CLIS) == {"kubectl", "aws", "oci"}
    with pytest.raises(ReadOnlyViolation):
        assert_read_only(["fakectl", "describe", "x"])


def test_unknown_adapters_say_what_is_known() -> None:
    with pytest.raises(KeyError, match="aws"):
        clouds.get("nope")
    with pytest.raises(KeyError, match="karpenter"):
        autoscalers.get("nope")


def test_do_not_disrupt_vetoes_only_under_karpenter() -> None:
    from rightsizer.engine.eviction import rules_from_snapshot

    w = {"namespace": "ns", "name": "db", "kind": "StatefulSet", "do_not_disrupt": True}
    facts = ClusterFacts(workload_constraints=[w])
    ca = Snapshot(env="x", collected_at="t", window_days=30, tunables={}, facts=facts)
    karp = Snapshot(env="x", collected_at="t", window_days=30, tunables={}, facts=facts,
                    autoscaler={"kind": "karpenter", "source": "test", "settings": {}})
    assert "ns/db/StatefulSet" not in rules_from_snapshot(ca).drain_blocked, "not a CA rule"
    assert "do-not-disrupt" in rules_from_snapshot(karp).drain_blocked["ns/db/StatefulSet"]


def test_asg_collection_records_zones_and_where_each_instance_is(monkeypatch) -> None:
    from rightsizer.adapters.clouds.aws import eks

    monkeypatch.setattr(eks, "run_json", lambda argv, timeout=120: {"AutoScalingGroups": [{
        "AutoScalingGroupName": "eks-db-2", "MinSize": 3, "MaxSize": 10, "DesiredCapacity": 3,
        "AvailabilityZones": ["us-east-1a", "us-east-1b", "us-east-1c"], "VPCZoneIdentifier": "subnet-1,subnet-2",
        "Instances": [{"InstanceId": "i-1", "AvailabilityZone": "us-east-1b"}],
        "Tags": [{"Key": "eks:nodegroup-name", "Value": "db-2"}]}]})
    (g,) = eks.collect_scaling_groups("c", profile=None, region="us-east-1")
    assert g["zones"] == ["us-east-1a", "us-east-1b", "us-east-1c"] and g["instance_zones"] == {"i-1": "us-east-1b"}
    assert g["subnets"] == ["subnet-1", "subnet-2"] and g["instances"] == ["i-1"]
