"""Kubernetes collector: the constraints Prometheus cannot see.

Anti-affinity, topology spread, PDB *spec*, local storage and the pod-density
ceiling are all absent from kube-state-metrics in the form needed to decide
whether a node can actually be removed. They are read once, here.

Lightweight by construction: this reads **controllers, not pods**. A pod template
carries the affinity and volume declarations that matter, and a cluster with 3,676
pods has perhaps 200 controllers -- an order of magnitude less data for the same
answer. One LIST per resource type, no watches, no pagination loops.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from rightsizer.adapters import node_group_of
from rightsizer.sources.shell import run_json

log = logging.getLogger(__name__)

HOSTNAME_TOPOLOGY_KEYS = frozenset({"kubernetes.io/hostname", "topology.kubernetes.io/hostname"})
SAFE_TO_EVICT = "cluster-autoscaler.kubernetes.io/safe-to-evict"
#: Volumes named here (comma-separated) do not block a drain even though they
#: are local: ``drain.getNonBlockingVolumes`` in Cluster Autoscaler.
SAFE_TO_EVICT_LOCAL_VOLUMES = "cluster-autoscaler.kubernetes.io/safe-to-evict-local-volumes"
#: A node carrying this is never removed: ``eligibility.HasNoScaleDownAnnotation``.
SCALE_DOWN_DISABLED = "cluster-autoscaler.kubernetes.io/scale-down-disabled"
DO_NOT_DISRUPT = "karpenter.sh/do-not-disrupt"


def blocking_local_volumes(volumes: list[dict], annotations: dict) -> list[str]:
    """The volumes Cluster Autoscaler counts as local storage that blocks a drain.

    ``drain.HasBlockingLocalStorage``: a hostPath, or an emptyDir whose medium
    is not ``Memory``, unless its name is listed in ``safe-to-evict-local-volumes``.
    A tmpfs emptyDir is memory, not disk, and does not block.
    """
    listed = (annotations.get(SAFE_TO_EVICT_LOCAL_VOLUMES) or "").split(",")
    exempt = {v.strip() for v in listed if v.strip()}
    out = []
    for v in volumes:
        tmpfs = (v.get("emptyDir") or {}).get("medium") == "Memory"
        local = "hostPath" in v or ("emptyDir" in v and not tmpfs)
        if local and v.get("name", "") not in exempt:
            out.append(v.get("name", ""))
    return out


@dataclass
class WorkloadConstraints:
    namespace: str
    name: str
    kind: str
    replicas: int = 0
    # required (not preferred) pod anti-affinity on a per-node topology key:
    # forces N >= replicas, and is frequently the real floor on a node group.
    hard_antiaffinity_hostname: bool = False
    topology_spread_keys: list[str] = field(default_factory=list)
    max_skew: int | None = None
    # emptyDir / hostPath with safe-to-evict unset: Cluster Autoscaler will not
    # drain this pod, so any node hosting it cannot be removed.
    local_storage: bool = False
    #: True when ``local_storage`` follows CA's own rule -- tmpfs emptyDirs and
    #: ``safe-to-evict-local-volumes`` excluded. Older snapshots counted every
    #: emptyDir, which can report a blocker CA does not have.
    local_storage_exact: bool = False
    safe_to_evict: bool | None = None
    do_not_disrupt: bool = False
    #: Hard (``DoNotSchedule``) topology spread: [{"key", "maxSkew"}]. Soft ones
    #: never stop the scheduler, so they are not kept.
    hard_spread: list[dict] = field(default_factory=list)
    #: Set when the spread constraints were read with their ``whenUnsatisfiable``.
    spread_known: bool = False
    #: Pod-template labels -- what a PDB's selector matches against.
    labels: dict[str, str] = field(default_factory=dict)
    #: Where this workload's pods are allowed to land. Without these, an
    #: evacuation plan can move a pod to a node that would never accept it.
    node_selector: dict[str, str] = field(default_factory=dict)
    #: Each entry is one requiredDuringScheduling nodeSelectorTerm, flattened to
    #: {key: [values]} for matchExpressions using In/Equals. A pod satisfies the
    #: affinity if it satisfies any one term.
    node_affinity_terms: list[dict] = field(default_factory=list)
    #: (key, operator, value, effect); an empty key with operator Exists
    #: tolerates everything.
    tolerations: list[dict] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.name}/{self.kind}"

    @property
    def blocks_drain(self) -> bool:
        return blocks_drain(
            local_storage=self.local_storage,
            safe_to_evict=self.safe_to_evict,
            do_not_disrupt=self.do_not_disrupt,
        )


def blocks_drain(
    *, local_storage: bool, safe_to_evict: bool | None, do_not_disrupt: bool
) -> bool:
    """Would Cluster Autoscaler refuse to evict this workload's pods?

    The three cases are independent, which is the part that is easy to get
    wrong:

    * ``safe-to-evict: "false"`` vetoes the pod **on its own**. CA's list of pod
      types that prevent a node being removed names the annotation without
      qualification -- it does not require local storage, and treating it as an
      emptyDir modifier means predicting a scale-down that never happens.
    * emptyDir/hostPath vetoes *unless* ``safe-to-evict: "true"`` overrides it.
    * Karpenter's ``do-not-disrupt`` is **not** a Cluster Autoscaler rule
      (v1.35 has no such drainability check) and changes nothing here.

    One definition, because this predicate decides whether a saving is real and
    two copies of it will not stay in agreement.
    """
    del do_not_disrupt  # Karpenter's, not Cluster Autoscaler's
    if safe_to_evict is False:
        return True
    return local_storage and safe_to_evict is not True


@dataclass
class PDBConstraint:
    namespace: str
    name: str
    max_unavailable: str | None = None
    min_available: str | None = None
    #: spec.selector.matchLabels. Without it a PDB cannot be attributed to the
    #: pods it governs, which is the whole of the "kube-system pod with a PDB"
    #: exemption and of the disruption-budget check.
    selector: dict[str, str] = field(default_factory=dict)
    #: True when the selector uses matchExpressions, which are not evaluated
    #: here. Such a PDB is treated as covering nothing, and said so rather than
    #: silently ignored.
    has_expressions: bool = False

    @property
    def blocks_drain(self) -> bool:
        """maxUnavailable: 0 permits no eviction at all -- drain can never proceed."""
        return str(self.max_unavailable) == "0"


@dataclass
class NodeFacts:
    name: str
    nodegroup: str = ""
    instance_type: str = ""
    max_pods: int = 0
    unschedulable: bool = False
    taints: list[str] = field(default_factory=list)
    #: Full label set, so a pod's nodeSelector and node affinity can be checked
    #: against a candidate destination.
    labels: dict[str, str] = field(default_factory=dict)
    #: (key, value, effect) per taint, kept structured alongside the display
    #: form above so tolerations can actually be matched.
    taint_specs: list[dict] = field(default_factory=list)
    #: ``cluster-autoscaler.kubernetes.io/scale-down-disabled: "true"``.
    scale_down_disabled: bool = False
    #: status.capacity: what the machine has, before the kubelet's reservations.
    #: Says which size a node is where the instance type does not (an OCI Flex
    #: shape is one name for every size).
    cpu_capacity: float = 0.0
    mem_capacity_gib: float = 0.0
    gpu_capacity: int = 0


def context_exec(context: str | None, kubeconfig: str | None = None) -> dict | None:
    """The token command a kubeconfig context authenticates with, or None.

    ``kubectl config view`` reads the file and nothing else -- no cluster is
    contacted and no token minted. Credentials are redacted by kubectl; the
    command and its arguments are what is wanted.
    """
    try:
        view = run_json(_kubectl(context, "config", "view", "--minify", "-o", "json",
                                 kubeconfig=kubeconfig), timeout=30)
    except Exception as exc:  # noqa: BLE001 - an enrichment, never required
        log.debug("kubeconfig not readable for %s: %s", context, exc)
        return None
    users = (view or {}).get("users") or []  # type: ignore[union-attr]
    return ((users[0].get("user") or {}).get("exec") if users else None) or None


def context_hint(context: str, kubeconfig: str | None = None) -> str | None:
    """When ``context`` is not a context in the kubeconfig, the one that was meant.

    A kubeconfig names a cluster entry and a context entry for each cluster, and
    `oci ce cluster create-kubeconfig` makes them differ only by prefix
    (cluster-x / context-x); the cluster's name is the easy one to pass by
    mistake. None when the context exists or nothing better is known.
    """
    try:
        view = run_json(_kubectl(None, "config", "view", "-o", "json", kubeconfig=kubeconfig), timeout=30)
    except Exception:  # noqa: BLE001
        return None
    contexts = (view or {}).get("contexts") or []  # type: ignore[union-attr]
    if any(c.get("name") == context for c in contexts):
        return None
    for c in contexts:
        if (c.get("context") or {}).get("cluster") == context:
            return (f"{context!r} is a cluster entry in the kubeconfig, not a context; "
                    f"its context is {c['name']!r} (--context {c['name']})")
    names = ", ".join(c.get("name", "") for c in contexts) or "none"
    return f"no context {context!r} in the kubeconfig (contexts: {names})"


#: Where Kubernetes mounts a pod's service account token (its CA sits beside it).
SERVICE_ACCOUNT_TOKEN = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
#: The kubeconfig :func:`use_in_cluster_config` wrote for this process, if any.
_IN_CLUSTER_KUBECONFIG: str | None = None


def in_cluster() -> bool:
    """Running in a pod that can reach its own cluster's API server.

    A ``$KUBECONFIG`` means a cluster was chosen explicitly, and that one wins
    -- unless it is the one :func:`use_in_cluster_config` wrote for the pod.
    """
    kubeconfig = os.environ.get("KUBECONFIG")
    if kubeconfig and kubeconfig == _IN_CLUSTER_KUBECONFIG:
        return True
    return (bool(os.environ.get("KUBERNETES_SERVICE_HOST"))
            and not kubeconfig
            and SERVICE_ACCOUNT_TOKEN.is_file())


def use_in_cluster_config() -> str | None:
    """Point every kubectl this process runs at the pod's own cluster.

    kubectl falls back to the service account only when no flag changes its
    client config, and ``--request-timeout`` does: it then dials its default,
    localhost:8080, and every call is refused. A kubeconfig naming the API
    server, the mounted CA and the mounted token works whatever the flags.
    The token is named by path, never copied, so a rotated token is read fresh.
    Returns the file, or None when not in a pod.
    """
    global _IN_CLUSTER_KUBECONFIG
    if not in_cluster():
        return None
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    server = f"https://[{host}]:{port}" if ":" in host else f"https://{host}:{port}"
    config = {
        "apiVersion": "v1", "kind": "Config", "current-context": "in-cluster",
        "clusters": [{"name": "in-cluster", "cluster": {
            "server": server,
            "certificate-authority": str(SERVICE_ACCOUNT_TOKEN.parent / "ca.crt")}}],
        "users": [{"name": "in-cluster", "user": {"tokenFile": str(SERVICE_ACCOUNT_TOKEN)}}],
        "contexts": [{"name": "in-cluster",
                      "context": {"cluster": "in-cluster", "user": "in-cluster"}}],
    }
    path = Path(tempfile.gettempdir()) / "r8r-in-cluster.kubeconfig"
    path.write_text(json.dumps(config))  # kubeconfig is YAML; JSON is YAML
    _IN_CLUSTER_KUBECONFIG = str(path)
    os.environ["KUBECONFIG"] = _IN_CLUSTER_KUBECONFIG
    return _IN_CLUSTER_KUBECONFIG


def has_access(context: str | None) -> bool:
    """kubectl can reach a cluster: a context was given, or the pod's own."""
    return bool(context) or in_cluster()


