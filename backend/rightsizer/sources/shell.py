"""Read-only command execution.

The guarantee this module provides is structural, not documentary: a command that
is not on the allowlist raises before ``subprocess`` is ever reached. Reviewing
this one file is enough to know the tool cannot mutate a cluster or a cloud
account, no matter what the callers do.

Enforcement is deny-by-default in three layers:

1. the binary must be in ``READONLY_CLIS``: ``kubectl``, and each cloud
   adapter's CLI (``aws``);
2. the verb must be on that binary's read-only allowlist;
3. no argument may match a mutating or streaming flag.

Streaming flags (``--watch``) are refused as well: they are read-only but
unbounded, and this tool takes one lightweight snapshot rather than holding
connections open.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from collections.abc import Sequence

log = logging.getLogger(__name__)

KUBECTL_READONLY_VERBS = frozenset(
    {"get", "describe", "version", "api-resources", "api-versions", "config", "cluster-info"}
)
#: ``kubectl config`` also edits the kubeconfig (set-context, use-context,
#: delete-cluster ...); only these subcommands only read it.
KUBECTL_CONFIG_READONLY = frozenset({"view", "get-contexts", "current-context", "get-clusters",
                                     "get-users"})

# aws subcommands are allowed only when the operation name is clearly a read.
AWS_READONLY_PREFIXES = ("describe-", "list-", "get-", "lookup-")

# Flags that consume the next argument. Without this the value of `--context ctx`
# parses as the subcommand, and the verb check inspects the wrong token -- which
# would let a mutating verb through in the right position.
VALUE_FLAGS = frozenset(
    {
        "--context", "--kubeconfig", "--namespace", "-n", "--output", "-o",
        "--request-timeout", "--selector", "-l", "--field-selector", "--chunk-size",
        "--cluster", "--user", "--as", "--as-group", "--server", "--token",
        "--profile", "--region", "--endpoint-url", "--query", "--cli-read-timeout",
        "--cli-connect-timeout", "--page-size", "--max-items", "--starting-token",
        # oci
        "--compartment-id", "--cluster-id", "--node-pool-id", "--config-file", "--auth",
        "--limit", "--page", "--name", "--lifecycle-state", "--sort-by", "--sort-order",
    }
)

FORBIDDEN_ARGS = frozenset(
    {
        "-w", "--watch", "--watch-only",          # unbounded streaming
        "-f", "--filename", "--edit", "--patch",  # mutation vectors
        "--force", "--cascade", "--grace-period",
        "--record", "--save-config", "--overwrite",
    }
)


class ReadOnlyViolation(RuntimeError):
    """Raised when a command is not provably read-only. Never caught internally."""


def _check_kubectl(positional: list[str]) -> None:
    if positional[0] not in KUBECTL_READONLY_VERBS:
        raise ReadOnlyViolation(
            f"kubectl verb not allowed: {positional[0]!r} "
            f"(allowed: {', '.join(sorted(KUBECTL_READONLY_VERBS))})"
        )
    if positional[0] == "config" and (len(positional) < 2
                                      or positional[1] not in KUBECTL_CONFIG_READONLY):
        raise ReadOnlyViolation(
            f"kubectl config subcommand not allowed: {positional[1:2]!r} "
            f"(allowed: {', '.join(sorted(KUBECTL_CONFIG_READONLY))})")


def _check_aws(positional: list[str]) -> None:
    """aws <service> <operation>: the operation must be named as a read."""
    if len(positional) < 2:
        raise ReadOnlyViolation("aws: expected <service> <operation>")
    operation = positional[1]
    if not operation.startswith(AWS_READONLY_PREFIXES):
        raise ReadOnlyViolation(
            f"aws operation not allowed: {operation!r} "
            f"(must start with {', '.join(AWS_READONLY_PREFIXES)})"
        )


#: The oci commands this tool runs, word for word. Exact, not a rule about
#: the last word: an unrecognised flag's value is a positional too, so
#: "oci ce node-pool delete --unknown list" ends in "list" -- and is refused
#: here because it is not one of these.
OCI_READONLY_COMMANDS = frozenset({
    ("ce", "cluster", "get"),
    ("ce", "node-pool", "get"),
    ("ce", "node-pool", "list"),
    ("compute", "shape", "list"),
    ("compute", "instance", "get"),
    ("compute", "instance", "list"),
})


def _check_oci(positional: list[str]) -> None:
    if tuple(positional) not in OCI_READONLY_COMMANDS:
        raise ReadOnlyViolation(
            f"oci command not allowed: {' '.join(positional)!r} (allowed: "
            f"{', '.join(' '.join(c) for c in sorted(OCI_READONLY_COMMANDS))})")


#: Every binary this tool may run, and the check its subcommand must pass.
#: A cloud adapter's CLI is added here by hand -- with its own deny-by-default
#: rule and tests in tests/test_readonly.py -- never registered from elsewhere,
#: so this file stays the whole of the read-only guarantee.
READONLY_CLIS = {
    "kubectl": _check_kubectl,
    "aws": _check_aws,
    "oci": _check_oci,
}


def assert_read_only(argv: Sequence[str]) -> None:
    if not argv:
        raise ReadOnlyViolation("empty command")

    binary = argv[0].rsplit("/", 1)[-1]
    if binary not in READONLY_CLIS:
        raise ReadOnlyViolation(f"binary not allowed: {binary!r}")

    for arg in argv[1:]:
        flag = arg.split("=", 1)[0]
        if flag in FORBIDDEN_ARGS:
            raise ReadOnlyViolation(f"forbidden argument: {arg!r}")

    positional: list[str] = []
    skip_next = False
    for arg in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg.startswith("-"):
            # `--flag=value` carries its value inline; `--flag value` does not.
            if "=" not in arg and arg in VALUE_FLAGS:
                skip_next = True
            continue
        positional.append(arg)

    if not positional:
        raise ReadOnlyViolation(f"{binary}: no subcommand")
    READONLY_CLIS[binary](positional)


def run_json(argv: Sequence[str], *, timeout: int = 120) -> dict | list:
    """Run an allowlisted read-only command and parse its JSON output."""
    assert_read_only(argv)
    if shutil.which(argv[0]) is None:
        raise FileNotFoundError(f"{argv[0]} not found on PATH")

    log.debug("exec: %s", " ".join(argv))
    proc = subprocess.run(  # noqa: S603 - argv is allowlisted above, never shell
        list(argv),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(argv[:3])} failed ({proc.returncode}): {proc.stderr[:400]}")
    if not proc.stdout.strip():
        return {}
    return json.loads(proc.stdout)
