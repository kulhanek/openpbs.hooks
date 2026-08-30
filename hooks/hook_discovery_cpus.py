# coding: utf-8
"""
OpenPBS execution-host discovery hook for CPU and memory resources only.

Published vnode resources
-------------------------
* ncpus          : number of physical CPU cores (PBS-consumable CPU capacity)
* nthreads       : number of online logical CPUs/PUs (informational)
* smt            : True when nthreads > ncpus
* hybrid_cpu     : True when SMT is present and physical cores have unequal PU counts
* npus_per_core  : uniform PUs per physical core, or "1" without SMT/on hybrid CPUs
* mem            : usable physical memory after configured reserve
* vmem           : usable physical memory + configured system swap
* cpu_model      : CPU model name(s)
* cpu_vendor     : native CPU vendor, optionally translated through cpu_vendor_map
* cpu_arch       : normalized CPU architecture (for example x86_64 or aarch64)
* cpu_flag       : CPU flags/features common to all online logical CPUs
* cpu_isa        : highest supported ISA baseline detected for the CPU architecture
* cpu_spec       : relative speed of one CPU core, derived from cpu_model

CPU topology is derived from Linux sysfs, CPU metadata from /proc/cpuinfo,
and memory from /proc/meminfo.

Recommended events
------------------
    exechost_startup, exechost_periodic

Suggested custom PBS resources
------------------------------
    nthreads       : long
    smt            : boolean
    hybrid_cpu     : boolean
    npus_per_core  : string
    cpu_model      : string - homogeneous SMT nodes
    cpu_vendor     : string - homogeneous SMT nodes
    cpu_arch       : string_array
    cpu_flag       : string_array
    cpu_isa        : string_array
    cpu_spec       : float

The standard resources ncpus, mem, and vmem already exist in OpenPBS.
"""

import fnmatch
import glob
import json
import os
import re
import traceback

import pbs


DEFAULT_CONFIG = {
    "memory_reserve": "0B",
    "publish_vmem": True,
    "cpu_vendor_map": [],
    "default_cpu_spec": 0.0,
    "cpu_spec_map": [],
}

_SIZE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([kmgtpe]?)(?:i?b)?\s*$", re.I)


# Linux /proc/cpuinfo flag names corresponding to the incremental x86-64
# psABI microarchitecture feature levels.
#
# x86-64-v1 is the x86-64 baseline and is inferred from the machine
# architecture.  For v3, Linux exposes LZCNT as "abm".  The psABI OSXSAVE
# requirement is represented here by "xsave" together with "avx": Linux only
# exposes AVX to userspace when the required XSAVE/XCR0 state is enabled.
_X86_64_ISA_LEVEL_FLAGS = (
    (
        "x86-64-v2",
        frozenset(("cx16", "lahf_lm", "popcnt", "pni", "sse4_1", "sse4_2", "ssse3")),
    ),
    (
        "x86-64-v3",
        frozenset(("avx", "avx2", "bmi1", "bmi2", "f16c", "fma", "abm", "movbe", "xsave")),
    ),
    (
        "x86-64-v4",
        frozenset(("avx512f", "avx512bw", "avx512cd", "avx512dq", "avx512vl")),
    ),
)

# AArch64 ISA baselines that can be established conservatively from Linux
# /proc/cpuinfo Features (arm64 ELF HWCAP names).  Some Arm architecture
# revisions add no uniquely observable userspace feature, therefore this hook
# never guesses such a revision merely from its numeric ordering.
#
# The table below follows the cumulative default feature sets used by GCC for
# -march=armv8.*-a / armv9.*-a, translated to Linux feature names.  Armv9 is a
# branch from the Armv8.5-A baseline, so it is handled separately below.
_AARCH64_ISA_LEVEL_FLAGS = (
    (
        "armv8.1-a",
        frozenset(("crc32", "atomics", "asimdrdm")),
    ),
    # armv8.2-a has no additional default GCC feature bundle over armv8.1-a;
    # it cannot be established reliably from /proc/cpuinfo Features alone.
    (
        "armv8.3-a",
        frozenset(("paca", "pacg", "fcma", "jscvt")),
    ),
    (
        "armv8.4-a",
        frozenset(("flagm", "asimdfhm", "asimddp", "ilrcpc")),
    ),
    (
        "armv8.5-a",
        frozenset(("sb", "ssbs", "predres", "frint", "flagm2")),
    ),
    (
        "armv8.6-a",
        frozenset(("bf16", "i8mm")),
    ),
)



