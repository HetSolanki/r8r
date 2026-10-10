"""Export every collected snapshot into one fleet dataset for the console.

Clusters are discovered, not listed: every ``out/<env>/snapshot.json`` is the
current period, and every ``out/<env>/snapshot-<YYYY-MM-DD>.json`` is a
backtest ending that day (the older flat ``out/snapshot-<env>.json`` is still
read). Each cluster and period is written to its own file under the console's
``public/data/<env>/``, and ``index.json`` is rebuilt from those files, so
exporting one cluster never touches another's (see ``rightsizer.core.layout``). Where a cluster lives -- region, AWS account, the
profile prices are fetched with -- comes from the snapshot itself (recorded at
``collect`` time), then from the optional ``clusters.json`` beside this file,
then from inference. Adding a cluster is running ``collect``; no code changes.

    python3 export_ui.py [DATA_DIR] [--env a,b] [--out-dir out] [--config clusters.json] [--discount 0.12]
    python3 export_ui.py --index-only    # rebuild index.json from the cluster files
"""
from __future__ import annotations
import argparse, dataclasses, functools, json, pathlib, re, sys
from rightsizer.core.models import Snapshot
from rightsizer.core.config import Tunables
from rightsizer.analysis.recommend import container_recommendations, nodegroup_recommendations
from rightsizer.analysis.actions import build_actions
from rightsizer.output.report import disposition, fleet_runrate, summarise
from rightsizer.adapters import autoscalers, clouds, group_change, group_source
from rightsizer.adapters.clouds import HOURS_PER_MONTH
from rightsizer.engine.autoscaler import estimate_scale_down
from rightsizer.engine.eviction import _observed_at, rules_from_snapshot, simulate_group
from rightsizer.engine import pressure
from rightsizer.analysis.advise import advise, family_of
from types import SimpleNamespace
from rightsizer.engine.simulate import replica_floors, replica_plan, scenarios
from rightsizer.core import layout

DEFAULT_DISCOUNT = 0.12
DEFAULT_REGION = "us-east-1"
SCHEMA_VERSION = 12
CONFIG_PATH = layout.HOME / "clusters.json"

#: The flat name, kept for anything that still imports it. Discovery itself is
#: :func:`rightsizer.core.layout.discover_snapshots`; a hand-made variant such as
#: snapshot-x.5m.json is skipped and said so.
SNAPSHOT_RE = layout.LEGACY_SNAPSHOT_RE


def load_config(path: pathlib.Path) -> dict:
    """Optional overrides: {"clusters": {env: {...}}, "known": [...], "periods": {...}}."""
    return json.loads(path.read_text()) if path.exists() else {}


def discover(out_dir: pathlib.Path) -> tuple[dict[str | None, list[str]], list[str]]:
    """{period end (None = current): [env, ...]}, plus the files that did not match."""
    found, skipped = layout.discover_snapshots(out_dir)
    return {end: sorted(envs) for end, envs in found.items()}, skipped


def _region_from_nodes(snap) -> str | None:
    for n in snap.facts.node_facts or []:
        labels = n.get("labels") or {}
        region = (labels.get("topology.kubernetes.io/region")
                  or labels.get("failure-domain.beta.kubernetes.io/region"))
        if region:
            return region
    return None


def resolve_meta(env: str, snap, config: dict) -> dict:
    """Where a cluster lives. clusters.json wins, then the snapshot, then inference.

    Region matters beyond display: prices are regional. When nothing says where
    a cluster is, the default is used *and flagged*, rather than quietly pricing
    eu-west-2 nodes at us-east-1 rates.
    """
    override = (config.get("clusters") or {}).get(env) or {}
    recorded = snap.meta or {}
    region = override.get("region") or recorded.get("region") or _region_from_nodes(snap)
    return {
        "cluster": override.get("cluster") or recorded.get("cluster") or env,
        "region": region or DEFAULT_REGION,
        "regionAssumed": not region,
        "profile": override.get("profile") or recorded.get("profile"),
        "account": str(override.get("account") or recorded.get("account") or ""),
        "source": "clusters.json" if override else ("snapshot" if recorded else "inferred"),
    }


