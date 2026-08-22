# `hook_job_gpus`

## Overview

`hook_job_gpus` turns a scheduled `ngpus` allocation into a concrete set of NVIDIA devices on each execution host. It tracks device ownership, attaches GPU device isolation to the cgroup created by `hook_job_cgroups_v2`, sets CUDA visibility for launched processes, and records lightweight GPU usage accounting.

The current implementation supports whole physical NVIDIA GPUs. MIG devices are not supported.

## User documentation

Users request GPUs through the standard `ngpus` resource, normally together with CPU and memory resources:

```bash
#PBS -l select=1:ncpus=8:ngpus=1:mem=32gb
```

The user does not choose physical GPU indices. On each execution host, the hook selects the required number of free GPUs and restricts the job to those devices.

At process launch the hook sets:

- `CUDA_VISIBLE_DEVICES` to the UUIDs of the GPUs assigned to the local job;
- `CUDA_DEVICE_ORDER=PCI_BUS_ID` when GPUs are assigned.

A job requesting no GPUs receives an empty CUDA visibility setting so that it does not accidentally use GPUs that were not allocated to it.

GPU usage is reported in PBS accounting while the job runs. In the current implementation:

- `resources_used.gpupercent` is a running arithmetic mean of the summed utilization percentages of all GPUs allocated on the local host; for multiple GPUs it can therefore exceed 100;
- `resources_used.gpumemmaxpercent` is the maximum observed aggregate fraction of allocated GPU framebuffer memory in use and is in the range 0-100.

GPU telemetry is intentionally lightweight and periodic, so these values are samples rather than a full high-frequency performance trace.

## Technical and administration documentation

### Hook events and ordering

The supplied `hook_job_gpus.qmgr` installs the hook for:

- `exechost_periodic`
- `execjob_begin`
- `execjob_launch`
- `execjob_epilogue`
- `execjob_end`
- `execjob_abort`
- `execjob_resize`

The periodic interval is 30 seconds and the hook order is 20. `hook_job_cgroups_v2` runs earlier at order 10 and must create the per-job cgroup before this hook attempts device isolation.

### Configuration

The supplied JSON configuration is based on:

```json
{
    "cgroup_root": "/sys/fs/cgroup/system.slice/pbs-mom.service",
    "jobs_subdir": "pbs_jobs",
    "state_subdir": "gpu_v2",
    "nvidia_smi": "/usr/bin/nvidia-smi",
    "device_isolation": true,
    "manage_drm_acl": true,
    "telemetry": true,
    "allocation": "index"
}
```

| Field | Description |
| --- | --- |
| `cgroup_root` | Same PBS Mom cgroup root used by `hook_job_cgroups_v2`. |
| `jobs_subdir` | Directory containing per-job cgroups. Must match the cgroup hook. |
| `state_subdir` | Mom-private directory used to persist GPU allocation/accounting state. |
| `nvidia_smi` | Absolute path to `nvidia-smi`. |
| `device_isolation` | Enable cgroup-v2 GPU device isolation. |
| `manage_drm_acl` | Manage access to related DRM device nodes when required. |
| `telemetry` | Enable periodic GPU utilization/memory sampling. |
| `allocation` | Physical-GPU selection policy. The implementation supports index-based allocation and topology-aware/NUMA selection where configured. |

### Device allocation

At `execjob_begin`, the hook sums the `ngpus` allocation for all chunks assigned to the local execution host. It inventories physical NVIDIA GPUs and selects the requested number while holding hook state/locking so that concurrent jobs do not receive the same device.

If a job requests GPUs but the required devices cannot be inventoried or allocated, execution is rejected rather than allowing the job to run without its requested devices.

The selected devices are persisted by GPU UUID, not merely by volatile CUDA index.

### Device isolation

When `device_isolation` is enabled, the hook attaches a cgroup v2 device filter to the existing per-job cgroup. The cgroup itself is owned by `hook_job_cgroups_v2`; this hook neither creates nor destroys the CPU/memory job cgroup.

DRM node access can be adjusted when `manage_drm_acl` is enabled. Device-isolation/allocation failures are treated as execution-critical. Telemetry failures are non-fatal and are logged without killing an otherwise valid job.

### Environment setup

At `execjob_launch`, the hook sets `CUDA_VISIBLE_DEVICES` from the UUIDs stored in local job state. This happens for each launched job process/session on the execution host.

### Telemetry and accounting

When telemetry is enabled, the periodic event samples allocated GPUs through `nvidia-smi` and updates PBS `resources_used` values.

The supplied `.qmgr` file defines the following accounting resources:

| Resource | Type | Flags | Current use |
| --- | --- | --- | --- |
| `gpupercent` | `long` | `r` | Populated by the hook as running mean aggregate GPU utilization. |
| `gpumemmaxpercent` | `long` | `r` | Populated as maximum observed aggregate GPU-memory percentage. |
| `gpupowerusageavg` | `long` | `r` | Defined in the supplied resource specification for GPU power accounting. |
| `gpuenergyconsumed` | `long` | `r` | Defined in the supplied resource specification for GPU energy accounting. |

The current Python implementation updates `gpupercent` and `gpumemmaxpercent`. It does not currently write `gpupowerusageavg` or `gpuenergyconsumed`; those two resource definitions are therefore reserved by the supplied configuration but are not populated by this hook version.

### Lifecycle

The periodic event also removes stale GPU state for jobs that no longer exist. Epilogue/end/abort events release GPU-related state and access changes. Dynamic GPU resizing is not supported and `execjob_resize` is rejected.

### Dependencies

This hook depends on:

- `ngpus`, discovered/published by `hook_discovery_gpus` and allocated by PBS;
- a working NVIDIA driver and configured `nvidia-smi`;
- the job cgroup created by `hook_job_cgroups_v2` when device isolation is enabled.

It does not use `gpu_cap`, `gpu_arch`, or `gpu_model` to choose the concrete physical device after scheduling; those resources constrain scheduling before execution.
