"""Where every file lives, in one place, so the CLI, the exporter, the migration
and the tests cannot disagree about a path.

Collection output, one directory per cluster::

    out/<env>/snapshot.json               the current window
    out/<env>/snapshot-<YYYY-MM-DD>.json  a backtest ending that day
    out/<env>/report.json, report.md      (and report-<YYYY-MM-DD>.*)

The console's data, one directory per cluster plus an index::

    <data>/index.json                     which clusters and periods exist
    <data>/<env>/current.json             one cluster, the current window
    <data>/<env>/backtest-<YYYY-MM-DD>.json
    <data>/<env>/hpa/*.json               per-HPA minute history
    <data>/<env>/trends/*.json            per-container usage trends

A cluster's files are written only when that cluster is exported, so exporting
one cluster -- or losing one file -- never takes the others with it. The index
holds nothing that is not in the cluster files: it is rebuilt from them on
every export, and :func:`build_index` rebuilds it alone.

The flat layout this replaced (``out/snapshot-<env>.json``) is still read, so a
checkout that has not been migrated keeps working; new files are written in
the layout above.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Bumped when the index or the cluster-file envelope changes shape. The
#: cluster payload inside has its own version, ``export_ui.SCHEMA_VERSION``.
LAYOUT_VERSION = 1

#: The checkout beside this package, when it is one (an editable install).
_REPO = Path(__file__).resolve().parents[2]
HOME = _REPO if (_REPO / "pyproject.toml").is_file() else Path.cwd()
DEFAULT_OUT = HOME / "out"
DEFAULT_DATA = HOME.parent / "frontend" / "public" / "data"

ENV_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_DATE = r"\d{4}-\d{2}-\d{2}"
#: out/<env>/snapshot.json or snapshot-<date>.json.
SNAPSHOT_RE = re.compile(rf"^snapshot(?:-(?P<end>{_DATE}))?\.json$")
#: The flat layout: out/snapshot-<env>.json or snapshot-<env>-<date>.json.
LEGACY_SNAPSHOT_RE = re.compile(rf"^snapshot-(?P<env>[A-Za-z0-9_-]+?)(?:-(?P<end>{_DATE}))?\.json$")
LEGACY_REPORT_RE = re.compile(rf"^report-(?P<env>[A-Za-z0-9_-]+?)(?:-(?P<end>{_DATE}))?\.(?P<ext>json|md)$")
#: <data>/<env>/current.json or backtest-<date>.json.
PERIOD_FILE_RE = re.compile(rf"^(?:current|backtest-(?P<end>{_DATE}))\.json$")


def check_env(env: str) -> str:
    """An env becomes a directory name, so it must be one."""
    if not ENV_RE.match(env or ""):
        raise ValueError(f"not a cluster name: {env!r}")
    return env


def _suffix(end: str | None) -> str:
    return f"-{end}" if end else ""


# -- collection output ---------------------------------------------------------

def env_out(out: Path, env: str) -> Path:
    return Path(out) / check_env(env)


def snapshot_path(out: Path, env: str, end: str | None = None) -> Path:
    """Where a snapshot is written."""
    return env_out(out, env) / f"snapshot{_suffix(end)}.json"


def legacy_snapshot_path(out: Path, env: str, end: str | None = None) -> Path:
    return Path(out) / f"snapshot-{check_env(env)}{_suffix(end)}.json"


def find_snapshot(out: Path, env: str, end: str | None = None) -> Path:
    """Where a snapshot is read from: the per-cluster file, else the flat one.

    Returns the per-cluster path when neither exists, so "not found" messages
    name the place a new snapshot would go.
    """
    new = snapshot_path(out, env, end)
    if new.exists():
        return new
    old = legacy_snapshot_path(out, env, end)
    return old if old.exists() else new


def report_path(out: Path, env: str, end: str | None, ext: str) -> Path:
    return env_out(out, env) / f"report{_suffix(end)}.{ext}"


def discover_snapshots(out: Path) -> tuple[dict[str | None, dict[str, Path]], list[str]]:
    """{period end (None = current): {env: snapshot path}}, plus files that did not match.

    Both layouts are read; where one cluster has both, the per-cluster file wins.
    """
    out = Path(out)
    found: dict[str | None, dict[str, Path]] = {}
    skipped: list[str] = []
    if not out.is_dir():
        return found, skipped
    for path in sorted(out.glob("snapshot-*.json")):
        m = LEGACY_SNAPSHOT_RE.match(path.name)
        if m:
            found.setdefault(m["end"], {})[m["env"]] = path
        else:
            skipped.append(path.name)
    for d in sorted(p for p in out.iterdir() if p.is_dir() and ENV_RE.match(p.name)):
        for path in sorted(d.glob("snapshot*.json")):
            m = SNAPSHOT_RE.match(path.name)
            if m:
                found.setdefault(m["end"], {})[d.name] = path
            else:
                skipped.append(f"{d.name}/{path.name}")
    return found, skipped


# -- console data --------------------------------------------------------------

def env_data(data: Path, env: str) -> Path:
    return Path(data) / check_env(env)


def period_id(end: str | None) -> str:
    return end or "current"


def period_file(data: Path, env: str, end: str | None) -> Path:
    return env_data(data, env) / ("current.json" if not end else f"backtest-{end}.json")


def hpa_dir(data: Path, env: str) -> Path:
    return env_data(data, env) / "hpa"


def trends_dir(data: Path, env: str) -> Path:
    return env_data(data, env) / "trends"


def index_path(data: Path) -> Path:
    return Path(data) / "index.json"


def write_atomic(path: Path, text: str) -> Path:
    """Write beside the target and rename over it.

    A reader -- the console, a sync to S3 -- sees the old file or the new one,
    never half of one, and an export killed mid-write leaves the old file whole.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def write_period_file(data: Path, env: str, period: dict, cluster: dict, *,
                      schema: int, discount: float, exported_at: str | None = None) -> Path:
    """One cluster for one period, with what the index needs to list it.

    ``period`` is ``{"id", "label", "end"}``. Everything the index says about
    this file is in the file, so the index can always be rebuilt from disk.
    """
    doc = {
        "layout": LAYOUT_VERSION,
        "schema": schema,
        "env": env,
        "period": {k: period.get(k) for k in ("id", "label", "end")},
        "discount": discount,
        "exportedAt": exported_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cluster": cluster,
    }
    return write_atomic(period_file(data, env, period.get("end")), json.dumps(doc))


