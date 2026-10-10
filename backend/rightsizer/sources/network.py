"""Node network bandwidth: what a node group sends and receives, and whether a new shape carries it.

An instance-type change is priced on CPU and memory, but the network moves with
it. EC2 gives every size a *baseline* bandwidth and, below about 16 vCPU, a
*burst* ceiling it can reach while it has network credits -- the "Up to 12.5
Gigabit" in the console. m6i.2xlarge: 3.125 Gbps baseline, 12.5 burst;
m6i.xlarge: half the baseline, the same burst. So:

* a smaller size in the same family has a lower baseline, though the same
  headline "up to" figure;
* packing onto fewer nodes puts the same traffic through fewer network cards;
* a node that runs above its baseline for longer than its credits last (EC2
  says 5 to 60 minutes) is throttled back to the baseline, and the excess is
  dropped -- retransmits and latency, not an error anyone sees.

None of those shows up in a CPU/memory packing. This measures each node's
traffic on its physical interfaces over the window and checks every option
against the new type's baseline and burst ceiling. The limits apply to each
direction separately, so receive and transmit are checked separately and the
busier one decides.
"""

from __future__ import annotations

import datetime as dt
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from rightsizer.sources import queries as q
from rightsizer.core.config import NODEGROUP_LABEL as NG
from rightsizer.core.config import Scope
from rightsizer.sources.thanos import ThanosClient

log = logging.getLogger(__name__)

GBPS = 1e9 / 8  # bytes/s in one Gbps
BUCKET_SECONDS = 21600
#: Physical interfaces on the host. ``eni*`` are the host side of pod veths --
#: the same bytes again -- and ``lo`` never leaves the node.
DEVICES = "ens.*|eth.*|enp.*"
#: Above this share of the baseline for an hour, one busy day spends the credits.
WARN_SHARE = 0.70

#: Published EC2 bandwidth (Gbps: baseline, burst ceiling) for the families this
#: fleet runs, used only until ``describe-instance-types`` has been cached for
#: a type. A described value always wins; see :func:`bandwidth`.
PUBLISHED: dict[str, tuple[float, float]] = {
    **{f"{fam}.{size}": bw
       for fam in ("m6i", "m6id", "c6i", "c6id", "r6i", "r6id", "m7i", "c7i", "r7i")
       for size, bw in {
           "large": (0.781, 12.5), "xlarge": (1.562, 12.5), "2xlarge": (3.125, 12.5),
           "4xlarge": (6.25, 12.5), "8xlarge": (12.5, 12.5), "12xlarge": (18.75, 18.75),
           "16xlarge": (25.0, 25.0),
       }.items()},
    "g4dn.xlarge": (5.0, 25.0), "g4dn.2xlarge": (10.0, 25.0), "g4dn.4xlarge": (20.0, 25.0),
    "g4dn.8xlarge": (50.0, 50.0), "g4dn.12xlarge": (50.0, 50.0), "g4dn.16xlarge": (50.0, 50.0),
}


def _pod_node(scope: Scope) -> str:
    """node-exporter pod -> node, one row per pod."""
    return (
        "topk by (namespace,pod) (1, max by (namespace,pod,node) "
        f"(kube_pod_info{{{scope.node_matchers(q.POD_SET, 'created_by_kind=\"DaemonSet\"')}}}))"
    )


def node_traffic_by_group(scope: Scope, direction: str, agg: str, window: str,
                          bucket: str, resolution: str) -> str:
    """Bytes/s through a node's physical interfaces, reduced per group.

    ``direction`` is ``receive`` or ``transmit``; ``window`` the rate window
    (``5m`` for a burst, ``1h`` for what credits must cover); ``agg`` ``max``
    (the busiest node) or ``avg`` (a typical one). node-exporter is scraped by
    two jobs, so each device is de-duplicated before the interfaces are summed.
    """
    sel = scope.node_matchers(f'device=~"{DEVICES}"')
    per_pod = (
        "sum by (namespace,pod) (max by (namespace,pod,device) "
        f"(rate(node_network_{direction}_bytes_total{{{sel}}}[{window}])))"
    )
    per_node = f"max by (node) (({per_pod}) * on(namespace,pod) group_left(node) {_pod_node(scope)})"
    grouped = f"{agg} by ({NG}) ({per_node} * on(node) group_left({NG}) {q.node_label_join(scope)})"
    return f"max_over_time(({grouped})[{bucket}:{resolution}])"


def queries(scope: Scope) -> dict[str, str]:
    b = f"{BUCKET_SECONDS}s"
    out = {}
    for d, short in (("receive", "rx"), ("transmit", "tx")):
        out[f"{short}_hot_peak"] = node_traffic_by_group(scope, d, "max", "5m", b, "5m")
        out[f"{short}_hot_sustained"] = node_traffic_by_group(scope, d, "max", "1h", b, "15m")
        out[f"{short}_avg_peak"] = node_traffic_by_group(scope, d, "avg", "5m", b, "5m")
        out[f"{short}_avg_sustained"] = node_traffic_by_group(scope, d, "avg", "1h", b, "15m")
    return out


