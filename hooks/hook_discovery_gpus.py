# coding: utf-8
"""
OpenPBS execution-host discovery hook for physical GPUs.

Supported vendors
-----------------
* NVIDIA, discovered with nvidia-smi
* AMD, discovered with amd-smi

Published vnode resources
-------------------------
* ngpus        : number of physical GPUs
* gpu_vendor   : GPU vendor ("nvidia" or "amd")
* gpu_model    : GPU model name
* gpu_cap      : GPU capabilities/targets
                  NVIDIA: native sm_XX capability
                  AMD: native gfx target plus optional configured portable target
* gpu_arch     : architecture derived from the native GPU capability/target
* gpu_mem      : minimum total framebuffer memory per physical GPU, in PBS kb
* cuda_version : maximum CUDA version reported by the NVIDIA driver (string_array)

The vendor-specific discovery configuration is stored below "vendors" in the
hook JSON configuration. gpu_cap, gpu_arch, and cuda_version are string_array
resources. GPU-homogeneous hosts are expected, so gpu_model is a scalar string.
For AMD, gpu_cap may contain two values: the native gfx target and its optional
portable target configured in portable_targets.

NVIDIA discovery uses nvidia-smi only. AMD discovery uses amd-smi only.
MIG instances are deliberately ignored by NVIDIA discovery: ngpus counts
physical GPUs.

Recommended events
------------------
    exechost_startup, exechost_periodic

Suggested custom PBS resources
------------------------------
    gpu_mem      : size
    gpu_vendor   : string
    gpu_model    : string
    gpu_cap      : string_array
    gpu_arch     : string_array
    cuda_version : string_array
"""

import json
import os
import re
import subprocess
import traceback

import pbs


DEFAULT_CONFIG = {
    "vendors": {
        "nvidia": {
            "enabled": True,
            "commands": {
                "nvidia_smi": "/usr/bin/nvidia-smi"
            },
            "architectures": {}
        },
        "amd": {
            "enabled": True,
            "commands": {
                "amd_smi": "/usr/bin/amd-smi"
            },
            "architectures": {},
            "portable_targets": {}
        }
    }
}


def log(level, msg):
    pbs.logmsg(level, "pbs_discovery_gpus: " + str(msg))


def deep_update(dst, src):
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            deep_update(dst[key], value)
        else:
            dst[key] = value
    return dst


def load_config():
    # Copy through JSON so nested dictionaries from DEFAULT_CONFIG are not
    # modified when the site configuration is merged into them.
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    path = os.environ.get("PBS_HOOK_CONFIG_FILE")
    if path and os.path.isfile(path):
        with open(path, "r") as f:
            deep_update(cfg, json.load(f))
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


def local_node_names():
    names = set()
    for value in (pbs.get_local_nodename(), os.uname().nodename):
        if value:
            names.add(str(value))
            names.add(str(value).split(".")[0])
    value = read_pbs_conf().get("PBS_MOM_NODE_NAME")
    if value:
        names.add(value)
        names.add(value.split(".")[0])
    return names


def vnode_is_local(name):
    base = str(name).split("[")[0]
    short = base.split(".")[0]
    names = local_node_names()
    return base in names or short in names


def run(cmd):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True)
    out, err = proc.communicate()
    return proc.returncode, out, err


def unique_values(values):
    return sorted(set(str(v).strip() for v in values if str(v).strip()))


def joined(values):
    return ",".join(unique_values(values))


def joined_ordered(values):
    """Join unique non-empty values while preserving first-seen order."""
    result = []
    seen = set()
    for value in values:
        value = str(value).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return ",".join(result)


def scalar(values, resource_name):
    """Return one scalar value for a resource expected to be homogeneous."""
    values = unique_values(values)
    if not values:
        return ""
    if len(values) > 1:
        log(pbs.EVENT_WARNING,
            "multiple values detected for scalar resource %s: %s; using %s" %
            (resource_name, ",".join(values), values[0]))
    return values[0]


