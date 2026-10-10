"""In-cluster mode: a pod reaches its own cluster with no kubeconfig context."""

from __future__ import annotations

import pytest

from rightsizer.sources import collect, hpa_history, kube


@pytest.fixture
def outside(monkeypatch, tmp_path):
    """A laptop: no service host, no mounted token."""
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.setattr(kube, "SERVICE_ACCOUNT_TOKEN", tmp_path / "absent")


@pytest.fixture
def inside(monkeypatch, tmp_path):
    """A pod: the service host is set and the token is mounted."""
    token = tmp_path / "token"
    token.write_text("t")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.setattr(kube, "SERVICE_ACCOUNT_TOKEN", token)


def test_a_laptop_without_a_context_has_no_cluster(outside) -> None:
    assert not kube.in_cluster()
    assert not kube.has_access(None)
    assert kube.has_access("ctx")


def test_a_pod_reaches_its_own_cluster_without_a_context(inside) -> None:
    assert kube.in_cluster()
    assert kube.has_access(None)


def test_a_kubeconfig_in_a_pod_names_another_cluster(inside, monkeypatch) -> None:
    monkeypatch.setenv("KUBECONFIG", "/somewhere/else")
    assert not kube.in_cluster()


def test_the_service_host_alone_is_not_enough(inside, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(kube, "SERVICE_ACCOUNT_TOKEN", tmp_path / "absent")
    assert not kube.in_cluster()


def test_a_pod_gets_a_kubeconfig_that_survives_kubectl_flags(inside, monkeypatch, tmp_path) -> None:
    # kubectl's own fallback is skipped once --request-timeout changes its
    # config, and it then dials localhost:8080; a kubeconfig is used regardless.
    import json

    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    monkeypatch.setattr(kube.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(kube, "_IN_CLUSTER_KUBECONFIG", None)
    path = kube.use_in_cluster_config()
    assert path and kube.os.environ["KUBECONFIG"] == path
    cfg = json.loads(open(path).read())
    cluster = cfg["clusters"][0]["cluster"]
    assert cluster["server"] == "https://10.96.0.1:443"
    assert cluster["certificate-authority"].endswith("ca.crt")
    # The token is referenced, not copied: a rotated token is read fresh.
    assert cfg["users"][0]["user"] == {"tokenFile": str(kube.SERVICE_ACCOUNT_TOKEN)}
    # Still in-cluster although KUBECONFIG is now set: it is our own file.
    assert kube.in_cluster() and kube.has_access(None)


def test_no_kubeconfig_is_written_on_a_laptop(outside, monkeypatch) -> None:
    monkeypatch.setattr(kube, "_IN_CLUSTER_KUBECONFIG", None)
    assert kube.use_in_cluster_config() is None
    assert "KUBECONFIG" not in kube.os.environ


def test_in_cluster_kubectl_names_no_context() -> None:
    assert "--context" not in kube._kubectl(None, "get", "nodes")


def _fake_collectors(monkeypatch) -> list[str | None]:
    seen: list[str | None] = []

    def record(result):
        def fn(context=None, **_):
            seen.append(context)
            return result
        return fn

    node = kube.NodeFacts(name="n1", nodegroup="g", max_pods=110)
    monkeypatch.setattr(kube, "collect_workload_constraints", record([]))
    monkeypatch.setattr(kube, "collect_pdbs", record([]))
    monkeypatch.setattr(kube, "collect_nodes", record([node]))
    return seen


def test_cluster_facts_are_collected_in_a_pod(inside, monkeypatch) -> None:
    seen = _fake_collectors(monkeypatch)
    facts = collect.collect_cluster_facts(context=None)
    assert seen == [None, None, None]
    assert facts.collected and facts.node_facts[0]["name"] == "n1"


def test_cluster_facts_are_skipped_on_a_laptop_without_a_context(outside, monkeypatch) -> None:
    seen = _fake_collectors(monkeypatch)
    facts = collect.collect_cluster_facts(context=None)
    assert seen == [] and not facts.collected


def test_hpa_behavior_is_read_in_a_pod(inside, monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake(argv, timeout=None):
        calls.append(argv)
        return {"items": [{"metadata": {"namespace": "ns", "name": "h"},
                           "spec": {"behavior": {"scaleDown": {}}}}]}

    monkeypatch.setattr(hpa_history, "run_json", fake)
    assert "ns/h" in hpa_history.read_behaviors(None)
    assert calls and "--context" not in calls[0]


def test_hpa_behavior_is_not_read_on_a_laptop_without_a_context(outside, monkeypatch) -> None:
    monkeypatch.setattr(hpa_history, "run_json", lambda *a, **k: pytest.fail("kubectl called"))
    assert hpa_history.read_behaviors(None) == {}
