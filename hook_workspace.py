# coding: utf-8
"""OpenPBS hook for per-job workspaces.

User-facing resources (all are size resources):
    scratch_local=<size>   logical local workspace; chooses one local subtype
    scratch_hdd=<size>     explicitly request the HDD backend
    scratch_ssd=<size>     explicitly request the SSD backend
    scratch_nvme=<size>    explicitly request the NVMe backend
    scratch_shared=<size>  shared workspace
    scratch_shm=true       tmpfs/shmem workspace; capacity is controlled by mem/cgroups

Exactly one scratch_* resource is allowed in each select chunk. scratch_shm is boolean; all other scratch resources are size resources.

The requested size is a scheduler reservation/accounting value.  It is not
enforced as a filesystem quota.

Discovery/publication is independently configurable for every user-facing
resource using "discover": true|false.  If discovery is disabled, this hook
does not modify resources_available.<resource>; the resource can therefore be
managed externally.  Backend validation is still performed at execjob_begin.

Each backend has its own preserve_nonempty policy.  In particular,
scratch_shm normally uses preserve_nonempty=false.
"""

import errno
import fcntl
import fnmatch
import glob
import json
import os
import pwd
import grp
import re
import shutil
import socket
import time
import traceback

import pbs


LOCAL_SUBTYPES = ("scratch_hdd", "scratch_ssd", "scratch_nvme")
USER_RESOURCES = (
    "scratch_local",
    "scratch_hdd",
    "scratch_ssd",
    "scratch_nvme",
    "scratch_shared",
    "scratch_shm",
)
LOCAL_RESOURCES = ("scratch_local",) + LOCAL_SUBTYPES

DEFAULT_CONFIG = {
    "state_subdir": "workspace",
    "scan_refresh": 7200,
    "fallback_dir": "/var/tmp/pbs.$PBS_JOBID",
    "scratch_local": {
        "discover": True,
        "subtypes": [],
    },
    "scratch_shared": None,
    "scratch_shm": None,
}

_SIZE_RE = re.compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([kmgtpe]?)(?:i?b)?\s*$", re.I
)


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def log(level, msg):
    pbs.logmsg(level, "pbs_workspace: " + str(msg))


