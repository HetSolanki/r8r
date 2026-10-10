"""Collection: Thanos -> Snapshot.

All queries for a phase are issued concurrently; the wall clock is the slowest
query rather than the sum. Nothing here interprets the data -- interpretation
lives in :mod:`rightsizer.analysis.recommend` and :mod:`rightsizer.analysis.blockers` so it can be
re-run against a frozen snapshot.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace

from rightsizer.sources import queries as q
from rightsizer.core.config import NODEGROUP_LABEL as NG
from rightsizer.core.config import Settings
from rightsizer.core.models import ClusterFacts, HPASpec, NodeGroupState, PDBState, Snapshot, UsageStat
from rightsizer.core.stats import percentile
from rightsizer.sources.thanos import ThanosClient, instant_map

log = logging.getLogger(__name__)

# A ReplicaSet name carries the Deployment's pod-template hash; a Job name created
# by a CronJob carries a timestamp. Left alone, each rollout looks like a new
# workload and the report fills with duplicates.
_RS_HASH = re.compile(r"-[a-z0-9]{6,10}$")
_JOB_STAMP = re.compile(r"-\d{8,10}$")

_LOGICAL_KIND = {"ReplicaSet": "Deployment", "Job": "CronJob"}


def logical_workload(kind: str, owner_name: str) -> tuple[str, str]:
    """Collapse a controller name to the workload a human configures."""
    if kind == "ReplicaSet":
        return _RS_HASH.sub("", owner_name), "Deployment"
    if kind == "Job":
        return _JOB_STAMP.sub("", owner_name), "CronJob"
    return owner_name, kind or "Unknown"


class PreflightError(RuntimeError):
    """The env cannot be analyzed: a required exporter is missing."""


def preflight(client: ThanosClient, scope, at: int | None = None, start: int | None = None) -> int | None:
    """Fail loudly when an exporter is missing, rather than reporting zeros.

    Returns the start the window can actually use: ``start`` when the store
    holds the whole window, a later whole-day boundary when it holds less.

    cAdvisor and kube-state-metrics are shipped by different components, and a
    cluster can have one without the other. Without kube_pod_owner there is no
    pod->workload mapping, so every usage series drops on the join and the run
    silently produces "no workloads need right-sizing" -- the most dangerous
    possible output, because it reads as a clean bill of health.
    """
    # An @ modifier pins the probe to the window's end, so a historical replay
    # is not judged by whether the exporters are running today.
    probes = client.query_many(
        {
            "cadvisor": f"count(container_cpu_usage_seconds_total{{{scope.node_matchers()}}})",
            "kube_state": f"count(kube_pod_owner{{{scope.node_matchers()}}})",
            "node_labels": f"count(kube_node_labels{{{scope.node_matchers()}}})",
        },
        at=at,
    )
    missing = [name for name, result in probes.items() if not result]
    if missing:
        present = [n for n in probes if n not in missing]
        raise PreflightError(
            f"env={scope.env!r} is missing {', '.join(missing)} "
            f"(present: {', '.join(present) or 'nothing'}). "
            "Without kube-state-metrics there is no pod->workload mapping and every "
            "usage series drops on the join. Check that KSM ships to Thanos for this "
            "cluster, or that the env label matches."
        )

    # Every node-group query joins on a label some adapter names (karpenter.sh/
    # nodepool, eks.amazonaws.com/nodegroup ...). kube-state-metrics exposes a
    # node label only when allowlisted; without one, every group comes back
    # empty and the run reads as a cluster with nothing to right-size.
    if not client.query_instant(f"count({q.node_label_join(scope)})", at):
        from rightsizer.adapters import node_group_label_keys

        keys = ", ".join(node_group_label_keys())
        raise PreflightError(
            f"no node in env={scope.env!r} carries a node-group label on kube_node_labels "
            f"(looked for: {keys}). kube-state-metrics must expose it: "
            "--metric-labels-allowlist=nodes=[<label>], or $RIGHTSIZER_GROUP_LABELS for "
            "a label of your own.")

    # The window must be inside what the store keeps. A Prometheus with 14 days
    # of retention asked for 30 answers with 14, and sizing as though it had 30
    # would claim evidence it does not have. Rather than refuse, shorten the
    # window to the whole days the store does hold; collect() records both
    # figures so every reader can see the recommendation rests on less.
    if start is None:
        return None
    return available_start(client, scope, start, at)


#: Below this much history there is nothing to size from: a single day misses
#: the weekly cycle already, and less than one misses the daily one.
MIN_WINDOW_DAYS = 1


def _has_history(client: ThanosClient, scope, ts: int) -> bool:
    probe = f"count(last_over_time(kube_pod_owner{{{scope.node_matchers()}}}[1h]))"
    return bool(client.query_instant(probe, ts + 3600))


def available_start(client: ThanosClient, scope, start: int, end: int | None) -> int:
    """The earliest whole-day boundary from ``start`` at which the store has data.

    Data missing at the front of a window is a retention cut or a cluster that
    did not exist yet; either way it is a prefix, so once a day has data every
    later day does too. That makes the first day with data a binary search:
    ``log2(days)`` instant probes rather than one per day.
    """
    if _has_history(client, scope, start):
        return start
    if end is None:
        raise PreflightError(
            f"no data for env={scope.env!r} at the start of the window, and no window "
            "end to measure the available history against.")
    total = (end - start) // 86400
    # Day offsets (from start) still in play; the last one leaves one full day.
    lo, hi = 1, total - MIN_WINDOW_DAYS
    if hi < lo or not _has_history(client, scope, start + hi * 86400):
        raise PreflightError(
            f"env={scope.env!r} has less than {MIN_WINDOW_DAYS} day(s) of history in the "
            f"metrics store before {dt.datetime.fromtimestamp(end, dt.UTC):%Y-%m-%d %H:%M} UTC; "
            "there is nothing to size from yet.")
    while lo < hi:
        mid = (lo + hi) // 2
        if _has_history(client, scope, start + mid * 86400):
            hi = mid
        else:
            lo = mid + 1
    return start + lo * 86400


def collect_cluster_facts(
    *,
    context: str | None = None,
    cluster: str | None = None,
    profile: str | None = None,
    region: str | None = None,
    cloud: str | None = None,
) -> ClusterFacts:
    """Read the constraints Prometheus cannot see. Every call is allowlisted read-only.

    Deliberately small: controllers rather than pods (a pod template carries the
    affinity and volumes that matter, and there are ~20x fewer of them), one LIST
    per resource type, no watches. Any failure degrades to "not collected" rather
    than aborting the run -- the metrics half still produces a report, marked as
    an upper bound.
    """
    from dataclasses import asdict as _asdict

    from rightsizer.sources.kube import has_access

    facts = ClusterFacts()
    if has_access(context):
        from rightsizer.sources.kube import collect_nodes, collect_pdbs, collect_workload_constraints

        try:
            facts.workload_constraints = [_asdict(w) for w in collect_workload_constraints(context)]
            facts.pdb_specs = [_asdict(p) for p in collect_pdbs(context)]
            facts.node_facts = [_asdict(n) for n in collect_nodes(context)]
            # `collected` gates the warning that anti-affinity, topology spread
            # and safe-to-evict were never checked. Setting it because the calls
            # did not raise -- rather than because they returned something --
            # made a cluster with zero facts look fully evaluated, and silently
            # dropped that warning from every node group in it.
            facts.collected = bool(
                facts.workload_constraints or facts.pdb_specs or facts.node_facts
            )
            if not facts.collected:
                log.warning(
                    "kubectl succeeded against %s but returned no workloads, "
                    "PDBs or nodes; treating cluster facts as uncollected",
                    f"context {context!r}" if context else "the pod's own cluster",
                )
        except Exception as exc:  # noqa: BLE001 - degrade, never abort
            log.warning("kubernetes facts unavailable (%s): %s", type(exc).__name__, exc)

    if cluster:
        from rightsizer.adapters import clouds

        provider = clouds.get(cloud, region=region, profile=profile)
        try:
            facts.nodegroup_configs = [_asdict(n) for n in provider.collect_nodegroups(cluster)]
            facts.collected = True
        except Exception as exc:  # noqa: BLE001
            log.warning("%s node group config unavailable (%s): %s",
                        provider.name, type(exc).__name__, exc)

    return facts


#: Quantiles kept for the concurrent-total series. Enough to replay an HPA and
#: to say what a proposed floor would have covered, without keeping the series.
LADDER = (50, 75, 90, 95, 99)


#: Phase-2 sets that are legitimately empty on some clusters. With no CPU limits
#: anywhere there is no limit or throttling series at all: every chunk comes back
#: empty, which is an absence, not a failed request.
OPTIONAL_COARSE = frozenset({"lim_cpu", "lim_mem", "throttle"})

#: What a gap in each phase-2 set distorts, so a warning says what to distrust.
COARSE_IMPACT = {
    "req_cpu": "CPU requests may be missing or stale",
    "req_mem": "memory requests may be missing or stale",
    "lim_cpu": "CPU limits may be missing",
    "lim_mem": "memory limits may be missing",
    "throttle": "worst-case CPU throttling may be understated",
    "wl_ng": "workload-to-node-group shares may be skewed",
    "ng_nodes": "30-day min/avg/peak node counts come from partial history",
}


def _coverage_warnings(
    sets: dict, *, impact: dict[str, str] | None = None,
    default: str = "values may be missing", optional: frozenset[str] = frozenset(),
) -> list[str]:
    """One warning per chunked query that did not return every chunk."""
    out = []
    for name, series in sets.items():
        if series.complete:
            continue
        if name in optional and series.chunks_ok == 0:
            continue
        out.append(
            f"{name}: only {series.chunks_ok}/{series.chunks_total} chunks returned data "
            f"({series.coverage:.0%}); {(impact or {}).get(name, default)}"
        )
    return out


def _history_warnings(series_set, expected: int, threshold: float = 0.9) -> list[str]:
    """Node-count history that is thin even though every chunk answered.

    A chunk counts as returned if it held any data at all, so a partial Thanos
    response -- a store down for part of the chunk -- passes the chunk check and
    still drops samples. Node counts feed the 30-day floor, the ceiling and the
    savings baseline directly, so they are checked per group.
    """
    out = []
    for key, values in sorted(series_set.values.items()):
        (group,) = key
        if len(values) < expected * threshold:
            out.append(
                f"ng_nodes: {group} has {len(values)} of ~{expected} samples; its 30-day "
                "min/avg/peak node counts come from partial history (or the group is newer "
                "than the window)"
            )
    return out


def _varied(series: list[float], tolerance: float = 0.02) -> bool:
    """Did this value move during the window?

    A request that changed mid-window makes every utilisation figure derived
    from it a blend of two regimes. The tolerance absorbs float noise from the
    scrape, not a real edit.
    """
    values = [v for v in series if v > 0]
    if len(values) < 2:
        return False
    lo, hi = min(values), max(values)
    return (hi - lo) > lo * tolerance


def _ladder(series: list[float]) -> dict:
    if not series:
        return {}
    out = {f"p{q}": round(percentile(series, q), 6) for q in LADDER}
    out["max"] = round(max(series), 6)
    out["n"] = len(series)
    return out


def collect(
    settings: Settings,
    facts: ClusterFacts | None = None,
    ca: dict | None = None,
    meta: dict | None = None,
) -> Snapshot:
    scope, tun = settings.scope, settings.tunables
    end = settings.end_ts or int(dt.datetime.now(dt.UTC).timestamp())
    start = end - tun.window_seconds

    requested_days = tun.window_days
    warnings: list[str] = []
    owner_keys = ("namespace", "owner_kind", "owner_name", "container")

    with ThanosClient(
        settings.thanos_url,
        max_workers=tun.max_workers,
        timeout=tun.http_timeout,
        retries=tun.http_retries,
    ) as client:
        usable = preflight(client, scope, end, start)
        if usable is not None and usable > start:
            requested = tun.window_days
            tun = replace(tun, window_days=(end - usable) // 86400)
            start = end - tun.window_seconds
            msg = (f"only {tun.window_days} of the {requested} requested days of history exist "
                   f"for env={scope.env!r}; collected and sized over {tun.window_days} days")
            warnings.append(msg)
            log.warning(msg)
        cpu_expr = q.cpu_per_container(scope, tun.rate_window_seconds)
        mem_expr = q.memory_per_container(scope)

        # ---- phase 1: usage history (the expensive part) -----------------
        log.info("collecting %dd usage for env=%s", tun.window_days, scope.env)
        usage_jobs = {
            f"{res}_{agg}": q.workload_usage(scope, expr, agg)
            for res, expr in (("cpu", cpu_expr), ("mem", mem_expr))
            for agg in ("max", "avg", "sum", "count")
        }
        usage_sets = {}
        with ThreadPoolExecutor(max_workers=2) as outer:
            futures = {
                outer.submit(
                    client.query_range_chunked,
                    expr,
                    start,
                    end,
                    step=tun.step_seconds,
                    chunk_hours=tun.chunk_hours,
                    label_keys=owner_keys,
                ): name
                for name, expr in usage_jobs.items()
            }
            for fut, name in futures.items():
                usage_sets[name] = fut.result()

        for msg in _coverage_warnings(usage_sets, default="peaks may be missing"):
            warnings.append(msg)
            log.warning(msg)

        # ---- phase 2: specs and topology (coarse, cheap) ------------------
        coarse = {
            "req_cpu": (q.workload_requests(scope, "cpu"), owner_keys),
            "req_mem": (q.workload_requests(scope, "memory"), owner_keys),
            "lim_mem": (q.workload_limits(scope, "memory"), owner_keys),
            "lim_cpu": (q.workload_limits(scope, "cpu"), owner_keys),
            "throttle": (q.cpu_throttling(scope), owner_keys),
            "wl_ng": (
                q.workload_to_nodegroup(scope),
                ("namespace", "owner_kind", "owner_name", NG),
            ),
            "ng_nodes": (q.nodegroup_node_count(scope), (NG,)),
        }
        coarse_sets = {}
        with ThreadPoolExecutor(max_workers=len(coarse)) as outer:
            futures = {
                outer.submit(
                    client.query_range_chunked,
                    expr,
                    start,
                    end,
                    step=tun.coarse_step_seconds,
                    chunk_hours=tun.chunk_hours * 10,
                    label_keys=keys,
                ): name
                for name, (expr, keys) in coarse.items()
            }
            for fut, name in futures.items():
                coarse_sets[name] = fut.result()

        for msg in [
            *_coverage_warnings(coarse_sets, impact=COARSE_IMPACT, optional=OPTIONAL_COARSE),
            *_history_warnings(
                coarse_sets["ng_nodes"], tun.window_seconds // tun.coarse_step_seconds
            ),
        ]:
            warnings.append(msg)
            log.warning(msg)

        # ---- phase 3: current state (instant) -----------------------------
        instant = client.query_many(
            {
                "alloc_cpu": q.nodegroup_allocatable(scope, "cpu"),
                "alloc_mem": q.nodegroup_allocatable(scope, "memory"),
                "ds_cpu": q.nodegroup_daemonset_requests(scope, "cpu"),
                "ds_mem": q.nodegroup_daemonset_requests(scope, "memory"),
                "wl_cpu": q.nodegroup_workload_requests(scope, "cpu"),
                "wl_mem": q.nodegroup_workload_requests(scope, "memory"),
                "pods": q.nodegroup_pod_count(scope),
                "nodes_now": q.nodegroup_node_count(scope),
                "node_pods": q.node_pod_count(scope),
                "ds_pods": q.nodegroup_daemonset_pod_count(scope),
                "pod_placement": q.pod_placement(scope),
                "pod_cpu": q.pod_requests(scope, "cpu"),
                "pod_mem": q.pod_requests(scope, "memory"),
                "pod_owner": q.pod_owner(scope),
                "pod_created": q.pod_created(scope),
                "node_cpu_req": q.node_requests(scope, "cpu"),
                "node_mem_req": q.node_requests(scope, "memory"),
                "oom": q.oomkilled(scope),
                "hpa_info": q.hpa_info(scope),
                "hpa_target": q.hpa_target_metric(scope),
                "hpa_min": q.hpa_replica_bounds(scope, "min"),
                "hpa_max": q.hpa_replica_bounds(scope, "max"),
                "pdb_desired": q.pdb_desired_healthy(scope),
                "pdb_expected": q.pdb_expected_pods(scope),
                "pdb_current": q.pdb_current_healthy(scope),
                "pod_unready": q.unready_pods(scope),
                "pod_waiting": q.container_waiting_reasons(scope),
                "pod_init_waiting": q.init_container_waiting_reasons(scope),
                "node_not_ready": q.node_not_ready(scope),
                "node_cordoned": q.node_cordoned(scope),
            },
            at=settings.end_ts,
        )

    usage = _build_usage(usage_sets, coarse_sets, instant)
    placements = _build_placements(instant)
    occupancy = _node_occupancy(placements)
    nodegroups = _build_nodegroups(instant, coarse_sets, occupancy)
    return Snapshot(
        env=scope.env,
        collected_at=dt.datetime.fromtimestamp(end, dt.UTC).isoformat(timespec="seconds"),
        window_days=tun.window_days,
        requested_window_days=requested_days,
        tunables=asdict(tun),
        usage=usage,
        nodegroups=nodegroups,
        hpas=_build_hpas(instant),
        pdbs=_build_pdbs(instant),
        workload_nodegroups=_build_workload_nodegroups(coarse_sets["wl_ng"]),
        facts=facts or ClusterFacts(),
        ca=ca or {},
        placements=placements,
        unhealthy_pods=_build_unhealthy_pods(instant),
        meta=meta or {},
        warnings=warnings,
    )


# --------------------------------------------------------------------------


def _merge_by_workload(raw: dict[tuple[str, ...], list[float]]) -> dict[tuple, list[float]]:
    """Fold controller-level keys into logical (namespace, workload, kind, container)."""
    merged: dict[tuple, list[float]] = {}
    for (namespace, owner_kind, owner_name, container), values in raw.items():
        name, kind = logical_workload(owner_kind, owner_name)
        merged.setdefault((namespace, name, kind, container), []).extend(values)
    return merged


def _build_usage(usage_sets, coarse_sets, instant) -> list[UsageStat]:
    peaks = {res: _merge_by_workload(usage_sets[f"{res}_max"].values) for res in ("cpu", "mem")}
    means = {res: _merge_by_workload(usage_sets[f"{res}_avg"].values) for res in ("cpu", "mem")}
    totals = {res: _merge_by_workload(usage_sets[f"{res}_sum"].values) for res in ("cpu", "mem")}
    counts = {res: _merge_by_workload(usage_sets[f"{res}_count"].values) for res in ("cpu", "mem")}
    reqs = {
        "cpu": _merge_by_workload(coarse_sets["req_cpu"].values),
        "mem": _merge_by_workload(coarse_sets["req_mem"].values),
    }
    limits = _merge_by_workload(coarse_sets["lim_mem"].values)
    cpu_limits = _merge_by_workload(coarse_sets["lim_cpu"].values)
    throttling = _merge_by_workload(coarse_sets["throttle"].values)

    oom_pairs = instant_map(instant.get("oom", []), ("namespace", "container"))
    oom_ns_container = {k for k, v in oom_pairs.items() if v > 0}

    stats: dict[tuple, UsageStat] = {}
    all_keys = set()
    for mapping in (*peaks.values(), *reqs.values()):
        all_keys |= set(mapping)

    for key in all_keys:
        namespace, workload, kind, container = key
        cpu_peaks = peaks["cpu"].get(key, [])
        mem_peaks = peaks["mem"].get(key, [])
        cpu_totals = totals["cpu"].get(key, [])
        mem_totals = totals["mem"].get(key, [])
        replicas = counts["cpu"].get(key) or counts["mem"].get(key) or []
        stats[("cpu", *key)] = UsageStat(
            resource="cpu",
            namespace=namespace,
            workload=workload,
            kind=kind,
            container=container,
            replicas=int(max(replicas)) if replicas else 0,
            request=max(reqs["cpu"].get(key, [0.0]) or [0.0]),
            request_varied=_varied(reqs["cpu"].get(key, [])),
            avg=_mean(means["cpu"].get(key, [])),
            p95=percentile(cpu_peaks, 95),
            peak=max(cpu_peaks) if cpu_peaks else 0.0,
            total_avg=_mean(cpu_totals),
            total_p95=percentile(cpu_totals, 95),
            total_peak=max(cpu_totals) if cpu_totals else 0.0,
            total_ladder=_ladder(cpu_totals),
            limit=max(cpu_limits.get(key, [0.0]) or [0.0]),
            # Worst throttling seen over the window, not the mean: a container
            # clipped hard for one hour a day averages to "fine".
            # Clamped here as well as in the query: a snapshot collected before
            # the query carried the clamp would otherwise report a ratio above 1.
            throttle_pct=min(1.0, max(throttling.get(key, [0.0]) or [0.0])),
            ladder=_ladder(cpu_peaks),
            avg_ladder=_ladder(means["cpu"].get(key, [])),
            samples=len(cpu_peaks),
        )
        stats[("mem", *key)] = UsageStat(
            resource="mem",
            namespace=namespace,
            workload=workload,
            kind=kind,
            container=container,
            replicas=int(max(replicas)) if replicas else 0,
            request=max(reqs["mem"].get(key, [0.0]) or [0.0]),
            request_varied=_varied(reqs["mem"].get(key, [])),
            limit=max(limits.get(key, [0.0]) or [0.0]),
            avg=_mean(means["mem"].get(key, [])),
            p95=percentile(mem_peaks, 95),
            peak=max(mem_peaks) if mem_peaks else 0.0,
            total_avg=_mean(mem_totals),
            total_p95=percentile(mem_totals, 95),
            total_peak=max(mem_totals) if mem_totals else 0.0,
            total_ladder=_ladder(mem_totals),
            ladder=_ladder(mem_peaks),
            avg_ladder=_ladder(means["mem"].get(key, [])),
            samples=len(mem_peaks),
            oom_killed=(namespace, container) in oom_ns_container,
        )

    for stat in stats.values():
        stat.container = stat.container or "(unnamed)"
    return list(stats.values())


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _node_occupancy(placements: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """nodegroup -> observed occupancy per node, split by pod kind.

    DaemonSet requests are reported separately rather than folded in. Cluster
    Autoscaler leaves them out of its utilisation sum and the packer treats them
    as fixed per-node overhead -- but a reader looking at a node wants to know how
    full it *actually* is, and hiding a third of its occupancy answers a different
    question than the one being asked.

    Built from per-pod placement rather than node-level aggregates, so the split
    is exact instead of an average.
    """
    out: dict[str, list[dict]] = {}
    for group, rows in placements.items():
        by_node: dict[str, dict[str, float]] = {}
        for row in rows:
            node = row.get("node", "")
            if not node:
                continue
            entry = by_node.setdefault(
                node, {"cpu": 0.0, "mem": 0.0, "pods": 0, "ds_cpu": 0.0, "ds_mem": 0.0, "ds_pods": 0}
            )
            if row.get("owner_kind") == "DaemonSet":
                entry["ds_cpu"] += row.get("cpu", 0.0)
                entry["ds_mem"] += row.get("mem", 0.0)
                entry["ds_pods"] += 1
            else:
                entry["cpu"] += row.get("cpu", 0.0)
                entry["mem"] += row.get("mem", 0.0)
                entry["pods"] += 1
        out[group] = sorted(
            (
                {
                    "cpu": round(e["cpu"], 3),
                    "mem": round(e["mem"] / 1024**3, 2),
                    "pods": int(e["pods"]),
                    "dsCpu": round(e["ds_cpu"], 3),
                    "dsMem": round(e["ds_mem"] / 1024**3, 2),
                    "dsPods": int(e["ds_pods"]),
                }
                for e in by_node.values()
            ),
            key=lambda r: -(r["pods"] + r["dsPods"]),
        )
    return out


def _build_nodegroups(instant, coarse_sets, occupancy) -> list[NodeGroupState]:
    alloc_cpu = instant_map(instant["alloc_cpu"], (NG,))
    alloc_mem = instant_map(instant["alloc_mem"], (NG,))
    ds_cpu = instant_map(instant["ds_cpu"], (NG,))
    ds_mem = instant_map(instant["ds_mem"], (NG,))
    wl_cpu = instant_map(instant["wl_cpu"], (NG,))
    wl_mem = instant_map(instant["wl_mem"], (NG,))
    pods = instant_map(instant["pods"], (NG,))
    ds_pods = instant_map(instant.get("ds_pods", []), (NG,))
    nodes_now = instant_map(instant["nodes_now"], (NG,))
    history = coarse_sets["ng_nodes"].values
    not_ready = _nodes_by_group(instant.get("node_not_ready"))
    cordoned = _nodes_by_group(instant.get("node_cordoned"))

    out = []
    for key in sorted(set(alloc_cpu) | set(nodes_now)):
        (name,) = key
        series = history.get(key, [])
        out.append(
            NodeGroupState(
                name=name,
                nodes_current=int(nodes_now.get(key, 0)),
                nodes_min_30d=int(min(series)) if series else 0,
                nodes_avg_30d=_mean(series),
                nodes_peak_30d=int(max(series)) if series else 0,
                allocatable_cpu=alloc_cpu.get(key, 0.0),
                allocatable_mem=alloc_mem.get(key, 0.0),
                daemonset_cpu=ds_cpu.get(key, 0.0),
                daemonset_mem=ds_mem.get(key, 0.0),
                workload_cpu_requests=wl_cpu.get(key, 0.0),
                workload_mem_requests=wl_mem.get(key, 0.0),
                pod_count=int(pods.get(key, 0)),
                daemonset_pods=int(ds_pods.get(key, 0)),
                occupancy=occupancy.get(name, []),
                not_ready_nodes=None if not_ready is None else not_ready.get(name, []),
                cordoned_nodes=None if cordoned is None else cordoned.get(name, []),
            )
        )
    return out


def _nodes_by_group(result) -> dict[str, list[str]] | None:
    """{node group: [node, ...]} from a node-level query; None when not queried.

    An empty result means "none", which is a finding; a missing key means the
    query was never run, which is not.
    """
    if result is None:
        return None
    out: dict[str, list[str]] = {}
    for series in result:
        m = series.get("metric", {})
        if m.get("node") and m.get(NG):
            out.setdefault(m[NG], []).append(m["node"])
    return {group: sorted(set(nodes)) for group, nodes in out.items()}


def _build_placements(instant) -> dict[str, list[dict]]:
    """Observed pod -> node placement, grouped by node group.

    Pods with no declared request are kept at zero: they still occupy a
    scheduling slot and still veto a drain, and dropping them is what made the
    earlier model disagree with the cluster.
    """
    cpu = instant_map(instant.get("pod_cpu", []), ("namespace", "pod"))
    mem = instant_map(instant.get("pod_mem", []), ("namespace", "pod"))
    created = instant_map(instant.get("pod_created", []), ("namespace", "pod"))
    owners: dict[tuple[str, str], tuple[str, str]] = {}
    for series in instant.get("pod_owner", []):
        m = series.get("metric", {})
        owners[(m.get("namespace", ""), m.get("pod", ""))] = (
            m.get("owner_kind", ""),
            m.get("owner_name", ""),
        )

    out: dict[str, list[dict]] = {}
    for series in instant.get("pod_placement", []):
        m = series.get("metric", {})
        group = m.get(NG, "")
        if not group:
            continue
        key = (m.get("namespace", ""), m.get("pod", ""))
        owner_kind, owner_name = owners.get(key, ("", ""))
        name, kind = logical_workload(owner_kind, owner_name or m.get("pod", ""))
        out.setdefault(group, []).append({
            "namespace": key[0],
            "pod": key[1],
            "node": m.get("node", ""),
            "cpu": round(cpu.get(key, 0.0), 4),
            "mem": round(mem.get(key, 0.0), 1),
            "owner_kind": owner_kind,
            # Unix seconds; how old a kube-system pod is decides whether CA
            # still treats it as a blocker.
            "created": int(created[key]) if key in created else None,
            # Workload key so a pod can be matched against the drain-blocked and
            # anti-affinity sets, which are keyed by workload rather than pod.
            "workload_key": f"{key[0]}/{name}/{kind}",
        })
    return out


def _build_hpas(instant) -> list[HPASpec]:
    info = instant.get("hpa_info", [])
    targets = instant.get("hpa_target", [])
    mins = instant_map(instant.get("hpa_min", []), ("namespace", "horizontalpodautoscaler"))
    maxs = instant_map(instant.get("hpa_max", []), ("namespace", "horizontalpodautoscaler"))

    specs: dict[tuple[str, str], HPASpec] = {}
    for series in info:
        m = series.get("metric", {})
        key = (m.get("namespace", ""), m.get("horizontalpodautoscaler", ""))
        specs[key] = HPASpec(
            namespace=key[0],
            name=key[1],
            target_kind=m.get("scaletargetref_kind", ""),
            target_name=m.get("scaletargetref_name", ""),
            min_replicas=int(mins.get(key, 0)),
            max_replicas=int(maxs.get(key, 0)),
        )
    for series in targets:
        m = series.get("metric", {})
        key = (m.get("namespace", ""), m.get("horizontalpodautoscaler", ""))
        spec = specs.setdefault(key, HPASpec(namespace=key[0], name=key[1]))
        pair = (m.get("metric_name", ""), m.get("metric_target_type", ""))
        if pair not in spec.targets:
            spec.targets.append(pair)
        if pair in (("cpu", "utilization"), ("memory", "utilization")):
            raw = series.get("value", [None, None])[1]
            if raw not in (None, "NaN"):
                # kube-state-metrics reports the target as a percentage.
                if pair[0] == "cpu":
                    spec.cpu_target = float(raw) / 100.0
                else:
                    spec.mem_target = float(raw) / 100.0
    return list(specs.values())


def _build_pdbs(instant) -> list[PDBState]:
    desired = instant_map(instant.get("pdb_desired", []), ("namespace", "poddisruptionbudget"))
    expected = instant_map(instant.get("pdb_expected", []), ("namespace", "poddisruptionbudget"))
    current = instant_map(instant.get("pdb_current", []), ("namespace", "poddisruptionbudget"))
    out = []
    for key in sorted(set(desired) | set(expected)):
        namespace, name = key
        out.append(
            PDBState(
                namespace=namespace,
                name=name,
                desired_healthy=int(desired.get(key, 0)),
                expected_pods=int(expected.get(key, 0)),
                # Absent stays None: a missing series is unknown, not healthy.
                current_healthy=int(current[key]) if key in current else None,
            )
        )
    return out


def _build_unhealthy_pods(instant) -> list[dict]:
    """Pods that are failing, with the owning workload and whatever reason was reported.

    A pod can be not Ready with no waiting reason -- a failing readiness probe
    on a running container -- so readiness is the list and reasons annotate it.
    """
    owners: dict[tuple[str, str], tuple[str, str]] = {}
    for series in instant.get("pod_owner", []):
        m = series.get("metric", {})
        owners[(m.get("namespace", ""), m.get("pod", ""))] = (
            m.get("owner_kind", ""), m.get("owner_name", ""),
        )

    reasons: dict[tuple[str, str], set[str]] = {}
    for source, tag in (("pod_waiting", ""), ("pod_init_waiting", " (init)")):
        for series in instant.get(source, []):
            m = series.get("metric", {})
            if m.get("reason", "") in q.STARTING_REASONS:
                continue  # starting, not failing -- the queries drop these too
            key = (m.get("namespace", ""), m.get("pod", ""))
            reasons.setdefault(key, set()).add(
                f"{m.get('container', '')}{tag}: {m.get('reason', '')}")

    pods = {
        (s.get("metric", {}).get("namespace", ""), s.get("metric", {}).get("pod", ""))
        for s in instant.get("pod_unready", [])
    } | set(reasons)

    out = []
    for namespace, pod in sorted(pods):
        if not pod:
            continue
        owner_kind, owner_name = owners.get((namespace, pod), ("", ""))
        name, kind = logical_workload(owner_kind, owner_name or pod)
        out.append({
            "namespace": namespace,
            "pod": pod,
            "workload_key": f"{namespace}/{name}/{kind}",
            "reasons": sorted(reasons.get((namespace, pod), set())) or ["not Ready"],
        })
    return out


def _build_workload_nodegroups(series_set) -> dict[str, dict[str, float]]:
    """Workload -> {node group: share of samples}.

    Pods move, so membership is time-varying. A share below ~0.9 means the
    workload spans groups and must be sized against both, never one.
    """
    counts: dict[str, dict[str, int]] = {}
    for (namespace, owner_kind, owner_name, nodegroup), values in series_set.values.items():
        if not nodegroup:
            continue
        name, kind = logical_workload(owner_kind, owner_name)
        wkey = f"{namespace}/{name}/{kind}"
        counts.setdefault(wkey, {}).setdefault(nodegroup, 0)
        counts[wkey][nodegroup] += len(values)
    return {
        wkey: {ng: n / total for ng, n in groups.items()}
        for wkey, groups in counts.items()
        if (total := sum(groups.values())) > 0
    }


def collect_pod_ages(snapshot, thanos_url: str, *, timeout: int = 120) -> int:
    """Backfill each placement row's creation time, at the snapshot's moment.

    Returns the number of rows filled. Read-only: one instant query.
    """
    import datetime as _dt

    from rightsizer.core.config import Scope as _Scope
    from rightsizer.sources.thanos import ThanosClient as _Client

    at = int(_dt.datetime.fromisoformat(snapshot.collected_at).timestamp())
    with _Client(thanos_url, timeout=timeout) as client:
        rows = client.query_many({"c": q.pod_created(_Scope.for_snapshot(snapshot))}, at=at)["c"]
    created = instant_map(rows, ("namespace", "pod"))
    filled = 0
    for group in (snapshot.placements or {}).values():
        for r in group:
            key = (r.get("namespace", ""), r.get("pod", ""))
            if key in created:
                r["created"] = int(created[key])
                filled += 1
    return filled
