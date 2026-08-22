# `hook_normalize_job_gpucap`

## Overview

`hook_normalize_job_gpucap` normalizes GPU compute-capability expressions in a submitted job's `Resource_List.select`. It converts CUDA `compute_XX` notation to the cluster's canonical `sm_XX` notation and can expand a requested capability into compatible alternatives according to the architecture mapping shared with `hook_discovery_gpus`.

The hook operates at job submission time. It does not discover GPUs or allocate devices.

## User documentation

The hook accepts several forms for the `gpu_cap` value in a `select` chunk.

### Canonical capability

A normal NVIDIA SM capability can be requested directly:

```bash
#PBS -l select=1:ncpus=8:ngpus=1:gpu_cap=sm_86
```

Whether an unwrapped value is expanded to compatible capabilities is controlled by the site configuration option `use_compatible_gpu_cap`.

### CUDA compute notation

CUDA-style compute notation is converted automatically:

```bash
#PBS -l select=1:ncpus=8:ngpus=1:gpu_cap=compute_86
```

is normalized to the canonical `sm_86` form before scheduling.

### Exact request

To prohibit compatibility expansion, use:

```bash
#PBS -l select=1:ncpus=8:ngpus=1:gpu_cap=exact[sm_86]
```

or equivalently:

```bash
#PBS -l select=1:ncpus=8:ngpus=1:gpu_cap=exact[compute_86]
```

The wrapper is removed and only the canonical requested capability remains.

### Compatible request

To explicitly request compatibility expansion, use:

```bash
#PBS -l select=1:ncpus=8:ngpus=1:gpu_cap=compat[sm_86]
```

Compatibility expansion is **forward-only**, and the order of capabilities in the vendor `architectures` map is significant: it is assumed to be sorted from oldest to newest.

For `compat[sm_XX]`, `sm_XX` is the minimum capability and expansion remains within the same GPU architecture. For example, with the supplied NVIDIA map:

```text
compat[sm_80] -> sm_80,sm_86,sm_87
compat[sm_86] -> sm_86,sm_87
```

The older `sm_80` capability is therefore not included for `compat[sm_86]`.

For `compat[compute_XX]`, `compute_XX` is canonicalized to `sm_XX`, but its compatibility semantics are different: `sm_XX` is the minimum and **all later capabilities from the same vendor are accepted regardless of architecture**. With the supplied NVIDIA map:

```text
compat[compute_86] -> sm_86,sm_87,sm_89,sm_90,sm_100,sm_103,sm_110,sm_120,sm_121
```

When cluster-state filtering is enabled, hook-generated alternatives not currently present in the cluster are removed from these lists.

The exact alternatives can be filtered against the capabilities currently known to exist in the cluster. The capability explicitly requested by the user is never removed merely because the aggregate state file is missing or stale.

Malformed `exact[...]` or `compat[...]` expressions are rejected at submission.

## Technical and administration documentation

### Hook event

The supplied `hook_normalize_job_gpucap.qmgr` installs the hook for `queuejob` with order 10.

The Python implementation contains logic that can recognize a modify-job event, but the supplied administrative configuration enables only `queuejob`; this documentation therefore describes the deployed behavior.

### Normalization algorithm

The hook parses `Resource_List.select` into chunks and only changes `gpu_cap` values. Other chunk resources and chunk multipliers are preserved.

Normalization performs the following operations:

1. Parse optional `exact[...]` or `compat[...]` wrapper.
2. Canonicalize `compute_XX` to `sm_XX`.
3. Decide whether compatibility expansion is enabled:
   - `exact[...]`: never expand;
   - `compat[...]`: always request expansion;
   - unwrapped value: follow `use_compatible_gpu_cap`.
4. Generate forward-compatible capabilities from the configured vendor architecture list, using different rules for `sm_XX` and `compute_XX`.
5. When aggregate cluster state is available, remove hook-generated alternatives not present in `resources.gpu_cap`.
6. De-duplicate and normalize the final list placed into the select expression.

### Compatibility rules

Compatibility is derived from the ordered capability-to-architecture mapping in `hook_discovery_gpus.json`. The insertion order of `vendors.*.architectures` is semantically significant and must be from oldest to newest capability.

For `compat[sm_XX]`:

- locate `sm_XX` in a vendor architecture map;
- use its position as the minimum accepted capability;
- determine the architecture assigned to `sm_XX`;
- walk forward from that position and include only capabilities belonging to the same architecture;
- never include an older capability, even if it belongs to the same architecture;
- if `sm_XX` is absent from the mapping, add no alternatives and retain the explicitly requested `sm_XX`.

Thus, for the supplied Ampere entries:

```text
compat[sm_80] -> sm_80,sm_86,sm_87
compat[sm_86] -> sm_86,sm_87
compat[sm_87] -> sm_87
```

For `compat[compute_XX]`:

- preserve the fact that the user wrote `compute_XX` before canonicalizing it to `sm_XX`;
- locate the corresponding `sm_XX` in the vendor map;
- use that position as the minimum accepted capability;
- walk forward and include every later capability from that vendor, regardless of architecture.

For example:

```text
compat[compute_86] -> sm_86,sm_87,sm_89,sm_90,sm_100,sm_103,sm_110,sm_120,sm_121
```

If the same canonical capability occurs in more than one vendor namespace, the lookup is ambiguous and compatibility expansion is skipped for that token.

### Shared configuration

The `.qmgr` file imports `hook_discovery_gpus.json` as this hook's configuration. Important fields are:

| Field | Description |
| --- | --- |
| `use_compatible_gpu_cap` | Default expansion policy for unwrapped `gpu_cap` requests. |
| `state_file` | JSON cluster-resource aggregate produced by `hook_aggregate_resources`. |
| `vendors.*.architectures` | Ordered capability-to-architecture map used for compatibility expansion. Entries must be ordered from oldest to newest capability. |

The supplied configuration currently has `use_compatible_gpu_cap: false`, so a plain `gpu_cap=sm_XX` remains exact unless the user explicitly uses `compat[...]`.

### Aggregate-state filtering

When `state_file` can be read, the hook uses `resources.gpu_cap` from that file to filter only the alternatives it generated. This prevents normalization from expanding a request to GPU capabilities that are known not to exist anywhere in the cluster.

If the state file is absent or malformed, compatibility normalization still works from the static configuration, but cluster-presence filtering is skipped.

### Preserving the submitted select

Before changing `Resource_List.select`, the hook stores the original value in `Resource_List.user_select` if that field is currently unset or empty. This preserves the user's initial request for inspection/accounting through the normalization pipeline.

`user_select` is defined by `hook_normalize_job_mpiomp.qmgr` as a string resource. It is metadata and must not be added to scheduler `resources` configuration.

### PBS resources

This hook's own `.qmgr` file creates no resources. It reads/modifies:

| Resource | Purpose |
| --- | --- |
| `Resource_List.select` | Input and normalized output. |
| `gpu_cap` inside select chunks | Capability expression being normalized. |
| `Resource_List.user_select` | Backup of the original select, defined by the MPI/OpenMP normalization setup. |

The scheduler-visible `gpu_cap` resource itself is defined by `hook_discovery_gpus.qmgr` as `string_array` with flags `ho`.