def deep_merge(base, update):
    out = dict(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def read_pbs_conf():
    path = os.environ.get("PBS_CONF_FILE", "/etc/pbs.conf")
    out = {}
    with open(path, "r") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def validate_backend(backend, require_name=False):
    if not isinstance(backend, dict):
        raise RuntimeError("scratch backend must be a JSON object")

    needed = ["mount_point", "job_dir"]
    if require_name:
        needed += ["name", "priority"]

    for key in needed:
        if key not in backend:
            raise RuntimeError("scratch backend misses '%s'" % key)

    if not os.path.isabs(str(backend["mount_point"])):
        raise RuntimeError("mount_point must be absolute")
    if not os.path.isabs(str(backend["job_dir"])):
        raise RuntimeError("job_dir must be absolute")

    for key in ("source_patterns", "filesystem_patterns"):
        if key in backend:
            value = backend[key]
            if not isinstance(value, list) or not value:
                raise RuntimeError("%s must be a non-empty list" % key)

    if "rotational" in backend and \
       backend["rotational"] not in (True, False, None):
        raise RuntimeError("rotational must be true, false, or null")

    if "discover" in backend and \
       backend["discover"] not in (True, False):
        raise RuntimeError("discover must be boolean")

    if "preserve_nonempty" in backend and \
       backend["preserve_nonempty"] not in (True, False):
        raise RuntimeError("preserve_nonempty must be boolean")


def load_config():
    cfg = deep_merge({}, DEFAULT_CONFIG)
    path = os.environ.get("PBS_HOOK_CONFIG_FILE")
    if path and os.path.isfile(path):
        with open(path, "r") as f:
            cfg = deep_merge(cfg, json.load(f))

    local_cfg = cfg.get("scratch_local")
    if local_cfg is None:
        cfg["scratch_local"] = {"discover": False, "subtypes": []}
        local_cfg = cfg["scratch_local"]

    subtypes = local_cfg.get("subtypes", [])
    if not isinstance(subtypes, list):
        raise RuntimeError("scratch_local.subtypes must be a list")

    seen = set()
    for backend in subtypes:
        validate_backend(backend, require_name=True)
        name = str(backend["name"])
        if name not in LOCAL_SUBTYPES:
            raise RuntimeError(
                "local subtype '%s' is not one of %s" %
                (name, ", ".join(LOCAL_SUBTYPES))
            )
        if name in seen:
            raise RuntimeError("duplicate local subtype %s" % name)
        seen.add(name)

    for resource in ("scratch_shared", "scratch_shm"):
        backend = cfg.get(resource)
        if backend is not None:
            validate_backend(backend, require_name=False)

    return cfg


def size_to_bytes(value):
    if value is None:
        return 0
    if isinstance(value, int):
        return int(value)

    text = str(value).strip()
    m = _SIZE_RE.match(text)
    if not m:
        return int(text)

    number = float(m.group(1))
    unit = m.group(2).lower()
    power = {
        "": 0, "k": 1, "m": 2, "g": 3,
        "t": 4, "p": 5, "e": 6,
    }[unit]
    return int(number * (1024 ** power))


def bytes_to_pbs_size(value):
    # PBS resource sizes are reported in KiB ('kb') while internal accounting remains in bytes.
    kb = max(0, int(value)) // 1024
    return pbs.size("%dkb" % kb)


def local_names():
    out = set()
    for value in (
        pbs.get_local_nodename(),
        socket.gethostname(),
        socket.getfqdn(),
    ):
        if value:
            out.add(str(value))
            out.add(str(value).split(".")[0])
    return out


def vnode_is_local(name):
    base = str(name).split("[")[0]
    return base in local_names() or base.split(".")[0] in local_names()

def local_resources(job):
    """Return all scratch reservations assigned to this MoM.

    chunk.chunk_resources is an OpenPBS pbs_resource object, not a Python
    dict, so it must not be iterated with .items().  Access only the known
    resources explicitly.
    """
    out = {}
    try:
        chunks = job.exec_vnode.chunks
    except Exception:
        chunks = []

    for chunk in chunks:
        if not vnode_is_local(chunk.vnode_name):
            continue

        resources = chunk.chunk_resources
        for resource in USER_RESOURCES:
            try:
                value = resources[resource]
            except Exception:
                continue

            if value is None:
                continue

            if resource == "scratch_shm":
                value_text = str(value).strip().lower()
                if value_text in ("true", "t", "1", "yes", "on"):
                    out[resource] = True
            else:
                try:
                    amount = size_to_bytes(value)
                except Exception:
                    continue

                if amount > 0:
                    out[resource] = out.get(resource, 0) + amount

    return out

def total_request(job, resource):
    total = 0
    try:
        chunks = job.exec_vnode.chunks
    except Exception:
        chunks = []

    for chunk in chunks:
        try:
            total += size_to_bytes(chunk.chunk_resources[resource])
        except Exception:
            pass
    return total


def short_jobid(jobid):
    return str(jobid).split(".", 1)[0]


def identity(job):
    user = str(job.Job_Owner).split("@", 1)[0]
    group = None
    try:
        if job.group_list:
            group = str(job.group_list).split("@", 1)[0]
    except Exception:
        pass
    return user, group


def expand_template(template, job, user):
    values = {
        "$PBS_JOBID_SHORT": short_jobid(job.id),
        "${PBS_JOBID_SHORT}": short_jobid(job.id),
        "$PBS_JOBID": str(job.id),
        "${PBS_JOBID}": str(job.id),
        "$USER": user,
        "${USER}": user,
        "$HOST": socket.gethostname().split(".")[0],
        "${HOST}": socket.gethostname().split(".")[0],
    }
    result = str(template)
    for key in sorted(values, key=len, reverse=True):
        result = result.replace(key, values[key])
    return os.path.normpath(result)


def path_under(path, root):
    try:
        return (
            os.path.commonpath(
                [os.path.abspath(path), os.path.abspath(root)]
            ) == os.path.abspath(root)
        )
    except Exception:
        return False


def set_permissions(path, user, group, umask):
    pw = pwd.getpwnam(user)
    gid = pw.pw_gid

    if group:
        try:
            gr = grp.getgrnam(group)
            memberships = {
                entry.gr_name
                for entry in grp.getgrall()
                if user in entry.gr_mem
            }
            memberships.add(grp.getgrgid(pw.pw_gid).gr_name)
            if group in memberships:
                gid = gr.gr_gid
        except Exception:
            pass

    os.chown(path, pw.pw_uid, gid)

    if umask is not None:
        try:
            os.chmod(path, 0o777 & ~int(str(umask), 8))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Mount/backend discovery
# ---------------------------------------------------------------------------

def _unescape_mount(value):
    return re.sub(
        r"\\([0-7]{3})",
        lambda match: chr(int(match.group(1), 8)),
        value,
    )


def mount_table():
    out = {}
    with open("/proc/self/mountinfo", "r") as f:
        for line in f:
            left, right = line.rstrip("\n").split(" - ", 1)
            lcols = left.split()
            rcols = right.split()
            if len(lcols) < 5 or len(rcols) < 2:
                continue

            mount_point = os.path.normpath(_unescape_mount(lcols[4]))
            out[mount_point] = {
                "mount_point": mount_point,
                "major_minor": lcols[2],
                "filesystem": _unescape_mount(rcols[0]),
                "source": _unescape_mount(rcols[1]),
            }
    return out



def _sysfs_dev_path(major_minor):
    path = os.path.realpath("/sys/dev/block/%s" % major_minor)
    return path if path and os.path.exists(path) else None


def _devnode_from_sysfs(path):
    """Best-effort conversion of a sysfs block-device path to /dev/<name>."""
    if not path:
        return None
    name = os.path.basename(path)
    if not name:
        return None

    # Device-mapper devices are exposed as dm-N; prefer their friendly name.
    dm_name = os.path.join(path, "dm", "name")
    try:
        with open(dm_name, "r") as f:
            friendly = f.read().strip()
        if friendly:
            return "/dev/mapper/%s" % friendly
    except Exception:
        pass

    return "/dev/%s" % name


def _leaf_block_devices_from_sysfs(path, seen=None):
    """
    Recursively resolve a mounted block device through dm/LVM/MD stacks.

    Returns leaf block devices as dictionaries with:
      sysfs, devnode, major_minor, rotational

    For a simple partition, the partition itself is treated as a leaf for
    naming purposes, but rotational is read from its parent queue if needed.
    """
    if seen is None:
        seen = set()

    if not path:
        return []

    path = os.path.realpath(path)
    if path in seen:
        return []
    seen.add(path)

    slaves_dir = os.path.join(path, "slaves")
    slaves = []
    try:
        for name in os.listdir(slaves_dir):
            slave = os.path.realpath(os.path.join(slaves_dir, name))
            if slave and os.path.exists(slave):
                slaves.append(slave)
    except Exception:
        slaves = []

    if slaves:
        leaves = []
        for slave in slaves:
            leaves.extend(_leaf_block_devices_from_sysfs(slave, seen))
        return leaves

    major_minor = None
    try:
        with open(os.path.join(path, "dev"), "r") as f:
            major_minor = f.read().strip()
    except Exception:
        pass

    rotational = None
    if major_minor:
        rotational = rotational_value(major_minor)

    return [{
        "sysfs": path,
        "devnode": _devnode_from_sysfs(path),
        "major_minor": major_minor,
        "rotational": rotational,
    }]


def resolve_leaf_block_devices(major_minor):
    """
    Resolve a mount's major:minor device to physical/logical leaf devices.

    Example:
      /dev/mapper/lvm-scratch -> dm-X -> nvme0n1pY

    This uses sysfs only and therefore does not depend on lvs/lsblk binaries.
    """
    root = _sysfs_dev_path(major_minor)
    if not root:
        return []
    return _leaf_block_devices_from_sysfs(root)


def leaf_matches_patterns(leaves, patterns):
    for leaf in leaves:
        devnode = leaf.get("devnode")
        if not devnode:
            continue
        for pattern in patterns:
            if fnmatch.fnmatch(devnode, str(pattern)):
                return True
    return False


def classify_rotational(leaves):
    """
    Return:
      0 if all known leaves are non-rotational,
      1 if all known leaves are rotational,
      None if mixed/unknown.

    Mixed HDD+SSD storage is deliberately not classified as either.
    """
    values = [
        leaf.get("rotational")
        for leaf in leaves
        if leaf.get("rotational") in (0, 1)
    ]
    if not values:
        return None
    if all(value == 0 for value in values):
        return 0
    if all(value == 1 for value in values):
        return 1
    return None


def rotational_value(major_minor):
    """Return 0/1 from sysfs where possible, otherwise None."""
    candidates = [
        "/sys/dev/block/%s/queue/rotational" % major_minor,
        "/sys/class/block/%s/queue/rotational" % major_minor,
    ]

    # Partitions usually expose queue/ through the parent block device.
    real = os.path.realpath("/sys/dev/block/%s" % major_minor)
    if real:
        parent = os.path.dirname(real)
        candidates.append(os.path.join(parent, "queue", "rotational"))

    for path in candidates:
        try:
            with open(path, "r") as f:
                return int(f.read().strip())
        except Exception:
            pass
    return None


def probe_backend(backend, mounts=None):
    if mounts is None:
        mounts = mount_table()

    mount_point = os.path.normpath(str(backend["mount_point"]))
    info = mounts.get(mount_point)

    if info is None:
        return {
            "usable": False,
            "reason": "%s is not an actual mount point" % mount_point,
            "mount_point": mount_point,
        }

    source_patterns = backend.get("source_patterns", ["*"])
    filesystem_patterns = backend.get("filesystem_patterns", ["*"])

    # Match either the mounted source itself or, for dm/LVM/MD stacks, any
    # resolved leaf block device.  This permits a mount such as
    # /dev/mapper/lvm-scratch to be identified as NVMe/SSD/HDD according to
    # its backing devices.
    leaves = resolve_leaf_block_devices(info["major_minor"])

    direct_source_match = any(
        fnmatch.fnmatch(info["source"], str(pattern))
        for pattern in source_patterns
    )
    leaf_source_match = leaf_matches_patterns(leaves, source_patterns)

    if not (direct_source_match or leaf_source_match):
        leaf_names = [
            leaf.get("devnode") for leaf in leaves if leaf.get("devnode")
        ]
        return dict(
            info,
            usable=False,
            reason="source %s / backing devices %s do not match %s" %
                   (info["source"], leaf_names or ["unknown"],
                    source_patterns),
        )

    if not any(
        fnmatch.fnmatch(info["filesystem"], str(pattern))
        for pattern in filesystem_patterns
    ):
        return dict(
            info,
            usable=False,
            reason="filesystem %s does not match %s" %
                   (info["filesystem"], filesystem_patterns),
        )

    expected_rot = backend.get("rotational", None)
    actual_rot = classify_rotational(leaves)
    if actual_rot is None:
        # Fall back to the mounted device itself for simple non-stacked
        # block devices or when sysfs leaf resolution is incomplete.
        actual_rot = rotational_value(info["major_minor"])

    if expected_rot is not None:
        expected = 1 if expected_rot else 0
        if actual_rot is None:
            return dict(
                info,
                usable=False,
                reason="cannot determine rotational property of backing "
                       "device(s)",
            )
        if actual_rot != expected:
            return dict(
                info,
                usable=False,
                reason="backing-device rotational=%s, expected %s" %
                       (actual_rot, expected),
            )

    try:
        stat = os.statvfs(mount_point)
        block = int(stat.f_frsize or stat.f_bsize)
        total = block * int(stat.f_blocks)
        free = block * int(stat.f_bavail)
    except Exception as exc:
        return dict(
            info,
            usable=False,
            reason="statvfs failed: %s" % exc,
        )

    return dict(
        info,
        usable=True,
        reason="",
        rotational=actual_rot,
        backing_devices=[
            leaf.get("devnode") for leaf in leaves if leaf.get("devnode")
        ],
        total=total,
        free=free,
    )


# ---------------------------------------------------------------------------
# Persistent state
# ---------------------------------------------------------------------------

class FileLock(object):
    def __init__(self, path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.handle = open(self.path, "a+")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()


class State(object):
    def __init__(self, cfg):
        pbs_conf = read_pbs_conf()
        pbs_home = pbs_conf.get(
            "PBS_MOM_HOME",
            pbs_conf.get("PBS_HOME", "/var/spool/pbs"),
        )
        self.root = os.path.join(
            pbs_home,
            "mom_priv",
            str(cfg.get("state_subdir", "workspace")),
        )
        self.jobs = os.path.join(self.root, "jobs")
        self.cache = os.path.join(self.root, "cache")
        self.lock_path = os.path.join(self.root, "lock")

        for path in (self.root, self.jobs, self.cache):
            if not os.path.isdir(path):
                os.makedirs(path, mode=0o700, exist_ok=True)

    def lock(self):
        return FileLock(self.lock_path)

    def jobfile(self, jobid):
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(jobid))
        return os.path.join(self.jobs, safe + ".json")

    def load(self, jobid):
        try:
            with open(self.jobfile(jobid), "r") as f:
                return json.load(f)
        except Exception:
            return None

    def save(self, jobid, value):
        path = self.jobfile(jobid)
        tmp = path + ".tmp.%d" % os.getpid()
        with open(tmp, "w") as f:
            json.dump(value, f, sort_keys=True, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def delete(self, jobid):
        try:
            os.unlink(self.jobfile(jobid))
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise

    def all(self):
        out = {}
        for path in glob.glob(os.path.join(self.jobs, "*.json")):
            try:
                with open(path, "r") as f:
                    value = json.load(f)
                out[str(value["jobid"])] = value
            except Exception:
                pass
        return out

    def cachefile(self, name):
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(name))
        return os.path.join(self.cache, safe + ".json")


# ---------------------------------------------------------------------------
# Dead/unmanaged-data accounting
# ---------------------------------------------------------------------------

def scan_unmanaged(account_root, live_paths, mount_point):
    """Count allocated bytes outside currently live PBS job directories."""
    account_root = os.path.normpath(account_root)
    live_paths = set(os.path.normpath(path) for path in live_paths)

    try:
        device = os.stat(mount_point).st_dev
    except Exception:
        return 0

    total = 0
    for root, dirs, files in os.walk(
        account_root, topdown=True, followlinks=False
    ):
        root_norm = os.path.normpath(root)

        if root_norm in live_paths:
            dirs[:] = []
            continue

        kept_dirs = []
        for name in dirs:
            path = os.path.normpath(os.path.join(root, name))
            if path in live_paths:
                continue
            try:
                if os.path.islink(path):
                    continue
                if os.stat(path).st_dev != device:
                    continue
                kept_dirs.append(name)
            except Exception:
                pass
        dirs[:] = kept_dirs

        for name in files:
            path = os.path.join(root, name)
            try:
                if os.path.islink(path):
                    continue
                stat = os.stat(path)
                if stat.st_dev != device:
                    continue
                total += int(stat.st_blocks) * 512
            except Exception:
                pass

    return total


def read_cache(path):
    try:
        with open(path, "r") as f:
            value = json.load(f)
        return int(value.get("bytes", 0)), float(value.get("time", 0))
    except Exception:
        return 0, 0.0


def pid_running(pidfile):
    try:
        with open(pidfile, "r") as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def unmanaged_cached(state, cache_name, backend, live_paths, cfg, probe):
    cache = state.cachefile(cache_name)
    pidfile = cache + ".pid"
    value, stamp = read_cache(cache)

    if stamp == 0:
        # Safe initial fallback: treat all currently used space as unavailable
        # until a background scan classifies live PBS data.
        value = max(0, int(probe["total"]) - int(probe["free"]))

    refresh = int(backend.get("scan_refresh", cfg.get("scan_refresh", 7200)))
    scan_enabled = bool(backend.get("scan_enabled", True))

    if scan_enabled and (stamp == 0 or time.time() - stamp >= refresh):
        if not pid_running(pidfile):
            pid = os.fork()
            if pid == 0:
                try:
                    root = backend.get(
                        "accounting_root", backend["mount_point"]
                    )
                    result = scan_unmanaged(
                        root, live_paths, backend["mount_point"]
                    )
                    tmp = cache + ".tmp.%d" % os.getpid()
                    with open(tmp, "w") as f:
                        json.dump(
                            {"bytes": result, "time": time.time()}, f
                        )
                    os.replace(tmp, cache)
                except Exception:
                    pass
                finally:
                    try:
                        os.unlink(pidfile)
                    except Exception:
                        pass
                    os._exit(0)

            try:
                with open(pidfile, "w") as f:
                    f.write(str(pid))
            except Exception:
                pass

    if not scan_enabled:
        return 0

    return max(0, int(value))


# ---------------------------------------------------------------------------
# Reservation accounting
# ---------------------------------------------------------------------------

class Accounting(object):
    def __init__(self, cfg, state):
        self.cfg = cfg
        self.state = state

    def live_states(self, event):
        live_ids = set(
            str(jobid)
            for jobid in getattr(event, "job_list", {}).keys()
        )
        now = time.time()
        out = {}

        for jobid, state in self.state.all().items():
            if jobid in live_ids or \
               now - float(state.get("created", 0)) < 60:
                out[jobid] = state
        return out

    def reserved(
        self,
        states,
        backend=None,
        requested_resource=None,
        exclude=None,
    ):
        total = 0
        for jobid, state in states.items():
            if exclude is not None and str(jobid) == str(exclude):
                continue
            if backend is not None and state.get("backend") != backend:
                continue
            if requested_resource is not None and \
               state.get("requested_resource") != requested_resource:
                continue
            total += int(state.get("requested_bytes", 0))
        return total

    def status(
        self,
        name,
        backend,
        states,
        probe=None,
        exclude=None,
    ):
        probe = probe_backend(backend) if probe is None else probe
        if not probe.get("usable"):
            return {
                "name": name,
                "usable": False,
                "reason": probe.get("reason", "unusable"),
                "reservable": 0,
            }

        filtered = {
            jobid: state
            for jobid, state in states.items()
            if exclude is None or str(jobid) != str(exclude)
        }

        live_paths = [
            state["path"]
            for state in filtered.values()
            if state.get("backend") == name and state.get("path")
        ]

        unmanaged = unmanaged_cached(
            self.state,
            name,
            backend,
            live_paths,
            self.cfg,
            probe,
        )

        # All reservations that use the same physical backend count here,
        # irrespective of whether the user asked for scratch_local or an
        # explicit subtype resource.
        reserved = self.reserved(filtered, backend=name)

        workspace = max(0, int(probe["total"]) - unmanaged)
        reservation_headroom = max(0, workspace - reserved)
        reservable = max(
            0,
            min(reservation_headroom, int(probe["free"])),
        )

        return dict(
            probe,
            name=name,
            usable=True,
            unmanaged=unmanaged,
            reserved=reserved,
            workspace=workspace,
            reservable=reservable,
        )

    def local_statuses(self, event, exclude=None):
        states = self.live_states(event)
        mounts = mount_table()
        out = []

        for backend in self.cfg["scratch_local"].get("subtypes", []):
            name = str(backend["name"])
            status = self.status(
                name,
                backend,
                states,
                probe_backend(backend, mounts),
                exclude,
            )
            status["priority"] = int(backend.get("priority", 0))
            status["config"] = backend
            out.append(status)

        return out, states

    def choose_local(self, event, requested, jobid):
        statuses, _ = self.local_statuses(event, exclude=jobid)

        candidates = [
            status
            for status in statuses
            if status["usable"] and status["reservable"] >= requested
        ]
        candidates.sort(
            key=lambda status: (-status["priority"], status["name"])
        )

        if not candidates:
            details = ", ".join(
                "%s=%d" % (status["name"], status["reservable"])
                for status in statuses
                if status["usable"]
            )
            raise RuntimeError(
                "no local scratch subtype can satisfy %d bytes (%s)" %
                (requested, details or "none usable")
            )

        return candidates[0]

    def exact_local(self, event, resource, requested, jobid):
        statuses, _ = self.local_statuses(event, exclude=jobid)
        for status in statuses:
            if status["name"] != resource:
                continue
            if not status["usable"]:
                raise RuntimeError(
                    "%s is unavailable: %s" %
                    (resource, status.get("reason", "unusable"))
                )
            if status["reservable"] < requested:
                raise RuntimeError(
                    "%s cannot satisfy %d bytes; %d bytes reservable" %
                    (resource, requested, status["reservable"])
                )
            return status

        raise RuntimeError("%s is not configured on this node" % resource)

    def singleton_status(self, event, resource, exclude=None):
        backend = self.cfg.get(resource)
        if backend is None:
            return None
        return self.status(
            resource,
            backend,
            self.live_states(event),
            exclude=exclude,
        )


# ---------------------------------------------------------------------------
# Directory/environment management
# ---------------------------------------------------------------------------

def create_dir(path, backend, job, user, group):
    if not path_under(path, backend["mount_point"]):
        raise RuntimeError(
            "workspace path %s is outside %s" %
            (path, backend["mount_point"])
        )

    umask = getattr(job, "umask", None)
    rerun_prefix = str(backend.get("rerun_prefix", ".run_count"))

    if os.path.isdir(path):
        backup = os.path.join(
            path,
            "%s-%d" %
            (rerun_prefix, int(getattr(job, "run_count", 0))),
        )
        if os.path.exists(backup):
            raise RuntimeError(
                "rerun backup already exists: %s" % backup
            )

        os.mkdir(backup)
        set_permissions(backup, user, group, umask)

        for name in os.listdir(path):
            if not name.startswith(rerun_prefix):
                shutil.move(os.path.join(path, name), backup)
    else:
        os.makedirs(path)
        set_permissions(path, user, group, umask)


def remove_dir(path, preserve_nonempty):
    if not os.path.isdir(path):
        return

    if preserve_nonempty:
        try:
            os.rmdir(path)
            log(
                pbs.EVENT_DEBUG,
                "removed empty workspace %s" % path,
            )
        except OSError as exc:
            if exc.errno == errno.ENOTEMPTY:
                log(
                    pbs.EVENT_DEBUG,
                    "preserving non-empty workspace %s" % path,
                )
            elif exc.errno != errno.ENOENT:
                raise
    else:
        shutil.rmtree(path)


def set_env(
    job,
    path,
    requested_resource,
    backend_name,
    local_size,
    total_size,
):
    variables = job.Variable_List

    for key in (
        "SCRATCHDIR",
        "SCRATCH",
        "SINGULARITY_TMPDIR",
        "SINGULARITY_CACHEDIR",
    ):
        variables[key] = path

    variables["SCRATCH_VOLUME"] = int(local_size)
    variables["PBS_RESC_SCRATCH_VOLUME"] = int(local_size)
    variables["TORQUE_RESC_SCRATCH_VOLUME"] = int(local_size)

    variables["PBS_RESC_TOTAL_SCRATCH_VOLUME"] = int(total_size)
    variables["TORQUE_RESC_TOTAL_SCRATCH_VOLUME"] = int(total_size)

    variables["SCRATCH_RESOURCE"] = requested_resource
    variables["SCRATCH_SUBTYPE"] = backend_name

    if requested_resource == "scratch_shared":
        variables["SCRATCH_TYPE"] = "shared"
    elif requested_resource == "scratch_shm":
        variables["SCRATCH_TYPE"] = "shm"
    elif requested_resource == "none":
        variables["SCRATCH_TYPE"] = "none"
    else:
        variables["SCRATCH_TYPE"] = "local"

    env_name = "PBS_RESC_" + requested_resource.upper()
    torque_name = "TORQUE_RESC_" + requested_resource.upper()
    if requested_resource != "none":
        variables[env_name] = int(local_size)
        variables[torque_name] = int(local_size)

    # A direct subtype request is still local scratch.  Keep the historical
    # local variables as aliases for applications that only understand them.
    if requested_resource in LOCAL_SUBTYPES:
        variables["PBS_RESC_SCRATCH_LOCAL"] = int(local_size)
        variables["TORQUE_RESC_SCRATCH_LOCAL"] = int(local_size)


# ---------------------------------------------------------------------------
# Hook
# ---------------------------------------------------------------------------

class WorkspaceHook(object):
    def __init__(self):
        self.cfg = load_config()
        self.state = State(self.cfg)
        self.acct = Accounting(self.cfg, self.state)

    def queue_modify(self, event):
        job = event.job

        # Validate workspace requests from Resource_List.select only.
        #
        # Do NOT infer how a scratch resource was submitted by testing
        # membership in job.Resource_List.  OpenPBS may expose custom
        # host-level resources there even when they were not explicitly
        # requested as job-wide resources.  For resources with flag=h, PBS
        # also normalizes resource requests into select chunks.

        try:
            select = str(job.Resource_List["select"])
        except Exception:
            select = ""

        if not select:
            return True

        for chunk in select.split("+"):
            found = []
            for resource in USER_RESOURCES:
                if re.search(
                    r"(^|:)" + re.escape(resource) + r"=",
                    chunk,
                ):
                    found.append(resource)

            # Also catch unsupported scratch_* resources early.
            all_scratch = re.findall(
                r"(?:^|:)(scratch_[A-Za-z0-9_]+)=",
                chunk,
            )
            unsupported = [
                name for name in all_scratch
                if name not in USER_RESOURCES
            ]
            if unsupported:
                event.reject(
                    "unsupported scratch resource(s) in select chunk: %s" %
                    ", ".join(sorted(set(unsupported)))
                )
                return False

            if len(found) > 1:
                event.reject(
                    "only one scratch_* resource is allowed per "
                    "select chunk; found: %s" %
                    ", ".join(found)
                )
                return False

            shm_match = re.search(
                r"(?:^|:)scratch_shm=([^:]+)",
                chunk,
                re.I,
            )
            if shm_match:
                shm_value = shm_match.group(1).strip().lower()
                if shm_value not in ("true", "t", "1", "yes", "on"):
                    event.reject(
                        "scratch_shm is a boolean resource and must be "
                        "requested as scratch_shm=true"
                    )
                    return False

        if re.search(r"(^|:)scratch_shared=", select):
            try:
                place = str(job.Resource_List["place"])
            except Exception:
                place = ""

            if "group=" not in place:
                job.Resource_List["place"] = pbs.place(
                    (place + ":" if place else "") + "group=cluster"
                )

        return True

    def _publish_resource(self, event, resource, capacity):
        for vnode_name in list(event.vnode_list.keys()):
            if vnode_is_local(vnode_name):
                event.vnode_list[vnode_name].resources_available[
                    resource
                ] = bytes_to_pbs_size(capacity)

    def publish(self, event):
        statuses, states = self.acct.local_statuses(event)

        # Concrete subtype resources.  Each resource_available value includes
        # only the reservations PBS subtracts from that same resource.
        for status in statuses:
            backend = status["config"]
            resource = status["name"]

            if bool(backend.get("discover", True)):
                direct_reserved = self.acct.reserved(
                    states,
                    requested_resource=resource,
                )
                capacity = direct_reserved + int(status["reservable"])
                self._publish_resource(event, resource, capacity)

            if status["usable"]:
                log(
                    pbs.EVENT_DEBUG,
                    "%s source=%s backing=%s fs=%s priority=%d "
                    "free=%d unmanaged=%d backend_reserved=%d "
                    "reservable=%d" %
                    (
                        resource,
                        status["source"],
                        status.get("backing_devices", []),
                        status["filesystem"],
                        status["priority"],
                        status["free"],
                        status["unmanaged"],
                        status["reserved"],
                        status["reservable"],
                    ),
                )
            else:
                log(
                    pbs.EVENT_DEBUG,
                    "%s unavailable: %s" %
                    (resource, status.get("reason", "unusable")),
                )

        # Logical scratch_local.  A single request must fit completely on one
        # concrete backend, hence max(reservable), never sum(reservable).
        local_cfg = self.cfg.get("scratch_local", {})
        if bool(local_cfg.get("discover", True)):
            local_reserved = self.acct.reserved(
                states,
                requested_resource="scratch_local",
            )
            local_free = max(
                [int(status["reservable"]) for status in statuses] or [0]
            )
            self._publish_resource(
                event,
                "scratch_local",
                local_reserved + local_free,
            )

        # Shared scratch remains a size-accounted singleton backend.
        backend = self.cfg.get("scratch_shared")
        if backend is not None and bool(backend.get("discover", True)):
            status = self.acct.singleton_status(event, "scratch_shared")
            reserved = self.acct.reserved(
                states,
                requested_resource="scratch_shared",
            )
            capacity = reserved
            if status and status["usable"]:
                capacity += int(status["reservable"])
            self._publish_resource(
                event,
                "scratch_shared",
                capacity,
            )

        # scratch_shm is boolean only.  No size discovery or reservation
        # accounting is performed; memory usage is governed by mem/cgroups.
        shm_backend = self.cfg.get("scratch_shm")
        if shm_backend is not None and bool(shm_backend.get("discover", True)):
            shm_probe = probe_backend(shm_backend)
            available = bool(shm_probe.get("usable"))
            for vnode_name in list(event.vnode_list.keys()):
                if vnode_is_local(vnode_name):
                    event.vnode_list[vnode_name].resources_available[
                        "scratch_shm"
                    ] = available

        return True

    def startup(self, event):
        return self.publish(event)

    def periodic(self, event):
        live = set(str(jobid) for jobid in event.job_list.keys())
        now = time.time()

        with self.state.lock():
            for jobid, state in self.state.all().items():
                if jobid not in live and \
                   now - float(state.get("created", 0)) >= 60:
                    self.state.delete(jobid)

        return self.publish(event)

    def _single_local_request(self, resources):
        found = [
            (resource, int(resources.get(resource, 0)))
            for resource in LOCAL_RESOURCES
            if int(resources.get(resource, 0)) > 0
        ]
        if len(found) > 1:
            raise RuntimeError(
                "more than one local scratch resource assigned to "
                "the local execution chunk"
            )
        return found[0] if found else (None, 0)

    def begin(self, event):
        job = event.job
        resources = local_resources(job)

        requested = []
        for resource in USER_RESOURCES:
            if resource == "scratch_shm":
                if bool(resources.get(resource, False)):
                    requested.append((resource, True))
            else:
                amount = int(resources.get(resource, 0))
                if amount > 0:
                    requested.append((resource, amount))

        if len(requested) > 1:
            event.reject(
                "pbs_workspace: local execution chunk contains more than "
                "one scratch_* resource"
            )
            return False

        if not requested:
            user, _ = identity(job)
            path = expand_template(
                self.cfg.get(
                    "fallback_dir",
                    "/var/tmp/pbs.$PBS_JOBID",
                ),
                job,
                user,
            )
            set_env(job, path, "none", "none", 0, 0)
            return True

        requested_resource, requested_bytes = requested[0]
        user, group = identity(job)

        if requested_resource in LOCAL_RESOURCES:
            with self.state.lock():
                if requested_resource == "scratch_local":
                    chosen = self.acct.choose_local(
                        event,
                        requested_bytes,
                        str(job.id),
                    )
                else:
                    chosen = self.acct.exact_local(
                        event,
                        requested_resource,
                        requested_bytes,
                        str(job.id),
                    )

                backend = chosen["config"]
                path = expand_template(
                    backend["job_dir"],
                    job,
                    user,
                )
                create_dir(path, backend, job, user, group)

                self.state.save(
                    job.id,
                    {
                        "jobid": str(job.id),
                        "created": time.time(),
                        "kind": "local",
                        "requested_resource": requested_resource,
                        "backend": chosen["name"],
                        "requested_bytes": requested_bytes,
                        "path": path,
                        "mount_point": backend["mount_point"],
                        "preserve_nonempty": bool(
                            backend.get("preserve_nonempty", True)
                        ),
                    },
                )

            set_env(
                job,
                path,
                requested_resource,
                chosen["name"],
                requested_bytes,
                total_request(job, requested_resource),
            )

            log(
                pbs.EVENT_DEBUG,
                "%s: %s=%d -> %s (%s)" %
                (
                    job.id,
                    requested_resource,
                    requested_bytes,
                    chosen["name"],
                    path,
                ),
            )
            return True

        if requested_resource == "scratch_shared":
            backend = self.cfg.get("scratch_shared")
            if backend is None:
                event.reject(
                    "pbs_workspace: scratch_shared is not configured"
                )
                return False

            probe = probe_backend(backend)
            if not probe.get("usable"):
                event.reject(
                    "pbs_workspace: scratch_shared unavailable: %s" %
                    probe.get("reason")
                )
                return False

            path = expand_template(backend["job_dir"], job, user)

            if job.in_ms_mom():
                with self.state.lock():
                    status = self.acct.singleton_status(
                        event,
                        "scratch_shared",
                        exclude=str(job.id),
                    )
                    if not status or status["reservable"] < requested_bytes:
                        event.reject(
                            "pbs_workspace: scratch_shared cannot satisfy "
                            "%d bytes" % requested_bytes
                        )
                        return False

                    create_dir(path, backend, job, user, group)
                    self.state.save(
                        job.id,
                        {
                            "jobid": str(job.id),
                            "created": time.time(),
                            "kind": "shared",
                            "requested_resource": "scratch_shared",
                            "backend": "scratch_shared",
                            "requested_bytes": requested_bytes,
                            "path": path,
                            "mount_point": backend["mount_point"],
                            "preserve_nonempty": bool(
                                backend.get("preserve_nonempty", True)
                            ),
                        },
                    )

            set_env(
                job,
                path,
                "scratch_shared",
                "scratch_shared",
                requested_bytes,
                total_request(job, "scratch_shared"),
            )
            return True

        if requested_resource == "scratch_shm":
            backend = self.cfg.get("scratch_shm")
            if backend is None:
                event.reject(
                    "pbs_workspace: scratch_shm is not configured"
                )
                return False

            probe = probe_backend(backend)
            if not probe.get("usable"):
                event.reject(
                    "pbs_workspace: scratch_shm unavailable: %s" %
                    probe.get("reason")
                )
                return False

            path = expand_template(backend["job_dir"], job, user)

            with self.state.lock():
                create_dir(path, backend, job, user, group)
                self.state.save(
                    job.id,
                    {
                        "jobid": str(job.id),
                        "created": time.time(),
                        "kind": "shm",
                        "requested_resource": "scratch_shm",
                        "backend": "scratch_shm",
                        "requested_bytes": 0,
                        "path": path,
                        "mount_point": backend["mount_point"],
                        "preserve_nonempty": bool(
                            backend.get("preserve_nonempty", False)
                        ),
                    },
                )

            # No scratch_shm size is exported/reserved.  Its usable size is
            # governed by the job's mem request and memory cgroup.
            variables = job.Variable_List
            for key in (
                "SCRATCHDIR",
                "SCRATCH",
                "SINGULARITY_TMPDIR",
                "SINGULARITY_CACHEDIR",
            ):
                variables[key] = path
            variables["SCRATCH_TYPE"] = "shm"
            variables["SCRATCH_RESOURCE"] = "scratch_shm"
            variables["SCRATCH_SUBTYPE"] = "scratch_shm"
            variables["SCRATCH_VOLUME"] = 0
            variables["PBS_RESC_SCRATCH_VOLUME"] = 0
            variables["TORQUE_RESC_SCRATCH_VOLUME"] = 0
            variables["PBS_RESC_TOTAL_SCRATCH_VOLUME"] = 0
            variables["TORQUE_RESC_TOTAL_SCRATCH_VOLUME"] = 0

            return True

        event.reject(
            "pbs_workspace: unsupported scratch resource %s" %
            requested_resource
        )
        return False

    def end(self, event):
        state = self.state.load(event.job.id)
        if state is None:
            return True

        if state.get("kind") == "shared" and not event.job.in_ms_mom():
            return True

        preserve_nonempty = bool(
            state.get("preserve_nonempty", True)
        )

        try:
            remove_dir(state["path"], preserve_nonempty)
        finally:
            with self.state.lock():
                self.state.delete(event.job.id)

        return True

    def resize(self, event):
        event.reject(
            "pbs_workspace: dynamic scratch resizing is not supported"
        )
        return False


def main():
    event = pbs.event()
    hook = WorkspaceHook()
    result = True

    if event.type in (pbs.QUEUEJOB, pbs.MODIFYJOB):
        result = hook.queue_modify(event)
    elif event.type == pbs.EXECHOST_STARTUP:
        result = hook.startup(event)
    elif event.type == pbs.EXECHOST_PERIODIC:
        result = hook.periodic(event)
    elif event.type == pbs.EXECJOB_BEGIN:
        result = hook.begin(event)
    elif event.type == pbs.EXECJOB_END:
        result = hook.end(event)
    elif hasattr(pbs, "EXECJOB_ABORT") and \
         event.type == pbs.EXECJOB_ABORT:
        result = hook.end(event)
    elif hasattr(pbs, "EXECJOB_RESIZE") and \
         event.type == pbs.EXECJOB_RESIZE:
        result = hook.resize(event)

    if result is not False:
        event.accept()


try:
    main()
except SystemExit:
    raise
except Exception as exc:
    log(
        pbs.EVENT_ERROR,
        "%s\n%s" % (exc, traceback.format_exc()),
    )
    try:
        pbs.event().reject("pbs_workspace failed: %s" % exc)
    except Exception:
        pass
