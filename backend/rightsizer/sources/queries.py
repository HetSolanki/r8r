"""Every PromQL string in the product.

Rules encoded here, learned the expensive way:

* ``env`` is pinned on every selector, on **both** sides of every join. Miss it on
  one side of a ``group_left`` and the query silently aggregates across clusters.
* Thanos serves duplicate series per receive replica, so each join operand is
  collapsed with ``max by (...)`` first. Without that the query fails with
  "found duplicate series for the match group".
* DaemonSet exclusion uses ``unless``, never an inner join on ``kube_pod_owner`` --
  an inner join silently drops bare pods.
* ``max by (owner)`` is the reducer for a right-sizing peak, never ``sum``:
  requests are per replica, so the peak that matters is the worst single replica
  at each instant.
* No percentile is computed in PromQL. Queries are chunked, and percentiles do not
  compose across chunks (the p95 of sixty 12h p95s is not the 30d p95). Only
  ``max`` / ``avg`` / ``count`` are aggregated server-side; percentiles happen in
  :mod:`rightsizer.core.stats` over the full concatenated series.
"""

from __future__ import annotations

import re

from rightsizer.core.config import NODEGROUP_LABEL, Scope

NG = NODEGROUP_LABEL

# Matcher fragments kept as constants: nesting quotes inside f-string expressions
# is unreadable and easy to get subtly wrong.
POD_SET = 'pod!=""'
CONTAINER_REAL = ('container!="POD"', 'container!=""', POD_SET)

# --------------------------------------------------------------------------
# building blocks
# --------------------------------------------------------------------------


def owner_join(scope: Scope) -> str:
    """pod -> (owner_kind, owner_name), de-duplicated across receive replicas."""
    sel = scope.matchers(POD_SET)
    return f"max by (namespace,pod,owner_kind,owner_name) (kube_pod_owner{{{sel}}})"


def ksm_label(key: str) -> str:
    """A node label as kube-state-metrics exposes it on kube_node_labels."""
    return "label_" + re.sub(r"[^A-Za-z0-9_]", "_", key)


def node_label_join(scope: Scope) -> str:
    """node -> node group, de-duplicated, whichever adapter named the group.

    Each registered cloud and autoscaler names a node's group with its own label
    (:func:`rightsizer.adapters.node_group_label_keys`). Each is renamed to
    ``NG``; ``or on(node)`` keeps, per node, the first in priority order, so a
    node carrying two never joins twice. Nodes with none are left out, as
    before. kube-state-metrics must expose the label
    (``--metric-labels-allowlist=nodes=[...]``) for its nodes to be seen.
    """
    from rightsizer.adapters import node_group_label_keys

    parts = []
    for key in node_group_label_keys():
        src = ksm_label(key)
        sel = scope.node_matchers(f'{src}!=""')
        parts.append(f'max by (node,{NG}) (label_replace(kube_node_labels{{{sel}}}, '
                     f'"{NG}", "$1", "{src}", "(.+)"))')
    # Parenthesised whole: callers write `x * on(node) group_left(NG) <this>`,
    # and `*` binds tighter than `or`.
    return "(" + " or on(node) ".join(f"({p})" for p in parts) + ")"


def _by_owner(inner: str, scope: Scope) -> str:
    return (
        f"({inner}) * on(namespace,pod) group_left(owner_kind,owner_name) {owner_join(scope)}"
    )


# --------------------------------------------------------------------------
# container usage
# --------------------------------------------------------------------------


def cpu_per_container(scope: Scope, rate_window: int) -> str:
    """CPU cores in use, per (namespace, pod, container).

    ``rate_window`` is the averaging window, not the query step. Matching it to
    the step smooths away exactly the bursts a request has to cover; see
    ``Tunables.rate_window_seconds`` for the measurements.
    """
    sel = scope.matchers(*CONTAINER_REAL)
    return (
        f"sum(rate(container_cpu_usage_seconds_total{{{sel}}}[{rate_window}s]))"
        " by (namespace,pod,container)"
    )


def memory_per_container(scope: Scope) -> str:
    """Working-set bytes per (namespace, pod, container).

    The inner ``max_over_time([5m:1m])`` catches a spike that falls between two
    5-minute evaluation steps. Memory spikes are what trigger OOMKills, so
    stepping over them defeats the purpose of the report.
    """
    sel = scope.matchers(*CONTAINER_REAL)
    return (
        "max_over_time((sum(container_memory_working_set_bytes"
        f"{{{sel}}}) by (namespace,pod,container))[5m:1m])"
    )


