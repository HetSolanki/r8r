"""Karpenter: found, configured, and replayed per NodePool.

Karpenter does not scale node groups; it provisions nodes per NodePool and
removes them by *consolidation*: deleting or replacing any node whose pods fit
elsewhere more cheaply, after ``consolidateAfter``, within the pool's
disruption budgets. There is no utilisation threshold and no fixed set of
instance types per group.

Each NodePool whose nodes and pods were read is replayed with Karpenter's own
provisioning and consolidation (:mod:`rightsizer.engine.karpenter`). Groups
that replay cannot cover -- a node group outside every NodePool, such as an
OKE managed pool beside Karpenter, or a cluster whose pods were not read -- fall
back to Cluster Autoscaler's rules set to the nearest equivalents
(:func:`_nearest_ca_args`), and every output that carries those figures says
they are an approximation.
"""

from __future__ import annotations

import logging

from rightsizer.adapters.autoscalers.base import AutoscalerConfig, NodeAutoscaler
from rightsizer.engine.autoscaler import DEFAULTS, CAConfig, duration_seconds

log = logging.getLogger(__name__)

#: v1 and v1beta1 NodePools, then the v1alpha5 Provisioner they replaced.
_RESOURCES = ("nodepools.karpenter.sh", "provisioners.karpenter.sh")
#: Consolidation that removes busy-but-underused nodes, not only empty ones.
_UNDERUTILIZED = ("WhenEmptyOrUnderutilized", "WhenUnderutilized")


def _pool(item: dict) -> dict:
    spec = item.get("spec") or {}
    disruption = spec.get("disruption") or {}
    # v1alpha5 Provisioners: consolidation.enabled / ttlSecondsAfterEmpty.
    if "consolidation" in spec or "ttlSecondsAfterEmpty" in spec:
        enabled = bool((spec.get("consolidation") or {}).get("enabled"))
        disruption = {"consolidationPolicy": "WhenUnderutilized" if enabled else "WhenEmpty",
                      "consolidateAfter": f"{spec.get('ttlSecondsAfterEmpty', 0)}s"}
    template = ((spec.get("template") or {}).get("spec") or {})
    requirements = template.get("requirements") or spec.get("requirements") or []

    def values(key: str) -> list[str]:
        return sorted({v for r in requirements if r.get("key") == key and r.get("operator") == "In"
                       for v in r.get("values") or []})
    types = values("node.kubernetes.io/instance-type")
    return {
        "name": (item.get("metadata") or {}).get("name", ""),
        "consolidationPolicy": disruption.get("consolidationPolicy", "WhenEmptyOrUnderutilized"),
        "consolidateAfter": disruption.get("consolidateAfter", "0s"),
        "budgets": disruption.get("budgets") or [],
        "limits": spec.get("limits") or {},
        "instanceTypes": types,
        "capacityTypes": values("karpenter.sh/capacity-type"),
        "nodeClass": (template.get("nodeClassRef") or {}).get("name", ""),
        "requirements": requirements,
        "weight": spec.get("weight"),
    }


def _nearest_ca_args(pools: list[dict]) -> dict[str, object]:
    """Cluster Autoscaler flags that behave most like these NodePools.

    * Consolidating underused nodes has no threshold: any node whose pods fit
      elsewhere is a candidate, which is a threshold of 1.0. Only-when-empty is
      the opposite: a node goes only with nothing on it.
    * ``consolidateAfter`` plays the part of ``scale-down-unneeded-time``.
    * Karpenter drains nodes with local storage and kube-system pods, honouring
      PDBs, so neither blocks scale-down as it does under CA's defaults.

    Several pools: the most aggressive policy and the shortest wait, so the
    approximation never claims less than the cluster would do.
    """
    args = dict(DEFAULTS)
    if not pools:
        return args
    underused = any(p["consolidationPolicy"] in _UNDERUTILIZED for p in pools)
    args["scale-down-utilization-threshold"] = 1.0 if underused else 0.001
    wait = min(duration_seconds(p["consolidateAfter"]) for p in pools)
    args["scale-down-unneeded-time"] = f"{int(wait)}s"
    args["skip-nodes-with-local-storage"] = False
    args["skip-nodes-with-system-pods"] = False
    return args


