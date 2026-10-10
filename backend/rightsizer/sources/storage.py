"""Node-local storage: what a node group's disks hold, and whether a new shape keeps it.

An instance-type change is priced on CPU and memory, but the disk moves with it.
On instance-store types (the ``d`` in ``m6id``) the node bootstrap RAIDs the
NVMe and kubelet, containerd and pod logs live on it -- on prod-us1 kubelet's
ephemeral capacity is exactly the 474 GB SSD of an m6id.2xlarge. So:

* a smaller size in the same family has a smaller disk (c6id.xlarge: 237 GB);
* a type without instance storage puts all of it on the EBS root volume;
* packing onto fewer nodes puts the same bytes on fewer disks.

None of those shows up in a CPU/memory packing. This measures the disk kubelet
writes to on each node over the window and checks every option against it.
Kubelet starts evicting pods at ``nodefs.available<10%`` / ``imagefs<15%``, so
a projected node above :data:`UNSAFE_SHARE` is an eviction storm, not a saving.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from rightsizer.sources import queries as q
from rightsizer.core.config import NODEGROUP_LABEL as NG
from rightsizer.core.config import Scope
from rightsizer.sources.thanos import ThanosClient

log = logging.getLogger(__name__)

GB = 1e9  # EC2 quotes instance storage in decimal GB
#: Kubelet's default hard eviction starts at 85% used on the image filesystem.
UNSAFE_SHARE = 0.85
WARN_SHARE = 0.70
#: A kubelet filesystem within this of the instance store's size is on it.
MATCH_TOLERANCE = 0.10
BUCKET_SECONDS = 21600


def collect_storage(snapshot, thanos_url: str, *, max_workers: int = 8,
                    timeout: int = 120) -> dict[str, dict[str, Any]]:
    """Per node group: kubelet disk size, usage over the window, ephemeral requests.

    Over the snapshot's own window, so it can be added to an existing snapshot
    without re-collecting everything else. A group with no node-exporter data is
    left out; the caller treats a missing group as "not measured", never as empty.
    """
    scope = Scope.for_snapshot(snapshot)
    end = int(dt.datetime.fromisoformat(snapshot.collected_at).timestamp())
    start = end - snapshot.window_days * 86400
    bucket = f"{BUCKET_SECONDS}s"

    with ThanosClient(thanos_url, max_workers=max_workers, timeout=timeout) as client:
        instant = client.query_many({
            "size": q.kubelet_fs_size_per_node(scope),
            "root": q.root_fs_size(scope),
            "eph_cap": q.nodegroup_ephemeral(scope, "kube_node_status_capacity"),
            "eph_alloc": q.nodegroup_ephemeral(scope, "kube_node_status_allocatable"),
            "eph_req": q._attach_nodegroup(q.pod_requests(scope, "ephemeral_storage"), scope),
        }, at=end)
        from concurrent.futures import ThreadPoolExecutor

        exprs = {
            "hot": q.kubelet_fs_used_by_group(scope, "max", bucket),
            "total": q.kubelet_fs_used_by_group(scope, "sum", bucket),
            "avg": q.kubelet_fs_used_by_group(scope, "avg", bucket),
            "share": q.kubelet_fs_used_share_max(scope, bucket),
        }
        with ThreadPoolExecutor(max_workers=len(exprs)) as pool:
            futures = {n: pool.submit(client.query_range, e, start, end, BUCKET_SECONDS)
                       for n, e in exprs.items()}
            ranged = {n: f.result() for n, f in futures.items()}

    def by_group(rows) -> dict[str, float]:
        return {r["metric"].get(NG, ""): float(r["value"][1]) for r in rows if r["metric"].get(NG)}

    def series(rows) -> dict[str, list[float]]:
        return {
            r["metric"].get(NG, ""): [float(v) for _, v in r.get("values", []) if v not in (None, "NaN")]
            for r in rows if r["metric"].get(NG)
        }

    sizes: dict[str, list[float]] = {}
    for r in instant["size"]:
        g = r["metric"].get(NG)
        if g:
            sizes.setdefault(g, []).append(float(r["value"][1]))
    reqs: dict[str, list[float]] = {}
    for r in instant["eph_req"]:
        g = r["metric"].get(NG)
        if g:
            reqs.setdefault(g, []).append(float(r["value"][1]))
    root = by_group(instant["root"])
    eph_cap = by_group(instant["eph_cap"])
    eph_alloc = by_group(instant["eph_alloc"])
    hot, total, share = series(ranged["hot"]), series(ranged["total"]), series(ranged["share"])
    avg = series(ranged["avg"])

    out: dict[str, dict[str, Any]] = {}
    for g, per_node in sizes.items():
        per_node.sort()
        fs = per_node[len(per_node) // 2]
        tot = total.get(g, [])
        # No usage series is a failed or missing query, not an empty disk.
        used_known = bool(tot and hot.get(g))
        out[g] = {
            "usage_measured": used_known,
            "nodes_measured": len(per_node),
            "fs_gb": round(fs / GB, 1),
            "root_gb": round(root.get(g, 0.0) / GB, 1),
            # kubelet has no mount of its own and sits on the root volume
            "on_root": abs(fs - root.get(g, -1.0)) < GB,
            "eph_capacity_gb": round(eph_cap.get(g, 0.0) / GB, 1),
            "eph_allocatable_gb": round(eph_alloc.get(g, 0.0) / GB, 1),
            "hot_peak_gb": round(max(hot.get(g, [0.0])) / GB, 2),
            "hot_peak_share": round(max(share.get(g, [0.0])), 4),
            "total_peak_gb": round(max(tot or [0.0]) / GB, 2),
            "total_avg_gb": round(sum(tot) / len(tot) / GB, 2) if tot else 0.0,
            # Per node, not per group: an autoscaled pool's total peaks when it
            # is scaled out, and dividing that by a packed node count overstates
            # it -- prod-eu1's engine pool peaked at 2.4 TB across 30-odd nodes.
            "avg_node_peak_gb": round(max(avg.get(g, [0.0])) / GB, 2),
            "eph_requests_gb": round(sum(reqs.get(g, [])) / GB, 2),
            "eph_request_max_gb": round(max(reqs.get(g, [0.0])) / GB, 2),
            "eph_request_pods": len(reqs.get(g, [])),
        }
    return out


def describe_disk(inst) -> str:
    """"1 × 474 GB NVMe SSD", or "none (EBS only)"."""
    if inst is None:
        return "unknown"
    if not getattr(inst, "storage_known", False):
        return "not described"
    if not inst.storage_gb:
        return f"none ({getattr(inst, 'root_volume', 'EBS')} only)"
    kind = (inst.storage_type or "").upper() or "disk"
    nvme = "NVMe " if inst.storage_nvme in ("required", "supported") else ""
    count = inst.storage_disks or 1
    each = inst.storage_gb / count
    return f"{count} × {each:,.0f} GB {nvme}{kind}"


def assess(measured: dict | None, current, proposed, *, nodes_now: int,
           nodes_after: int) -> dict[str, Any]:
    """Local storage before and after one option, and whether it still fits.

    Usage is taken as unchanged -- the same images, emptyDirs and logs -- and
    spread over the nodes the option leaves. The fullest node today stays at
    least as full, so the projection for the hottest node is the larger of its
    own peak and the new average: the cautious reading.
    """
    before_disk = describe_disk(current)
    after_disk = describe_disk(proposed)
    out: dict[str, Any] = {
        "diskBefore": before_disk, "diskAfter": after_disk,
        "ssdBefore": _is_ssd(current), "ssdAfter": _is_ssd(proposed),
        "storeGbBefore": getattr(current, "storage_gb", 0) or 0,
        "storeGbAfter": getattr(proposed, "storage_gb", 0) or 0,
        "notes": [], "verdict": "unknown",
    }
    lost_store = bool(out["storeGbBefore"]) and not out["storeGbAfter"] \
        and getattr(proposed, "storage_known", False)
    if _is_ssd(current) and getattr(proposed, "storage_known", False) and not _is_ssd(proposed) \
            and out["storeGbAfter"]:
        out["notes"].append("the new type's instance storage is HDD, not SSD")

    if not measured:
        if lost_store:
            out["verdict"] = "unsafe"
            out["notes"].append(
                "the current type has local NVMe and the proposed one has none; disk use was "
                "not measured, so anything kept there would move to the EBS root volume unchecked")
        else:
            out["notes"].append("node disk usage was not measured for this group -- re-run "
                                "`rightsizer storage` against the snapshot")
        return out

    fs = measured["fs_gb"]
    usage_known = measured.get("usage_measured", True)
    if not usage_known:
        out["notes"].append("disk size is known but its usage over the window was not measured; "
                            "re-run `rightsizer storage`")
    store_now = out["storeGbBefore"]
    on_store = bool(store_now) and not measured.get("on_root") \
        and abs(fs - store_now) <= store_now * MATCH_TOLERANCE
    out["kubeletOnInstanceStore"] = on_store
    # The share of a raw disk kubelet actually gets, measured on today's nodes.
    usable = fs / store_now if on_store and store_now else 1.0
    if on_store:
        cap_after = out["storeGbAfter"] * usable if out["storeGbAfter"] else measured.get("root_gb", 0.0)
    else:
        # EBS root: the launch template sets its size, not the instance type.
        cap_after = fs
    out["perNodeGbBefore"] = fs
    out["perNodeGbAfter"] = round(cap_after, 1)

    hot = measured["hot_peak_gb"]
    n_now, n_after = max(nodes_now, 1), max(nodes_after, 1)
    # What today's pods hold: the fullest the average node got, on today's
    # nodes. Snapshots collected before that was recorded fall back to the
    # group's peak total, which overstates an autoscaled pool.
    total = (measured["avg_node_peak_gb"] * n_now if measured.get("avg_node_peak_gb")
             else measured["total_peak_gb"])
    out["totalUsedPeakGb"] = total if usage_known else None
    out["before"] = None if not usage_known else {
        "avgShare": round(total / (n_now * fs), 4) if fs else None,
        "hotShare": measured.get("hot_peak_share") or (round(hot / fs, 4) if fs else None),
        "hotGb": hot,
    }
    avg_after = total / n_after
    hot_after = max(hot, avg_after)
    out["after"] = None if not usage_known else {
        "avgShare": round(avg_after / cap_after, 4) if cap_after else None,
        "hotShare": round(hot_after / cap_after, 4) if cap_after else None,
        "hotGb": round(hot_after, 2),
    }

    # Ephemeral-storage requests are a scheduling constraint like CPU: the
    # scheduler will not place a pod on a node whose allocatable cannot hold it.
    alloc_ratio = (measured["eph_allocatable_gb"] / measured["eph_capacity_gb"]
                   if measured.get("eph_capacity_gb") else 0.9)
    alloc_after = cap_after * alloc_ratio
    out["ephRequestsGb"] = measured["eph_requests_gb"]
    out["ephAllocatableGbAfter"] = round(alloc_after, 1)
    req_fail = (measured["eph_request_max_gb"] > alloc_after
                or measured["eph_requests_gb"] > alloc_after * n_after)

    share_after = (out["after"] or {}).get("hotShare") or 0.0
    verdict = "ok" if usage_known else "unknown"
    if lost_store and on_store:
        verdict = "unsafe"
        out["notes"].append(
            f"kubelet, images and pod logs live on the {store_now:,.0f} GB local NVMe today; "
            f"the proposed type has none, so they move to the {measured.get('root_gb', 0):,.0f} GB "
            "EBS root volume")
    if req_fail:
        verdict = "unsafe"
        out["notes"].append(
            f"pods request {measured['eph_requests_gb']:,.1f} GB of ephemeral storage "
            f"(largest {measured['eph_request_max_gb']:,.1f} GB); {n_after} node(s) at "
            f"{alloc_after:,.0f} GB allocatable cannot hold them")
    if share_after >= UNSAFE_SHARE:
        verdict = "unsafe"
        out["notes"].append(
            f"the fullest node would reach {share_after:.0%} of its disk -- kubelet evicts pods "
            f"from {UNSAFE_SHARE:.0%}")
    elif share_after >= WARN_SHARE and verdict in ("ok", "unknown"):
        verdict = "warn"
        out["notes"].append(f"the fullest node would reach {share_after:.0%} of its disk")
    if on_store and out["storeGbAfter"] and out["storeGbAfter"] < store_now and verdict == "ok":
        out["notes"].append(
            f"smaller local disk per node ({store_now:,.0f} → {out['storeGbAfter']:,.0f} GB), "
            "but the measured use still fits")
    out["verdict"] = verdict
    return out


def _is_ssd(inst) -> bool:
    return bool(inst and getattr(inst, "storage_gb", 0) and (inst.storage_type or "").lower() == "ssd")


#: Storage classes whose volumes any node can mount. Everything else -- EBS
#: (gp2, gp3, io1), a local PV -- is attached in one zone, or to one node.
SHARED_CLASSES = ("efs", "nfs", "fsx", "s3")


def collect_volumes(snapshot, thanos_url: str, *, max_workers: int = 8,
                    timeout: int = 120) -> dict[str, list[dict]]:
    """``namespace/pod`` -> the claims it mounts, with storage class and whether zonal.

    A pod with an EBS-backed claim can only restart in its volume's zone,
    whatever else the scheduler would allow -- the constraint that decides
    whether three postgres StatefulSets in three zones can share one node.
    Read at the snapshot's own instant, like the placements it qualifies.
    """
    scope = Scope.for_snapshot(snapshot)
    at = int(dt.datetime.fromisoformat(snapshot.collected_at).timestamp())
    with ThanosClient(thanos_url, max_workers=max_workers, timeout=timeout) as client:
        got = client.query_many({"claims": q.pod_claims(scope), "classes": q.claim_classes(scope)},
                                at=at)
    classes = {(r["metric"].get("namespace", ""), r["metric"].get("persistentvolumeclaim", "")):
               r["metric"].get("storageclass", "") for r in got["classes"]}
    out: dict[str, list[dict]] = {}
    for r in got["claims"]:
        m = r["metric"]
        ns, claim = m.get("namespace", ""), m.get("persistentvolumeclaim", "")
        cls = classes.get((ns, claim), "")
        out.setdefault(f"{ns}/{m.get('pod', '')}", []).append({
            "claim": claim, "class": cls,
            "zonal": not any(s in cls.lower() for s in SHARED_CLASSES),
        })
    return out


def prune_finished(snapshot, thanos_url: str, *, max_workers: int = 8, timeout: int = 120) -> int:
    """Drop Succeeded and Failed pods from the snapshot's placements.

    Read at the snapshot's own instant. Returns how many were removed. A
    finished pod counted as placed inflates a node's pod count past max-pods
    and hands the replay pods to move that no longer exist -- 27 of the 317
    pods on prod-us2's services-2 were completed Jobs.
    """
    scope = Scope.for_snapshot(snapshot)
    at = int(dt.datetime.fromisoformat(snapshot.collected_at).timestamp())
    with ThanosClient(thanos_url, max_workers=max_workers, timeout=timeout) as client:
        rows = client.query_instant(q.finished_pods(scope), at)
    done = {(r["metric"].get("namespace", ""), r["metric"].get("pod", "")) for r in rows}
    removed = 0
    for group, placed in (snapshot.placements or {}).items():
        keep = [r for r in placed if (r.get("namespace", ""), r.get("pod", "")) not in done]
        removed += len(placed) - len(keep)
        snapshot.placements[group] = keep
    return removed