def empty_resources():
    return {
        "ngpus": 0,
        "gpu_vendor": "",
        "gpu_model": "",
        "gpu_cap": "",
        "gpu_arch": "",
        "gpu_mem": None,
        "cuda_version": "",
    }


def nvidia_capability(value):
    """Convert NVIDIA compute capability, e.g. 8.9, to PBS value sm_89."""
    value = str(value).strip()
    match = re.match(r"^([0-9]+)\.([0-9]+)$", value)
    if not match:
        return ""
    return "sm_%s%s" % (match.group(1), match.group(2))


def memory_to_kb(value, unit):
    """Convert a reported memory value to the KiB unit used by PBS size kb."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None

    factors = {
        "B": 1.0 / 1024.0,
        "KB": 1.0,
        "KIB": 1.0,
        "MB": 1024.0,
        "MIB": 1024.0,
        "GB": 1024.0 * 1024.0,
        "GIB": 1024.0 * 1024.0,
        "TB": 1024.0 * 1024.0 * 1024.0,
        "TIB": 1024.0 * 1024.0 * 1024.0,
    }
    factor = factors.get(str(unit).strip().upper())
    if factor is None:
        return None
    return int(round(number * factor))


class NvidiaDiscovery(object):
    def __init__(self, cfg):
        self.cfg = cfg
        commands = cfg.get("commands", {})
        self.nvidia_smi = commands.get("nvidia_smi", "/usr/bin/nvidia-smi")
        self.architectures = cfg.get("architectures", {})

        if not os.path.isabs(self.nvidia_smi):
            raise RuntimeError("vendors.nvidia.commands.nvidia_smi must be an absolute path")

    def _cuda_version(self):
        rc, out, err = run([self.nvidia_smi])
        if rc != 0:
            return ""
        match = re.search(r"CUDA Version:\s*([0-9]+(?:\.[0-9]+)*)", out)
        return match.group(1) if match else ""

    def _architectures(self, capabilities):
        result = []
        for capability in sorted(set(capabilities)):
            architecture = self.architectures.get(capability)
            if architecture:
                result.append(str(architecture).strip())
            else:
                log(pbs.EVENT_WARNING,
                    "no NVIDIA architecture mapping for gpu_cap=%s" % capability)
        return result

    def discover(self):
        binary = self.nvidia_smi
        if not os.path.isfile(binary):
            return empty_resources()

        # compute_cap is supported by current NVIDIA drivers. Fall back to a
        # query without it so count/model/memory discovery still works with
        # older drivers.
        cmd = [binary,
               "--query-gpu=index,name,compute_cap,memory.total",
               "--format=csv,noheader,nounits"]
        rc, out, err = run(cmd)
        capabilities = []
        models = []
        memory_kb = []
        count = 0

        if rc == 0:
            for raw in out.splitlines():
                cols = [x.strip() for x in raw.split(",", 3)]
                if len(cols) != 4:
                    continue
                count += 1
                models.append(cols[1])
                if cols[2] and cols[2].upper() != "N/A":
                    capability = nvidia_capability(cols[2])
                    if capability:
                        capabilities.append(capability)
                    else:
                        log(pbs.EVENT_WARNING,
                            "unrecognized NVIDIA compute capability: %s" % cols[2])
                if cols[3] and cols[3].upper() != "N/A":
                    value = memory_to_kb(cols[3], "MiB")
                    if value is not None:
                        memory_kb.append(value)
        else:
            cmd = [binary,
                   "--query-gpu=index,name,memory.total",
                   "--format=csv,noheader,nounits"]
            rc, out, err = run(cmd)
            if rc != 0:
                # nvidia-smi commonly exits non-zero on a host with no NVIDIA
                # device. Let the vendor dispatcher try AMD before deciding
                # whether discovery failed.
                raise RuntimeError("nvidia-smi failed: %s" % err.strip())
            for raw in out.splitlines():
                cols = [x.strip() for x in raw.split(",", 2)]
                if len(cols) != 3:
                    continue
                count += 1
                models.append(cols[1])
                if cols[2] and cols[2].upper() != "N/A":
                    value = memory_to_kb(cols[2], "MiB")
                    if value is not None:
                        memory_kb.append(value)

        architectures = self._architectures(capabilities)

        return {
            "ngpus": count,
            "gpu_vendor": "nvidia" if count else "",
            "gpu_model": scalar(models, "gpu_model"),
            "gpu_cap": joined(capabilities),
            "gpu_arch": joined(architectures),
            "gpu_mem": min(memory_kb) if memory_kb else None,
            "cuda_version": joined([self._cuda_version()]),
        }


class AmdDiscovery(object):
    GPU_HEADER_RE = re.compile(r"^GPU:\s*([^\s]+)\s*$", re.IGNORECASE)
    FIELD_RE = re.compile(r"^\s*([A-Z0-9_ ]+):\s*(.*?)\s*$", re.IGNORECASE)
    MEMORY_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([KMGT]i?B|B)\b", re.IGNORECASE)
    TARGET_RE = re.compile(r"^gfx[0-9a-z]+$", re.IGNORECASE)

    def __init__(self, cfg):
        self.cfg = cfg
        commands = cfg.get("commands", {})
        self.amd_smi = commands.get("amd_smi", "/usr/bin/amd-smi")
        self.architectures = cfg.get("architectures", {})
        self.portable_targets = cfg.get("portable_targets", {})

        if not os.path.isabs(self.amd_smi):
            raise RuntimeError("vendors.amd.commands.amd_smi must be an absolute path")

    def _portable_capabilities(self, target):
        value = self.portable_targets.get(target)
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        value = str(value).strip()
        return [value] if value else []

    def _architecture(self, target):
        architecture = self.architectures.get(target)
        if architecture:
            return str(architecture).strip()
        log(pbs.EVENT_WARNING,
            "no AMD architecture mapping for gpu_cap=%s" % target)
        return ""

    def _parse_static_output(self, out):
        """Parse documented amd-smi static --asic --vram text output."""
        devices = []
        current = None
        section = ""

        for raw in out.splitlines():
            stripped = raw.strip()
            header = self.GPU_HEADER_RE.match(stripped)
            if header:
                current = {
                    "id": header.group(1),
                    "model": "",
                    "target": "",
                    "memory_kb": None,
                }
                devices.append(current)
                section = ""
                continue

            if current is None or not stripped:
                continue

            if stripped.endswith(":") and stripped.count(":") == 1:
                section = stripped[:-1].strip().upper()
                continue

            match = self.FIELD_RE.match(raw)
            if not match:
                continue
            key = match.group(1).strip().upper().replace(" ", "_")
            value = match.group(2).strip()

            if section == "ASIC":
                if key == "MARKET_NAME" and value.upper() != "N/A":
                    current["model"] = value
                elif key == "TARGET_GRAPHICS_VERSION" and value.upper() != "N/A":
                    target = value.lower()
                    if self.TARGET_RE.match(target):
                        current["target"] = target
                    else:
                        log(pbs.EVENT_WARNING,
                            "unrecognized AMD target graphics version: %s" % value)
            elif section == "VRAM" and key == "SIZE" and value.upper() != "N/A":
                mem = self.MEMORY_RE.match(value)
                if mem:
                    current["memory_kb"] = memory_to_kb(mem.group(1), mem.group(2))

        return devices

    def discover(self):
        binary = self.amd_smi
        if not os.path.isfile(binary):
            return empty_resources()

        # The static command reports all GPUs when no --gpu selector is given.
        # Request only ASIC and VRAM data: these contain MARKET_NAME,
        # TARGET_GRAPHICS_VERSION and VRAM SIZE.
        rc, out, err = run([binary, "static", "--asic", "--vram"])
        if rc != 0:
            raise RuntimeError("amd-smi failed: %s" % err.strip())

        devices = self._parse_static_output(out)
        if not devices:
            return empty_resources()

        models = []
        native_targets = []
        capabilities = []
        architectures = []
        memory_kb = []

        for device in devices:
            if device["model"]:
                models.append(device["model"])
            if device["memory_kb"] is not None:
                memory_kb.append(device["memory_kb"])

            target = device["target"]
            if target:
                native_targets.append(target)
                capabilities.append(target)
                capabilities.extend(self._portable_capabilities(target))
                architecture = self._architecture(target)
                if architecture:
                    architectures.append(architecture)

        if not native_targets:
            log(pbs.EVENT_WARNING,
                "AMD GPUs detected, but amd-smi did not report TARGET_GRAPHICS_VERSION")

        return {
            "ngpus": len(devices),
            "gpu_vendor": "amd",
            "gpu_model": scalar(models, "gpu_model"),
            "gpu_cap": joined_ordered(capabilities),
            "gpu_arch": joined(architectures),
            "gpu_mem": min(memory_kb) if memory_kb else None,
            # CUDA compatibility is NVIDIA-specific. Publishing None later
            # clears a stale value if a vnode changes vendor/configuration.
            "cuda_version": "",
        }


class GpuDiscovery(object):
    def __init__(self, cfg):
        self.cfg = cfg

    def discover(self):
        vendors = self.cfg.get("vendors", {})
        backends = []

        nvidia_cfg = vendors.get("nvidia", {})
        if nvidia_cfg.get("enabled", False):
            backends.append(("nvidia", NvidiaDiscovery(nvidia_cfg)))

        amd_cfg = vendors.get("amd", {})
        if amd_cfg.get("enabled", False):
            backends.append(("amd", AmdDiscovery(amd_cfg)))

        errors = []
        for vendor_name, backend in backends:
            try:
                resources = backend.discover()
            except Exception as exc:
                errors.append("%s: %s" % (vendor_name, exc))
                log(pbs.EVENT_WARNING,
                    "%s discovery failed; trying next enabled vendor: %s" %
                    (vendor_name, exc))
                continue

            if int(resources.get("ngpus", 0)) > 0:
                return resources

        if errors:
            raise RuntimeError("; ".join(errors))
        return empty_resources()

    def publish(self, event):
        resources = self.discover()
        updated = False
        for name in list(event.vnode_list.keys()):
            if not vnode_is_local(name):
                continue

            vnode = event.vnode_list[name]
            vnode.resources_available["ngpus"] = int(resources["ngpus"])

            for key in ("gpu_vendor", "gpu_model", "gpu_cap", "gpu_arch",
                        "cuda_version"):
                # None clears stale values when GPUs disappear or a property
                # cannot be discovered with the current driver/configuration.
                vnode.resources_available[key] = resources[key] or None

            vnode.resources_available["gpu_mem"] = (
                pbs.size("%dkb" % resources["gpu_mem"])
                if resources["gpu_mem"] is not None else None
            )
            updated = True

        if not updated:
            raise RuntimeError("local vnode not found in vnode_list")

        log(pbs.EVENT_DEBUG,
            "published ngpus=%d vendor=%s model=%s cap=%s arch=%s gpu_mem=%s" %
            (resources["ngpus"], resources["gpu_vendor"],
             resources["gpu_model"], resources["gpu_cap"],
             resources["gpu_arch"],
             (("%dkb" % resources["gpu_mem"])
              if resources["gpu_mem"] is not None else "")))


def main():
    event = pbs.event()
    if event.type in (pbs.EXECHOST_STARTUP, pbs.EXECHOST_PERIODIC):
        GpuDiscovery(load_config()).publish(event)
    event.accept()


try:
    main()
except SystemExit:
    raise
except Exception as exc:
    log(pbs.EVENT_ERROR, "%s\n%s" % (exc, traceback.format_exc()))
    try:
        pbs.event().reject("pbs_discovery_gpus failed: %s" % exc)
    except Exception:
        pass
