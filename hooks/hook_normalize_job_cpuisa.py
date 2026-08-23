# coding: utf-8
"""
OpenPBS queue-time CPU ISA normalization hook.

The hook normalizes cpu_isa values in every Resource_List.select chunk.

Accepted cpu_isa syntax
-----------------------
    TOKEN
    TOKEN,TOKEN,...
    compat[TOKEN]

Ordinary user tokens are preserved.  compat[TOKEN] contributes TOKEN itself
plus compatible alternatives from the shared configuration:

    cpu_isa.<cpu_arch>.<TOKEN> = [compatible ISA tokens ...]

Only hook-generated compatibility alternatives are optionally filtered against
resources.cpu_isa in the aggregate state file.  Explicit user tokens are never
removed by inventory filtering.  The final value is sorted and de-duplicated.

The configuration is intentionally shared with hook_discovery_cpus.
"""

import json
import os
import re
import traceback

import pbs


HOOK_NAME = "pbs_normalize_job_cpuisa"

DEFAULT_CONFIG = {
    "cpu_isa": {},
    "backup_user_select": True,
}

TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
COMPAT_RE = re.compile(r"^compat\[([A-Za-z0-9][A-Za-z0-9_.-]*)\]$")


def log(level, msg):
    pbs.logmsg(level, HOOK_NAME + ": " + str(msg))


