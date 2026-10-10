"""The read-only guarantee.

These tests are the guarantee. If one fails, the tool can mutate a cluster or an
AWS account, and no amount of careful calling convention elsewhere compensates.
"""

from __future__ import annotations

import pytest

from rightsizer.adapters.clouds import NodeGroupConfig
from rightsizer.adapters.clouds.aws.eks import _aws
from rightsizer.sources.kube import PDBConstraint, WorkloadConstraints, _kubectl
from rightsizer.sources.shell import ReadOnlyViolation, assert_read_only

MUTATING = [
    ["kubectl", "delete", "pod", "x"],
    ["kubectl", "apply", "-f", "manifest.yaml"],
    ["kubectl", "scale", "deployment/x", "--replicas=0"],
    ["kubectl", "drain", "node-1"],
    ["kubectl", "cordon", "node-1"],
    ["kubectl", "edit", "deployment/x"],
    ["kubectl", "patch", "deployment/x", "-p", "{}"],
    ["kubectl", "rollout", "restart", "deployment/x"],
    ["kubectl", "exec", "pod", "--", "sh"],
    ["kubectl", "create", "namespace", "x"],
    ["kubectl", "replace", "-f", "x.yaml"],
    ["kubectl", "annotate", "node", "x", "k=v"],
    ["kubectl", "label", "node", "x", "k=v"],
    ["kubectl", "taint", "nodes", "x", "k=v:NoSchedule"],
    ["aws", "eks", "update-nodegroup-config", "--cluster-name", "c"],
    ["aws", "ec2", "terminate-instances", "--instance-ids", "i-1"],
    ["aws", "autoscaling", "set-desired-capacity", "--desired-capacity", "0"],
    ["aws", "eks", "delete-nodegroup", "--nodegroup-name", "n"],
    ["aws", "ec2", "create-tags", "--resources", "i-1"],
    ["aws", "s3", "rm", "s3://bucket/key"],
    ["aws", "iam", "attach-role-policy", "--role-name", "r"],
]

READ_ONLY = [
    ["kubectl", "get", "nodes", "-o", "json"],
    ["kubectl", "get", "deployments,statefulsets,daemonsets", "-A", "-o", "json"],
    ["kubectl", "get", "poddisruptionbudgets", "-A", "-o", "json"],
    ["kubectl", "describe", "node", "ip-10-0-0-1"],
    ["kubectl", "version", "--client"],
    ["aws", "eks", "list-nodegroups", "--cluster-name", "c"],
    ["aws", "eks", "describe-nodegroup", "--cluster-name", "c", "--nodegroup-name", "n"],
    ["aws", "ec2", "describe-instances"],
    ["aws", "autoscaling", "describe-auto-scaling-groups"],
    ["aws", "ce", "get-cost-and-usage", "--time-period", "x"],
]


@pytest.mark.parametrize("argv", MUTATING, ids=lambda a: " ".join(a[:3]))
def test_mutating_commands_are_refused(argv: list[str]) -> None:
    with pytest.raises(ReadOnlyViolation):
        assert_read_only(argv)


@pytest.mark.parametrize("argv", READ_ONLY, ids=lambda a: " ".join(a[:3]))
def test_read_only_commands_are_permitted(argv: list[str]) -> None:
    assert_read_only(argv)


def test_only_kubectl_and_aws_are_allowed() -> None:
    for binary in ("bash", "sh", "curl", "helm", "terraform", "eksctl", "python3", "rm"):
        with pytest.raises(ReadOnlyViolation):
            assert_read_only([binary, "get", "x"])


def test_watch_is_refused_despite_being_read_only() -> None:
    """Read-only but unbounded: this tool takes one snapshot, never holds a stream."""
    for flag in ("-w", "--watch", "--watch-only"):
        with pytest.raises(ReadOnlyViolation):
            assert_read_only(["kubectl", "get", "pods", flag])


def test_mutating_flags_are_refused_on_allowed_verbs() -> None:
    with pytest.raises(ReadOnlyViolation):
        assert_read_only(["kubectl", "get", "pods", "--filename=x.yaml"])
    with pytest.raises(ReadOnlyViolation):
        assert_read_only(["kubectl", "get", "pods", "-f", "x.yaml"])


def test_aws_requires_service_and_operation() -> None:
    with pytest.raises(ReadOnlyViolation):
        assert_read_only(["aws", "eks"])
    with pytest.raises(ReadOnlyViolation):
        assert_read_only(["aws"])


def test_empty_and_flag_only_commands_are_refused() -> None:
    with pytest.raises(ReadOnlyViolation):
        assert_read_only([])
    with pytest.raises(ReadOnlyViolation):
        assert_read_only(["kubectl", "--help"])


