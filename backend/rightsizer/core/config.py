"""Configuration: query scope, tunables, and the statistical parameters.

Every number that affects a recommendation lives here so that a change to
methodology is a one-line diff and shows up in the snapshot provenance.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# The label every node-group query groups by. Clouds and autoscalers each name a
# node's group with their own label (eks.amazonaws.com/nodegroup,
# karpenter.sh/nodepool ...); `queries.node_label_join` renames whichever a node
# has to this one, so no other query knows which it was.
NODEGROUP_LABEL = "nodegroup"

DEFAULT_THANOS_URL = os.environ.get("THANOS_URL", "http://localhost:9090")


@dataclass(frozen=True)
class Scope:
    """Which series to look at.

    ``env`` is the cluster identity. The shared Thanos is multi-tenant with no
    cluster label, so *every* selector must pin it or results silently aggregate
    across clusters. Callers never build matchers by hand -- they go through
    :meth:`matchers` / :meth:`node_matchers`, which makes omitting env impossible.

    ``env_label`` is the label that carries ``env``: "env" on the shared Thanos.
    A Prometheus that scrapes one cluster only has no such label, and sets it to
    "" -- there is nothing to pin, and nothing to aggregate across.
    """

    env: str
    namespace: str = "all"
    replica: str = "all"
    env_label: str = "env"

    @classmethod
    def for_snapshot(cls, snapshot, **kw) -> Scope:
        """The scope a snapshot was collected with, so a later query matches it."""
        metrics = (getattr(snapshot, "meta", None) or {}).get("metrics") or {}
        return cls(env=snapshot.env, env_label=metrics.get("env_label", "env"), **kw)

    def _base(self) -> list[str]:
        parts = [f'{self.env_label}="{self.env}"'] if self.env_label else []
        if self.replica and self.replica.lower() != "all":
            parts.append(f'prometheus_replica="{self.replica}"')
        return parts

    def matchers(self, *extra: str) -> str:
        """Matchers for namespaced series (pods, containers)."""
        parts = self._base()
        if self.namespace and self.namespace.lower() != "all":
            parts.append(f'namespace="{self.namespace}"')
        parts.extend(x for x in extra if x)
        return ", ".join(parts)

    def node_matchers(self, *extra: str) -> str:
        """Matchers for node-scoped series, which carry no namespace label."""
        parts = self._base()
        parts.extend(x for x in extra if x)
        return ", ".join(parts)


@dataclass(frozen=True)
class Tunables:
    """Statistical and safety parameters.

    CPU and memory are asymmetric. CPU is compressible -- overshoot costs latency
    (throttling), so a percentile plus headroom is right. Memory is not -- overshoot
    is an OOMKill, so the peak plus a buffer is the floor, and request == limit.
    """

    # --- window / sampling -------------------------------------------------
    window_days: int = 30
    step_seconds: int = 300           # 5m usage resolution
    #: The window `rate()` averages over, which is the smoothing -- distinct
    #: from `step_seconds`, which is only how often that average is sampled.
    #:
    #: It was 300s, matching the step, and that understated what containers
    #: actually need: a 15-second burst averaged across five minutes all but
    #: disappears. Measured on this fleet, p95 per replica was 193m at [5m] and
    #: 320m at [1m] for api-server, 53m against 178m for search-server -- so the
    #: tool was recommending 220m and 70m requests for containers whose real p95
    #: is above that. Applying those would have throttled them.
    #:
    #: 60s sampled every 300s is a 1-in-5 subsample of the 1m distribution,
    #: which reproduces the true 1m p95 within 2% at no extra cost. It must stay
    #: at or above 4x the scrape interval (15s here) or `rate()` returns gaps.
    rate_window_seconds: int = 60
    #: Which point on the per-replica distribution a CPU request is sized from.
    #: "p95" leaves 5% of samples above the request -- fine while the container
    #: has no CPU limit and the node has room, which is the usual case, and a
    #: real exposure when either stops being true. Raise it to "p99" to size for
    #: the tail. One of p50/p75/p90/p95/p99/max.
    request_coverage: str = "p95"
    #: How far above p95 to size a container whose tail is fat, graduated by how
    #: fat it is. ``tail_trigger`` is the p99/p95 ratio below which nothing
    #: happens; ``tail_lift`` is the most it can ever add, as a multiple of p95.
    #:
    #:     r     = p99 / p95
    #:     basis = p95                                    if r <= tail_trigger
    #:           = min(p99, p95 x (1 + lift x (1 - trigger/r)))   otherwise
    #:
    #: Two things this gets right that a flat percentile does not.
    #:
    #: **It responds to the gap.** Most containers here are tight -- the median
    #: ratio is 1.24 -- and are left alone. Thirty-one are not, and the uplift
    #: grows with the ratio rather than switching on at a threshold.
    #:
    #: **It saturates.** The instinct is that a bigger gap deserves a bigger
    #: reservation, but the opposite holds at the extremes: job-controller sits
    #: at 20m for 95% of the window and needs 734m at p99, a ratio of 36.
    #: Reserving half its p99 would be 367m on every replica to cover something
    #: that happens 1% of the time -- 18x what it uses the rest of the month.
    #: The curve gives it 49m instead. A rare, enormous burst is the *worst*
    #: case to reserve for; the node absorbs it, which is what unreserved
    #: headroom is for.
    #:
    #: Fleet cost on prod-us1: 172 cores against 158 at flat p95 and 211 today.
    #: Set ``tail_lift`` to 0.0 to disable.
    tail_trigger: float = 1.5
    tail_lift: float = 1.5
    coarse_step_seconds: int = 21600  # 6h for owner/topology/requests
    chunk_hours: int = 12             # a single unchunked 30d subquery returns 504

    # --- CPU ---------------------------------------------------------------
    cpu_percentile: float = 95.0
    cpu_headroom: float = 1.5
    cpu_round_millicores: int = 10
    cpu_floor_millicores: int = 50

    # --- memory ------------------------------------------------------------
    mem_headroom: float = 1.5
    mem_oom_headroom: float = 1.25    # extra bump when an OOMKill was observed
    mem_round_mib: int = 16
    mem_floor_mib: int = 64

    # --- data sufficiency --------------------------------------------------
    min_samples: int = 100            # KRR's gate: below this, no recommendation
    min_coverage: float = 0.5         # ...and at least half the expected samples

    # --- node-group sizing -------------------------------------------------
    target_utilization: float = 0.75  # headroom applied to N_floor
    # Share of a node's allocatable the packer may fill when proposing a target.
    # Leaves room for rolling-update surge, a container exceeding its observed
    # peak, and kubelet eviction thresholds. Not applied when predicting Cluster
    # Autoscaler, which keeps no headroom of its own.
    # Neither the scheduler nor Cluster Autoscaler keeps headroom: nodes fill
    # to 100% of allocatable by request. Overload is checked on measured use
    # instead (rightsizer.engine.realistic.load_check).
    pack_headroom: float = 1.0
    # Below this request coverage a group's requests do not describe reality;
    # fall back to measured usage instead of recommending from requests.
    request_coverage_threshold: float = 0.8
    usage_target_utilization: float = 0.70

    # --- concurrency -------------------------------------------------------
    max_workers: int = 8              # shared production Thanos: stay polite
    http_timeout: int = 180
    http_retries: int = 3

    @property
    def window_seconds(self) -> int:
        return self.window_days * 86400

    def expected_samples(self) -> int:
        return max(1, self.window_seconds // self.step_seconds)


@dataclass(frozen=True)
class Settings:
    scope: Scope
    tunables: Tunables = field(default_factory=Tunables)
    thanos_url: str = DEFAULT_THANOS_URL
    end_ts: int | None = None # points to month end (default is None -> now)
