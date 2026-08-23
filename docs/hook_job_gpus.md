# `hook_job_gpus`

## Overview

`hook_job_gpus` turns a scheduled `ngpus` allocation into a concrete set of physical GPU devices on each execution host. It supports homogeneous NVIDIA or AMD GPU vnodes, tracks device ownership, attaches GPU device isolation to the cgroup created by `hook_job_cgroups_v2`, configures the vendor-specific runtime visibility environment, and records lightweight GPU utilization, memory, power, and energy accounting.

The GPU vendor is taken from the local vnode resource `resources_available.gpu_vendor`, which is expected to be published by `hook_discovery_gpus`. Supported values are `nvidia` and `amd`. The hook does not infer the vendor from installed management binaries.

The implementation supports whole physical GPUs. NVIDIA MIG and AMD GPU partitioning are intentionally not supported.

## User documentation

Users request GPUs through the standard `ngpus` resource, normally together with CPU and memory resources:

```bash
#PBS -l select=1:ncpus=8:ngpus=1:mem=32gb
```

Users do not choose physical GPU indices. On each execution host, the hook selects the required number of free GPUs and restricts the job to those devices.

The runtime visibility variables depend on the GPU vendor:

| GPU vendor | Environment configured by the hook |
| --- | --- |
| NVIDIA | `CUDA_VISIBLE_DEVICES` contains the UUIDs of the locally allocated GPUs. `CUDA_DEVICE_ORDER=PCI_BUS_ID` is also set when GPUs are allocated. |
| AMD | `HIP_VISIBLE_DEVICES` contains the HIP device indices of the locally allocated GPUs. |

For a job with zero locally allocated GPUs, the corresponding vendor visibility variable is set to an empty value so that applications do not accidentally use GPUs not allocated to the job.

GPU usage is reported in PBS accounting while the job runs:

| Resource | Meaning |
| --- | --- |
| `resources_used.gpupercent` | Running arithmetic mean of the sum of instantaneous GPU-utilization percentages across all GPUs allocated on the local host. For `N` GPUs the range is `0..100*N`. |
| `resources_used.gpumemmaxpercent` | Maximum observed aggregate fraction of allocated GPU memory in use: `100 * sum(memory.used) / sum(memory.total)`. Range `0..100`. |
| `resources_used.gpupowerusageavg` | Running arithmetic mean of the summed instantaneous power usage of all allocated GPUs, in watts. |
| `resources_used.gpuenergyconsumed` | Estimated energy consumed by the allocated GPUs, in watt-hours (Wh), obtained by integrating sampled GPU power over elapsed wall-clock time. |

GPU telemetry is intentionally lightweight and periodic. The values are therefore accounting estimates rather than a high-frequency performance or power trace. The supplied PBS resources use type `long`, so power and energy values are rounded to whole watts and whole watt-hours when written to `resources_used`.

## Technical and administration documentation

### Hook events and ordering

The hook uses:

- `exechost_periodic`
- `execjob_begin`
- `execjob_launch`
- `execjob_epilogue`
- `execjob_end`
- `execjob_abort`
- `execjob_resize`

The existing `.qmgr` setup does not need to change. `hook_job_cgroups_v2` must run before `hook_job_gpus` for `execjob_begin`, because the per-job cgroup must already exist before the GPU device BPF program is attached.

### Configuration

The JSON configuration is:

```json
{
    "cgroup_root": "/sys/fs/cgroup/system.slice/pbs-mom.service",
    "jobs_subdir": "pbs_jobs",
    "state_subdir": "gpu_v2",
    "device_isolation": true,
    "manage_drm_acl": true,
    "telemetry": true,
    "allocation": "index",
    "vendors": {
        "nvidia": {
            "smi": "/usr/bin/nvidia-smi"
        },
        "amd": {
            "smi": "/usr/bin/amd-smi"
        }
    }
}
```