def workload_usage(scope: Scope, per_container: str, agg: str) -> str:
    """Aggregate a per-container series up to the workload that owns it.

    ``agg`` is one of:

    ``max``   the hottest single replica at each instant -- the statistic that
              sizes a *request*, because requests are configured per replica.
    ``avg``   mean across replicas.
    ``sum``   every replica added together at each instant -- the workload's true
              concurrent footprint. This is the statistic for *capacity*, and it
              is not the same as ``max x replicas``: that would be a sum of peaks,
              and peaks do not coincide, so it over-provisions.
    ``count`` replicas seen.

    Grouping keeps ``container`` because requests are configured per container --
    a pod with a sidecar cannot take a workload-level number.
    """
    if agg not in {"max", "avg", "sum", "count"}:
        raise ValueError(f"unsupported aggregation: {agg!r}")
    return (
        f"{agg} by (namespace,owner_kind,owner_name,container) "
        f"({_by_owner(per_container, scope)})"
    )


# --------------------------------------------------------------------------
# configured requests / limits
# --------------------------------------------------------------------------


def workload_resource_spec(scope: Scope, metric: str, resource: str) -> str:
    """Configured request or limit per (workload, container)."""
    sel = scope.matchers(f'resource="{resource}"', POD_SET)
    per_pod = f"sum({metric}{{{sel}}}) by (namespace,pod,container)"
    return (
        "max by (namespace,owner_kind,owner_name,container) "
        f"({_by_owner(per_pod, scope)})"
    )


def workload_requests(scope: Scope, resource: str) -> str:
    return workload_resource_spec(scope, "kube_pod_container_resource_requests", resource)


def workload_limits(scope: Scope, resource: str) -> str:
    return workload_resource_spec(scope, "kube_pod_container_resource_limits", resource)


def cpu_throttling(scope: Scope) -> str:
    """Share of CFS periods in which the container was throttled, per workload.

    This is the only *direct* evidence of a CPU performance problem. A request
    does not cap anything -- it is a scheduling weight -- so a container over
    its request is only starved when the node is contended. A **limit** caps
    hard, and the kernel records every period it clipped. A container throttled
    for a meaningful share of its periods is being slowed down right now, and no
    percentile of usage would reveal it: usage looks *fine* precisely because
    the limit is holding it down.

    Only containers with a CPU limit emit these series at all, so an absent
    value means "no limit", not "no throttling".
    """
    sel = scope.matchers(*CONTAINER_REAL)
    window = "10m"
    throttled = (
        f"sum(rate(container_cpu_cfs_throttled_periods_total{{{sel}}}[{window}]))"
        " by (namespace,pod,container)"
    )
    total = (
        f"sum(rate(container_cpu_cfs_periods_total{{{sel}}}[{window}]))"
        " by (namespace,pod,container)"
    )
    # Clamped, because the ratio is two independently-extrapolated rates: a
    # container restart resets both counters, and `rate()` can extrapolate the
    # numerator across the reset a fraction of a step differently from the
    # denominator. Taking `max` over 30 days then selects exactly those
    # artefacts -- it reported 242.9% of periods throttled, which is not a
    # quantity that exists.
    ratio = f"clamp_max((({throttled}) / (({total}) > 0)), 1)"
    return (
        "max by (namespace,owner_kind,owner_name,container) ("
        f"({ratio}) * on(namespace,pod) group_left(owner_kind,owner_name) {owner_join(scope)}"
        ")"
    )


# --------------------------------------------------------------------------
# topology: workload -> node group
# --------------------------------------------------------------------------


def pod_to_nodegroup(scope: Scope) -> str:
    sel = scope.matchers(POD_SET)
    return (
        f"max by (namespace,pod,node) (kube_pod_info{{{sel}}})"
        f" * on(node) group_left({NG}) {node_label_join(scope)}"
    )


def workload_to_nodegroup(scope: Scope) -> str:
    """Which node groups each workload ran on, sampled coarsely.

    Pods move, so this is time-varying: the caller counts samples per
    (workload, group) pair and derives a dominant group plus a share. A workload
    whose share is below ~90% spans groups and must be sized against both.
    """
    return (
        f"max by (namespace,owner_kind,owner_name,{NG}) (({pod_to_nodegroup(scope)})"
        f" * on(namespace,pod) group_left(owner_kind,owner_name) {owner_join(scope)})"
    )


