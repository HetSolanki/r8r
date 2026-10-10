"""Node autoscalers. See :mod:`.base` for what one implements.

Adding one is a module with a :class:`~.base.NodeAutoscaler` subclass and one
line in ``BUILTIN`` below (or an entry point in the ``rightsizer.autoscalers``
group). Order matters: discovery tries them in this order.
"""

from __future__ import annotations

import logging

from rightsizer.adapters.autoscalers.base import AutoscalerConfig, NodeAutoscaler  # noqa: F401
from rightsizer.adapters.registry import Registry

log = logging.getLogger(__name__)

#: What a snapshot that recorded no autoscaler is assumed to run.
DEFAULT = "cluster-autoscaler"

BUILTIN = {
    "cluster-autoscaler": "rightsizer.adapters.autoscalers.cluster_autoscaler:ClusterAutoscaler",
    "karpenter-oci": "rightsizer.adapters.autoscalers.karpenter_oci:KarpenterOCI",
    "karpenter": "rightsizer.adapters.autoscalers.karpenter:Karpenter",
}

registry: Registry[NodeAutoscaler] = Registry("autoscaler", "rightsizer.autoscalers", BUILTIN)


def get(name: str | None = None) -> NodeAutoscaler:
    return registry.get(name or DEFAULT)()


def discover(*, context: str | None, namespace: str | None = None,
             only: str | None = None) -> AutoscalerConfig | None:
    """The autoscaler running in the cluster: ``only`` that one, else the first found.

    A cluster running more than one (Cluster Autoscaler for some groups,
    Karpenter for the rest) is reported; the first in registry order is used.
    """
    names = [only] if only else registry.names()
    found = [c for c in (get(n).discover(context=context, namespace=namespace) for n in names) if c]
    # Karpenter for OCI is also found as plain Karpenter; the specific one stays.
    superseded = {s for c in found for s in registry.get(c.kind).supersedes}
    found = [c for c in found if c.kind not in superseded]
    if len(found) > 1:
        log.warning("more than one node autoscaler found (%s); modelling %s",
                    ", ".join(c.kind for c in found), found[0].kind)
    return found[0] if found else None


def for_snapshot(snapshot) -> tuple[NodeAutoscaler, AutoscalerConfig | None]:
    """The adapter and configuration a snapshot recorded.

    Snapshots from before autoscalers were recorded carry Cluster Autoscaler's
    flags in ``snapshot.ca``; those still read as Cluster Autoscaler.
    """
    config = AutoscalerConfig.from_dict(getattr(snapshot, "autoscaler", None))
    if config is None and (snapshot.ca or {}).get("args"):
        config = AutoscalerConfig(kind=DEFAULT, source=snapshot.ca.get("source", "snapshot"),
                                  settings={"args": dict(snapshot.ca["args"])})
    return get(config.kind if config else DEFAULT), config


def scale_down_settings(snapshot):
    """The settings the engine's scale-down replay runs with for this snapshot."""
    adapter, config = for_snapshot(snapshot)
    return adapter.scale_down_settings(config)


def group_label_keys() -> tuple[str, ...]:
    return tuple(k for cls in registry for k in cls.group_label_keys)
