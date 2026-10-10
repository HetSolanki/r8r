"""Karpenter Provider for OCI (KPO): Karpenter, with OCI's node classes.

A KPO NodePool is a group with no OKE node pool behind it. Its nodes are
self-managed instances KPO launches with the plain Compute API and that join
the cluster on their own principal; the NodePool says what may run where,
and its OCINodeClass says how to launch it -- for a Flex shape, the sizes
(``spec.shapeConfigs``: ocpus, memoryInGbs) Karpenter may choose between.

So this adapter is where those groups come from (:meth:`nodegroup_configs`):

* **sizes** -- the NodeClass's shapeConfigs, as instance types
  (``VM.Standard.E5.Flex:4ocpu-32gb``), ``:preemptible`` for a spot pool;
* **current size** -- the size most of the pool's nodes run, matched from each
  node's capacity (the instance-type label is only the shape);
* **ceiling** -- ``spec.limits``, as a node count at the current size.

Scale-down is Karpenter's consolidation, approximated as for any Karpenter
cluster (:mod:`.karpenter`).

https://github.com/oracle/karpenter-provider-oci
"""

from __future__ import annotations

import logging
import math
from collections import Counter

from rightsizer.adapters.autoscalers.base import AutoscalerConfig
from rightsizer.adapters.autoscalers.karpenter import Karpenter
from rightsizer.adapters.clouds.base import NodeGroupConfig
from rightsizer.adapters.clouds.oci import shapes

log = logging.getLogger(__name__)

NODECLASS_RESOURCE = "ocinodeclasses"
#: What KPO launches when a pool names no shape (the estate's default too).
DEFAULT_SHAPE = "VM.Standard.E5.Flex"


def _nodeclass(item: dict) -> dict:
    spec = item.get("spec") or {}
    return {
        "name": (item.get("metadata") or {}).get("name", ""),
        "shapeConfigs": [{"ocpus": c.get("ocpus"), "memoryInGbs": c.get("memoryInGbs")}
                         for c in spec.get("shapeConfigs") or []],
        # Any shape the class names, wherever the CRD version keeps it.
        "shapes": sorted(_shapes_in(spec)),
    }


