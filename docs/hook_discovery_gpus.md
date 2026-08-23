# `hook_discovery_gpus`

## Overview

`hook_discovery_gpus` discovers physical GPUs installed on PBS execution hosts and publishes GPU count, vendor, model, memory, GPU capability/target, architecture, and NVIDIA CUDA compatibility information as vnode resources.

The hook supports both NVIDIA and AMD GPUs. NVIDIA hardware is discovered with `nvidia-smi`; AMD hardware is discovered with `amd-smi`. A vnode is expected to have a homogeneous GPU configuration: all installed GPU cards should be of the same vendor and model.

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
#PBS -l select=1:ncpus=8:ngpus=1:gpu_vendor=amd
```

For NVIDIA GPUs, `gpu_cap` contains the CUDA compute capability in `sm_XX` form, for example `sm_89`. `gpu_arch` contains the corresponding configured NVIDIA architecture family, for example `ada`.

For AMD GPUs, `gpu_cap` contains the native AMD GPU target, for example `gfx942`. If a portable target is configured for the native target, `gpu_cap` also contains that portable target, for example `gfx9-4-generic`. `gpu_arch` contains the configured AMD architecture family, for example `cdna3`.

Examples of capability and architecture constraints are:

```bash
# NVIDIA
#PBS -l select=1:ncpus=8:ngpus=1:gpu_cap=sm_89
#PBS -l select=1:ncpus=8:ngpus=1:gpu_arch=ada
```

```bash
# AMD native or portable target
#PBS -l select=1:ncpus=8:ngpus=1:gpu_cap=gfx942
#PBS -l select=1:ncpus=8:ngpus=1:gpu_cap=gfx9-4-generic
#PBS -l select=1:ncpus=8:ngpus=1:gpu_arch=cdna3
```

`gpu_mem` represents the memory capacity of an individual physical GPU on the vnode. On a host with several GPUs, the hook publishes the minimum total framebuffer memory among the detected physical GPUs, so a node is not advertised with a per-GPU memory value that some of its GPUs cannot satisfy.

`cuda_version` is populated only for NVIDIA GPUs. It is cleared for AMD GPUs.

`gpu_cap`, `gpu_arch`, and `cuda_version` are string-array resources. The normal GPU capability request syntax may also be normalized by `hook_normalize_job_gpucap`; see that hook's documentation for `compute_XX` forms.

## Technical and administration documentation

### Hook events

The supplied `hook_discovery_gpus.qmgr` installs the hook for:

- `exechost_startup`
- `exechost_periodic`

The default periodic interval is 1800 seconds and the hook order is 30.

### Discovery model

The hook evaluates enabled vendor backends and uses the first backend that discovers one or more GPUs. This permits the same hook and configuration to be deployed on a cluster containing NVIDIA nodes and AMD nodes.

The implementation assumes a homogeneous GPU setup within each vnode. `gpu_model` is therefore a scalar resource. If several different model strings are unexpectedly detected, the hook logs a warning and publishes the lexicographically first value. `gpu_mem` is always the minimum discovered per-GPU memory value.

### NVIDIA discovery

The NVIDIA backend uses the configured `nvidia-smi` executable to query physical GPU properties. MIG instances are not enumerated separately; `ngpus` counts physical GPUs.

For an NVIDIA host the hook derives:

- physical GPU count;
- vendor name `nvidia`;
- GPU model;
- total framebuffer memory;
- CUDA compute capability converted to `sm_XX`, for example compute capability `8.9` becomes `sm_89`;
- architecture family obtained from `vendors.nvidia.architectures`;
- CUDA version reported by the NVIDIA driver/tooling.

Example NVIDIA configuration:

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

If a discovered NVIDIA `sm_XX` value has no architecture mapping, the native `gpu_cap` is still published, while no `gpu_arch` value is generated for that capability and a warning is logged.

### AMD discovery

The AMD backend uses the configured `amd-smi` executable. It runs:

```text
amd-smi static --asic --vram
```

For each GPU, the hook reads the AMD SMI static fields:

- `MARKET_NAME` as `gpu_model`;
- `TARGET_GRAPHICS_VERSION` as the native AMD GPU target, for example `gfx942`;
- VRAM `SIZE` as the per-GPU memory capacity.

For an AMD host the hook publishes:

- physical GPU count;
- vendor name `amd`;
- GPU model;
- total framebuffer memory;
- native AMD target in `gpu_cap`;
- optionally, a configured portable target in `gpu_cap`;
- architecture family obtained from `vendors.amd.architectures`;
- no `cuda_version` value.

The native AMD target always remains in `gpu_cap`. A portable target is added only when a mapping exists in `vendors.amd.portable_targets`. For example:

```json
{
    "vendors": {
        "amd": {
            "enabled": true,
            "commands": {
                "amd_smi": "/usr/bin/amd-smi"
            },
            "architectures": {
                "gfx90a": "cdna2",
                "gfx942": "cdna3",
                "gfx950": "cdna4",
                "gfx1100": "rdna3"
            },
            "portable_targets": {
                "gfx942": "gfx9-4-generic",
                "gfx950": "gfx9-4-generic",
                "gfx1100": "gfx11-generic"
            }
        }
    }
}
```

With the example mapping, a `gfx942` GPU publishes:

```text
gpu_vendor = amd
gpu_cap    = gfx942,gfx9-4-generic
gpu_arch   = cdna3
```

If the native AMD target has no portable-target mapping, only the native target is published. If it has no architecture mapping, the native target is still published, `gpu_arch` is left unset for that target, and a warning is logged.

### Configuration

The hook configuration is vendor-oriented. Relevant fields are:

| Field | Description |
| --- | --- |
| `state_file` | Cluster-wide aggregate state file shared with GPU capability normalization/aggregation logic. It is not used directly by hardware discovery. |
| `vendors.nvidia.enabled` | Enables NVIDIA discovery. |
| `vendors.nvidia.commands.nvidia_smi` | Absolute path to `nvidia-smi`. |
| `vendors.nvidia.architectures` | Map from native NVIDIA `sm_XX` capability to architecture family. |
| `vendors.amd.enabled` | Enables AMD discovery. |
| `vendors.amd.commands.amd_smi` | Absolute path to `amd-smi`. |
| `vendors.amd.architectures` | Map from native AMD `gfx...` target to architecture family. |
| `vendors.amd.portable_targets` | Optional map from native AMD target to a portable AMD target added to `gpu_cap`. |

Both command paths must be absolute. An enabled vendor whose executable is absent is skipped. If a vendor tool exists but its discovery command fails, the hook logs a warning and tries the next enabled vendor.

### PBS resources

The resource types and flags are unchanged. The supplied `.qmgr` file defines:

| Resource | Type | Flags | Meaning |
| --- | --- | --- | --- |
| `ngpus` | `long` | `hn` | Number of physical GPUs on the vnode; consumable by jobs. |
| `gpu_mem` | `size` | `hl` | Minimum total framebuffer memory of one detected physical GPU. |
| `gpu_vendor` | `string` | `h` | GPU vendor: `nvidia` or `amd`. |
| `gpu_model` | `string` | `h` | GPU model. |
| `gpu_cap` | `string_array` | `ho` | NVIDIA `sm_XX`; AMD native `gfx...` target and optional portable target. |
| `gpu_arch` | `string_array` | `ho` | Configured GPU architecture family. |
| `cuda_version` | `string_array` | `ho` | CUDA version reported for NVIDIA hosts; unset on AMD hosts. |

### Interaction with other hooks

`hook_normalize_job_gpucap` can consume the capability/architecture mapping from this configuration and the aggregate GPU capability state produced by `hook_aggregate_resources`.

`hook_job_gpus` consumes the scheduled `ngpus` allocation at execution time, selects concrete GPU devices, isolates them through the job cgroup, sets vendor-specific GPU environment variables, and publishes GPU usage accounting as implemented by that hook.

### Administration notes

Administrators should keep architecture and portable-target mappings consistent with the GPU targets actually present in the cluster. The mappings are configuration data rather than hard-coded policy, so new GPU targets can be introduced without changing the discovery code.

On AMD nodes, `amd-smi` must provide the `TARGET_GRAPHICS_VERSION` field. If AMD GPUs are detected but this field is unavailable, the hook still publishes GPU count/model/memory where available and logs a warning, but cannot populate `gpu_cap` or `gpu_arch` for those devices.
