"""Find a cluster's local credentials by its name.

Each cluster lives in its own directory -- ``~/eks/prod/prod-us1/`` -- holding
a kubeconfig and an ``.envrc``::

    export KUBECONFIG="$PWD/kubeconfig_prod-us1.yaml"
    export AWS_PROFILE=main
    export AWS_REGION=us-east-1

and the kubeconfig's context is the EKS ARN, which carries the account and
cluster name. Everything ``collect`` needs is therefore already on disk, keyed
by the name passed as ``--env``; this module reads it so that nobody has to
copy exports out of a file before every run.

The ``.envrc`` is **parsed, never executed**: only ``export NAME=value`` lines
are read, with ``$PWD`` and ``~`` expanded. A shell file is code, and this tool
runs nothing it has not allowlisted.

Roots searched, in order: ``$RIGHTSIZER_CLUSTERS`` (colon-separated), else
``~/eks/prod`` and ``~/eks/stage``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOTS = ("~/eks/prod", "~/eks/stage")
ROOTS_VAR = "RIGHTSIZER_CLUSTERS"

_EXPORT = re.compile(r"""^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=(?:"([^"]*)"|'([^']*)'|(\S*))\s*(?:#.*)?$""")
# Any partition: arn:aws (commercial), arn:aws-us-gov (GovCloud), arn:aws-cn.
_ARN = re.compile(
    r"arn:aws[\w-]*:eks:(?P<region>[a-z0-9-]+):(?P<account>\d{12}):cluster/(?P<name>[\w.-]+)"
)
_CURRENT = re.compile(r"^current-context:\s*['\"]?([^'\"\s]+)", re.M)
_CONTEXT_NAME = re.compile(r"^\s*name:\s*['\"]?(arn:aws[\w-]*:eks:[^'\"\s]+)", re.M)


@dataclass(frozen=True)
class LocalCluster:
    env: str
    directory: Path
    kubeconfig: Path | None
    context: str | None
    cluster: str | None
    profile: str | None
    region: str | None
    account: str | None

    def describe(self) -> str:
        bits = [f"context {self.context}" if self.context else "no kubeconfig context",
                f"profile {self.profile}" if self.profile else None,
                f"region {self.region}" if self.region else None,
                f"account {self.account}" if self.account else None]
        return f"{self.directory}: " + ", ".join(b for b in bits if b)


def roots() -> list[Path]:
    raw = os.environ.get(ROOTS_VAR)
    parts = raw.split(":") if raw else list(DEFAULT_ROOTS)
    return [Path(p).expanduser() for p in parts if p]


def parse_envrc(path: Path) -> dict[str, str]:
    """``export NAME=value`` lines only, with $PWD and ~ expanded. Never executed."""
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        m = _EXPORT.match(line)
        if not m:
            continue
        value = next(v for v in m.groups()[1:] if v is not None)
        value = value.replace("${PWD}", str(path.parent)).replace("$PWD", str(path.parent))
        out[m.group(1)] = str(Path(value).expanduser()) if value.startswith("~") else value
    return out


def kube_context(kubeconfig: Path) -> str | None:
    """The kubeconfig's current context, or its first EKS context if none is set."""
    try:
        text = kubeconfig.read_text()
    except OSError:
        return None
    m = _CURRENT.search(text) or _CONTEXT_NAME.search(text)
    return m.group(1) if m else None


def find(env: str) -> LocalCluster | None:
    """The directory named ``env`` under the first root that has it."""
    for root in roots():
        directory = root / env
        if directory.is_dir():
            return load(env, directory)
    return None


def load(env: str, directory: Path) -> LocalCluster:
    envrc = directory / ".envrc"
    exported = parse_envrc(envrc) if envrc.is_file() else {}

    kubeconfig = Path(exported["KUBECONFIG"]) if exported.get("KUBECONFIG") else None
    if kubeconfig is None or not kubeconfig.is_file():
        # No .envrc, or it points elsewhere: the conventional file name.
        guess = directory / f"kubeconfig_{env}.yaml"
        kubeconfig = guess if guess.is_file() else None

    context = kube_context(kubeconfig) if kubeconfig else None
    arn = _ARN.search(context or "")
    return LocalCluster(
        env=env,
        directory=directory,
        kubeconfig=kubeconfig,
        context=context,
        cluster=arn["name"] if arn else None,
        profile=exported.get("AWS_PROFILE"),
        region=exported.get("AWS_REGION") or exported.get("AWS_DEFAULT_REGION")
        or (arn["region"] if arn else None),
        account=arn["account"] if arn else None,
    )


def discover() -> list[LocalCluster]:
    """Every cluster directory under the roots, first root winning on a clash."""
    seen: dict[str, LocalCluster] = {}
    for root in roots():
        if not root.is_dir():
            continue
        for directory in sorted(p for p in root.iterdir() if p.is_dir()):
            if directory.name not in seen and (
                (directory / ".envrc").is_file() or any(directory.glob("kubeconfig_*.yaml"))
            ):
                seen[directory.name] = load(directory.name, directory)
    return list(seen.values())
