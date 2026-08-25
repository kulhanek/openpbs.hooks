# `hook_normalize_job_gpucap`

## Overview

`hook_normalize_job_gpucap` normalizes `gpu_cap` values in every chunk of a submitted job's `Resource_List.select`.

A `gpu_cap` value can contain one token or a comma-separated list of tokens. NVIDIA requests support three forms:

- `sm_XY` — accepted exactly as supplied, without validation against `vendors.nvidia.architectures`;
- `compat[sm_XY]` — normalized to `sm_XY` and optionally expanded with configured compatible `sm_YY` values from the same major compute capability;
- `compute_XY` — handled as before: normalized to `sm_XY` and expanded with all later configured NVIDIA SM capabilities.

AMD native and portable target tokens continue to be passed through unchanged.

The hook runs at job submission time. It does not discover GPUs or allocate GPU devices.

## User documentation

A `gpu_cap` request can contain one value:

```bash
#PBS -l select=1:ncpus=8:ngpus=1:gpu_cap=sm_89
```

or a comma-separated list:

```bash
#PBS -l select=1:ncpus=8:ngpus=1:gpu_cap=sm_89,gfx942
```

Ordinary tokens may contain letters, digits, `_`, `.`, and `-`. AMD values such as `gfx942` and `gfx11-generic` are therefore accepted unchanged.

### Exact NVIDIA SM request: `sm_XY`

A plain `sm_XY` token is kept exactly as provided by the user. It is not checked against the NVIDIA architecture list in the JSON configuration.

For example:

```text
sm_65 -> sm_65
sm_89 -> sm_89
sm_999 -> sm_999
```

Whether such a request can actually be satisfied is left to the scheduler and the `gpu_cap` values published by the execution hosts.

### Same-major NVIDIA binary compatibility: `compat[sm_XY]`

`compat[sm_XY]` expresses a minimum SM binary version within the same major compute capability.

The requested value itself is always retained. The hook then adds configured `sm_YY` values for which:

- the major compute capability is the same as for `sm_XY`; and
- the numeric SM revision is greater than the requested revision.

With a configuration containing:

```text
sm_80, sm_86, sm_87, sm_89, sm_90, sm_100, ...
```

examples are:

```text
compat[sm_80] -> sm_80,sm_86,sm_87,sm_89
compat[sm_86] -> sm_86,sm_87,sm_89
compat[sm_89] -> sm_89
compat[sm_90] -> sm_90
```

The expansion does not cross a major compute-capability boundary. For example, `compat[sm_89]` does not add `sm_90`, and `compat[sm_90]` does not add `sm_100`.

The requested `sm_XY` does not need to exist in `vendors.nvidia.architectures`; it is still kept. Any additional compatible alternatives can only come from configured SM entries.

### NVIDIA virtual architecture request: `compute_XY`

`compute_XY` keeps the existing behavior. It is converted to `sm_XY` and expanded with every later NVIDIA SM capability in the ordered `vendors.nvidia.architectures` map, without restricting expansion to the same major revision.

For example, with the supplied configuration:

```text
compute_90 -> sm_90,sm_100,sm_103,sm_110,sm_120,sm_121
```

If the canonical `sm_XY` is not present in the configured NVIDIA map, `compute_XY` is still converted to `sm_XY`, but no compatibility alternatives are added.

### AMD tokens

AMD tokens retain the current behavior and are treated as ordinary values. Examples include:

```text
gfx942
gfx9-4-generic
gfx11-generic
```

They are neither interpreted nor expanded by this hook.

### Multiple values and filtering

All values generated from all tokens are merged, duplicates are removed, and the final list is sorted. The same normalization is performed independently for every `select` chunk containing `gpu_cap`.

If aggregate GPU inventory filtering is configured and available, only hook-generated compatibility alternatives are filtered against `resources.gpu_cap` in the aggregate state file. Explicit user values are never removed. This includes:

- plain `sm_XY`;
- the canonical `sm_XY` produced from `compat[sm_XY]`;
- the canonical `sm_XY` produced from `compute_XY`;
- AMD tokens.

## Technical and administration documentation

### Hook event

The supplied `hook_normalize_job_gpucap.qmgr` installs the hook for the `queuejob` event with order 20. The Python implementation also recognizes `modifyjob`, although the supplied `.qmgr` configuration enables only `queuejob`.

### Accepted `gpu_cap` syntax

A complete `gpu_cap` value is parsed as a comma-separated list. Each item must be either:

```text
[A-Za-z0-9][A-Za-z0-9_.-]*
```

or the special NVIDIA wrapper:

```text
compat[sm_[0-9]+]
```

Valid examples include:

```text
sm_89
sm_65
compute_90
compat[sm_86]
gfx942
gfx11-generic
sm_89,compat[sm_86],gfx942
```

Empty entries and unsupported bracket/wrapper expressions are rejected.

### Normalization algorithm

For every `select` chunk containing `gpu_cap`, the hook processes each token independently:

1. A plain token is retained unchanged. This includes all plain `sm_XY` and AMD tokens.
2. `compat[sm_XY]` is converted to canonical `sm_XY`.
3. For `compat[sm_XY]`, configured NVIDIA SM keys are inspected and only values with the same major compute capability and a greater numeric SM revision are added.
4. `compute_XY` is converted to `sm_XY`.
5. For `compute_XY`, all configured NVIDIA SM keys appearing after that canonical value are added, as in the previous implementation.
6. Generated alternatives are optionally filtered against the aggregate GPU capability inventory.
7. All values are merged, de-duplicated, sorted, and written back into the chunk.

The `compat[sm_XY]` implementation derives the major compute capability from the numeric SM token. For example:

```text
sm_86 -> major 8
sm_90 -> major 9
sm_100 -> major 10
sm_120 -> major 12
```

Consequently, architectural marketing families such as Ampere, Ada, Hopper, or Blackwell are not used when determining `compat[sm_XY]` compatibility.

### NVIDIA configuration semantics

`vendors.nvidia.architectures` still serves two purposes:

- `hook_discovery_gpus` maps a discovered native `sm_XY` to a human-readable `gpu_arch` value;
- `hook_normalize_job_gpucap` uses the configured SM keys as the source of generated compatibility alternatives.

Plain user-provided `sm_XY` values are deliberately not validated against this map.

For `compute_XY`, insertion order remains semantically significant because all entries after the requested canonical SM are considered compatible alternatives. The map must therefore remain ordered from oldest to newest capability.

For `compat[sm_XY]`, compatibility is determined numerically by major compute capability and requested-or-newer SM revision, so architecture-family labels are irrelevant.

### Aggregate-state filtering

If `state_file` is configured and the file exists, the hook reads `resources.gpu_cap` from the aggregate inventory.

Only generated alternatives are filtered. Canonical/user values are retained even if they are not present in the aggregate state. If `state_file` is missing, unavailable, or invalid, normalization continues without inventory filtering.

Relative `state_file` paths are resolved below `PBS_HOME`; if `PBS_HOME` is not available in the environment, it is read from `PBS_CONF_FILE`, falling back to `/var/spool/pbs`.

### Preserving the submitted select

Before changing `Resource_List.select`, the hook stores its current value in `Resource_List.user_select` only when `user_select` is unset or empty. If an earlier normalization hook has already populated `user_select`, that value is preserved.

### PBS resources

This hook creates no PBS resources and does not require changes to the existing resource definitions.

| Resource | Purpose |
| --- | --- |
| `Resource_List.select` | Input select specification and normalized output. |
| `gpu_cap` inside select chunks | Token or comma-separated token list normalized by this hook. |
| `Resource_List.user_select` | Optional backup of the pre-normalized select, defined elsewhere in the normalization setup. |

The scheduler-visible `gpu_cap` resource remains the `string_array` resource defined by `hook_discovery_gpus.qmgr`.
