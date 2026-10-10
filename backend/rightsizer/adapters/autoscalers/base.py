"""What rightsizer needs from a node autoscaler.

A saving is real only when the autoscaler actually removes a node, so the
autoscaler's rules decide every figure. An adapter answers:

* **Is it running, and how is it configured?** -- :meth:`NodeAutoscaler.discover`
  reads that from the cluster, read-only.
* **Which group does a node belong to?** -- the labels it puts on its nodes.
* **What would it remove?** -- the engine replays Cluster Autoscaler v1.35's
  scale-down (``engine/autoscaler.py``, ``engine/careplay.py``).
  :meth:`NodeAutoscaler.scale_down_settings` gives the settings that replay runs
  with. An autoscaler the engine does not replay natively
  (``models_scale_down = False``) maps its own settings onto the nearest ones
  and says, in :meth:`NodeAutoscaler.notes`, that the figures are an
  approximation -- never silently.

To add one: subclass :class:`NodeAutoscaler`, and register it in
:mod:`rightsizer.adapters.autoscalers` or with an entry point in the
``rightsizer.autoscalers`` group. A native replay for it is the
``scale_down_settings`` seam's successor: until one exists, say so.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from rightsizer.adapters.clouds.base import NodeGroupConfig
    from rightsizer.core.models import ClusterFacts
    from rightsizer.engine.autoscaler import CAConfig


@dataclass
class AutoscalerConfig:
    """What was found running, as stored in ``snapshot.autoscaler``."""

    kind: str
    #: Where the settings came from: "live deployment x", "--ca-arg flags", ...
    source: str
    settings: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict | None) -> AutoscalerConfig | None:
        if not raw or not raw.get("kind"):
            return None
        return cls(kind=raw["kind"], source=raw.get("source", "snapshot"),
                   settings=dict(raw.get("settings") or {}))


class NodeAutoscaler(ABC):
    #: Registry key, recorded in ``snapshot.autoscaler["kind"]``.
    name: ClassVar[str]
    title: ClassVar[str]
    #: True only when the engine replays this autoscaler's own decisions.
    models_scale_down: ClassVar[bool] = False
    #: Labels it puts on the nodes it manages, naming their group or pool.
    group_label_keys: ClassVar[tuple[str, ...]] = ()
    #: Adapters this one is a more specific form of: found together, this wins
    #: (Karpenter for OCI is Karpenter, with OCI's node classes).
    supersedes: ClassVar[tuple[str, ...]] = ()

    @abstractmethod
    def discover(self, *, context: str | None, namespace: str | None = None) -> AutoscalerConfig | None:
        """Its configuration as running in the cluster, or None if it is not there."""

    def from_flags(self, pairs: list[str]) -> AutoscalerConfig:
        """Settings given on the command line instead of read from the cluster."""
        raise NotImplementedError(f"{self.title} settings cannot be given as flags")

    @abstractmethod
    def scale_down_settings(self, config: AutoscalerConfig | None) -> CAConfig:
        """The settings the engine's scale-down replay runs with for this cluster."""

    def nodegroup_configs(self, config: AutoscalerConfig | None, facts: ClusterFacts,
                          warnings: list[str]) -> list[NodeGroupConfig]:
        """Groups this autoscaler defines that no cloud node pool describes.

        Cluster Autoscaler scales the cloud's own groups, so it adds none. A
        Karpenter NodePool is a group with no cloud resource behind it: its
        sizes and ceiling are in the cluster, and only this adapter can read
        them. ``facts.node_facts`` says what is running; anything a reader
        should know about the result goes in ``warnings``.
        """
        return []

    def retype_plan(self, config: dict) -> dict:
        """How a group this autoscaler defines reaches a different instance
        type; same shape as :meth:`CloudProvider.retype_plan`."""
        raise NotImplementedError

    def scale_in_command(self, cluster: str, config: dict, nodes: int) -> str:
        raise NotImplementedError

    def retype_command(self, cluster: str, config: dict, new_type: str, nodes: int) -> str:
        raise NotImplementedError

    def pool(self, config: dict, snapshot) -> dict | None:
        """For a group this autoscaler sizes itself (a Karpenter NodePool):
        what the engine's Karpenter replay needs -- {"policy", "spot",
        "limit_vcpu", "undisruptable"}. None for a group the cloud sizes."""
        return None

    def node_label_for(self, type_name: str) -> str:
        """The instance-type label Karpenter writes on a node of this type."""
        return type_name

    def node_type_name(self, labels: dict) -> str | None:
        """The instance-type name (as this tool names types) a node's labels
        say it is; None when the labels do not say."""
        return labels.get("node.kubernetes.io/instance-type") or None

    def drain_veto(self, workload: dict) -> str | None:
        """Why this autoscaler will not drain a node running ``workload``, beyond
        the Cluster Autoscaler rules the engine applies itself; None if it will.

        ``workload`` is one of ``snapshot.facts.workload_constraints``.
        """
        return None

    def manages(self, config: AutoscalerConfig | None, group: str) -> bool:
        """Whether this autoscaler scales ``group`` at all. Cluster Autoscaler is
        taken to scale every node group, as it is deployed in practice."""
        return True

    def notes(self, config: AutoscalerConfig | None) -> list[str]:
        """What a reader must know before trusting the figures."""
        return []

    def summary(self, config: AutoscalerConfig | None) -> str | None:
        """How this autoscaler was found to be configured -- context, not a caveat."""
        return None
