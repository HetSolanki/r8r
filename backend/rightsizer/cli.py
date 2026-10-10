"""Thin CLI over the library. Read-only: HTTP GET against Thanos, nothing else."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
from dataclasses import asdict, replace
import logging
import os
import re
import sys
import time
from pathlib import Path

import requests

from rightsizer.analysis.actions import build_actions
from rightsizer.adapters import autoscalers, clouds
from rightsizer.sources.collect import PreflightError, collect, collect_cluster_facts
from rightsizer.sources.kube import has_access, in_cluster, use_in_cluster_config
from rightsizer.core.config import DEFAULT_THANOS_URL, Scope, Settings, Tunables
from rightsizer.core.models import Snapshot
from rightsizer.analysis.recommend import container_recommendations, nodegroup_recommendations
from rightsizer.output.report import to_json, to_markdown
from rightsizer.analysis.plan import Finding, Plan, PlanError, pressure_findings, resolve, review, simulate, to_text
from rightsizer.sources.trends import TrendKey, write_trends
from rightsizer.core import layout


#: Output defaults beside the checkout (see :mod:`rightsizer.core.layout`) rather
#: than beside wherever the command was typed, so running from a cluster's own
#: directory does not scatter snapshots there.


def _metrics(args: argparse.Namespace, snapshot=None) -> tuple[str, str]:
    """(url, env label) to query: the flags, else what the snapshot was collected
    from, else the shared Thanos.

    --prometheus-url is one cluster's own Prometheus: it has no env label to pin,
    so the label is dropped unless --env-label says otherwise.
    """
    recorded = ((snapshot.meta or {}).get("metrics") or {}) if snapshot is not None else {}
    if args.prometheus_url:
        url, label = args.prometheus_url, ""
    elif args.thanos_url:
        url, label = args.thanos_url, "env"
    else:
        url = recorded.get("url") or DEFAULT_THANOS_URL
        label = recorded.get("env_label", "env")
    if args.env_label is not None:
        label = args.env_label
    return url, label


def _settings(args: argparse.Namespace, snapshot=None) -> Settings:
    end_ts = None
    if args.end:
        end_ts = int(
            dt.datetime.fromisoformat(args.end).replace(tzinfo=dt.UTC).timestamp()
        )
    url, env_label = _metrics(args, snapshot)
    return Settings(
        scope=Scope(env=args.env, namespace=args.namespace, replica=args.replica,
                    env_label=env_label),
        tunables=Tunables(
            window_days=args.days,
            max_workers=args.workers,
            chunk_hours=args.chunk_hours,
            **({"request_coverage": args.request_coverage}
               if getattr(args, "request_coverage", None) else {}),
        ),
        thanos_url=url,
        end_ts=end_ts,
    )


def _snapshot_path(args: argparse.Namespace) -> Path:
    """The snapshot to read: out/<env>/snapshot.json, else the old flat name."""
    return layout.find_snapshot(Path(args.out), args.env, args.end)


def _cloud_name(args: argparse.Namespace) -> str:
    """--cloud, else what the context says, else the default -- and say which."""
    if args.cloud:
        return args.cloud
    found = clouds.detect(context=args.context)
    if not found:
        print(f"note: cloud not recognisable from the context; assuming {clouds.DEFAULT} "
              "(--cloud to set it)", file=sys.stderr)
    return found or clouds.DEFAULT


def _discover_autoscaler(args: argparse.Namespace):
    """Explicit flags win; otherwise read what is running, which needs no
    repository and is what is actually in force."""
    if args.ca_arg:
        return autoscalers.get(args.autoscaler).from_flags(args.ca_arg)
    if has_access(args.context) and not args.no_ca_discovery:
        return autoscalers.discover(context=args.context, namespace=args.ca_namespace,
                                    only=args.autoscaler)
    return None


def cmd_collect(args: argparse.Namespace) -> int:
    settings = _settings(args)
    t0 = time.monotonic()
    cloud = _cloud_name(args)
    facts = collect_cluster_facts(
        context=args.context,
        cluster=args.cluster,
        profile=args.profile,
        region=args.region,
        cloud=cloud,
    )
    found = _discover_autoscaler(args)
    adapter = autoscalers.get(found.kind if found else args.autoscaler)
    # Groups the autoscaler defines that no cloud node pool describes
    # (Karpenter NodePools): their sizes and ceilings are in the cluster.
    group_warnings: list[str] = []
    known = {c.get("name") for c in facts.nodegroup_configs}
    added = [c for c in adapter.nodegroup_configs(found, facts, group_warnings) if c.name not in known]
    facts.nodegroup_configs.extend(asdict(c) for c in added)
    if added:
        print(f"{adapter.name}: {len(added)} node group(s) from the cluster: "
              + ", ".join(f"{c.name} ({c.instance_types[0]})" for c in added))
    for w in group_warnings:
        print(f"note: {w}", file=sys.stderr)
    # Cluster Autoscaler's flags also go where readers before adapters look.
    ca: dict = {}
    if found and found.kind == "cluster-autoscaler":
        ca = {"source": found.source, "args": found.settings["args"]}
    if found:
        settings_used = adapter.scale_down_settings(found)
        print(f"{adapter.name} ({found.source}): {settings_used.non_default()}")
    if summary := adapter.summary(found):
        print(summary, file=sys.stderr)
    for note in adapter.notes(found):
        print(f"note: {note}", file=sys.stderr)
    try:
        # Recorded so the console exporter can place this snapshot -- account,
        # region, pricing profile -- without a hand-maintained list of clusters.
        meta = {k: v for k, v in {
            "cloud": cloud,
            # Where the series came from, so every later query asks the same store.
            "metrics": {"url": settings.thanos_url, "env_label": settings.scope.env_label},
            "cluster": args.cluster, "region": args.region, "profile": args.profile,
            "account": args.account, "context": args.context,
            "in_cluster": not args.context and in_cluster(),
        }.items() if v}
        snapshot = collect(settings, facts=facts, ca=ca, meta=meta)
        snapshot.autoscaler = found.to_dict() if found else {}
        snapshot.warnings.extend(group_warnings)
        if snapshot.window_shortened:
            print(f"note: the metrics store holds only {snapshot.window_days} of the "
                  f"{snapshot.requested_window_days} days asked for; collecting and sizing "
                  f"over {snapshot.window_days}. The report and console say so.",
                  file=sys.stderr)
    except PreflightError as exc:
        print(f"preflight failed: {exc}", file=sys.stderr)
        return 2
    except requests.exceptions.ConnectionError:
        print(f"cannot reach the metrics store at {settings.thanos_url}"
              + (" -- is the kubectl port-forward running?" if args.prometheus_url else ""),
              file=sys.stderr)
        return 2
    if not args.no_hpa_history:
        from rightsizer.sources.hpa_history import collect_hpa_history
        snapshot.hpa_history = collect_hpa_history(
            snapshot, settings.thanos_url, context=args.context, max_workers=args.workers)
    from rightsizer.sources.storage import collect_storage
    from rightsizer.sources.storage import collect_volumes, prune_finished
    try:
        prune_finished(snapshot, settings.thanos_url, max_workers=args.workers)
        snapshot.storage = collect_storage(snapshot, settings.thanos_url, max_workers=args.workers)
        snapshot.volumes = collect_volumes(snapshot, settings.thanos_url, max_workers=args.workers)
    except Exception as exc:  # noqa: BLE001 - the disk check is additive
        print(f"  warning: local storage not collected: {exc}", file=sys.stderr)
    from rightsizer.sources.network import collect_network
    try:
        snapshot.network = collect_network(snapshot, settings.thanos_url, max_workers=args.workers)
    except Exception as exc:  # noqa: BLE001 - the bandwidth check is additive
        print(f"  warning: node network traffic not collected: {exc}", file=sys.stderr)
    from rightsizer.sources.ngtrends import collect_nodegroup_trends
    try:
        snapshot.nodegroup_trends = collect_nodegroup_trends(
            snapshot, settings.thanos_url, max_workers=args.workers)
    except Exception as exc:  # noqa: BLE001 - the trend chart is additive
        print(f"  warning: node-group trends not collected: {exc}", file=sys.stderr)
    path = snapshot.write(layout.snapshot_path(Path(args.out), args.env, args.end))
    f = snapshot.facts
    extra = (
        f", {len(f.workload_constraints)} workload constraints, "
        f"{len(f.nodegroup_configs)} nodegroup configs"
        if f.collected
        else " (no cluster facts: node counts are upper bounds)"
    )
    print(
        f"collected {len(snapshot.usage)} series, {len(snapshot.nodegroups)} node groups, "
        f"{len(snapshot.hpas)} HPAs{extra} in {time.monotonic() - t0:.1f}s -> {path}"
    )
    for warning in snapshot.warnings:
        print(f"  warning: {warning}", file=sys.stderr)
    return 0


def cmd_hpa_history(args: argparse.Namespace) -> int:
    """Add per-minute HPA history to an existing snapshot, over its own window."""
    from rightsizer.sources.hpa_history import collect_hpa_history

    path = _snapshot_path(args)
    if not path.exists():
        print(f"no snapshot at {path}; run `collect` first", file=sys.stderr)
        return 1
    t0 = time.monotonic()
    snapshot = Snapshot.read(path)
    snapshot.hpa_history = collect_hpa_history(
        snapshot, _metrics(args, snapshot)[0], context=args.context, max_workers=args.workers)
    snapshot.write(path)
    sources = {}
    for h in snapshot.hpa_history.values():
        src = h["behavior"]["source"]
        sources[src] = sources.get(src, 0) + 1
    print(f"hpa history for {len(snapshot.hpa_history)} HPAs (behavior: "
          f"{', '.join(f'{n} {k}' for k, n in sorted(sources.items()))}) "
          f"in {time.monotonic() - t0:.1f}s -> {path}")
    return 0


def cmd_network(args: argparse.Namespace) -> int:
    """Add node network traffic to an existing snapshot."""
    from rightsizer.sources.network import collect_network

    path = _snapshot_path(args)
    if not path.exists():
        print(f"no snapshot at {path}; run `collect` first", file=sys.stderr)
        return 1
    t0 = time.monotonic()
    snapshot = Snapshot.read(path)
    snapshot.network = collect_network(snapshot, _metrics(args, snapshot)[0], max_workers=args.workers)
    snapshot.write(path)
    print(f"network traffic for {len(snapshot.network)} node group(s) "
          f"in {time.monotonic() - t0:.1f}s -> {path}")
    return 0


def cmd_kube(args: argparse.Namespace) -> int:
    """Add the live pods, nodes, PDBs and ASGs to an existing snapshot. Read-only."""
    from rightsizer.sources import local
    from rightsizer.sources.kfacts import collect_kube

    path = _snapshot_path(args)
    if not path.exists():
        print(f"no snapshot at {path}; run `collect` first", file=sys.stderr)
        return 1
    # The cluster's local directory when there is one, else the flags -- an OKE
    # cluster has no ~/eks directory and is given --context/--profile instead.
    lc = None if args.no_local else local.find(args.env)
    context = args.context or (lc.context if lc else None)
    if not has_access(context):
        print(f"no context for {args.env}: pass --context (no local cluster directory)",
              file=sys.stderr)
        return 1
    snapshot = Snapshot.read(path)
    t0 = time.monotonic()
    snapshot.facts.kube = collect_kube(
        context=context,
        kubeconfig=args.kubeconfig or (str(lc.kubeconfig) if lc and lc.kubeconfig else None),
        profile=args.profile or (lc.profile if lc else None),
        region=args.region or (lc.region if lc else None),
        cluster=args.cluster or (lc.cluster if lc else None) or args.env,
        cloud=clouds.for_snapshot(snapshot).name)
    snapshot.write(path)
    k = snapshot.facts.kube
    print(f"{len(k['pods'])} pods, {len(k['nodes'])} nodes, {len(k['pdbs'])} PDBs, "
          f"{len(k['asgs'])} ASGs, CA {k['ca'].get('image', '?').rsplit(':', 1)[-1]} "
          f"in {time.monotonic() - t0:.1f}s -> {path}")
    return 0


def cmd_pod_ages(args: argparse.Namespace) -> int:
    """Add each pod's creation time to an existing snapshot's placement."""
    from rightsizer.sources.collect import collect_pod_ages

    path = _snapshot_path(args)
    if not path.exists():
        print(f"no snapshot at {path}; run `collect` first", file=sys.stderr)
        return 1
    snapshot = Snapshot.read(path)
    filled = collect_pod_ages(snapshot, _metrics(args, snapshot)[0])
    snapshot.write(path)
    total = sum(len(v) for v in (snapshot.placements or {}).values())
    print(f"creation time for {filled}/{total} placed pod(s) -> {path}")
    return 0


def cmd_storage(args: argparse.Namespace) -> int:
    """Add node-local disk capacity and usage to an existing snapshot."""
    from rightsizer.sources.storage import collect_storage

    path = _snapshot_path(args)
    if not path.exists():
        print(f"no snapshot at {path}; run `collect` first", file=sys.stderr)
        return 1
    t0 = time.monotonic()
    snapshot = Snapshot.read(path)
    snapshot.storage = collect_storage(snapshot, _metrics(args, snapshot)[0], max_workers=args.workers)
    from rightsizer.sources.storage import collect_volumes, prune_finished
    snapshot.volumes = collect_volumes(snapshot, _metrics(args, snapshot)[0], max_workers=args.workers)
    finished = prune_finished(snapshot, _metrics(args, snapshot)[0], max_workers=args.workers)
    snapshot.write(path)
    print(f"{finished} finished pod(s) dropped from placements; "
          f"{len(snapshot.volumes)} pod(s) with persistent volumes; "
          f"local storage for {len(snapshot.storage)} node group(s) "
          f"in {time.monotonic() - t0:.1f}s -> {path}")
    return 0


def cmd_node_trends(args: argparse.Namespace) -> int:
    """Add node-group CPU, memory and disk utilisation over time to a snapshot."""
    from rightsizer.sources.ngtrends import collect_nodegroup_trends

    path = _snapshot_path(args)
    if not path.exists():
        print(f"no snapshot at {path}; run `collect` first", file=sys.stderr)
        return 1
    t0 = time.monotonic()
    snapshot = Snapshot.read(path)
    snapshot.nodegroup_trends = collect_nodegroup_trends(
        snapshot, _metrics(args, snapshot)[0], max_workers=args.workers)
    snapshot.write(path)
    print(f"utilisation trends for {len(snapshot.nodegroup_trends)} node group(s) "
          f"in {time.monotonic() - t0:.1f}s -> {path}")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    path = _snapshot_path(args)
    if not path.exists():
        print(f"no snapshot at {path}; run `collect` first", file=sys.stderr)
        return 1
    snapshot = Snapshot.read(path)
    # Where the snapshot came from, when the flags do not say: the report is
    # priced in that region and the actions name that cluster. Before this,
    # `analyze` without --context priced an OCI cluster in us-east-1 and, with
    # no --cluster, built no actions at all.
    meta = snapshot.meta or {}
    cloud = clouds.for_snapshot(snapshot, region=args.region, profile=args.profile)
    cluster = args.cluster or meta.get("cluster")
    region = args.region or meta.get("region") or cloud.default_region
    profile = args.profile or meta.get("profile")
    account = args.account or meta.get("account") or ""
    tun = Tunables(**snapshot.tunables) if snapshot.tunables else Tunables()
    if getattr(args, "request_coverage", None):
        # Coverage is an analysis choice, not a collection one: the ladder is
        # already in the snapshot, so it can be changed without re-querying.
        tun = replace(tun, request_coverage=args.request_coverage)
    recs = container_recommendations(snapshot, tun)
    groups = nodegroup_recommendations(snapshot, tun, recs)

    actions = []
    if cluster:
        actions = build_actions(
            snapshot, groups,
            cluster=cluster, region=region,
            profile=profile, discount=args.discount, recommendations=recs,
        )

    report = lambda ext: layout.report_path(Path(args.out), args.env, args.end, ext)  # noqa: E731
    layout.write_atomic(report("json"), to_json(snapshot, groups, recs, actions))
    md = to_markdown(
        snapshot, groups, recs, actions, args.discount,
        region=region, profile=profile, account=account,
    )
    layout.write_atomic(report("md"), md)
    if args.stdout:
        print(md)
    else:
        usd = sum(a.monthly_delta for a in actions if not a.blocked)
        total = sum(g.node_hours_saved_month for g in groups)
        print(
            f"NET ${usd:,.2f}/month across "
            f"{len([a for a in actions if not a.blocked])} ready action(s); "
            f"{sum(1 for r in recs if r.actionable)}/{len(recs)} containers actionable "
            f"-> {report('md')}"
        )
    return 0


def cmd_trends(args: argparse.Namespace) -> int:
    """Per-container usage trends, for every container in the snapshot.

    Every one, not only those a recommendation touches: judging an instance
    type change means reading what everything on those nodes used, and the
    containers left alone are exactly the ones nobody has looked at. Those a
    recommendation touches go first, so a capped or interrupted run still
    covers them. Written as one file each so the console fetches a trend only
    when someone opens it, rather than shipping every series to every reader.
    """
    path = _snapshot_path(args)
    if not path.exists():
        print(f"no snapshot at {path}; run `collect` first", file=sys.stderr)
        return 1
    snapshot = Snapshot.read(path)
    tun = Tunables(**snapshot.tunables) if snapshot.tunables else Tunables()
    recs = container_recommendations(snapshot, tun)

    def touched(r) -> bool:
        return r.actionable or any(b.severity in ("unsafe", "zero-savings") for b in r.blockers)

    keys = [
        TrendKey(r.namespace, r.workload, r.container, r.kind)
        for r in sorted(recs, key=lambda r: not touched(r))
        if touched(r) or not args.actionable_only
    ]
    # Deduplicate while keeping order; --limit caps it when asked (0 = all).
    seen: dict[str, TrendKey] = {}
    for key in keys:
        seen.setdefault(key.slug, key)
    selected = list(seen.values())[: args.limit] if args.limit else list(seen.values())

    settings = _settings(args, snapshot)
    # --trends-out keeps its old meaning (a root with one directory per env).
    target = (Path(args.trends_out) / args.env if args.trends_out
              else layout.trends_dir(Path(args.ui_data), args.env))
    t0 = time.monotonic()
    detail = {
        TrendKey(r.namespace, r.workload, r.container, r.kind).slug for r in recs if touched(r)
    }
    written, skipped, empty = write_trends(
        settings, selected, target, refresh=args.refresh,
        # Each container already issues its seven aggregates at once.
        parallel=max(1, args.workers // 2), detailed=detail,
    )
    note = f", {skipped} already present" if skipped else ""
    print(
        f"wrote {written} trend file(s){note} to {target} "
        f"in {time.monotonic() - t0:.1f}s"
    )
    if empty:
        print(
            f"FAILED: {len(empty)} of {written} trend(s) came back with no series at "
            f"all -- the query matched nothing. First: {', '.join(empty[:3])}",
            file=sys.stderr,
        )
        return 1
    if args.limit and len(seen) > args.limit:
        print(
            f"note: {len(seen) - args.limit} more container(s) qualify; "
            f"raise --limit to cover them",
            file=sys.stderr,
        )
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    """Simulate a specific set of request changes, not the whole recommendation.

    Reads a frozen snapshot, so it costs nothing and can be re-run against the
    same data while the numbers are tuned.
    """
    path = _snapshot_path(args)
    if not path.exists():
        print(f"no snapshot at {path}; run `collect` first", file=sys.stderr)
        return 1
    lines: list[str] = []
    if args.plan_file:
        lines.extend(Path(args.plan_file).read_text().splitlines())
    lines.extend(args.set or [])
    if not lines:
        print("nothing to simulate: pass --plan-file and/or --set", file=sys.stderr)
        return 2
    try:
        plan = Plan.parse(lines)
    except PlanError as e:
        print(f"plan: {e}", file=sys.stderr)
        return 2

    snapshot = Snapshot.read(path)
    tun = Tunables(**snapshot.tunables) if snapshot.tunables else Tunables()
    recs = container_recommendations(snapshot, tun)
    resolved, missing = resolve(snapshot, plan, recs)
    if not resolved:
        print("no change in the plan matched a container in this snapshot", file=sys.stderr)
        for m in missing:
            print(f"  not found: {m}", file=sys.stderr)
        return 1

    # Prices are regional and the snapshot does not record where it came from,
    # so an unset --region is a guess and is reported as one rather than
    # quietly pricing eu-west-2 nodes at us-east-1 rates.
    cloud = clouds.for_snapshot(snapshot, region=args.region, profile=args.profile)
    region = args.region or (snapshot.meta or {}).get("region") or cloud.default_region
    unpriced: set[str] = set()

    def price_of(t: str) -> float:
        if not t:
            return 0.0
        monthly = cloud.monthly_price(t, args.discount, region)
        if not monthly:
            unpriced.add(t)
        return monthly

    outcomes = simulate(snapshot, plan, resolved, tun, price_of=price_of)
    # Per-container review of the edits, plus what they do to pods the plan never
    # names: CA packs by requests, so a cut can leave no-request pods with no room.
    findings = sorted(
        review(resolved) + pressure_findings(snapshot, outcomes, tun),
        key=lambda f: (Finding.ORDER.get(f.severity, 3), f.code, f.subject),
    )
    text = to_text(plan, resolved, missing, outcomes, findings)
    if not args.region:
        text += f"\n\n  NOTE: priced in {region} (--region not given)."
    if unpriced:
        # Silently reporting $0 would read as "this change is free".
        text += (
            "\n\n  NOTE: no price for " + ", ".join(sorted(unpriced))
            + f" in {region}; node counts above are real, dollar figures are not."
        )
    if args.plan_out:
        Path(args.plan_out).write_text(text + "\n")
        print(f"wrote {args.plan_out}")
    else:
        print(text)
    # A plan with an unsafe finding exits non-zero so CI can gate on it.
    return 1 if any(f.severity == "unsafe" for f in findings) else 0


def cmd_run(args: argparse.Namespace) -> int:
    """collect, then kube (live objects for the exact replay), then analyze."""
    rc = cmd_collect(args)
    if rc:
        return rc
    if not args.no_kube:
        try:
            if cmd_kube(args):
                print("  warning: live Kubernetes facts not collected; the export falls back to "
                      "the approximate Cluster Autoscaler replay for this cluster", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - the exact replay is additive
            print(f"  warning: live Kubernetes facts not collected ({exc}); the export falls back "
                  "to the approximate Cluster Autoscaler replay for this cluster", file=sys.stderr)
    return cmd_analyze(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rightsizer",
        description="Deterministic EKS right-sizing with blockers. Read-only.",
    )
    parser.add_argument("--env", help="cluster name, e.g. prod-eu1. Its context, profile, region "
                                      "and account are read from ~/eks/{prod,stage}/<env>/. A "
                                      "comma-separated list, or `all`, runs each in turn.")
    parser.add_argument("--namespace", default="all")
    parser.add_argument("--replica", default="all", help="prometheus_replica de-duplication")
    parser.add_argument("--days", type=int, default=30,
                        help="history to size from. When the metrics store holds less, the "
                             "window shrinks to what exists and the outputs say so.")
    parser.add_argument("--end", help="window end, ISO date/time UTC (default: now). "
                                      "Replays a historical window for backtesting.")
    parser.add_argument("--workers", type=int, default=8, help="concurrent Thanos queries")
    parser.add_argument("--chunk-hours", type=int, default=12)
    parser.add_argument("--thanos-url", default=None,
                        help=f"the shared Thanos, where every series carries env=<cluster> "
                             f"(default: what the snapshot was collected from, else {DEFAULT_THANOS_URL})")
    parser.add_argument("--prometheus-url", default=None,
                        help="one cluster's own Prometheus instead of Thanos, e.g. "
                             "http://localhost:9090 behind `kubectl port-forward`. It has no env "
                             "label, so none is pinned. A --days past its retention is shortened to fit.")
    parser.add_argument("--env-label", default=None,
                        help="the label carrying the cluster name (default: env on Thanos, none "
                             "with --prometheus-url; \"\" for none)")
    parser.add_argument("--context", help="kubectl context; enables Kubernetes constraints "
                                          "(default: the cluster directory's kubeconfig)")
    parser.add_argument("--kubeconfig", help="kubeconfig file for --context (default: the cluster "
                                             "directory's, else $KUBECONFIG)")
    parser.add_argument("--no-local", action="store_true",
                        help="do not look the cluster up under ~/eks/{prod,stage} "
                             "($RIGHTSIZER_CLUSTERS overrides the roots)")
    parser.add_argument("--cloud", choices=clouds.registry.names(), default=None,
                        help="where the cluster runs (default: recognised from the context, "
                             f"else {clouds.DEFAULT})")
    parser.add_argument("--autoscaler", choices=autoscalers.registry.names(), default=None,
                        help="the node autoscaler to read and model (default: whichever is "
                             "found running, tried in order: "
                             f"{', '.join(autoscalers.registry.names())})")
    parser.add_argument("--cluster", help="cluster name in the cloud; enables node group config "
                                          "(default: from the context ARN, else --env)")
    parser.add_argument("--profile", help="cloud credentials profile (AWS: default AWS_PROFILE in "
                                          "the cluster directory's .envrc)")
    parser.add_argument("--region", help="cloud region (AWS: default AWS_REGION in the .envrc, "
                                         "else the context ARN)")
    parser.add_argument(
        "--ca-arg", action="append", metavar="KEY=VALUE",
        help="Cluster Autoscaler flag as deployed (instead of reading it), e.g. "
             "--ca-arg scale-down-utilization-threshold=0.5 "
             "--ca-arg ignore-daemonsets-utilization=true. Repeatable. "
             "Unset flags fall back to upstream defaults.")
    parser.add_argument("--ca-namespace", default="kube-system",
                        help="namespace to look for the Cluster Autoscaler deployment in")
    parser.add_argument("--no-kube", action="store_true",
                        help="`run`: skip the live pods/nodes/PDBs/ASGs (kubectl get + aws describe, "
                             "read-only) that the exact Cluster Autoscaler replay needs")
    parser.add_argument("--no-hpa-history", action="store_true",
                        help="skip the per-minute HPA history (a few minutes of extra queries)")
    parser.add_argument("--no-ca-discovery", action="store_true",
                        help="do not look for a node autoscaler in the cluster")
    parser.add_argument("--account", help="AWS account id, for the report header "
                                          "(default: from the context ARN)")
    parser.add_argument("--discount", type=float, default=0.12,
                        help="enterprise discount applied to list price (0.12 = 12%%)")
    parser.add_argument("--out", default=os.environ.get("RIGHTSIZER_OUT", str(layout.DEFAULT_OUT)),
                        help="snapshot and report directory, one sub-directory per cluster "
                             "(default: $RIGHTSIZER_OUT, else out/ in the rightsizer checkout)")
    parser.add_argument("--ui-data", default=os.environ.get("RIGHTSIZER_UI_DATA", str(layout.DEFAULT_DATA)),
                        help="the console's data directory; trends go to <ui-data>/<env>/trends "
                             "(default: $RIGHTSIZER_UI_DATA, else ../frontend/public/data)")
    parser.add_argument("-v", "--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)
    parser.add_argument("--trends-out", default=None,
                        help="override: write trends to <trends-out>/<env>/ instead of "
                             "<ui-data>/<env>/trends")
    parser.add_argument("--limit", type=int, default=0,
                        help="maximum containers to collect trends for (default: all; "
                             "those a recommendation touches go first)")
    parser.add_argument("--actionable-only", action="store_true",
                        help="trends only for containers a recommendation touches")
    parser.add_argument("--request-coverage", choices=["p50", "p75", "p90", "p95", "p99", "max"],
                        help="which point on the per-replica distribution sizes a CPU "
                             "request (default p95); p99 sizes for the tail")
    parser.add_argument("--refresh", action="store_true",
                        help="re-collect trends that are already on disk")
    parser.add_argument("--plan-file", help="plan to simulate: one change per line, "
                                            "`namespace/workload/container cpu=100m mem=3Gi`")
    parser.add_argument("--set", action="append", metavar="CHANGE",
                        help="a plan line given inline; repeatable")
    parser.add_argument("--plan-out", help="write the plan report here instead of stdout")

    for name, fn, helptext in (
        ("collect", cmd_collect, "query Thanos and freeze a snapshot"),
        ("analyze", cmd_analyze, "recommend from an existing snapshot"),
        ("trends", cmd_trends, "per-container usage trends for the console"),
        ("hpa-history", cmd_hpa_history, "add per-minute HPA history to an existing snapshot"),
        ("storage", cmd_storage, "add node-local disk and persistent-volume facts to an existing snapshot"),
        ("network", cmd_network, "add node network traffic to an existing snapshot"),
        ("pod-ages", cmd_pod_ages, "add each pod's creation time to an existing snapshot"),
        ("kube", cmd_kube, "add the live pods, nodes, PDBs and ASGs to an existing snapshot (read-only)"),
        ("node-trends", cmd_node_trends, "add node-group utilisation over time to an existing snapshot"),
        ("plan", cmd_plan, "simulate a specific set of request changes"),
        ("run", cmd_run, "collect, kube, then analyze"),
        ("clusters", cmd_clusters, "list the clusters found locally, and their snapshots"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.set_defaults(func=fn)
        # Also accepted after the subcommand (`rightsizer trends -v --refresh`).
        # SUPPRESS keeps an absent one from overriding the same flag given before it.
        p.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                       help="progress as it runs")
        if name == "trends":
            p.add_argument("--refresh", action="store_true", default=argparse.SUPPRESS,
                           help="re-collect trends that are already on disk")
        if name not in ("collect", "hpa-history", "storage", "network", "pod-ages", "kube", "node-trends",
                        "clusters"):
            p.add_argument("--stdout", action="store_true", help="print the markdown report")
    return parser


def cmd_clusters(args: argparse.Namespace) -> int:
    """Every cluster directory found, what was read from it, and its snapshot."""
    from rightsizer.sources import local

    found = local.discover()
    if not found:
        print(f"no cluster directories under {', '.join(map(str, local.roots()))}", file=sys.stderr)
        return 1
    for c in found:
        snap = layout.find_snapshot(Path(args.out), c.env)
        when = ""
        if snap.exists():
            # collected_at sits near the top of the file; a regex on the first
            # few KB avoids parsing a 16 MB snapshot just to list it.
            with snap.open() as fh:
                m = re.search(r'"collected_at":\s*"([^"]+)"', fh.read(4096))
            when = m.group(1)[:16].replace("T", " ") if m else "present"
        ready = "ok " if c.context and c.profile else "?? "
        print(f"{ready}{c.env:22s} {c.region or '?':12s} {c.profile or '?':14s} "
              f"{c.account or '?':13s} {('snapshot ' + when) if when else 'no snapshot'}")
    print(f"\n{len(found)} cluster(s). `??` means the kubeconfig context or AWS profile was not "
          "found; pass --context/--profile for it.")
    return 0


def _apply_kubeconfig(args: argparse.Namespace) -> None:
    """Fill --cloud, --cluster, --region and --profile from the context itself.

    A kubeconfig a cloud's CLI wrote authenticates by running that CLI with the
    cluster named in it (`oci ce cluster generate-token --cluster-id <ocid>
    --region <r>`), so the context already says which cluster, where, and as
    whom. Anything given explicitly wins. Read from the file only.
    """
    if not args.context:
        return
    from rightsizer.sources.kube import context_exec, context_hint

    hint = context_hint(args.context, args.kubeconfig)
    if hint:
        print(f"error: {hint}", file=sys.stderr)
        raise SystemExit(2)
    found = clouds.from_kubeconfig(context_exec(args.context, args.kubeconfig))
    if not found:
        return
    cloud, info = found
    if args.cloud and args.cloud != cloud:
        return  # the user said otherwise; their flags stand
    filled = {}
    if not args.cloud:
        args.cloud = filled["cloud"] = cloud
    for key in ("cluster", "region", "profile"):
        if info.get(key) and not getattr(args, key):
            setattr(args, key, info[key])
            filled[key] = info[key]
    if filled:
        print("from the kubeconfig: " + ", ".join(f"{k} {v}" for k, v in filled.items()),
              file=sys.stderr)


def _apply_local(args: argparse.Namespace) -> None:
    """Fill what was not given from the cluster's own directory.

    Explicit flags always win. When the context comes from the directory, so
    does its kubeconfig: KUBECONFIG is pointed at it for this process only, so
    every kubectl call -- whichever module makes it -- reaches the right cluster
    without the file being threaded through each one. The shell is untouched.
    """
    if args.kubeconfig:
        os.environ["KUBECONFIG"] = str(Path(args.kubeconfig).expanduser())
    if args.no_local:
        return
    from rightsizer.sources import local

    lc = local.find(args.env)
    if lc is None:
        print(f"note: no local directory for {args.env} under "
              f"{', '.join(map(str, local.roots()))}; using flags and $KUBECONFIG only",
              file=sys.stderr)
        return
    if args.context is None and lc.context and lc.kubeconfig:
        args.context = lc.context
        if not args.kubeconfig:
            os.environ["KUBECONFIG"] = str(lc.kubeconfig)
    args.cluster = args.cluster or lc.cluster or args.env
    args.profile = args.profile or lc.profile
    args.region = args.region or lc.region
    args.account = args.account or lc.account
    # What this run will actually use -- overrides included -- not what the
    # directory holds.
    parts = [f"{k} {getattr(args, k)}" for k in ("context", "profile", "region", "account")
             if getattr(args, k)]
    if os.environ.get("KUBECONFIG"):
        parts.append(f"kubeconfig {os.environ['KUBECONFIG']}")
    print(f"using {args.env} ({lc.directory}): " + ", ".join(parts), file=sys.stderr)


#: Commands that make sense per cluster, so `--env a,b` / `--env all` runs each.
_PER_CLUSTER = {"collect", "run", "analyze", "trends", "hpa-history", "storage", "network", "pod-ages", "kube",
                "node-trends"}
#: Commands that call kubectl, and so say which cluster they reached.
_READS_CLUSTER = {"collect", "run", "hpa-history", "kube"}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    Path(args.out).mkdir(parents=True, exist_ok=True)
    if not hasattr(args, "stdout"):
        args.stdout = False
    if args.command == "clusters":
        return args.func(args)
    if not args.env:
        print("--env is required (a cluster name, a comma-separated list, or `all`)",
              file=sys.stderr)
        return 2

    if args.env == "all":
        from rightsizer.sources import local
        envs = [c.env for c in local.discover()]
    else:
        envs = [e.strip() for e in args.env.split(",") if e.strip()]
    if len(envs) > 1 and args.command not in _PER_CLUSTER:
        print(f"`{args.command}` takes one cluster, not {len(envs)}", file=sys.stderr)
        return 2

    worst = 0
    saved = os.environ.get("KUBECONFIG")
    for env in envs:
        one = copy.copy(args)
        one.env = env
        if len(envs) > 1:
            print(f"\n=== {env} ===", file=sys.stderr)
        try:
            _apply_local(one)
            _apply_kubeconfig(one)
            if one.command in _READS_CLUSTER and not one.context and use_in_cluster_config():
                print("in-cluster: kubectl uses this pod's service account", file=sys.stderr)
            worst = max(worst, one.func(one))
        finally:
            # Each cluster gets its own kubeconfig; never leak one into the next.
            if saved is None:
                os.environ.pop("KUBECONFIG", None)
            else:
                os.environ["KUBECONFIG"] = saved
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
