"""Amazon Web Services: EKS managed node groups, EC2 instance types and prices.

:mod:`.eks` reads node groups and their Auto Scaling groups, :mod:`.ec2` the
instance catalogue and the Pricing API. Both run the ``aws`` CLI through the
read-only allowlist in :mod:`rightsizer.sources.shell`.
"""

from __future__ import annotations

import re

from rightsizer.adapters.clouds.aws import ec2, eks
from rightsizer.adapters.clouds.base import CloudProvider, InstanceType, NodeGroupConfig

#: An EKS kubeconfig context is the cluster's ARN, in any partition:
#: arn:aws (commercial), arn:aws-us-gov (GovCloud), arn:aws-cn.
_EKS_ARN = re.compile(r"^arn:aws[\w-]*:eks:")


class AWSProvider(CloudProvider):
    name = "aws"
    title = "Amazon Web Services"
    cli = "aws"
    profile_env = "AWS_PROFILE"
    default_region = "us-east-1"
    nodegroup_label_keys = ("eks.amazonaws.com/nodegroup",)
    gpu_label_keys = ("k8s.amazonaws.com/accelerator",)

    @classmethod
    def matches_context(cls, context: str | None) -> bool:
        return bool(context and _EKS_ARN.match(context))

    @classmethod
    def from_kubeconfig(cls, exec_spec: dict) -> dict | None:
        """``aws eks get-token --cluster-name <n> --region <r>``, as
        `aws eks update-kubeconfig` writes it."""
        args = list(exec_spec.get("args") or [])
        if not (str(exec_spec.get("command", "")).rsplit("/", 1)[-1] == "aws" and "get-token" in args):
            return None
        env = {e.get("name"): e.get("value") for e in exec_spec.get("env") or []}
        return {"cluster": cls._flag(args, "--cluster-name"), "region": cls._flag(args, "--region"),
                "profile": cls._flag(args, "--profile") or env.get(cls.profile_env)}

    def retype_plan(self, config: dict) -> dict:
        return {"offered": [], "ifOffered": "",
                "template": "replace the node group (instance type is immutable)",
                "surface": "Terraform/ASG"}

    def scale_in_command(self, cluster: str, config: dict, nodes: int) -> str:
        return (f"aws eks update-nodegroup-config --cluster-name {cluster} \\\n"
                f"  --nodegroup-name {config.get('name')} \\\n"
                f"  --scaling-config minSize={min(int(config.get('min_size', 0)), nodes)},"
                f"maxSize={config.get('max_size', '?')},desiredSize={nodes}")

    def retype_command(self, cluster: str, config: dict, new_type: str, nodes: int) -> str:
        name = config.get("name")
        return ("# node group instance type is immutable: replace the group\n"
                f"eksctl create nodegroup --cluster {cluster} --name {name}-v2 \\\n"
                f"  --node-type {new_type} --nodes {nodes} \\\n"
                f"  --nodes-min {min(int(config.get('min_size', 0)), nodes)} "
                f"--nodes-max {config.get('max_size', 0)}\n"
                f"# then drain and delete {name}")

    def collect_nodegroups(self, cluster: str) -> list[NodeGroupConfig]:
        return eks.collect_nodegroups(cluster, profile=self.profile, region=self.region)

    def collect_scaling_groups(self, cluster: str) -> list[dict]:
        return eks.collect_scaling_groups(cluster, profile=self.profile, region=self.region)

    def describe_instance_types(self, names: list[str]) -> dict[str, InstanceType]:
        return ec2.describe_instance_types(names, profile=self.profile, region=self.region)

    def on_demand_price(self, instance_type: str, region: str | None = None) -> float:
        return ec2.fetch_on_demand_price(instance_type, region or self.region, profile=self.profile)

    def known_types(self) -> list[str]:
        cache = ec2._load_cache()
        return [n for n, c in sorted(cache.items()) if "vcpu" in c and c.get(f"price_{self.region}")]

    def family_ladder(self, family: str) -> list[InstanceType]:
        return ec2.family_ladder(family, profile=self.profile, region=self.region)

    def candidate_ladder(self, current_type: str) -> list[InstanceType]:
        # m/r/c siblings of the same generation and disk suffix: see ec2.candidate_ladder.
        return ec2.candidate_ladder(current_type, profile=self.profile, region=self.region)
