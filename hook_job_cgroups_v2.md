# `hook_job_cgroups_v2`

## Overview

`hook_job_cgroups_v2` provides per-job CPU and memory isolation using Linux cgroup v2. It creates one job cgroup on each execution host used by the job, selects physical CPU cores assigned to that host, configures the corresponding cpuset and memory limits, moves job processes into the cgroup, and records CPU/memory usage for PBS accounting.

The hook is designed around this site's CPU resource model: `ncpus` is physical-core capacity. Without SMT, one logical CPU from each allocated physical core is exposed to the job. With `smt=true`, all online SMT siblings of each allocated physical core are exposed.

GPU device filtering is deliberately not implemented here. `hook_job_gpus` attaches GPU device controls to the cgroup created by this hook.

## User documentation

For ordinary jobs the hook is automatic. A request such as:

```bash
#PBS -l select=1:ncpus=8:mem=16gb
```

causes the execution host to create a cgroup containing the CPU and memory resources assigned to the local part of the job. Processes started by PBS are placed into that cgroup automatically.

### SMT requests

A job may request SMT explicitly:

```bash
#PBS -l select=1:ncpus=8:smt=true
```

`ncpus=8` still reserves eight physical cores. With `smt=true`, all online hardware threads belonging to those eight cores are exposed. The actual number of logical CPUs is recorded in `resources_used.nthreads` and can depend on the host topology.

For MPI/OpenMP jobs that need a predictable application thread count, use the conventions documented for `hook_normalize_job_mpiomp`, including `npus_per_core` when an exact SMT expansion is required.

### Memory limits

When `mem` is requested, the job cgroup enforces a physical-memory limit. When `vmem` is also available, the hook derives the cgroup swap allowance from the relationship between `mem` and `vmem`.

Exceeding a cgroup memory limit can cause the kernel to terminate processes in the job. This is an enforced limit, not merely an accounting value.

### Multi-node and multi-chunk jobs

On each execution host, requests from all chunks placed on that host are aggregated into one local job cgroup and one cpuset. There is not a separate cgroup for each `select` chunk.

Processes launched on sister hosts through PBS-aware launch mechanisms are attached to the corresponding job cgroup on those hosts.

## Technical and administration documentation

### Hook events

The supplied `hook_job_cgroups_v2.qmgr` installs the hook for:

- `exechost_startup`
- `exechost_periodic`
- `execjob_begin`
- `execjob_launch`
- `execjob_attach`
- `execjob_epilogue`
- `execjob_end`
- `execjob_abort`
- `execjob_resize`

The periodic interval is 120 seconds and the hook order is 10. It must run before hooks that depend on the job cgroup, notably `hook_job_gpus`.

### Cgroup hierarchy

The supplied configuration is:

```json
{
    "cgroup_root": "/sys/fs/cgroup/system.slice/pbs-mom.service",
    "jobs_subdir": "pbs_jobs",
    "state_subdir": "cgroup_v2",
    "placement": "packed",
    "memory_default": "400MB",
    "publish_vmem": true,
    "periodic_usage_update": true,
    "kill_timeout": 10,
    "cpu_weight": 100
}
```

Job cgroups are created below:

```text
<cgroup_root>/<jobs_subdir>/<job-id>
```

The hook enables and uses the cgroup v2 `cpuset`, `cpu`, and `memory` controllers. The PBS Mom service cgroup must therefore be configured with delegation sufficient for the daemon to create and manage child cgroups and controllers. On systemd-based hosts, the PBS Mom unit must have appropriate `Delegate=` configuration.

### Configuration fields

| Field | Description |
| --- | --- |
| `cgroup_root` | cgroup v2 directory containing the PBS Mom service. |
| `jobs_subdir` | Child directory used for PBS job cgroups. |
| `state_subdir` | Directory below the Mom private directory used for persistent hook state. |
| `placement` | CPU-core placement policy. Supported implementation values include packed/balanced placement. |
| `memory_default` | Memory limit used when a local job allocation does not provide an explicit `mem` value. |
| `publish_vmem` | Whether virtual-memory usage is published to PBS accounting. |
| `periodic_usage_update` | Whether the periodic event refreshes running-job usage. |
| `kill_timeout` | Time allowed during cgroup cleanup before stronger termination/cleanup actions. |
| `cpu_weight` | Value written to cgroup v2 `cpu.weight`, constrained to the kernel-supported range. |

### CPU selection

The hook reads Linux CPU topology and maintains state so that simultaneously running PBS jobs do not reserve the same physical cores.

For a local allocation of `ncpus=N`:

1. `N` physical cores are selected.
2. Without SMT, one online logical CPU per selected core is placed in `cpuset.cpus`.
3. With `smt=true`, all online siblings of those selected cores are placed in the cpuset.
4. The corresponding NUMA memory nodes are written to `cpuset.mems`.

The whole physical core remains reserved to the PBS job even when only one logical thread is exposed. Hybrid or asymmetric SMT topologies are supported; consequently `resources_used.nthreads` is derived from the actual cpuset rather than assumed to be `ncpus * 2`.

### Memory enforcement

The hook writes `memory.max` from the local `mem` allocation. When both `mem` and `vmem` are positive, `memory.swap.max` is derived as `vmem - mem`; invalid cases where `vmem < mem` are normalized so the virtual-memory limit is not below physical memory. When no finite swap limit can be derived, the implementation can use the cgroup v2 unlimited value.

### Resource and capability dependencies

This `.qmgr` file creates no custom PBS resources. The hook consumes resources defined elsewhere:

| Resource | Source | Use |
| --- | --- | --- |
| `ncpus` | Standard PBS resource, populated by CPU discovery | Number of physical cores to reserve locally. |
| `mem` | Standard PBS resource | cgroup physical-memory limit. |
| `vmem` | Standard PBS resource | Used to derive swap/virtual-memory enforcement. |
| `smt` | `hook_discovery_cpus.qmgr` | Whether all SMT siblings of selected cores should be exposed. |
| `cgroups` | `hook_discovery_node.qmgr` | Execution vnode must advertise `v2`. |
| `nthreads` | `hook_discovery_cpus.qmgr` | Resource/accounting vocabulary for logical CPUs. |

At `execjob_begin`, every local allocated vnode must advertise cgroup v2. A job is rejected on a local vnode that does not.

### Usage accounting

The hook updates standard/custom `resources_used` values from cgroup files, including:

- `cput` from `cpu.stat`;
- `cpupercent` from CPU-usage deltas;
- `mem` from cgroup memory usage/peak data;
- `vmem` when enabled, including swap usage as implemented;
- `nthreads` as the actual number of logical CPUs exposed in the job cpuset;
- `smt` as the effective SMT mode.

The periodic event can refresh these values while a job runs, and the epilogue records final usage before cleanup.

### Lifecycle and state

The hook stores allocation state below the Mom private directory, using `state_subdir`. The periodic event also removes orphaned state/cgroups left by jobs that no longer exist.

`execjob_launch` and `execjob_attach` move job/session PIDs into the job cgroup. The epilogue/end/abort paths perform final accounting and cleanup. Dynamic job resizing is not supported by this implementation and is rejected.
