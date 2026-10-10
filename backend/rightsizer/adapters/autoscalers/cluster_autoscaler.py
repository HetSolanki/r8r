"""Cluster Autoscaler: the autoscaler the engine replays natively.

The replay itself is ``engine/autoscaler.py`` (per-group estimate, drain rules)
and ``engine/careplay.py`` (v1.35.0's whole-cluster scale-down loop). This
adapter finds the running deployment and turns its flags into the settings
that replay runs with.
"""

from __future__ import annotations

from rightsizer.adapters.autoscalers.base import AutoscalerConfig, NodeAutoscaler
from rightsizer.engine.autoscaler import CAConfig, parse_ca_args, read_ca_from_cluster

DEFAULT_NAMESPACE = "kube-system"


class ClusterAutoscaler(NodeAutoscaler):
    name = "cluster-autoscaler"
    title = "Cluster Autoscaler"
    models_scale_down = True

    def discover(self, *, context: str | None, namespace: str | None = None) -> AutoscalerConfig | None:
        cfg = read_ca_from_cluster(context, namespace or DEFAULT_NAMESPACE)
        return None if cfg is None else self._wrap(cfg)

    def from_flags(self, pairs: list[str]) -> AutoscalerConfig:
        return self._wrap(parse_ca_args(pairs))

    def _wrap(self, cfg: CAConfig) -> AutoscalerConfig:
        return AutoscalerConfig(kind=self.name, source=cfg.source, settings={"args": cfg.args})

    def scale_down_settings(self, config: AutoscalerConfig | None) -> CAConfig:
        if config is None or not (config.settings or {}).get("args"):
            return CAConfig()
        return CAConfig(source=config.source, args=dict(config.settings["args"]))

    def notes(self, config: AutoscalerConfig | None) -> list[str]:
        if config is None:
            return ["Cluster Autoscaler's flags were not read from the cluster; upstream defaults are assumed"]
        return []
