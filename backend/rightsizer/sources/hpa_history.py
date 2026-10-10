"""Per-minute history for each CPU HPA, added to a frozen snapshot.

What the replay in :mod:`hpa_replay` needs and the 5-minute usage series cannot
give it:

* ``cpu`` -- pod-level CPU (every container, as the HPA sums it) at a 1-minute
  rate, every minute. The controller reacts to about that; a 5-minute rate
  averaged ingest-worker's 6.1-core burst down to 2.5.
* ``desired`` -- what the real HPA asked for, which is the ground truth the
  replay is validated against and the honest "observed" line: pod counts
  include both sides of every rollout.
* ``ready`` -- available replicas, from which the time new pods take to become
  ready is measured rather than assumed.

Collected against the snapshot's own window, so it can be added to an existing
snapshot without moving any other number in it. Read-only, like everything else.
"""

from __future__ import annotations

import datetime as dt
import logging
from concurrent.futures import ThreadPoolExecutor

from rightsizer.core.config import Scope
from rightsizer.engine.hpa_replay import (
    Behavior,
    infer_behavior,
    measure_lag,
    parse_behavior,
    rle_decode,
    rle_encode,
)
from rightsizer.sources.kube import _kubectl, has_access
from rightsizer.core.models import Snapshot
from rightsizer.sources.shell import run_json
from rightsizer.sources.thanos import ThanosClient
from rightsizer.sources.trends import owner_selector

log = logging.getLogger(__name__)

STEP = 60
#: 72h at 1m is 4,320 points, well inside Prometheus' 11,000 per series.
CHUNK_HOURS = 72


def _queries(scope: Scope, hpa, kind: str) -> dict[str, str]:
    ns = f'namespace="{hpa.namespace}"'
    owner = scope.matchers(ns, 'pod!=""', owner_selector(kind, hpa.target_name))
    usage = scope.matchers(ns, 'pod!=""', 'container!=""', 'container!="POD"')
    cpu = (
        f"sum(sum by (namespace,pod) (rate(container_cpu_usage_seconds_total{{{usage}}}[1m]))"
        f" * on(namespace,pod) group_left() max by (namespace,pod) (kube_pod_owner{{{owner}}}))"
    )
    hpa_sel = scope.matchers(ns, f'horizontalpodautoscaler="{hpa.name}"')
    if kind == "StatefulSet":
        sel = scope.matchers(ns, f'statefulset="{hpa.target_name}"')
        ready = f"max(kube_statefulset_status_replicas_ready{{{sel}}})"
    else:
        sel = scope.matchers(ns, f'deployment="{hpa.target_name}"')
        ready = f"max(kube_deployment_status_replicas_available{{{sel}}})"
    mem = (
        f"sum(sum by (namespace,pod) (container_memory_working_set_bytes{{{usage}}})"
        f" * on(namespace,pod) group_left() max by (namespace,pod) (kube_pod_owner{{{owner}}}))"
    )
    return {
        "cpu": cpu,
        # Working set, for an HPA that scales on memory. Fetched only for those.
        "mem": mem,
        "desired": f"max(kube_horizontalpodautoscaler_status_desired_replicas{{{hpa_sel}}})",
        "ready": ready,
        # The floor in force each minute: a replay is validated against it,
        # since floors do move -- api-server's went 100 -> 75.
        "min": f"max(kube_horizontalpodautoscaler_spec_min_replicas{{{hpa_sel}}})",
    }


def _aligned(client: ThanosClient, query: str, start: int, end: int) -> list[float | None]:
    """One scalar series, one slot per minute of the window, None where absent."""
    n = (end - start) // STEP
    out: list[float | None] = [None] * n
    windows = []
    cur = start
    while cur < end:
        nxt = min(cur + CHUNK_HOURS * 3600, end)
        windows.append((cur, nxt))
        cur = nxt
    with ThreadPoolExecutor(max_workers=min(len(windows), client.max_workers)) as pool:
        fetch = lambda w: client.query_range(query, w[0], w[1] - STEP, STEP)  # noqa: E731
        for result in pool.map(fetch, windows):
            for series in result or []:
                for t, raw in series.get("values", []):
                    i = (int(float(t)) - start) // STEP
                    if 0 <= i < n and raw not in (None, "NaN"):
                        out[i] = float(raw)
    return out


