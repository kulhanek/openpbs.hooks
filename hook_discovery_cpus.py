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
* cpu_flag       : CPU flags common to all online logical CPUs
* cpu_isa        : cumulative x86-64 psABI feature levels supported by the node
* spec           : relative speed of one CPU core, derived from cpu_model

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
    cpu_model      : string_array (or string on homogeneous nodes)
    cpu_vendor     : string_array (or string on homogeneous nodes)
    cpu_flag       : string_array
    cpu_isa        : string_array
    spec           : float

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
    "default_spec": 0.0,
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


def detect_x86_64_isa(common_flags):
    """
    Return cumulative x86-64 psABI microarchitecture feature levels.

    The result is suitable for a PBS string_array resource.  For example,
    a v3-capable node publishes:
        x86-64-v1,x86-64-v2,x86-64-v3

    Non-x86-64 systems return an empty string.
    """
    machine = str(os.uname().machine or "").lower()
    if machine not in ("x86_64", "amd64"):
        return ""

    flags = set(str(flag).strip().lower() for flag in common_flags if str(flag).strip())
    levels = ["x86-64-v1"]

    for level, required in _X86_64_ISA_LEVEL_FLAGS:
        if required.issubset(flags):
            levels.append(level)
        else:
            # Levels are cumulative.  If one level is not supported, higher
            # levels must not be advertised even if their incremental flags
            # happen to be present.
            break

    return join_resource_values(levels)


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


def map_cpu_vendor(vendor, vendor_map):
    """
    Translate a native CPU vendor string using the first matching map entry.

    Supported entry fields:
        pattern : shell-style wildcard pattern, e.g. "*AMD*"
        cs      : case-sensitive matching when true; default false
        alias   : value published in cpu_vendor when matched

    If no entry matches, the native vendor string is returned unchanged.
    """
    native = str(vendor or "").strip()
    if not native:
        return native

    for entry in vendor_map or []:
        if not isinstance(entry, dict):
            continue

        pattern = str(entry.get("pattern", "")).strip()
        alias = str(entry.get("alias", "")).strip()
        if not pattern or not alias:
            continue

        cs = bool(entry.get("cs", False))
        if cs:
            matched = fnmatch.fnmatchcase(native, pattern)
        else:
            matched = fnmatch.fnmatchcase(native.lower(), pattern.lower())

        if matched:
            return alias

    return native


def map_cpu_spec(cpu_model, spec_map, default_spec):
    """
    Return the floating-point CPU-core performance value for cpu_model.

    cpu_spec_map is evaluated in order and the first matching entry wins.

    Supported entry fields:
        pattern : shell-style wildcard pattern matched against cpu_model
        cs      : case-sensitive matching when true; default false
        value   : floating-point value published in spec

    If no entry matches, default_spec is returned.
    """
    model = str(cpu_model or "").strip()

    for entry in spec_map or []:
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

    return float(default_spec)


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
        flag_sets = []
        vendor_map = self.cfg.get("cpu_vendor_map", [])

        for rec in selected:
            model = rec.get("model name") or rec.get("Processor") or rec.get("Hardware")
            vendor = rec.get("vendor_id") or rec.get("CPU implementer")
            flags = rec.get("flags") or rec.get("Features")

            if model:
                models.append(model)
            if vendor:
                vendors.append(map_cpu_vendor(vendor, vendor_map))
            if flags:
                flag_sets.append(set(flags.split()))

        common_flags = sorted(set.intersection(*flag_sets)) if flag_sets else []
        return {
            "cpu_model": join_resource_values(models),
            "cpu_vendor": join_resource_values(vendors),
            "cpu_flag": join_resource_values(common_flags),
            "cpu_isa": detect_x86_64_isa(common_flags),
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
            "spec": map_cpu_spec(
                cpuinfo.get("cpu_model", ""),
                self.cfg.get("cpu_spec_map", []),
                self.cfg.get("default_spec", 0.0),
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
                if value == "" and key not in ("cpu_flag", "cpu_isa"):
                    continue
                vnode.resources_available[key] = value
            updated = True

        if not updated:
            raise RuntimeError("local vnode not found in vnode_list")

        log(
            pbs.EVENT_DEBUG,
            "published ncpus=%d nthreads=%d smt=%s hybrid_cpu=%s npus_per_core=%s mem=%s%s cpu_vendor=%s cpu_isa=%s spec=%s"
            % (
                resources["ncpus"],
                resources["nthreads"],
                resources["smt"],
                resources["hybrid_cpu"],
                resources["npus_per_core"],
                resources["mem"],
                " vmem=%s" % resources["vmem"] if self.cfg.get("publish_vmem", True) else "",
                resources.get("cpu_vendor", ""),
                resources.get("cpu_isa", ""),
                resources.get("spec", ""),
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