class Karpenter(NodeAutoscaler):
    name = "karpenter"
    title = "Karpenter"
    models_scale_down = False
    group_label_keys = ("karpenter.sh/nodepool", "karpenter.sh/provisioner-name")

    def discover(self, *, context: str | None, namespace: str | None = None) -> AutoscalerConfig | None:
        from rightsizer.sources.shell import run_json

        for resource in _RESOURCES:
            argv = ["kubectl"] + (["--context", context] if context else []) + [
                "get", resource, "-o", "json", "--request-timeout=45s"]
            try:
                items = run_json(argv, timeout=60).get("items", [])  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001 - not installed is the common case
                log.debug("no %s: %s", resource, exc)
                continue
            if items:
                pools = [_pool(i) for i in items]
                return AutoscalerConfig(kind=self.name, source=f"{len(pools)} {resource}",
                                        settings={"nodepools": pools})
        return None

    def pool(self, config: dict, snapshot) -> dict | None:
        found = (getattr(snapshot, "autoscaler", None) or {}).get("settings") or {}
        entry = next((p for p in found.get("nodepools") or [] if p.get("name") == config.get("name")), None)
        if entry is None:
            return None
        limits = entry.get("limits") or {}
        try:
            from rightsizer.sources.kfacts import quantity
            limit_vcpu = quantity(limits["cpu"]) if limits.get("cpu") else None
            limit_mem = int(quantity(limits["memory"])) if limits.get("memory") else None
        except Exception:  # noqa: BLE001
            limit_vcpu = limit_mem = None
        # The API defaults an absent budget list to one of 10%. A budget with a
        # schedule is out of its window most of the day; the steady state is
        # set by the ones without.
        budgets = tuple(str(b.get("nodes")) for b in entry.get("budgets") or []
                        if b.get("nodes") is not None and not b.get("schedule")) or ("10%",)
        pinned = frozenset(f"{w.get('namespace')}/{w.get('name')}/{w.get('kind')}"
                           for w in (snapshot.facts.workload_constraints or []) if w.get("do_not_disrupt"))
        return {"policy": entry.get("consolidationPolicy") or "WhenEmptyOrUnderutilized",
                "spot": entry.get("capacityTypes") == ["spot"],
                "limit_vcpu": limit_vcpu, "limit_mem": limit_mem, "budgets": budgets,
                "undisruptable": pinned}

    def drain_veto(self, workload: dict) -> str | None:
        if workload.get("do_not_disrupt"):
            return (f"{workload.get('namespace')}/{workload.get('name')}/{workload.get('kind')} "
                    "is annotated karpenter.sh/do-not-disrupt")
        return None

    def scale_down_settings(self, config: AutoscalerConfig | None) -> CAConfig:
        pools = (config.settings if config else {}).get("nodepools") or []
        return CAConfig(source=f"approximated from Karpenter ({len(pools)} NodePool(s))",
                        args=_nearest_ca_args(pools))

    def manages(self, config: AutoscalerConfig | None, group: str) -> bool:
        # Karpenter scales its own NodePools and nothing else: a group beside
        # them (an OKE managed pool) keeps whatever size someone gave it. With no
        # NodePools read there is nothing to tell them apart, so every group is
        # taken as Karpenter's and approximated, as before.
        pools = (config.settings if config else {}).get("nodepools") or []
        return not pools or any(p.get("name") == group for p in pools)

    def notes(self, config: AutoscalerConfig | None) -> list[str]:
        if (config.settings if config else {}).get("nodepools"):
            return []
        return ["Karpenter manages this cluster, but no NodePools were read: scale-down "
                "figures use Cluster Autoscaler's general rules and are an approximation."]

    def summary(self, config: AutoscalerConfig | None) -> str | None:
        pools = (config.settings if config else {}).get("nodepools") or []
        if not pools:
            return None
        from collections import Counter

        policies = Counter(f"{p['consolidationPolicy']} after {p['consolidateAfter']}" for p in pools)
        described = ", ".join(f"{n} {k}" if len(policies) > 1 else k for k, n in policies.most_common())
        return (f"Karpenter manages this cluster ({len(pools)} NodePools, {described}). "
                "Each pool is replayed through Karpenter's own provisioning and consolidation.")
