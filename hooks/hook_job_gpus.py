# coding: utf-8
"""
OpenPBS job hook: whole physical GPU allocation, cgroup-v2 device
isolation, vendor-specific runtime environment setup, and lightweight GPU
accounting for NVIDIA and AMD GPUs.

Scope
-----
* Physical NVIDIA and AMD GPUs only. NVIDIA MIG and AMD GPU partitioning are
  intentionally unsupported.
* Each vnode is expected to contain GPUs from one vendor, published as
  resources_available.gpu_vendor by hook_discovery_gpus.
* GPU allocation is immutable for the lifetime of a job.
* CPU/memory/cgroup creation is owned by hook_job_cgroups_v2.py.
* This hook attaches a BPF_CGROUP_DEVICE policy to that existing job cgroup.
* NVIDIA telemetry uses nvidia-smi; AMD telemetry uses amd-smi. Telemetry is
  non-fatal. GPU allocation or device-isolation failures are fatal.
* This hook does not publish vnode/resources_available GPU discovery data.

PBS accounting resources are unchanged: gpupercent, gpumemmaxpercent,
gpupowerusageavg, and gpuenergyconsumed are long resources with flag r.

Events to enable
----------------
    exechost_periodic, execjob_begin, execjob_launch,
    execjob_epilogue, execjob_end, execjob_abort, execjob_resize

For execjob_begin this hook must run AFTER hook_job_cgroups_v2.py, because the job
cgroup must already exist before the BPF device program is attached.
"""

import ctypes
import fcntl
import glob
import json
import os
import platform
import re
import stat
import subprocess
import time
import traceback

import pbs


DEFAULT_CONFIG = {
    "cgroup_root": "/sys/fs/cgroup/system.slice/pbs-mom.service",
    "jobs_subdir": "pbs_jobs",
    "state_subdir": "gpu_v2",
    "device_isolation": True,
    "manage_drm_acl": True,
    "telemetry": True,
    "allocation": "index",          # index | numa
    "vendors": {
        "nvidia": {"smi": "/usr/bin/nvidia-smi"},
        "amd": {"smi": "/usr/bin/amd-smi"},
    },
}

def log(level, msg):
    pbs.logmsg(level, "pbs_job_gpus: " + str(msg))


def read_pbs_conf():
    path = os.environ.get("PBS_CONF_FILE", "/etc/pbs.conf")
    out = {}
    try:
        with open(path, "r") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                out[key.strip()] = value.strip().strip('"').strip("'")
    except Exception as exc:
        raise RuntimeError("cannot read %s: %s" % (path, exc))
    return out


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


def local_node_names():
    names = set()
    for value in (pbs.get_local_nodename(), os.uname().nodename):
        if value:
            names.add(str(value))
            names.add(str(value).split(".")[0])
    conf = read_pbs_conf()
    if conf.get("PBS_MOM_NODE_NAME"):
        value = conf["PBS_MOM_NODE_NAME"]
        names.add(value)
        names.add(value.split(".")[0])
    return names


def vnode_is_local(name):
    base = str(name).split("[")[0]
    short = base.split(".")[0]
    names = local_node_names()
    return base in names or short in names


def local_ngpus(job):
    total = 0
    try:
        chunks = job.exec_vnode.chunks
    except Exception:
        chunks = []
    for chunk in chunks:
        if not vnode_is_local(chunk.vnode_name):
            continue
        try:
            value = chunk.chunk_resources["ngpus"]
        except Exception:
            value = None
        if value is not None:
            try:
                total += int(value)
            except (TypeError, ValueError):
                pass
    return total


def run(cmd, check=False):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True)
    out, err = proc.communicate()
    if check and proc.returncode != 0:
        raise RuntimeError("command failed (%d): %s: %s" %
                           (proc.returncode, " ".join(cmd), err.strip()))
    return proc.returncode, out, err


