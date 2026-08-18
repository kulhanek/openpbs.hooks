# coding: utf-8
"""
OpenPBS job hook: cgroup v2 CPU and memory management.

Design goals
------------
* cgroup v2 only.
* resources_available.ncpus is published by hook_discovery_cpus and is
  the number of PHYSICAL cores.
* A normal ncpus=N allocation reserves N whole physical cores and exposes
  one logical CPU from each selected core.
* If the job requests ``smt=true``, all online SMT siblings of
  each selected physical core are added to the cpuset.  Hybrid/mixed cores are
  allowed, so the resulting number of logical CPUs is topology-dependent.
* resources_used.nthreads reports the actual number of logical CPUs in the
  job cpuset.
* Mixed SMT/non-SMT and hybrid processors are supported.  CPU numbering may
  be grouped or interleaved; topology is derived from sysfs sibling masks.
* The whole physical core is reserved internally even when only its primary
  logical CPU is exposed to the job.
* This hook owns the job cgroup lifetime.  A separate GPU hook may attach a
  BPF device policy to the job cgroup, but must not create/delete it.
* Dynamic resource resizing is deliberately unsupported.

Required PBS resource definition
--------------------------------
    create resource smt
    set resource smt type = boolean
    set resource smt flag = h

Systemd/cgroup requirement
--------------------------
The pbs_mom systemd unit must delegate its cgroup and keep the unit cgroup
itself free of processes so that domain controllers can be delegated to job
children.  On systemd >= 254 this is conveniently achieved with:

    [Service]
    Delegate=yes
    DelegateSubgroup=mom

The default delegated unit path used below is:
    /sys/fs/cgroup/system.slice/pbs-mom.service

and job cgroups are created under:
    .../pbs_jobs/<jobid>

Events to enable
----------------
    exechost_startup, exechost_periodic,
    execjob_begin, execjob_launch, execjob_attach,
    execjob_epilogue, execjob_end, execjob_abort, execjob_resize

hook_job_cgroups_v2 must run before hook_job_gpus for execjob_begin.
"""

import errno
import fcntl
import glob
import json
import os
import re
import signal
import time
import traceback

import pbs


# ---------------------------------------------------------------------------
# Configuration and small helpers
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "cgroup_root": "/sys/fs/cgroup/system.slice/pbs-mom.service",
    "jobs_subdir": "pbs_jobs",
    "state_subdir": "cgroup_v2",
    "placement": "packed",          # packed | balanced
    "memory_default": "400MB",
    "publish_vmem": True,
    "periodic_usage_update": True,
    "kill_timeout": 10,
    "cpu_weight": 100,
}


def log(level, msg):
    pbs.logmsg(level, "pbs_job_cgroups_v2: " + str(msg))


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


def parse_cpu_list(value):
    """Parse Linux cpulist syntax such as '0-3,8,10-11'."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return sorted(set(int(x) for x in value))
    result = []
    for part in str(value).strip().split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            result.extend(range(int(a), int(b) + 1))
        else:
            result.append(int(part))
    return sorted(set(result))


def format_cpu_list(values):
    vals = sorted(set(int(x) for x in values))
    if not vals:
        return ""
    ranges = []
    start = prev = vals[0]
    for value in vals[1:]:
        if value == prev + 1:
            prev = value
            continue
        ranges.append(str(start) if start == prev else "%d-%d" % (start, prev))
        start = prev = value
    ranges.append(str(start) if start == prev else "%d-%d" % (start, prev))
    return ",".join(ranges)


_SIZE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([kmgtpe]?)(?:i?b)?\s*$", re.I)


def size_to_bytes(value):
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if not s:
        return 0
    # pbs.size string representations are normally e.g. 1024kb/4gb.
    m = _SIZE_RE.match(s)
    if not m:
        # Bare value used by PBS may be in bytes.
        try:
            return int(s)
        except Exception:
            raise ValueError("invalid size: %s" % value)
    number = float(m.group(1))
    unit = m.group(2).lower()
    power = {"": 0, "k": 1, "m": 2, "g": 3, "t": 4, "p": 5, "e": 6}[unit]
    return int(number * (1024 ** power))


def bytes_to_pbs_size(value):
    value = max(0, int(value))
    return pbs.size("%db" % value)


def read_text(path, default=None):
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except Exception:
        return default


def write_text(path, value):
    with open(path, "w") as f:
        f.write(str(value))


def resource_truth(value):
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "enabled")


def resource_string_array(value):
    """Return a PBS string_array-like value as a list of strings."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(x).strip() for x in value if str(x).strip()]

    # Some PBS string_array objects are iterable, while others are best
    # represented by their comma-separated string form.  Support both.
    if not isinstance(value, str):
        try:
            items = [str(x).strip() for x in value]
            if items:
                return [x for x in items if x]
        except Exception:
            pass

    text = str(value).strip()
    if not text:
        return []
    text = text.strip("[]{}()")
    return [x.strip().strip("\"'") for x in re.split(r"[,\s]+", text)
            if x.strip().strip("\"'")]


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


