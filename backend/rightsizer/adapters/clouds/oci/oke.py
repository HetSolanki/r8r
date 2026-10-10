"""OKE: the cluster's managed node pools.

Only the pools OKE itself runs. Nodes Karpenter launches are self-managed
instances that belong to no OKE pool; their groups come from the Karpenter
adapter instead (:mod:`rightsizer.adapters.autoscalers.karpenter_oci`).

The OCI CLI wants OCIDs, so ``cluster`` is the cluster's OCID
(``ocid1.cluster...``); its compartment is read from the cluster itself. Every
call goes through the read-only allowlist in :mod:`rightsizer.sources.shell`.
"""

from __future__ import annotations

import logging

from rightsizer.adapters.clouds.base import NodeGroupConfig
from rightsizer.adapters.clouds.oci.shapes import type_name
from rightsizer.sources.shell import run_json

log = logging.getLogger(__name__)


def _oci(profile: str | None, region: str | None, *args: str) -> list[str]:
    argv = ["oci", *args]
    if region:
        argv += ["--region", region]
    if profile:
        argv += ["--profile", profile]
    return argv + ["--output", "json"]


def _get(d: dict, key: str, default=None):
    """The CLI writes kebab-case keys; the API and SDK camelCase. Accept both."""
    if key in d:
        return d[key]
    camel = key.split("-")[0] + "".join(w.title() for w in key.split("-")[1:])
    return d.get(camel, default)


def pool_config(pool: dict) -> NodeGroupConfig:
    shape = _get(pool, "node-shape", "")
    sc = _get(pool, "node-shape-config") or {}
    details = _get(pool, "node-config-details") or {}
    size = int(_get(details, "size", 0) or 0)
    preemptible = bool(_get(details, "preemptible-node-config"))
    return NodeGroupConfig(
        name=_get(pool, "name", ""),
        id=_get(pool, "id", "") or "",
        # An OKE pool holds its size until something resizes it: min, max and
        # desired are all its size unless Cluster Autoscaler owns it.
        min_size=size, max_size=size, desired_size=size,
        instance_types=[type_name(shape, _get(sc, "ocpus"), _get(sc, "memory-in-gbs"), preemptible)],
        capacity_type="preemptible" if preemptible else "on-demand",
        taints=[],
    )


def collect_nodegroups(cluster: str, *, profile: str | None, region: str | None,
                       timeout: int = 60) -> list[NodeGroupConfig]:
    if not cluster.startswith("ocid1.cluster"):
        log.warning("OKE node pools need the cluster's OCID (--cluster ocid1.cluster...), "
                    "not %r; managed pools not read", cluster)
        return []
    found = run_json(_oci(profile, region, "ce", "cluster", "get", "--cluster-id", cluster),
                     timeout=timeout)
    compartment = _get((found or {}).get("data") or {}, "compartment-id")
    if not compartment:
        return []
    listing = run_json(_oci(profile, region, "ce", "node-pool", "list", "--compartment-id", compartment,
                            "--cluster-id", cluster, "--all"), timeout=timeout)
    return [pool_config(p) for p in (listing or {}).get("data") or []]