def deep_update(dst, src):
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            deep_update(dst[key], value)
        else:
            dst[key] = value
    return dst


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    path = os.environ.get("PBS_HOOK_CONFIG_FILE")
    if path and os.path.isfile(path):
        with open(path, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise RuntimeError("hook configuration must contain a JSON object")
        deep_update(cfg, data)
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


def pbs_home():
    value = os.environ.get("PBS_HOME")
    if value:
        return value
    value = read_pbs_conf().get("PBS_HOME")
    if value:
        return value
    return "/var/spool/pbs"


def resolve_state_file(path):
    if os.path.isabs(path):
        return path
    return os.path.join(pbs_home(), path)


def load_cluster_cpu_isas(cfg):
    """
    Return the aggregate resources.cpu_isa inventory as a set.

    None means filtering is disabled/unavailable.  A missing, unreadable, or
    malformed optional state file therefore never disables compatibility
    expansion; it only disables inventory filtering.
    """
    state_file = str(cfg.get("state_file", "") or "").strip()
    if not state_file:
        return None

    path = resolve_state_file(state_file)
    if not os.path.isfile(path):
        log(pbs.EVENT_DEBUG,
            "state_file %s does not exist; cpu_isa filtering skipped" % path)
        return None

    try:
        with open(path, "r") as f:
            state = json.load(f)
        values = state.get("resources", {}).get("cpu_isa")
        if not isinstance(values, list):
            raise ValueError("resources.cpu_isa is not a list")
        return set(str(value).strip() for value in values if str(value).strip())
    except Exception as exc:
        log(pbs.EVENT_WARNING,
            "cannot use state_file %s; cpu_isa filtering skipped: %s" %
            (path, exc))
        return None


def parse_cpu_isa_tokens(value):
    """Return [(mode, token), ...] for one complete cpu_isa value."""
    text = str(value)
    if not text.strip():
        raise ValueError("cpu_isa is empty")

    result = []
    for raw in text.split(","):
        item = raw.strip()
        if not item:
            raise ValueError("cpu_isa contains an empty token")

        match = COMPAT_RE.match(item)
        if match:
            result.append(("compat", match.group(1)))
            continue

        if TOKEN_RE.match(item):
            result.append(("plain", item))
            continue

        raise ValueError("invalid cpu_isa token %r" % item)

    return result


def explicit_chunk_arch(chunk):
    """
    Return an explicitly requested single cpu_arch token, if available.

    cpu_arch itself is not normalized here.  A comma-separated cpu_arch request
    does not uniquely identify one compatibility namespace, so it is ignored
    for lookup and the cpu_isa token is used to infer the namespace instead.
    """
    for field in str(chunk).split(":"):
        if "=" not in field:
            continue
        name, value = field.split("=", 1)
        if name.strip() != "cpu_arch":
            continue
        values = [part.strip() for part in value.split(",") if part.strip()]
        if len(values) == 1:
            return values[0]
    return None


def isa_map_for_token(cfg, token, requested_arch=None):
    """Return the unique compatibility map applicable to token, or None."""
    all_maps = cfg.get("cpu_isa", {})
    if not isinstance(all_maps, dict):
        raise ValueError("configuration item cpu_isa must be an object")

    if requested_arch:
        arch_map = all_maps.get(requested_arch)
        if isinstance(arch_map, dict) and token in arch_map:
            return arch_map
        # An explicit architecture with an unknown token is not an error: the
        # user's token remains exact and simply receives no alternatives.
        return None

    matches = []
    for arch, arch_map in all_maps.items():
        if isinstance(arch_map, dict) and token in arch_map:
            matches.append((arch, arch_map))

    if not matches:
        return None
    if len(matches) > 1:
        raise ValueError(
            "cpu_isa token %r is present in multiple architecture maps (%s); "
            "specify cpu_arch in the same select chunk" %
            (token, ",".join(sorted(str(item[0]) for item in matches)))
        )
    return matches[0][1]


def compatible_tokens(cfg, token, requested_arch=None):
    arch_map = isa_map_for_token(cfg, token, requested_arch)
    if arch_map is None:
        return []

    values = arch_map.get(token, [])
    if not isinstance(values, list):
        raise ValueError(
            "cpu_isa compatibility entry for %r must be a list" % token
        )

    result = []
    for value in values:
        value = str(value).strip()
        if not value:
            continue
        if not TOKEN_RE.match(value):
            raise ValueError(
                "invalid configured cpu_isa compatibility token %r" % value
            )
        result.append(value)
    return result


def normalize_cpu_isa(value, cfg, cluster_isas=None, requested_arch=None):
    """Normalize one complete cpu_isa resource value."""
    user_values = set()
    generated_values = set()

    for mode, token in parse_cpu_isa_tokens(value):
        # compat[XX] always contributes XX itself.  This is an explicit user
        # requirement and is deliberately not filtered by cluster inventory.
        user_values.add(token)

        if mode != "compat":
            continue

        for alternative in compatible_tokens(cfg, token, requested_arch):
            if alternative != token:
                generated_values.add(alternative)

    if cluster_isas is not None:
        generated_values.intersection_update(cluster_isas)

    return ",".join(sorted(user_values | generated_values))


def normalize_chunk(chunk, cfg, cluster_isas):
    fields = str(chunk).split(":")
    requested_arch = explicit_chunk_arch(chunk)
    changed = False

    for index, field in enumerate(fields):
        if "=" not in field:
            continue
        name, value = field.split("=", 1)
        if name.strip() != "cpu_isa":
            continue

        normalized = normalize_cpu_isa(
            value, cfg, cluster_isas, requested_arch=requested_arch
        )
        new_field = name + "=" + normalized
        if new_field != field:
            fields[index] = new_field
            changed = True

    return ":".join(fields), changed


def normalize_select(select_value, cfg):
    cluster_isas = load_cluster_cpu_isas(cfg)
    chunks = str(select_value).split("+")
    result = []
    changed = False

    for chunk in chunks:
        normalized, chunk_changed = normalize_chunk(chunk, cfg, cluster_isas)
        result.append(normalized)
        changed = changed or chunk_changed

    return "+".join(result), changed


def get_resource(job, name):
    try:
        return job.Resource_List[name]
    except Exception:
        return None


def backup_select(job, select_text, cfg):
    if not bool(cfg.get("backup_user_select", True)):
        return

    backup = get_resource(job, "user_select")
    if backup is None or str(backup).strip() == "":
        job.Resource_List["user_select"] = str(select_text)
        log(pbs.EVENT_DEBUG,
            "saved Resource_List.user_select=%s" % select_text)


def normalize_job(event, cfg):
    job = event.job
    select_value = get_resource(job, "select")
    if select_value is None or not str(select_value).strip():
        return

    select_text = str(select_value)
    normalized, changed = normalize_select(select_text, cfg)
    if not changed:
        return

    backup_select(job, select_text, cfg)
    job.Resource_List["select"] = pbs.select(normalized)
    log(pbs.EVENT_DEBUG,
        "normalized select: %s -> %s" % (select_text, normalized))


def main():
    event = pbs.event()

    if event.type not in (pbs.QUEUEJOB, pbs.MODIFYJOB):
        event.accept()
        return

    cfg = load_config()
    try:
        normalize_job(event, cfg)
    except ValueError as exc:
        event.reject("invalid cpu_isa specification: %s" % exc)
        return

    event.accept()


try:
    main()
except SystemExit:
    raise
except Exception as exc:
    log(pbs.EVENT_ERROR, "%s\n%s" % (exc, traceback.format_exc()))
    try:
        pbs.event().reject("cpu_isa normalization failed: %s" % exc)
    except Exception:
        pass
