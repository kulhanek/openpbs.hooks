# `hook_discovery_gpus`

## Overview

`hook_discovery_gpus` discovers physical GPUs installed on PBS execution hosts and publishes GPU count, identity, memory, compute capability, architecture, and CUDA compatibility information as vnode resources.

The current implementation supports NVIDIA GPUs. The configuration is vendor-oriented so that support for additional GPU vendors can be added later without changing the public resource names.

This hook performs hardware discovery only. GPU allocation, device isolation, environment setup, and runtime accounting are handled by `hook_job_gpus`.

## User documentation

Users can request GPU resources in a normal PBS `select` specification. For example:

```bash
#PBS -l select=1:ncpus=8:ngpus=1
```

Additional discovered GPU properties can be used to constrain node selection, for example:

```bash
#PBS -l select=1:ncpus=8:ngpus=1:gpu_vendor=nvidia
```

```bash
#PBS -l select=1:ncpus=8:ngpus=1:gpu_cap=sm_89
```

```bash
#PBS -l select=1:ncpus=8:ngpus=1:gpu_arch=ada
```

`gpu_mem` represents the memory capacity of an individual physical GPU on the vnode. On a host with several GPUs, the hook publishes the minimum total framebuffer memory among the detected physical GPUs, so a node is not advertised with a per-GPU memory value that some of its GPUs cannot satisfy.

`gpu_cap`, `gpu_arch`, and `cuda_version` are string-array resources. The normal GPU capability request syntax may also be normalized by `hook_normalize_job_gpucap`; see that hook's documentation for `exact[...]`, `compat[...]`, and `compute_XX` forms.

## Technical and administration documentation

### Hook events

The supplied `hook_discovery_gpus.qmgr` installs the hook for:

- `exechost_startup`
- `exechost_periodic`

The default periodic interval is 1800 seconds and the hook order is 30.

### NVIDIA discovery

The NVIDIA backend uses the configured `nvidia-smi` executable to query physical GPU properties. The implementation publishes physical GPUs rather than MIG instances.

For an NVIDIA host the hook derives:

- physical GPU count;
- vendor name `nvidia`;
- GPU model;
- total framebuffer memory;
- CUDA compute capability in the form `sm_XX`, for example `sm_86`;
- architecture name obtained from the configured capability-to-architecture mapping;
- CUDA version reported by the NVIDIA driver/tooling.

The current design assumes that GPUs in one execution host are homogeneous enough for scalar resources such as `gpu_model`. The array-valued capability and architecture resources are retained deliberately for scheduler matching and future extensibility.

### Configuration

The hook uses the same JSON configuration file as `hook_normalize_job_gpucap`. Important top-level fields are:

| Field | Description |
| --- | --- |
| `use_compatible_gpu_cap` | Controls whether an unwrapped `gpu_cap` request is expanded by the normalization hook. It does not change hardware discovery itself. |
| `state_file` | Cluster-wide aggregate state file used by the normalization hook to filter generated compatibility alternatives. |
| `vendors` | Vendor-specific discovery configuration. |

The supplied NVIDIA section enables the NVIDIA backend and specifies:

```json
{
    "vendors": {
        "nvidia": {
            "enabled": true,
            "commands": {
                "nvidia_smi": "/usr/bin/nvidia-smi"
            },
            "architectures": {
                "sm_80": "ampere",
                "sm_86": "ampere",
                "sm_87": "ampere",
                "sm_89": "ada",
                "sm_90": "hopper"
            }
        }
    }
}
```

The actual supplied mapping contains the supported capabilities from older NVIDIA generations through current Blackwell entries. Capability keys are listed by GPU generation. In the normalization hook contained in this archive, compatibility is determined by equal architecture values; mapping order is not used as a minimum-version rule.

### PBS resources

The supplied `.qmgr` file defines:

| Resource | Type | Flags | Meaning |
| --- | --- | --- | --- |
| `ngpus` | `long` | `hn` | Number of physical GPUs on the vnode; consumable by jobs. |
| `gpu_mem` | `size` | `hl` | Minimum total framebuffer memory of one detected physical GPU. |
| `gpu_vendor` | `string` | `h` | GPU vendor, currently `nvidia`. |
| `gpu_model` | `string` | `h` | GPU model. |
| `gpu_cap` | `string_array` | `ho` | GPU compute capability, e.g. `sm_86`. |
| `gpu_arch` | `string_array` | `ho` | Architecture family, e.g. `ampere`, `ada`, or `hopper`. |
| `cuda_version` | `string_array` | `ho` | CUDA version reported for the host. |

### Interaction with other hooks

`hook_normalize_job_gpucap` consumes the architecture mapping in this hook's configuration and can consume the aggregate GPU capability state produced by `hook_aggregate_resources`.

`hook_job_gpus` consumes the scheduled `ngpus` allocation at execution time, selects concrete GPU devices, isolates them through the job cgroup, sets CUDA environment variables, and publishes GPU usage accounting.

### Administration notes

If NVIDIA discovery is disabled or `nvidia-smi` is unavailable, no NVIDIA resources should be advertised for that host. Administrators should keep the architecture mapping consistent with the capabilities present in the cluster and with the desired compatibility semantics of `hook_normalize_job_gpucap`.
