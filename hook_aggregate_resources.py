# coding: utf-8
"""
OpenPBS server-periodic hook for aggregating vnode string resources.

For each configured collection, the hook reads one resources_available
resource from every vnode, creates a sorted unique union of all values, and
publishes the result as a server resources_available string_array.

Only string and string_array source values are considered.  Collections whose
source resolves to another PBS/Python type are silently skipped.

The target resource is changed only when its normalized value differs from the
newly collected value.
"""

import json
import os
import traceback

import pbs


DEFAULT_CONFIG = {
    "collections": []
}


def log(level, msg):
    pbs.logmsg(level, "pbs_aggregate_resources: " + str(msg))


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = os.environ.get("PBS_HOOK_CONFIG_FILE")
    if path and os.path.isfile(path):
        with open(path, "r") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update(data)
    return cfg


def _string_values(value):
    """
    Convert a PBS string or string_array value to a list of strings.

    Return:
        list  - valid string/string_array value
        None  - unsupported resource type

    PBS exposes scalar string resources as Python strings.  string_array
    resources are normally list-like; the type-name fallback covers PBS
    string-array wrapper classes without accepting unrelated iterable types.
    """
    if value is None:
        return []

    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []

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


def _normalized_target(value):
    """
    Normalize a target string_array into a sorted unique list.

    Targets are produced by this hook as comma-separated string_array values,
    therefore a scalar representation can safely be split on commas here.
    """
    if value is None:
        return []

    if isinstance(value, str):
        return sorted(set(
            item.strip() for item in value.split(",") if item.strip()
        ))

    values = _string_values(value)
    if values is None:
        return None

    return sorted(set(values))


def _get_resource(resources, name):
    try:
        return resources[name]
    except (KeyError, AttributeError, TypeError):
        return None


def collect(server, source):
    """
    Return the sorted unique values of source across all server vnodes.

    If a non-string/non-string_array value is encountered, return None so the
    complete collection is silently skipped.
    """
    values = set()

    for vnode in server.vnodes():
        raw = _get_resource(vnode.resources_available, source)
        parts = _string_values(raw)

        if parts is None:
            return None

        values.update(parts)

    return sorted(values)


def update_collection(server, source, target):
    new_values = collect(server, source)

    # Unsupported source type: deliberately silent and leave target untouched.
    if new_values is None:
        return False

    old_raw = _get_resource(server.resources_available, target)
    old_values = _normalized_target(old_raw)

    # A mis-typed target cannot be safely updated; leave it untouched.
    if old_values is None:
        return False

    if old_values == new_values:
        return False

    # PBS string_array resources accept a comma-separated string.  None clears
    # a stale aggregate when no vnode currently publishes the source resource.
    server.resources_available[target] = (
        ",".join(new_values) if new_values else None
    )

    log(
        pbs.EVENT_DEBUG,
        "updated server resources_available.%s from [%s] to [%s]"
        % (target, ",".join(old_values), ",".join(new_values))
    )
    return True


def run(cfg):
    server = pbs.server()

    collections = cfg.get("collections", [])
    if not isinstance(collections, list):
        return

    for item in collections:
        if not isinstance(item, dict):
            continue

        source = item.get("source")
        target = item.get("target")

        if not isinstance(source, str) or not source.strip():
            continue
        if not isinstance(target, str) or not target.strip():
            continue

        source = source.strip()
        target = target.strip()

        if source == target:
            continue

        update_collection(server, source, target)


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