def build(env: str, out_dir: pathlib.Path, end: str | None = None, *,
          config: dict | None = None, discount: float = DEFAULT_DISCOUNT) -> dict | None:
    path = layout.find_snapshot(out_dir, env, end)
    if not path.exists():
        return None
    snap = Snapshot.read(path)
    meta = resolve_meta(env, snap, config or {})
    cluster, profile, region, account = (
        meta["cluster"], meta["profile"], meta["region"], meta["account"])
    DISCOUNT = discount  # noqa: N806 - the per-run discount, read by the price closure
    tun = Tunables(**snap.tunables)
    recs = container_recommendations(snap, tun)
    groups = nodegroup_recommendations(snap, tun, recs)
    actions = build_actions(snap, groups, cluster=cluster, region=region,
                            profile=profile, discount=DISCOUNT, recommendations=recs)
    current = fleet_runrate(snap, region, profile, DISCOUNT)
    cloud = clouds.for_snapshot(snap, region=region, profile=profile)
    price = lambda t: cloud.monthly_price(t, DISCOUNT)  # noqa: E731
    by_name = {g.name: g for g in groups}

    usage_idx = _usage_index(snap)
    autoscaler, found = autoscalers.for_snapshot(snap)
    # None: nothing was read from the cluster, so no estimate is shown.
    ca = autoscaler.scale_down_settings(found) if found else None
    rules = rules_from_snapshot(snap)

    # HPAs whose ceiling the load reaches, placed on the node group that runs
    # most of their pods.
    from rightsizer.engine.simulate import replica_ceilings
    ceilings_by_group: dict[str, list[dict]] = {}
    for c in replica_ceilings(snap, recs):
        shares = snap.workload_nodegroups.get(c.key) or {}
        home = max(shares, key=shares.get) if shares else ""
        ceilings_by_group.setdefault(home, []).append({
            "key": c.key, "hpa": c.hpa, "max": c.current_max, "suggestedMax": c.suggested_max,
            "when": c.when, "pinnedMinutes": c.pinned_minutes, "cappedNow": c.capped_now,
            "peakWantedNow": c.peak_wanted_now, "cappedAfter": c.capped_after,
            "peakWantedAfter": c.peak_wanted_after, "cappedAtSuggested": c.capped_at_suggested,
            "basis": c.basis,
        })

    ngs = []
    for g in snap.nodegroups:
        cfg = snap.facts.config_for(g.name) or {}
        rec = by_name.get(g.name)
        act = next((a for a in actions if a.subject == g.name and a.monthly_delta < 0), None)
        it = (cfg.get("instance_types") or [""])[0]
        # One search per group. Computing the scenarios a second time here to
        # read the advice off them would be a second path to the same number,
        # and two paths to one number do not stay equal.
        decision = _decision(snap, g, it, recs, price, profile, region, cfg, ca, rules)
        rec_saving = (decision.get("advice") or {}).get("monthlyDelta") or 0.0
        # The markdown report's disposition, from the same function -- the
        # console is where decisions get made, so it must not say less.
        verdict, scale_in, why = disposition(g, rec, act)
        ngs.append({
            "verdict": verdict, "scaleIn": scale_in, "why": why,
            "blockers": [{"code": b.code, "severity": b.severity, "detail": b.detail}
                         for b in (rec.blockers if rec else [])],
            "evidence": rec.evidence if rec else {},
            "name": g.name, "nodes": g.nodes_current, "instanceType": it,
            "min": cfg.get("min_size"), "desired": cfg.get("desired_size"), "max": cfg.get("max_size"),
            "capacityType": cfg.get("capacity_type", ""),
            "cpuUtil": round(g.workload_cpu_requests / g.allocatable_cpu * 100, 1) if g.allocatable_cpu else 0,
            "memUtil": round(g.workload_mem_requests / g.allocatable_mem * 100, 1) if g.allocatable_mem else 0,
            "avg30d": round(g.nodes_avg_30d, 1), "min30d": g.nodes_min_30d, "peak30d": g.nodes_peak_30d,
            # null when the snapshot predates the check -- unknown, not zero
            "notReady": g.nodes_not_ready, "cordoned": g.nodes_cordoned,
            "target": rec.nodes_target if rec else g.nodes_current,
            "binding": rec.binding_constraint if rec else "",
            **decision,
            "autoscaler": _pool_estimate(g, decision, price) or _ca_estimate(snap, g, cfg, ca, price, it, rules),
            # A Karpenter pool's nodes today, by size: what the pool runs is
            # not one type times a count anyone set.
            "fleet": next((s.get("fleet") for s in decision.get("scenarios") or [] if s.get("key") == "asis"), []),
            "drivers": _drivers(recs, g.name, usage_idx),
            "sim": _sim(snap, g, cfg, recs, price, it),
            # Every candidate type, so the console can price one the advice did not pick.
            "instanceChoices": _instance_choices(snap, g, it, price, profile, region, DISCOUNT, cfg=cfg),
            # Who changes this group's shape, and how, for the console's own re-pricing.
            "source": group_source(snap, cfg),
            "retype": group_change(snap, cfg).retype_plan(cfg),
            "replicaChanges": _replica_changes(snap, g.name),
            # maxReplicas the load reaches -- today, or once requests are cut.
            "hpaCeilings": ceilings_by_group.get(g.name, []),
            # 6-hourly node-level usage and capacity, for the before/after chart.
            "trend": snap.nodegroup_trends.get(g.name),
            "monthlyCost": round(price(it) * g.nodes_current, 2) if it else 0,
            # Two different questions, and conflating them made the Clusters
            # tab promise $5,245 while the Recommendations tab -- following this
            # tool's own advice -- delivered $5,107. `saving` is what you get by
            # doing what it says; `savingIfEverything` is the best option on the
            # board, including the migrations the advice explicitly rejects as
            # not worth the steps.
            "saving": round(abs(rec_saving), 2),
            "savingIfEverything": round(abs(act.monthly_delta), 2) if act else 0,
            "action": act.summary if act else "",
            "prerequisite": act.prerequisite if act and act.prerequisite != "none" else "",
            "empty": False,
        })
    seen = {g.name for g in snap.nodegroups}
    for c in snap.facts.nodegroup_configs:
        if c["name"] in seen:
            continue
        it = (c.get("instance_types") or [""])[0]
        ngs.append({
            "verdict": "EMPTY", "scaleIn": "n/a",
            "why": (f"Costs $0 today. max_size={c.get('max_size', 0)} x {it} is latent spend "
                    "if anything schedules here."),
            "blockers": [], "evidence": {}, "notReady": None, "cordoned": None,
            "name": c["name"], "nodes": 0, "instanceType": it, "min": c.get("min_size"),
            "desired": c.get("desired_size"), "max": c.get("max_size"),
            "capacityType": c.get("capacity_type", ""), "cpuUtil": None, "memUtil": None,
            "avg30d": 0, "min30d": 0, "peak30d": 0, "target": 0, "binding": "",
            "monthlyCost": 0, "saving": 0, "action": "", "prerequisite": "",
            "latent": round(price(it) * int(c.get("max_size", 0)), 2) if it else 0,
            # An empty group has nothing to simulate, but the key must still be
            # present: the UI reads it unconditionally and a missing key is an
            # undefined, not an empty list.
            "scenarios": [],
            "drivers": [],
            "replicaChanges": [],
            "advice": None,
            "optionsConsidered": 0,
            "autoscaler": None,
            "empty": True,
        })

    # Every container, actionable ones first by impact. Capping this list hid
    # exactly the rows a reader most needs -- the ones the tool declined to
    # touch, and why (OOMKilled, no request declared, insufficient data).
    top = sorted(
        recs,
        key=lambda r: (not r.actionable, -max(
            abs((r.cpu_recommended - r.cpu_request) * max(r.replicas, 1))
            if r.cpu_recommended is not None else 0.0,
            abs((r.mem_recommended - r.mem_request) * max(r.replicas, 1) / 1024**3)
            if r.mem_recommended is not None else 0.0,
        ), r.namespace, r.workload, r.container),
    )
    summary = summarise(actions, current)
    export_warnings = []
    if meta["regionAssumed"]:
        export_warnings.append(
            f"region not recorded for {env}; priced as {region}. Re-run collect with "
            "--region, or add it to clusters.json.")
    if not current and snap.nodegroups and any(g.nodes_current for g in snap.nodegroups):
        export_warnings.append(
            "no instance prices resolved, so every dollar figure for this cluster is $0 -- "
            f"check the {cloud.title} credentials (collect --profile, or clusters.json) and --cluster.")
    if snap.window_shortened:
        export_warnings.append(
            f"only {snap.window_days} of the {snap.requested_window_days} requested days of "
            "metrics history exist, so every recommendation is sized from that shorter window "
            "and may miss a weekly or monthly peak.")
    export_warnings.extend(autoscaler.notes(found))
    return {
        "env": env, "cluster": cluster, "region": region, "account": account,
        "regionAssumed": meta["regionAssumed"], "metaSource": meta["source"],
        "collectedAt": snap.collected_at, "windowDays": snap.window_days,
        "requestedWindowDays": snap.requested_window_days or snap.window_days,
        "hasFacts": snap.facts.collected,
        "cloud": cloud.name,
        "autoscaler": {"kind": autoscaler.name, "title": autoscaler.title,
                       "source": found.source if found else None,
                       # Replayed either by the adapter's own rules or, for a
                       # Karpenter pool whose nodes and pods were read, by the
                       # provisioning + consolidation replay (engine/karpenter.py).
                       "modelled": autoscaler.models_scale_down or (
                           bool(ngs) and all((n.get("autoscaler") or {}).get("replayed")
                                             for n in ngs if not n.get("empty")))},
        "caConfigured": found is not None,
        "caThreshold": ca.utilization_threshold if ca else None,
        # The live cluster for the exact replay (kube-scheduler + CA v1.35), shared
        # by every node group's model; absent without `rightsizer kube` facts.
        "live": _live_model(snap),
        "discount": DISCOUNT,
        # The report's cost summary, from the same function the report uses.
        "summary": summary,
        "exportWarnings": export_warnings,
        "currentRunRate": round(current, 2),
        # The fleet headline must be what following the recommendations yields.
        # Summing the best option per group promised savings the tool's own
        # advice tells you not to take.
        "net": -round(sum(n["saving"] for n in ngs), 2),
        "netIfEverything": round(sum(a.monthly_delta for a in actions if not a.blocked), 2),
        "containersActionable": sum(1 for r in recs if r.actionable),
        "containersTotal": len(recs),
        "nodesTotal": sum(g.nodes_current for g in snap.nodegroups),
        "warnings": snap.warnings,
        "actions": [{
            "kind": a.kind, "subject": a.subject, "summary": a.summary,
            "delta": round(a.monthly_delta, 2), "mechanism": a.mechanism,
            "severity": a.severity, "surface": a.surface, "prerequisite": a.prerequisite,
            "verifiedBy": a.verified_by, "command": a.command, "evidence": a.evidence,
            "blocked": a.blocked,
            "blockers": [{"code": b.code, "severity": b.severity, "detail": b.detail}
                         for b in a.relevant_blockers],
        } for a in actions],
        "nodegroups": ngs,
        "containers": [dict({
            "ns": r.namespace, "workload": r.workload, "container": r.container,
            # `replicas` is the 30-day *maximum* pod count, which is what a
            # column headed "Repl" was showing: api-server read 104
            # while 75 were running, because its floor was 100 until 16 Sept.
            # Three counts ship, and the column says which is which.
            "kind": r.kind, "replicas": r.replicas, "nodegroup": r.nodegroup,
            "replicasNow": _running_now(snap).get(
                (r.namespace, r.workload, r.kind), 0),
            "replicasAvg": _avg_replicas(usage_idx, r),
            "cpuReq": round(r.cpu_request, 4),
            "cpuRec": round(r.cpu_recommended, 4) if r.cpu_recommended is not None else None,
            "memReq": round(r.mem_request / 1024**3, 3),
            "memRec": round(r.mem_recommended / 1024**3, 3) if r.mem_recommended is not None else None,
            "cpuFleetPeak": round(r.cpu_total_peak, 3),
            "coinc": round(r.cpu_coincidence, 3) if r.cpu_coincidence is not None else None,
            "cpuDelta": round(
                ((r.cpu_recommended - r.cpu_request) * max(r.replicas, 1))
                if r.cpu_recommended is not None else 0.0, 3),
            "memDelta": round(
                ((r.mem_recommended - r.mem_request) * max(r.replicas, 1) / 1024**3)
                if r.mem_recommended is not None else 0.0, 3),
            "direction": _direction(
                ((r.cpu_recommended - r.cpu_request) * max(r.replicas, 1))
                if r.cpu_recommended is not None else 0.0,
                ((r.mem_recommended - r.mem_request) * max(r.replicas, 1) / 1024**3)
                if r.mem_recommended is not None else 0.0),
            "basis": r.basis,
            "actionable": r.actionable,
            "blockers": [{"code": b.code, "severity": b.severity, "detail": b.detail}
                         for b in r.blockers],
        }, **_usage_fields(usage_idx, r, "cpu"), **_usage_fields(usage_idx, r, "mem", 1024**3))
            for r in top],
    }


def _pool_estimate(g, decision, price) -> dict | None:
    """What Karpenter does to this pool by itself, from the consolidation replay.

    The baseline scenario of a Karpenter pool carries the replay's outcome with
    nothing edited (`Scenario.karpenter`). Shaped like the Cluster Autoscaler
    estimate so the console's panel reads either.
    """
    asis = next((s for s in decision.get("scenarios") or [] if s.get("key") == "asis"), None)
    k = (asis or {}).get("karpenter")
    if not k:
        return None
    removed = max(0, g.nodes_current - k["nodesAfter"])
    saving = max(0.0, round((asis.get("monthlyUsd") or 0.0) - k["monthlyAfter"], 2))
    reasons = list(k.get("notes") or [])
    fleet_after = k["fleetAfter"]
    if k.get("stuck"):
        # Karpenter consolidates continuously: a replay of today that ends
        # elsewhere is a gap in the model, never a saving. Today's fleet is
        # what Karpenter keeps.
        removed, saving, fleet_after = 0, 0.0, asis.get("fleet") or fleet_after
    return {
        "source": "Karpenter provisioning and consolidation, replayed on the recorded nodes and pods",
        "threshold": 1.0, "unneededNodes": removed, "removableNodes": removed,
        "removableIfUnblocked": removed + len(k["held"]), "floorBlocked": 0, "minSize": None,
        "monthlyIfRemoved": saving, "monthlyIfFloorLowered": saving, "monthlyIfNothingBlocked": saving,
        "reasons": reasons, "nonDefaultArgs": {},
        "vetoes": k["held"], "notEvacuable": [], "checked": [], "unchecked": [],
        "simulated": True, "replayed": True,
        "fleetToday": asis.get("fleet") or [], "fleetAfter": fleet_after,
        "pressureAfterScaleDown": [], "pressureIfFloorLowered": [], "unreservedLoad": None,
    }


