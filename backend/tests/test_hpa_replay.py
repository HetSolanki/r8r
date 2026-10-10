"""The per-minute HPA replay: controller semantics, behavior, and floor choice."""

from __future__ import annotations

from rightsizer.engine.hpa_replay import (
    BURST_UTIL_CAP,
    Behavior,
    Policy,
    Rules,
    choose_floor,
    default_behavior,
    infer_behavior,
    measure_lag,
    parse_behavior,
    replay,
    rle_decode,
    rle_encode,
    validate,
)

#: The shape every HPA in prod-us1 uses: +1 pod / 30s, -1 pod / 600s after 600s.
FLEET = Behavior(Rules(0, "Max", [Policy("Pods", 1, 30)]),
                 Rules(600, "Max", [Policy("Pods", 1, 600)]), "spec")


def run(cpu, floor=5, request=1.0, target=0.6, behavior=FLEET, lag=0, max_replicas=30):
    return replay(cpu, pod_request=request, target=target, min_replicas=floor,
                  max_replicas=max_replicas, behavior=behavior, lag_minutes=lag)


def test_quiet_load_sits_on_the_floor() -> None:
    r = run([0.5] * 120)
    assert set(r.replicas) == {5}
    assert r.over_request_minutes == 0


def test_tolerance_band_holds_the_count() -> None:
    # 5 pods x 1.0 x 0.6 = 3.0 cores at target; 3.2 is inside 10%.
    r = run([3.2] * 30)
    assert set(r.replicas) == {5}


def test_scale_up_is_rate_limited_by_policy() -> None:
    # 12 cores wants 20 pods; 1 pod / 30s allows two a minute.
    r = run([12.0] * 10)
    assert r.replicas[:4] == [7, 9, 11, 13]


def test_scale_down_waits_for_stabilization_then_steps() -> None:
    cpu = [12.0] * 10 + [0.5] * 60
    r = run(cpu)
    peak_at = r.replicas.index(max(r.replicas))
    after = r.replicas[10:]
    # Held for the 600s window, then one pod per 600s.
    assert after[:9] == [max(r.replicas)] * 9
    drops = [i for i in range(1, len(after)) if after[i] < after[i - 1]]
    assert all(b - a >= 10 for a, b in zip(drops, drops[1:], strict=False))
    assert peak_at < 10


def test_readiness_lag_leaves_ready_pods_overloaded() -> None:
    cpu = [0.5] * 5 + [9.0] * 10
    quick = run(cpu, lag=0)
    slow = run(cpu, lag=4)
    assert slow.over_request_minutes > quick.over_request_minutes
    assert slow.ready[6] < slow.replicas[6]


def test_default_behavior_scales_up_fast() -> None:
    r = run([12.0] * 3, behavior=default_behavior())
    assert r.replicas[0] == 20


def test_disabled_scale_down_never_shrinks() -> None:
    b = Behavior(FLEET.up, Rules(0, "Disabled", []), "spec")
    r = run([12.0] * 5 + [0.1] * 60, behavior=b)
    assert r.replicas[-1] == max(r.replicas)


def test_floor_choice_rejects_a_floor_that_deepens_bursts() -> None:
    cpu = [0.5] * 1440
    for day in range(0, 1440, 240):
        cpu[day:day + 3] = [6.0] * 3
    chosen = choose_floor(lambda f: run(cpu, floor=f, request=1.024), 5, allowance_minutes=30)
    assert chosen is not None
    floor, after, base = chosen
    assert 2 <= floor < 5
    assert after.max_util <= max(base.max_util, BURST_UTIL_CAP)
    # One lower would have broken a limit, or it would have been chosen.
    lower = run(cpu, floor=floor - 1, request=1.024) if floor > 2 else None
    if lower is not None:
        assert (lower.max_util > max(base.max_util, BURST_UTIL_CAP)
                or lower.over_request_minutes > base.over_request_minutes + 30)


def test_quiet_workload_goes_to_the_minimum_floor() -> None:
    chosen = choose_floor(lambda f: run([0.2] * 600, floor=f), 10, allowance_minutes=30)
    assert chosen is not None and chosen[0] == 2


