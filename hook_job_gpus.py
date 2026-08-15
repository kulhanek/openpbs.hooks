# coding: utf-8
"""
OpenPBS job hook: whole NVIDIA GPU allocation, cgroup-v2 device
isolation, CUDA environment setup, and DCGM accounting.

Scope
-----
* Physical NVIDIA GPUs only.  MIG and MIC are intentionally unsupported.
* GPU allocation is immutable for the lifetime of a job.
* CPU/memory/cgroup creation is owned by hook_job_cgroups_v2.py.
* This hook attaches a BPF_CGROUP_DEVICE policy to that existing job cgroup.
* DCGM failures are accounting failures and are non-fatal.  GPU allocation or
  device-isolation failures are fatal and reject the job.

PBS resources expected for DCGM accounting
------------------------------------------
    create resource gpupercent
    set resource gpupercent type = long
    set resource gpupercent flag = r

    create resource gpumemmaxpercent
    set resource gpumemmaxpercent type = long
    set resource gpumemmaxpercent flag = r

    create resource gpupowerusage
    set resource gpupowerusage type = float
    set resource gpupowerusage flag = r

Events to enable
----------------
    exechost_periodic, execjob_begin, execjob_launch,
    execjob_epilogue, execjob_end, execjob_abort, execjob_resize

For execjob_begin this hook must run AFTER hook_job_cgroups_v2.py, because the job
cgroup must already exist before the BPF device program is attached.
"""

import ctypes
import errno
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
    "nvidia_smi": "/usr/bin/nvidia-smi",
    "dcgmi": "/usr/bin/dcgmi",
    "device_isolation": True,
    "manage_drm_acl": True,
    "enable_dcgm": True,
    "periodic_dcgm_update": True,
    "allocation": "index",          # index | numa
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
            if "ngpus" in chunk.chunk_resources:
                total += int(chunk.chunk_resources["ngpus"])
        except Exception:
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
# NVIDIA GPU discovery
# ---------------------------------------------------------------------------

