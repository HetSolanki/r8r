"""Cloud providers. See :mod:`.base` for what one implements.

Adding a cloud is a module with a :class:`~.base.CloudProvider` subclass, one
line in ``BUILTIN`` below (or an entry point in the ``rightsizer.clouds``
group), and its CLI's read-only rules in ``sources/shell.py``.
"""

from __future__ import annotations

from rightsizer.adapters.clouds.base import (  # noqa: F401 - the public surface
    HOURS_PER_MONTH,
    CloudProvider,
    InstanceType,
    NodeGroupConfig,
)
from rightsizer.adapters.registry import Registry

#: Every snapshot collected before clouds were recorded came from here.
DEFAULT = "aws"

BUILTIN = {
    "aws": "rightsizer.adapters.clouds.aws:AWSProvider",
    "oci": "rightsizer.adapters.clouds.oci:OCIProvider",
}

registry: Registry[CloudProvider] = Registry("cloud", "rightsizer.clouds", BUILTIN)


def get(name: str | None = None, *, region: str | None = None, profile: str | None = None,
        account: str | None = None) -> CloudProvider:
    """A provider for ``name`` (default: AWS), bound to a region and credentials."""
    return registry.get(name or DEFAULT)(region=region, profile=profile, account=account)


def detect(*, context: str | None = None, node_labels: list[dict] | None = None) -> str | None:
    """Which cloud a cluster runs in, from its context name or its nodes' labels."""
    for cls in registry:
        if cls.matches_context(context):
            return cls.name
    for labels in node_labels or []:
        for cls in registry:
            if cls.matches_labels(labels):
                return cls.name
    return None


def from_kubeconfig(exec_spec: dict | None) -> tuple[str, dict] | None:
    """(cloud, {cluster, region, profile}) from a context's token command."""
    for cls in registry:
        info = cls.from_kubeconfig(exec_spec or {})
        if info is not None:
            return cls.name, {k: v for k, v in info.items() if v}
    return None


def ladder_for(snapshot, config: dict | None, current_type: str, *,
               region: str | None = None, profile: str | None = None) -> list[InstanceType]:
    """Every type worth packing a group onto: the cloud's ladder around the
    current type, plus whatever the group itself already lists (a Karpenter
    pool's other sizes), so an option Karpenter can take by itself is in the
    search and is named as such."""
    provider = for_snapshot(snapshot, region=region, profile=profile)
    ladder = {i.name: i for i in provider.candidate_ladder(current_type)}
    listed = [t for t in (config or {}).get("instance_types") or [] if t not in ladder]
    for name, inst in provider.describe_instance_types(listed).items():
        price = inst.on_demand_hourly or provider.on_demand_price(name)
        ladder[name] = inst if price == inst.on_demand_hourly else inst.__class__(
            **{**inst.__dict__, "on_demand_hourly": price})
    return sorted(ladder.values(), key=lambda i: (i.vcpu, i.memory_gib, i.name))


def for_snapshot(snapshot, *, region: str | None = None, profile: str | None = None) -> CloudProvider:
    """The provider a snapshot was collected from, bound to where it lives.

    ``region``/``profile`` override what the snapshot recorded (the console
    export resolves them from clusters.json first).
    """
    meta = getattr(snapshot, "meta", None) or {}
    name = meta.get("cloud") or snapshot.__dict__.get("_cloud_detected")
    if not name:
        name = detect(context=meta.get("context"),
                      node_labels=[n.get("labels") or {} for n in (snapshot.facts.node_facts or [])]
                      ) or DEFAULT
        snapshot.__dict__["_cloud_detected"] = name
    return get(name, region=region or meta.get("region"), profile=profile or meta.get("profile"),
               account=meta.get("account"))
