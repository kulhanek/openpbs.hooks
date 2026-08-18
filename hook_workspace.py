# coding: utf-8
"""OpenPBS hook for per-job scratch/workspace directories.

User-facing resources:
    scratch_local=<size>    local workspace
    scratch_shared=<size>   shared workspace
    scratch_shm=true        tmpfs/shmem workspace

Each top-level scratch class has one persistently selected discovery subtype.
Subtypes describe storage technology/backends (for example ``nvme``, ``ssd``,
``hdd``, ``lustre``, ``nfs`` or ``tmpfs``); they are not consumable scratch
resources themselves.  The selected subtype is published as a scheduler-
selectable string resource:

    resources_available.scratch_local_subtype
    resources_available.scratch_shared_subtype
    resources_available.scratch_shm_subtype

A subtype is selected by discovery according to configured priority and then
persisted in MoM-private state.  Periodic discovery only refreshes availability
and capacity of that selected backend.  It never silently falls back to a
different subtype if the selected backend becomes unavailable.

Exactly one top-level scratch_* resource is allowed in each select chunk.
Scratch sizes are scheduler reservation/accounting values, not filesystem
quotas.  scratch_shm is boolean; its practical capacity is governed by the
job's memory allocation/cgroup.
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


SIZE_RESOURCES = ("scratch_local", "scratch_shared")
BOOLEAN_RESOURCES = ("scratch_shm",)
USER_RESOURCES = SIZE_RESOURCES + BOOLEAN_RESOURCES
SUBTYPE_RESOURCES = tuple(resource + "_subtype" for resource in USER_RESOURCES)

DEFAULT_CONFIG = {
    "state_subdir": "workspace",
    "scan_refresh": 7200,
    "fallback_dir": "/var/tmp/pbs.$PBS_JOBID",
    "scratch_local": {"discover": True, "subtypes": []},
    "scratch_shared": {"discover": True, "subtypes": []},
    "scratch_shm": {"discover": True, "subtypes": []},
}

_SIZE_RE = re.compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([kmgtpe]?)(?:i?b)?\s*$", re.I
)
_SUBTYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


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


def validate_backend(backend):
    if not isinstance(backend, dict):
        raise RuntimeError("scratch subtype must be a JSON object")

    for key in ("name", "priority", "mount_point", "job_dir"):
        if key not in backend:
            raise RuntimeError("scratch subtype misses '%s'" % key)

    name = str(backend["name"])
    if not _SUBTYPE_RE.match(name):
        raise RuntimeError("invalid scratch subtype name '%s'" % name)
    if name.startswith("scratch_"):
        raise RuntimeError(
            "scratch subtype '%s' must not use the 'scratch_' prefix" % name
        )

    try:
        int(backend["priority"])
    except Exception:
        raise RuntimeError("scratch subtype priority must be an integer")

    if not os.path.isabs(str(backend["mount_point"])):
        raise RuntimeError("mount_point must be absolute")
    if not os.path.isabs(str(backend["job_dir"])):
        raise RuntimeError("job_dir must be absolute")

    for key in ("source_patterns", "filesystem_patterns"):
        if key in backend:
            value = backend[key]
            if not isinstance(value, list) or not value:
                raise RuntimeError("%s must be a non-empty list" % key)

    if "rotational" in backend and backend["rotational"] not in (
        True, False, None
    ):
        raise RuntimeError("rotational must be true, false, or null")

    if "preserve_nonempty" in backend and backend["preserve_nonempty"] not in (
        True, False
    ):
        raise RuntimeError("preserve_nonempty must be boolean")


def load_config():
    cfg = deep_merge({}, DEFAULT_CONFIG)
    path = os.environ.get("PBS_HOOK_CONFIG_FILE")
    if path and os.path.isfile(path):
        with open(path, "r") as f:
            cfg = deep_merge(cfg, json.load(f))

    for resource in USER_RESOURCES:
        section = cfg.get(resource)
        if section is None:
            section = {"discover": False, "subtypes": []}
            cfg[resource] = section
        if not isinstance(section, dict):
            raise RuntimeError("%s must be a JSON object or null" % resource)
        if section.get("discover", True) not in (True, False):
            raise RuntimeError("%s.discover must be boolean" % resource)
        subtypes = section.get("subtypes", [])
        if not isinstance(subtypes, list):
            raise RuntimeError("%s.subtypes must be a list" % resource)

        seen = set()
        for backend in subtypes:
            validate_backend(backend)
            name = str(backend["name"])
            if name in seen:
                raise RuntimeError(
                    "duplicate subtype '%s' in %s" % (name, resource)
                )
            seen.add(name)

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
    power = {"": 0, "k": 1, "m": 2, "g": 3, "t": 4, "p": 5, "e": 6}[unit]
    return int(number * (1024 ** power))


def bytes_to_pbs_size(value):
    kb = max(0, int(value)) // 1024
    return pbs.size("%dkb" % kb)


def local_names():
    out = set()
    for value in (pbs.get_local_nodename(), socket.gethostname(), socket.getfqdn()):
        if value:
            out.add(str(value))
            out.add(str(value).split(".")[0])
    return out


def vnode_is_local(name):
    base = str(name).split("[")[0]
    return base in local_names() or base.split(".")[0] in local_names()


def _bool_true(value):
    return str(value).strip().lower() in ("true", "t", "1", "yes", "on")


def local_resources(job):
    """Return top-level scratch reservations assigned to this MoM."""
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
            if resource in BOOLEAN_RESOURCES:
                if _bool_true(value):
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
            value = chunk.chunk_resources[resource]
            if resource in BOOLEAN_RESOURCES:
                total += 1 if _bool_true(value) else 0
            else:
                total += size_to_bytes(value)
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
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) == os.path.abspath(root)
    except Exception:
        return False


def set_permissions(path, user, group, umask):
    pw = pwd.getpwnam(user)
    gid = pw.pw_gid
    if group:
        try:
            gr = grp.getgrnam(group)
            memberships = {
                entry.gr_name for entry in grp.getgrall() if user in entry.gr_mem
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
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), value)


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
    if not path:
        return None
    name = os.path.basename(path)
    if not name:
        return None
    try:
        with open(os.path.join(path, "dm", "name"), "r") as f:
            friendly = f.read().strip()
        if friendly:
            return "/dev/mapper/%s" % friendly
    except Exception:
        pass
    return "/dev/%s" % name


def rotational_value(major_minor):
    candidates = [
        "/sys/dev/block/%s/queue/rotational" % major_minor,
        "/sys/class/block/%s/queue/rotational" % major_minor,
    ]
    real = os.path.realpath("/sys/dev/block/%s" % major_minor)
    if real:
        candidates.append(os.path.join(os.path.dirname(real), "queue", "rotational"))
    for path in candidates:
        try:
            with open(path, "r") as f:
                return int(f.read().strip())
        except Exception:
            pass
    return None


def _leaf_block_devices_from_sysfs(path, seen=None):
    if seen is None:
        seen = set()
    if not path:
        return []
    path = os.path.realpath(path)
    if path in seen:
        return []
    seen.add(path)

    slaves = []
    try:
        for name in os.listdir(os.path.join(path, "slaves")):
            slave = os.path.realpath(os.path.join(path, "slaves", name))
            if slave and os.path.exists(slave):
                slaves.append(slave)
    except Exception:
        pass
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
    return [{
        "sysfs": path,
        "devnode": _devnode_from_sysfs(path),
        "major_minor": major_minor,
        "rotational": rotational_value(major_minor) if major_minor else None,
    }]


def resolve_leaf_block_devices(major_minor):
    root = _sysfs_dev_path(major_minor)
    return _leaf_block_devices_from_sysfs(root) if root else []


def leaf_matches_patterns(leaves, patterns):
    for leaf in leaves:
        devnode = leaf.get("devnode")
        if not devnode:
            continue
        if any(fnmatch.fnmatch(devnode, str(pattern)) for pattern in patterns):
            return True
    return False


def classify_rotational(leaves):
    values = [leaf.get("rotational") for leaf in leaves if leaf.get("rotational") in (0, 1)]
    if not values:
        return None
    if all(value == 0 for value in values):
        return 0
    if all(value == 1 for value in values):
        return 1
    return None


def probe_backend(backend, mounts=None):
    if mounts is None:
        mounts = mount_table()
    mount_point = os.path.normpath(str(backend["mount_point"]))
    info = mounts.get(mount_point)
    if info is None:
        return {"usable": False, "reason": "%s is not an actual mount point" % mount_point, "mount_point": mount_point}

    source_patterns = backend.get("source_patterns", ["*"])
    filesystem_patterns = backend.get("filesystem_patterns", ["*"])
    leaves = resolve_leaf_block_devices(info["major_minor"])

    direct_match = any(fnmatch.fnmatch(info["source"], str(p)) for p in source_patterns)
    leaf_match = leaf_matches_patterns(leaves, source_patterns)
    if not (direct_match or leaf_match):
        leaf_names = [leaf.get("devnode") for leaf in leaves if leaf.get("devnode")]
        return dict(info, usable=False, reason="source %s / backing devices %s do not match %s" % (info["source"], leaf_names or ["unknown"], source_patterns))

    if not any(fnmatch.fnmatch(info["filesystem"], str(p)) for p in filesystem_patterns):
        return dict(info, usable=False, reason="filesystem %s does not match %s" % (info["filesystem"], filesystem_patterns))

    expected_rot = backend.get("rotational", None)
    actual_rot = classify_rotational(leaves)
    if actual_rot is None:
        actual_rot = rotational_value(info["major_minor"])
    if expected_rot is not None:
        expected = 1 if expected_rot else 0
        if actual_rot is None:
            return dict(info, usable=False, reason="cannot determine rotational property of backing device(s)")
        if actual_rot != expected:
            return dict(info, usable=False, reason="backing-device rotational=%s, expected %s" % (actual_rot, expected))

    try:
        stat = os.statvfs(mount_point)
        block = int(stat.f_frsize or stat.f_bsize)
        total = block * int(stat.f_blocks)
        free = block * int(stat.f_bavail)
    except Exception as exc:
        return dict(info, usable=False, reason="statvfs failed: %s" % exc)

    return dict(info, usable=True, reason="", rotational=actual_rot,
                backing_devices=[leaf.get("devnode") for leaf in leaves if leaf.get("devnode")],
                total=total, free=free)


# ---------------------------------------------------------------------------
# Persistent state and subtype selection
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
        pbs_home = pbs_conf.get("PBS_MOM_HOME", pbs_conf.get("PBS_HOME", "/var/spool/pbs"))
        self.root = os.path.join(pbs_home, "mom_priv", str(cfg.get("state_subdir", "workspace")))
        self.jobs = os.path.join(self.root, "jobs")
        self.cache = os.path.join(self.root, "cache")
        self.selection = os.path.join(self.root, "selected_subtypes.json")
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

    def load_selections(self):
        try:
            with open(self.selection, "r") as f:
                value = json.load(f)
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def save_selections(self, value):
        tmp = self.selection + ".tmp.%d" % os.getpid()
        with open(tmp, "w") as f:
            json.dump(value, f, sort_keys=True, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.selection)


class Selection(object):
    """Persistent one-subtype-per-top-level-resource selection."""

    def __init__(self, cfg, state):
        self.cfg = cfg
        self.state = state

    def backend_by_name(self, resource, name):
        for backend in self.cfg[resource].get("subtypes", []):
            if str(backend["name"]) == str(name):
                return backend
        return None

    def ensure(self, resource, mounts=None):
        """Return the persistent selected subtype and backend.

        Existing selection is retained as long as that subtype remains in the
        configuration, even when it currently probes unusable.  A new subtype
        is selected only if no selection exists or the configured subtype was
        removed/renamed.
        """
        selections = self.state.load_selections()
        selected = selections.get(resource)
        backend = self.backend_by_name(resource, selected) if selected else None
        if backend is not None:
            return str(selected), backend, False

        candidates = []
        mounts = mount_table() if mounts is None else mounts
        for candidate in self.cfg[resource].get("subtypes", []):
            probe = probe_backend(candidate, mounts)
            if probe.get("usable"):
                candidates.append((int(candidate.get("priority", 0)), str(candidate["name"]), candidate))
        candidates.sort(key=lambda x: (-x[0], x[1]))
        if not candidates:
            return None, None, False

        _, name, backend = candidates[0]
        selections[resource] = name
        self.state.save_selections(selections)
        log(pbs.EVENT_DEBUG, "selected persistent subtype %s=%s" % (resource, name))
        return name, backend, True


# ---------------------------------------------------------------------------
# Dead/unmanaged-data accounting
# ---------------------------------------------------------------------------

def scan_unmanaged(account_root, live_paths, mount_point):
    account_root = os.path.normpath(account_root)
    live_paths = set(os.path.normpath(path) for path in live_paths)
    try:
        device = os.stat(mount_point).st_dev
    except Exception:
        return 0

    total = 0
    for root, dirs, files in os.walk(account_root, topdown=True, followlinks=False):
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
                if os.path.islink(path) or os.stat(path).st_dev != device:
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
                if stat.st_dev == device:
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
        value = max(0, int(probe["total"]) - int(probe["free"]))

    refresh = int(backend.get("scan_refresh", cfg.get("scan_refresh", 7200)))
    scan_enabled = bool(backend.get("scan_enabled", True))
    if scan_enabled and (stamp == 0 or time.time() - stamp >= refresh):
        if not pid_running(pidfile):
            pid = os.fork()
            if pid == 0:
                try:
                    root = backend.get("accounting_root", backend["mount_point"])
                    result = scan_unmanaged(root, live_paths, backend["mount_point"])
                    tmp = cache + ".tmp.%d" % os.getpid()
                    with open(tmp, "w") as f:
                        json.dump({"bytes": result, "time": time.time()}, f)
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
        live_ids = set(str(jobid) for jobid in getattr(event, "job_list", {}).keys())
        now = time.time()
        out = {}
        for jobid, value in self.state.all().items():
            if jobid in live_ids or now - float(value.get("created", 0)) < 60:
                out[jobid] = value
        return out

    def reserved(self, states, resource=None, subtype=None, exclude=None):
        total = 0
        for jobid, value in states.items():
            if exclude is not None and str(jobid) == str(exclude):
                continue
            if resource is not None and value.get("requested_resource") != resource:
                continue
            if subtype is not None and value.get("subtype") != subtype:
                continue
            total += int(value.get("requested_bytes", 0))
        return total

    def status(self, event, resource, subtype, backend, exclude=None, probe=None):
        probe = probe_backend(backend) if probe is None else probe
        if not probe.get("usable"):
            return {"usable": False, "reason": probe.get("reason", "unusable"), "reservable": 0, "resource": resource, "subtype": subtype}

        states = self.live_states(event)
        filtered = {jobid: value for jobid, value in states.items() if exclude is None or str(jobid) != str(exclude)}
        live_paths = [value["path"] for value in filtered.values() if value.get("requested_resource") == resource and value.get("subtype") == subtype and value.get("path")]
        unmanaged = unmanaged_cached(self.state, "%s.%s" % (resource, subtype), backend, live_paths, self.cfg, probe)
        reserved = self.reserved(filtered, resource=resource, subtype=subtype)
        workspace = max(0, int(probe["total"]) - unmanaged)
        headroom = max(0, workspace - reserved)
        reservable = max(0, min(headroom, int(probe["free"])))
        return dict(probe, usable=True, resource=resource, subtype=subtype,
                    unmanaged=unmanaged, reserved=reserved, workspace=workspace,
                    reservable=reservable)


# ---------------------------------------------------------------------------
# Directory/environment management
# ---------------------------------------------------------------------------

def create_dir(path, backend, job, user, group):
    if not path_under(path, backend["mount_point"]):
        raise RuntimeError("workspace path %s is outside %s" % (path, backend["mount_point"]))
    umask = getattr(job, "umask", None)
    rerun_prefix = str(backend.get("rerun_prefix", ".run_count"))
    if os.path.isdir(path):
        backup = os.path.join(path, "%s-%d" % (rerun_prefix, int(getattr(job, "run_count", 0))))
        if os.path.exists(backup):
            raise RuntimeError("rerun backup already exists: %s" % backup)
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
            log(pbs.EVENT_DEBUG, "removed empty workspace %s" % path)
        except OSError as exc:
            if exc.errno == errno.ENOTEMPTY:
                log(pbs.EVENT_DEBUG, "preserving non-empty workspace %s" % path)
            elif exc.errno != errno.ENOENT:
                raise
    else:
        shutil.rmtree(path)


def set_env(job, path, requested_resource, subtype, local_size, total_size):
    variables = job.Variable_List
    for key in ("SCRATCHDIR", "SCRATCH", "SINGULARITY_TMPDIR", "SINGULARITY_CACHEDIR"):
        variables[key] = path

    variables["SCRATCH_VOLUME"] = int(local_size)
    variables["PBS_RESC_SCRATCH_VOLUME"] = int(local_size)
    variables["TORQUE_RESC_SCRATCH_VOLUME"] = int(local_size)
    variables["PBS_RESC_TOTAL_SCRATCH_VOLUME"] = int(total_size)
    variables["TORQUE_RESC_TOTAL_SCRATCH_VOLUME"] = int(total_size)
    variables["SCRATCH_RESOURCE"] = requested_resource
    variables["SCRATCH_SUBTYPE"] = subtype

    if requested_resource == "scratch_local":
        variables["SCRATCH_TYPE"] = "local"
    elif requested_resource == "scratch_shared":
        variables["SCRATCH_TYPE"] = "shared"
    elif requested_resource == "scratch_shm":
        variables["SCRATCH_TYPE"] = "shm"
    else:
        variables["SCRATCH_TYPE"] = "none"

    if requested_resource != "none":
        env_name = "PBS_RESC_" + requested_resource.upper()
        torque_name = "TORQUE_RESC_" + requested_resource.upper()
        variables[env_name] = int(local_size)
        variables[torque_name] = int(local_size)


# ---------------------------------------------------------------------------
# Hook
# ---------------------------------------------------------------------------

class WorkspaceHook(object):
    def __init__(self):
        self.cfg = load_config()
        self.state = State(self.cfg)
        self.selection = Selection(self.cfg, self.state)
        self.acct = Accounting(self.cfg, self.state)

    def queue_modify(self, event):
        job = event.job
        try:
            select = str(job.Resource_List["select"])
        except Exception:
            select = ""
        if not select:
            return True

        for chunk in select.split("+"):
            found = []
            for resource in USER_RESOURCES:
                if re.search(r"(^|:)" + re.escape(resource) + r"=", chunk):
                    found.append(resource)

            # scratch_*_subtype are properties and do not count as scratch
            # reservations. Any other scratch_* resource is rejected.
            all_scratch = re.findall(r"(?:^|:)(scratch_[A-Za-z0-9_]+)=", chunk)
            allowed = set(USER_RESOURCES + SUBTYPE_RESOURCES)
            unsupported = [name for name in all_scratch if name not in allowed]
            if unsupported:
                event.reject("unsupported scratch resource(s) in select chunk: %s" % ", ".join(sorted(set(unsupported))))
                return False

            if len(found) > 1:
                event.reject("only one top-level scratch resource is allowed per select chunk; found: %s" % ", ".join(found))
                return False

            shm_match = re.search(r"(?:^|:)scratch_shm=([^:]+)", chunk, re.I)
            if shm_match and not _bool_true(shm_match.group(1)):
                event.reject("scratch_shm is a boolean resource and must be requested as scratch_shm=true")
                return False

            # A subtype property without the matching top-level resource is
            # allowed: it can be used purely as a node-selection property.

        if re.search(r"(^|:)scratch_shared=", select):
            try:
                place = str(job.Resource_List["place"])
            except Exception:
                place = ""
            if "group=" not in place:
                job.Resource_List["place"] = pbs.place((place + ":" if place else "") + "group=cluster")
        return True

    def _publish_size(self, event, resource, capacity):
        for vnode_name in list(event.vnode_list.keys()):
            if vnode_is_local(vnode_name):
                event.vnode_list[vnode_name].resources_available[resource] = bytes_to_pbs_size(capacity)

    def _publish_bool(self, event, resource, available):
        for vnode_name in list(event.vnode_list.keys()):
            if vnode_is_local(vnode_name):
                event.vnode_list[vnode_name].resources_available[resource] = bool(available)

    def _publish_subtype(self, event, resource, subtype):
        if subtype is None:
            return
        attr = resource + "_subtype"
        for vnode_name in list(event.vnode_list.keys()):
            if vnode_is_local(vnode_name):
                event.vnode_list[vnode_name].resources_available[attr] = str(subtype)

    def _selected(self, resource, mounts=None):
        with self.state.lock():
            return self.selection.ensure(resource, mounts)

    def publish(self, event):
        mounts = mount_table()
        for resource in USER_RESOURCES:
            section = self.cfg.get(resource, {})
            if not bool(section.get("discover", True)):
                continue

            subtype, backend, _ = self._selected(resource, mounts)
            if subtype is None or backend is None:
                log(pbs.EVENT_DEBUG, "%s has no usable subtype to select" % resource)
                if resource in SIZE_RESOURCES:
                    self._publish_size(event, resource, 0)
                else:
                    self._publish_bool(event, resource, False)
                continue

            # Always publish the persisted subtype, including while its
            # backend is temporarily unavailable.
            self._publish_subtype(event, resource, subtype)
            probe = probe_backend(backend, mounts)

            if resource in BOOLEAN_RESOURCES:
                self._publish_bool(event, resource, bool(probe.get("usable")))
                if not probe.get("usable"):
                    log(pbs.EVENT_DEBUG, "%s subtype=%s unavailable: %s" % (resource, subtype, probe.get("reason", "unusable")))
                else:
                    log(pbs.EVENT_DEBUG, "%s subtype=%s available" % (resource, subtype))
                continue

            status = self.acct.status(event, resource, subtype, backend, probe=probe)
            states = self.acct.live_states(event)
            direct_reserved = self.acct.reserved(states, resource=resource, subtype=subtype)
            capacity = direct_reserved + int(status.get("reservable", 0)) if status.get("usable") else direct_reserved
            self._publish_size(event, resource, capacity)

            if status.get("usable"):
                log(pbs.EVENT_DEBUG, "%s subtype=%s source=%s backing=%s fs=%s free=%d unmanaged=%d reserved=%d reservable=%d" % (
                    resource, subtype, status["source"], status.get("backing_devices", []), status["filesystem"], status["free"], status["unmanaged"], status["reserved"], status["reservable"]))
            else:
                log(pbs.EVENT_DEBUG, "%s subtype=%s unavailable: %s" % (resource, subtype, status.get("reason", "unusable")))
        return True

    def startup(self, event):
        return self.publish(event)

    def periodic(self, event):
        live = set(str(jobid) for jobid in event.job_list.keys())
        now = time.time()
        with self.state.lock():
            for jobid, value in self.state.all().items():
                if jobid not in live and now - float(value.get("created", 0)) >= 60:
                    self.state.delete(jobid)
        return self.publish(event)

    def _get_selected_backend(self, resource):
        with self.state.lock():
            subtype, backend, _ = self.selection.ensure(resource)
        if subtype is None or backend is None:
            raise RuntimeError("%s has no selected subtype on this node" % resource)
        return subtype, backend

    def begin(self, event):
        job = event.job
        resources = local_resources(job)
        requested = []
        for resource in USER_RESOURCES:
            if resource in BOOLEAN_RESOURCES:
                if bool(resources.get(resource, False)):
                    requested.append((resource, True))
            else:
                amount = int(resources.get(resource, 0))
                if amount > 0:
                    requested.append((resource, amount))

        if len(requested) > 1:
            event.reject("pbs_workspace: local execution chunk contains more than one top-level scratch resource")
            return False

        if not requested:
            user, _ = identity(job)
            path = expand_template(self.cfg.get("fallback_dir", "/var/tmp/pbs.$PBS_JOBID"), job, user)
            set_env(job, path, "none", "none", 0, 0)
            return True

        requested_resource, requested_value = requested[0]
        user, group = identity(job)
        try:
            subtype, backend = self._get_selected_backend(requested_resource)
        except RuntimeError as exc:
            event.reject("pbs_workspace: %s" % exc)
            return False

        probe = probe_backend(backend)
        if not probe.get("usable"):
            event.reject("pbs_workspace: %s subtype %s is unavailable: %s" % (requested_resource, subtype, probe.get("reason", "unusable")))
            return False

        path = expand_template(backend["job_dir"], job, user)

        if requested_resource == "scratch_shm":
            with self.state.lock():
                create_dir(path, backend, job, user, group)
                self.state.save(job.id, {
                    "jobid": str(job.id), "created": time.time(), "kind": "shm",
                    "requested_resource": requested_resource, "subtype": subtype,
                    "requested_bytes": 0, "path": path,
                    "mount_point": backend["mount_point"],
                    "preserve_nonempty": bool(backend.get("preserve_nonempty", False)),
                })
            set_env(job, path, requested_resource, subtype, 0, 0)
            return True

        requested_bytes = int(requested_value)
        shared = requested_resource == "scratch_shared"
        # Shared directory/state is owned by mother-superior only.  Sister
        # MoMs still receive identical environment variables.
        if (not shared) or job.in_ms_mom():
            with self.state.lock():
                status = self.acct.status(event, requested_resource, subtype, backend, exclude=str(job.id), probe=probe)
                if not status.get("usable") or int(status.get("reservable", 0)) < requested_bytes:
                    event.reject("pbs_workspace: %s subtype %s cannot satisfy %d bytes; %d bytes reservable" % (
                        requested_resource, subtype, requested_bytes, int(status.get("reservable", 0))))
                    return False
                create_dir(path, backend, job, user, group)
                self.state.save(job.id, {
                    "jobid": str(job.id), "created": time.time(),
                    "kind": "shared" if shared else "local",
                    "requested_resource": requested_resource, "subtype": subtype,
                    "requested_bytes": requested_bytes, "path": path,
                    "mount_point": backend["mount_point"],
                    "preserve_nonempty": bool(backend.get("preserve_nonempty", True)),
                })

        set_env(job, path, requested_resource, subtype, requested_bytes, total_request(job, requested_resource))
        log(pbs.EVENT_DEBUG, "%s: %s=%d subtype=%s -> %s" % (job.id, requested_resource, requested_bytes, subtype, path))
        return True

    def end(self, event):
        value = self.state.load(event.job.id)
        if value is None:
            return True
        if value.get("kind") == "shared" and not event.job.in_ms_mom():
            return True
        try:
            remove_dir(value["path"], bool(value.get("preserve_nonempty", True)))
        finally:
            with self.state.lock():
                self.state.delete(event.job.id)
        return True

    def resize(self, event):
        event.reject("pbs_workspace: dynamic scratch resizing is not supported")
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
    elif hasattr(pbs, "EXECJOB_ABORT") and event.type == pbs.EXECJOB_ABORT:
        result = hook.end(event)
    elif hasattr(pbs, "EXECJOB_RESIZE") and event.type == pbs.EXECJOB_RESIZE:
        result = hook.resize(event)
    if result is not False:
        event.accept()


try:
    main()
except SystemExit:
    raise
except Exception as exc:
    log(pbs.EVENT_ERROR, "%s\n%s" % (exc, traceback.format_exc()))
    try:
        pbs.event().reject("pbs_workspace failed: %s" % exc)
    except Exception:
        pass
