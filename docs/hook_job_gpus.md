# `hook_job_gpus`

## Overview

`hook_job_gpus` turns a scheduled `ngpus` allocation into a concrete set of NVIDIA devices on each execution host. It tracks device ownership, attaches GPU device isolation to the cgroup created by `hook_job_cgroups_v2`, sets CUDA visibility for launched processes, and records lightweight GPU usage, memory, power, and energy accounting.

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

GPU usage is reported in PBS accounting while the job runs:

| Resource | Meaning |
| --- | --- |
| `resources_used.gpupercent` | Running arithmetic mean of the sum of instantaneous GPU-utilization percentages across all GPUs allocated on the local host. For `N` GPUs the range is `0..100*N`. |
| `resources_used.gpumemmaxpercent` | Maximum observed aggregate fraction of allocated GPU framebuffer memory in use: `100 * sum(memory.used) / sum(memory.total)`. Range `0..100`. |
| `resources_used.gpupowerusageavg` | Running arithmetic mean of the summed instantaneous `power.draw` of all allocated GPUs, in watts. |
| `resources_used.gpuenergyconsumed` | Estimated energy consumed by the allocated GPUs, in watt-hours (Wh), obtained by integrating sampled GPU power over elapsed wall-clock time. |

GPU telemetry is intentionally lightweight and periodic. The values are therefore accounting estimates based on `nvidia-smi` samples rather than a high-frequency performance or power trace. Because the supplied resource definitions use PBS type `long`, power and energy values are rounded to whole watts and whole watt-hours when written to `resources_used`.

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

The supplied JSON configuration is:

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
| `telemetry` | Enable periodic GPU utilization, memory, power, and energy accounting. |
| `allocation` | Physical-GPU selection policy. The implementation supports index-based allocation and NUMA-based ordering. |

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

When telemetry is enabled, each periodic event obtains one node-wide `nvidia-smi` sample containing:

```text
uuid, utilization.gpu, memory.used, memory.total, power.draw
```

For each live GPU job, only rows matching its allocated UUIDs are used. If any allocated GPU is missing from the sample, that sample is discarded for the job.

The supplied `.qmgr` file defines:

| Resource | Type | Flags | Populated value |
| --- | --- | --- | --- |
| `gpupercent` | `long` | `r` | Running mean of summed GPU utilization percentages. |
| `gpumemmaxpercent` | `long` | `r` | Maximum observed aggregate GPU-memory percentage. |
| `gpupowerusageavg` | `long` | `r` | Running mean of summed GPU power draw, in W. |
| `gpuenergyconsumed` | `long` | `r` | Integrated GPU energy consumption, in Wh. |

#### GPU utilization

For every valid sample, the hook sums `utilization.gpu` over all GPUs allocated to the local job. `gpupercent` is the arithmetic mean of these per-sample sums.

#### GPU memory

The hook computes:

```text
100 * sum(memory.used) / sum(memory.total)
```

for the allocated GPUs and retains the maximum observed value as `gpumemmaxpercent`.

#### GPU power

If `power.draw` is available for every allocated GPU, their instantaneous power values are summed. `gpupowerusageavg` is the arithmetic mean of these summed power samples. Therefore it represents average total GPU power for the local allocation, not average power per GPU.

If `power.draw` is unsupported or reported as `N/A`, utilization and memory accounting remain valid; only power and energy accounting are skipped for that sample.

#### GPU energy

`gpuenergyconsumed` is accumulated internally in Wh from the summed power samples. The first valid power sample is treated as representative from job creation until that sample. Subsequent intervals use trapezoidal integration between the previous and current total-power samples:

```text
energy += 0.5 * (previous_power + current_power) * elapsed_seconds / 3600
```

The accumulated floating-point energy is persisted in the hook state, while the PBS `long` resource receives the rounded whole-Wh value.

The hook intentionally does not take a final epilogue sample because the GPU workload has normally exited by then; such a sample would bias utilization and average power downward.

### Persistent state

Per-job state below `<PBS_MOM_HOME>/mom_priv/hooks/<state_subdir>/` includes the allocated GPU inventory and telemetry accumulators. In addition to utilization and memory state, power accounting persists:

- sum and count of valid power samples;
- accumulated energy in Wh;
- previous power value and timestamp used for integration.

This allows periodic hook invocations to continue the running averages and energy integral.

### Lifecycle

The periodic event also removes stale GPU state for jobs that no longer exist. Epilogue/end/abort events release GPU-related state and access changes. Dynamic GPU resizing is not supported and `execjob_resize` is rejected.

### Dependencies

This hook depends on:

- `ngpus`, discovered/published by `hook_discovery_gpus` and allocated by PBS;
- a working NVIDIA driver and configured `nvidia-smi`;
- `power.draw` support for power/energy accounting;
- the job cgroup created by `hook_job_cgroups_v2` when device isolation is enabled.

It does not use `gpu_cap`, `gpu_arch`, or `gpu_model` to choose the concrete physical device after scheduling; those resources constrain scheduling before execution.
