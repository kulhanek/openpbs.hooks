#!/usr/bin/env python3

import json
import os
import tempfile
import time

import pbs


HOOK_NAME = "aggregate_resources"
STATE_VERSION = 1


def log(level, message):
    pbs.logmsg(level, "%s: %s" % (HOOK_NAME, message))


def load_config():
    path = pbs.hook_config_filename
    if not path:
        raise RuntimeError("hook configuration file is not available")

    with open(path, "r", encoding="utf-8") as fh:
        config = json.load(fh)

    state_file = config.get("state_file")
    sources = config.get("sources")

    if not isinstance(state_file, str) or not state_file.strip():
        raise ValueError("'state_file' must be a non-empty string")

    if not isinstance(sources, list) or not sources:
        raise ValueError("'sources' must be a non-empty list")

    normalized_sources = []
    seen = set()

    for source in sources:
        if not isinstance(source, str) or not source.strip():
            raise ValueError("each item in 'sources' must be a non-empty string")

        source = source.strip()
        if source not in seen:
            seen.add(source)
            normalized_sources.append(source)

    return {
        "state_file": state_file.strip(),
        "sources": normalized_sources,
    }


def resolve_state_file(path):
    if os.path.isabs(path):
        return os.path.normpath(path)

    pbs_home = pbs.pbs_conf.get("PBS_HOME")
    if not pbs_home:
        raise RuntimeError("PBS_HOME is not available in pbs.pbs_conf")

    return os.path.normpath(os.path.join(pbs_home, path))


def split_value(value):
    """
    Treat every PBS resource value as text and split it only on commas.

    This is intentional even for resources declared as string_array:
    str(value) is used first, then the resulting text is split into
    individual items.
    """
    if value is None:
        return []

    items = []
    for item in str(value).split(","):
        item = item.strip()
        if item:
            items.append(item)

    return items


def collect_resources(sources):
    aggregated = {source: set() for source in sources}
    server = pbs.server()

    for vnode in server.vnodes():
        for source in sources:
            try:
                value = vnode.resources_available[source]
            except (KeyError, AttributeError):
                continue

            if value is None:
                continue

            aggregated[source].update(split_value(value))

    return {
        source: sorted(aggregated[source])
        for source in sorted(aggregated)
    }


def write_state_file(path, data):
    directory = os.path.dirname(path)
    if not directory:
        raise RuntimeError("state file has no parent directory: %s" % path)

    os.makedirs(directory, mode=0o755, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        prefix=".aggregate_resources.",
        suffix=".tmp",
        dir=directory,
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=4, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())

        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)

    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def main():
    config = load_config()
    state_file = resolve_state_file(config["state_file"])
    resources = collect_resources(config["sources"])

    data = {
        "generated": int(time.time()),
        "resources": resources,
        "version": STATE_VERSION,
    }

    write_state_file(state_file, data)

    for source in sorted(resources):
        log(
            pbs.EVENT_SYSTEM,
            "aggregated %s: %s"
            % (source, ", ".join(resources[source]) if resources[source] else "<empty>"),
        )

    log(
        pbs.EVENT_SYSTEM,
        "aggregated resource data written to %s" % state_file,
    )


try:
    main()
except Exception as exc:
    log(pbs.EVENT_ERROR, "failed: %s" % exc)
    raise
