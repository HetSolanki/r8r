"""Read-only Thanos client with concurrent, chunked range queries.

Two properties matter here.

**Speed.** A 30-day window at a 12-hour chunk size is 60 requests per query, and a
full collection issues a dozen queries -- several hundred round trips. Issued
serially at ~1.5s each that is 10+ minutes; issued through a bounded pool it is
under a minute. The pool is deliberately small: this is shared production
observability, not a load target.

**Honesty about gaps.** An empty chunk is indistinguishable from "no data" but
usually means the request was rejected or timed out, and a missing chunk silently
removes real peaks from the percentiles computed downstream. Every chunk result
records whether it returned data, and coverage travels with the series so a
recommendation can be suppressed rather than quietly computed from half a window.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

SeriesKey = tuple[str, ...]


@dataclass
class SeriesSet:
    """Values per label-tuple, plus how much of the window actually returned."""

    values: dict[SeriesKey, list[float]] = field(default_factory=dict)
    chunks_total: int = 0
    chunks_ok: int = 0

    @property
    def coverage(self) -> float:
        return (self.chunks_ok / self.chunks_total) if self.chunks_total else 0.0

    @property
    def complete(self) -> bool:
        return self.chunks_total > 0 and self.chunks_ok == self.chunks_total


class ThanosClient:
    def __init__(
        self,
        base_url: str,
        *,
        max_workers: int = 8,
        timeout: int = 180,
        retries: int = 3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_workers = max_workers
        self.timeout = timeout
        self._session = requests.Session()
        retry = Retry(
            total=retries,
            backoff_factor=0.6,
            status_forcelist=(429, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(
            max_retries=retry,
            # Several chunked queries run concurrently, each with its own worker
            # pool, so total in-flight requests exceed max_workers. Undersizing
            # here makes urllib3 discard and re-open connections under load.
            pool_connections=max_workers * 4,
            pool_maxsize=max_workers * 4,
        )
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    # -- low level ---------------------------------------------------------

    def _get(self, path: str, params: dict[str, object]) -> list[dict]:
        r = self._session.get(f"{self.base_url}{path}", params=params, timeout=self.timeout)
        if r.status_code != 200:
            log.warning("thanos %s -> HTTP %s: %s", path, r.status_code, r.text[:200])
            return []
        payload = r.json()
        if payload.get("status") != "success":
            log.warning("thanos %s -> %s", path, str(payload.get("error"))[:200])
            return []
        return payload["data"]["result"]

    def query_instant(self, query: str, at: int | None = None) -> list[dict]:
        """Evaluate at a point in time. ``at`` pins it to a past instant.

        Without this a historical replay would pull *today's* capacity, node
        counts and requests into a snapshot whose usage came from a past window
        -- mixing two different clusters' worth of state and quietly invalidating
        the comparison it exists to make.
        """
        params: dict[str, object] = {"query": query}
        if at:
            params["time"] = at
        return self._get("/api/v1/query", params)

    def query_range(self, query: str, start: int, end: int, step: int) -> list[dict]:
        return self._get(
            "/api/v1/query_range",
            {"query": query, "start": start, "end": end, "step": step},
        )

    # -- chunked + concurrent ---------------------------------------------

    def query_range_chunked(
        self,
        query: str,
        start: int,
        end: int,
        *,
        step: int,
        chunk_hours: int,
        label_keys: Sequence[str],
    ) -> SeriesSet:
        """Range-query in parallel chunks, keyed by ``label_keys``.

        Chunking keeps each request small enough that Thanos does not return 504
        on a 30-day window; the pool keeps the wall clock proportional to the
        slowest chunk rather than the sum of all of them.
        """
        chunk_seconds = chunk_hours * 3600
        windows = []
        cur = start
        while cur < end:
            nxt = min(cur + chunk_seconds, end)
            windows.append((cur, nxt))
            cur = nxt

        out = SeriesSet(chunks_total=len(windows))
        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {
                pool.submit(self.query_range, query, w0, w1, step): (w0, w1) for w0, w1 in windows
            }
            for fut in as_completed(futures):
                result = fut.result()
                if result:
                    out.chunks_ok += 1
                _absorb(result, label_keys, out.values)

        log.info(
            "chunked query: %d/%d chunks, %d series, %.1fs",
            out.chunks_ok,
            out.chunks_total,
            len(out.values),
            time.monotonic() - t0,
        )
        return out

    def query_many(
        self, queries: dict[str, str], at: int | None = None
    ) -> dict[str, list[dict]]:
        """Issue independent instant queries concurrently, all at the same instant."""
        out: dict[str, list[dict]] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(self.query_instant, qs, at): name for name, qs in queries.items()}
            for fut in as_completed(futures):
                out[futures[fut]] = fut.result()
        return out

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> ThanosClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _absorb(
    result: Iterable[dict],
    label_keys: Sequence[str],
    into: dict[SeriesKey, list[float]],
) -> None:
    for series in result:
        metric = series.get("metric", {})
        key = tuple(metric.get(k, "") for k in label_keys)
        bucket = into.setdefault(key, [])
        for _, raw in series.get("values", []):
            if raw in (None, "NaN"):
                continue
            value = float(raw)
            if math.isnan(value):
                continue
            bucket.append(value)


def instant_map(
    result: Iterable[dict], label_keys: Sequence[str]
) -> dict[SeriesKey, float]:
    """Collapse an instant-query result to {label tuple: value}."""
    out: dict[SeriesKey, float] = {}
    for series in result:
        metric = series.get("metric", {})
        key = tuple(metric.get(k, "") for k in label_keys)
        raw = series.get("value", [None, None])[1]
        if raw in (None, "NaN"):
            continue
        value = float(raw)
        if not math.isnan(value):
            out[key] = max(out.get(key, value), value)
    return out