| Field | Description |
| --- | --- |
| `cgroup_root` | Same PBS Mom cgroup root used by `hook_job_cgroups_v2`. |
| `jobs_subdir` | Directory containing per-job cgroups. Must match the cgroup hook. |
| `state_subdir` | Mom-private directory used to persist GPU allocation and accounting state. |
| `device_isolation` | Enable cgroup-v2 GPU device isolation. |
| `manage_drm_acl` | Manage access to associated DRM device nodes. |
| `telemetry` | Enable periodic GPU utilization, memory, power, and energy accounting. |
| `allocation` | Physical-GPU selection policy. Supported values are `index` and `numa`. |
| `vendors.nvidia.smi` | Absolute path to `nvidia-smi`. |
| `vendors.amd.smi` | Absolute path to `amd-smi`. |

### Vendor selection

The local GPU vendor is obtained from:

```text
resources_available.gpu_vendor
```

The value is expected to be `nvidia` or `amd`, as published by `hook_discovery_gpus`. A GPU job is rejected if `ngpus > 0` but the local vnode has no supported `gpu_vendor`, or if the corresponding vendor management tool is unavailable.

This design assumes that all GPUs installed on one vnode are from the same vendor. The hook does not support a mixed NVIDIA/AMD vnode.

### Common GPU representation

Both vendor backends normalize physical devices into a common internal representation containing:

- allocation index;
- runtime device index;
- persistent GPU UUID;
- PCI bus address;
- NUMA node;
- memory size;
- device nodes protected by the cgroup device filter;
- associated DRM device nodes.

The common allocation, state management, device isolation, ACL handling, telemetry aggregation, accounting, and cleanup code operates only on this normalized representation.

### Device allocation

At `execjob_begin`, the hook sums the `ngpus` allocation for all chunks assigned to the local execution host. It inventories physical GPUs using the backend selected by `gpu_vendor` and selects the requested number while holding the hook state lock so that concurrent jobs do not receive the same physical GPU.

The selected devices are persisted by UUID. If `allocation` is `index`, free GPUs are selected in index order. If it is `numa`, they are ordered by NUMA node and then index.

If the requested number of GPUs cannot be inventoried or allocated, execution is rejected rather than allowing the job to run without its requested devices.

### NVIDIA backend

The NVIDIA backend uses `nvidia-smi` for inventory and telemetry.

Inventory obtains:

```text
index, uuid, pci.bus_id, memory.total
```

The PCI address is used to determine NUMA locality and associated DRM devices. The per-GPU `/dev/nvidiaN` node and corresponding DRM nodes are included in the device-isolation set.

At launch, the backend sets:

```text
CUDA_VISIBLE_DEVICES=<allocated GPU UUIDs>
CUDA_DEVICE_ORDER=PCI_BUS_ID
```

Telemetry obtains one node-wide sample containing:

```text
uuid, utilization.gpu, memory.used, memory.total, power.draw
```

The output is normalized to the common telemetry representation before accounting is updated.

### AMD backend

The AMD backend uses `amd-smi`.

Inventory uses:

```text
amd-smi list -e --json
```

The enumeration output supplies GPU identity, PCI BDF, UUID, and HIP enumeration information. Associated `/dev/dri/card*` and `/dev/dri/renderD*` devices are resolved from the PCI address. VRAM size is read from the AMD DRM sysfs `mem_info_vram_total` attribute when available.

At launch, the backend sets:

```text
HIP_VISIBLE_DEVICES=<allocated HIP device indices>
```

`ROCR_VISIBLE_DEVICES` is intentionally not set by the hook. Applying both a lower-level ROCR visibility filter and a HIP ordinal filter can change the device ordinal space. The cgroup device policy provides the OS-level isolation boundary, while `HIP_VISIBLE_DEVICES` provides the expected HIP runtime view.

Telemetry uses:

```text
amd-smi metric -u -m -p --json
```

