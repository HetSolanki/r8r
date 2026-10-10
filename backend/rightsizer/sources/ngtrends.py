"""Node-group utilisation over the window: what the nodes actually used, and had.

An option says "22 nodes become 12". The packing behind it is a single moment;
this is the month behind it -- CPU, memory and kubelet's disk as node-exporter
measured them on every node of the group, beside the capacity the group had at
each point. The console divides the same usage by the capacity an option would
leave, which is the before-and-after a reader can judge a change by.

Node-level rather than pod-level on purpose: it is what a node group is sized
against -- DaemonSets, kubelet and system daemons included -- and it is a few
hundred series rather than every container.
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

STEP_SECONDS = 21600  # 6-hourly points, like the container trends
GIB = 1024**3
GB = 1e9


def _per_node(scope: Scope, inner: str) -> str:
    """A per-node-exporter-pod expression, mapped to node and then summed per group.

    node-exporter is scraped twice here (two jobs), so every series exists in
    duplicate; the inner expression is reduced to one value per exporter pod
    before anything is summed.
    """
    pod_node = (
        "topk by (namespace,pod) (1, max by (namespace,pod,node) "
        f"(kube_pod_info{{{scope.node_matchers(q.POD_SET, 'created_by_kind=\"DaemonSet\"')}}}))"
    )
    return (
        f"sum by ({NG}) ((max by (node) (({inner})"
        f" * on(namespace,pod) group_left(node) {pod_node}))"
        f" * on(node) group_left({NG}) {q.node_label_join(scope)})"
    )


def queries(scope: Scope) -> dict[str, str]:
    sel = scope.node_matchers()
    b = f"[{STEP_SECONDS}s:15m]"
    busy = (
        "sum by (namespace,pod) (max by (namespace,pod,cpu,mode) "
        f'(rate(node_cpu_seconds_total{{{scope.node_matchers('mode!~"idle|iowait|steal"')}}}[5m])))'
    )
    cores = (
        "count by (namespace,pod) (max by (namespace,pod,cpu) "
        f'(node_cpu_seconds_total{{{scope.node_matchers('mode="idle"')}}}))'
    )
    mem_used = (
        f"max by (namespace,pod) (node_memory_MemTotal_bytes{{{sel}}}"
        f" - node_memory_MemAvailable_bytes{{{sel}}})"
    )
    mem_total = f"max by (namespace,pod) (node_memory_MemTotal_bytes{{{sel}}})"
    disk_size = q._with_ng(q.kubelet_fs(scope, "node_filesystem_size_bytes"), scope)
    return {
        # Both take the bucket's peak. Pairing peak usage with *average*
        # capacity read 224% on an autoscaled pool: a node that joins for an
        # hour is busy at once but barely moves the bucket's average. Usage at
        # any instant is within capacity at that instant, so max/max is <= 100%.
        "cpu": f"max_over_time(({_per_node(scope, busy)}){b})",
        "cpuCap": f"max_over_time(({_per_node(scope, cores)}){b})",
        "mem": f"max_over_time(({_per_node(scope, mem_used)}){b})",
        "memCap": f"max_over_time(({_per_node(scope, mem_total)}){b})",
        "disk": q.kubelet_fs_used_by_group(scope, "sum", f"{STEP_SECONDS}s"),
        "diskCap": f"max_over_time((sum by ({NG}) ({disk_size})){b})",
        "nodes": f"max_over_time(({q.nodegroup_node_count(scope)}){b})",
    }


SCALE = {"cpu": 1.0, "cpuCap": 1.0, "mem": GIB, "memCap": GIB, "disk": GB, "diskCap": GB,
         "nodes": 1.0}


def collect_nodegroup_trends(snapshot, thanos_url: str, *, max_workers: int = 8,
                             timeout: int = 300) -> dict[str, dict[str, Any]]:
    """Per node group: 6-hourly usage and capacity over the snapshot's own window.

    Keyed by timestamp so the series line up; a point missing from one series is
    null in that series rather than shifting the others.
    """
    scope = Scope.for_snapshot(snapshot)
    end = int(dt.datetime.fromisoformat(snapshot.collected_at).timestamp())
    start = end - snapshot.window_days * 86400
    exprs = queries(scope)
    with ThanosClient(thanos_url, max_workers=max_workers, timeout=timeout) as client:
        with ThreadPoolExecutor(max_workers=len(exprs)) as pool:
            futures = {n: pool.submit(client.query_range, e, start, end, STEP_SECONDS)
                       for n, e in exprs.items()}
            raw = {n: f.result() for n, f in futures.items()}

    per: dict[str, dict[str, dict[int, float]]] = {}
    for name, rows in raw.items():
        for r in rows:
            g = r["metric"].get(NG)
            if not g:
                continue
            per.setdefault(g, {})[name] = {
                int(float(t)): float(v) / SCALE[name]
                for t, v in r.get("values", []) if v not in (None, "NaN")
            }

    out: dict[str, dict[str, Any]] = {}
    for g, series in per.items():
        stamps = sorted(set().union(*(s.keys() for s in series.values())))
        if len(stamps) < 2 or not series.get("cpu"):
            continue
        out[g] = {"t": stamps, **{
            name: [None if (v := series.get(name, {}).get(t)) is None else round(v, 3)
                   for t in stamps]
            for name in exprs
        }}
    return out