def _ca_estimate(snap, g, cfg, ca, price, itype, rules) -> dict | None:
    """What Cluster Autoscaler would do to this group on its own.

    Distinct from every scenario: those describe what *could* fit, this describes
    what the controller will actually remove without anyone changing a request.
    """
    if ca is None or g.nodes_current == 0:
        return None
    est = estimate_scale_down(g, ca, cfg.get("min_size"))
    unit = price(itype) if itype else 0.0

    # `estimate_scale_down` counts under-utilised nodes and subtracts the ASG
    # floor. It never looks at which pods are on which node, so it reports nodes
    # CA will refuse to touch: two on this fleet, one held by an emptyDir pod
    # and one by a kube-system pod with no PDB. The per-node replay does look,
    # so it is what gets reported; the estimate stays as the upper bound it is.
    run, checked = simulate_group(snap, g, ca, rules, cfg.get("min_size") or 0)
    # "If the floor were lowered" has to be the same replay with the floor taken
    # off, not the raw count of under-utilised nodes. pipeline-1 has no floor
    # holding it at all -- a pod CA cannot evict does -- yet the old figure
    # offered $305/mo for lowering a minSize that is not the constraint.
    unfloored, _ = simulate_group(snap, g, ca, rules, 0)
    removable = run.total if run is not None else est.removable
    if_floor_lowered = unfloored.total if unfloored is not None else est.unneeded
    floor_blocked = max(0, if_floor_lowered - removable)
    # Vetoes come from the *unfloored* replay. With the floor in place the loop
    # stops at `len(remaining) <= min_size` before it evaluates a single
    # candidate, so a group held at its minimum recorded zero vetoes while seven
    # of its nodes were in fact blocked -- and the column read "—" for exactly
    # the groups where the blockers matter most.
    source = unfloored if unfloored is not None else run
    vetoes = (
        [{"node": n, "reason": r} for n, r in sorted(source.vetoed.items())]
        if source is not None else []
    )

    # CA removes by requests, so the nodes it keeps can end up carrying more
    # real load than they hold -- load from pods with no request at all. Checked
    # against measured use on the placement each replay leaves behind.
    loads = pressure.workload_loads(snap)

    def after(r) -> list[dict]:
        if r is None:
            return []
        return [
            {"node": p.node, "severity": p.severity,
             "memBusyGiB": round(p.mem_busy / 1024**3, 2),
             "memAllocGiB": round(p.mem_alloc / 1024**3, 2),
             "cpuBusy": round(p.cpu_busy, 2), "cpuAlloc": round(p.cpu_alloc, 2),
             "bestEffortPods": [e.pod for e in p.besteffort][:10]}
            for p in pressure.flagged(pressure.assess(
                r.placement, cpu_alloc=g.alloc_cpu_per_node,
                mem_alloc=g.alloc_mem_per_node, loads=loads))
        ]

    hidden = pressure.unreserved(snap.placements.get(g.name) or [], loads)
    return {
        "source": ca.source,
        "threshold": est.threshold,
        "unneededNodes": est.unneeded,
        "removableNodes": removable,
        "removableIfUnblocked": est.removable,
        "floorBlocked": floor_blocked,
        "minSize": cfg.get("min_size"),
        "monthlyIfRemoved": round(unit * removable, 2),
        "monthlyIfFloorLowered": round(unit * if_floor_lowered, 2),
        # What a count of under-utilised nodes alone would have claimed. Kept so
        # the gap between "looks empty" and "CA will actually remove it" is
        # visible rather than silently absorbed.
        "monthlyIfNothingBlocked": round(unit * est.unneeded, 2),
        "reasons": est.reasons,
        "nonDefaultArgs": ca.non_default(),
        "vetoes": vetoes,
        "notEvacuable": list(run.not_evacuable) if run is not None else [],
        "checked": list(checked),
        "unchecked": list(rules.unknown),
        "simulated": run is not None,
        # Nodes whose measured load would not fit once CA acts; empty is good.
        "pressureAfterScaleDown": after(run),
        "pressureIfFloorLowered": after(unfloored),
        "unreservedLoad": {
            "bestEffortPods": hidden.besteffort_pods,
            "bestEffortMemGiB": round(hidden.besteffort_mem / 1024**3, 2),
            "bestEffortCpu": round(hidden.besteffort_cpu, 2),
            "overRequestPods": hidden.over_request_pods,
            "overRequestMemGiB": round(hidden.over_request_mem / 1024**3, 2),
        },
    }


def _decision(snap, g, itype, recs, price, profile, region, cfg, ca, rules) -> dict:
    """The recommendation, plus only the shapes worth showing beside it.

    The search considers dozens of combinations; publishing all of them makes the
    tool look like it is offering a catalogue rather than an answer. What ships
    is the baseline, the recommendation, and the handful of alternatives the
    advisor judged to be real trade-offs -- everything else did its job during
    the search and has no reader.
    """
    everything = _scenarios(snap, g, itype, recs, price, profile, region, cfg)
    _annotate_drain(snap, g, everything, ca, rules, cfg)
    for sc in everything:
        req = sc.get("requires") or []
        if not any("minReplicas" in r for r in req):
            continue
        sc["replicaChanges"] = _replica_changes(
            snap, g.name,
            recs if any("container request changes" in r for r in req) else None,
        )
    # What each option changes, counted: the table shows it, and the advisor
    # breaks ties with it -- 8 HPA floors is less to ship than 25 requests.
    for sc in everything:
        sc["changes"] = _changes(g.name, sc, recs)
        sc["family"] = family_of(SimpleNamespace(requires=sc.get("requires") or []))
    advice = _advice(snap, g, everything, recs, cfg)
    if not advice:
        return {"scenarios": everything[:1], "advice": None, "optionsConsidered": len(everything)}

    keep = {"asis", advice.get("scenarioKey")}
    keep |= {alt["key"] for alt in advice.get("alternatives", [])}
    # An option held back only by pods CA will not evict ships when the advisor
    # offers it -- whenever it saves more than the pick -- with its blockers
    # attached rather than presented as ready to run.
    shown = [s for s in everything if s["key"] in keep]
    return {
        "scenarios": shown,
        "advice": advice,
        # How wide the search was, so the number is auditable without shipping it.
        "optionsConsidered": len(everything) - 1,
    }


def _annotate_drain(snap, g, scenarios: list[dict], ca, rules, cfg) -> None:
    """Name what blocks an option priced "once unblocked", and how to fix it.

    The engine replays Cluster Autoscaler, so an ordinary option already stops
    where CA stops. An option carrying ``blocked`` was replayed again with the
    fewest blockers fixed that reach its node count: ``nodes`` are the nodes
    CA must be able to empty, with *every* pod pinning each one; ``keep`` are
    blocked nodes that stay and need nothing. One entry per blocking workload,
    with the fix, rather than a prose list -- "replace the node group" should
    be a choice made against this, not the only option left standing because
    the cheaper one was filtered out.
    """
    zone_of = {n: (labels or {}).get("topology.kubernetes.io/zone")
               for n, labels in (rules.node_labels or {}).items()}
    for sc in scenarios:
        held = (sc.get("blocked") or {}).get("nodes") or {}
        if not held:
            continue
        keep = (sc.get("blocked") or {}).get("keep") or {}
        blockers: dict[str, dict] = {}
        for node, reasons in held.items():
            # Snapshots from before every blocker was recorded carry one reason.
            pairs = [(reasons.split(" ")[0], reasons)] if isinstance(reasons, str) else reasons
            for subject, reason in pairs:
                entry = blockers.setdefault(subject, {
                    "subject": subject,
                    "reason": reason,
                    "nodes": [],
                    "fix": _drain_fix(reason),
                    "replicas": rules.replicas.get(subject, 0),
                })
                if "no PDB" in reason and "pdb" not in entry:
                    entry["pdb"] = _pdb_suggestion(
                        subject, rules.replicas.get(subject, 0), rules.labels.get(subject, {})
                    )
                if node not in entry["nodes"]:
                    entry["nodes"].append(node)
        # A StatefulSet's EBS volume lives in its node's zone; its pod can only
        # restart where a node that stays shares that zone. The replay places
        # by request and labels, not by volume, so say it where it matters.
        staying = (sc.get("blocked") or {}).get("stays") or list(keep)
        zones_left = {zone_of.get(n) for n in staying} - {None}
        for entry in blockers.values():
            if not entry["subject"].endswith("/StatefulSet") or not zones_left:
                continue
            away = sorted({zone_of.get(n) for n in entry["nodes"]} - zones_left - {None})
            if away:
                entry["zoneWarning"] = (
                    f"its volume is in {', '.join(away)} and no node that stays is: with an EBS "
                    "PVC the pod would stay Pending -- keep a node in that zone, or move the volume")
        detail = sorted(blockers.values(), key=lambda b: (-len(b["nodes"]), b["subject"]))
        names = ", ".join(b["subject"] for b in detail[:3]) + (
            f" and {len(detail) - 3} more" if len(detail) > 3 else "")
        sc["requires"] = [
            (f"unblock {len(held)} node(s) by fixing {len(detail)} workload(s) ({names}) -- "
             f"Cluster Autoscaler will not evict them; without the fix it stops at "
             f"{sc['blocked'].get('nodesIfNot')} nodes")
            if r.startswith("unblock ") else r
            for r in sc.get("requires") or []
        ]
        sc["drainBlocked"] = len(held)
        sc["drainBlockers"] = detail
        # Blocked too, but staying: CA fills them rather than empties them.
        sc["drainKept"] = [
            {"node": n, "subjects": sorted({s for s, _ in (rs if not isinstance(rs, str) else [(rs.split(" ")[0], rs)])})}
            for n, rs in sorted(keep.items())
        ]
        # The one-off alternative to changing any workload: drain the nodes by
        # hand in a window, and let CA remove them once empty. kubectl honours
        # PDBs just as CA does.
        flags = "--ignore-daemonsets --delete-emptydir-data"
        if any("no controller" in b["reason"] for b in detail):
            flags += " --force"
        sc["drainByHand"] = [f"kubectl drain {n} {flags}" for n in sorted(held)]

# Labels that change on a chart upgrade or a rollout. A PDB selector built from
# them stops matching the moment the workload is redeployed, which is worse than
# no PDB -- it silently governs nothing.
_VOLATILE_LABELS = frozenset({
    "helm.sh/chart", "app.kubernetes.io/version", "app.kubernetes.io/managed-by",
    "pod-template-hash", "controller-revision-hash", "pod-template-generation",
})


def _pdb_selector(labels: dict) -> dict:
    """The smallest stable selector that identifies this workload."""
    for keys in (
        ("app.kubernetes.io/name", "app.kubernetes.io/instance"),
        ("app.kubernetes.io/name",),
        ("app",),
        ("k8s-app",),
    ):
        if all(k in labels for k in keys):
            return {k: labels[k] for k in keys}
    return {k: v for k, v in labels.items() if k not in _VOLATILE_LABELS}