def read_behaviors(context: str | None) -> dict[str, dict]:
    """``spec.behavior`` per HPA, keyed ``namespace/name``, when kubectl can reach it."""
    if not has_access(context):
        return {}
    try:
        payload = run_json(_kubectl(context, "get", "hpa", "-A", "-o", "json"), timeout=60)
    except Exception as exc:  # noqa: BLE001 -- an unreachable cluster means "infer instead"
        log.warning("could not read HPA behavior via kubectl (%s); inferring from history", exc)
        return {}
    out = {}
    for item in (payload or {}).get("items", []):
        meta, spec = item.get("metadata", {}), item.get("spec", {})
        out[f"{meta.get('namespace')}/{meta.get('name')}"] = {
            "behavior": spec.get("behavior"),
            # Anything but a CPU utilization metric means a CPU replay cannot
            # stand in for this HPA's decisions.
            "cpu_only": all(m.get("type") == "Resource"
                            and m.get("resource", {}).get("name") == "cpu"
                            for m in spec.get("metrics") or []),
            # Resource utilization metrics the replay can model, with targets.
            "resources": {
                m["resource"]["name"]: m["resource"]["target"]["averageUtilization"] / 100
                for m in spec.get("metrics") or []
                if m.get("type") == "Resource"
                and m.get("resource", {}).get("name") in ("cpu", "memory")
                and (m["resource"].get("target") or {}).get("type") == "Utilization"
                and (m["resource"].get("target") or {}).get("averageUtilization")
            },
        }
    return out


def _ints(values: list[float | None]) -> list[int | None]:
    return [None if v is None else int(round(v)) for v in values]


def collect_hpa_history(
    snapshot: Snapshot, thanos_url: str, *, context: str | None = None,
    max_workers: int = 8, timeout: int = 180,
) -> dict[str, dict]:
    """History for every CPU-utilization HPA in the snapshot, over its window.

    Series an existing entry already holds are kept rather than fetched again,
    so adding a new series to the history costs one query per HPA, not a
    re-collection of the month.
    """
    end = int(dt.datetime.fromisoformat(snapshot.collected_at).timestamp()) // STEP * STEP
    start = end - snapshot.window_days * 86400
    scope = Scope.for_snapshot(snapshot)
    kinds = {(s.namespace, s.workload): s.kind for s in snapshot.usage}
    specs = read_behaviors(context)
    existing = snapshot.hpa_history or {}
    out: dict[str, dict] = {}

    with ThanosClient(thanos_url, max_workers=max_workers, timeout=timeout) as client:
        for hpa in snapshot.hpas:
            targets = hpa.resource_targets
            if not hpa.target_name or not targets:
                continue
            key = f"{hpa.namespace}/{hpa.name}"
            kind = kinds.get((hpa.namespace, hpa.target_name), hpa.target_kind or "Deployment")
            have = existing.get(key) if (existing.get(key) or {}).get("start") == start else None
            held = {}
            if have:
                held = {"cpu": [None if v is None else v / 1000 for v in have["cpu_m"]],
                        "desired": rle_decode(have["desired"]), "ready": rle_decode(have["ready"])}
                if have.get("min"):
                    held["min"] = rle_decode(have["min"])
            if have and have.get("mem_mib"):
                held["mem"] = [None if v is None else v * 2**20 for v in have["mem_mib"]]
            wanted = {n: q for n, q in _queries(scope, hpa, kind).items()
                      if n != "mem" or "memory" in targets}
            series = {name: held[name] if name in held else _aligned(client, q, start, end)
                      for name, q in wanted.items()}
            if not any(v is not None for v in series["cpu"]):
                log.info("no CPU history for %s; skipped", key)
                continue
            desired = _ints(series["desired"])
            ready = _ints(series["ready"])
            spec = specs.get(key)
            if spec is not None:
                behavior = parse_behavior(spec["behavior"])
                cpu_only = spec["cpu_only"]
            elif have and have["behavior"].get("source") == "spec":
                # Read from the cluster last time; no context now is no reason
                # to throw that away for an inference.
                behavior = Behavior.from_dict(have["behavior"])
                cpu_only = have.get("cpu_only", True)
            else:
                behavior = infer_behavior(desired, STEP)
                cpu_only = list(hpa.targets) == [("cpu", "utilization")]
            lag = measure_lag(desired, ready)
            out[key] = {
                "start": start,
                "step": STEP,
                # Millicores, as integers: a third the size of floats in JSON.
                "cpu_m": [None if v is None else int(round(v * 1000)) for v in series["cpu"]],
                "desired": rle_encode(desired),
                "ready": rle_encode(ready),
                "min": rle_encode(_ints(series["min"])),
                "behavior": behavior.to_dict(),
                "lag_minutes": lag,
                "cpu_only": cpu_only,
                # The resource targets the replay drives, e.g. {"cpu": 0.7, "memory": 0.8}.
                "targets": (spec or {}).get("resources") or targets,
            }
            if "mem" in series:
                # MiB, as integers.
                out[key]["mem_mib"] = [None if v is None else int(round(v / 2**20)) for v in series["mem"]]
            log.info("hpa history %s: behavior %s, lag %s", key, behavior.source, lag)
    return out
