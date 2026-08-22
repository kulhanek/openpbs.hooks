# coding: utf-8
"""
OpenPBS execution-host discovery hook for OS, cgroup version, and PBS server.

Published vnode resources
-------------------------
* os         : site-defined OS token selected from PRETTY_NAME
* osfamily   : site-defined OS family selected from PRETTY_NAME
* cgroups    : string_array containing either "v1" or "v2"
* pbs_server : PBS server name

The OS mapping is read from the hook JSON configuration.  Each distro entry
contains a shell-style glob in "name" which is matched, in configuration
order, against PRETTY_NAME from /etc/os-release or /usr/lib/os-release.
The first matching entry wins.

Recommended events
------------------
    exechost_startup, exechost_periodic
"""

import fnmatch
import json
import os
import traceback

import pbs


DEFAULT_CONFIG = {
    "distros": []
}


def log(level, msg):
    pbs.logmsg(level, "pbs_discovery_node: " + str(msg))


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = os.environ.get("PBS_HOOK_CONFIG_FILE")
    if path and os.path.isfile(path):
        with open(path, "r") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            cfg.update(loaded)
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
            value = str(value)
            names.add(value)
            names.add(value.split(".")[0])

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


def read_pretty_name():
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            with open(path, "r") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    if key.strip() == "PRETTY_NAME":
                        return value.strip().strip('"').strip("'")
        except IOError:
            continue
    raise RuntimeError("PRETTY_NAME not found in /etc/os-release or /usr/lib/os-release")


def map_os(pretty_name, cfg):
    distros = cfg.get("distros", [])
    if not isinstance(distros, list):
        raise RuntimeError("configuration item 'distros' must be a list")

    for entry in distros:
        if not isinstance(entry, dict):
            continue
        pattern = str(entry.get("name", ""))
        if pattern and fnmatch.fnmatchcase(pretty_name, pattern):
            os_value = str(entry.get("os", "")).strip()
            family_value = str(entry.get("osfamily", "")).strip()
            if not os_value or not family_value:
                raise RuntimeError(
                    "matching distro entry '%s' must define both os and osfamily" % pattern
                )
            return os_value, family_value

    raise RuntimeError("no distro mapping matches PRETTY_NAME=%r" % pretty_name)


def detect_cgroups():
    """Return 'v1' or 'v2'.

    An explicit systemd.unified_cgroup_hierarchy kernel option takes
    precedence.  If it is absent, inspect the mounted cgroup filesystem.
    """
    try:
        with open("/proc/cmdline", "r") as f:
            cmdline = f.read().split()
    except Exception:
        cmdline = []

    for arg in cmdline:
        if arg == "systemd.unified_cgroup_hierarchy=0":
            return "v1"
        if arg == "systemd.unified_cgroup_hierarchy=1":
            return "v2"

    if os.path.isfile("/sys/fs/cgroup/cgroup.controllers"):
        return "v2"

    try:
        with open("/proc/self/mountinfo", "r") as f:
            for line in f:
                if " - cgroup2 " in line:
                    return "v2"
                if " - cgroup " in line:
                    return "v1"
    except Exception:
        pass

    raise RuntimeError("unable to determine cgroup hierarchy version")


def pbs_server_name():
    try:
        value = pbs.server().name
        if value:
            return str(value)
    except Exception:
        pass

    value = read_pbs_conf().get("PBS_SERVER")
    if value:
        return value

    raise RuntimeError("PBS server name not available")


class NodeDiscovery(object):
    def __init__(self):
        self.cfg = load_config()

    def discover(self):
        pretty_name = read_pretty_name()
        os_value, osfamily_value = map_os(pretty_name, self.cfg)
        return {
            "os": os_value,
            "osfamily": osfamily_value,
            "cgroups": detect_cgroups(),
            "pbs_server": pbs_server_name(),
            "_pretty_name": pretty_name,
        }

    def publish(self, event):
        resources = self.discover()
        pretty_name = resources.pop("_pretty_name")
        updated = False

        for name in list(event.vnode_list.keys()):
            if not vnode_is_local(name):
                continue
            vnode = event.vnode_list[name]
            for key, value in resources.items():
                vnode.resources_available[key] = value
            updated = True

        if not updated:
            raise RuntimeError("local vnode not found in vnode_list")

        log(
            pbs.EVENT_DEBUG,
            "published PRETTY_NAME=%r os=%s osfamily=%s cgroups=%s pbs_server=%s" % (
                pretty_name,
                resources["os"],
                resources["osfamily"],
                resources["cgroups"],
                resources["pbs_server"],
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
        pbs.event().reject("pbs_discovery_node failed: %s" % exc)
    except Exception:
        pass