def _pdb_suggestion(subject: str, replicas: int, labels: dict) -> dict | None:
    """A PDB that would let CA evict this workload, or why one will not do.

    A single-replica workload is the case that needs saying out loud. CA skips a
    kube-system node only when the pod has *no* PDB, so adding one is what makes
    it willing -- but the budget then governs the eviction, and with one replica
    there is no budget that both permits the eviction and keeps the workload up.
    ``minAvailable: 1`` permits nothing and leaves CA exactly as blocked;
    ``maxUnavailable: 1`` permits it at the cost of a gap. Scaling to two first
    is the only answer that is both unblocking and safe.
    """
    ns, _, rest = subject.partition("/")
    name, _, kind = rest.rpartition("/")
    name = name or rest
    if kind in ("Job", "CronJob"):
        # A PDB on a batch pod is the wrong instrument: the pod is going to
        # finish and disappear on its own, and a budget would only stop CA
        # while it runs.
        return {
            "replicas": replicas, "safe": False, "yaml": None,
            "note": f"this is a {kind} pod, not a service -- a PDB is the wrong tool. "
                    "It blocks the node only while the job runs; either wait for it, "
                    "or annotate the pod template "
                    "cluster-autoscaler.kubernetes.io/safe-to-evict=true if the work "
                    "is safe to restart.",
        }
    selector = _pdb_selector(labels or {})
    if not selector:
        return {"replicas": replicas, "safe": False, "yaml": None,
                "note": "no pod-template labels recorded, so a selector cannot be "
                        "suggested -- re-run collect with --context"}
    sel = "\n".join(f"      {k}: {v}" for k, v in sorted(selector.items()))
    yaml = (
        "apiVersion: policy/v1\n"
        "kind: PodDisruptionBudget\n"
        "metadata:\n"
        f"  name: {name}-pdb\n"
        f"  namespace: {ns}\n"
        "spec:\n"
        "  maxUnavailable: 1\n"
        "  selector:\n"
        "    matchLabels:\n"
        f"{sel}"
    )
    if replicas >= 2:
        return {
            "replicas": replicas, "safe": True, "yaml": yaml,
            "note": f"{replicas} replicas, so maxUnavailable: 1 lets Cluster "
                    f"Autoscaler take one at a time and keeps {replicas - 1} serving.",
        }
    return {
        "replicas": replicas, "safe": False, "yaml": yaml,
        "note": "only 1 replica: no budget both permits the eviction and keeps this "
                "up. minAvailable: 1 would leave CA just as blocked, and "
                "maxUnavailable: 1 unblocks it at the cost of a gap while the pod "
                "reschedules. Scale to 2 replicas first if that gap matters.",
    }


def _drain_fix(reason: str) -> str:
    """The specific change that would let CA drain this node."""
    if "no controller" in reason:
        return "give the pod a controller, or delete it"
    if "custom controller" in reason:
        return ("annotate its pods cluster-autoscaler.kubernetes.io/safe-to-evict=true, "
                "or run CA with --skip-nodes-with-custom-controller-pods=false")
    if "no PDB" in reason:
        if "age unknown" in reason:
            return ("add a PodDisruptionBudget so CA may evict it (CA also lets it go once "
                    "the pod is older than --blocking-system-pod-distruption-timeout; its age "
                    "was not collected -- run `rightsizer pod-ages`)")
        return ("add a PodDisruptionBudget so CA may evict it, or wait: CA lets it go once "
                "the pod is older than --blocking-system-pod-distruption-timeout (1h)")
    if "scale-down-disabled" in reason:
        return "remove the node's cluster-autoscaler.kubernetes.io/scale-down-disabled annotation"
    if "safe-to-evict=false" in reason:
        return "set cluster-autoscaler.kubernetes.io/safe-to-evict=true"
    if "emptyDir" in reason or "local storage" in reason:
        return ("annotate cluster-autoscaler.kubernetes.io/safe-to-evict=true "
                "if the scratch data is disposable")
    if "PDB" in reason and "allows" in reason:
        return ("raise maxUnavailable (or lower minAvailable) so the budget allows at least "
                "one disruption")
    return "resolve the constraint above"


def _changes(group: str, sc: dict, recs) -> dict:
    """How many individual changes an option makes, by kind."""
    req = sc.get("requires") or []
    requests = any("container request changes" in r for r in req)
    memory_only = requests and sc.get("key", "").startswith("memory")
    n_req = 0
    if requests:
        for r in recs:
            if r.nodegroup != group or not r.actionable:
                continue
            if memory_only:
                n_req += r.mem_recommended is not None and r.mem_recommended > r.mem_request
            else:
                n_req += r.cpu_recommended is not None or r.mem_recommended is not None
    hpas = len(sc.get("replicaChanges") or []) if any("minReplicas" in r for r in req) else 0
    return {"requests": int(n_req), "hpas": hpas,
            "minSize": any("minSize" in r for r in req),
            "newType": any("replace the node group" in r for r in req)}


def _edit_count(changes: dict) -> int:
    return (changes.get("requests", 0) + changes.get("hpas", 0)
            + int(bool(changes.get("minSize"))) + int(bool(changes.get("newType"))))


def _advice(snap, g, scenario_dicts, recs, cfg) -> dict | None:
    """The recommendation for one node group, derived from its scenarios."""
    from dataclasses import asdict as _asdict
    from rightsizer.engine.simulate import Scenario

    if not scenario_dicts:
        return None
    # Rebuild typed scenarios so the advisor works on objects, not dicts.
    rows = [
        Scenario(
            key=d["key"], label=d["label"], instance_type=d["instanceType"],
            nodes=d["nodes"], monthly_usd=d["monthlyUsd"], delta_usd=d["deltaUsd"],
            cpu_utilization=d["cpuUtil"], mem_utilization=d["memUtil"],
            binding=d["binding"], feasible=d["feasible"], note=d["note"],
            requires=d["requires"], direction=d.get("direction", "neutral"),
            rationale=d.get("rationale", ""), vcpu=d.get("vcpu", 0),
            mem_gib=d.get("memGib", 0.0), storage=d.get("storage") or {},
            network=d.get("network") or {},
            load=d.get("load") or {}, blocked=d.get("blocked") or {},
            margin=d.get("margin") or {},
        )
        for d in scenario_dicts
    ]
    edits = {d["key"]: _edit_count(d.get("changes") or {}) for d in scenario_dicts}
    a = advise(snap, g, rows, recs, min_size=cfg.get("min_size"), edits=edits)
    # camelCase to match the rest of the payload; dataclasses give snake_case.
    return {
        "group": a.group,
        "verdict": a.verdict,
        "headline": a.headline,
        "reason": a.reason,
        "scenarioKey": a.scenario_key,
        "monthlyDelta": round(a.monthly_delta, 2),
        "steps": a.steps,
        "alternatives": a.alternatives,
        "problems": [
            {"kind": p.kind, "subject": p.subject, "detail": p.detail, "severity": p.severity}
            for p in a.problems
        ],
        "findingCounts": a.finding_counts,
        "findingsTotal": sum(a.finding_counts.values()),
        "findingsSummary": a.summarise_findings(),
        "diagnostics": a.diagnostics,
    }


def _running_now(snap) -> dict:
    """Pods per workload across every node group, from observed placement."""
    cached = getattr(snap, "_running_now_cache", None)
    if cached is not None:
        return cached
    counts: dict[tuple, int] = {}
    for rows in (snap.placements or {}).values():
        for row in rows:
            if row.get("owner_kind") == "DaemonSet":
                continue
            key = row.get("workload_key", "")
            ns, _, rest = key.partition("/")
            name, _, kind = rest.rpartition("/")
            counts[(ns, name, kind)] = counts.get((ns, name, kind), 0) + 1
    snap._running_now_cache = counts
    return counts


def _avg_replicas(idx: dict, r) -> float:
    """Replicas actually running, averaged over the window.

    `total / mean-across-replicas` recovers the count that was live at each
    instant, which is the only honest average when a floor moved mid-window.
    """
    stat = idx.get((r.namespace, r.workload, r.kind, r.container, "cpu"))
    if stat is None or not stat.avg:
        return 0.0
    return round(stat.total_avg / stat.avg, 1)


def _usage_index(snap) -> dict:
    """(namespace, workload, kind, container, resource) -> the observed series.

    A recommendation is only as persuasive as the usage behind it, so every row
    that proposes a change carries the numbers that produced it.
    """
    return {
        (s.namespace, s.workload, s.kind, s.container, s.resource): s
        for s in snap.usage
    }


def _usage_fields(idx: dict, r, resource: str, scale: float = 1.0) -> dict:
    """Per-replica avg / p95 / peak, plus peak as a share of the request.

    ``resource`` is the snapshot's own spelling -- ``"cpu"`` or ``"mem"``. Asking
    for anything else used to return ``{}``, so a mis-spelled key shipped a
    console where every container read "peak 0.00 GiB" and nothing failed.
    """
    if resource not in ("cpu", "mem"):
        raise ValueError(f"unknown resource {resource!r}; snapshots use 'cpu' or 'mem'")
    stat = idx.get((r.namespace, r.workload, r.kind, r.container, resource))
    if stat is None:
        return {}
    prefix = resource
    request = r.cpu_request if resource == "cpu" else r.mem_request
    return {
        f"{prefix}Avg": round(stat.avg / scale, 4),
        f"{prefix}P95": round(stat.p95 / scale, 4),
        f"{prefix}Peak": round(stat.peak / scale, 4),
        # Peak against the current request: over 100% means the container has
        # been using more than it reserved.
        f"{prefix}UtilPct": round(stat.peak / request * 100, 1) if request > 0 else None,
        f"{prefix}Samples": stat.samples,
        f"{prefix}OomKilled": stat.oom_killed if resource == "mem" else None,
        # The distribution the request is sized against, so "how often would
        # this container be over the proposed request" is answerable rather
        # than inferred from a single percentile.
        f"{prefix}Ladder": {
            k: round(v / scale, 4) for k, v in (stat.ladder or {}).items() if k != "n"
        },
        # A container with no limit is never throttled by its request; one with
        # a limit is capped at it. That changes what a tight request costs, and
        # it was invisible.
        f"{prefix}Limit": round(stat.limit / scale, 4) if stat.limit else None,
    }


