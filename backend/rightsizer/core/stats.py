"""Statistics and the request-recommendation arithmetic.

CPU and memory are treated asymmetrically on purpose:

* **CPU is compressible.** Overshooting a request costs latency (CFS throttling),
  not availability, so a percentile with headroom is the right target.
* **Memory is not.** Overshooting is an OOMKill, so the 30-day peak is a floor
  rather than a target, and request == limit.
"""

from __future__ import annotations

import math

from rightsizer.core.config import Tunables
from rightsizer.core.models import UsageStat


def percentile(values: list[float], p: float) -> float:
    """Linear-interpolated percentile.

    Computed here rather than in PromQL because queries are chunked and
    percentiles do not compose across chunks.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * (p / 100.0)
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return ordered[int(k)]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def has_enough_data(stat: UsageStat, tun: Tunables) -> tuple[bool, str]:
    """Gate a recommendation on data sufficiency, per series.

    A workload that existed for 3 of 30 days still yields a confident-looking p95
    from 10% of the window. A global chunk-completeness check does not catch that;
    this does.
    """
    if stat.samples < tun.min_samples:
        return False, f"only {stat.samples} samples (need {tun.min_samples})"
    expected = tun.expected_samples()
    ratio = stat.samples / expected if expected else 0.0
    if ratio < tun.min_coverage:
        return False, f"series covers {ratio:.0%} of the window (need {tun.min_coverage:.0%})"
    return True, ""


def round_up(value: float, increment: float) -> float:
    if increment <= 0:
        return value
    return math.ceil(value / increment) * increment


def tail_adjusted(stat: UsageStat, tun: Tunables) -> float:
    """The sizing basis, lifted for a container whose tail is fat.

    A flat percentile describes a tight distribution well and a bursty one
    badly. This scales the lift by ``p99/p95`` and saturates it, so a moderate
    tail is covered and an extreme one is not reserved for -- see
    ``Tunables.tail_lift`` for why that asymmetry is the right way round.
    """
    basis = stat.at(tun.request_coverage)
    p95, p99 = stat.at("p95"), stat.at("p99")
    if tun.tail_lift <= 0 or p95 <= 0 or p99 <= p95:
        return basis
    ratio = p99 / p95
    if ratio <= tun.tail_trigger:
        return basis
    lifted = p95 * (1 + tun.tail_lift * (1 - tun.tail_trigger / ratio))
    # Never above the tail itself: covering more than p99 is what `max` does.
    return max(basis, min(p99, lifted))


def recommend_cpu(stat: UsageStat, tun: Tunables, hpa_target: float | None = None) -> float:
    """CPU request in cores.

    Sized from ``Tunables.request_coverage`` on the per-replica distribution --
    p95 by default, so 5% of samples sit above the request. That is safe while
    the container has no CPU limit and its node has room, and an exposure when
    either changes; raising the coverage is how you pay to remove it.

    :func:`tail_adjusted` then lifts the basis for a container whose p99 is far
    above its p95 -- the case a flat percentile describes worst -- by an amount
    that grows with the gap and saturates.

    Normally ``basis x headroom``. For a workload behind a CPU-utilization HPA that
    is *not* currently governing (its replica floor is), size instead to
    ``p95 / target``: that lands measured utilization on the HPA's own target, so
    the replica count stays where it is rather than the HPA reacting to the change.
    A flat headroom factor would either leave money on the table or push
    utilization past the target and trigger a scale-out.
    """
    basis = tail_adjusted(stat, tun)
    if hpa_target and 0 < hpa_target < 1:
        target = basis / hpa_target
    else:
        target = basis * tun.cpu_headroom
    millicores = round_up(target * 1000.0, tun.cpu_round_millicores)
    return max(millicores, tun.cpu_floor_millicores) / 1000.0


def recommend_memory(stat: UsageStat, tun: Tunables) -> float:
    """Memory request (== limit) in bytes: peak x headroom, bumped if OOMKilled."""
    headroom = tun.mem_headroom
    if stat.oom_killed:
        headroom = max(headroom, tun.mem_headroom * tun.mem_oom_headroom)
    mib = round_up(stat.peak * headroom / (1024 * 1024), tun.mem_round_mib)
    return max(mib, tun.mem_floor_mib) * 1024 * 1024


def fmt_cores(value: float) -> str:
    return f"{value * 1000:.0f}m" if value < 1 else f"{value:.2f}"


def fmt_bytes(value: float) -> str:
    mib = value / (1024 * 1024)
    return f"{mib / 1024:.2f}Gi" if mib >= 1024 else f"{mib:.0f}Mi"
