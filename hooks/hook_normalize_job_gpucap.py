# coding: utf-8
"""
OpenPBS queue-time GPU capability normalization hook.

Events
------
    queuejob, modifyjob

Purpose
-------
Normalize gpu_cap values inside every Resource_List.select chunk.

Accepted gpu_cap syntax
-----------------------
    TOKEN
    TOKEN,TOKEN,...

A token may contain letters, digits, underscore, dot, and hyphen.  Empty
entries, wrappers, brackets, or other expression syntax are rejected.

NVIDIA convenience syntax
-------------------------
Each compute_XX token is canonicalized to sm_XX.  In addition, all NVIDIA
sm_YY entries following sm_XX in vendors.nvidia.architectures are added as
compatible alternatives.  The NVIDIA architecture map is therefore ordered
from oldest to newest capability.

If "state_file" is configured and the referenced aggregate inventory exists,
only hook-added compatibility alternatives present in resources.gpu_cap are
kept.  User-provided tokens (after compute_XX -> sm_XX canonicalization) are
never removed by state-file filtering.

All user values and generated alternatives are combined, sorted, and
de-duplicated.

Before modifying Resource_List.select, the hook stores the current select in
Resource_List.user_select only when user_select is None or empty.  This lets
the first normalization hook in a pipeline own the backup.

The JSON configuration is intentionally shared with hook_discovery_gpus.
"""

import json
import os
import re
import traceback

import pbs


HOOK_NAME = "pbs_normalize_job_gpucap"

DEFAULT_CONFIG = {
    "vendors": {}
}

# gpu_cap values published by the GPU discovery hook include forms such as
# sm_90, gfx942, and gfx11-generic.  Keep the accepted syntax deliberately
# simple while allowing all of those forms.
TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
COMPUTE_RE = re.compile(r"^compute_([0-9]+)$")
SM_RE = re.compile(r"^sm_([0-9]+)$")


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


def parse_gpu_cap_tokens(value):
    """Parse and validate one complete gpu_cap resource value."""
    text = str(value)
    raw_tokens = text.split(",")
    tokens = []

    for raw in raw_tokens:
        token = raw.strip()
        if not token:
            raise ValueError("gpu_cap contains an empty token")
        if not TOKEN_RE.match(token):
            raise ValueError("invalid gpu_cap token: %s" % token)
        tokens.append(token)

    return tokens


def nvidia_architectures(cfg):
    """Return the ordered NVIDIA capability map, or an empty map."""
    vendors = cfg.get("vendors", {})
    if not isinstance(vendors, dict):
        return {}

    nvidia = vendors.get("nvidia", {})
    if not isinstance(nvidia, dict):
        return {}

    architectures = nvidia.get("architectures", {})
    if not isinstance(architectures, dict):
        return {}

    return architectures


def compatible_compute_tokens(cfg, sm_token):
    """
    Return NVIDIA capabilities configured after sm_token.

    sm_token itself is not returned because compute_XX is already
    canonicalized to sm_XX as a user-provided value.  Compatibility continues
    through all later NVIDIA entries, regardless of architecture family.
    """
    architectures = nvidia_architectures(cfg)
    keys = [str(value).strip() for value in architectures.keys()]

    if sm_token not in keys:
        return []

    index = keys.index(sm_token)
    values = [value for value in keys[index + 1:] if value]

    log(pbs.EVENT_DEBUG3,
        "NVIDIA compatibility lookup %s -> %s" %
        (sm_token, ",".join(values)))
    return values


def load_cluster_gpu_caps(cfg):
    """
    Return the current aggregate gpu_cap inventory when available.

    None means filtering is disabled because state_file is not configured,
    does not exist, or cannot be used.  An empty set means a valid aggregate
    state file was read but it contains no gpu_cap values.
    """
    if "state_file" not in cfg:
        return None

    configured = cfg.get("state_file")
    if configured is None or not str(configured).strip():
        return None

    path = resolve_state_file(str(configured).strip())
    if not os.path.isfile(path):
        log(pbs.EVENT_DEBUG,
            "state_file does not exist; compatibility filtering skipped: %s" %
            path)
        return None

    try:
        with open(path, "r") as f:
            data = json.load(f)

        resources = data.get("resources", {})
        values = resources.get("gpu_cap", [])

        if values is None:
            values = []
        elif isinstance(values, str):
            values = values.split(",")
        elif not isinstance(values, (list, tuple, set)):
            raise ValueError("resources.gpu_cap is not a list or string")

        return set(
            str(value).strip()
            for value in values
            if str(value).strip()
        )
    except Exception as exc:
        log(pbs.EVENT_WARNING,
            "cannot use state_file %s; compatibility filtering skipped: %s" %
            (path, exc))
        return None


def token_sort_key(token):
    """
    Sort NVIDIA sm_NN tokens numerically; sort all other tokens naturally.

    This keeps sequences such as sm_90, sm_100, sm_103 in capability order
    instead of lexicographic order (which would put sm_100 before sm_90).
    """
    match = SM_RE.match(token)
    if match:
        return (0, int(match.group(1)), token)

    parts = re.split(r"([0-9]+)", token)
    natural = tuple(
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in parts if part != ""
    )
    return (1, natural, token)


def normalize_gpu_cap(value, cfg, cluster_caps=None):
    """
    Normalize one complete gpu_cap value.

    Every user token is retained exactly as written except compute_XX, which
    becomes sm_XX.  Each compute_XX additionally contributes all configured
    later NVIDIA capabilities.  Only those generated alternatives are subject
    to aggregate-state filtering.
    """
    result = set()

    for token in parse_gpu_cap_tokens(value):
        match = COMPUTE_RE.match(token)
        if not match:
            result.add(token)
            continue

        canonical = "sm_" + match.group(1)
        result.add(canonical)

        for alternative in compatible_compute_tokens(cfg, canonical):
            if cluster_caps is not None and alternative not in cluster_caps:
                continue
            result.add(alternative)

    return ",".join(sorted(result, key=token_sort_key))


def normalize_chunk(chunk, cfg, cluster_caps):
    fields = str(chunk).split(":")
    changed = False

    for i, field in enumerate(fields):
        if "=" not in field:
            continue

        name, value = field.split("=", 1)
        if name.strip() != "gpu_cap":
            continue

        normalized = normalize_gpu_cap(value, cfg, cluster_caps)
        new_field = name + "=" + normalized
        if new_field != field:
            fields[i] = new_field
            changed = True

    return ":".join(fields), changed


def normalize_select(select_value, cfg):
    cluster_caps = load_cluster_gpu_caps(cfg)
    chunks = str(select_value).split("+")
    out = []
    changed = False

    for chunk in chunks:
        normalized, chunk_changed = normalize_chunk(
            chunk, cfg, cluster_caps
        )
        out.append(normalized)
        changed = changed or chunk_changed

    return "+".join(out), changed


def get_resource(job, name):
    try:
        return job.Resource_List[name]
    except Exception:
        return None


def backup_select(job, select_text):
    """Backup select only if the shared pipeline backup is still empty."""
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

    # Save the original select immediately before this hook first changes it.
    # If an earlier normalization hook already populated user_select, preserve
    # that original value unchanged.
    backup_select(job, select_text)

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
        event.reject("invalid gpu_cap specification: %s" % exc)
        return

    event.accept()


try:
    main()
except SystemExit:
    raise
except Exception as exc:
    log(pbs.EVENT_ERROR, "%s\n%s" % (exc, traceback.format_exc()))
    try:
        pbs.event().reject("gpu_cap normalization failed: %s" % exc)
    except Exception:
        pass