def _direction(cpu_delta: float, mem_delta: float) -> str:
    """What this change does to reserved capacity.

    Signed and explicit, because the two directions are not the same kind of
    finding: freeing capacity is a saving, needing more is a stability fix that
    may cost money. A table that shows both without saying which is which makes
    the reader do the arithmetic.
    """
    frees = cpu_delta < -1e-9 or mem_delta < -1e-9
    needs = cpu_delta > 1e-9 or mem_delta > 1e-9
    if frees and needs:
        return "mixed"
    if frees:
        return "frees"
    if needs:
        return "needs"
    return "none"


def _replica_changes(snap, group: str, recommendations: list | None = None) -> list[dict]:
    """The specific HPAs to change, for the replica-floor option.

    A scenario that says "reduce minReplicas" and stops is not an instruction.
    This names each HPA, what its floor is, what to set it to, and the capacity
    that frees -- scoped to workloads actually running on this node group.

    ``recommendations`` is passed for an option that *also* cuts requests, so
    the floors shown are the ones that option produces. Without it a combined
    option would list admin-server at 5 when its own request cut puts the
    equilibrium at 18 -- the table contradicting the option it belongs to.
    """
    floors = {f.key: f for f in replica_floors(snap, recommendations)}
    if not floors:
        return []
    import collections

    placed = [
        row for row in (snap.placements or {}).get(group, [])
        if row.get("owner_kind") != "DaemonSet"
    ]
    here = {row.get("workload_key") for row in placed}
    # Pods of this workload on this node group, right now. `stat.replicas` is a
    # 30-day maximum and counts both sides of a rolling update, so
    # api-server reads 104 there against 75 actually running -- a
    # number that belongs under "peak", never under "running".
    running_now = collections.Counter(row.get("workload_key") for row in placed)
    hpas = {(h.namespace, h.target_name): h for h in snap.hpas if h.target_name}
    usage = {(s.namespace, s.workload): s for s in snap.usage if s.resource == "cpu"}

    out = []
    for key, plan in floors.items():
        suggested = plan.suggested
        if key not in here:
            continue
        namespace, workload, _kind = key.split("/", 2)
        hpa = hpas.get((namespace, workload))
        stat = usage.get((namespace, workload))
        if not hpa or not stat:
            continue
        # Pod-level when the minute replay ran: the HPA divides pod usage by
        # pod request, and the floor was sized against that.
        req_now = plan.request_now if plan.method == "replay-1m" else stat.request
        out.append({
            "ns": namespace,
            "workload": workload,
            # Names the trend file the projection chart replays over.
            "container": stat.container,
            "hpa": hpa.name,
            "minReplicas": hpa.min_replicas,
            "suggestedMin": suggested,
            "maxReplicas": hpa.max_replicas,
            "runningNow": running_now.get(key, 0),
            "avgReplicas": plan.avg_replicas,
            "mixedWindow": plan.mixed_window,
            "peakReplicas": stat.replicas,
            "observedReplicas": stat.replicas,  # kept for older bundles
            "cpuTarget": hpa.cpu_target,
            "requestPerReplica": round(req_now, 4),
            # The request the suggested floor was sized against. Differs from
            # the one above for an option that also cuts requests, and the
            # projection must use it or it contradicts its own floor.
            "requestAfter": round(plan.request or stat.request, 4),
            "p95PerReplica": round(stat.p95, 4),
            "utilPct": round(stat.p95 / stat.request * 100, 1) if stat.request else None,
            # What each replica was actually working at, against the target the
            # HPA steers on -- the reason the floor is not doing anything.
            "utilAvgPct": round(plan.util_avg * 100, 1),
            "utilMaxPct": round(plan.util_max * 100, 1),
            "targetPct": round((plan.target or 0) * 100),
            "neededAvg": plan.desired_avg,
            "freedCores": round((hpa.min_replicas - suggested) * req_now, 2),
            # How the floor was reached, and the replay behind it when there was
            # one: exposure today and at the new floor, and how closely the
            # replay of today tracked the real HPA.
            "method": plan.method,
            "replayNow": plan.replay_now,
            "replayAfter": plan.replay_after,
            "validation": plan.validation,
            "behaviorSource": plan.behavior_source,
            "lagMinutes": plan.lag_minutes,
            # What the 30-day replay actually saw, so the floor is a conclusion
            # rather than an assertion.
            "replayFloor": plan.floor_at_coverage,
            # The busiest sample wanted several times this floor, so the HPA has
            # a long way to travel. Reported so the change can be staged by
            # choice rather than by a cap nobody asked for.
            "longScaleUp": plan.long_scale_up,
            "peakDesired": plan.peak_desired,
            "desired": plan.desired,
            "basis": plan.basis,
        })
    return sorted(out, key=lambda r: -r["freedCores"])


def _drivers(recs, group: str, idx: dict) -> list[dict]:
    """The specific workload changes that produce a node group's scenario.

    A scenario says "38 nodes instead of 58". This says which requests have to
    change for that to be true, and by how much -- without it the number is not
    actionable, only impressive. Sorted by the capacity each one moves.
    """
    rows = []
    for r in recs:
        if r.nodegroup != group or not r.actionable:
            continue
        n = max(r.replicas, 1)
        cpu_d = ((r.cpu_recommended - r.cpu_request) * n) if r.cpu_recommended is not None else 0.0
        mem_d = ((r.mem_recommended - r.mem_request) * n / 1024**3) if r.mem_recommended is not None else 0.0
        if abs(cpu_d) < 1e-9 and abs(mem_d) < 1e-9:
            continue
        row = {
            "ns": r.namespace, "workload": r.workload, "container": r.container,
            "replicas": r.replicas,
            "cpuReq": round(r.cpu_request, 4),
            "cpuRec": round(r.cpu_recommended, 4) if r.cpu_recommended is not None else None,
            "memReq": round(r.mem_request / 1024**3, 3),
            "memRec": round(r.mem_recommended / 1024**3, 3) if r.mem_recommended is not None else None,
            "cpuDelta": round(cpu_d, 3),
            "memDelta": round(mem_d, 3),
            "direction": _direction(cpu_d, mem_d),
            "basis": r.basis,
        }
        row.update(_usage_fields(idx, r, "cpu"))
        row.update(_usage_fields(idx, r, "mem", 1024**3))
        rows.append(row)
    return sorted(rows, key=lambda x: (x["cpuDelta"], x["memDelta"]))


#: Instance types the console's picker offers beyond each group's ladder.
EXTRA_TYPES: list[str] = []


def _cloud_name(snap) -> str:
    return clouds.for_snapshot(snap).name


@functools.lru_cache(maxsize=None)
def _priced_ladder(cloud: str, itype: str, profile, region, listed: tuple = ()) -> tuple:
    """The candidate instance types for a group, with their on-demand price.

    family_ladder returns specs only; the price is attached so an option can
    show the rate of the node it proposes. Cached: the scenario search and the
    console's instance-type picker must offer the same list.
    """
    provider = clouds.get(cloud, region=region, profile=profile)
    config = {"instance_types": [itype, *listed]}
    return tuple(
        dataclasses.replace(i, on_demand_hourly=provider.on_demand_price(i.name))
        for i in clouds.ladder_for(SimpleNamespace(meta={"cloud": cloud}, facts=SimpleNamespace(node_facts=[])),
                                   config, itype, region=region, profile=profile)
    )


def _instance_choices(snap, g, itype, price, profile, region, discount: float, cfg=None) -> dict | None:
    """What the console needs to price any candidate type in the browser.

    Specs and price per type, plus the measured disk and network the storage
    and bandwidth checks read -- the same inputs :func:`rightsizer.sources.storage.assess`
    and :func:`rightsizer.sources.network.assess` take, so the picker and the options
    judge a type the same way.
    """
    from rightsizer.sources.network import bandwidth

    if not itype or "." not in itype or g.nodes_current == 0:
        return None
    listed = tuple((cfg or {}).get("instance_types") or [])
    ladder = list(_priced_ladder(_cloud_name(snap), itype, profile, region, listed))
    names = {i.name for i in ladder}
    # Beyond the ladder: every type the provider has already described and
    # priced for this region locally (no API call), and any the config or
    # --types asks for (described and priced now, with the run's credentials).
    cloud = clouds.for_snapshot(snap, region=region, profile=profile)
    cached = [n for n in cloud.known_types() if n not in names]
    wanted = [n for n in EXTRA_TYPES if n not in names and n not in cached]
    extra = [dataclasses.replace(i, on_demand_hourly=cloud.on_demand_price(i.name))
             for batch in (wanted, cached) if batch
             for i in cloud.describe_instance_types(batch).values()]
    types = []
    for i in [*ladder, *extra]:
        if not i.on_demand_hourly and i.name not in names:
            continue  # no price for this region: the console asks for one instead
        bw = bandwidth(i)
        types.append({
            "name": i.name, "vcpu": i.vcpu, "memGib": i.memory_gib,
            # A size the group may already run (a Karpenter pool's listed rung).
            "offered": i.name in listed,
            # Searched by the advice, or only available to compare against.
            "ladder": i.name in names,
            "hourlyUsd": round(i.on_demand_hourly, 4),
            "monthlyPerNode": round(price(i.name), 4),
            "storage": {"gb": i.storage_gb, "disks": i.storage_disks, "type": i.storage_type,
                        "nvme": i.storage_nvme, "known": i.storage_known},
            "net": {"baseline": bw[0], "burst": bw[1], "source": bw[2]} if bw else None,
        })
    return {
        "types": types,
        # For a type the reader names that is not in `types`: its price is
        # entered by hand, and turned monthly the way `price` does.
        "discount": discount, "hoursPerMonth": HOURS_PER_MONTH,
        "disk": (getattr(snap, "storage", None) or {}).get(g.name),
        "net": (getattr(snap, "network", None) or {}).get(g.name),
    }