# --------------------------------------------------------------------------
# node group capacity
# --------------------------------------------------------------------------


def nodegroup_node_count(scope: Scope) -> str:
    return f"count by ({NG}) ({node_label_join(scope)})"


def nodegroup_allocatable(scope: Scope, resource: str) -> str:
    sel = scope.node_matchers(f'resource="{resource}"')
    return (
        f"sum by ({NG}) (max by (node) (kube_node_status_allocatable{{{sel}}})"
        f" * on(node) group_left({NG}) {node_label_join(scope)})"
    )


def _pod_requests_by_pod(scope: Scope, resource: str) -> str:
    sel = scope.matchers(f'resource="{resource}"', POD_SET)
    return f"sum(kube_pod_container_resource_requests{{{sel}}}) by (namespace,pod)"


def _daemonset_pods(scope: Scope) -> str:
    sel = scope.matchers('owner_kind="DaemonSet"', POD_SET)
    return f"max by (namespace,pod) (kube_pod_owner{{{sel}}})"


def _attach_nodegroup(inner: str, scope: Scope) -> str:
    sel = scope.matchers(POD_SET)
    return (
        f"(({inner}) * on(namespace,pod) group_left(node)"
        f" max by (namespace,pod,node) (kube_pod_info{{{sel}}}))"
        f" * on(node) group_left({NG}) {node_label_join(scope)}"
    )


def nodegroup_daemonset_requests(scope: Scope, resource: str) -> str:
    """Total DaemonSet requests landing on each node group.

    DaemonSet pods run on every node, so their requests shrink the per-node
    capacity available to everything else -- they belong in the denominator of the
    node-count math, not the numerator. Divide by the node count for the per-node
    ``D`` term.
    """
    ds = (
        f"({_pod_requests_by_pod(scope, resource)})"
        f" * on(namespace,pod) group_left() {_daemonset_pods(scope)}"
    )
    return f"sum by ({NG}) ({_attach_nodegroup(ds, scope)})"


def nodegroup_workload_requests(scope: Scope, resource: str) -> str:
    """Total non-DaemonSet requests on each node group (the numerator)."""
    regular = (
        f"({_pod_requests_by_pod(scope, resource)})"
        f" unless on(namespace,pod) {_daemonset_pods(scope)}"
    )
    return f"sum by ({NG}) ({_attach_nodegroup(regular, scope)})"


def nodegroup_pod_count(scope: Scope) -> str:
    """Pods per node group -- checked against the ENI-derived maxPods ceiling."""
    sel = scope.matchers(POD_SET)
    inner = f"max by (namespace,pod) (kube_pod_info{{{sel}}})"
    return f"count by ({NG}) ({_attach_nodegroup(inner, scope)})"


def nodegroup_daemonset_pod_count(scope: Scope) -> str:
    """DaemonSet pods per node group.

    They are modelled as per-node overhead, not as items to pack, so they must be
    subtracted before the reconstructed pod set is reconciled against the group's
    observed pod count -- otherwise the schedulable fleet is inflated by exactly
    the number of DaemonSet pods.
    """
    return f"count by ({NG}) ({_attach_nodegroup(_daemonset_pods(scope), scope)})"


def node_not_ready(scope: Scope) -> str:
    """Nodes whose Ready condition is false or unknown, with their node group.

    Such a node accepts no new pods, so Cluster Autoscaler cannot evacuate onto
    it -- a replay that counts its free space as a destination removes nodes CA
    will not.
    """
    sel = scope.node_matchers('condition="Ready"', 'status!="true"')
    return (
        f"(max by (node) (kube_node_status_condition{{{sel}}}) == 1)"
        f" * on(node) group_left({NG}) {node_label_join(scope)}"
    )


def node_cordoned(scope: Scope) -> str:
    """Cordoned nodes (spec.unschedulable), with their node group."""
    sel = scope.node_matchers()
    return (
        f"(max by (node) (kube_node_spec_unschedulable{{{sel}}}) == 1)"
        f" * on(node) group_left({NG}) {node_label_join(scope)}"
    )