def _period_label(end: str | None) -> str:
    return "Current" if end is None else f"Backtest · window ending {end}"


def build_index(data: Path, *, known: list[str] | tuple[str, ...] = (),
                labels: dict | None = None) -> dict:
    """The index, from the cluster files on disk -- never from memory.

    A file that cannot be read is listed under ``unreadable`` rather than
    dropped silently: the console says which cluster is missing and why.
    """
    data = Path(data)
    labels = labels or {}
    periods: dict[str | None, dict] = {}
    unreadable: list[dict] = []
    discounts: Counter[float] = Counter()
    envs: set[str] = set()
    dirs = sorted(p for p in data.iterdir() if p.is_dir() and ENV_RE.match(p.name)) if data.is_dir() else []
    for d in dirs:
        for f in sorted(d.glob("*.json")):
            m = PERIOD_FILE_RE.match(f.name)
            if not m:
                continue
            rel = f"{d.name}/{f.name}"
            try:
                doc = json.loads(f.read_text())
                cluster = doc["cluster"]
                if doc.get("env") != d.name or cluster.get("env") != d.name:
                    raise ValueError(f"file says env {doc.get('env')!r}, directory is {d.name!r}")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                unreadable.append({"env": d.name, "file": rel, "error": str(exc)[:200]})
                continue
            end = m["end"]
            envs.add(d.name)
            discounts[doc.get("discount")] += 1
            p = periods.setdefault(end, {
                "id": period_id(end),
                "label": labels.get(period_id(end)) or (doc.get("period") or {}).get("label")
                         or _period_label(end),
                "end": end,
                "clusters": [],
            })
            p["clusters"].append({
                "env": d.name,
                "file": rel,
                "schema": doc.get("schema"),
                "collectedAt": cluster.get("collectedAt"),
                "exportedAt": doc.get("exportedAt"),
                "currentRunRate": cluster.get("currentRunRate") or 0,
            })
    # Current first, then backtests newest first; largest spend first within
    # a period, because the console opens on the first cluster.
    ordered = [periods[None]] if None in periods else []
    ordered += [periods[e] for e in sorted((e for e in periods if e is not None), reverse=True)]
    for p in ordered:
        p["clusters"].sort(key=lambda c: (-c["currentRunRate"], c["env"]))
        p["collectedAt"] = max((c["collectedAt"] or "" for c in p["clusters"]), default=None)
    discount, _ = discounts.most_common(1)[0] if discounts else (None, 0)
    return {
        "layout": LAYOUT_VERSION,
        "generatedAt": ordered[0]["collectedAt"] if ordered else None,
        "indexedAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "discount": discount,
        # Exported at different discounts: the fleet total mixes them.
        "discountMixed": len(discounts) > 1,
        "periods": ordered,
        "uncollected": sorted(set(known) - envs),
        "unreadable": unreadable,
    }


def write_index(data: Path, **kw: Any) -> dict:
    index = build_index(data, **kw)
    write_atomic(index_path(data), json.dumps(index, indent=1))
    return index


def assemble_fleet(data: Path) -> dict | None:
    """The whole fleet as one object, in the shape the console builds in memory.

    For the tests and for anything that wants every cluster at once; None when
    there is no index.
    """
    data = Path(data)
    if not index_path(data).exists():
        return None
    index = json.loads(index_path(data).read_text())
    periods = []
    for p in index["periods"]:
        clusters = [json.loads((data / c["file"]).read_text())["cluster"] for c in p["clusters"]]
        periods.append({**{k: p[k] for k in ("id", "label", "end", "collectedAt")}, "clusters": clusters})
    return {
        "schema": min((c["schema"] or 0 for p in index["periods"] for c in p["clusters"]), default=None),
        "generatedAt": index["generatedAt"],
        "discount": index["discount"],
        "periods": periods,
        "uncollected": index["uncollected"],
    }
