# coding: utf-8
"""
OpenPBS server-periodic hook for aggregating vnode string resources.

For each configured collection, the hook reads one resources_available
resource from every vnode, creates a sorted unique union of all values, and
publishes the result as a server resources_available string_array.

Important OpenPBS detail:
    pbs.server() returns a read-only server object in a periodic hook.
    Therefore server resources cannot be changed by assigning to
    pbs.server().resources_available.  When an aggregate changes, this hook
    performs one local qmgr SET/UNSET operation instead.

Only string and string_array source values are considered.  Unsupported source
types are silently skipped.

The target is changed only when its normalized value differs from the newly
collected value.
"""

import json
import os
import re
import subprocess
import traceback

import pbs


RESOURCE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def log(level, msg):
    pbs.logmsg(level, "pbs_aggregate_resources: " + str(msg))


def load_config():
    path = os.environ.get("PBS_HOOK_CONFIG_FILE")
    if not path or not os.path.isfile(path):
        return {"collections": []}

    with open(path, "r") as f:
        data = json.load(f)

    return data if isinstance(data, dict) else {"collections": []}


def read_pbs_exec():
    """
    Determine PBS_EXEC without assuming a particular installation prefix.

    Prefer the environment if PBS supplies it.  Otherwise read PBS_CONF_FILE
    (or /etc/pbs.conf) and obtain PBS_EXEC from there.
    """
    value = os.environ.get("PBS_EXEC")
    if value:
        return value

    conf = os.environ.get("PBS_CONF_FILE", "/etc/pbs.conf")

    try:
        with open(conf, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue

                key, value = line.split("=", 1)
                if key.strip() == "PBS_EXEC":
                    value = value.strip().strip('"').strip("'")
                    if value:
                        return value
    except OSError:
        pass

    return None


def get_qmgr_path(config):
    """
    Return an absolute qmgr path.

    An explicit JSON "qmgr" entry takes precedence.  Otherwise qmgr is derived
    from PBS_EXEC.
    """
    qmgr = config.get("qmgr")

    if isinstance(qmgr, str) and qmgr:
        if os.path.isabs(qmgr):
            return qmgr
        return None

    pbs_exec = read_pbs_exec()
    if not pbs_exec:
        return None

    return os.path.join(pbs_exec, "bin", "qmgr")


def string_values(value):
    """
    Convert a PBS string or string_array source value to a list of strings.

    Return:
        list  - valid string/string_array value
        None  - unsupported resource type
    """
    if value is None:
        return []

    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []

    # PBS string_array resources are exposed as list-like values.
    if isinstance(value, (list, tuple)):
        result = []
        for item in value:
            if not isinstance(item, str):
                return None
            text = item.strip()
            if text:
                result.append(text)
        return result

    typename = type(value).__name__.lower()
    if "string_array" in typename:
        result = []
        try:
            for item in value:
                if not isinstance(item, str):
                    return None
                text = item.strip()
                if text:
                    result.append(text)
        except TypeError:
            return None
        return result

    return None


def get_resource(resources, name):
    try:
        return resources[name]
    except (KeyError, AttributeError, TypeError):
        return None


def normalize_target(value):
    """
    Normalize the current server target into a sorted unique list.
    """
    if value is None:
        return []

    if isinstance(value, str):
        return sorted(set(
            item.strip() for item in value.split(",") if item.strip()
        ))

    values = string_values(value)
    if values is None:
        return None

    return sorted(set(values))


def collect_from_vnodes(vnode_list, source):
    """
    Return the sorted unique union of source over all vnodes.

    Unset values are ignored.  If a defined value has an unsupported type,
    the entire collection is silently skipped.
    """
    values = set()

    for vnode in vnode_list.values():
        raw = get_resource(vnode.resources_available, source)
        parts = string_values(raw)

        if parts is None:
            return None

        values.update(parts)

    return sorted(values)


def qmgr_quote(value):
    """
    Quote a qmgr string value.

    qmgr receives the command directly (shell=False), so this protects only
    qmgr's own command parser, not a shell.
    """
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def run_qmgr(qmgr, command):
    proc = subprocess.Popen(
        [qmgr, "-c", command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        close_fds=True
    )

    stdout, stderr = proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(
            "qmgr failed (rc=%d): %s%s"
            % (
                proc.returncode,
                stderr.strip(),
                ("; " + stdout.strip()) if stdout.strip() else ""
            )
        )


def update_target(qmgr, source, target, old_values, new_values):
    """
    Update or clear one server target with qmgr.
    """
    if new_values:
        value = ",".join(new_values)
        command = (
            "set server resources_available.%s = %s"
            % (target, qmgr_quote(value))
        )
    else:
        command = "unset server resources_available.%s" % target

    run_qmgr(qmgr, command)

    log(
        pbs.EVENT_DEBUG,
        "updated server resources_available.%s from [%s] to [%s] "
        "(source=%s)"
        % (
            target,
            ",".join(old_values),
            ",".join(new_values),
            source
        )
    )


def run(config):
    event = pbs.event()
    vnode_list = event.vnode_list
    server = pbs.server()  # read-only; used only for reading current targets

    collections = config.get("collections", [])
    if not isinstance(collections, list):
        return

    qmgr = get_qmgr_path(config)
    if not qmgr or not os.path.isabs(qmgr) or not os.access(qmgr, os.X_OK):
        raise RuntimeError("cannot locate executable qmgr using an absolute path")

    for item in collections:
        if not isinstance(item, dict):
            continue

        source = item.get("source")
        target = item.get("target")

        if not isinstance(source, str) or not RESOURCE_NAME_RE.match(source):
            continue
        if not isinstance(target, str) or not RESOURCE_NAME_RE.match(target):
            continue
        if source == target:
            continue

        new_values = collect_from_vnodes(vnode_list, source)

        # Unsupported source type: deliberately silent and leave target alone.
        if new_values is None:
            continue

        old_raw = get_resource(server.resources_available, target)
        old_values = normalize_target(old_raw)

        # A wrongly typed target is also left untouched.
        if old_values is None:
            continue

        if old_values == new_values:
            continue

        update_target(
            qmgr=qmgr,
            source=source,
            target=target,
            old_values=old_values,
            new_values=new_values
        )


def main():
    event = pbs.event()

    if event.type == pbs.PERIODIC:
        run(load_config())

    event.accept()


try:
    main()
except SystemExit:
    raise
except Exception as exc:
    log(pbs.EVENT_ERROR, "%s\n%s" % (exc, traceback.format_exc()))
    try:
        pbs.event().reject("pbs_aggregate_resources failed: %s" % exc)
    except Exception:
        pass