def validate_local_vnode_cgroups(job):
    """Require every local vnode allocated to the job to advertise cgroups=v2."""
    vnode_names = []
    try:
        chunks = job.exec_vnode.chunks
    except Exception:
        chunks = []

    for chunk in chunks:
        name = str(chunk.vnode_name)
        if vnode_is_local(name) and name not in vnode_names:
            vnode_names.append(name)

    if not vnode_names:
        raise RuntimeError("cannot determine local vnode from exec_vnode")

    server = pbs.server()
    for name in vnode_names:
        try:
            vnode = server.vnode(name)
            if vnode is None:
                raise RuntimeError("vnode not found")
            value = vnode.resources_available["cgroups"]
            cgroups = resource_string_array(value)
        except Exception as exc:
            raise RuntimeError(
                "cannot read resources_available.cgroups for vnode %s: %s" %
                (name, exc)
            )

        if "v2" not in [x.lower() for x in cgroups]:
            raise RuntimeError(
                "vnode %s does not support cgroup v2: "
                "resources_available.cgroups=%r" % (name, cgroups)
            )


def requested_smt(job):
    """
    Return the job-wide SMT request from schedselect.

    Cross-chunk consistency is validated by hook_job_enqueued at queue time.
    This execution-side parser is deliberately tolerant for already queued or
    otherwise legacy jobs: SMT is enabled if at least one chunk explicitly
    requests smt=true.
    """
    try:
        select = job.schedselect
        if select is None:
            return False
        select = str(select)
    except Exception as exc:
        log(pbs.EVENT_DEBUG,
            "job %s: cannot read schedselect: %s" %
            (job.id, exc))
        return False

    for chunk in select.split("+"):
        for part in chunk.split(":"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            if key.strip().lower() == "smt" and resource_truth(value):
                return True

    return False

def local_job_resources(job):
    """Return resources assigned by exec_vnode and select to this MoM."""
    result = {}

    try:
        chunks = job.exec_vnode.chunks
    except Exception:
        chunks = []

    for chunk in chunks:
        if not vnode_is_local(chunk.vnode_name):
            continue

        r = chunk.chunk_resources

        try:
            value = r["ncpus"]
            if value is not None:
                result["ncpus"] = int(result.get("ncpus", 0)) + int(value)
        except Exception:
            pass

        try:
            value = r["mem"]
            if value is not None:
                result["mem"] = int(result.get("mem", 0)) + size_to_bytes(value)
        except Exception:
            pass

        try:
            value = r["vmem"]
            if value is not None:
                result["vmem"] = int(result.get("vmem", 0)) + size_to_bytes(value)
        except Exception:
            pass

    result["smt"] = requested_smt(job)

    return result

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
# CPU topology
# ---------------------------------------------------------------------------

class CpuTopology(object):
    """Physical-core topology derived entirely from sysfs."""

    SYS_CPU = "/sys/devices/system/cpu"

    def __init__(self):
        self.online = set(parse_cpu_list(read_text(os.path.join(self.SYS_CPU, "online"), "")))
        if not self.online:
            self.online = set(
                int(os.path.basename(p)[3:])
                for p in glob.glob(os.path.join(self.SYS_CPU, "cpu[0-9]*"))
            )
        self.cores = self._discover_cores()

    def _numa_node(self, cpu):
        matches = glob.glob(os.path.join(self.SYS_CPU, "cpu%d" % cpu, "node[0-9]*"))
        if matches:
            try:
                return int(os.path.basename(matches[0])[4:])
            except Exception:
                pass
        return 0

    def _int_file(self, cpu, name, default=-1):
        value = read_text(os.path.join(self.SYS_CPU, "cpu%d" % cpu, "topology", name))
        try:
            return int(value)
        except Exception:
            return default

    def _discover_cores(self):
        seen = set()
        cores = []
        for cpu in sorted(self.online):
            sibling_path = os.path.join(self.SYS_CPU, "cpu%d" % cpu,
                                        "topology", "core_cpus_list")
            siblings = parse_cpu_list(read_text(sibling_path, ""))
            if not siblings:
                sibling_path = os.path.join(self.SYS_CPU, "cpu%d" % cpu,
                                            "topology", "thread_siblings_list")
                siblings = parse_cpu_list(read_text(sibling_path, str(cpu)))
            siblings = tuple(sorted(set(siblings) & self.online))
            if not siblings or siblings in seen:
                continue
            seen.add(siblings)

            representative = siblings[0]
            cores.append({
                "key": format_cpu_list(siblings),
                "threads": list(siblings),
                "primary": min(siblings),
                "package": self._int_file(representative, "physical_package_id", 0),
                "die": self._int_file(representative, "die_id", 0),
                "core_id": self._int_file(representative, "core_id", representative),
                "numa": self._numa_node(representative),
            })
        if not cores:
            raise RuntimeError("no usable physical CPU cores discovered")
        return cores

    def core_by_key(self):
        return dict((c["key"], c) for c in self.cores)

    def numa_nodes(self):
        return sorted(set(c["numa"] for c in self.cores))

    def select(self, count, reserved_keys, placement="packed"):
        count = int(count)
        free = [c for c in self.cores if c["key"] not in reserved_keys]
        if count > len(free):
            raise RuntimeError("requested %d physical cores, only %d are free" %
                               (count, len(free)))

        by_numa = {}
        for core in free:
            by_numa.setdefault(core["numa"], []).append(core)
        for node in by_numa:
            by_numa[node].sort(key=lambda c: c["primary"])

        selected = []
        if placement == "balanced":
            nodes = sorted(by_numa)
            while len(selected) < count:
                progressed = False
                for node in nodes:
                    if by_numa[node]:
                        selected.append(by_numa[node].pop(0))
                        progressed = True
                        if len(selected) == count:
                            break
                if not progressed:
                    break
        else:
            # Prefer as few NUMA nodes as possible: fullest nodes first.
            nodes = sorted(by_numa, key=lambda n: (-len(by_numa[n]), n))
            for node in nodes:
                take = min(count - len(selected), len(by_numa[node]))
                selected.extend(by_numa[node][:take])
                if len(selected) == count:
                    break

        if len(selected) != count:
            raise RuntimeError("unable to allocate requested physical cores")
        return selected


# ---------------------------------------------------------------------------
# cgroup v2 manager
# ---------------------------------------------------------------------------

class CgroupV2(object):
    def __init__(self, cfg, pbs_home):
        self.cfg = cfg
        self.unit_root = os.path.realpath(cfg["cgroup_root"])
        self.jobs_root = os.path.join(self.unit_root, cfg["jobs_subdir"])
        self.state_dir = os.path.join(pbs_home, "mom_priv", "hooks", cfg["state_subdir"])
        self.lock_file = os.path.join(self.state_dir, ".lock")
        if not os.path.isdir(self.state_dir):
            os.makedirs(self.state_dir, mode=0o700, exist_ok=True)

    def state_path(self, jobid):
        return os.path.join(self.state_dir, "%s.json" % jobid)

    def cgroup_path(self, jobid):
        return os.path.join(self.jobs_root, str(jobid))

    def load_state(self, jobid):
        try:
            with open(self.state_path(jobid), "r") as f:
                return json.load(f)
        except Exception:
            return None

    def save_state(self, jobid, state):
        path = self.state_path(jobid)
        tmp = path + ".tmp.%d" % os.getpid()
        with open(tmp, "w") as f:
            json.dump(state, f, sort_keys=True, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def delete_state(self, jobid):
        try:
            os.remove(self.state_path(jobid))
        except FileNotFoundError:
            pass

    def all_states(self):
        states = {}
        for path in glob.glob(os.path.join(self.state_dir, "*.json")):
            try:
                with open(path, "r") as f:
                    state = json.load(f)
                jobid = state.get("jobid") or os.path.basename(path)[:-5]
                states[str(jobid)] = state
            except Exception as exc:
                log(pbs.EVENT_ERROR, "cannot read state %s: %s" % (path, exc))
        return states

    def _write_subtree_control(self, path, controllers):
        available = set((read_text(os.path.join(path, "cgroup.controllers"), "") or "").split())
        wanted = [c for c in controllers if c in available]
        if not wanted:
            return
        control = os.path.join(path, "cgroup.subtree_control")
        current = set((read_text(control, "") or "").split())
        missing = [c for c in wanted if c not in current]
        if missing:
            try:
                write_text(control, " ".join("+" + c for c in missing))
            except OSError as exc:
                if exc.errno in (errno.EBUSY, errno.EPERM):
                    procs = read_text(os.path.join(path, "cgroup.procs"), "")
                    raise RuntimeError(
                        "cannot delegate cgroup v2 controllers below %s: %s; "
                        "cgroup.procs=%r. Ensure pbs-mom.service has Delegate=yes "
                        "and DelegateSubgroup=mom" % (path, exc, procs))
                raise

    def setup_hierarchy(self):
        if read_text("/sys/fs/cgroup/cgroup.controllers") is None:
            raise RuntimeError("unified cgroup v2 hierarchy is not mounted")
        if not os.path.isdir(self.unit_root):
            raise RuntimeError("delegated cgroup root does not exist: %s" % self.unit_root)

        controllers = ["cpu", "cpuset", "memory"]
        self._write_subtree_control(self.unit_root, controllers)
        os.makedirs(self.jobs_root, mode=0o755, exist_ok=True)

        # Initialize the intermediate cpuset from its effective parent values.
        for name in ("cpuset.mems", "cpuset.cpus"):
            path = os.path.join(self.jobs_root, name)
            if os.path.isfile(path) and not read_text(path, ""):
                eff = read_text(os.path.join(self.unit_root, name + ".effective"), "")
                if eff:
                    write_text(path, eff)
        self._write_subtree_control(self.jobs_root, controllers)

    def create_job(self, jobid, cpus, mems, mem_bytes, vmem_bytes, cpu_weight):
        path = self.cgroup_path(jobid)
        if os.path.isdir(path):
            self.delete_job(jobid, force=True)
        os.mkdir(path, 0o755)

        # cpuset requires mems before cpus on many systems.
        if os.path.isfile(os.path.join(path, "cpuset.mems")):
            write_text(os.path.join(path, "cpuset.mems"), format_cpu_list(mems))
        if os.path.isfile(os.path.join(path, "cpuset.cpus")):
            write_text(os.path.join(path, "cpuset.cpus"), format_cpu_list(cpus))

        if os.path.isfile(os.path.join(path, "cpu.weight")):
            write_text(os.path.join(path, "cpu.weight"), max(1, min(10000, int(cpu_weight))))
        if os.path.isfile(os.path.join(path, "cpu.max")):
            write_text(os.path.join(path, "cpu.max"), "max 100000")

        if os.path.isfile(os.path.join(path, "memory.max")):
            write_text(os.path.join(path, "memory.max"), "max" if mem_bytes <= 0 else int(mem_bytes))
        if os.path.isfile(os.path.join(path, "memory.swap.max")):
            if vmem_bytes > 0 and mem_bytes > 0:
                swap = max(0, int(vmem_bytes) - int(mem_bytes))
                write_text(os.path.join(path, "memory.swap.max"), swap)
            else:
                write_text(os.path.join(path, "memory.swap.max"), "max")

    def attach_pid(self, jobid, pid):
        path = os.path.join(self.cgroup_path(jobid), "cgroup.procs")
        if not os.path.isfile(path):
            raise RuntimeError("job cgroup does not exist for %s" % jobid)
        write_text(path, int(pid))

    def attach_session(self, jobid, pid):
        pids = []
        try:
            sid = os.getsid(pid)
        except OSError:
            sid = -1
        if sid > 1:
            for stat_path in glob.glob("/proc/[0-9]*/stat"):
                try:
                    with open(stat_path, "r") as f:
                        line = f.read()
                    # comm may contain spaces/parentheses; parse after final ')'.
                    rparen = line.rfind(")")
                    head = line[:line.find("(")].strip()
                    rest = line[rparen + 2:].split()
                    proc_pid = int(head)
                    proc_sid = int(rest[3])  # fields after comm: state,ppid,pgrp,session
                    if proc_sid == sid:
                        pids.append(proc_pid)
                except Exception:
                    pass
        if not pids:
            pids = [pid]
        for proc_pid in sorted(set(pids)):
            try:
                self.attach_pid(jobid, proc_pid)
            except FileNotFoundError:
                pass

    def usage(self, jobid):
        path = self.cgroup_path(jobid)
        result = {}
        if not os.path.isdir(path):
            return result

        cpu_stat = read_text(os.path.join(path, "cpu.stat"), "") or ""
        for line in cpu_stat.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].isdigit():
                result[parts[0]] = int(parts[1])

        for key, filename in (
            ("memory_current", "memory.current"),
            ("memory_peak", "memory.peak"),
            ("swap_current", "memory.swap.current"),
            ("swap_peak", "memory.swap.peak"),
        ):
            value = read_text(os.path.join(path, filename))
            if value is not None and str(value).isdigit():
                result[key] = int(value)
        return result

    def _kill_cgroup(self, path):
        killfile = os.path.join(path, "cgroup.kill")
        if os.path.isfile(killfile):
            try:
                write_text(killfile, "1")
            except Exception:
                pass
        else:
            procs = read_text(os.path.join(path, "cgroup.procs"), "") or ""
            for value in procs.splitlines():
                try:
                    os.kill(int(value), signal.SIGKILL)
                except Exception:
                    pass

    def delete_job(self, jobid, force=False):
        path = self.cgroup_path(jobid)
        if not os.path.isdir(path):
            return
        if force:
            self._kill_cgroup(path)
        deadline = time.time() + float(self.cfg["kill_timeout"])
        while time.time() < deadline:
            procs = read_text(os.path.join(path, "cgroup.procs"), "") or ""
            if not procs.strip():
                break
            if not force:
                self._kill_cgroup(path)
                force = True
            time.sleep(0.05)
        try:
            os.rmdir(path)
        except OSError as exc:
            if exc.errno not in (errno.ENOENT,):
                raise


# ---------------------------------------------------------------------------
# Job allocation/accounting
# ---------------------------------------------------------------------------

class CpuCgroupHook(object):
    def __init__(self):
        self.cfg = load_config()
        conf = read_pbs_conf()
        self.pbs_home = conf.get("PBS_MOM_HOME", conf.get("PBS_HOME", "/var/spool/pbs"))
        self.cg = CgroupV2(self.cfg, self.pbs_home)

    def topology(self):
        return CpuTopology()

    def startup(self, e):
        # Resource publication belongs to hook_discovery_cpus.  This startup
        # event only verifies/initializes the delegated cgroup-v2 hierarchy.
        self.cg.setup_hierarchy()
        log(pbs.EVENT_DEBUG, "cgroup v2 hierarchy initialized")

    def _reserved_core_keys(self, exclude_jobid=None):
        reserved = set()
        for jobid, state in self.cg.all_states().items():
            if exclude_jobid is not None and str(jobid) == str(exclude_jobid):
                continue
            reserved.update(state.get("core_keys", []))
        return reserved

    def begin(self, e):
        job = e.job

        try:
            validate_local_vnode_cgroups(job)
        except Exception as exc:
            e.reject("pbs_job_cgroups_v2: %s" % exc)
            return False

        resc = local_job_resources(job)
        ncpus = int(resc.get("ncpus", 0))
        if ncpus <= 0:
            e.reject("pbs_job_cgroups_v2: jobs must allocate at least one physical CPU core on this host")
            return False

        self.cg.setup_hierarchy()
        topo = self.topology()
        smt = resource_truth(resc.get("smt", False))

        with FileLock(self.cg.lock_file):
            reserved = self._reserved_core_keys(exclude_jobid=job.id)
            selected = topo.select(ncpus, reserved, self.cfg.get("placement", "packed"))
            cpus = []
            for core in selected:
                if smt:
                    cpus.extend(core["threads"])
                else:
                    cpus.append(core["primary"])
            mems = sorted(set(core["numa"] for core in selected)) or topo.numa_nodes()

            mem = int(resc.get("mem", 0))
            if mem <= 0:
                mem = size_to_bytes(self.cfg.get("memory_default", "0B"))
            vmem = int(resc.get("vmem", 0))
            if vmem > 0 and mem > 0 and vmem < mem:
                vmem = mem

            self.cg.create_job(job.id, cpus, mems, mem, vmem,
                               self.cfg.get("cpu_weight", 100))
            state = {
                "jobid": str(job.id),
                "created": time.time(),
                "ncpus": ncpus,
                "smt": smt,
                "core_keys": [c["key"] for c in selected],
                "cpus": sorted(set(cpus)),
                "nthreads": len(set(cpus)),
                "mems": mems,
                "mem": mem,
                "vmem": vmem,
                "last_usage_usec": 0,
                "last_usage_time": time.time(),
            }
            self.cg.save_state(job.id, state)

        self.update_usage(job.id, job.resources_used, force=True)
        log(pbs.EVENT_DEBUG,
            "job %s: ncpus=%d physical cores, nthreads=%d, cpuset=%s, smt=%s" %
            (job.id, ncpus, len(set(cpus)), format_cpu_list(cpus), smt))
        return True

    def launch(self, e):
        # Match the historical hook: parent of pbs_python/hook process is the
        # process being launched by MoM.  Attach its whole session.
        self.cg.attach_session(e.job.id, os.getppid())

    def attach(self, e):
        self.cg.attach_session(e.job.id, int(e.pid))

    def update_usage(self, jobid, resources_used, force=False):
        state = self.cg.load_state(jobid) or {}

        # These values describe the actual CPU placement chosen by this hook
        # and should be available in resources_used even if cgroup usage files
        # are temporarily unavailable.
        try:
            resources_used["nthreads"] = int(state.get("nthreads", len(state.get("cpus", []))))
        except Exception:
            pass
        try:
            resources_used["smt"] = bool(state.get("smt", False))
        except Exception:
            pass

        usage = self.cg.usage(jobid)
        if not usage:
            return

        usage_usec = int(usage.get("usage_usec", 0))
        resources_used["cput"] = int(usage_usec / 1000000)

        mem_peak = int(usage.get("memory_peak", usage.get("memory_current", 0)))
        resources_used["mem"] = bytes_to_pbs_size(mem_peak)

        swap_peak = int(usage.get("swap_peak", usage.get("swap_current", 0)))
        if self.cfg.get("publish_vmem", True):
            resources_used["vmem"] = bytes_to_pbs_size(mem_peak + swap_peak)

        now = time.time()
        prev_t = float(state.get("last_usage_time", now))
        prev_u = int(state.get("last_usage_usec", usage_usec))
        dt = now - prev_t
        if dt > 0.1 and usage_usec >= prev_u:
            pct = int(round((usage_usec - prev_u) / (dt * 10000.0)))
            try:
                resources_used["cpupercent"] = max(0, pct)
            except Exception:
                pass
        state["last_usage_time"] = now
        state["last_usage_usec"] = usage_usec
        if state:
            try:
                self.cg.save_state(jobid, state)
            except Exception:
                pass

    def periodic(self, e):
        live = set(str(x) for x in e.job_list.keys())
        for jobid, state in self.cg.all_states().items():
            if jobid not in live:
                # Allow very recent begin events to survive a stale periodic
                # job_list snapshot.
                if time.time() - float(state.get("created", 0)) < 30:
                    continue
                try:
                    self.cg.delete_job(jobid, force=True)
                    self.cg.delete_state(jobid)
                    log(pbs.EVENT_DEBUG, "removed orphan cgroup for %s" % jobid)
                except Exception as exc:
                    log(pbs.EVENT_ERROR, "failed to remove orphan %s: %s" % (jobid, exc))
        if self.cfg.get("periodic_usage_update", True):
            for jobid, job in e.job_list.items():
                try:
                    self.update_usage(str(jobid), job.resources_used)
                except Exception as exc:
                    log(pbs.EVENT_DEBUG, "usage update failed for %s: %s" % (jobid, exc))

    def epilogue(self, e):
        self.update_usage(e.job.id, e.job.resources_used)
        # Keep state until END so GPU hook / late accounting can still inspect
        # the allocation; delete the actual cgroup here as historical hook did.
        try:
            self.cg.delete_job(e.job.id, force=True)
        except Exception as exc:
            log(pbs.EVENT_ERROR, "failed deleting cgroup at epilogue for %s: %s" % (e.job.id, exc))

    def end(self, e):
        try:
            self.cg.delete_job(e.job.id, force=True)
        finally:
            self.cg.delete_state(e.job.id)

    def resize(self, e):
        e.reject("pbs_job_cgroups_v2: dynamic job resource resizing is not supported")
        return False


# ---------------------------------------------------------------------------
# Event dispatch
# ---------------------------------------------------------------------------


def main():
    e = pbs.event()
    hook = CpuCgroupHook()

    result = True
    if e.type == pbs.EXECHOST_STARTUP:
        result = hook.startup(e)
    elif e.type == pbs.EXECHOST_PERIODIC:
        result = hook.periodic(e)
    elif e.type == pbs.EXECJOB_BEGIN:
        result = hook.begin(e)
    elif e.type == pbs.EXECJOB_LAUNCH:
        result = hook.launch(e)
    elif e.type == pbs.EXECJOB_ATTACH:
        result = hook.attach(e)
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
    try:
        pbs.event().reject("pbs_job_cgroups_v2 failed: %s" % exc)
    except Exception:
        pass
