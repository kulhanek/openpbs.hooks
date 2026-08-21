# `hook_job_cgroups_v2`

## 1. Overview

`hook_job_cgroups_v2` provides per-job **cgroup v2 CPU and memory management**. It reserves whole physical CPU cores, constructs a per-job cpuset, optionally exposes SMT sibling PUs, applies memory/swap limits, attaches launched processes to the job cgroup, and updates PBS resource usage.

The hook is specifically designed around the convention that `resources_available.ncpus` represents **physical CPU cores**. CPU topology is read directly from Linux sysfs, so mixed-SMT and hybrid processors and non-trivial logical-CPU numbering are supported without assuming a fixed thread/core ratio.

The hook owns the lifetime of the job cgroup. `hook_job_gpus` may attach a cgroup-device BPF program to that cgroup but must run after this hook and must not create or delete the cgroup itself.

## 2. User documentation

### Resources operated by the hook

| Resource | Direction | Meaning |
|---|---|---|
| `ncpus` | request/allocation | Number of **physical CPU cores** reserved for the job on each host. |
| `smt` | request | Boolean request controlling whether all online SMT siblings of each selected physical core are exposed inside the job cpuset. |
| `mem` | request + `resources_used` | Requested memory becomes `memory.max`. If omitted/non-positive, `memory_default` is used. Peak cgroup memory becomes `resources_used.mem`. |
| `vmem` | request + `resources_used` | If supplied with `mem`, the difference `vmem - mem` becomes the cgroup swap limit. `resources_used.vmem` is peak memory plus peak swap when enabled. |
| `nthreads` | `resources_used` | Job-wide total of the normalized `nthreads` values across all select chunks, including chunk multiplicities. |
| `smt` | `resources_used` | Boolean recording whether SMT was enabled for this job. |
| `cput` | `resources_used` | CPU time derived from cgroup `cpu.stat` usage. |
| `cpupercent` | `resources_used` | Approximate CPU utilisation calculated from the change in cgroup CPU usage between updates. |

The hook expects the custom request resource:

```qmgr
create resource smt
set resource smt type = boolean
set resource smt flag = h
```

`nthreads` and `smt` must also be defined appropriately if their `resources_used` values are to be retained by PBS/site configuration.

### Normal CPU request

```bash
#PBS -l select=1:ncpus=8:mem=16gb
```

This reserves eight whole physical cores. With no SMT request, the hook places one logical CPU (the primary thread) from each selected core into the job cpuset. The sibling logical CPUs remain unavailable for allocation to another job because the hook records the physical core as reserved.

### Requesting SMT

```bash
#PBS -l select=1:ncpus=8:mem=16gb:smt=true
```

The job still consumes/reserves eight physical cores, but all online logical CPU siblings belonging to those cores are added to the cpuset. On a uniform 2-way SMT request with `npus_per_core=2`, this contributes `nthreads=16` to the job-wide `resources_used.nthreads` total. For generic/hybrid `smt=true` requests without `npus_per_core`, queue-time `nthreads` remains equal to `ncpus`; the local cgroup may nevertheless expose additional topology-dependent SMT siblings.

### Multi-chunk requests

`smt` is a **job-wide** cpuset policy. Cross-chunk consistency is validated by `hook_normalize_job_mpiomp` at `queuejob`, before the job is scheduled. Explicit `smt=true` and `smt=false` values must therefore not be mixed in one `select` request. Chunks which omit `smt` inherit the job-wide setting.

For robustness with jobs that were queued before the queue hook was installed or updated, this execution hook does not reject contradictory values itself. Its fallback parser enables SMT when **at least one** chunk contains `smt=true`. New submissions should never reach this fallback with contradictory explicit values.

If `smt` is absent from all chunks, it defaults to false.

### Restrictions

- The local vnode allocated to the job must advertise `resources_available.cgroups` containing `v2`; otherwise the job is rejected at `execjob_begin`.
- Every local job must allocate at least one physical CPU core (`ncpus > 0`).
- Only cgroup v2 is supported.
- Dynamic resource resizing is not supported; `execjob_resize` is rejected.
- The hook must run before `hook_job_gpus` on `execjob_begin`.
- `vmem < mem` is normalised upward to `mem`; swap is never configured as a negative amount.

## 3. Technical documentation

### Events

The supplied qmgr configuration enables:

```text
exechost_startup
exechost_periodic
execjob_begin
execjob_launch
execjob_attach
execjob_epilogue
execjob_end
execjob_abort
execjob_resize
```

It configures hook order `10`, periodic frequency `120` seconds, and imports both the Python hook and JSON configuration.

### Systemd/cgroup requirements

The default delegated cgroup root is:

```text
/sys/fs/cgroup/system.slice/pbs-mom.service
```

and per-job cgroups are created under:

```text
/sys/fs/cgroup/system.slice/pbs-mom.service/pbs_jobs/<jobid>
```