def dev_info(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    if stat.S_ISCHR(st.st_mode):
        dtype = "c"
    elif stat.S_ISBLK(st.st_mode):
        dtype = "b"
    else:
        return None
    return {
        "path": path,
        "type": dtype,
        "major": os.major(st.st_rdev),
        "minor": os.minor(st.st_rdev),
    }


class FileLock(object):
    def __init__(self, path):
        self.path = path
        self.fd = None

    def __enter__(self):
        self.fd = open(self.path, "a+")
        fcntl.flock(self.fd.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.fd:
            fcntl.flock(self.fd.fileno(), fcntl.LOCK_UN)
            self.fd.close()


# ---------------------------------------------------------------------------
# Vendor-neutral PCI helpers and vendor runtimes
# ---------------------------------------------------------------------------

def normalize_pci_bus_id(bus_id):
    bus = str(bus_id).strip().lower()
    fields = bus.split(":")
    if len(fields) == 3 and len(fields[0]) == 8:
        bus = fields[0][-4:] + ":" + fields[1] + ":" + fields[2]
    return bus


def pci_numa_node(bus_id):
    path = os.path.join("/sys/bus/pci/devices", normalize_pci_bus_id(bus_id),
                        "numa_node")
    try:
        value = int(open(path, "r").read().strip())
        return 0 if value < 0 else value
    except Exception:
        return 0


def pci_drm_devices(bus_id):
    result = []
    bus_id = normalize_pci_bus_id(bus_id)
    for cls in ("card[0-9]*", "renderD[0-9]*"):
        for path in glob.glob(os.path.join("/sys/class/drm", cls)):
            if bus_id not in os.path.realpath(path).lower():
                continue
            info = dev_info(os.path.join("/dev/dri", os.path.basename(path)))
            if info and info not in result:
                result.append(info)
    return result


def _iter_dicts(value, path=()):
    if isinstance(value, dict):
        yield path, value
        for key, item in value.items():
            for entry in _iter_dicts(item, path + (str(key),)):
                yield entry
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            for entry in _iter_dicts(item, path + (str(idx),)):
                yield entry


def _flatten(value, prefix="", out=None):
    if out is None:
        out = {}
    if isinstance(value, dict):
        for key, item in value.items():
            token = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
            name = (prefix + "_" + token).strip("_")
            _flatten(item, name, out)
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            _flatten(item, prefix + "_%d" % idx, out)
    else:
        out[prefix] = value
    return out


def _number(value):
    if isinstance(value, (int, float)):
        return float(value)
    if value is None:
        return None
    match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", str(value))
    return float(match.group(0)) if match else None


def _find_flat(flat, names):
    names = tuple(re.sub(r"[^a-z0-9]+", "_", x.lower()).strip("_") for x in names)
    for name in names:
        for key, value in flat.items():
            if key == name or key.endswith("_" + name):
                return value
    return None


def _find_number(flat, names):
    value = _find_flat(flat, names)
    return _number(value)


class NvidiaRuntime(object):
    vendor = "nvidia"

    def __init__(self, cfg):
        self.cfg = cfg
        self.smi = cfg["vendors"]["nvidia"]["smi"]

    def available(self):
        return os.path.isfile(self.smi)

    def _device_minor(self, uuid, bus_id):
        """Return the NVIDIA character-device minor for a physical GPU.

        nvidia-smi's GPU index is an enumeration ordinal and is not guaranteed
        to be the same as the minor used by /dev/nvidiaN.  The NVIDIA kernel
        driver publishes the authoritative mapping in
        /proc/driver/nvidia/gpus/*/information.
        """
        wanted_bus = normalize_pci_bus_id(bus_id)
        for path in glob.glob('/proc/driver/nvidia/gpus/*/information'):
            values = {}
            try:
                with open(path, 'r') as f:
                    for raw in f:
                        if ':' not in raw:
                            continue
                        key, value = raw.split(':', 1)
                        values[key.strip().lower()] = value.strip()
            except OSError:
                continue

            found_uuid = values.get('gpu uuid')
            found_bus = values.get('bus location')
            if found_uuid != uuid:
                continue
            if found_bus and normalize_pci_bus_id(found_bus) != wanted_bus:
                continue

            value = values.get('device minor')
            if value is None or value == '':
                raise RuntimeError(
                    'NVIDIA GPU %s at %s has no Device Minor in %s' %
                    (uuid, bus_id, path))
            try:
                return int(value, 0)
            except ValueError:
                raise RuntimeError(
                    'invalid NVIDIA Device Minor %r for GPU %s in %s' %
                    (value, uuid, path))

        raise RuntimeError(
            'cannot map NVIDIA GPU %s at %s to a /dev/nvidiaN minor' %
            (uuid, bus_id))

    def inventory(self):
        if not self.available():
            return []
        cmd = [self.smi,
               "--query-gpu=index,uuid,pci.bus_id,memory.total",
               "--format=csv,noheader,nounits"]
        rc, out, err = run(cmd)
        if rc != 0:
            raise RuntimeError("nvidia-smi failed: %s" % err.strip())

        gpus = []
        for raw in out.splitlines():
            parts = [x.strip() for x in raw.split(",")]
            if len(parts) != 4:
                continue
            index = int(parts[0])
            uuid = parts[1]
            bus = normalize_pci_bus_id(parts[2])
            memory = int(float(parts[3]) * 1024 * 1024)
            device_minor = self._device_minor(uuid, bus)
            ndev_path = "/dev/nvidia%d" % device_minor
            ndev = dev_info(ndev_path)
            if ndev is None:
                raise RuntimeError(
                    "GPU index %d UUID %s at %s maps to missing %s" %
                    (index, uuid, bus, ndev_path))
            # Be defensive: the filename and actual character-device minor
            # should agree.  The BPF rule uses the actual st_rdev values.
            if ndev["minor"] != device_minor:
                raise RuntimeError(
                    "GPU %s maps to %s but its device minor is %d" %
                    (uuid, ndev_path, ndev["minor"]))
            drm = pci_drm_devices(bus)
            gpus.append({
                "index": index,
                "runtime_index": index,
                "device_minor": device_minor,
                "uuid": uuid,
                "pci_bus_id": bus,
                "numa": pci_numa_node(bus),
                "memory": memory,
                "devices": [ndev] + list(drm),
                "drm_devices": drm,
            })
        gpus.sort(key=lambda g: g["index"])
        return gpus

    def telemetry(self):
        if not self.available():
            return {}
        cmd = [self.smi,
               "--query-gpu=uuid,utilization.gpu,memory.used,memory.total,power.draw",
               "--format=csv,noheader,nounits"]
        rc, out, err = run(cmd)
        if rc != 0:
            raise RuntimeError("nvidia-smi telemetry failed: %s" % err.strip())
        values = {}
        for raw in out.splitlines():
            parts = [x.strip() for x in raw.split(",")]
            if len(parts) != 5:
                continue
            try:
                util = float(parts[1])
                mem_used = float(parts[2])
                mem_total = float(parts[3])
            except (TypeError, ValueError):
                continue
            try:
                power_w = float(parts[4])
            except (TypeError, ValueError):
                power_w = None
            values[parts[0]] = {
                "util": util, "mem_used": mem_used,
                "mem_total": mem_total, "power_w": power_w,
            }
        return values

    def environment(self, selected):
        uuids = [g["uuid"] for g in selected]
        env = {"CUDA_VISIBLE_DEVICES": "\\,".join(uuids) if uuids else ""}
        if uuids:
            env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        return env


class AmdRuntime(object):
    vendor = "amd"

    def __init__(self, cfg):
        self.cfg = cfg
        self.smi = cfg["vendors"]["amd"]["smi"]

    def available(self):
        return os.path.isfile(self.smi)

    def _json(self, args, what):
        rc, out, err = run([self.smi] + list(args) + ["--json"])
        if rc != 0:
            raise RuntimeError("amd-smi %s failed: %s" % (what, err.strip()))
        try:
            return json.loads(out)
        except Exception as exc:
            raise RuntimeError("cannot parse amd-smi %s JSON: %s" % (what, exc))

    def _vram_total(self, bus, drm):
        candidates = []
        for dev in drm:
            base = os.path.basename(dev["path"])
            if base.startswith("card"):
                candidates.append(os.path.join("/sys/class/drm", base, "device",
                                               "mem_info_vram_total"))
        candidates.append(os.path.join("/sys/bus/pci/devices", bus,
                                       "mem_info_vram_total"))
        for path in candidates:
            try:
                return int(open(path, "r").read().strip())
            except Exception:
                pass
        return 0

    def inventory(self):
        if not self.available():
            return []
        data = self._json(["list", "-e"], "list -e")
        rows = []
        seen = set()
        for path, obj in _iter_dicts(data):
            flat = _flatten(obj)
            bdf = _find_flat(flat, ("bdf", "pci_bdf", "pci_bus", "pci_bus_id"))
            uuid = _find_flat(flat, ("uuid", "hip_uuid", "gpu_uuid"))
            if bdf is None or uuid is None:
                continue
            bus = normalize_pci_bus_id(bdf)
            key = (str(uuid), bus)
            if key in seen:
                continue
            seen.add(key)
            index = _find_number(flat, ("gpu", "gpu_id", "index", "id"))
            hip_index = _find_number(flat, ("hip_id", "hip_index"))
            if index is None:
                for token in reversed(path):
                    match = re.search(r"(?:gpu[_ -]?)?(\d+)$", token.lower())
                    if match:
                        index = float(match.group(1))
                        break
            rows.append((index, hip_index, str(uuid), bus))

        rows.sort(key=lambda row: (row[0] is None, row[0] if row[0] is not None else 0,
                                   row[3]))
        gpus = []
        for fallback, row in enumerate(rows):
            index, hip_index, uuid, bus = row
            index = fallback if index is None else int(index)
            hip_index = index if hip_index is None else int(hip_index)
            drm = pci_drm_devices(bus)
            render = [d for d in drm if os.path.basename(d["path"]).startswith("renderD")]
            if not render:
                raise RuntimeError("AMD GPU %d at %s has no DRM render device" %
                                   (index, bus))
            gpus.append({
                "index": index,
                "runtime_index": hip_index,
                "uuid": uuid,
                "pci_bus_id": bus,
                "numa": pci_numa_node(bus),
                "memory": self._vram_total(bus, drm),
                "devices": list(drm),
                "drm_devices": drm,
            })
        return gpus

    def telemetry(self):
        if not self.available():
            return {}
        inventory = self.inventory()
        by_index = {g["index"]: g["uuid"] for g in inventory}
        by_runtime = {g["runtime_index"]: g["uuid"] for g in inventory}
        data = self._json(["metric", "-u", "-m", "-p"], "metric")
        values = {}
        for path, obj in _iter_dicts(data):
            flat = _flatten(obj)
            uuid = _find_flat(flat, ("uuid", "hip_uuid", "gpu_uuid"))
            if uuid is None:
                idx = _find_number(flat, ("gpu", "gpu_id", "index", "id", "hip_id"))
                if idx is not None:
                    uuid = by_index.get(int(idx), by_runtime.get(int(idx)))
            if uuid is None:
                continue
            util = _find_number(flat, (
                "gfx_activity", "gfx_usage", "gfx_percent", "gpu_usage",
                "gpu_utilization", "usage_gfx", "gfx"))
            mem_used = _find_number(flat, (
                "vram_used", "vram_usage_used", "used_vram", "memory_vram_used"))
            mem_total = _find_number(flat, (
                "vram_total", "vram_usage_total", "total_vram", "memory_vram_total"))
            power_w = _find_number(flat, (
                "current_socket_power", "average_socket_power", "socket_power",
                "power_usage", "current_power", "power"))
            if util is None or mem_used is None or mem_total is None:
                continue
            values[str(uuid)] = {
                "util": util, "mem_used": mem_used,
                "mem_total": mem_total, "power_w": power_w,
            }
        return values

    def environment(self, selected):
        # Use the HIP ordinal reported by amd-smi list -e.  Do not set
        # ROCR_VISIBLE_DEVICES at the same time: combining lower-level ROCR
        # filtering with HIP ordinal filtering can change the ordinal space.
        indices = [str(g["runtime_index"]) for g in selected]
        return {
            "HIP_VISIBLE_DEVICES": "\\,".join(indices) if indices else "",
        }

# ---------------------------------------------------------------------------
# Minimal cgroup-device BPF loader
# ---------------------------------------------------------------------------

class BpfInsn(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ubyte),
        ("dst_src", ctypes.c_ubyte),
        ("off", ctypes.c_int16),
        ("imm", ctypes.c_int32),
    ]


def insn(code, dst=0, src=0, off=0, imm=0):
    return BpfInsn(code=code, dst_src=((src & 0xf) << 4) | (dst & 0xf),
                   off=off, imm=imm)


class BpfProgLoadAttr(ctypes.Structure):
    _fields_ = [
        ("prog_type", ctypes.c_uint32),
        ("insn_cnt", ctypes.c_uint32),
        ("insns", ctypes.c_uint64),
        ("license", ctypes.c_uint64),
        ("log_level", ctypes.c_uint32),
        ("log_size", ctypes.c_uint32),
        ("log_buf", ctypes.c_uint64),
        ("kern_version", ctypes.c_uint32),
        ("prog_flags", ctypes.c_uint32),
        ("prog_name", ctypes.c_char * 16),
        ("prog_ifindex", ctypes.c_uint32),
        ("expected_attach_type", ctypes.c_uint32),
    ]


class BpfProgAttachAttr(ctypes.Structure):
    _fields_ = [
        ("target_fd", ctypes.c_uint32),
        ("attach_bpf_fd", ctypes.c_uint32),
        ("attach_type", ctypes.c_uint32),
        ("attach_flags", ctypes.c_uint32),
        ("replace_bpf_fd", ctypes.c_uint32),
        ("relative_fd", ctypes.c_uint32),
        ("expected_revision", ctypes.c_uint64),
    ]


class DeviceBpf(object):
    # linux/uapi bpf.h values, stable ABI.
    BPF_PROG_LOAD = 5
    BPF_PROG_ATTACH = 8
    BPF_PROG_TYPE_CGROUP_DEVICE = 15
    BPF_CGROUP_DEVICE = 6

    # eBPF opcodes used here.
    BPF_LDX_MEM_W = 0x61
    BPF_JMP_JNE_K = 0x55
    BPF_ALU64_MOV_K = 0xb7
    BPF_JMP_EXIT = 0x95

    def __init__(self):
        arch = platform.machine().lower()
        numbers = {
            "x86_64": 321,
            "amd64": 321,
            "aarch64": 280,
            "arm64": 280,
        }
        if arch not in numbers:
            raise RuntimeError("unsupported architecture for bpf() syscall: %s" % arch)
        self.nr_bpf = numbers[arch]
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.syscall.restype = ctypes.c_long

    def _syscall(self, cmd, attr):
        rc = self.libc.syscall(self.nr_bpf, cmd, ctypes.byref(attr), ctypes.sizeof(attr))
        if rc < 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))
        return int(rc)

    def _program(self, protected):
        """
        protected: iterable of (major, minor, allow_boolean).

        Non-GPU devices are allowed.  Every physical NVIDIA and associated DRM
        node is explicitly matched and allowed only when it belongs to this
        job's allocation.
        """
        program = [
            insn(self.BPF_LDX_MEM_W, dst=2, src=1, off=4),  # ctx->major
            insn(self.BPF_LDX_MEM_W, dst=3, src=1, off=8),  # ctx->minor
        ]
        for major, minor, allowed in protected:
            # If major differs, skip minor test + MOV + EXIT to next tuple.
            program.append(insn(self.BPF_JMP_JNE_K, dst=2, off=3, imm=int(major)))
            # If minor differs, skip MOV + EXIT to next tuple.
            program.append(insn(self.BPF_JMP_JNE_K, dst=3, off=2, imm=int(minor)))
            program.append(insn(self.BPF_ALU64_MOV_K, dst=0, imm=1 if allowed else 0))
            program.append(insn(self.BPF_JMP_EXIT))
        # Default: allow all unrelated devices.
        program.append(insn(self.BPF_ALU64_MOV_K, dst=0, imm=1))
        program.append(insn(self.BPF_JMP_EXIT))
        return program

    def attach(self, cgroup_path, protected):
        program = self._program(protected)
        array_type = BpfInsn * len(program)
        insn_array = array_type(*program)
        license_buf = ctypes.create_string_buffer(b"GPL\0")
        log_buf = ctypes.create_string_buffer(65536)

        attr = BpfProgLoadAttr()
        attr.prog_type = self.BPF_PROG_TYPE_CGROUP_DEVICE
        attr.insn_cnt = len(program)
        attr.insns = ctypes.addressof(insn_array)
        attr.license = ctypes.addressof(license_buf)
        attr.log_level = 1
        attr.log_size = ctypes.sizeof(log_buf)
        attr.log_buf = ctypes.addressof(log_buf)
        attr.prog_name = b"pbs_gpu_dev"
        attr.expected_attach_type = self.BPF_CGROUP_DEVICE

        try:
            prog_fd = self._syscall(self.BPF_PROG_LOAD, attr)
        except OSError as exc:
            verifier = log_buf.value.decode("utf-8", "replace").strip()
            raise RuntimeError("BPF_CGROUP_DEVICE load failed: %s; verifier: %s" %
                               (exc, verifier[-4000:]))

        cgroup_fd = -1
        try:
            cgroup_fd = os.open(cgroup_path, os.O_RDONLY | os.O_DIRECTORY)
            attach = BpfProgAttachAttr()
            attach.target_fd = cgroup_fd
            attach.attach_bpf_fd = prog_fd
            attach.attach_type = self.BPF_CGROUP_DEVICE
            attach.attach_flags = 0
            self._syscall(self.BPF_PROG_ATTACH, attach)
        finally:
            if cgroup_fd >= 0:
                os.close(cgroup_fd)
            os.close(prog_fd)