def _scenarios(snap, g, itype, recs, price, profile, region, cfg=None) -> list[dict]:
    """The four-way decomposition for one node group, as plain dicts for the UI."""
    if not itype or "." not in itype or g.nodes_current == 0:
        return []
    ladder = _priced_ladder(_cloud_name(snap), itype, profile, region,
                            tuple((cfg or {}).get("instance_types") or []))
    rows = scenarios(snap, g, ladder, recs, current_type=itype, price_of=price,
                     max_pods=snap.facts.max_pods_for(g.name),
                     headroom=Tunables(**snap.tunables).pack_headroom,
                     min_size=cfg.get("min_size"))
    return [{
        "key": s.key, "label": s.label, "instanceType": s.instance_type, "nodes": s.nodes,
        "monthlyUsd": round(s.monthly_usd, 2), "deltaUsd": round(s.delta_usd, 2),
        "cpuUtil": round(s.cpu_utilization, 4), "memUtil": round(s.mem_utilization, 4),
        "binding": s.binding, "feasible": s.feasible, "note": s.note, "requires": s.requires,
        "retype": s.retype, "retypeSteps": s.retype_steps, "fleet": s.fleet, "karpenter": s.karpenter,
        "headroom": s.headroom, "unverified": s.unverified,
        "direction": s.direction, "rationale": s.rationale, "usageDrivenPods": s.usage_driven_pods,
        "measured": s.measured,
        "nodeCpu": s.node_cpu, "nodeMemGib": s.node_mem_gib, "nodeMaxPods": s.node_max_pods,
        "vcpu": s.vcpu, "memGib": s.mem_gib, "hourlyUsd": round(s.hourly_usd, 4),
        "placement": s.placement,
        "minNodes": s.min_nodes,
        "storage": s.storage,
        "network": s.network,
        "blocked": s.blocked,
        "margin": s.margin,
        "load": s.load,
        "model": s.model,
    } for s in rows]


# Collected windows, newest first. `end` is None for "now".
def _model(snap, g, cfg, recs) -> dict | None:
    """Everything :mod:`rightsizer.engine.realistic` reads, for the browser's port.

    The dialog re-prices a reader's selection in the browser, and it must land
    where the engine does -- which is Cluster Autoscaler replayed on today's
    placement for the same node type, and a by-request packing at 100% for a
    new one, followed by the measured-load check. So the inputs of exactly that
    ship: every pod with its node (DaemonSets included -- they hold space and
    are not evacuated), the CA flags, the eviction and scheduling rules for this
    group's workloads and nodes, measured loads, today's load baseline and the
    month's node trend. Same CA config and rules the engine's ``scenarios`` uses
    by default, so the two cannot be configured differently.
    """
    from rightsizer.engine import realistic as R

    rows = (snap.placements or {}).get(g.name) or []
    if not rows:
        return None
    ca = autoscalers.scale_down_settings(snap)
    rules = rules_from_snapshot(snap)
    loads = pressure.workload_loads(snap)

    keys: list[str] = []
    key_index: dict[str, int] = {}

    def k(wkey: str) -> int:
        if wkey not in key_index:
            key_index[wkey] = len(keys)
            keys.append(wkey)
        return key_index[wkey]

    nodes = sorted({r["node"] for r in rows if r.get("node")})
    node_index = {n: i for i, n in enumerate(nodes)}
    placed = [
        {"p": r.get("pod", ""), "ns": r.get("namespace", ""),
         "n": node_index.get(r.get("node", ""), -1), "k": k(r.get("workload_key", "")),
         "c": float(r.get("cpu", 0.0)), "m": float(r.get("mem", 0.0)),
         "o": r.get("owner_kind", ""), "a": r.get("created")}
        for r in rows
    ]

    # DaemonSet load per node and today's load, computed as `scenarios` does.
    ds_nodes: dict[str, list[float]] = {}
    today = R.Outcome(nodes=g.nodes_current, mode="measured", placement={})
    replicas_now: dict[str, int] = {}
    for r in rows:
        if r.get("owner_kind") != "DaemonSet":
            replicas_now[r.get("workload_key", "")] = replicas_now.get(r.get("workload_key", ""), 0) + 1
        if not r.get("node"):
            continue
        today.placement.setdefault(r["node"], []).append((
            r.get("workload_key", ""), float(r.get("cpu", 0.0)), float(r.get("mem", 0.0)),
            r.get("owner_kind") == "DaemonSet"))
        if r.get("owner_kind") == "DaemonSet":
            load = loads.get(r.get("workload_key", ""))
            acc = ds_nodes.setdefault(r["node"], [0.0, 0.0])
            acc[0] += load.cpu_busy if load else float(r.get("cpu", 0.0))
            acc[1] += load.mem_busy if load else float(r.get("mem", 0.0))
    ds_busy = ((sum(v[0] for v in ds_nodes.values()) / len(ds_nodes),
                sum(v[1] for v in ds_nodes.values()) / len(ds_nodes)) if ds_nodes else (0.0, 0.0))
    today_load = R.load_check(today, alloc_cpu=g.alloc_cpu_per_node, alloc_mem=g.alloc_mem_per_node,
                              loads=loads, replicas_now=replicas_now, replicas_kept={},
                              ds_busy=ds_busy)

    mine = set(keys)
    unschedulable = sorted(set(g.unschedulable_nodes) | {
        n.get("name", "") for n in (snap.facts.node_facts or [])
        if n.get("unschedulable") and n.get("name") in nodes})
    pick = lambda d: {w: v for w, v in d.items() if w in mine}  # noqa: E731
    return {
        "nodes": nodes,
        "keys": keys,
        "placed": placed,
        "allocCpu": g.alloc_cpu_per_node, "allocMem": g.alloc_mem_per_node,
        "dsCpu": g.ds_cpu_per_node, "dsMem": g.ds_mem_per_node,
        # kubelet's max-pods on this group's nodes (0 = unknown): the replay
        # places no pod past it, DaemonSets counted.
        "maxPods": snap.facts.max_pods_for(g.name),
        # DaemonSet pods per node: a new node runs them too, so a new group
        # packs max-pods minus these.
        "dsPods": round(g.daemonset_pods / g.nodes_current) if g.nodes_current else 0,
        "start": len(nodes),
        # The floor the engine replays the as-is shape against.
        "minSize": int(cfg.get("min_size") or 0),
        "ca": {
            "source": ca.source, "enabled": ca.enabled, "threshold": ca.utilization_threshold,
            "ignoresDaemonsets": ca.ignores_daemonsets, "skipsSystemPods": ca.skips_system_pods,
            "skipsLocalStorage": ca.skips_local_storage,
            "bulk": int(ca.get("max-empty-bulk-delete") or 10),
            # v1.35.0 flags the replay follows.
            "ignoresMirror": ca.ignores_mirror_pods,
            "customControllers": ca.skips_custom_controllers,
            "systemTimeout": ca.system_pod_timeout,
            "parallelism": ca.parallelism,
            "drainParallelism": ca.drain_parallelism,
        },
        # When the placement was observed: a kube-system pod's age is measured to it.
        "now": _observed_at(snap),
        "rules": {
            "drainBlocked": pick(rules.drain_blocked),
            "pdb": {w: {"ns": p.namespace, "name": p.name, "minAvailable": p.min_available,
                        "maxUnavailable": p.max_unavailable}
                    for w, p in pick(rules.pdb_for).items()},
            "replicas": pick(rules.replicas),
            "nodeSelector": pick(rules.node_selector),
            "nodeAffinity": pick(rules.node_affinity),
            "tolerations": pick(rules.tolerations),
            "antiaffinity": sorted(w for w in rules.antiaffinity if w in mine),
            "nodeLabels": {n: rules.node_labels[n] for n in nodes if n in rules.node_labels},
            "nodeTaints": {n: rules.node_taints[n] for n in nodes if n in rules.node_taints},
            "unschedulable": unschedulable,
            "zoneBound": rules.zone_bound,
            "safeToEvict": sorted(w for w in rules.safe_to_evict if w in mine),
            "notSafe": sorted(w for w in rules.not_safe_to_evict if w in mine),
            "localStorage": sorted(w for w in rules.local_storage if w in mine),
            "hardSpread": pick(rules.hard_spread),
            "scaleDownDisabled": sorted(n for n in rules.scale_down_disabled if n in nodes),
            # status.disruptionsAllowed per budget, "ns/name" -> n (None: unreadable).
            "pdbAllowed": {f"{p.namespace}/{p.name}": rules.pdb_allowed(p)
                           for p in {id(p): p for p in pick(rules.pdb_for).values()}.values()},
        },
        "loads": {w: [loads[w].cpu_busy, loads[w].mem_busy] for w in keys if w in loads},
        "dsBusy": list(ds_busy),
        "today": {"hotCpu": today_load.get("hotCpu", 0.0), "hotMem": today_load.get("hotMem", 0.0)},
        "trend": (getattr(snap, "nodegroup_trends", None) or {}).get(g.name),
        # The engine's levers per mode, for this group's workloads: what
        # "everything ticked" means. The dialog lists only workloads whose main
        # node group this is, yet a lever changes every pod it has here -- so
        # the browser takes these for anything the dialog does not show.
        "levers": _levers(snap, recs, mine, g.name),
        "group": g.name,
    }


#: Lever combinations `scenarios` searches.
_MODES = ("all", "memory", "replicas", "all+replicas", "memory+replicas")


def _levers(snap, recs, keys: set[str], group: str | None = None) -> dict:
    from rightsizer.engine import realistic as R

    live = bool((snap.facts.kube or {}).get("pods"))
    out = {}
    for mode in _MODES:
        deltas, keep = R.lever_changes(snap, mode, recs)
        out[mode] = {
            "deltas": {w: d for w, d in deltas.items() if w in keys and (d["cpu"] or d["mem"])},
            "keep": {w: n for w, n in keep.items() if w in keys},
        }
        if live and group:
            # Per container, for the exact replay: "ns\0workload\0container" -> [m, bytes].
            reqs, _ = R.scoped(snap, group, R.lever_requests(snap, mode, recs), {})
            out[mode]["requests"] = {"\u0000".join(k): list(v) for k, v in sorted(reqs.items())}
    return out