The backend extracts and normalizes GPU/GFX utilization, VRAM used and total, and power values into the same internal telemetry fields used by the NVIDIA backend:

```text
util
mem_used
mem_total
power_w
```

If AMD SMI does not provide a usable power value for a sample, utilization and memory accounting remain valid while power and energy accounting are skipped for that sample.

### Device isolation

When `device_isolation` is enabled, the hook attaches a cgroup v2 `BPF_CGROUP_DEVICE` program to the existing per-job cgroup. The BPF implementation is vendor-neutral. Each backend only determines which per-GPU device nodes belong to each physical GPU.

For NVIDIA, protected per-GPU nodes include `/dev/nvidiaN` and associated DRM nodes. Shared NVIDIA control nodes remain available as unrelated devices.

For AMD, the associated DRM card/render nodes are protected. `/dev/kfd` is a shared ROCm device and is not assigned to a single physical GPU; it remains available, while access to non-allocated per-GPU DRM nodes is denied.

The cgroup itself is owned by `hook_job_cgroups_v2`; `hook_job_gpus` neither creates nor destroys the CPU/memory job cgroup.

DRM node access can additionally be adjusted with POSIX ACLs when `manage_drm_acl` is enabled.

GPU allocation and device-isolation failures are execution-critical. Telemetry failures are non-fatal and are logged without terminating an otherwise valid job.

### Telemetry and accounting

Each vendor backend produces a node-wide normalized sample keyed by GPU UUID:

```text
UUID -> {
    util,
    mem_used,
    mem_total,
    power_w
}
```

All accounting calculations are common to both NVIDIA and AMD.

For every valid sample, `gpupercent` accumulates the sum of instantaneous utilization percentages over all GPUs allocated to the local job and reports the arithmetic mean of those sums.

`gpumemmaxpercent` is the maximum observed value of:

```text
100 * sum(memory.used) / sum(memory.total)
```

If power is available for every allocated GPU in the sample, the instantaneous values are summed. `gpupowerusageavg` is the arithmetic mean of those summed power samples.

`gpuenergyconsumed` is accumulated internally in Wh. The first valid power sample is treated as representative from job creation until that sample. Later intervals use trapezoidal integration:

```text
energy += 0.5 * (previous_power + current_power) * elapsed_seconds / 3600
```

The accumulated floating-point value is stored in hook state; the PBS `long` resource receives the rounded whole-Wh value.

The hook intentionally does not take a final epilogue telemetry sample because the GPU workload has normally exited by then, which would bias utilization and average power downward.

### Persistent state

Per-job state is stored below:

```text
<PBS_MOM_HOME>/mom_priv/hooks/<state_subdir>/
```

State includes:

- job ID and user;
- GPU vendor;
- normalized allocated GPU inventory;
- utilization sample sum/count;
- peak memory percentage;
- power sample sum/count;
- accumulated energy;
- previous power value and timestamp.

Persisting the vendor means later launch, periodic, and cleanup events do not need to rediscover which backend was used for the allocation.

### Lifecycle and cleanup

The periodic event samples telemetry and removes stale GPU allocation state for jobs that no longer exist. The BPF device program is bound to the job cgroup and disappears when the cgroup hook removes that cgroup.

Epilogue/end/abort events remove GPU-related DRM ACLs and allocation state. Dynamic GPU resizing is not supported and `execjob_resize` is rejected.

### Dependencies

The hook depends on:

- `ngpus`, allocated by PBS;
- `gpu_vendor`, published by `hook_discovery_gpus`;
- `nvidia-smi` on NVIDIA vnodes;
- `amd-smi` on AMD vnodes;
- the corresponding kernel GPU driver and device nodes;
- `setfacl` when DRM ACL management is enabled;
- the job cgroup created by `hook_job_cgroups_v2` when device isolation is enabled.

The hook does not use `gpu_cap`, `gpu_arch`, or `gpu_model` when choosing the concrete physical GPU after scheduling. Those resources constrain scheduling before execution.