class NvidiaDiscovery(object):
    def __init__(self, cfg):
        self.cfg = cfg

    def available(self):
        return os.path.isfile(self.cfg["nvidia_smi"])

    def _numa_node(self, bus_id):
        path = os.path.join("/sys/bus/pci/devices", bus_id.lower(), "numa_node")
        try:
            value = int(open(path, "r").read().strip())
            return 0 if value < 0 else value
        except Exception:
            return 0

    def _drm_devices(self, bus_id):
        result = []
        bus_id = bus_id.lower()
        for cls in ("card[0-9]*", "renderD[0-9]*"):
            for path in glob.glob(os.path.join("/sys/class/drm", cls)):
                if bus_id not in os.path.realpath(path).lower():
                    continue
                info = dev_info(os.path.join("/dev/dri", os.path.basename(path)))
                if info and info not in result:
                    result.append(info)
        return result

    def discover(self):
        if not self.available():
            return []
        cmd = [self.cfg["nvidia_smi"],
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
            # nvidia-smi usually returns 00000000:BB:DD.F; sysfs uses 0000:BB:DD.F.
            bus = parts[2].lower()
            fields = bus.split(":")
            if len(fields) == 3 and len(fields[0]) == 8:
                bus = fields[0][-4:] + ":" + fields[1] + ":" + fields[2]
            memory = int(float(parts[3]) * 1024 * 1024)
            ndev = dev_info("/dev/nvidia%d" % index)
            if ndev is None:
                raise RuntimeError("GPU %d has no /dev/nvidia%d device" % (index, index))
            gpus.append({
                "index": index,
                "uuid": uuid,
                "pci_bus_id": bus,
                "numa": self._numa_node(bus),
                "memory": memory,
                "nvidia_device": ndev,
                "drm_devices": self._drm_devices(bus),
            })
        gpus.sort(key=lambda g: g["index"])
        return gpus


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
# DCGM accounting
# ---------------------------------------------------------------------------

class DcgmAccounting(object):
    GROUP_RE = re.compile(r"group ID of ([0-9]+)\s*$")

    def __init__(self, cfg):
        self.cfg = cfg

    def available(self):
        return (self.cfg.get("enable_dcgm", True)
                and os.path.isfile(self.cfg["dcgmi"]))

    def _dcgm_ids(self):
        """Return UUID -> DCGM GPU entity ID from `dcgmi discovery -l`."""
        rc, out, err = run([self.cfg["dcgmi"], "discovery", "-l"])
        if rc != 0:
            raise RuntimeError("dcgmi discovery failed: %s" % err.strip())
        result = {}
        current_id = None
        for line in out.splitlines():
            cols = line.split("|")
            if len(cols) != 4:
                continue
            left = cols[1].strip()
            field = cols[2].strip()
            if field.startswith("Name:"):
                try:
                    current_id = int(left)
                except Exception:
                    current_id = None
            elif field.startswith("Device UUID:") and current_id is not None:
                result[field.split(":", 1)[1].strip()] = current_id
        return result

    def start(self, jobid, gpus):
        if not self.available() or not gpus:
            return None
        name = "pbs_" + re.sub(r"[^A-Za-z0-9_.-]", "_", str(jobid))
        rc, out, err = run([self.cfg["dcgmi"], "group", "-c", name])
        if rc != 0:
            raise RuntimeError("cannot create DCGM group: %s" % err.strip())
        groupid = None
        for line in out.splitlines():
            m = self.GROUP_RE.search(line)
            if m:
                groupid = int(m.group(1))
                break
        if groupid is None:
            raise RuntimeError("cannot parse DCGM group ID from: %s" % out.strip())

        try:
            mapping = self._dcgm_ids()
            for gpu in gpus:
                if gpu["uuid"] not in mapping:
                    raise RuntimeError("GPU UUID %s absent from DCGM discovery" % gpu["uuid"])
                run([self.cfg["dcgmi"], "group", "-g", str(groupid),
                     "-a", str(mapping[gpu["uuid"]])], check=True)
                gpu["dcgm_id"] = mapping[gpu["uuid"]]
            # Enabling stats is global/idempotent in DCGM.
            run([self.cfg["dcgmi"], "stats", "-e"])
            run([self.cfg["dcgmi"], "stats", "-g", str(groupid),
                 "-s", str(jobid)], check=True)
        except Exception:
            run([self.cfg["dcgmi"], "group", "-d", str(groupid)])
            raise
        return groupid

    def collect(self, job, state):
        if not self.available() or state.get("dcgm_group_id") is None:
            return
        rc, out, err = run([self.cfg["dcgmi"], "stats", "-j", str(job.id), "-v"])
        if rc != 0:
            raise RuntimeError("dcgmi stats failed: %s" % err.strip())

        gpupercent = 0
        total_max_mem = 0
        total_power_avg = 0.0
        for line in out.splitlines():
            cols = line.split("|")
            if len(cols) != 4:
                continue
            name = cols[1].strip()
            value = cols[2].strip()
            if name.startswith("SM Utilization"):
                m = re.search(r"Avg:\s*([0-9]+(?:\.[0-9]+)?)", value)
                if m:
                    gpupercent += int(round(float(m.group(1))))
            elif name.startswith("Max GPU Memory Used"):
                m = re.search(r"([0-9]+)", value)
                if m:
                    total_max_mem += int(m.group(1))
            elif name.startswith("Power Usage"):
                m = re.search(r"Avg:\s*([0-9]+(?:\.[0-9]+)?)", value)
                if m:
                    total_power_avg += float(m.group(1))

        total_mem = sum(int(g.get("memory", 0)) for g in state.get("gpus", []))
        mem_pct = int(round(100.0 * total_max_mem / total_mem)) if total_mem > 0 else 0
        elapsed_h = max(0.0, time.time() - float(state.get("dcgm_started_at", time.time()))) / 3600.0
        energy_wh = total_power_avg * elapsed_h

        job.resources_used["gpupercent"] = gpupercent
        job.resources_used["gpumemmaxpercent"] = mem_pct
        job.resources_used["gpupowerusage"] = energy_wh

    def stop(self, jobid, state):
        if not self.available() or state.get("dcgm_group_id") is None:
            return
        groupid = int(state["dcgm_group_id"])
        run([self.cfg["dcgmi"], "stats", "-x", str(jobid)])
        run([self.cfg["dcgmi"], "stats", "-r", str(jobid)])
        run([self.cfg["dcgmi"], "group", "-d", str(groupid)])


# ---------------------------------------------------------------------------
# Hook orchestration
# ---------------------------------------------------------------------------

class GpuHook(object):
    def __init__(self):
        self.cfg = load_config()
        conf = read_pbs_conf()
        pbs_home = conf.get("PBS_MOM_HOME", conf.get("PBS_HOME", "/var/spool/pbs"))
        self.state = GpuState(self.cfg, pbs_home)
        self.discovery = NvidiaDiscovery(self.cfg)
        self.dcgm = DcgmAccounting(self.cfg)
        self.jobs_root = os.path.join(os.path.realpath(self.cfg["cgroup_root"]),
                                      self.cfg["jobs_subdir"])

    def cgroup_path(self, jobid):
        return os.path.join(self.jobs_root, str(jobid))

    def _allocated_uuids(self, exclude_jobid=None):
        used = set()
        for jobid, state in self.state.all().items():
            if exclude_jobid is not None and str(jobid) == str(exclude_jobid):
                continue
            for gpu in state.get("gpus", []):
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
            devices = [gpu["nvidia_device"]] + list(gpu.get("drm_devices", []))
            for dev in devices:
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
                    log(pbs.EVENT_DEBUG, "DRM ACL cleanup failed for %s: %s" % (dev["path"], exc))

    def begin(self, e):
        job = e.job
        count = local_ngpus(job)
        all_gpus = self.discovery.discover() if self.discovery.available() else []
        if count > 0 and not all_gpus:
            e.reject("pbs_job_gpus: job requests GPUs but no NVIDIA GPUs are available")
            return False

        cgroup_path = self.cgroup_path(job.id)
        if not os.path.isdir(cgroup_path):
            e.reject("pbs_job_gpus: job cgroup does not exist; ensure hook_job_cgroups_v2 runs before hook_job_gpus")
            return False

        with FileLock(self.state.lock_file):
            used = self._allocated_uuids(exclude_jobid=job.id)
            selected = self._choose(all_gpus, count, used)

            if self.cfg.get("device_isolation", True):
                protected = self._protected_devices(all_gpus, selected)
                DeviceBpf().attach(cgroup_path, protected)

            self._add_drm_acls(job.euser, selected)

            state = {
                "jobid": str(job.id),
                "created": time.time(),
                "euser": str(job.euser),
                "gpus": selected,
                "dcgm_group_id": None,
                "dcgm_started_at": None,
            }

            # Accounting failure must not fail the job.
            if selected and self.dcgm.available():
                try:
                    groupid = self.dcgm.start(job.id, selected)
                    state["dcgm_group_id"] = groupid
                    state["dcgm_started_at"] = time.time()
                except Exception as exc:
                    log(pbs.EVENT_ERROR, "DCGM startup failed for %s: %s" % (job.id, exc))

            self.state.save(job.id, state)

        log(pbs.EVENT_DEBUG, "job %s allocated GPUs: %s" %
            (job.id, [g["uuid"] for g in selected]))
        return True

    def launch(self, e):
        state = self.state.load(e.job.id)
        if state is None:
            return
        uuids = [g["uuid"] for g in state.get("gpus", [])]
        # Explicit empty value prevents accidental use of an unallocated GPU.
        e.env["CUDA_VISIBLE_DEVICES"] = "\\,".join(uuids) if uuids else ""
        if uuids:
            e.env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"

    def _collect_nonfatal(self, job, state):
        if state is None:
            return
        try:
            self.dcgm.collect(job, state)
        except Exception as exc:
            log(pbs.EVENT_ERROR, "DCGM collection failed for %s: %s" % (job.id, exc))

    def periodic(self, e):
        live = set(str(x) for x in e.job_list.keys())
        states = self.state.all()
        if self.cfg.get("periodic_dcgm_update", True):
            for jobid, job in e.job_list.items():
                state = states.get(str(jobid))
                if state:
                    self._collect_nonfatal(job, state)

        # Clean stale GPU/DCGM state.  Device BPF state is tied to the cgroup
        # and disappears when the CPU hook deletes the cgroup.
        for jobid, state in states.items():
            if jobid in live:
                continue
            if time.time() - float(state.get("created", 0)) < 30:
                continue
            try:
                self.dcgm.stop(jobid, state)
            except Exception as exc:
                log(pbs.EVENT_ERROR, "DCGM orphan cleanup failed for %s: %s" % (jobid, exc))
            self._remove_drm_acls(state.get("euser", ""), state)
            self.state.delete(jobid)

    def epilogue(self, e):
        state = self.state.load(e.job.id)
        if state is None:
            return
        self._collect_nonfatal(e.job, state)
        try:
            self.dcgm.stop(e.job.id, state)
        except Exception as exc:
            log(pbs.EVENT_ERROR, "DCGM stop failed for %s: %s" % (e.job.id, exc))
        state["dcgm_group_id"] = None
        self.state.save(e.job.id, state)
        self._remove_drm_acls(e.job.euser, state)

    def end(self, e):
        state = self.state.load(e.job.id)
        if state is None:
            return
        # Defensive cleanup for paths where epilogue did not run.
        if state.get("dcgm_group_id") is not None:
            try:
                self._collect_nonfatal(e.job, state)
                self.dcgm.stop(e.job.id, state)
            except Exception as exc:
                log(pbs.EVENT_ERROR, "DCGM final cleanup failed for %s: %s" % (e.job.id, exc))
        self._remove_drm_acls(state.get("euser", str(e.job.euser)), state)
        self.state.delete(e.job.id)

    def resize(self, e):
        e.reject("pbs_job_gpus: dynamic job resource resizing is not supported")
        return False


def main():
    e = pbs.event()
    hook = GpuHook()

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
    # GPU allocation/isolation failures are fatal.  DCGM paths catch their own
    # exceptions before they reach this level.
    try:
        pbs.event().reject("pbs_job_gpus failed: %s" % exc)
    except Exception:
        pass
