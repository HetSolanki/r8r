"""Oracle Cloud Infrastructure: OKE node pools, shapes, and list prices.

:mod:`.shapes` turns a shape and its size into an instance type
(``VM.Standard.E5.Flex:4ocpu-32gb``), :mod:`.pricing` prices it from Oracle's
public price list, :mod:`.oke` reads the cluster's managed node pools with the
``oci`` CLI through the read-only allowlist.
"""

from __future__ import annotations

from rightsizer.adapters.clouds.base import CloudProvider, InstanceType, NodeGroupConfig
from rightsizer.adapters.clouds.oci import oke, pricing, shapes


def _instance(t: shapes.OciType) -> InstanceType:
    s = shapes.SHAPES[t.shape]
    alloc_cpu, alloc_mem = shapes.allocatable(t)
    return InstanceType(
        name=t.name, vcpu=t.vcpu, memory_gib=t.memory_gb,
        alloc_vcpu=alloc_cpu, alloc_mem_gib=alloc_mem,
        on_demand_hourly=pricing.hourly(s.series, ocpus=t.ocpus, memory_gb=t.memory_gb,
                                        gpus=s.gpus, preemptible=t.preemptible),
        # Standard and GPU VMs boot from block volumes: no instance store, and
        # that is known rather than undescribed.
        storage_known=True,
        root_volume="boot volume",
    )


class OCIProvider(CloudProvider):
    name = "oci"
    title = "Oracle Cloud Infrastructure"
    cli = "oci"
    profile_env = "OCI_CLI_PROFILE"
    default_region = "us-ashburn-1"
    # OKE's managed pools carry no label naming the pool that is documented;
    # nodes Karpenter launches carry karpenter.sh/nodepool (the autoscaler's
    # label). $RIGHTSIZER_GROUP_LABELS names an estate's own for the rest.
    nodegroup_label_keys = ()
    # Not oci.oraclecloud.com/gpu-shape: it can be present as "false" on CPU
    # nodes, and the GPU check looks only for a label's presence. A GPU node
    # is found by its nvidia.com/gpu capacity instead.
    gpu_label_keys = ()

    @classmethod
    def matches_context(cls, context: str | None) -> bool:
        # `oci ce cluster create-kubeconfig` names contexts "context-<id>", which
        # says nothing about the cloud; the nodes do.
        return False

    @classmethod
    def from_kubeconfig(cls, exec_spec: dict) -> dict | None:
        """``oci ce cluster generate-token --cluster-id <ocid> --region <r>``,
        as `oci ce cluster create-kubeconfig` writes it."""
        args = list(exec_spec.get("args") or [])
        if not (str(exec_spec.get("command", "")).rsplit("/", 1)[-1] == "oci" and "generate-token" in args):
            return None
        env = {e.get("name"): e.get("value") for e in exec_spec.get("env") or []}
        return {"cluster": cls._flag(args, "--cluster-id"), "region": cls._flag(args, "--region"),
                "profile": cls._flag(args, "--profile") or env.get(cls.profile_env)}

    @classmethod
    def matches_labels(cls, labels: dict) -> bool:
        return any(k.startswith(("oci.oraclecloud.com/", "oke.oraclecloud.com/")) for k in labels)

    def collect_nodegroups(self, cluster: str) -> list[NodeGroupConfig]:
        return oke.collect_nodegroups(cluster, profile=self.profile, region=self.region)

    # An OKE pool's shape config is a property of the pool, not of each node:
    # it is updated in place, and the nodes are then cycled onto it.
    def retype_plan(self, config: dict) -> dict:
        return {"offered": [], "ifOffered": "",
                "template": "update the node pool's shape config to {ocpus} OCPU / {gb} GB "
                            "({type}) and cycle its nodes",
                "surface": "OKE node pool"}

    def scale_in_command(self, cluster: str, config: dict, nodes: int) -> str:
        return (f"oci ce node-pool update --node-pool-id {config.get('id') or '<node-pool-ocid>'} "
                f"--size {nodes}")

    def retype_command(self, cluster: str, config: dict, new_type: str, nodes: int) -> str:
        t = shapes.parse(new_type)
        cfg = (f"{{\"ocpus\": {shapes._num(t.ocpus)}, \"memoryInGBs\": {shapes._num(t.memory_gb)}}}"
               if t else "{...}")
        pool = config.get("id") or "<node-pool-ocid>"
        return (f"oci ce node-pool update --node-pool-id {pool} --node-shape-config '{cfg}' --size {nodes}\n"
                "# then cycle the nodes onto the new config (node pool cycling, or one node at a time)")

    def describe_instance_types(self, names: list[str]) -> dict[str, InstanceType]:
        return {n: _instance(t) for n in names if (t := shapes.parse(n))}

    def on_demand_price(self, instance_type: str, region: str | None = None) -> float:
        t = shapes.parse(instance_type)
        return _instance(t).on_demand_hourly if t else 0.0

    def family_ladder(self, family: str) -> list[InstanceType]:
        return [_instance(t) for t in shapes.ladder(family)]

    def candidate_ladder(self, current_type: str) -> list[InstanceType]:
        """Every size of the same shape, at 4, 8 and 16 GB per OCPU, and the
        current size whatever it is. Same shape: a different shape is a
        different processor, and a GPU shape stays a GPU shape."""
        t = shapes.parse(current_type)
        if t is None:
            return []
        sizes = {x.name: x for x in [*shapes.ladder(t.shape, t.preemptible), t]}
        return sorted((_instance(x) for x in sizes.values()), key=lambda i: (i.vcpu, i.memory_gib))
