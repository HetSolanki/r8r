"""Move a checkout from the flat layout to one directory per cluster.

    python3 scripts/migrate_layout.py --dry-run   # say what would move
    python3 scripts/migrate_layout.py

Collection output::

    out/snapshot-<env>[-<date>].json  ->  out/<env>/snapshot[-<date>].json
    out/report-<env>[-<date>].<ext>   ->  out/<env>/report[-<date>].<ext>

Console data::

    public/fleet.json                 ->  public/data/<env>/current.json, backtest-<date>.json
    public/hpa/<env>/                 ->  public/data/<env>/hpa/
    public/trends/<env>/              ->  public/data/<env>/trends/
                                          public/data/index.json, rebuilt from the above

Nothing is deleted. Every file is copied into the new layout first; only once
that has succeeded are the originals moved aside, to ``out/_legacy/`` and to
``frontend/_legacy-data/`` (outside ``public/``, so a deploy does not ship
them). A file already in the new layout is never overwritten. Run it twice and
the second run finds nothing to do.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
from pathlib import Path

from rightsizer.core import layout


def _aside(src: Path, legacy: Path, dry: bool) -> None:
    """Move an original out of the way, never over an earlier one."""
    dest = legacy / src.name
    n = 1
    while dest.exists():
        dest = legacy / f"{src.name}.{n}"
        n += 1
    print(f"  aside  {src} -> {dest}")
    if not dry:
        legacy.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))


def _copy(src: Path, dest: Path, dry: bool) -> bool:
    if dest.exists():
        print(f"  keep   {dest} (already there; {src.name} not copied)")
        return False
    print(f"  copy   {src} -> {dest}")
    if not dry:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    return True


def migrate_out(out: Path, dry: bool) -> int:
    print(f"{out}:")
    legacy = out / "_legacy"
    moved = 0
    for f in sorted(out.glob("*.json")) + sorted(out.glob("*.md")):
        if m := layout.LEGACY_SNAPSHOT_RE.match(f.name):
            target = layout.snapshot_path(out, m["env"], m["end"])
        elif m := layout.LEGACY_REPORT_RE.match(f.name):
            target = layout.report_path(out, m["env"], m["end"], m["ext"])
        elif f.name.startswith(("snapshot-", "report-")):
            # A hand-made variant (snapshot-x.5m.json): nothing reads it, so it
            # is kept aside rather than given a name it was never meant to have.
            _aside(f, legacy, dry)
            moved += 1
            continue
        else:
            continue
        _copy(f, target, dry)
        _aside(f, legacy, dry)
        moved += 1
    return moved


def migrate_public(public: Path, dry: bool) -> int:
    print(f"{public}:")
    data = public / "data"
    legacy = public.parent / "_legacy-data"
    moved = 0
    fleet_path = public / "fleet.json"
    envs: set[str] = set()
    uncollected: list[str] = []
    if fleet_path.exists():
        fleet = json.loads(fleet_path.read_text())
        exported = dt.datetime.fromtimestamp(fleet_path.stat().st_mtime, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        uncollected = fleet.get("uncollected") or []
        for period in fleet["periods"]:
            for cluster in period["clusters"]:
                env = layout.check_env(cluster["env"])
                envs.add(env)
                target = layout.period_file(data, env, period.get("end"))
                if target.exists():
                    print(f"  keep   {target} (already there)")
                    continue
                print(f"  split  {fleet_path.name} [{period['id']}] {env} -> {target}")
                if not dry:
                    layout.write_period_file(data, env, period, cluster, schema=fleet.get("schema") or 0,
                                             discount=fleet.get("discount"), exported_at=exported)
    for kind in ("hpa", "trends"):
        root = public / kind
        if not root.is_dir():
            continue
        for env_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            target = layout.env_data(data, env_dir.name) / kind
            files = sorted(env_dir.glob("*.json"))
            fresh = [f for f in files if not (target / f.name).exists()]
            print(f"  copy   {env_dir} -> {target} ({len(fresh)} of {len(files)} file(s))")
            if not dry:
                target.mkdir(parents=True, exist_ok=True)
                for f in fresh:
                    shutil.copy2(f, target / f.name)

    if not dry and (fleet_path.exists() or data.is_dir()):
        index = layout.write_index(data, known=[*envs, *uncollected])
        n = sum(len(p["clusters"]) for p in index["periods"])
        print(f"  index  {layout.index_path(data)}: {len(index['periods'])} period(s), {n} cluster file(s)")
        if index["unreadable"]:
            print(f"  error  {len(index['unreadable'])} file(s) unreadable; originals left in place",
                  file=sys.stderr)
            return moved
    # Only now, with the new layout written and indexed, are the originals moved.
    for src in (fleet_path, public / "hpa", public / "trends"):
        if src.exists():
            _aside(src, legacy, dry)
            moved += 1
    return moved


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=layout.DEFAULT_OUT)
    ap.add_argument("--public", type=Path, default=layout.DEFAULT_DATA.parent,
                    help="the console's public directory (default: ../frontend/public)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    args = ap.parse_args(argv)
    n = migrate_out(args.out, args.dry_run) if args.out.is_dir() else 0
    n += migrate_public(args.public, args.dry_run) if args.public.is_dir() else 0
    print(("would move" if args.dry_run else "moved") + f" {n} original(s) aside" if n else "nothing to migrate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
