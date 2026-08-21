# coding: utf-8
"""
OpenPBS execution-host discovery hook for physical GPUs.

Currently supported vendors
---------------------------
* NVIDIA, discovered with nvidia-smi

Published vnode resources
-------------------------
* ngpus        : number of physical GPUs
* gpu_vendor   : GPU vendor (currently "nvidia")
* gpu_model    : unique GPU model name(s)
* gpu_cap      : native GPU capability, e.g. sm_89
* gpu_arch     : architecture derived from gpu_cap, e.g. ada
* gpu_mem      : minimum total framebuffer memory per physical GPU, in PBS kb
* cuda_version : maximum CUDA version reported by the NVIDIA driver

The vendor-specific discovery configuration is stored below "vendors" in the
hook JSON configuration.  gpu_cap and gpu_arch are string_array resources even
though GPU-homogeneous hosts are expected and therefore normally publish one
value only.

No ams-host dependency is used. NVIDIA discovery uses nvidia-smi only.
MIG instances are deliberately ignored: ngpus counts physical GPUs.

Recommended events
------------------
    exechost_startup, exechost_periodic

Suggested custom PBS resources
------------------------------
    gpu_vendor   : string
    gpu_model    : string_array
    gpu_cap      : string_array
    gpu_arch     : string_array
    gpu_mem      : size
    cuda_version : string
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


def joined(values):
    return ",".join(sorted(set(str(v).strip() for v in values if str(v).strip())))


def nvidia_capability(value):
    """Convert NVIDIA compute capability, e.g. 8.9, to PBS value sm_89."""
    value = str(value).strip()
    match = re.match(r"^([0-9]+)\.([0-9]+)$", value)
    if not match:
        return ""
    return "sm_%s%s" % (match.group(1), match.group(2))


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
            return {
                "ngpus": 0,
                "gpu_vendor": "",
                "gpu_model": "",
                "gpu_cap": "",
                "gpu_arch": "",
                "gpu_mem": None,
                "cuda_version": "",
            }

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
                    try:
                        # With nounits, memory.total is reported in MiB. PBS
                        # size suffix "kb" is KiB, therefore multiply by 1024.
                        memory_kb.append(int(round(float(cols[3]) * 1024.0)))
                    except ValueError:
                        pass
        else:
            cmd = [binary,
                   "--query-gpu=index,name,memory.total",
                   "--format=csv,noheader,nounits"]
            rc, out, err = run(cmd)
            if rc != 0:
                raise RuntimeError("nvidia-smi failed: %s" % err.strip())
            for raw in out.splitlines():
                cols = [x.strip() for x in raw.split(",", 2)]
                if len(cols) != 3:
                    continue
                count += 1
                models.append(cols[1])
                if cols[2] and cols[2].upper() != "N/A":
                    try:
                        memory_kb.append(int(round(float(cols[2]) * 1024.0)))
                    except ValueError:
                        pass

        architectures = self._architectures(capabilities)

        return {
            "ngpus": count,
            "gpu_vendor": "nvidia" if count else "",
            "gpu_model": joined(models),
            "gpu_cap": joined(capabilities),
            "gpu_arch": joined(architectures),
            "gpu_mem": min(memory_kb) if memory_kb else None,
            "cuda_version": self._cuda_version(),
        }


class GpuDiscovery(object):
    def __init__(self, cfg):
        self.cfg = cfg

    def discover(self):
        vendors = self.cfg.get("vendors", {})
        nvidia_cfg = vendors.get("nvidia", {})

        if nvidia_cfg.get("enabled", False):
            return NvidiaDiscovery(nvidia_cfg).discover()

        return {
            "ngpus": 0,
            "gpu_vendor": "",
            "gpu_model": "",
            "gpu_cap": "",
            "gpu_arch": "",
            "gpu_mem": None,
            "cuda_version": "",
        }

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
