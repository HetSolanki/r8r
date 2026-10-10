"""EKS: managed node groups and the Auto Scaling groups behind them.

One ``list-nodegroups`` plus one ``describe-nodegroup`` each -- the smallest set
of calls that answers the question Prometheus cannot: **is this group already at
its configured minimum?** A group pinned at ``minSize`` cannot scale in no matter
what the usage says, and recommending against it produces a saving that never
arrives.

All calls go through the read-only allowlist in :mod:`rightsizer.sources.shell`.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from rightsizer.adapters.clouds.base import NodeGroupConfig
from rightsizer.sources.shell import run_json

log = logging.getLogger(__name__)


def _aws(profile: str | None, region: str | None, *args: str) -> list[str]:
    argv = ["aws"]
    if profile:
        argv += ["--profile", profile]
    if region:
        argv += ["--region", region]
    return argv + list(args) + ["--output", "json"]


def collect_nodegroups(
    cluster: str,
    *,
    profile: str | None = None,
    region: str | None = None,
    max_workers: int = 6,
    timeout: int = 60,
) -> list[NodeGroupConfig]:
    listing = run_json(
        _aws(profile, region, "eks", "list-nodegroups", "--cluster-name", cluster),
        timeout=timeout,
    )
    names = listing.get("nodegroups", []) if isinstance(listing, dict) else []
    if not names:
        return []

    def describe(name: str) -> NodeGroupConfig | None:
        payload = run_json(
            _aws(
                profile, region, "eks", "describe-nodegroup",
                "--cluster-name", cluster, "--nodegroup-name", name,
            ),
            timeout=timeout,
        )
        ng = payload.get("nodegroup", {}) if isinstance(payload, dict) else {}
        if not ng:
            return None
        scaling = ng.get("scalingConfig", {})
        return NodeGroupConfig(
            name=ng.get("nodegroupName", name),
            min_size=int(scaling.get("minSize", 0)),
            max_size=int(scaling.get("maxSize", 0)),
            desired_size=int(scaling.get("desiredSize", 0)),
            instance_types=list(ng.get("instanceTypes", []) or []),
            capacity_type=ng.get("capacityType", ""),
            ami_type=ng.get("amiType", ""),
            taints=[
                f"{t.get('key')}={t.get('value', '')}:{t.get('effect')}"
                for t in (ng.get("taints") or [])
            ],
        )

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(describe, names))
    return [r for r in results if r is not None]


#: Tags Cluster Autoscaler reads per-group options from (scale-down threshold,
#: unneeded time ...), overriding its flags for that group.
ASG_OPTION_PREFIX = "k8s.io/cluster-autoscaler/node-template/autoscaling-options/"


def collect_scaling_groups(cluster: str, *, profile: str | None, region: str | None) -> list[dict]:
    """Auto Scaling groups Cluster Autoscaler discovers for ``cluster``. Read-only."""
    raw = run_json(_aws(profile, region, "autoscaling", "describe-auto-scaling-groups",
                        "--filters", f"Name=tag-key,Values=k8s.io/cluster-autoscaler/{cluster}"),
                   timeout=120)
    out = []
    for g in raw.get("AutoScalingGroups", []):
        tags = {t["Key"]: t.get("Value", "") for t in g.get("Tags", [])}
        out.append({
            "name": g.get("AutoScalingGroupName", ""),
            "min": g.get("MinSize"), "max": g.get("MaxSize"), "desired": g.get("DesiredCapacity"),
            "nodegroup": tags.get("eks:nodegroup-name", ""),
            "instances": [i.get("InstanceId", "") for i in g.get("Instances", [])],
            # Where the ASG may launch, in its own order, and where each instance
            # is: a new node lands in the AZ with the fewest, ties in this order.
            "zones": list(g.get("AvailabilityZones") or []),
            "instance_zones": {i.get("InstanceId", ""): i.get("AvailabilityZone", "")
                               for i in g.get("Instances", [])},
            "subnets": [s for s in (g.get("VPCZoneIdentifier") or "").split(",") if s],
            "options": {k[len(ASG_OPTION_PREFIX):]: v for k, v in tags.items()
                        if k.startswith(ASG_OPTION_PREFIX)},
        })
    return out
