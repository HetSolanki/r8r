"""The cluster as Cluster Autoscaler and kube-scheduler see it: pods, nodes, budgets.

Both controllers decide per *pod*, from the live objects -- each container's
requests, host ports, affinity, spread constraints, volumes, priority, and each
PDB's current ``disruptionsAllowed``. A workload template or a metric is a
stand-in for those; this reads the objects themselves, once, so the replay in
:mod:`rightsizer.engine.autoscaler` and :mod:`rightsizer.engine.kscheduler` runs on what the
real controllers run on.

Read-only by construction: every call is a ``kubectl get`` or an
``aws ... describe-*`` through :func:`rightsizer.sources.shell.run_json`, whose guard
refuses anything else. Nothing here can scale, cordon, evict or terminate.

The result is a plain dict (JSON-serialisable) kept on the snapshot as
``facts.kube``:

* ``pods`` -- one record per pod, compacted (see :func:`_pod`);
* ``nodes`` -- labels, taints, allocatable, readiness, images, annotations;
* ``csinodes`` -- per-node CSI attach limits;
* ``pvcs`` / ``pvs`` -- claim -> volume -> CSI driver and zone;
* ``pdbs`` -- spec *and* status;
* ``namespaces`` -- labels, for namespaceSelector;
* ``services`` / ``owners`` -- the selectors kube-scheduler's default spread uses;
* ``asgs`` -- each Auto Scaling group's size and Cluster Autoscaler option tags;
* ``ca`` -- the running Cluster Autoscaler's image and flags.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
from contextlib import contextmanager
from typing import Any

from rightsizer.sources.kube import SAFE_TO_EVICT, SAFE_TO_EVICT_LOCAL_VOLUMES, SCALE_DOWN_DISABLED, _kubectl
from rightsizer.sources.shell import run_json

log = logging.getLogger(__name__)

GPU = "nvidia.com/gpu"
#: ASG tag prefix for per-node-group Cluster Autoscaler options
#: (``cloudprovider/aws``: ``autoscalingOptionsTagsPrefix``).

_SUFFIX = {
    "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60,
    "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12,
    "P": 1e15, "E": 1e18,
}


def quantity(text: str | int | float | None) -> float:
    """A Kubernetes resource quantity ("100m", "1.5Gi", "2e3") as a number."""
    if text is None:
        return 0.0
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip()
    m = re.fullmatch(r"([+-]?[0-9.]+(?:[eE][+-]?[0-9]+)?)([A-Za-z]*)", s)
    if not m:
        return 0.0
    num, unit = m.groups()
    return float(num) * _SUFFIX.get(unit, 1.0)


def milli(text) -> int:
    """CPU in millicores, rounded up as Quantity.MilliValue does."""
    import math

    return int(math.ceil(quantity(text) * 1000 - 1e-9))


def whole(text) -> int:
    """Bytes (or a count), rounded up as Quantity.Value does."""
    import math

    return int(math.ceil(quantity(text) - 1e-9))


def _ts(text: str | None) -> int | None:
    if not text:
        return None
    return int(dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


def _selector(sel: dict | None) -> dict | None:
    """A LabelSelector, kept as {"matchLabels", "matchExpressions"}; None when absent."""
    if sel is None:
        return None
    return {"matchLabels": dict(sel.get("matchLabels") or {}),
            "matchExpressions": [{"key": e.get("key", ""), "operator": e.get("operator", ""),
                                  "values": list(e.get("values") or [])}
                                 for e in sel.get("matchExpressions") or []]}


def _node_term(term: dict) -> dict:
    return {"matchExpressions": [{"key": e.get("key", ""), "operator": e.get("operator", ""),
                                  "values": list(e.get("values") or [])}
                                 for e in term.get("matchExpressions") or []],
            "matchFields": [{"key": e.get("key", ""), "operator": e.get("operator", ""),
                             "values": list(e.get("values") or [])}
                            for e in term.get("matchFields") or []]}


def _pod_term(term: dict) -> dict:
    return {"labelSelector": _selector(term.get("labelSelector")),
            "namespaces": list(term.get("namespaces") or []),
            "namespaceSelector": _selector(term.get("namespaceSelector")),
            "topologyKey": term.get("topologyKey", ""),
            "matchLabelKeys": list(term.get("matchLabelKeys") or []),
            "mismatchLabelKeys": list(term.get("mismatchLabelKeys") or [])}


def _container(c: dict, *, init: bool = False) -> dict:
    req = (c.get("resources") or {}).get("requests") or {}
    out = {
        "name": c.get("name", ""),
        "cpu": milli(req["cpu"]) if "cpu" in req else None,       # millicores; None: not set
        "mem": whole(req["memory"]) if "memory" in req else None,  # bytes
        "gpu": whole(req.get(GPU, 0)),
        "eph": whole(req.get("ephemeral-storage", 0)),
        "image": c.get("image", ""),
        "ports": [{"port": int(p["hostPort"]), "proto": p.get("protocol", "TCP"),
                   "ip": p.get("hostIP", "") or "0.0.0.0"}
                  for p in c.get("ports") or [] if p.get("hostPort")],
    }
    if init:
        out["sidecar"] = c.get("restartPolicy") == "Always"
    return out


def _pod(p: dict) -> dict:
    """One pod, compacted to what the two controllers read."""
    meta, spec, status = p.get("metadata", {}), p.get("spec", {}), p.get("status", {})
    ann = meta.get("annotations") or {}
    owner = next((o for o in meta.get("ownerReferences") or [] if o.get("controller")), None)
    aff = spec.get("affinity") or {}
    na, pa, paa = aff.get("nodeAffinity") or {}, aff.get("podAffinity") or {}, aff.get("podAntiAffinity") or {}
    req = na.get("requiredDuringSchedulingIgnoredDuringExecution") or {}
    return {
        "ns": meta.get("namespace", ""),
        "name": meta.get("name", ""),
        "node": spec.get("nodeName", ""),
        "phase": status.get("phase", ""),
        "owner_kind": owner.get("kind", "") if owner else "",
        "owner_name": owner.get("name", "") if owner else "",
        "created": _ts(meta.get("creationTimestamp")),
        "deleting": _ts(meta.get("deletionTimestamp")),
        "grace": spec.get("terminationGracePeriodSeconds"),
        "priority": spec.get("priority"),
        "scheduler": spec.get("schedulerName", "default-scheduler"),
        "labels": dict(meta.get("labels") or {}),
        "safe_to_evict": ann.get(SAFE_TO_EVICT),
        "safe_local_volumes": ann.get(SAFE_TO_EVICT_LOCAL_VOLUMES),
        # Karpenter reads this from the pod, not its controller: operators and
        # charts often set it on the running pod only.
        "do_not_disrupt": ann.get("karpenter.sh/do-not-disrupt") == "true",
        "containers": [_container(c) for c in spec.get("containers") or []],
        "init": [_container(c, init=True) for c in spec.get("initContainers") or []],
        "overhead": {"cpu": milli((spec.get("overhead") or {}).get("cpu", 0)),
                     "mem": whole((spec.get("overhead") or {}).get("memory", 0))},
        "host_network": bool(spec.get("hostNetwork")),
        "node_selector": dict(spec.get("nodeSelector") or {}),
        "node_required": [_node_term(t) for t in req.get("nodeSelectorTerms") or []],
        "node_preferred": [{"weight": int(t.get("weight", 0)), "term": _node_term(t.get("preference") or {})}
                           for t in na.get("preferredDuringSchedulingIgnoredDuringExecution") or []],
        "pod_affinity": [_pod_term(t) for t in pa.get("requiredDuringSchedulingIgnoredDuringExecution") or []],
        "pod_anti": [_pod_term(t) for t in paa.get("requiredDuringSchedulingIgnoredDuringExecution") or []],
        "pod_affinity_pref": [{"weight": int(t.get("weight", 0)), "term": _pod_term(t.get("podAffinityTerm") or {})}
                              for t in pa.get("preferredDuringSchedulingIgnoredDuringExecution") or []],
        "pod_anti_pref": [{"weight": int(t.get("weight", 0)), "term": _pod_term(t.get("podAffinityTerm") or {})}
                          for t in paa.get("preferredDuringSchedulingIgnoredDuringExecution") or []],
        "tolerations": [{"key": t.get("key", ""), "operator": t.get("operator", "Equal"),
                         "value": t.get("value", ""), "effect": t.get("effect", "")}
                        for t in spec.get("tolerations") or []],
        "spread": [{"maxSkew": int(c.get("maxSkew", 1)), "topologyKey": c.get("topologyKey", ""),
                    "whenUnsatisfiable": c.get("whenUnsatisfiable", "DoNotSchedule"),
                    "labelSelector": _selector(c.get("labelSelector")),
                    "minDomains": c.get("minDomains"),
                    "nodeAffinityPolicy": c.get("nodeAffinityPolicy") or "Honor",
                    "nodeTaintsPolicy": c.get("nodeTaintsPolicy") or "Ignore",
                    "matchLabelKeys": list(c.get("matchLabelKeys") or [])}
                   for c in spec.get("topologySpreadConstraints") or []],
        "volumes": [_volume(v) for v in spec.get("volumes") or []],
    }


def _volume(v: dict) -> dict:
    if "emptyDir" in v:
        return {"name": v.get("name", ""), "kind": "emptyDir",
                "medium": (v.get("emptyDir") or {}).get("medium", "")}
    if "hostPath" in v:
        return {"name": v.get("name", ""), "kind": "hostPath"}
    if "persistentVolumeClaim" in v:
        return {"name": v.get("name", ""), "kind": "pvc",
                "claim": (v.get("persistentVolumeClaim") or {}).get("claimName", "")}
    if "ephemeral" in v:
        return {"name": v.get("name", ""), "kind": "ephemeral"}
    return {"name": v.get("name", ""), "kind": "other"}


def _node(n: dict) -> dict:
    meta, spec, status = n.get("metadata", {}), n.get("spec", {}), n.get("status", {})
    alloc = status.get("allocatable") or {}
    ready = next((c.get("status") == "True" for c in status.get("conditions") or []
                  if c.get("type") == "Ready"), False)
    return {
        "name": meta.get("name", ""),
        "labels": dict(meta.get("labels") or {}),
        "taints": [{"key": t.get("key", ""), "value": t.get("value", ""), "effect": t.get("effect", "")}
                   for t in spec.get("taints") or []],
        "unschedulable": bool(spec.get("unschedulable")),
        "ready": ready,
        "created": _ts(meta.get("creationTimestamp")),
        "provider_id": spec.get("providerID", ""),
        "scale_down_disabled": (meta.get("annotations") or {}).get(SCALE_DOWN_DISABLED) == "true",
        "alloc": {"cpu": milli(alloc.get("cpu", 0)), "mem": whole(alloc.get("memory", 0)),
                  "pods": whole(alloc.get("pods", 0)), "gpu": whole(alloc.get(GPU, 0)),
                  "eph": whole(alloc.get("ephemeral-storage", 0))},
        "images": [{"names": list(i.get("names") or []), "size": int(i.get("sizeBytes", 0) or 0)}
                   for i in status.get("images") or []],
    }


def _pdb(p: dict) -> dict:
    meta, spec, status = p.get("metadata", {}), p.get("spec", {}), p.get("status", {})
    return {
        "ns": meta.get("namespace", ""), "name": meta.get("name", ""),
        "selector": _selector(spec.get("selector")),
        "minAvailable": str(spec["minAvailable"]) if "minAvailable" in spec else None,
        "maxUnavailable": str(spec["maxUnavailable"]) if "maxUnavailable" in spec else None,
        "disruptionsAllowed": status.get("disruptionsAllowed"),
        "currentHealthy": status.get("currentHealthy"),
        "desiredHealthy": status.get("desiredHealthy"),
        "expectedPods": status.get("expectedPods"),
    }


def _pv(v: dict) -> dict:
    """A PV's CSI driver, handle and required node affinity (VolumeBinding checks it)."""
    spec = v.get("spec", {})
    terms = ((spec.get("nodeAffinity") or {}).get("required") or {}).get("nodeSelectorTerms") or []
    return {"driver": (spec.get("csi") or {}).get("driver", ""),
            "handle": (spec.get("csi") or {}).get("volumeHandle", v.get("metadata", {}).get("name", "")),
            "node_terms": [_node_term(t) for t in terms]}