def pod_placement(scope: Scope) -> str:
    """Every pod with the node and node group it is actually on.

    Cluster Autoscaler evacuates a specific node onto the nodes that remain, so
    simulating it needs real placement -- which pod sits where -- not an
    aggregate. Pods with no declared request still appear here: they occupy a
    scheduling slot and block a drain like any other.
    """
    sel = scope.matchers(POD_SET)
    return (
        f"max by (namespace,pod,node,{NG}) (kube_pod_info{{{sel}}}"
        f" * on(node) group_left({NG}) {node_label_join(scope)})"
    )


def pod_created(scope: Scope) -> str:
    """Each pod's creation time (unix seconds).

    Cluster Autoscaler stops treating a kube-system pod without a PDB as a
    blocker once it is older than ``--blocking-system-pod-distruption-timeout``
    (an hour by default), so the replay needs every pod's age.
    """
    sel = scope.matchers(POD_SET)
    return f"max by (namespace,pod) (kube_pod_created{{{sel}}})"


def pod_requests(scope: Scope, resource: str) -> str:
    """Per-pod request totals, keyed the same way as :func:`pod_placement`."""
    sel = scope.matchers(f'resource="{resource}"', POD_SET)
    return (
        "sum by (namespace,pod) "
        f"(kube_pod_container_resource_requests{{{sel}}})"
    )


def pod_owner(scope: Scope) -> str:
    """Owner kind *and name* per pod.

    The name is what maps a pod back to the workload its request is configured
    on; the kind alone leaves only the pod name, whose generated suffix is not a
    workload identity.
    """
    sel = scope.matchers(POD_SET)
    return f"max by (namespace,pod,owner_kind,owner_name) (kube_pod_owner{{{sel}}})"


def node_pod_count(scope: Scope) -> str:
    """Pods actually running on each node, with its node group.

    This is observed placement, not a simulation. The "as-is" view must show what
    the cluster is really doing -- a re-packed hypothetical presented as current
    state is indistinguishable from a wrong answer.
    """
    sel = scope.matchers(POD_SET)
    return (
        f"count by (node,{NG}) (max by (namespace,pod,node) (kube_pod_info{{{sel}}})"
        f" * on(node) group_left({NG}) {node_label_join(scope)})"
    )


def node_requests(scope: Scope, resource: str) -> str:
    """Requests actually reserved on each node."""
    per_pod = _pod_requests_by_pod(scope, resource)
    sel = scope.matchers(POD_SET)
    return (
        f"sum by (node,{NG}) ((({per_pod})"
        f" * on(namespace,pod) group_left(node)"
        f" max by (namespace,pod,node) (kube_pod_info{{{sel}}}))"
        f" * on(node) group_left({NG}) {node_label_join(scope)})"
    )


# --------------------------------------------------------------------------
# blocker evidence
# --------------------------------------------------------------------------


def oomkilled(scope: Scope) -> str:
    """Containers whose last termination was an OOMKill -- ground truth, not a proxy."""
    sel = scope.matchers('reason="OOMKilled"', POD_SET)
    return (
        "max by (namespace,pod,container) "
        f"(kube_pod_container_status_last_terminated_reason{{{sel}}})"
    )


def hpa_info(scope: Scope) -> str:
    return (
        "max by (namespace,horizontalpodautoscaler,scaletargetref_kind,scaletargetref_name) "
        f"(kube_horizontalpodautoscaler_info{{{scope.matchers()}}})"
    )


def hpa_target_metric(scope: Scope) -> str:
    """HPA targets by metric name and target type.

    ``metric_target_type="utilization"`` on cpu or memory is the trap: utilization
    is usage/request, so cutting the request raises it, the HPA scales out, and
    ``replicas x request`` stays roughly invariant. Savings are structurally zero.
    """
    return (
        "max by (namespace,horizontalpodautoscaler,metric_name,metric_target_type) "
        f"(kube_horizontalpodautoscaler_spec_target_metric{{{scope.matchers()}}})"
    )


def hpa_replica_bounds(scope: Scope, bound: str) -> str:
    if bound not in {"min", "max"}:
        raise ValueError(bound)
    return (
        "max by (namespace,horizontalpodautoscaler) "
        f"(kube_horizontalpodautoscaler_spec_{bound}_replicas{{{scope.matchers()}}})"
    )


def pdb_desired_healthy(scope: Scope) -> str:
    return (
        "max by (namespace,poddisruptionbudget) "
        f"(kube_poddisruptionbudget_status_desired_healthy{{{scope.matchers()}}})"
    )