def detect_x86_64_isa(common_flags):
    """
    Return the highest supported x86-64 psABI microarchitecture feature level.

    The result is suitable for a PBS string_array resource containing exactly
    one value.  For example, a v3-capable node publishes:
        x86-64-v3

    The caller is responsible for dispatching this function only for x86-64.
    """
    flags = set(str(flag).strip().lower() for flag in common_flags if str(flag).strip())
    highest = "x86-64-v1"

    for level, required in _X86_64_ISA_LEVEL_FLAGS:
        if required.issubset(flags):
            highest = level
        else:
            # Levels are cumulative.  If one level is not supported, higher
            # levels must not be advertised even if their incremental flags
            # happen to be present.
            break

    # cpu_isa remains a PBS string_array resource, but only the highest
    # supported ISA level is published.
    return highest


def normalize_cpu_arch(machine):
    """Return a stable architecture token suitable for cpu_arch."""
    arch = str(machine or "").strip().lower()
    aliases = {
        "amd64": "x86_64",
        "x86-64": "x86_64",
        "arm64": "aarch64",
    }
    return aliases.get(arch, arch)


def detect_aarch64_isa(common_flags):
    """
    Return the highest AArch64 ISA baseline that can be established safely.

    Linux exposes AArch64 CPU capabilities as /proc/cpuinfo ``Features``.
    Those capabilities are sufficient to prove many Arm architectural levels,
    but not every revision.  In particular Armv8.2-A adds no distinct default
    feature bundle over Armv8.1-A, so the function deliberately reports the
    highest level whose required feature set is observable instead of guessing.

    Armv9-A derives from the Armv8.5-A baseline plus SVE/SVE2.  Armv9.1-A adds
    BF16 and I8MM, matching the Armv8.6-A additions.
    """
    flags = set(str(flag).strip().lower() for flag in common_flags if str(flag).strip())

    # AArch64 Linux implies an Armv8-A userspace baseline.
    highest_v8 = "armv8-a"
    for level, required in _AARCH64_ISA_LEVEL_FLAGS:
        if required.issubset(flags):
            highest_v8 = level
        else:
            break

    # Armv9-A is not simply the next Armv8 revision.  It branches from
    # Armv8.5-A and additionally requires SVE and SVE2.
    if highest_v8 in ("armv8.5-a", "armv8.6-a") and {"sve", "sve2"}.issubset(flags):
        if highest_v8 == "armv8.6-a":
            return "armv9.1-a"
        return "armv9-a"

    return highest_v8


def detect_cpu_isa(cpu_arch, common_flags):
    """Return the highest supported ISA baseline for the normalized architecture."""
    if cpu_arch == "x86_64":
        return detect_x86_64_isa(common_flags)
    if cpu_arch == "aarch64":
        return detect_aarch64_isa(common_flags)
    return ""


def map_cpu_identity(vendor, vendor_map, system_arch):
    """
    Map native CPU vendor and, optionally, architecture using cpu_vendor_map.

    Supported entry fields:
        pattern : shell-style wildcard pattern matched against the native vendor
        cs      : case-sensitive matching when true; default false
        alias   : value published in cpu_vendor when matched
        arch    : optional value published in cpu_arch when matched

    The first matching entry wins.  If no mapping supplies ``arch``, cpu_arch
    falls back to the normalized system architecture from uname(2).
    """
    native = str(vendor or "").strip()
    arch = normalize_cpu_arch(system_arch)
    if not native:
        return native, arch

    for entry in vendor_map or []:
        if not isinstance(entry, dict):
            continue

        pattern = str(entry.get("pattern", "")).strip()
        if not pattern:
            continue

        cs = bool(entry.get("cs", False))
        if cs:
            matched = fnmatch.fnmatchcase(native, pattern)
        else:
            matched = fnmatch.fnmatchcase(native.lower(), pattern.lower())

        if matched:
            alias = str(entry.get("alias", "")).strip() or native
            mapped_arch = str(entry.get("arch", "")).strip()
            if mapped_arch:
                arch = normalize_cpu_arch(mapped_arch)
            return alias, arch

    return native, arch


def log(level, msg):
    pbs.logmsg(level, "pbs_discovery_cpus: " + str(msg))