@contextmanager
def _profile(env: dict[str, str]):
    """kubectl's exec plugin runs the cloud's CLI (EKS: AWS_PROFILE, OKE:
    OCI_CLI_PROFILE); set its profile for the call only."""
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def collect_kube(*, context: str | None, kubeconfig: str | None, profile: str | None = None,
                 region: str | None = None, cluster: str | None = None,
                 ca_namespace: str = "kube-system", timeout: int = 300,
                 cloud: str | None = None) -> dict[str, Any]:
    """Every object the two controllers read, as of now. Read-only."""
    def get(*what: str, ns_all: bool = True) -> list[dict]:
        args = ["get", *what] + (["-A"] if ns_all else []) + ["-o", "json"]
        return run_json(_kubectl(context, *args, kubeconfig=kubeconfig), timeout=timeout).get("items", [])

    from rightsizer.adapters import clouds

    provider = clouds.get(cloud, region=region, profile=profile)
    out: dict[str, Any] = {"collected_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}
    with _profile(provider.credential_env()):
        out["pods"] = [_pod(p) for p in get("pods")
                       if p.get("status", {}).get("phase") not in ("Succeeded", "Failed")]
        out["nodes"] = [_node(n) for n in get("nodes", ns_all=False)]
        out["csinodes"] = {
            c["metadata"]["name"]: {d.get("name", ""): (d.get("allocatable") or {}).get("count")
                                    for d in (c.get("spec") or {}).get("drivers") or []}
            for c in get("csinodes", ns_all=False)}
        out["pvcs"] = {f"{c['metadata']['namespace']}/{c['metadata']['name']}":
                       {"volume": (c.get("spec") or {}).get("volumeName", ""),
                        "class": (c.get("spec") or {}).get("storageClassName", "")}
                       for c in get("persistentvolumeclaims")}
        out["pvs"] = {v["metadata"]["name"]: _pv(v) for v in get("persistentvolumes", ns_all=False)}
        out["pdbs"] = [_pdb(p) for p in get("poddisruptionbudgets")]
        out["namespaces"] = {n["metadata"]["name"]: dict(n["metadata"].get("labels") or {})
                             for n in get("namespaces", ns_all=False)}
        # kube-scheduler's default spread groups a pod with the pods its
        # Services, ReplicaSet or StatefulSet select (helper.DefaultSelector).
        out["services"] = [{"ns": v["metadata"]["namespace"],
                            "selector": dict((v.get("spec") or {}).get("selector") or {})}
                           for v in get("services") if (v.get("spec") or {}).get("selector")]
        out["owners"] = {}
        for kind, items in (("ReplicaSet", get("replicasets")), ("StatefulSet", get("statefulsets"))):
            for o in items:
                out["owners"][f"{o['metadata']['namespace']}/{kind}/{o['metadata']['name']}"] = \
                    _selector((o.get("spec") or {}).get("selector"))
        for o in get("replicationcontrollers"):
            out["owners"][f"{o['metadata']['namespace']}/ReplicationController/{o['metadata']['name']}"] = \
                {"matchLabels": dict((o.get("spec") or {}).get("selector") or {}), "matchExpressions": []}
        out["ca"] = _ca_deployment(run_json(_kubectl(context, "get", "deployments", "-n", ca_namespace,
                                                     "-o", "json", kubeconfig=kubeconfig),
                                            timeout=timeout).get("items", []))
    # The cloud's scaling groups behind the node groups (EKS: Auto Scaling groups).
    out["asgs"] = provider.collect_scaling_groups(cluster) if cluster else []
    return out


def _ca_deployment(items: list[dict]) -> dict:
    for item in items:
        if "cluster-autoscaler" not in item.get("metadata", {}).get("name", ""):
            continue
        for c in item["spec"]["template"]["spec"].get("containers", []):
            args: dict[str, Any] = {}
            for token in list(c.get("command", [])) + list(c.get("args", [])):
                if token.startswith("--"):
                    key, _, raw = token[2:].partition("=")
                    args[key] = raw if raw else "true"
            return {"name": item["metadata"]["name"], "image": c.get("image", ""), "args": args}
    return {}