def pdb_expected_pods(scope: Scope) -> str:
    """When desired_healthy == expected_pods, no pod may be evicted: drain is blocked."""
    return (
        "max by (namespace,poddisruptionbudget) "
        f"(kube_poddisruptionbudget_status_expected_pods{{{scope.matchers()}}})"
    )


def pdb_current_healthy(scope: Scope) -> str:
    """Pods the PDB counts as healthy right now.

    The eviction budget is ``current_healthy - desired_healthy``, not
    ``expected - desired``. A PDB that allows one disruption on paper allows
    none while one of its pods is crash-looping, and nothing in the spec or in
    desired/expected shows it.
    """
    return (
        "max by (namespace,poddisruptionbudget) "
        f"(kube_poddisruptionbudget_status_current_healthy{{{scope.matchers()}}})"
    )


#: Waiting reasons of a pod that is starting, not failing. Every pod passes
#: through them on each rollout and scale-out, so counting them reports a
#: healthy deploy as a fleet of broken pods.
STARTING_REASONS = ("ContainerCreating", "PodInitializing")
_STARTING = "|".join(STARTING_REASONS)


def unready_pods(scope: Scope) -> str:
    """Pods whose Ready condition is false -- what a PDB does not count as healthy.

    Finished pods are excluded: a completed Job pod reports not-Ready forever,
    and on a cluster full of CronJobs they outnumber the genuinely failing ones.
    So are pods that are still starting. A pod starting because an init
    container is crash-looping shows its main containers as PodInitializing;
    it is not lost here, because ``init_container_waiting_reasons`` reports it.
    """
    ready = scope.matchers('condition="false"', POD_SET)
    done = scope.matchers('phase=~"Succeeded|Failed"', POD_SET)
    starting = scope.matchers(f'reason=~"{_STARTING}"', POD_SET)
    return (
        f"(max by (namespace,pod) (kube_pod_status_ready{{{ready}}}) == 1)"
        f" unless on(namespace,pod) (max by (namespace,pod) (kube_pod_status_phase{{{done}}}) == 1)"
        " unless on(namespace,pod) (max by (namespace,pod) "
        f"(kube_pod_container_status_waiting_reason{{{starting}}}) == 1)"
    )


def container_waiting_reasons(scope: Scope) -> str:
    """Why a container is not running: CrashLoopBackOff, ImagePullBackOff, ...

    Carried so a blocker can name the cause rather than only the count. Startup
    reasons are left out: they are not a cause, and a pod listed for one is
    listed as failing.
    """
    sel = scope.matchers(f'reason!~"{_STARTING}"', POD_SET)
    return (
        "max by (namespace,pod,container,reason) "
        f"(kube_pod_container_status_waiting_reason{{{sel}}}) == 1"
    )


def init_container_waiting_reasons(scope: Scope) -> str:
    """The same for init containers: Init:CrashLoopBackOff, Init:ImagePullBackOff.

    Needed once PodInitializing stopped counting as unhealthy, since that is all
    the main containers of a pod with a failing init container ever report.
    """
    sel = scope.matchers(f'reason!~"{_STARTING}"', POD_SET)
    return (
        "max by (namespace,pod,container,reason) "
        f"(kube_pod_init_container_status_waiting_reason{{{sel}}}) == 1"
    )


# --------------------------------------------------------------------------
# node-local storage
# --------------------------------------------------------------------------

#: Where kubelet keeps pods' writable layers, emptyDirs and logs. node-exporter
#: may see the host under a prefix, depending on how it mounts the root.
KUBELET_MOUNT = "(/host|/rootfs|/host/root)?/var/lib/kubelet"
ROOT_MOUNT = "/|/host|/rootfs|/host/root"


def _node_fs(scope: Scope, metric: str, mount: str) -> str:
    """One filesystem expression per node, mapped from node-exporter's pod to its node.

    node-exporter labels its series with its own pod, not the node, so the node
    comes from that pod's kube_pod_info -- which works whatever the scrape
    relabelling did to `instance`. `metric` may be an arithmetic expression over
    series of *one* filesystem ({sel} is filled into each), so used and share are
    computed within a mount before any mount is chosen.
    """
    sel = scope.node_matchers(f'mountpoint=~"{mount}"', 'fstype!~"tmpfs|overlay|ramfs"')
    # node-exporter is a DaemonSet; restricting the join to those pods keeps a
    # 30-day range query to a few hundred series instead of every pod.
    # One node per pod. A pod that moved has two kube_pod_info series for a
    # scrape or two, and a many-to-many join rejects the whole query.
    pod_node = (
        "topk by (namespace,pod) (1, max by (namespace,pod,node) "
        f"(kube_pod_info{{{scope.node_matchers(POD_SET, 'created_by_kind=\"DaemonSet\"')}}}))"
    )
    expr = metric.replace("{sel}", "{" + sel + "}") if "{sel}" in metric else f"{metric}{{{sel}}}"
    return (
        f"max by (node) (({expr})"
        f" * on(namespace,pod) group_left(node) {pod_node})"
    )