# ---------------------------------------------------------------------------
# Persistent GPU state and ACLs
# ---------------------------------------------------------------------------

class GpuState(object):
    def __init__(self, cfg, pbs_home):
        self.cfg = cfg
        self.state_dir = os.path.join(pbs_home, "mom_priv", "hooks", cfg["state_subdir"])
        self.lock_file = os.path.join(self.state_dir, ".lock")
        os.makedirs(self.state_dir, mode=0o700, exist_ok=True)

    def path(self, jobid):
        return os.path.join(self.state_dir, "%s.json" % jobid)

    def load(self, jobid):
        try:
            with open(self.path(jobid), "r") as f:
                return json.load(f)
        except Exception:
            return None

    def save(self, jobid, state):
        path = self.path(jobid)
        tmp = path + ".tmp.%d" % os.getpid()
        with open(tmp, "w") as f:
            json.dump(state, f, sort_keys=True, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def delete(self, jobid):
        try:
            os.remove(self.path(jobid))
        except FileNotFoundError:
            pass

    def all(self):
        out = {}
        for path in glob.glob(os.path.join(self.state_dir, "*.json")):
            try:
                with open(path, "r") as f:
                    state = json.load(f)
                out[str(state.get("jobid", os.path.basename(path)[:-5]))] = state
            except Exception as exc:
                log(pbs.EVENT_ERROR, "cannot read GPU state %s: %s" % (path, exc))
        return out


def setfacl(path, user, add=True):
    binary = "/usr/bin/setfacl"
    if not os.path.isfile(binary):
        binary = "/bin/setfacl"
    if not os.path.isfile(binary):
        raise RuntimeError("setfacl not installed")
    if add:
        cmd = [binary, "-m", "u:%s:rwx" % user, path]
    else:
        cmd = [binary, "-x", "u:%s" % user, path]
    rc, out, err = run(cmd)
    if rc != 0:
        raise RuntimeError("setfacl failed for %s: %s" % (path, err.strip()))


# ---------------------------------------------------------------------------
# Vendor-neutral telemetry accounting and hook orchestration
# ---------------------------------------------------------------------------

def set_resource_used(job, name, value):
    """Set a resources_used value without iterating over pbs_resource."""
    try:
        job.resources_used[name] = value
    except Exception as exc:
        log(pbs.EVENT_ERROR, "cannot set resources_used.%s for %s: %s" %
            (name, job.id, exc))


class GpuHook(object):
    def __init__(self, event=None):
        self.cfg = load_config()
        self.event = event
        conf = read_pbs_conf()
        pbs_home = conf.get("PBS_MOM_HOME", conf.get("PBS_HOME", "/var/spool/pbs"))
        self.state = GpuState(self.cfg, pbs_home)
        self.jobs_root = os.path.join(os.path.realpath(self.cfg["cgroup_root"]),
                                      self.cfg["jobs_subdir"])
        self.vendor = self._local_gpu_vendor(event)
        self.runtime = self._runtime(self.vendor)

    def _runtime(self, vendor):
        if vendor == "nvidia":
            return NvidiaRuntime(self.cfg)
        if vendor == "amd":
            return AmdRuntime(self.cfg)
        return None

    def _local_gpu_vendor(self, e):
        local = local_node_names()
        try:
            vnode_list = e.vnode_list
        except Exception:
            vnode_list = None
        if vnode_list:
            try:
                items = vnode_list.items()
            except Exception:
                items = []
            for name, vnode in items:
                base = str(name).split("[")[0]
                if base not in local and base.split(".")[0] not in local:
                    continue
                try:
                    value = vnode.resources_available["gpu_vendor"]
                except Exception:
                    value = None
                if value:
                    return str(value).strip().lower()

        # Fallback for event types where vnode_list is unavailable.
        names = []
        try:
            for chunk in e.job.exec_vnode.chunks:
                if vnode_is_local(chunk.vnode_name):
                    names.append(str(chunk.vnode_name).split("[")[0])
        except Exception:
            pass
        try:
            server = pbs.server()
            for name in names:
                vnode = server.vnode(name)
                value = vnode.resources_available["gpu_vendor"]
                if value:
                    return str(value).strip().lower()
        except Exception:
            pass
        return None

    def cgroup_path(self, jobid):
        return os.path.join(self.jobs_root, str(jobid))

    def _allocated_uuids(self, exclude_jobid=None):
        used = set()
        for jobid, state in self.state.all().items():
            if exclude_jobid is not None and str(jobid) == str(exclude_jobid):
                continue
            for gpu in state.get("gpus", []):
                if gpu.get("uuid"):
                    used.add(gpu.get("uuid"))
        return used

    def _choose(self, gpus, count, used):
        free = [g for g in gpus if g["uuid"] not in used]
        if self.cfg.get("allocation") == "numa":
            free.sort(key=lambda g: (g["numa"], g["index"]))
        else:
            free.sort(key=lambda g: g["index"])
        if count > len(free):
            raise RuntimeError("requested %d GPUs, only %d physical GPUs are free" %
                               (count, len(free)))
        return free[:count]

    def _protected_devices(self, all_gpus, selected):
        selected_uuids = set(g["uuid"] for g in selected)
        entries = {}
        for gpu in all_gpus:
            allowed = gpu["uuid"] in selected_uuids
            for dev in gpu.get("devices", []):
                entries[(dev["major"], dev["minor"])] = allowed
        return [(major, minor, allowed)
                for (major, minor), allowed in sorted(entries.items())]

    def _add_drm_acls(self, user, selected):
        if not self.cfg.get("manage_drm_acl", True):
            return
        for gpu in selected:
            for dev in gpu.get("drm_devices", []):
                setfacl(dev["path"], user, add=True)

    def _remove_drm_acls(self, user, state):
        if not self.cfg.get("manage_drm_acl", True):
            return
        for gpu in state.get("gpus", []):
            for dev in gpu.get("drm_devices", []):
                try:
                    setfacl(dev["path"], user, add=False)
                except Exception as exc:
                    log(pbs.EVENT_DEBUG, "DRM ACL cleanup failed for %s: %s" %
                        (dev["path"], exc))

    def _update_telemetry(self, job, state, sample):
        """Update vendor-neutral accounting from one normalized telemetry sample."""
        uuids = [g.get("uuid") for g in state.get("gpus", []) if g.get("uuid")]
        if not uuids:
            return False
        rows = [sample[u] for u in uuids if u in sample]
        if len(rows) != len(uuids):
            missing = [u for u in uuids if u not in sample]
            log(pbs.EVENT_DEBUG, "telemetry missing GPUs for %s: %s" %
                (job.id, missing))
            return False

        now = time.time()
        util_sum = sum(float(row["util"]) for row in rows)
        mem_used = sum(float(row["mem_used"]) for row in rows)
        mem_total = sum(float(row["mem_total"]) for row in rows)
        mem_pct = 100.0 * mem_used / mem_total if mem_total > 0.0 else 0.0
        state["gpu_util_sum"] = float(state.get("gpu_util_sum", 0.0)) + util_sum
        state["gpu_samples"] = int(state.get("gpu_samples", 0)) + 1
        state["gpu_mem_peak_pct"] = max(float(state.get("gpu_mem_peak_pct", 0.0)),
                                         mem_pct)
        state["telemetry_updated"] = now
        set_resource_used(job, "gpupercent",
                          int(round(state["gpu_util_sum"] / state["gpu_samples"])))
        set_resource_used(job, "gpumemmaxpercent",
                          int(round(state["gpu_mem_peak_pct"])))

        if all(row.get("power_w") is not None for row in rows):
            power_w = sum(float(row["power_w"]) for row in rows)
            power_samples = int(state.get("gpu_power_samples", 0))
            state["gpu_power_sum_w"] = float(state.get("gpu_power_sum_w", 0.0)) + power_w
            state["gpu_power_samples"] = power_samples + 1
            previous_time = state.get("gpu_last_power_time")
            previous_power = state.get("gpu_last_power_w")
            if previous_time is None or previous_power is None:
                previous_time = float(state.get("created", now))
                previous_power = power_w
            elapsed = max(0.0, now - float(previous_time))
            state["gpu_energy_wh"] = float(state.get("gpu_energy_wh", 0.0)) + (
                0.5 * (float(previous_power) + power_w) * elapsed / 3600.0)
            state["gpu_last_power_time"] = now
            state["gpu_last_power_w"] = power_w
            set_resource_used(job, "gpupowerusageavg", int(round(
                state["gpu_power_sum_w"] / state["gpu_power_samples"])))
            set_resource_used(job, "gpuenergyconsumed",
                              int(round(state["gpu_energy_wh"])))
        return True

    def begin(self, e):
        job = e.job
        count = local_ngpus(job)
        if count > 0 and self.vendor not in ("nvidia", "amd"):
            e.reject("pbs_job_gpus: job requests GPUs but vnode gpu_vendor is not nvidia or amd")
            return False
        if count > 0 and (self.runtime is None or not self.runtime.available()):
            e.reject("pbs_job_gpus: %s GPU runtime tool is not available" % self.vendor)
            return False
        all_gpus = self.runtime.inventory() if self.runtime is not None else []
        if count > 0 and not all_gpus:
            e.reject("pbs_job_gpus: job requests GPUs but no %s GPUs are available" % self.vendor)
            return False

        cgroup_path = self.cgroup_path(job.id)
        if not os.path.isdir(cgroup_path):
            e.reject("pbs_job_gpus: job cgroup does not exist; ensure job_cgroups_v2 runs before job_gpus")
            return False

        with FileLock(self.state.lock_file):
            used = self._allocated_uuids(exclude_jobid=job.id)
            selected = self._choose(all_gpus, count, used)
            if self.cfg.get("device_isolation", True):
                protected = self._protected_devices(all_gpus, selected)
                DeviceBpf().attach(cgroup_path, protected)
            self._add_drm_acls(job.euser, selected)
            state = {
                "jobid": str(job.id), "created": time.time(),
                "euser": str(job.euser), "vendor": self.vendor,
                "gpus": selected, "gpu_util_sum": 0.0, "gpu_samples": 0,
                "gpu_mem_peak_pct": 0.0, "gpu_power_sum_w": 0.0,
                "gpu_power_samples": 0, "gpu_energy_wh": 0.0,
                "gpu_last_power_time": None, "gpu_last_power_w": None,
                "telemetry_updated": None,
            }
            self.state.save(job.id, state)

        if selected:
            for name in ("gpupercent", "gpumemmaxpercent",
                         "gpupowerusageavg", "gpuenergyconsumed"):
                set_resource_used(job, name, 0)
        log(pbs.EVENT_DEBUG, "job %s allocated %s GPUs: %s" %
            (job.id, self.vendor, [g["uuid"] for g in selected]))
        return True

    def launch(self, e):
        state = self.state.load(e.job.id)
        if state is None:
            return
        runtime = self._runtime(state.get("vendor"))
        if runtime is None:
            # Defensive hiding if state predates vendor support or vendor is unknown.
            for name in ("CUDA_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES",
                         "HIP_VISIBLE_DEVICES"):
                e.env[name] = ""
            return
        for name, value in runtime.environment(state.get("gpus", [])).items():
            e.env[name] = value

    def periodic(self, e):
        live = set(str(jobid) for jobid in e.job_list.keys())
        states = self.state.all()
        samples = {}
        if self.cfg.get("telemetry", True):
            vendors = set(state.get("vendor") for state in states.values()
                          if state.get("gpus"))
            for vendor in vendors:
                runtime = self._runtime(vendor)
                if runtime is None or not runtime.available():
                    continue
                try:
                    samples[vendor] = runtime.telemetry()
                except Exception as exc:
                    log(pbs.EVENT_ERROR, "%s GPU telemetry failed: %s" %
                        (vendor, exc))

        for jobid in e.job_list.keys():
            state = states.get(str(jobid))
            if not state or not state.get("gpus"):
                continue
            sample = samples.get(state.get("vendor"), {})
            if not sample:
                continue
            try:
                job = e.job_list[jobid]
                if self._update_telemetry(job, state, sample):
                    self.state.save(jobid, state)
            except Exception as exc:
                log(pbs.EVENT_ERROR, "GPU telemetry update failed for %s: %s" %
                    (jobid, exc))

        for jobid, state in states.items():
            if jobid in live:
                continue
            if time.time() - float(state.get("created", 0)) < 30:
                continue
            self._remove_drm_acls(state.get("euser", ""), state)
            self.state.delete(jobid)

    def epilogue(self, e):
        state = self.state.load(e.job.id)
        if state is None:
            return
        if state.get("gpus") and int(state.get("gpu_samples", 0)) == 0:
            log(pbs.EVENT_DEBUG, "job %s ended before any periodic GPU telemetry sample" %
                e.job.id)
        self._remove_drm_acls(e.job.euser, state)

    def end(self, e):
        state = self.state.load(e.job.id)
        if state is None:
            return
        self._remove_drm_acls(state.get("euser", str(e.job.euser)), state)
        self.state.delete(e.job.id)

    def resize(self, e):
        e.reject("pbs_job_gpus: dynamic job resource resizing is not supported")
        return False


def main():
    e = pbs.event()
    hook = GpuHook(e)

    result = True
    if e.type == pbs.EXECHOST_PERIODIC:
        result = hook.periodic(e)
    elif e.type == pbs.EXECJOB_BEGIN:
        result = hook.begin(e)
    elif e.type == pbs.EXECJOB_LAUNCH:
        result = hook.launch(e)
    elif e.type == pbs.EXECJOB_EPILOGUE:
        result = hook.epilogue(e)
    elif e.type == pbs.EXECJOB_END:
        result = hook.end(e)
    elif hasattr(pbs, "EXECJOB_ABORT") and e.type == pbs.EXECJOB_ABORT:
        result = hook.end(e)
    elif hasattr(pbs, "EXECJOB_RESIZE") and e.type == pbs.EXECJOB_RESIZE:
        result = hook.resize(e)
    if result is not False:
        e.accept()


try:
    main()
except SystemExit:
    raise
except Exception as exc:
    log(pbs.EVENT_ERROR, "%s\n%s" % (exc, traceback.format_exc()))
    # GPU allocation/isolation failures are fatal. Telemetry failures are caught
    # inside the periodic path and never reach this level.
    try:
        pbs.event().reject("pbs_job_gpus failed: %s" % exc)
    except Exception:
        pass
