# coding: utf-8
"""
OpenPBS server-periodic hook for aggregating vnode string resources.

For each configured collection, the hook reads one resources_available
resource from every vnode, creates a sorted unique union of all values, and
publishes the result as a server resources_available string_array.

Important implementation details:
- pbs.server() is read-only in a periodic hook, therefore changed server
  resources are written through the local qmgr executable.
- PBS may expose string_array members as PBS-specific string-like wrapper
  objects.  Every member is explicitly converted to a plain Python str before
  deduplication.  This is required because two wrapper objects may display the
  same text while not comparing/hashing identically.
- Existing target values preserve duplicates during comparison so stale
  duplicates can be detected and repaired.
- Changed targets are replaced using UNSET followed by SET.

Only string and string_array sources are considered. Unsupported source types
are silently skipped.
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
    qmgr = config.get("qmgr")

    if isinstance(qmgr, str) and qmgr:
        return qmgr if os.path.isabs(qmgr) else None

    pbs_exec = read_pbs_exec()
    if not pbs_exec:
        return None

    return os.path.join(pbs_exec, "bin", "qmgr")


def plain_string(value):
    """
    Return a normalized plain Python str.

    str(...) is intentional even for str-like PBS wrapper objects.
    """
    text = str(value).strip()
    return text if text else None


def string_values(value):
    """
    Convert a PBS string or string_array value to a list of *plain Python str*.

    Return:
        list  - valid string/string_array value
        None  - unsupported type
    """
    if value is None:
        return []

    if isinstance(value, str):
        text = plain_string(value)
        return [text] if text else []

    if isinstance(value, (list, tuple)):
        result = []
        for item in value:
            if not isinstance(item, str):
                return None
            text = plain_string(item)
            if text:
                result.append(text)
        return result

    typename = type(value).__name__.lower()
    if "string_array" in typename:
        result = []
        try:
            for item in value:
                # PBS may return a string-like wrapper rather than exact str.
                text = plain_string(item)
                if text:
                    result.append(text)
        except (TypeError, ValueError):
            return None
        return result

    return None


def get_resource(resources, name):
    try:
        return resources[name]
    except (KeyError, AttributeError, TypeError):
        return None


def normalize_existing_target(value):
    """
    Normalize existing target WITHOUT deduplication.

    Duplicates must remain visible so malformed server arrays are repaired.
    """
    if value is None:
        return []

    if isinstance(value, str):
        return sorted(
            plain_string(item)
            for item in str(value).split(",")
            if plain_string(item)
        )

    values = string_values(value)
    if values is None:
        return None

    return sorted(values)


def collect_from_vnodes(vnode_list, source):
    """
    Return a canonical sorted unique list of source values from all vnodes.

    Values are converted to plain Python str before entering the set.
    A second dict-based canonicalization pass is retained deliberately as a
    defensive guard against unusual PBS wrapper behaviour.
    """
    values = set()

    for vnode in vnode_list.values():
        raw = get_resource(vnode.resources_available, source)
        parts = string_values(raw)

        if parts is None:
            return None

        for item in parts:
            # Explicit plain-str conversion before hashing/equality.
            text = plain_string(item)
            if text:
                values.add(text)

    # Defensive second canonicalization by textual key.
    canonical = {}
    for item in values:
        text = plain_string(item)
        if text:
            canonical[text] = text

    return sorted(canonical.keys())


def qmgr_quote(value):
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
    run_qmgr(
        qmgr,
        "unset server resources_available.%s" % target
    )

    if new_values:
        value = ",".join(new_values)
        run_qmgr(
            qmgr,
            "set server resources_available.%s = %s"
            % (target, qmgr_quote(value))
        )

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
    server = pbs.server()

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

        if new_values is None:
            continue

        old_raw = get_resource(server.resources_available, target)
        old_values = normalize_existing_target(old_raw)

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
