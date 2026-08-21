# coding: utf-8
"""
OpenPBS server-periodic hook for aggregating vnode string resources.

The hook collects configured vnode resources, builds sorted unique lists, and
stores the result in a generated JSON state file under PBS_HOME.

The output path is configured as a path relative to PBS_HOME.  The hook writes
the file atomically and replaces it only when the aggregated resource content
changes.

Only string and string_array source values are considered. Unsupported source
types are silently skipped.
"""

import json
import os
import tempfile
import time
import traceback

import pbs


DEFAULT_STATE_FILE = "server_priv/hooks/hook_data/aggregate_resources.json"


def log(level, msg):
    pbs.logmsg(level, "pbs_aggregate_resources: " + str(msg))


def load_config():
    path = os.environ.get("PBS_HOOK_CONFIG_FILE")
    if not path or not os.path.isfile(path):
        return {
            "state_file": DEFAULT_STATE_FILE,
            "collections": []
        }

    with open(path, "r") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        data = {}

    if "state_file" not in data:
        data["state_file"] = DEFAULT_STATE_FILE

    if "collections" not in data:
        data["collections"] = []

    return data


def read_pbs_home():
    """
    Determine PBS_HOME from the environment or PBS configuration.
    """
    value = os.environ.get("PBS_HOME")
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
                if key.strip() == "PBS_HOME":
                    value = value.strip().strip('"').strip("'")
                    if value:
                        return value
    except OSError:
        pass

    return None


def resolve_state_path(config):
    """
    Resolve the configured state_file relative to PBS_HOME.

    Absolute paths and paths escaping PBS_HOME are rejected.
    """
    relpath = config.get("state_file", DEFAULT_STATE_FILE)

    if not isinstance(relpath, str) or not relpath.strip():
        raise RuntimeError("state_file must be a non-empty relative path")

    relpath = relpath.strip()

    if os.path.isabs(relpath):
        raise RuntimeError("state_file must be relative to PBS_HOME")

    pbs_home = read_pbs_home()
    if not pbs_home:
        raise RuntimeError("cannot determine PBS_HOME")

    pbs_home = os.path.realpath(pbs_home)
    path = os.path.realpath(os.path.join(pbs_home, relpath))

    if path != pbs_home and not path.startswith(pbs_home + os.sep):
        raise RuntimeError("state_file escapes PBS_HOME")

    return path


def plain_string(value):
    """
    Convert a PBS string-like object to a plain Python string.
    """
    text = str(value).strip()
    return text if text else None


def string_values(value):
    """
    Convert a PBS string or string_array value to plain Python strings.

    Return:
        list  - valid string/string_array value
        None  - unsupported resource type
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


def collect_resource(vnode_list, source):
    """
    Return a canonical sorted unique list for one source resource.

    If a defined source value has an unsupported type, return None and silently
    skip this collection.
    """
    values = set()

    for vnode in vnode_list.values():
        raw = get_resource(vnode.resources_available, source)
        parts = string_values(raw)

        if parts is None:
            return None

        for item in parts:
            text = plain_string(item)
            if text:
                values.add(text)

    return sorted(values)


def build_resources(vnode_list, collections):
    """
    Build the complete resource mapping from configuration.
    """
    result = {}

    if not isinstance(collections, list):
        return result

    for item in collections:
        if not isinstance(item, dict):
            continue

        source = item.get("source")

        if not isinstance(source, str):
            continue

        source = source.strip()
        if not source:
            continue

        values = collect_resource(vnode_list, source)

        # Unsupported source type: deliberately silent.
        if values is None:
            continue

        result[source] = values

    return result


def load_existing_state(path):
    """
    Load an existing generated state file.

    Invalid or unreadable files are treated as absent so they are replaced.
    """
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None

    return data if isinstance(data, dict) else None


def same_resources(old_state, resources):
    """
    Compare only aggregated resource content.

    Metadata such as generated timestamps must not force needless rewrites.
    """
    if not isinstance(old_state, dict):
        return False

    old_resources = old_state.get("resources")
    return old_resources == resources


def atomic_write_json(path, data):
    """
    Atomically replace the state file.

    The temporary file is created in the same directory so os.replace() stays
    on the same filesystem.
    """
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o750, exist_ok=True)

    fd, tmppath = tempfile.mkstemp(
        prefix=".aggregate_resources.",
        suffix=".tmp",
        dir=directory
    )

    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=4, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())

        os.chmod(tmppath, 0o640)
        os.replace(tmppath, path)

        # Persist the directory entry where supported.
        try:
            dirfd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        except OSError:
            pass

    except Exception:
        try:
            os.unlink(tmppath)
        except OSError:
            pass
        raise


def run(config):
    event = pbs.event()
    vnode_list = event.vnode_list

    resources = build_resources(
        vnode_list,
        config.get("collections", [])
    )

    path = resolve_state_path(config)
    old_state = load_existing_state(path)

    if same_resources(old_state, resources):
        return

    state = {
        "version": 1,
        "generated": int(time.time()),
        "resources": resources
    }

    atomic_write_json(path, state)

    log(
        pbs.EVENT_DEBUG,
        "updated aggregate resource state file %s" % path
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