def _live_model(snap) -> dict | None:
    """The cluster as the exact replay sees it, for the console's port of it."""
    import json as _json

    from rightsizer.engine import realistic as R
    from rightsizer.engine.careplay import Settings, _workload_of, state_from_kube
    from rightsizer.engine.kscheduler import cluster_from_kube

    kube = snap.facts.kube or {}
    if not kube.get("pods"):
        return None
    nodes, pods = cluster_from_kube(kube, workload_of=_workload_of)
    raw = {(p["ns"], p["name"]): p for p in kube["pods"] if p.get("node")}
    specs: list[dict] = []
    spec_ix: dict[int, int] = {}
    labels: list[dict] = []
    label_ix: dict[str, int] = {}

    def spec_json(sp) -> dict:
        return {"nodeSelector": sp.node_selector, "nodeRequired": sp.node_required,
                "nodePreferred": sp.node_preferred, "tolerations": sp.tolerations,
                "podAffinity": sp.pod_affinity, "podAnti": sp.pod_anti,
                "podAffinityPref": sp.pod_affinity_pref, "podAntiPref": sp.pod_anti_pref,
                "spread": sp.spread, "defaultSelector": sp.default_selector}

    def container(c: dict) -> dict:
        out = {"name": c.get("name", ""), "cpu": c.get("cpu"), "mem": c.get("mem"),
               "gpu": c.get("gpu") or 0, "eph": c.get("eph") or 0}
        if c.get("sidecar"):
            out["sidecar"] = True
        return out

    out_pods = []
    for p in pods:
        if id(p.spec) not in spec_ix:
            spec_ix[id(p.spec)] = len(specs)
            specs.append(spec_json(p.spec))
        lk = _json.dumps(p.labels, sort_keys=True)
        if lk not in label_ix:
            label_ix[lk] = len(labels)
            labels.append(p.labels)
        r = raw[(p.ns, p.name)]
        out_pods.append({
            "ns": p.ns, "name": p.name, "node": p.node, "s": spec_ix[id(p.spec)], "l": label_ix[lk],
            "w": _workload_of(r)[0], "ownerKind": p.owner_kind, "ownerName": p.owner_name, "wkey": p.wkey,
            "created": p.created, "deleting": p.deleting, "grace": p.grace, "priority": p.priority,
            "safeToEvict": p.safe_to_evict, "localVolumes": list(p.local_volumes),
            "cpu": p.cpu, "mem": p.mem, "gpu": p.gpu, "eph": p.eph, "nzCpu": p.nz_cpu, "nzMem": p.nz_mem,
            "images": list(p.images), "ports": [list(x) for x in p.ports], "csi": [list(x) for x in p.csi],
            "pvTerms": [list(t) for t in p.pv_terms],
            "containers": [container(c) for c in r.get("containers") or []],
            "init": [container(c) for c in r.get("init") or []],
            "overhead": r.get("overhead") or {"cpu": 0, "mem": 0},
        })
    st = state_from_kube(kube, now=R._kube_now(snap))
    settings = st.settings
    return {
        "now": R._kube_now(snap),
        "settings": {"threshold": settings.threshold, "gpuThreshold": settings.gpu_threshold,
                     "ignoreDaemonsets": settings.ignore_daemonsets, "ignoreMirror": settings.ignore_mirror,
                     "skipLocalStorage": settings.skip_local_storage, "skipSystemPods": settings.skip_system_pods,
                     "skipCustomControllers": settings.skip_custom_controllers,
                     "systemTimeout": settings.system_timeout, "parallelism": settings.parallelism,
                     "drainParallelism": settings.drain_parallelism,
                     "expendableCutoff": settings.expendable_cutoff, "okUnready": settings.ok_unready,
                     "maxUnreadyPct": settings.max_unready_pct, "unneededTime": settings.unneeded_time,
                     "scanInterval": settings.scan_interval, "enabled": settings.enabled},
        "groups": {g: {"name": g, "min": i.min, "max": i.max, "options": i.options, "zones": i.zones}
                   for g, i in st.groups.items()},
        "nodes": [{"name": n.name, "labels": n.labels, "taints": n.taints, "cpu": n.cpu, "mem": n.mem,
                   "pods": n.pods, "gpu": n.gpu, "eph": n.eph, "unschedulable": n.unschedulable,
                   "ready": n.ready, "scaleDownDisabled": n.scale_down_disabled, "group": n.group,
                   "images": n.images, "csiLimits": n.csi_limits} for n in nodes],
        "specs": specs, "labels": labels, "pods": out_pods,
        "pdbs": [{k: b.get(k) for k in ("ns", "name", "selector", "minAvailable", "maxUnavailable",
                                       "disruptionsAllowed", "currentHealthy", "expectedPods")}
                 for b in kube.get("pdbs") or []],
        "namespaces": kube.get("namespaces") or {},
        "baseline": R.kube_baseline(snap),
    }


def _sim(snap, g, cfg, recs, price, itype) -> dict | None:
    """Everything the console needs to pack this node group itself.

    The simulator has to run in the browser -- the console is static, and a
    per-selection answer cannot be precomputed: the subsets are combinatorial.
    So the pod set ships, and the packer is ported.

    Two implementations of the money math is the failure this project already
    had once, when `whatif()` and `scenarios()` disagreed about instance types.
    The guard is `packFixtures`: cases packed here, by the engine behind every
    published number, with the node count each produced. The console's packer
    must reproduce them exactly or its test fails.
    """
    rows = [
        r for r in (snap.placements or {}).get(g.name, [])
        if r.get("owner_kind") != "DaemonSet"
    ]
    if not rows or not itype:
        return None

    from rightsizer.engine.simulate import _peak_index, pack, Pod

    peaks = _peak_index(snap)
    # Measured use per pod -- the mean replica's p95 -- for the scheduler check:
    # the packer sizes by peak, but what a node actually holds is summed use.
    loads = pressure.workload_loads(snap)
    antiaffinity = {
        f"{w.get('namespace')}/{w.get('name')}/{w.get('kind')}"
        for w in snap.facts.workload_constraints
        if w.get("hard_antiaffinity_hostname")
    }
    floors = {f.key: f for f in replica_floors(snap)}
    # The same HPAs replayed at the recommended requests: what the load asks
    # for depends on the request in force, and an option that cuts requests
    # prices its replicas on this one.
    floors_rec = {f.key: f for f in replica_floors(snap, recs)}
    by_container: dict[str, list] = {}
    for r in recs:
        by_container.setdefault(f"{r.namespace}/{r.workload}/{r.kind}", []).append(r)

    order: dict[str, int] = {}
    workloads: list[dict] = []
    pods: list[dict] = []
    for row in rows:
        key = row.get("workload_key", "")
        if key not in order:
            ns, _, rest = key.partition("/")
            name, _, kind = rest.rpartition("/")
            peak = peaks.get((ns, name, kind), {"cpu": 0.0, "mem": 0.0})
            plan = floors.get(key)
            workloads.append({
                "key": key, "ns": ns, "name": name, "kind": kind,
                "peakCpu": round(peak["cpu"], 4),
                "peakMem": round(peak["mem"]),
                "busyCpu": round(loads[key].cpu_busy, 4) if key in loads else None,
                "busyMem": round(loads[key].mem_busy) if key in loads else None,
                "qos": loads[key].qos if key in loads else None,
                "antiaffinity": key in antiaffinity,
                "containers": [
                    {
                        "name": r.container,
                        "replicas": r.replicas,
                        # Unrounded: the browser replays the engine's model
                        # from these, and a rounded delta can move a pod.
                        "cpuReq": r.cpu_request,
                        "cpuRec": r.cpu_recommended,
                        "memReq": r.mem_request,
                        "memRec": r.mem_recommended,
                        "actionable": r.actionable,
                        "blockers": [
                            {"code": b.code, "severity": b.severity, "detail": b.detail}
                            for b in (r.blockers or [])
                        ],
                    }
                    for r in by_container.get(key, [])
                ],
                "hpa": None if plan is None else {
                    "name": plan.hpa, "min": plan.current_min,
                    "suggested": plan.suggested, "replayFloor": plan.floor_at_coverage,
                    "peakDesired": plan.peak_desired, "max": plan.max_replicas,
                    # Replicas the load alone wants, {count: minutes}, at the pod
                    # request each was replayed at -- today's and the recommended.
                    # The console reads what a floor actually runs off these.
                    "demand": [
                        {"request": round(p.request, 6), "minutes": p.demand}
                        for p in (plan, floors_rec.get(key)) if p is not None and p.demand
                    ],
                },
                "drainBlocked": None,
            })
            order[key] = len(workloads) - 1
        pods.append({
            "w": order[key],
            "c": round(float(row.get("cpu", 0.0)), 4),
            "m": round(float(row.get("mem", 0.0))),
        })

    cpu_per_node = round(g.alloc_cpu_per_node - g.ds_cpu_per_node, 4)
    mem_per_node = round(g.alloc_mem_per_node - g.ds_mem_per_node)
    max_pods = snap.facts.max_pods_for(g.name) or 110
    headroom = Tunables(**snap.tunables).pack_headroom if snap.tunables else 0.85

    # Fixtures: the same packer, on the same pod set, at three scales. If the
    # console's port disagrees with any of these, its numbers are not this
    # tool's numbers.
    def nodes_for(scale_cpu: float, scale_mem: float) -> int:
        packed = [
            Pod(name=str(i), workload=workloads[p["w"]]["key"],
                cpu=max(p["c"] * scale_cpu, workloads[p["w"]]["peakCpu"]),
                memory=max(p["m"] * scale_mem, workloads[p["w"]]["peakMem"]),
                antiaffinity=(workloads[p["w"]]["key"]
                              if workloads[p["w"]]["antiaffinity"] else ""))
            for i, p in enumerate(pods)
        ]
        return pack(packed, cpu_per_node=cpu_per_node, mem_per_node=mem_per_node,
                    max_pods_per_node=max_pods, headroom=headroom).nodes_required

    def floors_nodes() -> int:
        kept: dict[str, int] = {}
        packed = []
        for i, p in enumerate(pods):
            w = workloads[p["w"]]
            plan = floors.get(w["key"])
            if plan is not None:
                seen = kept.get(w["key"], 0)
                if seen >= plan.suggested:
                    continue
                kept[w["key"]] = seen + 1
            packed.append(Pod(
                name=str(i), workload=w["key"],
                cpu=max(p["c"], w["peakCpu"]), memory=max(p["m"], w["peakMem"]),
                antiaffinity=w["key"] if w["antiaffinity"] else "",
            ))
        return pack(packed, cpu_per_node=cpu_per_node, mem_per_node=mem_per_node,
                    max_pods_per_node=max_pods, headroom=headroom).nodes_required

    return {
        # What runs today, so a selection can be judged on the node count it
        # would actually leave: running minus what it frees.
        "nodesNow": g.nodes_current,
        # The engine's realistic model, for the browser to replay per selection.
        "model": _model(snap, g, cfg, recs),
        "nodeCpu": cpu_per_node,
        "nodeMem": mem_per_node,
        "maxPods": max_pods,
        "headroom": headroom,
        "pricePerNode": round(price(itype), 2) if itype else 0.0,
        "pods": pods,
        "workloads": workloads,
        "packFixtures": [
            {"scaleCpu": 1.0, "scaleMem": 1.0, "nodes": nodes_for(1.0, 1.0)},
            {"scaleCpu": 0.5, "scaleMem": 1.0, "nodes": nodes_for(0.5, 1.0)},
            {"scaleCpu": 1.0, "scaleMem": 1.5, "nodes": nodes_for(1.0, 1.5)},
            # The replica path, which scaling alone does not exercise and which
            # is the one a reader actually uses.
            {"scaleCpu": 1.0, "scaleMem": 1.0, "applyFloors": True,
             "nodes": floors_nodes()},
        ],
    }