The source requires the `pbs_mom` systemd unit to delegate its cgroup hierarchy and keep the unit cgroup suitable for enabling controllers. The documented systemd >= 254 arrangement is:

```ini
[Service]
Delegate=yes
DelegateSubgroup=mom
```

At startup the hook verifies that unified cgroup v2 is mounted, enables available `cpu`, `cpuset`, and `memory` controllers below the delegated hierarchy, creates the jobs subdirectory, and initialises inherited cpuset values where necessary.

### Physical-core allocation

CPU topology is derived from `/sys/devices/system/cpu`. Logical CPUs are grouped using `core_cpus_list`, with `thread_siblings_list` as a fallback. For each physical core the hook records:

- sibling logical CPU IDs;
- a primary logical CPU (the lowest sibling ID);
- package/die/core identifiers;
- NUMA node;
- a stable core key based on its sibling CPU list.

Persistent per-job state records reserved core keys. This prevents a different job from receiving an SMT sibling of a core already reserved by another job, even when the first job exposes only its primary thread.

`placement` determines how free physical cores are selected:

- `packed`: prefer as few NUMA nodes as possible, choosing the fullest NUMA nodes first;
- `balanced`: round-robin physical cores across NUMA nodes.

The job cpuset's `cpuset.mems` is the set of NUMA nodes corresponding to the selected cores.

### Memory control

`memory.max` is set to the requested local `mem`, or to `memory_default` if no positive `mem` value is available. If the effective memory limit is non-positive, `memory.max` is set to `max`.

When both positive `vmem` and `mem` are available, the hook sets:

```text
memory.swap.max = max(0, vmem - mem)
```

Otherwise swap remains unlimited (`max`). Thus `vmem` is interpreted as total virtual-memory allowance (`RAM + swap`), not as a swap-only value.

### Process attachment

- `execjob_launch`: the hook attaches the process session associated with the MoM launch path to the job cgroup.
- `execjob_attach`: the explicitly supplied PID/session is attached to the existing job cgroup.

### Usage accounting

The hook reads cgroup usage files and updates PBS usage:

- CPU usage from `cpu.stat` (`usage_usec`) -> `resources_used.cput`;
- peak/current memory -> `resources_used.mem`;
- peak/current swap combined with memory -> `resources_used.vmem` when enabled;
- interval CPU usage -> `resources_used.cpupercent`;
- normalized job allocation -> job-wide `resources_used.nthreads` and `resources_used.smt`;
- local state separately retains the actual local cpuset PU count as `nthreads_local`.

Periodic accounting can be disabled. The hook also removes stale cgroups/state for jobs no longer present in the MoM periodic job list, with a short grace interval after creation.

At epilogue it performs a final usage update and removes the cgroup, while retaining state until the end event so other hooks/late accounting can still inspect the allocation. At end/abort it removes both cgroup and state.

### JSON configuration

| Item | Type | Default | Description |
|---|---:|---:|---|
| `cgroup_root` | string/path | `/sys/fs/cgroup/system.slice/pbs-mom.service` | Delegated cgroup-v2 root belonging to `pbs_mom`. |
| `jobs_subdir` | string | `pbs_jobs` | Directory below `cgroup_root` in which per-job cgroups are created. |
| `state_subdir` | string | `cgroup_v2` | Persistent state directory below `PBS_MOM_HOME/mom_priv/hooks/`. |
| `placement` | string | `packed` | Physical-core placement policy. Recognised design values are `packed` and `balanced`; any value other than `balanced` follows the packed branch. |
| `memory_default` | size | `400MB` | Memory limit used when a positive local `mem` request cannot be obtained. |
| `publish_vmem` | boolean | `true` | Whether to update `resources_used.vmem`. This does not disable application of an explicitly requested vmem/swap limit. |
| `periodic_usage_update` | boolean | `true` | If true, refresh usage values for live jobs during `exechost_periodic`. |
| `kill_timeout` | number/seconds | `10` | Maximum wait while terminating processes before removing a job cgroup. |
| `cpu_weight` | integer | `100` | Value written to cgroup `cpu.weight`, clamped by the implementation to `1..10000`. The hook leaves `cpu.max` unlimited. |

### Limitations and design notes

- This is Linux/cgroup-v2-specific code and depends on writable delegated cgroup controller files.
- Core reservations are tracked in hook state files rather than reconstructed from arbitrary external cpusets. Other software modifying the same CPU placement independently can therefore invalidate the hook's assumptions.
- CPU quota throttling is not used: `cpu.max` is set to `max 100000`; CPU containment relies on cpusets and whole-core reservation.
- `smt` is job-wide, not independently configurable per chunk on different hosts; queue-time validation is owned by `hook_normalize_job_mpiomp`.
- `cpupercent` is based on successive cgroup usage samples and is therefore interval/sampling dependent.
- Dynamic resizing is deliberately unsupported.