def collect_network(snapshot, thanos_url: str, *, max_workers: int = 8,
                    timeout: int = 300) -> dict[str, dict[str, Any]]:
    """Per node group: the busiest and a typical node's traffic, per direction, in Gbps.

    ``*_peak`` is the highest 5-minute rate over the window -- what a burst
    must reach; ``*_sustained`` the highest 1-hour average -- what the baseline
    must carry once the credits are spent. A group with no node-exporter
    series is left out; the caller reads a missing group as "not measured".
    """
    scope = Scope.for_snapshot(snapshot)
    end = int(dt.datetime.fromisoformat(snapshot.collected_at).timestamp())
    start = end - snapshot.window_days * 86400
    exprs = queries(scope)
    with ThanosClient(thanos_url, max_workers=max_workers, timeout=timeout) as client:
        with ThreadPoolExecutor(max_workers=len(exprs)) as pool:
            futures = {n: pool.submit(client.query_range, e, start, end, BUCKET_SECONDS)
                       for n, e in exprs.items()}
            raw = {n: f.result() for n, f in futures.items()}

    peaks: dict[str, dict[str, float]] = {}
    for name, rows in raw.items():
        for r in rows:
            g = r["metric"].get(NG)
            vals = [float(v) for _, v in r.get("values", []) if v not in (None, "NaN")]
            if g and vals:
                peaks.setdefault(g, {})[name] = max(vals) / GBPS

    out: dict[str, dict[str, Any]] = {}
    for g, p in peaks.items():
        if "rx_hot_peak" not in p and "tx_hot_peak" not in p:
            continue
        out[g] = {k: round(v, 4) for k, v in sorted(p.items())}
        out[g]["measured"] = True
    return out


def bandwidth(inst) -> tuple[float, float, str] | None:
    """(baseline, burst ceiling, source) in Gbps, or None when unknown.

    ``source`` is "described" (EC2's own answer, cached) or "published" (the
    table above, for a cache written before bandwidth was recorded).
    """
    if inst is None:
        return None
    base = getattr(inst, "net_baseline_gbps", 0.0) or 0.0
    if base:
        return base, max(getattr(inst, "net_peak_gbps", 0.0) or base, base), "described"
    if inst.name in PUBLISHED:
        b, p = PUBLISHED[inst.name]
        return b, p, "published"
    return None


def describe_bandwidth(inst) -> str:
    """"3.125 Gbps, burst to 12.5", or "12.5 Gbps"."""
    bw = bandwidth(inst)
    if bw is None:
        return "not described"
    base, peak, _ = bw
    return f"{base:g} Gbps, burst to {peak:g}" if peak > base else f"{base:g} Gbps"


def _dir(measured: dict, key: str) -> float:
    """The busier direction: EC2 limits receive and transmit separately."""
    return max(measured.get(f"rx_{key}", 0.0), measured.get(f"tx_{key}", 0.0))


def assess(measured: dict | None, current, proposed, *, nodes_now: int,
           nodes_after: int) -> dict[str, Any]:
    """Network bandwidth before and after one option, and whether it still fits.

    Traffic is taken as unchanged and spread over the nodes the option leaves.
    The busiest node today stays at least as busy, so the projection for it is
    the larger of its own figure and the new average: the cautious reading, as
    for local disk.
    """
    before, after = bandwidth(current), bandwidth(proposed)
    out: dict[str, Any] = {
        "bandwidthBefore": describe_bandwidth(current),
        "bandwidthAfter": describe_bandwidth(proposed),
        "baselineBefore": before[0] if before else None,
        "baselineAfter": after[0] if after else None,
        "burstBefore": before[1] if before else None,
        "burstAfter": after[1] if after else None,
        "source": after[2] if after else None,
        "notes": [], "verdict": "unknown",
    }
    if after is None:
        out["notes"].append(f"network bandwidth of {getattr(proposed, 'name', '?')} is not known "
                            "-- refresh the instance cache with `aws ec2 describe-instance-types` access")
        return out
    if before and after[0] < before[0]:
        out["notes"].append(f"baseline per node drops {before[0]:g} → {after[0]:g} Gbps")
    if before and before[1] <= before[0] and after[1] > after[0]:
        out["notes"].append("the new type reaches its headline bandwidth only while it has "
                            "network credits")
    if not measured:
        out["notes"].append("node network traffic was not measured for this group -- run "
                            "`rightsizer network` against the snapshot")
        return out

    n_now, n_after = max(nodes_now, 1), max(nodes_after, 1)
    spread = n_now / n_after
    hot_peak, hot_sus = _dir(measured, "hot_peak"), _dir(measured, "hot_sustained")
    avg_peak, avg_sus = _dir(measured, "avg_peak"), _dir(measured, "avg_sustained")
    peak_after = max(hot_peak, avg_peak * spread)
    sus_after = max(hot_sus, avg_sus * spread)
    base, burst = after[0], after[1]
    out["before"] = {"hotPeakGbps": round(hot_peak, 3), "hotSustainedGbps": round(hot_sus, 3),
                     "sustainedShare": round(hot_sus / before[0], 4) if before else None}
    out["after"] = {"hotPeakGbps": round(peak_after, 3), "hotSustainedGbps": round(sus_after, 3),
                    "sustainedShare": round(sus_after / base, 4)}

    verdict = "ok"
    if sus_after > base:
        verdict = "unsafe"
        out["notes"].append(
            f"the busiest node would average {sus_after:.2f} Gbps over an hour, above the "
            f"{base:g} Gbps baseline -- network credits run out and EC2 throttles it to the baseline")
    if peak_after > burst:
        verdict = "unsafe"
        out["notes"].append(
            f"the busiest node would reach {peak_after:.2f} Gbps, above the {burst:g} Gbps "
            "ceiling -- EC2 drops the excess")
    if verdict == "ok" and peak_after > base:
        verdict = "warn"
        out["notes"].append(
            f"the busiest node's peaks ({peak_after:.2f} Gbps) exceed the {base:g} Gbps baseline "
            "and depend on network credits")
    elif verdict == "ok" and sus_after >= WARN_SHARE * base:
        verdict = "warn"
        out["notes"].append(
            f"the busiest node would average {sus_after / base:.0%} of its {base:g} Gbps "
            "baseline over its busiest hour")
    out["verdict"] = verdict
    return out