HPA_FILE_SCHEMA = 1


def write_hpa_files(env: str, out_dir: pathlib.Path, data: pathlib.Path) -> int:
    """Per-HPA minute history for the console's projection, with fixtures.

    Fetched per HPA when a row is opened, like the trend files: a month at one
    minute is 43,200 points per HPA, and every reader does not need every one.
    The fixtures are this engine's replay at today's floor and the suggested
    one, so the browser's port can prove it reproduces them before it draws.
    """
    from rightsizer.engine.hpa_replay import Behavior, effective_lag, replay

    snap = Snapshot.read(layout.find_snapshot(out_dir, env))
    if not snap.hpa_history:
        return 0
    floors = {f.hpa: f for f in replica_floors(snap)}
    containers: dict[tuple[str, str], float] = {}
    for s in snap.usage:
        if s.resource == "cpu":
            containers[(s.namespace, s.workload)] = containers.get((s.namespace, s.workload), 0.0) + s.request
    target_dir = layout.hpa_dir(data, env)
    written = 0
    for hpa in snap.hpas:
        hist = snap.hpa_history.get(f"{hpa.namespace}/{hpa.name}")
        request = containers.get((hpa.namespace, hpa.target_name), 0.0)
        if not hist or not hpa.cpu_target or request <= 0:
            continue
        # The console's projection replays CPU only; for an HPA that also
        # scales on memory it would draw a controller that is not this one.
        # Its floor and ceiling come from the engine's replay, which models both.
        if "memory" in (hpa.resource_targets or {}):
            continue
        cpu = [None if v is None else v / 1000 for v in hist["cpu_m"]]
        behavior = Behavior.from_dict(hist["behavior"])
        lag = effective_lag(hist.get("lag_minutes"))
        plan = floors.get(hpa.name)
        fixtures = []
        for floor in sorted({hpa.min_replicas, *( [plan.suggested] if plan else [] )}):
            r = replay(cpu, pod_request=request, target=hpa.cpu_target, min_replicas=floor,
                       max_replicas=hpa.max_replicas or 0, behavior=behavior, lag_minutes=lag)
            fixtures.append({"floor": floor, "request": request, **r.summary()})
        doc = {
            "schema": HPA_FILE_SCHEMA, "env": env, "namespace": hpa.namespace, "hpa": hpa.name,
            "workload": hpa.target_name, "start": hist["start"], "step": hist["step"],
            "cpuM": hist["cpu_m"], "desired": hist["desired"], "ready": hist["ready"],
            "minHistory": hist.get("min"),
            "validation": plan.validation if plan else None,
            "behavior": hist["behavior"], "lagMinutes": lag, "cpuOnly": hist.get("cpu_only", True),
            "target": hpa.cpu_target, "minReplicas": hpa.min_replicas,
            "maxReplicas": hpa.max_replicas or 0, "podRequest": request,
            "fixtures": fixtures,
        }
        slug = f"{hpa.namespace}__{hpa.name}".replace("/", "-")
        layout.write_atomic(target_dir / f"{slug}.json", json.dumps(doc, separators=(",", ":")))
        written += 1
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Export collected snapshots for the console, one file per "
                                             "cluster and period, and rebuild the index.")
    # Default straight into the UI's public directory. Writing somewhere else and
    # copying by hand is how the console ends up rendering a payload older than
    # the code that reads it -- which looks exactly like a bug in the analysis.
    ap.add_argument("dest", nargs="?", type=pathlib.Path, default=layout.DEFAULT_DATA,
                    help="the console's data directory (default: ../frontend/public/data)")
    ap.add_argument("--out-dir", type=pathlib.Path, default=layout.DEFAULT_OUT,
                    help="where collect wrote the snapshots")
    ap.add_argument("--config", type=pathlib.Path, default=CONFIG_PATH,
                    help="optional overrides: per-cluster region/profile/account, known "
                         "clusters, period labels")
    ap.add_argument("--discount", type=float, default=None,
                    help=f"enterprise discount on list price (default {DEFAULT_DISCOUNT})")
    ap.add_argument("--types", default="",
                    help="extra instance types to offer in the console's type picker, comma "
                         "separated (described and priced with the run's AWS access); also "
                         "`instanceTypes` in the config")
    ap.add_argument("--env", default="",
                    help="only re-export these clusters, comma separated (e.g. prod-us2); "
                         "every other cluster's files are left as they are")
    ap.add_argument("--current-only", action="store_true",
                    help="skip the backtest periods (older snapshots)")
    ap.add_argument("--index-only", action="store_true",
                    help="rebuild index.json from the cluster files already in DEST; read no snapshot")
    args = ap.parse_args(argv)

    if args.dest.suffix == ".json":
        print(f"{args.dest}: DEST is now the data directory (one file per cluster), not a single "
              f"fleet.json -- e.g. {layout.DEFAULT_DATA}", file=sys.stderr)
        return 2
    config = load_config(args.config)
    labels = config.get("periods") or {}
    known = config.get("known") or []

    if not args.index_only:
        EXTRA_TYPES[:] = sorted({t.strip() for t in [*args.types.split(","), *(config.get("instanceTypes") or [])]
                                 if t and t.strip()})
        discount = args.discount if args.discount is not None else float(
            config.get("discount", DEFAULT_DISCOUNT))
        found, skipped = discover(args.out_dir)
        only = {e.strip() for e in args.env.split(",") if e.strip()}
        if only:
            found = {end: [e for e in envs if e in only] for end, envs in found.items()}
            found = {end: envs for end, envs in found.items() if envs}
            missing = only - {e for envs in found.values() for e in envs}
            for env in sorted(missing):
                print(f"no snapshot for {env} in {args.out_dir}", file=sys.stderr)
        if args.current_only:
            found = {end: envs for end, envs in found.items() if end is None}
        if not found:
            print(f"no snapshots in {args.out_dir}", file=sys.stderr)
            return 1

        # Current first, then backtests newest first.
        order = [None] + sorted((e for e in found if e is not None), reverse=True)
        for end in (e for e in order if e in found):
            pid = layout.period_id(end)
            period = {"id": pid, "end": end,
                      "label": labels.get(pid) or ("Current" if end is None
                                                   else f"Backtest \u00b7 window ending {end}")}
            print(f"[{pid}]")
            for env in sorted(found[end]):
                c = build(env, args.out_dir, end, config=config, discount=discount)
                if not c:
                    continue
                path = layout.write_period_file(args.dest, env, period, c,
                                                schema=SCHEMA_VERSION, discount=discount)
                note = "" if c["metaSource"] != "inferred" else "  (metadata inferred)"
                print(f"   {env:14s} ${c['currentRunRate']:>10,.2f}/mo  net ${c['net']:>10,.2f}  "
                      f"{len(c['nodegroups']):2d} groups  {len(c['actions']):2d} actions  "
                      f"-> {path.relative_to(args.dest)}{note}")
                for w in c["exportWarnings"]:
                    print(f"      warning: {w}", file=sys.stderr)
        for name in skipped:
            print(f"skipped {name}: not <env>/snapshot.json, <env>/snapshot-<YYYY-MM-DD>.json "
                  f"or the older snapshot-<env>[-<YYYY-MM-DD>].json", file=sys.stderr)
        for env in sorted(found.get(None, [])):
            n = write_hpa_files(env, args.out_dir, args.dest)
            if n:
                print(f"   {env:14s} {n} HPA history file(s)")

    # Always from disk, so clusters this run did not touch stay listed.
    index = layout.write_index(args.dest, known=known, labels=labels)
    for u in index["unreadable"]:
        print(f"unreadable {u['file']}: {u['error']} -- left out of the index", file=sys.stderr)
    if index["discountMixed"]:
        print("warning: clusters were exported at different discounts; re-export them together "
              "(--discount) so the fleet total uses one", file=sys.stderr)
    stale = sorted({c["env"] for p in index["periods"] for c in p["clusters"]
                    if (c["schema"] or 0) < SCHEMA_VERSION})
    if stale:
        print(f"warning: {', '.join(stale)} exported by an older version; the console will not show "
              f"them until re-exported (--env {','.join(stale)})", file=sys.stderr)
    envs = {c["env"] for p in index["periods"] for c in p["clusters"]}
    print(f"wrote {layout.index_path(args.dest)}: {len(index['periods'])} period(s), "
          f"{len(envs)} cluster(s), {len(index['uncollected'])} uncollected")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