def test_parse_behavior_reads_the_spec() -> None:
    b = parse_behavior({
        "scaleDown": {"policies": [{"periodSeconds": 600, "type": "Pods", "value": 1}],
                      "selectPolicy": "Max", "stabilizationWindowSeconds": 600},
        "scaleUp": {"policies": [{"periodSeconds": 30, "type": "Pods", "value": 1}],
                    "stabilizationWindowSeconds": 0},
    })
    assert b.source == "spec"
    assert b.down.stabilization == 600 and b.down.policies[0].period == 600
    assert b.up.policies[0].value == 1
    # Round-trips through the snapshot.
    assert Behavior.from_dict(b.to_dict()) == b


def test_parse_behavior_fills_a_missing_direction_with_defaults() -> None:
    b = parse_behavior(
        {"scaleUp": {"policies": [{"periodSeconds": 60, "type": "Pods", "value": 2}]}})
    assert b.down == default_behavior().down


def test_infer_behavior_reads_the_step_policy_off_history() -> None:
    desired = [5] * 10 + [9] * 30 + [8] * 10 + [7] * 10 + [6] * 10 + [5] * 30
    b = infer_behavior(desired)
    assert b.source == "inferred"
    assert b.down.policies[0].value == 1 and b.down.policies[0].period == 600


def test_unreadable_behavior_is_assumed_slow_not_defaulted_fast() -> None:
    b = infer_behavior([5] * 500)
    assert b.source == "assumed"
    assert b.up.policies[0].type == "Pods" and b.up.policies[0].period == 30


def test_measure_lag() -> None:
    desired = [5] * 5 + [6] * 20 + [5] * 5 + [7] * 20 + [5] * 5 + [6] * 20
    ready = [5] * 8 + [6] * 22 + [5] * 2 + [7] * 23 + [5] * 3 + [6] * 20
    assert measure_lag(desired, ready) == 3.0


def test_rle_round_trip() -> None:
    values = [5, 5, 5, None, None, 6, 5, 5]
    assert rle_encode(values) == [[5, 3], [None, 2], [6, 1], [5, 2]]
    assert rle_decode(rle_encode(values)) == values


def test_validate_reports_fidelity() -> None:
    v = validate([5] * 100, [5] * 90 + [7] * 10)
    assert v["within_one"] == 0.9 and v["exact"] == 0.9


class _FakeClient:
    """Answers every range query with a constant, one point per step."""

    max_workers = 2

    def __init__(self, value):
        self.value = value
        self.calls = []

    def query_range(self, query, start, end, step):
        self.calls.append((query, start, end))
        return [{"values": [[t, str(self.value)] for t in range(start, end + 1, step)]}]


def test_aligned_series_fills_every_minute_across_chunks() -> None:
    from rightsizer.sources.hpa_history import CHUNK_HOURS, STEP, _aligned

    start = 1_700_000_000 // STEP * STEP
    end = start + 7 * 86400
    client = _FakeClient(2.5)
    got = _aligned(client, "q", start, end)
    assert len(got) == (end - start) // STEP
    assert all(v == 2.5 for v in got)
    # Chunked so no single request exceeds Prometheus' per-series point limit.
    assert len(client.calls) == -(-(end - start) // (CHUNK_HOURS * 3600))
    assert all((e - s) // STEP < 11_000 for _, s, e in client.calls)


def test_queries_select_the_hpa_and_its_workload() -> None:
    from rightsizer.core.config import Scope
    from rightsizer.sources.hpa_history import _queries
    from rightsizer.core.models import HPASpec

    hpa = HPASpec(namespace="apps", name="ingest-worker-hpa", target_kind="Deployment",
                  target_name="ingest-worker", min_replicas=5, max_replicas=30,
                  targets=[("cpu", "utilization")], cpu_target=0.6)
    q = _queries(Scope(env="test-cluster"), hpa, "Deployment")
    assert 'horizontalpodautoscaler="ingest-worker-hpa"' in q["desired"]
    assert 'deployment="ingest-worker"' in q["ready"]
    # Pod-level CPU at a 1-minute rate, restricted to this workload's pods.
    assert "[1m]" in q["cpu"] and 'owner_name=~"ingest-worker-[a-z0-9]+"' in q["cpu"]
    for query in q.values():
        assert query.count("{") == query.count('env="test-cluster"')