def _kubectl(context: str | None, *args: str, kubeconfig: str | None = None) -> list[str]:
    """Build a read-only kubectl argv.

    ``--context`` names a context; ``--kubeconfig`` points at the file holding
    it. A cluster kept in its own file -- one per cluster under a directory,
    which is how these are laid out -- has no entry in the default config at
    all, so a context name alone cannot reach it.
    """
    argv = ["kubectl"]
    if kubeconfig:
        argv += ["--kubeconfig", kubeconfig]
    if context:
        argv += ["--context", context]
    return argv + list(args) + ["--request-timeout=60s"]


def collect_workload_constraints(
    context: str | None = None, timeout: int = 120, kubeconfig: str | None = None
) -> list[WorkloadConstraints]:
    out: list[WorkloadConstraints] = []
    payload = run_json(
        _kubectl(context, "get", "deployments,statefulsets,daemonsets", "-A", "-o", "json", kubeconfig=kubeconfig),
        timeout=timeout,
    )
    for item in payload.get("items", []):  # type: ignore[union-attr]
        meta = item.get("metadata", {})
        spec = item.get("spec", {})
        template = spec.get("template", {})
        tmeta = template.get("metadata", {})
        tspec = template.get("spec", {})
        annotations = {**meta.get("annotations", {}), **tmeta.get("annotations", {})}

        affinity = (tspec.get("affinity") or {}).get("podAntiAffinity") or {}
        hard = [
            term
            for term in affinity.get("requiredDuringSchedulingIgnoredDuringExecution", []) or []
            if term.get("topologyKey") in HOSTNAME_TOPOLOGY_KEYS
        ]
        spreads = tspec.get("topologySpreadConstraints") or []
        volumes = tspec.get("volumes") or []
        local = bool(blocking_local_volumes(volumes, annotations))

        # Only the exact strings count: "on-completion" (or anything else) is
        # neither the override nor the veto.
        raw_evict = annotations.get(SAFE_TO_EVICT)
        out.append(
            WorkloadConstraints(
                namespace=meta.get("namespace", ""),
                name=meta.get("name", ""),
                kind=item.get("kind", ""),
                replicas=int(spec.get("replicas", 0) or 0),
                hard_antiaffinity_hostname=bool(hard),
                topology_spread_keys=[s.get("topologyKey", "") for s in spreads],
                max_skew=min((int(s.get("maxSkew", 1)) for s in spreads), default=None),
                local_storage=local,
                local_storage_exact=True,
                safe_to_evict={"true": True, "false": False}.get(raw_evict or ""),
                hard_spread=[
                    {"key": sp.get("topologyKey", ""), "maxSkew": int(sp.get("maxSkew", 1))}
                    for sp in spreads
                    if sp.get("whenUnsatisfiable", "DoNotSchedule") == "DoNotSchedule"
                ],
                spread_known=True,
                do_not_disrupt=annotations.get(DO_NOT_DISRUPT) == "true",
                labels=dict(tmeta.get("labels") or {}),
                node_selector=dict(tspec.get("nodeSelector") or {}),
                node_affinity_terms=_node_affinity_terms(tspec),
                tolerations=[
                    {
                        "key": t.get("key", ""),
                        "operator": t.get("operator", "Equal"),
                        "value": t.get("value", ""),
                        "effect": t.get("effect", ""),
                    }
                    for t in (tspec.get("tolerations") or [])
                ],
            )
        )
    return out


