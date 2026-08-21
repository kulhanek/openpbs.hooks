# `hook_discovery_gpus`

## 1. Overview

`hook_discovery_gpus` discovers physical GPUs and publishes GPU capacity and descriptive properties on the local PBS vnode. It is intended for `exechost_startup` and `exechost_periodic`.

The hook is structured around vendor-specific configuration under the `vendors` section of the JSON file. Currently only NVIDIA GPUs are supported. NVIDIA discovery uses `nvidia-smi`; support for additional vendors such as AMD can be added later without changing the published resource model.

The hook has no DCGM or AMS dependency. NVIDIA MIG instances are deliberately not treated as independently schedulable GPUs; `ngpus` counts physical GPUs.

## 2. User documentation

### Published resources

| Resource | Type | Meaning | Example |
|---|---|---|---|
| `ngpus` | `long` | Number of physical GPUs detected on the vnode. | `ngpus=4` |
| `gpu_vendor` | `string` | GPU vendor. Currently `nvidia`. | `gpu_vendor=nvidia` |
| `gpu_model` | `string_array` | Unique GPU model name(s) detected on the vnode. | `gpu_model=NVIDIA GeForce RTX 4090` |
| `gpu_cap` | `string_array` | Native vendor-specific GPU capability. NVIDIA compute capability is normalized to `sm_XX`, e.g. `8.9` becomes `sm_89`. | `gpu_cap=sm_89` |
| `gpu_arch` | `string_array` | GPU architecture derived from `gpu_cap` through the vendor-specific JSON mapping. | `gpu_arch=ada` |
| `gpu_mem` | `size` | Total framebuffer memory available on one physical GPU, published in PBS `kb`. The minimum value across detected GPUs is used. | `gpu_mem=49140mb` |
| `cuda_version` | `string` | Maximum CUDA version reported by the installed NVIDIA driver. | `cuda_version=13.0` |

`gpu_cap` and `gpu_arch` are deliberately defined as `string_array`. Current deployment assumes GPU-homogeneous hosts, so each normally contains exactly one value. Keeping them as arrays leaves room for future scheduling semantics without changing the resource types.

For NVIDIA the relationship is intentionally simple:

```text
gpu_model -> detected directly
gpu_cap   -> normalized compute capability, e.g. sm_89
gpu_arch  -> configured mapping from gpu_cap, e.g. ada
```

No model-name pattern matching is used to derive `gpu_arch`.

### Example requests

One GPU with eight CPU cores:

```bash
#PBS -l select=1:ncpus=8:ngpus=1
```

A capability-specific request:

```bash
#PBS -l select=1:ncpus=8:ngpus=1:gpu_cap=sm_90
```

An architecture-specific request:

```bash
#PBS -l select=1:ncpus=8:ngpus=1:gpu_arch=hopper
```

A model-specific request can also be used if desired by site policy:

```bash
#PBS -l select=1:ncpus=8:ngpus=1:gpu_model=<configured-model-value>
```

### Restrictions

- Currently only NVIDIA GPUs are supported.
- MIG instances are ignored as scheduling units; `ngpus` is a physical-GPU count.
- This hook only discovers and publishes GPU resources. Per-job allocation, isolation, environment setup, and accounting are handled by `hook_job_gpus`.
- If the configured NVIDIA discovery executable is absent, the hook publishes `ngpus=0` and clears all descriptive GPU properties.
- On older NVIDIA drivers where the `compute_cap` query field is unsupported, model/count/memory discovery continues through a fallback query, but `gpu_cap` and therefore `gpu_arch` remain unset.
- `gpu_mem` describes memory of one physical GPU, not aggregate framebuffer memory across all GPUs on the vnode. The minimum detected value is published.
- GPU-homogeneous hosts are expected. The implementation still de-duplicates values before publishing string-array resources.

## 3. Technical documentation

### Vendor-specific JSON configuration

The configuration is grouped by GPU vendor:

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
                "sm_89": "ada",
                "sm_90": "hopper"
            }
        }
    }
}
```

The supplied configuration contains a broader NVIDIA capability-to-architecture table.

| Item | Type | Description |
|---|---|---|
| `vendors.nvidia.enabled` | boolean | Enables NVIDIA GPU discovery. |
| `vendors.nvidia.commands.nvidia_smi` | absolute path | Path to `nvidia-smi`. The hook requires an absolute path. |
| `vendors.nvidia.architectures` | object/map | Maps normalized NVIDIA `gpu_cap` values such as `sm_89` to architecture names such as `ada`. |

The mapping is deliberately configuration data rather than hard-coded Python logic. A future AMD implementation can use its own vendor section and native capability identifiers, for example `gfx942`, while publishing through the same `gpu_cap` and `gpu_arch` resources.

### NVIDIA discovery

The hook first executes:

```text
nvidia-smi --query-gpu=index,name,compute_cap,memory.total --format=csv,noheader,nounits
```

For every successfully parsed physical GPU row it collects:

- model name,
- compute capability,
- framebuffer memory.

NVIDIA compute capability is converted to the native capability string used by PBS:

```text
7.5  -> sm_75
8.0  -> sm_80
8.9  -> sm_89
9.0  -> sm_90
10.0 -> sm_100
12.1 -> sm_121
```

Each resulting `gpu_cap` is then looked up directly in `vendors.nvidia.architectures`. For example:

```text
sm_80 -> ampere
sm_86 -> ampere
sm_89 -> ada
sm_90 -> hopper
```

If a detected `gpu_cap` has no mapping, the hook still publishes the capability, leaves the corresponding `gpu_arch` unset, and writes a warning to the PBS log. Discovery is therefore not broken by a newly introduced GPU capability whose architecture mapping has not yet been added to the JSON configuration.

If querying `compute_cap` is unsupported, the hook falls back to:

```text
nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader,nounits
```

This preserves `ngpus`, `gpu_vendor`, `gpu_model`, and `gpu_mem` discovery, while `gpu_cap` and `gpu_arch` remain unset.

`memory.total` is reported by `nvidia-smi` in MiB with `nounits`; the hook converts it to KiB by multiplying by 1024 and publishes it with the PBS `kb` suffix.

The hook separately executes ordinary `nvidia-smi` output and extracts the `CUDA Version:` field for `cuda_version`.

### Publishing and stale values

The discovered values are published only to local vnodes. Empty descriptive values are assigned as `None`, which clears stale resource values if GPUs disappear, a vendor is disabled, or a property can no longer be detected.

Published descriptive resources are:

```text
gpu_vendor
gpu_model
gpu_cap
gpu_arch
gpu_mem
cuda_version
```

### Limitations and failure behaviour

- The current Python implementation contains an NVIDIA discovery backend only.
- The CSV parser assumes the fields produced by `nvidia-smi` follow the expected query format.
- CUDA compatibility is represented by the driver-reported `CUDA Version`, not by discovery of an installed CUDA Toolkit.
- If the configured `nvidia-smi` executable exists but both GPU queries fail, the hook raises an error and rejects the event.
- If no supported vendor is enabled, `ngpus=0` is published and descriptive GPU resources are cleared.
- Local-vnode identification is based on PBS/local host names; absence of a matching local vnode causes event rejection.

## 4. qmgr setup

The supplied qmgr setup defines:

```text
ngpus        : long
gpu_vendor   : string
gpu_model    : string_array
gpu_cap      : string_array
gpu_arch     : string_array
gpu_mem      : size
cuda_version : string
```

It also creates and configures the `discovery_gpus` hook for `exechost_startup` and `exechost_periodic`, then imports the Python hook and JSON configuration.
