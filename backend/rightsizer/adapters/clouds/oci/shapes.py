"""OCI shapes as rightsizer's instance types.

A fixed shape (``VM.GPU.A10.1``) is one size, like an EC2 instance type. A Flex
shape (``VM.Standard.E5.Flex``) is every size: OCPUs and memory are chosen per
instance, so the shape name alone does not say how big a node is. An instance
type here is therefore the shape *and* its size::

    VM.Standard.E5.Flex:4ocpu-32gb          a 4 OCPU / 32 GB E5 Flex instance
    VM.Standard.E5.Flex:4ocpu-32gb:preemptible
    VM.GPU.A10.1                            a fixed shape
    VM.GPU.A10.1:preemptible

``:preemptible`` is OCI's spot capacity: the same machine at half the price
(https://blogs.oracle.com/cloud-infrastructure/announcing-ga-of-oracle-preemptible-instances-vm-shapes).

One OCPU is two vCPUs on x86 and on A2/A4, one on A1; Kubernetes counts
vCPUs, so a node's ``cpu`` capacity is ``ocpus * vcpu_per_ocpu``.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

PREEMPTIBLE = "preemptible"
_FLEX_SIZE = re.compile(r"^(?P<ocpus>\d+(?:\.\d+)?)ocpu-(?P<gb>\d+(?:\.\d+)?)gb$")
#: Karpenter for OCI's own instance-type name, as it labels its nodes
#: (``node.kubernetes.io/instance-type``): ``VM.Standard.E5.Flex.4o.32g.1_1b``,
#: the last part the burstable baseline (1_1b = full OCPUs).
_KPO_NAME = re.compile(r"^(?P<shape>.+\.Flex)\.(?P<ocpus>\d+)o\.(?P<gb>\d+)g\.(?P<baseline>1_[128]b)$")


@dataclass(frozen=True)
class Shape:
    name: str
    #: The price-list series: "E5" prices as Compute - Standard - E5 - OCPU/Memory.
    series: str
    vcpu_per_ocpu: int = 2
    flex: bool = True
    #: Fixed shapes only.
    ocpus: float = 0.0
    memory_gb: float = 0.0
    gpus: int = 0
    #: Flex limits, per instance (OCI's compute shapes reference).
    max_ocpus: int = 64
    min_gb_per_ocpu: int = 1
    max_gb_per_ocpu: int = 64
    max_gb: int = 1024


SHAPES: dict[str, Shape] = {s.name: s for s in (
    Shape("VM.Standard.E3.Flex", "E3", max_ocpus=64),
    Shape("VM.Standard.E4.Flex", "E4", max_ocpus=114),
    Shape("VM.Standard.E5.Flex", "E5", max_ocpus=126),
    Shape("VM.Standard.E6.Flex", "E6", max_ocpus=126),
    Shape("VM.Standard3.Flex", "X9", max_ocpus=32, max_gb=512),
    Shape("VM.Standard.A1.Flex", "A1", vcpu_per_ocpu=1, max_ocpus=80, max_gb=512),
    Shape("VM.Standard.A2.Flex", "A2", max_ocpus=78),
    # GPU shapes are priced per GPU, which covers the whole machine.
    Shape("VM.GPU.A10.1", "A10", flex=False, ocpus=15, memory_gb=240, gpus=1),
    Shape("VM.GPU.A10.2", "A10", flex=False, ocpus=30, memory_gb=480, gpus=2),
)}


@dataclass(frozen=True)
class OciType:
    shape: str
    ocpus: float
    memory_gb: float
    preemptible: bool = False

    @property
    def name(self) -> str:
        base = self.shape
        if SHAPES.get(self.shape, Shape(self.shape, "")).flex:
            base = f"{self.shape}:{_num(self.ocpus)}ocpu-{_num(self.memory_gb)}gb"
        return f"{base}:{PREEMPTIBLE}" if self.preemptible else base

    @property
    def vcpu(self) -> int:
        return int(self.ocpus * SHAPES[self.shape].vcpu_per_ocpu) if self.shape in SHAPES else 0


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(x)


def parse(name: str) -> OciType | None:
    """An instance-type name back into shape and size; None if it is not one.

    A Flex shape with no size (the bare node label) cannot be priced, so it does
    not parse.
    """
    parts = name.split(":")
    preemptible = parts[-1] == PREEMPTIBLE
    if preemptible:
        parts = parts[:-1]
    if len(parts) == 1 and (m := _KPO_NAME.match(parts[0])) and m["shape"] in SHAPES:
        return OciType(m["shape"], float(m["ocpus"]), float(m["gb"]), preemptible)
    shape = SHAPES.get(parts[0])
    if shape is None:
        return None
    if not shape.flex:
        return OciType(shape.name, shape.ocpus, shape.memory_gb, preemptible) if len(parts) == 1 else None
    m = _FLEX_SIZE.match(parts[1]) if len(parts) == 2 else None
    if not m:
        return None
    return OciType(shape.name, float(m["ocpus"]), float(m["gb"]), preemptible)


def kpo_name(t: OciType) -> str:
    """The name Karpenter for OCI gives this size, as on its nodes' labels."""
    s = SHAPES.get(t.shape)
    if s is not None and not s.flex:
        return t.shape
    return f"{t.shape}.{_num(t.ocpus)}o.{_num(t.memory_gb)}g.1_1b"