def _node_affinity_terms(tspec: dict) -> list[dict]:
    """requiredDuringScheduling node affinity, flattened to {key: [values]}.

    Only In and Exists are represented. NotIn/DoesNotExist would need the
    negation carried through the matcher, and a term that uses them is dropped
    rather than approximated -- an affinity read as more permissive than it is
    produces an evacuation plan the scheduler will refuse.
    """
    affinity = (tspec.get("affinity") or {}).get("nodeAffinity") or {}
    required = affinity.get("requiredDuringSchedulingIgnoredDuringExecution") or {}
    out: list[dict] = []
    for term in required.get("nodeSelectorTerms") or []:
        flat: dict[str, list[str]] = {}
        usable = True
        for expr in term.get("matchExpressions") or []:
            op = expr.get("operator")
            if op == "In":
                flat[expr.get("key", "")] = list(expr.get("values") or [])
            elif op == "Exists":
                flat[expr.get("key", "")] = []
            else:
                usable = False
                break
        if usable and flat:
            out.append(flat)
    return out


def collect_pdbs(context: str | None = None, timeout: int = 60,
                 kubeconfig: str | None = None) -> list[PDBConstraint]:
    """PDB *spec*, which carries maxUnavailable: 0 -- the status metrics do not."""
    payload = run_json(
        _kubectl(context, "get", "poddisruptionbudgets", "-A", "-o", "json", kubeconfig=kubeconfig), timeout=timeout
    )
    out = []
    for item in payload.get("items", []):  # type: ignore[union-attr]
        meta, spec = item.get("metadata", {}), item.get("spec", {})
        out.append(
            PDBConstraint(
                namespace=meta.get("namespace", ""),
                name=meta.get("name", ""),
                max_unavailable=(
                    str(spec["maxUnavailable"]) if "maxUnavailable" in spec else None
                ),
                min_available=(str(spec["minAvailable"]) if "minAvailable" in spec else None),
                selector=dict((spec.get("selector") or {}).get("matchLabels") or {}),
                has_expressions=bool((spec.get("selector") or {}).get("matchExpressions")),
            )
        )
    return out


