"""Minute-by-minute replay of a CPU HorizontalPodAutoscaler.

The equilibrium model -- ``desired = ceil(total / (request x target))`` over
quantiles of a 5-minute series -- answers "where does the HPA settle", and on a
bursty service that is the wrong question. Measured on ingest-worker: the
HPA went 5 -> 9 on a 2-minute burst of 6.1 cores that the 5-minute rate
averaged to 2.5, then took 40 minutes to come back down, one pod per 10
minutes. The equilibrium model said 5 the whole time.

So this replays the controller, not the formula, at the resolution it reacts to:

* the recommendation ``ceil(total / (podRequest x target))``, held inside the
  10% tolerance band, computed over pods that are *ready* -- new pods take
  real minutes to become ready, measured per workload from its own history;
* ``behavior``: stabilization windows (max of recent recommendations on the way
  down, min on the way up) and the rate policies -- ``1 pod per 30s`` up is a
  2-pods-a-minute ceiling, which is what makes a low floor slow to recover;
* min/max clamping.

It is still a model. The controller runs every 15s on metrics-server samples;
this runs every minute on a 1-minute rate. :func:`validate` replays today's
floor and compares with what the real HPA did, so the fidelity is a number
rather than a hope.
"""

from __future__ import annotations

import bisect
import math
from collections import deque
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

TOLERANCE = 0.10
MIN_REPLICA_FLOOR = 2
#: Readiness delay assumed when the history cannot show one.
DEFAULT_LAG_MINUTES = 2
#: Cap on a measured delay: a scale-up that never completes (quota, pending
#: pods) is a different failure, not a slow start.
MAX_LAG_MINUTES = 15
#: A lower floor may not push the ready pods' peak utilisation past this, or
#: past today's peak if that is already higher. Minutes-over-request alone
#: misses depth: a floor of 2 can add half an hour at 290% of request and still
#: read as "29 minutes".
BURST_UTIL_CAP = 1.5
#: The memory equivalent: a lower floor may not take the working set past the
#: pods' memory request, or past today's peak if that is already higher.
#: Memory above request is what the kubelet evicts first under pressure.
MEM_UTIL_CAP = 1.0


# -- behavior -----------------------------------------------------------------

@dataclass
class Policy:
    type: str          # "Pods" | "Percent"
    value: int
    period: int        # seconds


@dataclass
class Rules:
    stabilization: int = 0                          # seconds
    select: str = "Max"                             # "Max" | "Min" | "Disabled"
    policies: list[Policy] = field(default_factory=list)


@dataclass
class Behavior:
    up: Rules
    down: Rules
    #: "spec" (read from the HPA), "inferred" (from its history), "assumed"
    #: (neither was possible) or "default" (the Kubernetes defaults, when a
    #: spec is read and leaves a direction unset).
    source: str = "default"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> Behavior:
        def rules(r: dict) -> Rules:
            return Rules(stabilization=int(r.get("stabilization", 0)),
                         select=r.get("select", "Max"),
                         policies=[Policy(**p) for p in r.get("policies", [])])
        return cls(up=rules(raw["up"]), down=rules(raw["down"]),
                   source=raw.get("source", "default"))


def default_behavior() -> Behavior:
    """The Kubernetes defaults (autoscaling/v2), used when nothing better is known."""
    return Behavior(
        up=Rules(0, "Max", [Policy("Percent", 100, 15), Policy("Pods", 4, 15)]),
        down=Rules(300, "Max", [Policy("Percent", 100, 15)]),
        source="default",
    )


def assumed_behavior() -> Behavior:
    """What to assume when an HPA's behavior can be neither read nor inferred.

    Not the Kubernetes defaults: those scale up by 100% or 4 pods every 15s,
    which makes any low floor look safe. Every HPA in this fleet whose spec
    could be read scales up at 1 pod / 30s and down at 1 pod per 5-10 minutes,
    so that is the cautious assumption -- and it is labelled as one.
    """
    return Behavior(
        up=Rules(0, "Max", [Policy("Pods", 1, 30)]),
        down=Rules(300, "Max", [Policy("Pods", 1, 300)]),
        source="assumed",
    )


