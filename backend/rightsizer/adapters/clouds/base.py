"""What rightsizer needs from a cloud, and nothing more.

A cloud adapter answers four questions the cluster cannot:

* **Node groups** -- each group's configured min/max/desired size and instance
  types. A group pinned at its minimum cannot scale in, whatever the usage says.
* **Scaling groups** -- the cloud's own autoscaling groups behind those node
  groups, with the options an autoscaler reads off them (optional).
* **The instance catalogue** -- vCPU, memory, local disk and network for an
  instance type, and the sizes worth simulating instead of today's.
* **Prices** -- on-demand list price per hour, before any discount.

It also names the node labels that cloud's managed node groups carry, so the
engine can tell which group a node belongs to without knowing which cloud it
is in.

Everything is read-only. A provider runs its cloud's CLI through
:func:`rightsizer.sources.shell.run_json`, which refuses any command not
allowlisted there -- adding a cloud means adding its CLI's read-only rules to
that one file, deliberately, so the guarantee stays reviewable in one place.

To add a cloud: subclass :class:`CloudProvider`, implement the abstract
methods, and register it -- one line in :mod:`rightsizer.adapters.clouds`, or
an entry point in the ``rightsizer.clouds`` group from another package.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar

HOURS_PER_MONTH = 730.0


@dataclass(frozen=True)
class InstanceType:
    """One machine shape, as every cloud describes it."""

    name: str
    vcpu: int
    memory_gib: float
    on_demand_hourly: float = 0.0
    # Instance store. `storage_known` separates "has none" from "was never
    # described" -- a cache written before storage was recorded must not read
    # as network-disk-only.
    storage_gb: float = 0.0
    storage_disks: int = 0
    storage_type: str = ""      # "ssd" | "hdd"
    storage_nvme: str = ""      # "required" | "supported" | "unsupported"
    storage_known: bool = False
    # Network: the bandwidth a node keeps up indefinitely, and the ceiling it
    # bursts to while it has credits (equal for types without burst).
    # 0.0 = not described.
    net_baseline_gbps: float = 0.0
    net_peak_gbps: float = 0.0
    net_performance: str = ""       # the cloud's own label, e.g. "Up to 12.5 Gigabit"
    #: What a node without instance storage keeps everything on, in the cloud's
    #: own words (AWS: EBS; OCI: boot volume).
    root_volume: str = "EBS"
    #: What a node of this type offers pods before DaemonSets, when the cloud
    #: can say (OCI: Karpenter's own formula). 0 = not known; the engine then
    #: takes the running node's overhead as every candidate's.
    alloc_vcpu: float = 0.0
    alloc_mem_gib: float = 0.0

    @property
    def family(self) -> str:
        return self.name.split(".", 1)[0]

    def monthly(self, discount: float) -> float:
        return self.on_demand_hourly * (1 - discount) * HOURS_PER_MONTH


@dataclass
class NodeGroupConfig:
    """A managed node group's scaling bounds, as the cloud configures them."""

    name: str
    min_size: int = 0
    max_size: int = 0
    desired_size: int = 0
    instance_types: list[str] = field(default_factory=list)
    capacity_type: str = ""
    ami_type: str = ""
    taints: list[str] = field(default_factory=list)
    #: A ceiling on what the group may hold rather than a node count -- a
    #: Karpenter NodePool's spec.limits ({"cpu": "80", "memory": "320Gi"}).
    limits: dict[str, str] = field(default_factory=dict)
    #: The cloud's identifier, where its CLI wants one (an OKE node pool's OCID).
    id: str = ""
    #: Who defines the group: "cloud" for the cloud's own node pool, else the
    #: autoscaler adapter that read it (a Karpenter NodePool). That adapter then
    #: says how the group is changed.
    source: str = "cloud"

    @property
    def at_min(self) -> bool:
        return self.desired_size <= self.min_size