def collect_nodes(context: str | None = None, timeout: int = 60,
                  kubeconfig: str | None = None) -> list[NodeFacts]:
    """Node facts, including the ENI-derived maxPods ceiling from allocatable.pods."""
    from rightsizer.sources.kfacts import quantity

    payload = run_json(_kubectl(context, "get", "nodes", "-o", "json", kubeconfig=kubeconfig), timeout=timeout)
    out = []
    for item in payload.get("items", []):  # type: ignore[union-attr]
        meta, spec, status = item.get("metadata", {}), item.get("spec", {}), item.get("status", {})
        labels = meta.get("labels", {})
        out.append(
            NodeFacts(
                name=meta.get("name", ""),
                nodegroup=node_group_of(labels),
                instance_type=labels.get("node.kubernetes.io/instance-type", ""),
                max_pods=int(status.get("allocatable", {}).get("pods", 0) or 0),
                unschedulable=bool(spec.get("unschedulable", False)),
                # taints=[
                #     f"{t.get('key')}={t.get('value', '')}:{t.get('effect')}"
                #     for t in (spec.get("taints") or [])
                # ],
                labels=dict(labels),
                scale_down_disabled=(
                    (meta.get("annotations") or {}).get(SCALE_DOWN_DISABLED) == "true"),
                taint_specs=[
                    {
                        "key": t.get("key", ""),
                        "value": t.get("value", ""),
                        "effect": t.get("effect", ""),
                    }
                    for t in (spec.get("taints") or [])
                ],
                cpu_capacity=quantity((status.get("capacity") or {}).get("cpu")),
                mem_capacity_gib=quantity((status.get("capacity") or {}).get("memory")) / 1024**3,
                gpu_capacity=int(quantity((status.get("capacity") or {}).get("nvidia.com/gpu"))),
            )
        )
    return out