def parse_behavior(spec: dict | None) -> Behavior:
    """``spec.behavior`` of an autoscaling/v2 HPA, defaults filled per direction."""
    base = default_behavior()
    if not spec:
        return base

    def rules(raw: dict | None, fallback: Rules) -> Rules:
        if not raw:
            return fallback
        policies = [Policy(p.get("type", "Pods"), int(p.get("value", 0)),
                           int(p.get("periodSeconds", 15)))
                    for p in raw.get("policies") or []] or fallback.policies
        stab = raw.get("stabilizationWindowSeconds")
        return Rules(stabilization=int(fallback.stabilization if stab is None else stab),
                     select=raw.get("selectPolicy", "Max"), policies=policies)

    return Behavior(up=rules(spec.get("scaleUp"), base.up),
                    down=rules(spec.get("scaleDown"), base.down), source="spec")


def infer_behavior(desired: Sequence[int | None], step: int = 60) -> Behavior:
    """Read the rate policies off what the HPA actually did.

    Scale-down: within a run of consecutive decreases, the spacing between
    steps and the size of each step are the policy. A scale-down that moved one
    pod every ten minutes is ``1 pod / 600s`` whatever the spec says -- and the
    stabilization window is set to the same period, which is how every HPA with
    an explicit policy in this fleet is configured.
    """
    base = assumed_behavior()
    ups: list[tuple[int, int]] = []     # (index, delta)
    downs: list[tuple[int, int]] = []
    prev = None
    for i, v in enumerate(desired):
        if v is None:
            continue
        if prev is not None and v != prev:
            (ups if v > prev else downs).append((i, abs(v - prev)))
        prev = v

    def pace(events: list[tuple[int, int]], run_gap: int) -> tuple[int, int] | None:
        gaps = [b[0] - a[0] for a, b in zip(events, events[1:], strict=False)
                if b[0] - a[0] <= run_gap]
        if len(gaps) < 3:
            return None
        gaps.sort()
        period_steps = max(1, gaps[len(gaps) // 2])
        sizes = sorted(d for _, d in events)
        return sizes[len(sizes) // 2], period_steps * step

    def fastest(events: list[tuple[int, int]]) -> tuple[int, int] | None:
        """The quickest scale-up seen: a lower bound on what the policy allows.

        The median would measure how fast demand ramped, not the rate limit --
        an HPA only meets its limit on a sharp burst. On prod-us1 the median read
        1 pod / 60-120s against a spec of 1 / 30s; the fastest reads 2 a minute.
        """
        if len(events) < 3:
            return None
        per_step = max(d for _, d in events)
        if per_step >= 2:
            return per_step, step
        gaps = [b[0] - a[0] for a, b in zip(events, events[1:], strict=False)]
        # Single pods minutes apart are a limit; hours apart are just demand.
        if min(gaps) > 5:
            return None
        return 1, max(1, min(gaps)) * step

    down = pace(downs, run_gap=60)
    up = fastest(ups)
    if not down and not up:
        return base
    return Behavior(
        up=Rules(0, "Max", [Policy("Pods", up[0], up[1])]) if up else base.up,
        down=Rules(down[1], "Max", [Policy("Pods", down[0], down[1])]) if down else base.down,
        source="inferred",
    )


def measure_lag(desired: Sequence[int | None], ready: Sequence[int | None]) -> float | None:
    """Median minutes from the HPA asking for more pods to that many being ready."""
    lags: list[int] = []
    prev = None
    for i, want in enumerate(desired):
        if want is None:
            continue
        if prev is not None and want > prev:
            for j in range(i, min(len(ready), i + MAX_LAG_MINUTES + 1)):
                if ready[j] is not None and ready[j] >= want:
                    lags.append(j - i)
                    break
        prev = want
    if len(lags) < 3:
        return None
    lags.sort()
    return float(lags[len(lags) // 2])


# -- encoding -------------------------------------------------------------------

def rle_encode(values: Sequence[int | None]) -> list[list]:
    out: list[list] = []
    for v in values:
        if out and out[-1][0] == v:
            out[-1][1] += 1
        else:
            out.append([v, 1])
    return out


def rle_decode(runs: Sequence[Sequence]) -> list:
    out: list = []
    for v, n in runs:
        out.extend([v] * int(n))
    return out


# -- the replay -----------------------------------------------------------------

def _allowance(
    rules: Rules, replicas: int, events: list[tuple[int, int]], t: int, step: int,
) -> int:
    """How far one tick may move, under a direction's policies.

    ``events`` are (time, pods moved) in this direction. A policy with a period
    shorter than the tick compounds within it -- ``1 pod / 30s`` is two a minute.
    """
    if rules.select == "Disabled":
        return 0
    limits = []
    for p in rules.policies:
        if p.period < step:
            per_tick = max(1, step // max(p.period, 1))
            limits.append(p.value * per_tick if p.type == "Pods"
                          else math.ceil(replicas * ((1 + p.value / 100) ** per_tick - 1)))
            continue
        moved = sum(n for at, n in events if t - at < p.period)
        start = replicas - moved
        cap = p.value if p.type == "Pods" else math.ceil(max(start, 1) * p.value / 100)
        limits.append(max(0, cap - moved))
    if not limits:
        return 10**9
    return max(limits) if rules.select != "Min" else min(limits)


@dataclass
class ReplayResult:
    replicas: list[int]              # spec replicas each minute
    ready: list[int]
    util: list[float | None]         # usage / (ready x request)
    minutes: int
    avg_replicas: float
    peak_replicas: int
    #: Minutes the ready pods ran above their CPU request -- the exposure a lower
    #: floor adds, because fewer pods absorb a burst while new ones start.
    over_request_minutes: int
    over_target_minutes: int
    max_util: float
    longest_over_request: int
    #: CPU used beyond what the ready pods reserved, summed: duration and depth
    #: in one number (core-minutes).
    unreserved_core_minutes: float
    #: Minutes the controller wanted more than ``maxReplicas`` and was held at
    #: it, and the most it wanted: where the ceiling, not the floor, is the risk.
    capped_minutes: int = 0
    peak_wanted: int = 0
    #: The same exposure for memory, when the HPA also scales on it: minutes
    #: the ready pods' working set was above their memory request, and the peak.
    mem_over_request_minutes: int = 0
    mem_max_util: float = 0.0
    #: What the controller wanted each minute before maxReplicas clamped it.
    wanted: list[int] = field(default_factory=list, repr=False)

    def sustained_wanted(self, minutes: int = 5) -> int:
        """The most replicas wanted for ``minutes`` in a row: a ceiling sized on
        load that lasted, not on one noisy sample."""
        w = self.wanted
        if len(w) < minutes:
            return max(w, default=0)
        best = 0
        window: deque[tuple[int, int]] = deque()
        for i, v in enumerate(w):
            while window and window[-1][1] >= v:
                window.pop()
            window.append((i, v))
            if window[0][0] <= i - minutes:
                window.popleft()
            if i >= minutes - 1:
                best = max(best, window[0][1])
        return best

    def summary(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in (
            "minutes", "avg_replicas", "peak_replicas", "over_request_minutes",
            "over_target_minutes", "max_util", "longest_over_request",
            "unreserved_core_minutes", "capped_minutes", "peak_wanted",
            "mem_over_request_minutes", "mem_max_util")}


@dataclass
class MemoryMetric:
    """A memory utilization target, and the load it sees.

    Memory does not spread the way CPU does. Every pod carries a footprint of
    its own -- runtime, caches -- whatever the replica count, so the working set
    is modelled as ``ready x base + variable(t)``: ``base`` a pod's idle
    footprint, ``variable`` the load-driven rest, which does divide across
    pods. Where ``base`` alone is above the target, the HPA can never scale
    down -- the classic memory-HPA trap, and the replay shows it.
    """
    variable: list[float | None]     # bytes each minute beyond ready x base
    base: float                      # bytes per pod
    pod_request: float               # bytes
    target: float


def memory_metric(mem: Sequence[float | None], ready: Sequence[int | None],
                  pod_request: float, target: float) -> MemoryMetric:
    """Split a measured working-set series into per-pod footprint and load.

    ``base`` is the 5th percentile of per-pod working set over the window --
    the quietest a pod gets -- so a pod is never modelled lighter than it ran.
    """
    per_pod = sorted(m / r for m, r in zip(mem, ready, strict=False)
                     if m is not None and r)
    base = per_pod[len(per_pod) // 20] if per_pod else 0.0
    variable = [None if m is None else max(0.0, m - (r or 0) * base)
                for m, r in zip(mem, list(ready) + [None] * (len(mem) - len(ready)), strict=False)]
    return MemoryMetric(variable=variable, base=base, pod_request=pod_request, target=target)


def effective_lag(measured: float | None) -> float:
    """Minutes from a burst being seen to new capacity serving it.

    Measured readiness is often under a minute here -- ready pods catch up with
    the desired count inside one sample. But the controller can only act on a
    burst it has already observed, so capacity is at least one tick behind.
    """
    if measured is None:
        return DEFAULT_LAG_MINUTES
    return max(1.0, measured)


def replay(
    cpu: Sequence[float | None], *, pod_request: float, target: float,
    min_replicas: int | Sequence[int | None], max_replicas: int, behavior: Behavior,
    lag_minutes: float = DEFAULT_LAG_MINUTES, step: int = 60,
    memory: MemoryMetric | None = None,
) -> ReplayResult:
    """Run the controller over a per-minute series of total CPU (cores).

    ``min_replicas`` is a constant for a proposal, or the floor that was in
    force each minute -- which is how a replay is validated against a window
    in which someone changed the floor.
    """
    floors = None if isinstance(min_replicas, int) else list(min_replicas)
    floor_now = min_replicas if floors is None else next(
        (f for f in floors if f is not None), 1)
    ceiling = max_replicas if max_replicas > 0 else 10**6
    # Half-up, not Python's round-half-even, so the console's port agrees.
    lag = max(0, int(math.floor(lag_minutes + 0.5)))

    def recommend(total: float) -> int:
        return max(1, math.ceil(total / (pod_request * target)))

    mem_util: list[float | None] = []
    wanted: list[int] = []
    capped = 0
    peak_wanted = 0

    # Start on the floor, as an HPA that had been sitting quietly would. Over a
    # 43,200-minute window the first minute is immaterial; starting from the
    # first sample's recommendation instead would begin a burst already scaled.
    spec = min(max(floor_now, 1), ceiling)
    # The minute each pod becomes ready. Pods are added in time order, so this
    # stays sorted: counting ready pods is a bisection, and the newest -- the
    # unready ones, which the controller removes first -- are at the end.
    ready_at: list[int] = [-(10**9)] * spec
    # Sliding min (scale-up window) and max (scale-down window) of recent
    # recommendations, as monotonic deques: O(1) per minute rather than a
    # rescan of the window. Same answer as min/max over the window.
    up_win: deque[tuple[int, int]] = deque()
    down_win: deque[tuple[int, int]] = deque()
    up_events: list[tuple[int, int]] = []
    down_events: list[tuple[int, int]] = []
    out_spec: list[int] = []
    out_ready: list[int] = []
    out_util: list[float | None] = []
    horizon = max(behavior.up.stabilization, behavior.down.stabilization,
                  *(p.period for p in behavior.up.policies + behavior.down.policies), step)

    for i, total in enumerate(cpu):
        t = i * step
        if floors is not None and i < len(floors) and floors[i] is not None:
            floor_now = floors[i]
        ready = bisect.bisect_right(ready_at, i) or spec
        if total is None:
            out_spec.append(spec)
            out_ready.append(ready)
            out_util.append(None)
            continue
        util = total / (ready * pod_request) if pod_request > 0 else 0.0
        ratio = util / target if target > 0 else 1.0
        rec = spec if abs(ratio - 1.0) <= TOLERANCE else recommend(total)
        # Several metrics: each proposes, and the controller takes the largest.
        if memory is not None:
            var = memory.variable[i] if i < len(memory.variable) else None
            if var is None:
                mem_util.append(None)
            else:
                used = ready * memory.base + var
                mu = used / (ready * memory.pod_request) if memory.pod_request > 0 else 0.0
                mem_util.append(mu)
                mratio = mu / memory.target if memory.target > 0 else 1.0
                if abs(mratio - 1.0) > TOLERANCE:
                    rec = max(rec, math.ceil(ready * mratio))
                else:
                    rec = max(rec, spec)
        if rec > ceiling:
            capped += 1
        peak_wanted = max(peak_wanted, rec)
        wanted.append(rec)
        rec = min(max(rec, floor_now), ceiling)

        while up_win and up_win[-1][1] >= rec:
            up_win.pop()
        up_win.append((t, rec))
        while t - up_win[0][0] > behavior.up.stabilization:
            up_win.popleft()
        while down_win and down_win[-1][1] <= rec:
            down_win.pop()
        down_win.append((t, rec))
        while t - down_win[0][0] > behavior.down.stabilization:
            down_win.popleft()
        up_rec = up_win[0][1]
        down_rec = down_win[0][1]
        new = spec
        if new < up_rec:
            new = up_rec
        if new > down_rec:
            new = down_rec

        up_events = [(at, n) for at, n in up_events if t - at < horizon]
        down_events = [(at, n) for at, n in down_events if t - at < horizon]
        if new > spec:
            new = min(new, spec + _allowance(behavior.up, spec, up_events, t, step))
        elif new < spec:
            new = max(new, spec - _allowance(behavior.down, spec, down_events, t, step))
        new = min(max(new, floor_now), ceiling)

        if new > spec:
            ready_at.extend([i + lag] * (new - spec))
            up_events.append((t, new - spec))
        elif new < spec:
            del ready_at[new:]
            down_events.append((t, spec - new))
        spec = new

        out_spec.append(spec)
        out_ready.append(ready)
        out_util.append(util)

    utils = [u for u in out_util if u is not None]
    unreserved = sum(max(0.0, c - r * pod_request)
                     for c, r in zip(cpu, out_ready, strict=True) if c is not None)
    over, run, longest = 0, 0, 0
    for u in out_util:
        if u is not None and u > 1.0:
            over += 1
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    n = max(len(out_spec), 1)
    return ReplayResult(
        replicas=out_spec, ready=out_ready, util=out_util, minutes=len(out_spec),
        avg_replicas=round(sum(out_spec) / n, 3), peak_replicas=max(out_spec, default=0),
        over_request_minutes=over,
        over_target_minutes=sum(1 for u in utils if u > target),
        max_util=round(max(utils, default=0.0), 4), longest_over_request=longest,
        unreserved_core_minutes=round(unreserved, 2),
        capped_minutes=capped, peak_wanted=peak_wanted, wanted=wanted,
        mem_over_request_minutes=sum(1 for u in mem_util if u is not None and u > 1.0),
        mem_max_util=round(max((u for u in mem_util if u is not None), default=0.0), 4),
    )


def validate(model: Sequence[int], actual: Sequence[int | None]) -> dict[str, Any] | None:
    """How closely a replay of today's floor tracks what the HPA really did."""
    pairs = [(m, a) for m, a in zip(model, actual, strict=False) if a is not None]
    if len(pairs) < 60:
        return None
    err = [abs(m - a) for m, a in pairs]
    return {
        "minutes": len(pairs),
        "within_one": round(sum(1 for e in err if e <= 1) / len(pairs), 4),
        "exact": round(sum(1 for e in err if e == 0) / len(pairs), 4),
        "mae": round(sum(err) / len(pairs), 3),
        "model_avg": round(sum(m for m, _ in pairs) / len(pairs), 3),
        "actual_avg": round(sum(a for _, a in pairs) / len(pairs), 3),
    }


def choose_floor(
    run, current_min: int, *, allowance_minutes: int, base: ReplayResult | None = None,
) -> tuple[int, ReplayResult, ReplayResult] | None:
    """The lowest floor that does not materially add burst exposure.

    ``run(floor)`` replays at that floor. A floor is acceptable when, with the
    pods that were actually ready,

    * it adds no more than ``allowance_minutes`` above request over the window
      compared with today, and
    * the peak stays under :data:`BURST_UTIL_CAP`, or today's peak if higher.

    Exposure only falls as the floor rises, so the boundary is found by bisection
    -- a handful of replays rather than one per candidate.

    Returns (floor, at floor, at current), or None when nothing lower qualifies.
    """
    # Judged against what runs today. Passed in when today's request differs
    # from the one being tried -- an option that also cuts requests.
    base = base or run(current_min)
    budget = base.over_request_minutes + allowance_minutes
    peak_cap = max(base.max_util, BURST_UTIL_CAP)
    # Memory above request is eviction exposure rather than latency: the same
    # minutes allowance, but the peak may not rise past request or today's.
    mem_budget = base.mem_over_request_minutes + allowance_minutes
    mem_cap = max(base.mem_max_util, MEM_UTIL_CAP)
    lo, hi = MIN_REPLICA_FLOOR, current_min - 1
    if hi < lo:
        return None
    best: tuple[int, ReplayResult] | None = None
    cache: dict[int, ReplayResult] = {}
    while lo <= hi:
        mid = (lo + hi) // 2
        res = cache.setdefault(mid, run(mid))
        if (res.over_request_minutes <= budget and res.max_util <= peak_cap
                and res.mem_over_request_minutes <= mem_budget and res.mem_max_util <= mem_cap):
            best = (mid, res)
            hi = mid - 1
        else:
            lo = mid + 1
    if best is None:
        return None
    return best[0], best[1], base
