# coding: utf-8
"""
OpenPBS queue-time GPU capability normalization hook.

Events
------
    queuejob, modifyjob

Purpose
-------
Normalize gpu_cap values inside every Resource_List.select chunk.

Supported user syntax
---------------------
    TOKEN
    exact[TOKEN]
    compat[TOKEN]

NVIDIA convenience syntax:
    compute_XX -> sm_XX

For a plain TOKEN, compatibility expansion is controlled by
"use_compatible_gpu_cap".  exact[TOKEN] never expands.  compat[TOKEN] always
attempts expansion.

Compatibility is derived from the ordered, vendor-local "architectures" maps.
For sm_XX, compatibility is forward-only within the same architecture.  For
compute_XX, compatibility is forward-only within the same vendor regardless
of architecture.

If "state_file" is configured and readable, only hook-added compatibility
alternatives are filtered against resources.gpu_cap from that file.  A value
explicitly requested by the user (after canonicalization such as
compute_86 -> sm_86) is never removed by state-file filtering.

The final gpu_cap list is de-duplicated while preserving compatibility order.

Before modifying Resource_List.select, the hook stores the current select in
Resource_List.user_select only when user_select is None or empty.  This
allows the first normalization hook in a pipeline to own the backup.

The JSON configuration is intentionally shared with hook_discovery_gpus.
"""

import json
import os
import re
import traceback

import pbs


HOOK_NAME = "pbs_normalize_job_gpucap"

DEFAULT_CONFIG = {
    "use_compatible_gpu_cap": False,
    "vendors": {}
}

WRAPPER_RE = re.compile(r"^(exact|compat)\[(.*)\]$")
COMPUTE_RE = re.compile(r"^compute_([0-9]+)$")


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


def canonical_token(token):
    """Canonicalize vendor-specific aliases while keeping generic tokens opaque."""
    match = COMPUTE_RE.match(token)
    if match:
        return "sm_" + match.group(1)
    return token


def token_compatibility_kind(token):
    """Return the compatibility semantics implied by the user's spelling."""
    if COMPUTE_RE.match(token):
        return "compute"
    return "sm"


def parse_user_token(token):
    """
    Return (mode, canonical_value, compatibility_kind).

    mode is one of:
        default - plain token
        exact
        compat

    compatibility_kind preserves whether the user wrote compute_XX or sm_XX,
    because their compat[] expansion rules differ.

    exact[] and compat[] require one non-empty, comma-free inner token.
    Unknown capability names are valid and are left untouched.
    """
    token = str(token).strip()
    if not token:
        raise ValueError("gpu_cap contains an empty token")

    match = WRAPPER_RE.match(token)
    if match:
        mode = match.group(1)
        value = match.group(2).strip()
        if not value:
            raise ValueError("%s[] requires a non-empty capability" % mode)
        if "," in value:
            raise ValueError("%s[] capability must not contain ','" % mode)
        return mode, canonical_token(value), token_compatibility_kind(value)

    if token.startswith("exact[") or token.startswith("compat["):
        raise ValueError("malformed gpu_cap wrapper: %s" % token)

    return "default", canonical_token(token), token_compatibility_kind(token)


def architecture_maps(cfg):
    """
    Return [(vendor_name, architectures_dict), ...].

    Compatibility namespaces remain vendor-local.  The "enabled" flag controls
    discovery and does not alter the syntax understood by this normalizer.
    """
    result = []
    vendors = cfg.get("vendors", {})
    if not isinstance(vendors, dict):
        return result

    for vendor_name in sorted(vendors):
        vendor_cfg = vendors.get(vendor_name)
        if not isinstance(vendor_cfg, dict):
            continue
        architectures = vendor_cfg.get("architectures", {})
        if isinstance(architectures, dict):
            result.append((str(vendor_name), architectures))
    return result


def compatible_tokens(cfg, token, compatibility_kind):
    """
    Return forward-compatible configured values for token.

    The order of entries in vendors.*.architectures is significant and is
    assumed to run from oldest to newest capability.

    sm_XX:
        start at sm_XX and include only later capabilities with the same
        architecture in the same vendor.

    compute_XX:
        start at sm_XX and include every later capability in the same vendor,
        regardless of architecture.

    If token is absent from the configured maps, no alternatives are added.
    If it occurs in multiple vendor namespaces, expansion is ambiguous and is
    skipped.
    """
    matches = []
    for vendor_name, architectures in architecture_maps(cfg):
        keys = [str(value).strip() for value in architectures.keys()]
        if token in keys:
            index = keys.index(token)
            matches.append((vendor_name, architectures, keys, index))

    if not matches:
        return []

    if len(matches) > 1:
        log(pbs.EVENT_WARNING,
            "gpu_cap %s occurs in multiple vendor architecture maps; "
            "compatibility expansion skipped" % token)
        return []

    vendor_name, architectures, keys, index = matches[0]
    architecture = architectures.get(token)
    values = []

    for capability in keys[index:]:
        if compatibility_kind == "sm" and architectures.get(capability) != architecture:
            continue
        if capability:
            values.append(capability)

    log(pbs.EVENT_DEBUG3,
        "compatibility lookup %s/%s/%s -> %s" %
        (vendor_name, compatibility_kind, token, ",".join(values)))
    return values


def load_cluster_gpu_caps(cfg):
    """
    Return:
        None  -> state filtering is disabled/unavailable
        set() -> valid state file, but no gpu_cap values are present
        set(values) -> current aggregated gpu_cap inventory

    Any missing/unreadable/malformed state file disables filtering for this
    invocation.  The state file is an optimization, not a correctness
    requirement.
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


def normalize_gpu_cap(value, cfg, cluster_caps=None):
    """
    Normalize one complete gpu_cap resource value.

    User/canonical values and hook-added alternatives are tracked separately so
    state-file filtering can never remove a value requested by the user.
    Output order follows the user request and the configured capability order.
    """
    raw_tokens = str(value).split(",")
    result = []
    seen = set()

    expand_plain = bool(cfg.get("use_compatible_gpu_cap", False))

    for raw in raw_tokens:
        mode, token, compatibility_kind = parse_user_token(raw)

        if token not in seen:
            result.append(token)
            seen.add(token)

        expand = (mode == "compat") or (
            mode == "default" and expand_plain
        )

        if not expand:
            continue

        for alternative in compatible_tokens(
                cfg, token, compatibility_kind):
            alternative = str(alternative).strip()
            if not alternative or alternative == token:
                continue
            if cluster_caps is not None and alternative not in cluster_caps:
                continue
            if alternative not in seen:
                result.append(alternative)
                seen.add(alternative)

    return ",".join(result)


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
    """
    Backup select only if the shared pipeline backup does not already exist.
    """
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

    # The backup is taken immediately before this hook first changes select.
    # If an earlier normalization hook already populated select_backup, it is
    # preserved unchanged.
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
