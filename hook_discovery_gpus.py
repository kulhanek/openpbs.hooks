# coding: utf-8
"""
OpenPBS execution-host discovery hook for physical NVIDIA GPUs.

Published vnode resources
-------------------------
* ngpus        : number of physical NVIDIA GPUs
* gpu_model    : unique NVIDIA GPU model name(s)
* gpu_cap      : unique CUDA compute capability value(s)
* gpu_mem      : minimum total framebuffer memory per physical GPU, in PBS kb
* cuda_version : maximum CUDA version reported by the installed NVIDIA driver

No ams-host dependency is used. Discovery uses nvidia-smi only.
MIG instances are deliberately ignored: ngpus counts physical GPUs.

Recommended events
------------------
    exechost_startup, exechost_periodic

Suggested custom PBS resources
------------------------------
    gpu_model    : string_array
    gpu_cap      : string_array
    gpu_mem      : size
    cuda_version : string

The standard ngpus resource already exists in OpenPBS installations that use
GPU scheduling; otherwise define it according to local OpenPBS policy.
"""

import json
import os
import re
import subprocess
import traceback

import pbs


DEFAULT_CONFIG = {
    "nvidia_smi": "/usr/bin/nvidia-smi"
}


def log(level, msg):
    pbs.logmsg(level, "pbs_discovery_gpus: " + str(msg))


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = os.environ.get("PBS_HOOK_CONFIG_FILE")
    if path and os.path.isfile(path):
        with open(path, "r") as f:
            cfg.update(json.load(f))
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


class NvidiaDiscovery(object):
    def __init__(self, cfg):
        self.cfg = cfg

    def _cuda_version(self):
        rc, out, err = run([self.cfg["nvidia_smi"]])
        if rc != 0:
            return ""
        match = re.search(r"CUDA Version:\s*([0-9]+(?:\.[0-9]+)*)", out)
        return match.group(1) if match else ""

    def discover(self):
        binary = self.cfg["nvidia_smi"]
        if not os.path.isfile(binary):
            return {"ngpus": 0, "gpu_model": "", "gpu_cap": "", "gpu_mem": None, "cuda_version": ""}

        # compute_cap is supported by current NVIDIA drivers.  Fall back to a
        # query without it so ngpus/model discovery still works on older ones.
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
                    capabilities.append(cols[2])
                if cols[3] and cols[3].upper() != "N/A":
                    try:
                        # With nounits, memory.total is reported in MiB. PBS
                        # size suffix "kb" is KiB, therefore multiply by 1024.
                        memory_kb.append(int(round(float(cols[3]) * 1024.0)))
                    except ValueError:
                        pass
        else:
            cmd = [binary, "--query-gpu=index,name,memory.total", "--format=csv,noheader,nounits"]
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

        return {
            "ngpus": count,
            "gpu_model": joined(models),
            "gpu_cap": joined(capabilities),
            "gpu_mem": min(memory_kb) if memory_kb else None,
            "cuda_version": self._cuda_version(),
        }

    def publish(self, event):
        resources = self.discover()
        updated = False
        for name in list(event.vnode_list.keys()):
            if not vnode_is_local(name):
                continue
            vnode = event.vnode_list[name]
            vnode.resources_available["ngpus"] = int(resources["ngpus"])
            for key in ("gpu_model", "gpu_cap", "cuda_version"):
                # None clears stale values when GPUs disappear or a property
                # cannot be discovered on the current driver.
                vnode.resources_available[key] = resources[key] or None
            vnode.resources_available["gpu_mem"] = (
                pbs.size("%dkb" % resources["gpu_mem"])
                if resources["gpu_mem"] is not None else None
            )
            updated = True
        if not updated:
            raise RuntimeError("local vnode not found in vnode_list")
        log(pbs.EVENT_DEBUG, "published ngpus=%d model=%s capability=%s gpu_mem=%s" %
            (resources["ngpus"], resources["gpu_model"], resources["gpu_cap"],
             (("%dkb" % resources["gpu_mem"]) if resources["gpu_mem"] is not None else "")))


def main():
    event = pbs.event()
    if event.type in (pbs.EXECHOST_STARTUP, pbs.EXECHOST_PERIODIC):
        NvidiaDiscovery(load_config()).publish(event)
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