def test_builders_emit_only_allowlisted_commands() -> None:
    """Whatever the collectors construct must itself pass the guard."""
    assert_read_only(_kubectl("ctx", "get", "nodes", "-o", "json"))
    assert_read_only(_kubectl(None, "get", "poddisruptionbudgets", "-A", "-o", "json"))
    assert_read_only(_aws("prof", "us-east-1", "eks", "list-nodegroups", "--cluster-name", "c"))
    assert_read_only(_aws(None, None, "eks", "describe-nodegroup", "--cluster-name", "c"))


# -- constraint semantics ---------------------------------------------------


def test_pdb_max_unavailable_zero_blocks_drain() -> None:
    assert PDBConstraint("n", "p", max_unavailable="0").blocks_drain
    assert not PDBConstraint("n", "p", max_unavailable="1").blocks_drain
    assert not PDBConstraint("n", "p", min_available="2").blocks_drain


def test_local_storage_blocks_drain_unless_marked_safe() -> None:
    base = dict(namespace="n", name="w", kind="Deployment")
    assert WorkloadConstraints(**base, local_storage=True).blocks_drain
    assert not WorkloadConstraints(**base, local_storage=True, safe_to_evict=True).blocks_drain
    assert WorkloadConstraints(**base, local_storage=True, safe_to_evict=False).blocks_drain
    # Karpenter's annotation: Cluster Autoscaler has no rule for it.
    assert not WorkloadConstraints(**base, do_not_disrupt=True).blocks_drain
    assert not WorkloadConstraints(**base).blocks_drain


def test_only_the_volumes_ca_counts_as_local_storage_block() -> None:
    """``drain.HasBlockingLocalStorage``: hostPath, or an emptyDir not on tmpfs,
    minus the names in ``safe-to-evict-local-volumes``."""
    from rightsizer.sources.kube import SAFE_TO_EVICT_LOCAL_VOLUMES, blocking_local_volumes

    vols = [{"name": "cache", "emptyDir": {}}, {"name": "shm", "emptyDir": {"medium": "Memory"}},
            {"name": "logs", "hostPath": {"path": "/var/log"}}, {"name": "cfg", "configMap": {}}]
    assert blocking_local_volumes(vols, {}) == ["cache", "logs"]
    assert blocking_local_volumes(vols, {SAFE_TO_EVICT_LOCAL_VOLUMES: "cache, logs"}) == []
    assert blocking_local_volumes([vols[1], vols[3]], {}) == []


def test_nodegroup_at_min_is_detected() -> None:
    assert NodeGroupConfig("g", min_size=3, desired_size=3).at_min
    assert NodeGroupConfig("g", min_size=3, desired_size=5).at_min is False


# -- cluster autoscaler config ----------------------------------------------


def test_ca_args_parse_and_coerce() -> None:
    from rightsizer.engine.autoscaler import parse_ca_args

    cfg = parse_ca_args([
        "scale-down-utilization-threshold=0.45",
        "--ignore-daemonsets-utilization=true",
        "scale-down-unneeded-time=5m",
        "max-graceful-termination-sec=1800",
    ])
    assert cfg.utilization_threshold == pytest.approx(0.45)
    assert cfg.ignores_daemonsets is True
    assert cfg.get("scale-down-unneeded-time") == "5m"
    assert cfg.get("max-graceful-termination-sec") == 1800
    # a flag nobody set still resolves, from upstream defaults
    assert cfg.skips_local_storage is True
    assert cfg.non_default()["scale-down-unneeded-time"] == "5m"


def test_ca_defaults_when_nothing_is_passed() -> None:
    from rightsizer.engine.autoscaler import parse_ca_args

    cfg = parse_ca_args([])
    assert cfg.utilization_threshold == pytest.approx(0.5)
    assert cfg.ignores_daemonsets is False
    assert cfg.non_default() == {}


def test_ca_discovery_uses_only_read_only_commands() -> None:
    """Reading the deployment must go through the same allowlist as everything else."""
    assert_read_only(
        ["kubectl", "--context", "prod-us1", "get", "deployments", "-n", "apps",
         "-o", "json", "--request-timeout=45s"]
    )


def test_kubectl_config_may_only_read_the_kubeconfig() -> None:
    import pytest

    from rightsizer.sources.shell import ReadOnlyViolation, assert_read_only

    assert_read_only(["kubectl", "config", "view"])
    assert_read_only(["kubectl", "config", "current-context"])
    for verb in ("set-context", "use-context", "delete-cluster", "set", "rename-context"):
        with pytest.raises(ReadOnlyViolation):
            assert_read_only(["kubectl", "config", verb, "x"])
    with pytest.raises(ReadOnlyViolation):
        assert_read_only(["kubectl", "config"])
