import json
import os
import re
import subprocess

import pbs


HOOK_NAME = "hook_discovery_containers"
RESOURCE_NAME = "containers"
PROBE_TIMEOUT = 5
RUNTIME_NAME_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")


def log(level, message):
    pbs.logmsg(level, "%s: %s" % (HOOK_NAME, message))


def load_config():
    path = os.environ.get("PBS_HOOK_CONFIG_FILE")
    if not path:
        raise ValueError("PBS_HOOK_CONFIG_FILE is not set")

    with open(path, "r") as handle:
        config = json.load(handle)

    runtimes = config.get("runtimes")
    if not isinstance(runtimes, dict) or not runtimes:
        raise ValueError("configuration item 'runtimes' must be a non-empty object")

    validated = {}

    for runtime_name, runtime_cfg in runtimes.items():
        if not isinstance(runtime_name, str) or not runtime_name:
            raise ValueError("runtime names must be non-empty strings")
        if not RUNTIME_NAME_RE.match(runtime_name):
            raise ValueError(
                "runtime name '%s' contains unsupported characters" % runtime_name
            )
        if not isinstance(runtime_cfg, dict):
            raise ValueError("runtime '%s' configuration must be an object" % runtime_name)

        if "enabled" not in runtime_cfg or not isinstance(runtime_cfg["enabled"], bool):
            raise ValueError("runtime '%s': 'enabled' must be a boolean" % runtime_name)

        commands = runtime_cfg.get("commands")
        if not isinstance(commands, list) or not commands:
            raise ValueError(
                "runtime '%s': 'commands' must be a non-empty list" % runtime_name
            )

        for command in commands:
            if not isinstance(command, str) or not command:
                raise ValueError(
                    "runtime '%s': every command must be a non-empty string"
                    % runtime_name
                )
            if not os.path.isabs(command):
                raise ValueError(
                    "runtime '%s': command '%s' is not an absolute path"
                    % (runtime_name, command)
                )

        probe = runtime_cfg.get("probe", [])
        if not isinstance(probe, list) or not all(isinstance(arg, str) for arg in probe):
            raise ValueError(
                "runtime '%s': 'probe' must be a list of strings" % runtime_name
            )

        validated[runtime_name] = {
            "enabled": runtime_cfg["enabled"],
            "commands": commands,
            "probe": probe,
        }

    return validated


def executable_exists(path):
    return os.path.isfile(path) and os.access(path, os.X_OK)


def probe_command(command, args):
    argv = [command] + args
    process = None

    try:
        with open(os.devnull, "wb") as devnull:
            process = subprocess.Popen(
                argv,
                stdin=devnull,
                stdout=devnull,
                stderr=devnull,
                close_fds=True,
            )
            return_code = process.wait(timeout=PROBE_TIMEOUT)
        return return_code == 0
    except subprocess.TimeoutExpired:
        if process is not None:
            try:
                process.kill()
                process.wait()
            except Exception:
                pass
        log(
            pbs.EVENT_DEBUG,
            "probe timed out for command '%s'" % command,
        )
    except Exception as exc:
        log(
            pbs.EVENT_DEBUG,
            "probe failed for command '%s': %s" % (command, exc),
        )

    return False


def detect_runtime(runtime_name, runtime_cfg):
    if not runtime_cfg["enabled"]:
        log(pbs.EVENT_DEBUG, "runtime '%s' is disabled" % runtime_name)
        return False

    for command in runtime_cfg["commands"]:
        if not executable_exists(command):
            continue

        if probe_command(command, runtime_cfg["probe"]):
            log(
                pbs.EVENT_DEBUG,
                "detected runtime '%s' using '%s'" % (runtime_name, command),
            )
            return True

        log(
            pbs.EVENT_DEBUG,
            "runtime '%s': probe failed for '%s'" % (runtime_name, command),
        )

    return False


def publish(vnode_list, local_node, containers):
    value = ",".join(containers) if containers else None
    vnode_list[local_node].resources_available[RESOURCE_NAME] = value

    if containers:
        log(
            pbs.EVENT_DEBUG,
            "resources_available.%s=%s" % (RESOURCE_NAME, value),
        )
    else:
        log(
            pbs.EVENT_DEBUG,
            "resources_available.%s cleared; no enabled runtime detected"
            % RESOURCE_NAME,
        )


def main():
    event = pbs.event()

    if event.type not in (pbs.EXECHOST_STARTUP, pbs.EXECHOST_PERIODIC):
        log(pbs.EVENT_DEBUG, "unsupported hook event")
        event.accept()
        return

    vnode_list = event.vnode_list
    local_node = pbs.get_local_nodename()

    if vnode_list is None or local_node is None or local_node not in vnode_list:
        log(pbs.EVENT_ERROR, "cannot obtain local vnode")
        event.accept()
        return

    try:
        runtimes = load_config()
    except Exception as exc:
        log(pbs.EVENT_ERROR, "configuration error: %s" % exc)
        event.accept()
        return

    containers = []
    for runtime_name, runtime_cfg in runtimes.items():
        try:
            if detect_runtime(runtime_name, runtime_cfg):
                containers.append(runtime_name)
        except Exception as exc:
            log(
                pbs.EVENT_DEBUG,
                "runtime '%s': detection failed: %s" % (runtime_name, exc),
            )

    publish(vnode_list, local_node, containers)
    event.accept()


try:
    main()
except SystemExit:
    pass
except Exception as exc:
    try:
        pbs.logmsg(pbs.EVENT_ERROR, "%s: unhandled error: %s" % (HOOK_NAME, exc))
        pbs.event().accept()
    except Exception:
        pass