def kubelet_fs(scope: Scope, metric: str) -> str:
    """The filesystem kubelet writes to on each node.

    Its own mount when one exists -- on instance-store nodes it is bind-mounted
    from the NVMe RAID -- otherwise the root filesystem it lives on. The choice
    is made on the finished expression: choosing separately for size and for
    free space once subtracted the root volume's free space from the NVMe's size
    while a node booted, and read as 322 GB used on a disk holding 127.
    """
    return (
        f"({_node_fs(scope, metric, KUBELET_MOUNT)})"
        f" or ({_node_fs(scope, metric, ROOT_MOUNT)})"
    )


_USED = "(node_filesystem_size_bytes{sel} - node_filesystem_avail_bytes{sel})"
_SHARE = f"({_USED} / node_filesystem_size_bytes{{sel}})"


def root_fs_size(scope: Scope) -> str:
    return f"max by ({NG}) (({_node_fs(scope, 'node_filesystem_size_bytes', ROOT_MOUNT)})" \
           f" * on(node) group_left({NG}) {node_label_join(scope)})"


def _with_ng(inner: str, scope: Scope) -> str:
    return f"(({inner}) * on(node) group_left({NG}) {node_label_join(scope)})"


def kubelet_fs_size_per_node(scope: Scope) -> str:
    """Capacity of kubelet's filesystem, one series per node with its group."""
    return _with_ng(kubelet_fs(scope, "node_filesystem_size_bytes"), scope)


def kubelet_fs_used_by_group(scope: Scope, agg: str, bucket: str) -> str:
    """Used bytes on kubelet's filesystem per group.

    Bucketed like the trends -- the largest value inside each coarse step, not
    the value at it -- so a job that fills a disk for an hour is not stepped
    over. `agg` is `max` (hottest node), `avg` (a typical node) or `sum`
    (the whole group).
    """
    used = _with_ng(kubelet_fs(scope, _USED), scope)
    return f"max_over_time(({agg} by ({NG}) ({used}))[{bucket}:15m])"


def kubelet_fs_used_share_max(scope: Scope, bucket: str) -> str:
    """The fullest node's used share of its own disk, per group."""
    share = _with_ng(kubelet_fs(scope, _SHARE), scope)
    return f"max_over_time((max by ({NG}) ({share}))[{bucket}:15m])"


def nodegroup_ephemeral(scope: Scope, series: str) -> str:
    """Per-node ephemeral-storage capacity or allocatable, per group."""
    sel = scope.node_matchers('resource="ephemeral_storage"')
    return (
        f"max by ({NG}) (max by (node) ({series}{{{sel}}})"
        f" * on(node) group_left({NG}) {node_label_join(scope)})"
    )


def pod_claims(scope: Scope) -> str:
    """Pod -> the PersistentVolumeClaims it mounts."""
    sel = scope.matchers(POD_SET)
    return f"max by (namespace,pod,persistentvolumeclaim) (kube_pod_spec_volumes_persistentvolumeclaims_info{{{sel}}})"


def claim_classes(scope: Scope) -> str:
    """Claim -> its storage class, which says whether the volume is zonal."""
    sel = scope.matchers()
    return f"max by (namespace,persistentvolumeclaim,storageclass) (kube_persistentvolumeclaim_info{{{sel}}})"


def finished_pods(scope: Scope) -> str:
    """Pods that have run to completion or failed: they hold no slot.

    ``kube_pod_info`` keeps a completed CronJob pod until it is garbage
    collected, but the kubelet's max-pods counts only pods still running, and
    Cluster Autoscaler neither moves nor waits for them.
    """
    sel = scope.matchers(POD_SET, 'phase=~"Succeeded|Failed"')
    return f"max by (namespace,pod) (kube_pod_status_phase{{{sel}}}) == 1"