def deep_merge(base, update):
    result = dict(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = os.environ.get("PBS_HOOK_CONFIG_FILE")
    if path and os.path.isfile(path):
        with open(path, "r") as f:
            cfg = deep_merge(cfg, json.load(f))
    return cfg


def read_text(path, default=None):
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except Exception:
        return default


def parse_cpu_list(value):
    result = []
    for part in str(value or "").strip().split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            first, last = part.split("-", 1)
            result.extend(range(int(first), int(last) + 1))
        else:
            result.append(int(part))
    return sorted(set(result))


def size_to_bytes(value):
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return 0
    match = _SIZE_RE.match(text)
    if not match:
        return int(text)
    number = float(match.group(1))
    unit = match.group(2).lower()
    power = {"": 0, "k": 1, "m": 2, "g": 3, "t": 4, "p": 5, "e": 6}[unit]
    return int(number * (1024 ** power))


def bytes_to_pbs_size(value):
    """Convert bytes to a PBS size expressed explicitly in kB."""
    kb = max(0, int(value)) // 1024
    return pbs.size("%dkb" % kb)


def local_node_names():
    names = set()
    for value in (pbs.get_local_nodename(), os.uname().nodename):
        if value:
            names.add(str(value))
            names.add(str(value).split(".")[0])
    return names


def vnode_is_local(name):
    base = str(name).split("[")[0]
    short = base.split(".")[0]
    names = local_node_names()
    return base in names or short in names


def join_resource_values(values):
    """Return a stable comma-separated value suitable for string_array resources."""
    return ",".join(sorted(set(str(v).strip() for v in values if str(v).strip())))


def map_cpu_spec(cpu_model, cpu_spec_map, default_cpu_spec):
    """
    Return the floating-point CPU-core performance value for cpu_model.

    cpu_spec_map is evaluated in order and the first matching entry wins.

    Supported entry fields:
        pattern : shell-style wildcard pattern matched against cpu_model
        cs      : case-sensitive matching when true; default false
        value   : floating-point value published in cpu_spec

    If no entry matches, default_cpu_spec is returned.
    """
    model = str(cpu_model or "").strip()

    for entry in cpu_spec_map or []:
        if not isinstance(entry, dict):
            continue

        pattern = str(entry.get("pattern", "")).strip()
        if not pattern or "value" not in entry:
            continue

        cs = bool(entry.get("cs", False))
        if cs:
            matched = fnmatch.fnmatchcase(model, pattern)
        else:
            matched = fnmatch.fnmatchcase(model.lower(), pattern.lower())

        if matched:
            return float(entry["value"])

    return float(default_cpu_spec)


class CpuTopology(object):
    SYS_CPU = "/sys/devices/system/cpu"

    def __init__(self):
        self.online = set(parse_cpu_list(read_text(os.path.join(self.SYS_CPU, "online"), "")))
        if not self.online:
            self.online = set(
                int(os.path.basename(path)[3:])
                for path in glob.glob(os.path.join(self.SYS_CPU, "cpu[0-9]*"))
            )
        self.cores = self._discover_cores()

    def _discover_cores(self):
        seen = set()
        cores = []
        for cpu in sorted(self.online):
            path = os.path.join(self.SYS_CPU, "cpu%d" % cpu, "topology", "core_cpus_list")
            siblings = parse_cpu_list(read_text(path, ""))
            if not siblings:
                path = os.path.join(
                    self.SYS_CPU, "cpu%d" % cpu, "topology", "thread_siblings_list"
                )
                siblings = parse_cpu_list(read_text(path, str(cpu)))

            siblings = tuple(sorted(set(siblings) & self.online))
            if not siblings or siblings in seen:
                continue

            seen.add(siblings)
            cores.append(siblings)

        if not cores:
            raise RuntimeError("no online physical CPU cores discovered")
        return cores

    @property
    def ncpus(self):
        return len(self.cores)

    @property
    def nthreads(self):
        return sum(len(core) for core in self.cores)

    @property
    def smt(self):
        return self.nthreads > self.ncpus

    @property
    def hybrid_cpu(self):
        if not self.smt:
            return False
        return len(set(len(core) for core in self.cores)) > 1

    @property
    def npus_per_core(self):
        if not self.smt or self.hybrid_cpu:
            return "1"
        return str(len(self.cores[0]))


class NodeDiscovery(object):
    def __init__(self):
        self.cfg = load_config()

    def _memory(self):
        total = 0
        swap = 0
        with open("/proc/meminfo", "r") as f:
            for line in f:
                cols = line.split()
                if not cols:
                    continue
                if cols[0] == "MemTotal:":
                    total = int(cols[1]) * 1024
                elif cols[0] == "SwapTotal:":
                    swap = int(cols[1]) * 1024

        reserve = size_to_bytes(self.cfg.get("memory_reserve", "0B"))
        usable = max(0, total - reserve)
        return usable, usable + swap

    def _cpuinfo(self, topo):
        records = []
        current = {}
        try:
            with open("/proc/cpuinfo", "r") as f:
                for raw in f:
                    line = raw.rstrip("\n")
                    if not line.strip():
                        if current:
                            records.append(current)
                            current = {}
                        continue
                    if ":" not in line:
                        continue
                    key, value = line.split(":", 1)
                    current[key.strip()] = value.strip()
            if current:
                records.append(current)
        except Exception:
            records = []

        selected = []
        for rec in records:
            try:
                proc = int(rec.get("processor", -1))
            except Exception:
                proc = -1
            if not topo.online or proc in topo.online:
                selected.append(rec)
        if not selected:
            selected = records

        models = []
        vendors = []
        arches = []
        flag_sets = []
        vendor_map = self.cfg.get("cpu_vendor_map", [])
        system_arch = normalize_cpu_arch(os.uname().machine)

        for rec in selected:
            model = rec.get("model name") or rec.get("Processor") or rec.get("Hardware")
            vendor = rec.get("vendor_id") or rec.get("CPU implementer")
            flags = rec.get("flags") or rec.get("Features")

            if model:
                models.append(model)
            if vendor:
                mapped_vendor, mapped_arch = map_cpu_identity(vendor, vendor_map, system_arch)
                if mapped_vendor:
                    vendors.append(mapped_vendor)
                if mapped_arch:
                    arches.append(mapped_arch)
            if flags:
                flag_sets.append(set(flags.split()))

        # cpu_arch is always available from the system even if /proc/cpuinfo
        # does not expose a vendor field or no cpu_vendor_map entry matches.
        if not arches and system_arch:
            arches.append(system_arch)

        common_flags = sorted(set.intersection(*flag_sets)) if flag_sets else []
        cpu_arch = join_resource_values(arches)
        # A physical host is expected to have one execution architecture.
        # If malformed/mixed input produced more than one token, do not derive
        # an ISA from an ambiguous architecture value.
        isa_arch = cpu_arch if "," not in cpu_arch else ""

        return {
            "cpu_model": join_resource_values(models),
            "cpu_vendor": join_resource_values(vendors),
            "cpu_arch": cpu_arch,
            "cpu_flag": join_resource_values(common_flags),
            "cpu_isa": detect_cpu_isa(isa_arch, common_flags),
        }

    def discover(self):
        topo = CpuTopology()
        mem, vmem = self._memory()

        cpuinfo = self._cpuinfo(topo)
        result = {
            "ncpus": topo.ncpus,
            "nthreads": topo.nthreads,
            "smt": topo.smt,
            "hybrid_cpu": topo.hybrid_cpu,
            "npus_per_core": topo.npus_per_core,
            "mem": bytes_to_pbs_size(mem),
            "vmem": bytes_to_pbs_size(vmem),
            "cpu_spec": map_cpu_spec(
                cpuinfo.get("cpu_model", ""),
                self.cfg.get("cpu_spec_map", []),
                self.cfg.get("default_cpu_spec", 0.0),
            ),
        }
        result.update(cpuinfo)
        return result

    def publish(self, event):
        resources = self.discover()
        updated = False

        for name in list(event.vnode_list.keys()):
            if not vnode_is_local(name):
                continue

            vnode = event.vnode_list[name]
            for key, value in resources.items():
                if key == "vmem" and not self.cfg.get("publish_vmem", True):
                    continue
                if value == "" and key not in ("cpu_flag", "cpu_isa", "cpu_arch"):
                    continue
                vnode.resources_available[key] = value
            updated = True

        if not updated:
            raise RuntimeError("local vnode not found in vnode_list")

        log(
            pbs.EVENT_DEBUG,
            "published ncpus=%d nthreads=%d smt=%s hybrid_cpu=%s npus_per_core=%s mem=%s%s cpu_vendor=%s cpu_arch=%s cpu_isa=%s cpu_spec=%s"
            % (
                resources["ncpus"],
                resources["nthreads"],
                resources["smt"],
                resources["hybrid_cpu"],
                resources["npus_per_core"],
                resources["mem"],
                " vmem=%s" % resources["vmem"] if self.cfg.get("publish_vmem", True) else "",
                resources.get("cpu_vendor", ""),
                resources.get("cpu_arch", ""),
                resources.get("cpu_isa", ""),
                resources.get("cpu_spec", ""),
            ),
        )


def main():
    event = pbs.event()
    if event.type in (pbs.EXECHOST_STARTUP, pbs.EXECHOST_PERIODIC):
        NodeDiscovery().publish(event)
    event.accept()


try:
    main()
except SystemExit:
    raise
except Exception as exc:
    log(pbs.EVENT_ERROR, "%s\n%s" % (exc, traceback.format_exc()))
    try:
        pbs.event().reject("pbs_discovery_cpus failed: %s" % exc)
    except Exception:
        pass