class CloudProvider(ABC):
    """One cloud, bound to one region and one set of credentials."""

    #: Registry key, recorded in each snapshot's ``meta["cloud"]``.
    name: ClassVar[str]
    title: ClassVar[str]
    #: The CLI this provider runs. It must be allowlisted in ``sources/shell.py``.
    cli: ClassVar[str]
    default_region: ClassVar[str] = ""
    #: Labels the cloud's managed node groups put on their nodes, naming the group.
    nodegroup_label_keys: ClassVar[tuple[str, ...]] = ()
    #: Labels that mark a node as carrying a GPU (Cluster Autoscaler's GPULabel()).
    gpu_label_keys: ClassVar[tuple[str, ...]] = ()

    def __init__(self, *, region: str | None = None, profile: str | None = None,
                 account: str | None = None) -> None:
        self.region = region or self.default_region
        self.profile = profile
        self.account = account

    # -- recognising this cloud ----------------------------------------------

    @classmethod
    def matches_context(cls, context: str | None) -> bool:
        """Whether a kubeconfig context name says the cluster runs here."""
        return False

    @classmethod
    def from_kubeconfig(cls, exec_spec: dict) -> dict | None:
        """What a kubeconfig's token command says about the cluster, or None if
        the command is not this cloud's.

        ``exec_spec`` is the context user's ``exec`` stanza ({command, args,
        env}). Returns any of {"cluster", "region", "profile"} it names, so a
        context is enough and the same values are not typed twice.
        """
        return None

    @staticmethod
    def _flag(args: list[str], *names: str) -> str | None:
        """The value of ``--name value`` or ``--name=value`` in an argv."""
        for i, a in enumerate(args):
            for n in names:
                if a == n and i + 1 < len(args):
                    return args[i + 1]
                if a.startswith(n + "="):
                    return a.split("=", 1)[1]
        return None

    @classmethod
    def matches_labels(cls, labels: dict) -> bool:
        """Whether a node's labels say it runs here."""
        return any(k in labels for k in cls.nodegroup_label_keys)

    #: Environment variable that names the credentials profile for this cloud's
    #: CLI -- and for kubectl, whose kubeconfig exec plugin runs that CLI.
    profile_env: ClassVar[str] = ""

    def credential_env(self) -> dict[str, str]:
        """What to set for a kubectl call so its token plugin uses our profile."""
        return {self.profile_env: self.profile} if self.profile_env and self.profile else {}

    # -- node groups -----------------------------------------------------------

    @abstractmethod
    def collect_nodegroups(self, cluster: str) -> list[NodeGroupConfig]:
        """Every managed node group of ``cluster``, with its scaling bounds."""

    def collect_scaling_groups(self, cluster: str) -> list[dict]:
        """The autoscaling groups an autoscaler discovers for ``cluster``.

        Each is ``{"name", "min", "max", "desired", "nodegroup", "instances",
        "options"}``. A cloud without such a layer returns nothing.
        """
        return []

    # -- the instance catalogue and prices -----------------------------------

    @abstractmethod
    def describe_instance_types(self, names: list[str]) -> dict[str, InstanceType]:
        """Specs for each type the cloud knows; unknown names are left out."""

    @abstractmethod
    def on_demand_price(self, instance_type: str, region: str | None = None) -> float:
        """List on-demand price per hour in ``region`` (default: the bound one)."""

    @abstractmethod
    def family_ladder(self, family: str) -> list[InstanceType]:
        """Same-family sizes, smallest first."""

    def candidate_ladder(self, current_type: str) -> list[InstanceType]:
        """Every type worth simulating for a group running ``current_type``.

        The default stays in the current family; a cloud overrides it to offer
        sibling families that keep the same generation and local-disk shape.
        """
        family = current_type.partition(".")[0]
        return self.family_ladder(family) if family else []

    # -- changing a group -------------------------------------------------------
    # The cloud's own node pools: how a count or a type is changed, in the
    # cloud's words. A group an autoscaler defines asks that adapter instead
    # (see rightsizer.adapters.group_change).

    def retype_plan(self, config: dict) -> dict:
        """How this group reaches a different instance type.

        ``offered``: types the group may already run without any change (a
        Karpenter pool's listed sizes); ``ifOffered``: the step when the type
        is one of them; ``template``: the step otherwise, with ``{type}``,
        ``{ocpus}`` and ``{gb}`` filled in; ``surface``: where the change is
        made. The console fills the same template for a type the reader picks.
        """
        return {"offered": [], "ifOffered": "",
                "template": "change the node group's instance type to {type}",
                "surface": "node group"}

    def scale_in_command(self, cluster: str, config: dict, nodes: int) -> str:
        return f"# set {config.get('name')} to {nodes} node(s)"

    def retype_command(self, cluster: str, config: dict, new_type: str, nodes: int) -> str:
        return f"# move {config.get('name')} to {nodes} x {new_type}"

    def known_types(self) -> list[str]:
        """Types already described and priced for the bound region, with no API call.

        The console offers these beyond each group's ladder. Default: none.
        """
        return []

    def monthly_price(self, instance_type: str, discount: float, region: str | None = None) -> float:
        return self.on_demand_price(instance_type, region) * (1 - discount) * HOURS_PER_MONTH

    def __repr__(self) -> str:
        return f"{type(self).__name__}(region={self.region!r}, profile={self.profile!r})"