def _shapes_in(value) -> set[str]:
    if isinstance(value, str):
        return {value} if value in shapes.SHAPES else set()
    if isinstance(value, dict):
        return set().union(*(_shapes_in(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_shapes_in(v) for v in value)) if value else set()
    return set()


def _cpu(limit) -> float:
    from rightsizer.sources.kfacts import quantity

    return quantity(limit)


class KarpenterOCI(Karpenter):
    name = "karpenter-oci"
    title = "Karpenter (OCI provider)"
    supersedes = ("karpenter",)

    def discover(self, *, context: str | None, namespace: str | None = None) -> AutoscalerConfig | None:
        found = super().discover(context=context, namespace=namespace)
        if found is None:
            return None
        from rightsizer.sources.shell import run_json

        argv = ["kubectl"] + (["--context", context] if context else []) + [
            "get", NODECLASS_RESOURCE, "-o", "json", "--request-timeout=45s"]
        try:
            items = run_json(argv, timeout=60).get("items", [])  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - not KPO: plain Karpenter
            log.debug("no %s: %s", NODECLASS_RESOURCE, exc)
            return None
        if not items:
            return None
        classes = {c["name"]: c for c in (_nodeclass(i) for i in items)}
        found.settings["nodeclasses"] = classes
        found.source = f"{found.source}, {len(classes)} {NODECLASS_RESOURCE}"
        return found

    def nodegroup_configs(self, config: AutoscalerConfig | None, facts,
                          warnings: list[str]) -> list[NodeGroupConfig]:
        if config is None:
            return []
        classes = config.settings.get("nodeclasses") or {}
        nodes_by_pool: dict[str, list[dict]] = {}
        for n in facts.node_facts or []:
            nodes_by_pool.setdefault(n.get("nodegroup", ""), []).append(n)
        out = []
        for pool in config.settings.get("nodepools") or []:
            cls = classes.get(pool.get("nodeClass", "")) or {}
            nodes = nodes_by_pool.get(pool["name"], [])
            preemptible = pool.get("capacityTypes") == ["spot"]
            shape = self._shape(pool, cls, nodes, warnings)
            sizes = self._sizes(shape, cls, preemptible)
            if not sizes:
                warnings.append(f"NodePool {pool['name']}: shape {shape} has no known size; not priced")
                continue
            current = self._current(pool["name"], shape, sizes, nodes, warnings)
            limit_cpu = _cpu((pool.get("limits") or {}).get("cpu"))
            out.append(NodeGroupConfig(
                name=pool["name"],
                source=self.name,
                # Karpenter has no floor and no desired count: it runs what the
                # pods need, up to the limit.
                min_size=0,
                max_size=math.floor(limit_cpu / current.vcpu) if limit_cpu and current.vcpu else 0,
                desired_size=len(nodes),
                instance_types=[current.name, *(s.name for s in sizes if s.name != current.name)],
                capacity_type="preemptible" if preemptible else "on-demand",
                limits={k: str(v) for k, v in (pool.get("limits") or {}).items()},
            ))
        return out

    @staticmethod
    def _shape(pool: dict, cls: dict, nodes: list[dict], warnings: list[str]) -> str:
        named = [v for r in pool.get("requirements") or [] for v in r.get("values") or []
                 if v in shapes.SHAPES] + list(cls.get("shapes") or [])
        running = [t.shape for n in nodes if (t := shapes.parse(n.get("instance_type") or ""))]
        for candidates in (named, running):
            if candidates:
                return Counter(candidates).most_common(1)[0][0]
        warnings.append(f"NodePool {pool['name']}: no shape named or running; assumed {DEFAULT_SHAPE}")
        return DEFAULT_SHAPE

    @staticmethod
    def _sizes(shape: str, cls: dict, preemptible: bool) -> list[shapes.OciType]:
        s = shapes.SHAPES.get(shape)
        if s is None:
            return []
        if not s.flex:
            return [shapes.OciType(shape, s.ocpus, s.memory_gb, preemptible)]
        return [shapes.OciType(shape, float(c["ocpus"]), float(c["memoryInGbs"]), preemptible)
                for c in cls.get("shapeConfigs") or [] if c.get("ocpus") and c.get("memoryInGbs")]

    @staticmethod
    def _current(name: str, shape: str, sizes: list[shapes.OciType], nodes: list[dict],
                 warnings: list[str]) -> shapes.OciType:
        """The size most of the pool's nodes are; the smallest when none run."""
        def size_of(n: dict) -> shapes.OciType | None:
            # The label KPO writes names the size exactly; a node without one
            # (or from before KPO labelled sizes) is matched by its capacity.
            t = shapes.parse(n.get("instance_type") or "")
            if t and t.shape == shape:
                return next((s for s in sizes if (s.ocpus, s.memory_gb) == (t.ocpus, t.memory_gb)), t)
            if n.get("cpu_capacity"):
                return shapes.nearest(shape, n["cpu_capacity"], n.get("mem_capacity_gib") or 0, sizes)
            return None

        seen = Counter(t.name for n in nodes if (t := size_of(n)))
        if not seen:
            return min(sizes, key=lambda t: (t.vcpu, t.memory_gb))
        if len(seen) > 1:
            # The engine prices a group at one size; a mixed pool is priced at
            # its commonest, and the reader is told.
            mix = ", ".join(f"{c}x {n.split(':')[1]}" for n, c in seen.most_common())
            warnings.append(f"NodePool {name} runs mixed sizes ({mix}); priced at the commonest")
        commonest = seen.most_common(1)[0][0]
        return next((t for t in sizes if t.name == commonest), shapes.parse(commonest))

    # -- how a pool is changed ----------------------------------------------------
    # A NodePool has no instance type of its own: Karpenter launches whichever
    # of its NodeClass's sizes the pending pods fit, and consolidates onto a
    # cheaper one when it finds it. So "move to a size" is either nothing (it
    # is listed already) or one more entry in shapeConfigs.

    def retype_plan(self, config: dict) -> dict:
        return {
            "offered": list(config.get("instance_types") or []),
            "ifOffered": "already offered to Karpenter: nothing to change in the pool",
            # Keeping the other sizes matters: removing one is drift, and
            # Karpenter replaces every node running it.
            "template": ("add { ocpus = {ocpus}, memoryInGbs = {gb} } to the pool's shapeConfigs "
                         "in karpenter.hcl, keeping the existing sizes"),
            "surface": "karpenter.hcl",
        }

    def scale_in_command(self, cluster: str, config: dict, nodes: int) -> str:
        return (f"# Karpenter sets {config.get('name')}'s node count from the pods' requests: "
                f"apply the request changes and it consolidates to {nodes}")

    def retype_command(self, cluster: str, config: dict, new_type: str, nodes: int) -> str:
        t = shapes.parse(new_type)
        key = str(config.get("name", "")).replace("-", "_")
        rungs = [shapes.parse(n) for n in config.get("instance_types") or []]
        lines = [f"# karpenter.hcl -> karpenter_nodepools.{key}.shapeConfigs (config/2.products/<product>/)",
                 "  shapeConfigs = ["]
        have = {(r.ocpus, r.memory_gb) for r in rungs if r}
        new = (t.ocpus, t.memory_gb) if t and (t.ocpus, t.memory_gb) not in have else None
        # The new rung in its place in the ladder, marked as the one line to add.
        for o, m in sorted(have | ({new} if new else set())):
            mark = "+  " if (o, m) == new else "   "
            lines.append(f"{mark} {{ ocpus = {shapes._num(o)}, memoryInGbs = {shapes._num(m)} }},")
        lines.append("  ]")
        limit = (config.get("limits") or {}).get("cpu")
        if t and limit:
            lines.append(f"# capacity.hcl is unchanged: the {limit} vCPU ceiling is "
                         f"{int(float(limit) // max(t.vcpu, 1))} node(s) of this size")
        return "\n".join(lines)

    def node_label_for(self, type_name: str) -> str:
        t = shapes.parse(type_name)
        return shapes.kpo_name(t) if t else type_name

    def node_type_name(self, labels: dict) -> str | None:
        t = shapes.parse(labels.get("node.kubernetes.io/instance-type") or "")
        if t is None:
            return None
        if (labels.get("karpenter.sh/capacity-type") or "") == "spot":
            t = shapes.OciType(t.shape, t.ocpus, t.memory_gb, True)
        return t.name

    def notes(self, config: AutoscalerConfig | None) -> list[str]:
        return [n.replace("Karpenter manages", "Karpenter for OCI manages")
                for n in super().notes(config)]

    def summary(self, config: AutoscalerConfig | None) -> str | None:
        s = super().summary(config)
        return s and s.replace("Karpenter manages", "Karpenter for OCI manages")
