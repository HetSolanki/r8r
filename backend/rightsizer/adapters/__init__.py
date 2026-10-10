"""Adapters: everything that differs between clouds and between node autoscalers.

The rest of rightsizer is written against two interfaces and never names a
cloud or an autoscaler:

* :mod:`.clouds` -- node groups, the instance catalogue, prices, node labels
  (:class:`~.clouds.base.CloudProvider`; AWS built in).
* :mod:`.autoscalers` -- discovery, configuration, node labels, and the
  settings the scale-down replay runs with
  (:class:`~.autoscalers.base.NodeAutoscaler`; Cluster Autoscaler and Karpenter
  built in).

Each snapshot records which it came from (``meta["cloud"]``,
``snapshot.autoscaler``), so everything downstream picks the same adapter.
"""

from __future__ import annotations

import os

_keys: tuple[tuple, tuple[str, ...]] | None = None

#: Labels of this estate's own that name a node's group, comma separated, tried
#: after every adapter's -- for nodes no cloud or autoscaler labels, such as an
#: OKE managed pool beside Karpenter ("example.com/node-pool").
GROUP_LABELS_ENV = "RIGHTSIZER_GROUP_LABELS"


def _group_keys() -> tuple[str, ...]:
    """Node-group label keys of every registered adapter, cached per registry version."""
    global _keys
    from rightsizer.adapters import autoscalers, clouds

    clouds.registry.names(), autoscalers.registry.names()  # plugins first: they bump the version
    extra = tuple(k.strip() for k in os.environ.get(GROUP_LABELS_ENV, "").split(",") if k.strip())
    version = (clouds.registry.version, autoscalers.registry.version, extra)
    if _keys is None or _keys[0] != version:
        found = (*(k for cls in clouds.registry for k in cls.nodegroup_label_keys),
                 *autoscalers.group_label_keys(), *extra)
        _keys = (version, tuple(dict.fromkeys(found)))  # in order, once each
    return _keys[1]


def node_group_label_keys() -> tuple[str, ...]:
    """Every node label that can name a node's group, highest priority first."""
    return _group_keys()


def node_group_of(labels: dict | None) -> str:
    """The group or pool a node belongs to, whichever cloud or autoscaler named it.

    A cloud's managed node group wins over an autoscaler's pool label: on a
    managed group, that is the group the scaling bounds belong to.
    """
    labels = labels or {}
    for key in _group_keys():
        if labels.get(key):
            return labels[key]
    return ""


def group_source(snapshot, config: dict | None) -> str:
    """Who defines a group: "cloud", or the autoscaler adapter that read it.

    Recorded on the group since adapters write it; a snapshot from before then
    is read by what only a Karpenter NodePool carries -- a resource ceiling
    (``limits``) and no cloud identifier -- on a cluster that runs Karpenter.
    """
    from rightsizer.adapters import autoscalers

    config = config or {}
    if config.get("source"):
        return config["source"]
    adapter, _ = autoscalers.for_snapshot(snapshot)
    if config.get("limits") and not config.get("id") and adapter.name.startswith("karpenter"):
        return adapter.name
    return "cloud"


def group_change(snapshot, config: dict | None):
    """The adapter that owns how a group is changed: the autoscaler for a
    NodePool it defines, the cloud for the cloud's own node pool."""
    from rightsizer.adapters import autoscalers, clouds

    source = group_source(snapshot, config)
    return autoscalers.get(source) if source != "cloud" else clouds.for_snapshot(snapshot)


def fill_step(template: str, new_type: str) -> str:
    """A retype_plan template with the type's name and, where it is an OCI
    size, its OCPUs and GB."""
    ocpus = gb = ""
    try:
        from rightsizer.adapters.clouds.oci import shapes

        t = shapes.parse(new_type)
        if t:
            ocpus, gb = shapes._num(t.ocpus), shapes._num(t.memory_gb)
    except ImportError:  # pragma: no cover - the OCI adapter is built in
        pass
    return template.replace("{type}", new_type).replace("{ocpus}", ocpus).replace("{gb}", gb)


def retype_steps(snapshot, config: dict | None, new_type: str) -> list[str]:
    """What it takes for this group to run ``new_type``, in its owner's words."""
    plan = group_change(snapshot, config).retype_plan(config or {})
    if new_type in (plan.get("offered") or []) and plan.get("ifOffered"):
        return [fill_step(plan["ifOffered"], new_type)]
    return [fill_step(plan["template"], new_type)]


def gpu_label_keys() -> tuple[str, ...]:
    """Every registered cloud's GPU node labels."""
    from rightsizer.adapters import clouds

    return tuple(k for cls in clouds.registry for k in cls.gpu_label_keys)