def type_name(shape: str, ocpus: float | None = None, memory_gb: float | None = None,
              preemptible: bool = False) -> str:
    known = SHAPES.get(shape)
    if known and not known.flex:
        return OciType(shape, known.ocpus, known.memory_gb, preemptible).name
    return OciType(shape, float(ocpus or 0), float(memory_gb or 0), preemptible).name


#: The sizes offered when looking for a better one: OCPUs, by memory per OCPU.
#: A Flex shape sells any ratio from 1 to 64 GB per OCPU, which is the lever a
#: fixed instance family lacks: 8 GB/OCPU is a common general-purpose ratio, 2 and 4
#: suit CPU-bound groups, 16 and 32 memory-bound ones. Karpenter launches only
#: the sizes a pool's NodeClass lists, so a size from here that pays is a
#: shapeConfigs change, and the advice says so.
LADDER_OCPUS = (1, 2, 4, 8, 12, 16, 24, 32)
LADDER_GB_PER_OCPU = (2, 4, 8, 16, 32)


def ladder(shape: str, preemptible: bool = False) -> list[OciType]:
    s = SHAPES.get(shape)
    if s is None:
        return []
    if not s.flex:
        return [OciType(s.name, s.ocpus, s.memory_gb, preemptible)]
    return [OciType(s.name, o, o * r, preemptible)
            for o in LADDER_OCPUS if o <= s.max_ocpus
            for r in LADDER_GB_PER_OCPU
            if s.min_gb_per_ocpu <= r <= s.max_gb_per_ocpu and o * r <= s.max_gb]


# -- what a node of a size has left for pods ------------------------------------
# Karpenter for OCI's own model (pkg/providers/instancetype: vm_memory_overhead.go,
# kubeReservedResources, systemReservedResources), which is what it packs and
# prices with. Reproduced rather than estimated from one running node, because
# the kubelet's reservation grows with the node: a 16 GB node keeps 2.6 GiB
# back, a 128 GB one 9.3 GiB, and a ladder judged by the small node's overhead
# would overstate every large one.
VM_OVERHEAD_BASE_MIB = 600
VM_OVERHEAD_PER_GB_MIB = 19
SYSTEM_RESERVED_CPU_M = 100
SYSTEM_RESERVED_MEM_MIB = 100


def kube_reserved(vcpu: float, gb: float) -> tuple[float, float]:
    """(millicores, GiB) the kubelet reserves on an OKE node of this size."""
    steps = {1: 60.0, 2: 72.0, 3: 80.0, 4: 85.0, 5: 90.0}
    cpu_m = steps.get(int(vcpu)) if float(vcpu).is_integer() and vcpu <= 5 else 90.0 + (vcpu - 5) * 2.5
    if gb <= 4:
        mem = 0.25 * gb
    elif gb <= 8:
        mem = 0.20 * (gb - 4) + 1
    elif gb <= 16:
        mem = 0.10 * (gb - 8) + 1.8
    elif gb <= 128:
        mem = 0.06 * (gb - 16) + 2.6
    else:
        mem = 0.02 * (gb - 128) + 9.32
    return cpu_m, mem


def allocatable(t: OciType) -> tuple[float, float]:
    """(vCPU, GiB) a node of this size offers pods, before DaemonSets.

    The hypervisor keeps 600 MiB + 19 MiB/GB of the marketed memory (bare
    metal keeps none); the kubelet reserves :func:`kube_reserved` and the
    system 100m / 100 MiB on top.
    """
    s = SHAPES.get(t.shape)
    vcpu, gb = t.vcpu, t.memory_gb
    cap_mib = math.floor(gb * 1024)
    if not t.shape.startswith("BM."):
        cap_mib -= math.ceil(VM_OVERHEAD_BASE_MIB + VM_OVERHEAD_PER_GB_MIB * gb)
    cpu_m, mem_gib = kube_reserved(vcpu, gb)
    alloc_cpu = vcpu - (math.ceil(cpu_m) + SYSTEM_RESERVED_CPU_M) / 1000
    alloc_mem = (cap_mib - math.ceil(mem_gib * 1024) - SYSTEM_RESERVED_MEM_MIB) / 1024
    del s
    return max(alloc_cpu, 0.0), max(alloc_mem, 0.0)


def nearest(shape: str, vcpus: float, memory_gib: float, sizes: list[OciType]) -> OciType | None:
    """The configured size a node of this capacity was launched as.

    A node reports a little less memory than it was sold with (the kernel keeps
    some), so the match is the nearest size, relative on both axes, not an exact
    one. The sizes a pool offers double from one to the next, so a few percent
    either way cannot pick the wrong one.
    """
    pool = [t for t in sizes if t.shape == shape]
    if not pool:
        return None
    return min(pool, key=lambda t: (abs(t.vcpu - vcpus) / max(t.vcpu, 1)
                                    + abs(t.memory_gb - memory_gib) / max(t.memory_gb, 1)))
