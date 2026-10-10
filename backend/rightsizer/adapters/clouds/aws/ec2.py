"""EC2 instance catalogue and pricing.

Prices come from the AWS Pricing API (``get-products`` -- read-only, allowlisted)
and are cached on disk, because the catalogue changes rarely and the API is slow.

Two deliberate choices about money:

* An **enterprise discount factor** is applied to list price. Reporting list price
  overstates every saving by whatever your EDP is.
* Savings Plan coverage is **not** modelled. A node-hour freed on committed
  capacity is not cash unless other uncovered usage absorbs the commitment, so
  every dollar figure here is reported as a **ceiling** and labelled as one.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from rightsizer.adapters.clouds.base import HOURS_PER_MONTH, InstanceType  # noqa: F401 - re-exported
from rightsizer.sources.shell import run_json

log = logging.getLogger(__name__)

CACHE = Path.home() / ".cache" / "rightsizer" / "instances.json"


def _load_cache() -> dict[str, dict]:
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def _save_cache(data: dict[str, dict]) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(data, indent=2, sort_keys=True))


def describe_instance_types(
    names: list[str], *, profile: str | None = None, region: str = "us-east-1"
) -> dict[str, InstanceType]:
    """vCPU and memory for each type, cached. One API call for all of them."""
    cache = _load_cache()
    # A cached entry may hold only a price (from fetch_on_demand_price) and no
    # specs, so "in cache" is not the same as "described".
    missing = [
        n for n in names
        if any(k not in cache.get(n, {}) for k in ("vcpu", "storage_gb", "net_baseline_gbps"))
        and not cache.get(n, {}).get("absent")
    ]

    def _describe(batch: list[str]) -> str | None:
        """Describe a batch; return the error text instead of raising."""
        argv = ["aws"]
        if profile:
            argv += ["--profile", profile]
        argv += ["--region", region, "ec2", "describe-instance-types",
                 "--instance-types", *batch, "--output", "json"]
        try:
            payload = run_json(argv, timeout=90)
        except Exception as exc:  # noqa: BLE001 - pricing is optional
            return str(exc)
        for item in payload.get("InstanceTypes", []):  # type: ignore[union-attr]
            store = item.get("InstanceStorageInfo") or {}
            disks = store.get("Disks") or []
            net = item.get("NetworkInfo") or {}
            # Multi-card types (the 32xlarges) add up; one card is the rest.
            cards = net.get("NetworkCards") or []
            cache.setdefault(item["InstanceType"], {}).update(
                vcpu=item["VCpuInfo"]["DefaultVCpus"],
                memory_gib=item["MemoryInfo"]["SizeInMiB"] / 1024,
                storage_gb=float(store.get("TotalSizeInGB", 0) or 0),
                storage_disks=sum(int(d.get("Count", 0)) for d in disks),
                storage_type=(disks[0].get("Type", "") if disks else ""),
                storage_nvme=store.get("NvmeSupport", ""),
                net_baseline_gbps=float(sum(c.get("BaselineBandwidthInGbps", 0) or 0 for c in cards)),
                net_peak_gbps=float(sum(c.get("PeakBandwidthInGbps", 0) or 0 for c in cards)),
                net_performance=net.get("NetworkPerformance", ""),
            )
        return None

    if missing:
        # Size names are generated speculatively (m6id.large ... m6id.16xlarge) and
        # not every family offers every size. EC2 rejects the whole call when any
        # one name is invalid, so drop the names it objects to and retry once
        # rather than losing the sizes that do exist.
        error = _describe(missing)
        if error and "do not exist" in error:
            bad = {
                n for n in missing
                if n in error.split("do not exist")[-1]
            }
            remaining = [n for n in missing if n not in bad]
            log.info("skipping instance types that do not exist: %s", ", ".join(sorted(bad)))
            for n in bad:
                cache.setdefault(n, {})["absent"] = True
            error = _describe(remaining) if remaining else None
        if error:
            log.warning("instance specs unavailable: %s", error)
        _save_cache(cache)

    return {
        n: InstanceType(
            n, c["vcpu"], c["memory_gib"], c.get("on_demand_hourly", 0.0),
            storage_gb=c.get("storage_gb", 0.0), storage_disks=c.get("storage_disks", 0),
            storage_type=c.get("storage_type", ""), storage_nvme=c.get("storage_nvme", ""),
            storage_known="storage_gb" in c,
            net_baseline_gbps=c.get("net_baseline_gbps", 0.0),
            net_peak_gbps=c.get("net_peak_gbps", 0.0),
            net_performance=c.get("net_performance", ""),
        )
        for n in names
        if (c := cache.get(n)) and "vcpu" in c
    }


def fetch_on_demand_price(
    instance_type: str, billing_region: str, *, profile: str | None = None
) -> float:
    """List on-demand $/hour. The Pricing API lives only in us-east-1."""
    cache = _load_cache()
    entry = cache.get(instance_type, {})
    key = f"price_{billing_region}"
    if entry.get(key):
        return float(entry[key])

    argv = ["aws"]
    if profile:
        argv += ["--profile", profile]
    argv += [
        "--region", "us-east-1", "pricing", "get-products",
        "--service-code", "AmazonEC2",
        "--filters",
        f"Type=TERM_MATCH,Field=instanceType,Value={instance_type}",
        f"Type=TERM_MATCH,Field=regionCode,Value={billing_region}",
        "Type=TERM_MATCH,Field=operatingSystem,Value=Linux",
        "Type=TERM_MATCH,Field=tenancy,Value=Shared",
        "Type=TERM_MATCH,Field=preInstalledSw,Value=NA",
        "Type=TERM_MATCH,Field=capacitystatus,Value=Used",
        "--max-items", "1", "--output", "json",
    ]
    try:
        payload = run_json(argv, timeout=90)
        products = payload.get("PriceList", [])  # type: ignore[union-attr]
        if not products:
            return 0.0
        product = json.loads(products[0]) if isinstance(products[0], str) else products[0]
        for term in product["terms"]["OnDemand"].values():
            for dim in term["priceDimensions"].values():
                price = float(dim["pricePerUnit"]["USD"])
                cache.setdefault(instance_type, {})[key] = price
                _save_cache(cache)
                return price
    except Exception as exc:  # noqa: BLE001
        log.warning("price unavailable for %s: %s", instance_type, exc)
    return 0.0


# Families worth considering as alternatives, by the resource that binds.
# Compute-optimised has ~2 GiB per vCPU, general ~4, memory-optimised ~8, so a
# memory-bound group on a general family is paying for CPU it cannot use, and
# vice versa.
_ALTERNATIVE_FAMILIES = ("m", "r", "c")


def candidate_ladder(
    current_type: str, *, profile: str | None = None, region: str = "us-east-1"
) -> list[InstanceType]:
    """Every instance worth simulating for a node group, across families.

    Restricted to the same generation and the same storage suffix as what is
    running: ``m6id`` yields ``m6id``/``r6id``/``c6id``, never ``m6i``. The ``d``
    means local NVMe, and a group whose pods use it would lose their disk on a
    family change that looks free on paper.

    Searching only within one family is what makes a memory-bound group look
    like it has no better option: it can only be offered more of the same ratio.
    """
    prefix, _, _size = current_type.partition(".")
    if not prefix:
        return []
    # e.g. "m6id" -> letter "m", rest "6id"
    letter, rest = prefix[0], prefix[1:]
    if letter not in _ALTERNATIVE_FAMILIES or not rest:
        return family_ladder(prefix, profile=profile, region=region)

    ladder: list[InstanceType] = []
    for alt in _ALTERNATIVE_FAMILIES:
        ladder.extend(family_ladder(f"{alt}{rest}", profile=profile, region=region))
    # Keep the current type even if its family somehow resolved empty.
    if not any(i.name == current_type for i in ladder):
        ladder.extend(family_ladder(prefix, profile=profile, region=region))
    return sorted({i.name: i for i in ladder}.values(), key=lambda i: (i.vcpu, i.memory_gib))


def family_ladder(
    family: str, *, profile: str | None = None, region: str = "us-east-1"
) -> list[InstanceType]:
    """Same-family sizes, smallest first.

    Restricted to one family on purpose: changing family changes CPU architecture,
    local NVMe, and network characteristics, which is an application decision
    rather than a right-sizing one.
    """
    sizes = ["large", "xlarge", "2xlarge", "4xlarge", "8xlarge", "12xlarge", "16xlarge"]
    names = [f"{family}.{s}" for s in sizes]
    catalogue = describe_instance_types(names, profile=profile, region=region)
    return sorted(catalogue.values(), key=lambda i: i.vcpu)
