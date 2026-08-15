# coding: utf-8
"""
OpenPBS execution-host discovery hook for CPU, memory, OS, and PBS metadata.

Published vnode resources
-------------------------
* ncpus          : number of physical CPU cores (PBS-consumable CPU capacity)
* nthreads       : number of online logical CPUs/PUs (informational)
* hyperthreading : True when nthreads > ncpus
* mem            : usable physical memory after configured reserve
* vmem           : usable physical memory + configured system swap
* cpu_model      : CPU model name(s)
* cpu_vendor     : CPU vendor ID(s)
* cpu_flag       : CPU flags common to all online logical CPUs
* os             : OS ID and version, e.g. ubuntu-24.04
* os_family      : OS family, derived from ID_LIKE or ID
* pbs_server     : PBS server name

No ams-host dependency is used. CPU topology is derived from Linux sysfs,
CPU metadata from /proc/cpuinfo, OS metadata from /etc/os-release, and memory
from /proc/meminfo.

Recommended events
------------------
    exechost_startup, exechost_periodic

Suggested custom PBS resources
------------------------------
Define these according to the site's scheduler policy before enabling the hook:
    nthreads       : long, informational (do not use as an independent CPU pool)
    hyperthreading : boolean
    cpu_model      : string_array (or string on homogeneous nodes)
    cpu_vendor     : string_array (or string)
    cpu_flag       : string_array
    os             : string
    os_family      : string_array (or string)
    pbs_server     : string

The standard resources ncpus, mem, and vmem already exist in OpenPBS.
"""

import glob
import json
import os
import re
import traceback

import pbs


DEFAULT_CONFIG = {
    "memory_reserve": "0B",
    "memory_reserve_percent": 0,
    "publish_vmem": True,
}

_SIZE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([kmgtpe]?)(?:i?b)?\s*$", re.I)


def log(level, msg):
    pbs.logmsg(level, "pbs_discovery_node: " + str(msg))


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


def read_pbs_conf():
    path = os.environ.get("PBS_CONF_FILE", "/etc/pbs.conf")
    result = {}
    try:
        with open(path, "r") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                result[key.strip()] = value.strip().strip('"').strip("'")
    except Exception:
        pass
    return result


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
    return pbs.size("%db" % max(0, int(value)))


def local_node_names():
    names = set()
    for value in (pbs.get_local_nodename(), os.uname().nodename):
        if value:
            names.add(str(value))
            names.add(str(value).split(".")[0])
    conf = read_pbs_conf()
    value = conf.get("PBS_MOM_NODE_NAME")
    if value:
        names.add(value)
        names.add(value.split(".")[0])
    return names


def vnode_is_local(name):
    base = str(name).split("[")[0]
    short = base.split(".")[0]
    names = local_node_names()
    return base in names or short in names


def join_resource_values(values):
    """Return a stable comma-separated value suitable for string_array resources."""
    return ",".join(sorted(set(str(v).strip() for v in values if str(v).strip())))


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
                path = os.path.join(self.SYS_CPU, "cpu%d" % cpu, "topology", "thread_siblings_list")
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
        reserve += int(total * float(self.cfg.get("memory_reserve_percent", 0)) / 100.0)
        usable = max(0, total - reserve)
        return usable, usable + swap

    def _cpuinfo(self):
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

        online = set(CpuTopology().online)
        selected = []
        for rec in records:
            try:
                proc = int(rec.get("processor", -1))
            except Exception:
                proc = -1
            if not online or proc in online:
                selected.append(rec)
        if not selected:
            selected = records

        models = []
        vendors = []
        flag_sets = []
        for rec in selected:
            model = rec.get("model name") or rec.get("Processor") or rec.get("Hardware")
            vendor = rec.get("vendor_id") or rec.get("CPU implementer")
            flags = rec.get("flags") or rec.get("Features")
            if model:
                models.append(model)
            if vendor:
                vendors.append(vendor)
            if flags:
                flag_sets.append(set(flags.split()))

        common_flags = sorted(set.intersection(*flag_sets)) if flag_sets else []
        return {
            "cpu_model": join_resource_values(models),
            "cpu_vendor": join_resource_values(vendors),
            "cpu_flag": join_resource_values(common_flags),
        }

    def _osinfo(self):
        info = {}
        path = "/etc/os-release"
        try:
            with open(path, "r") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    info[key] = value.strip().strip('"').strip("'")
        except Exception:
            pass

        os_id = info.get("ID", "linux")
        version = info.get("VERSION_ID", "")
        os_value = os_id + ("-" + version if version else "")
        family = info.get("ID_LIKE", "").split()
        if not family:
            family = [os_id]
        return {
            "os": os_value,
            "os_family": join_resource_values(family),
        }

    def _pbs_server(self):
        try:
            value = pbs.server().name
            if value:
                return str(value)
        except Exception:
            pass
        return read_pbs_conf().get("PBS_SERVER", "None") or "None"

    def discover(self):
        topo = CpuTopology()
        mem, vmem = self._memory()
        result = {
            "ncpus": topo.ncpus,
            "nthreads": topo.nthreads,
            "hyperthreading": topo.nthreads > topo.ncpus,
            "mem": bytes_to_pbs_size(mem),
            "vmem": bytes_to_pbs_size(vmem),
            "pbs_server": self._pbs_server(),
        }
        result.update(self._cpuinfo())
        result.update(self._osinfo())
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
                if value == "" and key not in ("cpu_flag",):
                    continue
                vnode.resources_available[key] = value
            updated = True

        if not updated:
            raise RuntimeError("local vnode not found in vnode_list")

        log(pbs.EVENT_DEBUG,
            "published ncpus=%d nthreads=%d hyperthreading=%s" %
            (resources["ncpus"], resources["nthreads"], resources["hyperthreading"]))


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
        pbs.event().reject("pbs_discovery_node failed: %s" % exc)
    except Exception:
        pass
