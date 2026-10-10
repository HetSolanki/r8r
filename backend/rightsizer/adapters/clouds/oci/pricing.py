"""OCI list prices, from Oracle's public price list.

https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/ -- no account,
no credentials, one GET for the whole catalogue. Prices are per part number:
a Flex series has one for OCPU-hours and one for GB-hours, a GPU shape one
per GPU-hour that covers the machine. OCI prices every region the same, so
the region never enters into it.

Cached on disk for a week; an unreachable price list falls back to the cache
however old, and with neither a price is 0 -- which the export reports as "no
instance prices resolved" rather than quietly calling a node free.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

PRICE_LIST_URL = "https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/"
CACHE = Path.home() / ".cache" / "rightsizer" / "oci-prices.json"
MAX_AGE = 7 * 86400

#: Part numbers per price-list series, as the list names them
#: ("Compute - Standard - E5 - OCPU" is B97384).
PARTS: dict[str, dict[str, str]] = {
    "E3": {"ocpu": "B92306", "memory": "B92307"},
    "E4": {"ocpu": "B93113", "memory": "B93114"},
    "E5": {"ocpu": "B97384", "memory": "B97385"},
    "E6": {"ocpu": "B111129", "memory": "B111130"},
    "X9": {"ocpu": "B94176", "memory": "B94177"},
    "A1": {"ocpu": "B93297", "memory": "B93298"},
    "A2": {"ocpu": "B109529", "memory": "B109530"},
    "A10": {"gpu": "B95909"},
}

#: Preemptible capacity is always half the on-demand rate.
PREEMPTIBLE_FACTOR = 0.5

_memo: dict[str, float] | None = None


def _paid_rate(item: dict, currency: str) -> float | None:
    """The rate a running instance pays: where a part has a free tier first,
    the tier above it."""
    for loc in item.get("currencyCodeLocalizations") or []:
        if loc.get("currencyCode") != currency:
            continue
        prices = [p for p in loc.get("prices") or [] if p.get("model") == "PAY_AS_YOU_GO"]
        if prices:
            return float(max(prices, key=lambda p: p.get("rangeMin") or 0)["value"])
    return None


def parse_price_list(payload: dict, currency: str = "USD") -> dict[str, float]:
    out = {}
    for item in payload.get("items") or []:
        rate = _paid_rate(item, currency)
        if item.get("partNumber") and rate is not None:
            out[item["partNumber"]] = rate
    return out


def _fetch(currency: str) -> dict[str, float]:
    import requests

    r = requests.get(PRICE_LIST_URL, params={"currencyCode": currency}, timeout=60)
    r.raise_for_status()
    return parse_price_list(r.json(), currency)


def part_prices(currency: str = "USD") -> dict[str, float]:
    """Every part's hourly rate: the cache when fresh, else the list, else the stale cache."""
    global _memo
    if _memo is not None:
        return _memo
    cached: dict = {}
    if CACHE.exists():
        try:
            cached = json.loads(CACHE.read_text())
        except ValueError:
            cached = {}
    fresh = cached.get("currency") == currency and time.time() - cached.get("fetched_at", 0) < MAX_AGE
    if not fresh:
        try:
            parts = _fetch(currency)
            cached = {"currency": currency, "fetched_at": time.time(), "parts": parts}
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(json.dumps(cached))
        except Exception as exc:  # noqa: BLE001 - prices degrade, never abort
            log.warning("OCI price list unavailable (%s); %s", exc,
                        "using the cached copy" if cached.get("parts") else "prices will be 0")
    _memo = dict(cached.get("parts") or {})
    return _memo


def hourly(series: str, *, ocpus: float = 0, memory_gb: float = 0, gpus: int = 0,
           preemptible: bool = False) -> float:
    """On-demand $/hour for one instance; 0 when a part has no price."""
    parts, prices = PARTS.get(series) or {}, part_prices()
    if "gpu" in parts:
        rate = gpus * prices.get(parts["gpu"], 0.0)
    else:
        ocpu, mem = prices.get(parts.get("ocpu", ""), 0.0), prices.get(parts.get("memory", ""), 0.0)
        rate = ocpus * ocpu + memory_gb * mem if ocpu else 0.0
    return rate * (PREEMPTIBLE_FACTOR if preemptible else 1.0)
