"""Per-workload usage trends, for reading the evidence behind a recommendation.

A recommendation says "p95 is 195m, so request 217m". This is the series that
produced it, so the number can be judged rather than taken.

**Why not one line per replica.** Over thirty days a busy workload churns through
hundreds of pod identities -- restarts, rollouts, scale events -- so "every
replica" is not the stable set of lines it sounds like. One workload here
produced 710 distinct pod names and 426KB for CPU alone. What a reader actually
wants from that picture is the *spread* between replicas and their *total*, and
those are better served directly:

* **bands** -- max, p95, median and min across replicas at each instant, so the
  gap between the hottest replica and the typical one is visible at a glance;
* **total** -- every replica summed, which is what the node group must hold and
  is emphatically not ``max x replicas``;
* **a sample of individual replicas**, the hottest few, for readers who want to
  see real lines rather than statistics.

Written as one file per container so the console stays small and fetches a trend
only when someone opens it.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from rightsizer.core.config import Scope, Tunables
from rightsizer.sources.thanos import ThanosClient

log = logging.getLogger(__name__)

# 6-hourly over the window: enough shape to judge a percentile, small enough that
# a container's trend is a few kilobytes.
TREND_STEP_SECONDS = 21600
SAMPLE_REPLICAS = 8
#: Resolution *inside* each plotted step. Matches ``Tunables.step_seconds``, the
#: resolution the snapshot's own statistics are computed at, so a band on the
#: chart and the number in the recommendation come from the same samples.
BUCKET_RESOLUTION = 300
#: The CPU rate window: the snapshot's own (``Tunables.rate_window_seconds``),
#: so the chart reads the series the recommendation and the "peak" figure read.
#: A 5-minute rate averaged a one-minute 640m burst on api-server down to 133m,
#: and the chart showed no peak the header named.
CPU_RATE_WINDOW = f"{Tunables().rate_window_seconds}s"

#: Bumped whenever a change alters the numbers a trend file reports. The console
#: refuses to plot a file below this rather than drawing last week's method as
#: though it were this week's: a chart that silently disagrees with the
#: recommendation it is meant to justify is worse than no chart.
#:
#: 1. 6-hourly instant samples (under-reported every peak between samples).
#: 2. bucketed over the step at :data:`BUCKET_RESOLUTION`.
#: 3. restricted to the workload's own pods (2 plotted every container of
#:    that name in the namespace, whoever owned it).
TREND_SCHEMA = 3


@dataclass(frozen=True)
class TrendKey:
    namespace: str
    workload: str
    container: str
    #: The controller kind, needed to find this workload's pods. Without it the
    #: owner filter falls back to a looser match; see :func:`owner_selector`.
    kind: str = ""

    @property
    def slug(self) -> str:
        raw = f"{self.namespace}__{self.workload}__{self.container}"
        return re.sub(r"[^A-Za-z0-9_.-]", "-", raw)


#: RE2 -- what PromQL uses -- rejects escapes it does not recognise, and `\-`
#: is one of them. `re.escape` emits exactly that, so a workload name with a
#: dash produced `parse error: unknown escape sequence U+002D` and the query
#: returned nothing. Escape only the characters RE2 treats as special.
_RE2_META = re.compile(r"([.+*?()\[\]{}^$|\\])")


def re2_escape(text: str) -> str:
    """Quote a literal for a PromQL (RE2) regex matcher."""
    return _RE2_META.sub(r"\\\1", text)


def owner_selector(kind: str, workload: str) -> str:
    """Matchers on ``kube_pod_owner`` selecting exactly this workload's pods.

    Namespace and container name do **not** identify a workload. Three separate
    postgres StatefulSets in one namespace all run a container called
    ``database``; selecting on those two labels alone plotted all three on one
    chart, and the hottest line belonged to a different database entirely --
    which read as the recommendation being wrong.

    PromQL regexes are fully anchored, so ``foo-[a-z0-9]+`` cannot match
    ``foo-bar-5dcfdd95df``: a hash contains no dash, so a workload whose name is
    a prefix of another's does not bleed into it.
    """
    esc = re2_escape(workload)
    if kind == "Deployment":
        # Pods belong to a ReplicaSet named <deployment>-<hash>.
        return f'owner_kind="ReplicaSet", owner_name=~"{esc}-[a-z0-9]+"'
    if kind == "CronJob":
        return f'owner_kind="Job", owner_name=~"{esc}-[0-9]+"'
    if kind:
        return f'owner_kind="{kind}", owner_name="{workload}"'
    # Kind unknown (an older snapshot): accept the workload itself or one
    # hash-suffixed level below it, which is still far tighter than no filter.
    return f'owner_name=~"{esc}(-[a-z0-9]+)?"'


def _series(client, expr, start, end, step, label):
    out: dict[str, list[tuple[int, float]]] = {}
    for item in client.query_range(expr, start, end, step):
        name = item.get("metric", {}).get(label, "")
        points = [
            (int(t), float(v))
            for t, v in item.get("values", [])
            if v not in (None, "NaN")
        ]
        if points:
            out[name] = points
    return out


def collect_trend(settings, key: TrendKey, *, resource: str) -> dict:
    """Bands, total and a replica sample for one container."""
    scope: Scope = settings.scope
    tun: Tunables = settings.tunables
    end = settings.end_ts or int(dt.datetime.now(dt.UTC).timestamp())
    start = end - tun.window_seconds

    sel = scope.matchers(
        f'namespace="{key.namespace}"', f'container="{key.container}"', 'pod!=""'
    )
    # Restrict to the pods this workload owns. An inner join is right here --
    # a pod with no owner is not part of any workload, so dropping it is the
    # intent rather than the silent data loss it would be elsewhere.
    owner = scope.matchers(f'namespace="{key.namespace}"', 'pod!=""',
                           owner_selector(key.kind, key.workload))
    mine = f'max by (namespace,pod) (kube_pod_owner{{{owner}}})'
    if resource == "cpu":
        per_pod = (
            f"sum(rate(container_cpu_usage_seconds_total{{{sel}}}[{CPU_RATE_WINDOW}]))"
            " by (namespace,pod)"
        )
        scale = 1.0
    else:
        # The same inner max_over_time the snapshot uses, so the chart and the
        # recommendation are reading the identical series.
        per_pod = (
            "max_over_time((sum(container_memory_working_set_bytes"
            f"{{{sel}}}) by (namespace,pod))[5m:1m])"
        )
        scale = 1024**3

    per_pod = f"({per_pod}) * on(namespace,pod) group_left() ({mine})"

    with ThanosClient(
        settings.thanos_url, max_workers=tun.max_workers, timeout=tun.http_timeout
    ) as client:
        # Every band is bucketed over the step, not sampled at it.
        #
        # A 6-hourly *instant* read of a 5-minute series plots 120 of the 8,640
        # points the recommendation is computed from, so it misses 71 of every
        # 72 -- and a spike is exactly what it misses. Measured on one container
        # here: instant sampling showed a 194m peak where the real peak, and the
        # figure the recommendation used, was 529m. The chart was not wrong
        # about what it drew; it was wrong about calling it the maximum.
        #
        # Each band keeps its own meaning across the bucket: the envelope edges
        # take the extremes, the middle takes the mean. Costs nothing -- the
        # subquery measured slightly *faster* than the instant read.
        b = f"[{TREND_STEP_SECONDS}s:{BUCKET_RESOLUTION}s]"
        aggregates = {
            "max": f"max_over_time((max({per_pod})){b})",
            "p95": f"max_over_time((quantile(0.95, {per_pod})){b})",
            "median": f"avg_over_time((quantile(0.5, {per_pod})){b})",
            "min": f"min_over_time((min({per_pod})){b})",
            "total": f"max_over_time((sum({per_pod})){b})",
            "replicas": f"max_over_time((count({per_pod})){b})",
        }
        # The aggregates are independent, so issue them together rather than
        # waiting on each in turn.
        from concurrent.futures import ThreadPoolExecutor

        jobs = dict(aggregates)
        # Sample lines are bucketed the same way, or an individual replica would
        # appear to sit below a band that is supposed to be its own maximum.
        jobs["__pods__"] = f"max_over_time(({per_pod}){b})"
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = {
                name: pool.submit(
                    _series, client, expr, start, end, TREND_STEP_SECONDS,
                    "pod" if name == "__pods__" else "__none__",
                )
                for name, expr in jobs.items()
            }
            results = {name: f.result() for name, f in futures.items()}

        # `replicas` is a count of pods, not an amount of the resource, so it
        # must not be divided by the byte scale -- doing so reported every
        # memory trend as having 0.0000 replicas.
        bands = {
            name: [
                [t, round(v / (1.0 if name == "replicas" else scale), 4)]
                for t, v in next(iter(results[name].values()), [])
            ]
            for name in aggregates
        }
        pods = results["__pods__"]
        hottest = sorted(
            pods.items(), key=lambda kv: -max(v for _, v in kv[1])
        )[:SAMPLE_REPLICAS]
        sample = [
            {
                "pod": name,
                "points": [[t, round(v / scale, 4)] for t, v in points],
            }
            for name, points in hottest
        ]

    return {
        "resource": resource,
        "unit": "cores" if resource == "cpu" else "GiB",
        "step": TREND_STEP_SECONDS,
        "start": start,
        "end": end,
        "bands": bands,
        "sample": sample,
        "podIdentities": len(pods),
    }


#: Controller of a pod -> the workload it belongs to, in PromQL: the owner
#: name with the suffix its controller adds stripped (a ReplicaSet's
#: ``-<hash>``, a Job's ``-<timestamp>``), as ``owner_selector`` matches it.
_OWNERS = (
    ("ReplicaSet", "(.+)-[a-z0-9]+", "Deployment"),
    ("Job", "(.+)-[0-9]+", "CronJob"),
)
#: Time per request for the cluster-wide queries; a multiple of the step so
#: chunks meet on a plotted point instead of shifting the grid.
BATCH_CHUNK_SECONDS = 5 * 86400


def _batch_exprs(scope: Scope, resource: str) -> dict[str, str]:
    """The six bands for every container in the cluster, one query each.

    The same series, the same owner restriction and the same bucketing as
    :func:`collect_trend`, grouped by workload and container instead of
    filtered to one -- so a band is identical whichever way it was collected.
    """
    own = scope.matchers('pod!=""')
    owners = " or ".join(
        f'label_replace(label_replace(max by (namespace,pod,owner_name) '
        f'(kube_pod_owner{{{own}, owner_kind="{kind}"}}), "workload", "$1", "owner_name", '
        f'"{rx}"), "wkind", "{wkind}", "owner_name", ".*")'
        for kind, rx, wkind in _OWNERS
    )
    # Every other controller -- StatefulSet, DaemonSet, an operator's own kind
    # such as the GPU operator's ClusterPolicy -- names its pods' workload
    # directly, as ``owner_selector`` matches it.
    direct = "|".join(kind for kind, _rx, _w in _OWNERS)
    owners += (
        f' or label_replace(label_replace(max by (namespace,pod,owner_kind,owner_name) '
        f'(kube_pod_owner{{{own}, owner_kind!~"{direct}|<none>|"}}), "workload", "$1", '
        f'"owner_name", "(.+)"), "wkind", "$1", "owner_kind", "(.+)")'
    )
    mine = f"max by (namespace,pod,workload,wkind) ({owners})"
    sel = scope.matchers('container!=""', 'container!="POD"', 'pod!=""')
    if resource == "cpu":
        per_pod = ("sum by (namespace,pod,container) "
                   f"(rate(container_cpu_usage_seconds_total{{{sel}}}[{CPU_RATE_WINDOW}]))")
    else:
        per_pod = ("max_over_time((sum by (namespace,pod,container) "
                   f"(container_memory_working_set_bytes{{{sel}}}))[5m:1m])")
    per_pod = f"({per_pod}) * on(namespace,pod) group_left(workload,wkind) ({mine})"
    g = "namespace,workload,wkind,container"
    b = f"[{TREND_STEP_SECONDS}s:{BUCKET_RESOLUTION}s]"
    return {
        "max": f"max_over_time((max by ({g}) ({per_pod})){b})",
        "p95": f"max_over_time((quantile by ({g}) (0.95, {per_pod})){b})",
        "median": f"avg_over_time((quantile by ({g}) (0.5, {per_pod})){b})",
        "min": f"min_over_time((min by ({g}) ({per_pod})){b})",
        "total": f"max_over_time((sum by ({g}) ({per_pod})){b})",
        "replicas": f"max_over_time((count by ({g}) ({per_pod})){b})",
    }


def collect_batched(settings, keys: list[TrendKey]) -> dict[str, dict]:
    """Bands for many containers at once: {slug: {"cpu": ..., "memory": ...}}.

    Per container, a trend is 14 range queries -- about 25 s of Thanos time,
    and an hour for the 450 containers of one cluster. Grouped, it is 12
    queries for the whole cluster, chunked over time. What grouping cannot
    give is the per-replica sample (every pod identity of every workload for
    a month), so these files carry bands only; the console then offers the
    bands view, which is what the recommendation is read from.
    """
    from concurrent.futures import ThreadPoolExecutor

    scope: Scope = settings.scope
    tun: Tunables = settings.tunables
    end = settings.end_ts or int(dt.datetime.now(dt.UTC).timestamp())
    start = end - tun.window_seconds
    windows = []
    cur = start
    while cur < end:
        windows.append((cur, min(cur + BATCH_CHUNK_SECONDS, end)))
        cur += BATCH_CHUNK_SECONDS
    jobs = [(res, band, expr, w) for res in ("cpu", "memory")
            for band, expr in _batch_exprs(scope, res).items() for w in windows]
    # (namespace, workload, kind, container) -> resource -> band -> {t: v}
    got: dict[tuple, dict[str, dict[str, dict[int, float]]]] = {}
    with ThanosClient(settings.thanos_url, max_workers=tun.max_workers,
                      timeout=max(tun.http_timeout, 300)) as client:
        log.info("batch: %d container(s) in %d quer%s (%d bands x %d windows of %d days), "
                 "%d at a time", len(keys), len(jobs), "y" if len(jobs) == 1 else "ies",
                 len(jobs) // max(len(windows), 1), len(windows), BATCH_CHUNK_SECONDS // 86400,
                 tun.max_workers)
        done = [0]

        def run(j):
            t0 = time.monotonic()
            rows = client.query_range(j[2], j[3][0], j[3][1], TREND_STEP_SECONDS)
            done[0] += 1
            log.info("batch: [%d/%d] %s %s %s..%s -> %d series in %.1fs", done[0], len(jobs), j[0], j[1],
                     dt.datetime.fromtimestamp(j[3][0], dt.UTC).strftime("%d %b"),
                     dt.datetime.fromtimestamp(j[3][1], dt.UTC).strftime("%d %b"),
                     len(rows), time.monotonic() - t0)
            return j, rows

        with ThreadPoolExecutor(max_workers=tun.max_workers) as pool:
            results = pool.map(run, jobs)
            for (res, band, _expr, _w), rows in results:
                for r in rows:
                    m = r.get("metric", {})
                    ident = (m.get("namespace", ""), m.get("workload", ""), m.get("wkind", ""),
                             m.get("container", ""))
                    pts = got.setdefault(ident, {}).setdefault(res, {}).setdefault(band, {})
                    for ts, v in r.get("values", []):
                        if v not in (None, "NaN"):
                            pts[int(ts)] = float(v)

    out: dict[str, dict] = {}
    for key in keys:
        series = got.get((key.namespace, key.workload, key.kind, key.container))
        if not series:
            continue
        doc = {}
        for res, scale, unit in (("cpu", 1.0, "cores"), ("memory", 1024**3, "GiB")):
            bands = series.get(res, {})
            doc[res] = {
                "resource": res, "unit": unit, "step": TREND_STEP_SECONDS,
                "start": start, "end": end,
                "bands": {
                    band: [[ts, round(v / (1.0 if band == "replicas" else scale), 4)]
                           for ts, v in sorted(bands.get(band, {}).items())]
                    for band in ("max", "p95", "median", "min", "total", "replicas")
                },
                "sample": [],
                "podIdentities": 0,
                "batched": True,
            }
        out[key.slug] = doc
    return out


def write_trends(
    settings, keys: list[TrendKey], out_dir: Path, refresh: bool = False,
    parallel: int = 1, detailed: set[str] | None = None,
) -> tuple[int, int, list[str]]:
    """One file per container, holding both resources.

    ``detailed`` names the containers collected one by one, with their
    per-replica sample -- those a recommendation touches. Every other one is
    collected in one cluster-wide batch (:func:`collect_batched`): bands only,
    at a fraction of the cost. A container already on disk is left alone
    unless ``refresh`` is set, except that a batched file is upgraded once its
    container needs the detail.
    Returns ``(written, skipped, empty)`` -- the last being the slugs whose
    query produced no data at all, which is a collection failure rather than a
    workload with nothing to report.
    """
    from concurrent.futures import ThreadPoolExecutor

    out_dir.mkdir(parents=True, exist_ok=True)
    # A pod with no controller has no workload to chart under.
    keys = [k for k in keys if k.workload]
    # Not told which need detail: all of them, one by one, as before batching.
    detailed = {k.slug for k in keys} if detailed is None else set(detailed)

    def stale(key: TrendKey) -> bool:
        target = out_dir / f"{key.slug}.json"
        if refresh or not target.exists():
            return True
        # Batched earlier, detail wanted now.
        return key.slug in detailed and '"batched": true' in target.read_text()[:600]

    todo = [k for k in keys if stale(k)]
    skipped = len(keys) - len(todo)
    empty: list[str] = []
    rest = [k for k in todo if k.slug not in detailed]
    log.info("plan: %d container(s): %d to write (%d detailed one by one, %d in one batch), "
             "%d already present%s", len(keys), len(todo), len(todo) - len(rest), len(rest), skipped,
             "" if refresh else " (--refresh rewrites them)")
    t_batch = time.monotonic()
    batched = collect_batched(settings, rest) if rest else {}
    if rest:
        log.info("batch: %d of %d container(s) have series, in %.1fs", len(batched), len(rest),
                 time.monotonic() - t_batch)
    n_detail = sum(1 for k in todo if k.slug in detailed)
    detail_done = [0]

    def one(key: TrendKey) -> bool:
        """Write one container's file; True when the query matched nothing."""
        target = out_dir / f"{key.slug}.json"
        if key.slug in detailed:
            t0 = time.monotonic()
            series = {"cpu": collect_trend(settings, key, resource="cpu"),
                      "memory": collect_trend(settings, key, resource="memory")}
            detail_done[0] += 1
            log.info("detail: [%d/%d] %s in %.1fs", detail_done[0], n_detail, key.slug,
                     time.monotonic() - t0)
        else:
            series = batched.get(key.slug) or {
                res: {"resource": res, "unit": "cores" if res == "cpu" else "GiB",
                      "step": TREND_STEP_SECONDS, "bands": {"max": []}, "sample": [],
                      "podIdentities": 0, "batched": True}
                for res in ("cpu", "memory")}
        payload = {
            "schema": TREND_SCHEMA,
            "namespace": key.namespace,
            "kind": key.kind,
            "workload": key.workload,
            "container": key.container,
            "env": settings.scope.env,
            # Bands only, from the cluster-wide batch: no per-replica sample.
            "batched": key.slug not in detailed,
            **series,
        }
        # An empty trend is not a trend. A malformed matcher returns HTTP 400,
        # the client logs a warning and hands back nothing, and the run writes a
        # well-formed file with no series in it -- 115 of them, once, and exited
        # 0. Count them so the caller can refuse to call that a success.
        # Written whole to a temporary name and renamed, so an interrupted run
        # never leaves a half file the console would then refuse.
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(target)
        log.debug("wrote %s", target)
        return not payload["cpu"]["bands"].get("max") and not payload["memory"]["bands"].get("max")

    # Several containers at once: one at a time, 450 of them took an hour.
    quiet = 0
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        for key, was_empty in zip(todo, pool.map(one, todo)):
            if not was_empty:
                continue
            # A batched container with no series is usually real: a CronJob
            # whose pods finish between two scrapes leaves one sample a month.
            # Only a detailed one coming back empty -- or the whole batch --
            # says the query itself is broken.
            if key.slug in detailed:
                empty.append(key.slug)
            else:
                quiet += 1
    written = len(todo)
    if rest and quiet == len(rest):
        empty.extend(k.slug for k in rest)
    elif quiet:
        log.info("%d container(s) have no usage series over the window -- pods too short-lived "
                 "to be scraped; their files say so", quiet)
    if empty:
        log.warning(
            "%d of %d trend(s) came back with no series: %s%s",
            len(empty), written, ", ".join(empty[:3]),
            " ..." if len(empty) > 3 else "",
        )
    return written, skipped, empty
