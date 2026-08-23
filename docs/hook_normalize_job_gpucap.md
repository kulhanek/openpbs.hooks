# `hook_normalize_job_gpucap`

## Overview

`hook_normalize_job_gpucap` normalizes `gpu_cap` values in every chunk of a submitted job's `Resource_List.select`.

A `gpu_cap` value is either one token or a comma-separated list of tokens. Ordinary user-provided tokens are kept unchanged. NVIDIA CUDA `compute_XX` tokens are converted to `sm_XX` and expanded with newer NVIDIA SM capabilities from the ordered configuration shared with `hook_discovery_gpus`.

The hook runs at job submission time. It does not discover GPUs or allocate GPU devices.

## User documentation

A `gpu_cap` request can contain one token:

```bash
#PBS -l select=1:ncpus=8:ngpus=1:gpu_cap=sm_90
```

or several comma-separated tokens:

```bash
#PBS -l select=1:ncpus=8:ngpus=1:gpu_cap=sm_90,gfx942
```

Tokens may contain letters, digits, `_`, `.`, and `-`, allowing values such as `sm_90`, `gfx942`, and `gfx11-generic`.

All ordinary tokens are kept as specified by the user. Only `compute_XX` has special meaning. For example:

```text
compute_90
```

is converted to:

```text
sm_90
```

and the hook also adds every later NVIDIA capability from the configured NVIDIA capability list. With the supplied configuration:

```text
compute_90 -> sm_90,sm_100,sm_103,sm_110,sm_120,sm_121
```

More than one `compute_XX` token can be used in the same `gpu_cap` value. Their generated compatibility alternatives are combined with all other user-provided tokens. Duplicates are removed and the final value is sorted.

The same normalization is performed independently for every `select` chunk containing `gpu_cap`.

If aggregate GPU inventory filtering is configured and available, generated compatibility alternatives that do not currently exist in the aggregate `gpu_cap` inventory are removed. Explicit user-provided values, including the `sm_XX` produced directly from `compute_XX`, are not removed by this filtering.

## Technical and administration documentation

### Hook event

The supplied `hook_normalize_job_gpucap.qmgr` installs the hook for the `queuejob` event with order 20. The Python implementation also recognizes `modifyjob`, although the supplied `.qmgr` configuration enables only `queuejob`.

### Accepted `gpu_cap` syntax

A complete `gpu_cap` value is parsed as a comma-separated list of tokens. Every token must match:

```text
[A-Za-z0-9][A-Za-z0-9_.-]*
```

The following are therefore valid examples:

```text
sm_90
gfx942
gfx11-generic
sm_90,gfx942
compute_90,sm_89,gfx942
```

Empty entries, brackets, wrappers, and other expression syntax are rejected.

### Normalization algorithm

For each `select` chunk containing `gpu_cap`, the hook:

1. splits `gpu_cap` on commas and validates every token;
2. keeps every ordinary user-provided token unchanged;
3. converts every `compute_XX` token to `sm_XX`;
4. locates that `sm_XX` in `vendors.nvidia.architectures`;
5. adds all NVIDIA capability keys appearing *after* that entry, continuing to the end of the configured list without restricting expansion to the same architecture family;
6. optionally filters only these generated alternatives against the aggregate `resources.gpu_cap` inventory;
7. combines user values and generated values, removes duplicates, sorts the result, and writes it back to the chunk.

For example, with the supplied NVIDIA map:

```text
compute_90 -> sm_90,sm_100,sm_103,sm_110,sm_120,sm_121
```

The `sm_90` value is the canonicalized user request. The generated compatibility list starts with the next configured entry, `sm_100`, because `sm_90` is already present in the result.

If the canonical `sm_XX` value is not present in `vendors.nvidia.architectures`, `compute_XX` is still converted to `sm_XX`, but no compatibility alternatives are added.

The insertion order of `vendors.nvidia.architectures` is therefore semantically significant and must be from oldest to newest NVIDIA capability.

### Multiple `compute_XX` values

Each `compute_XX` token is processed independently before the results are merged. For example, a request such as:

```text
compute_90,compute_120,gfx942
```

uses both NVIDIA lookup points, keeps `gfx942` unchanged, merges all generated values, and removes duplicates before sorting.

### Aggregate-state filtering

The configuration is shared with `hook_discovery_gpus`. If `state_file` is present in the hook configuration and the referenced file exists, the hook reads `resources.gpu_cap` from the aggregate inventory.

Only compatibility alternatives generated from the NVIDIA map are filtered. Values explicitly provided by the user are retained, and `compute_XX -> sm_XX` is treated as the canonical form of the user's own value and is therefore also retained.

If `state_file` is not configured, does not exist, or cannot be parsed, compatibility expansion continues from the static NVIDIA configuration without aggregate-inventory filtering.

Relative `state_file` paths are resolved below `PBS_HOME`; if `PBS_HOME` is not available in the environment, it is read from `PBS_CONF_FILE`, falling back to `/var/spool/pbs`.

### Preserving the submitted select

Before changing `Resource_List.select`, the hook stores its current value in `Resource_List.user_select` only when `user_select` is unset or empty. If an earlier normalization hook has already populated `user_select`, that value is preserved.

This keeps the existing shared backup behavior used by the select-normalization hook pipeline.

### PBS resources

This hook creates no PBS resources and the supplied `.qmgr` resource definitions do not need to change.

| Resource | Purpose |
| --- | --- |
| `Resource_List.select` | Input select specification and normalized output. |
| `gpu_cap` inside select chunks | Token or comma-separated token list normalized by this hook. |
| `Resource_List.user_select` | Optional backup of the pre-normalized select, defined elsewhere in the normalization setup. |

The scheduler-visible `gpu_cap` resource remains the `string_array` resource defined by `hook_discovery_gpus.qmgr`.
