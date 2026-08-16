# `hook_job_gpus`

## 1. Overview

`hook_job_gpus` manages whole NVIDIA GPUs for running PBS jobs. It allocates physical GPUs to the local job, optionally isolates GPU device nodes with a cgroup-v2 `BPF_CGROUP_DEVICE` program, manages DRM device ACLs, sets CUDA visibility for launched processes, and records lightweight GPU utilisation/memory telemetry from `nvidia-smi`.

CPU/memory cgroup creation is intentionally outside this hook. The job cgroup must already have been created by `hook_job_cgroups_v2`; consequently the supplied configuration runs `job_gpus` after the cgroup hook (`order=20` versus `order=10`).

## 2. User documentation

### GPU requests

The hook consumes the local `ngpus` assignment from `exec_vnode`. A normal request is:

```bash
#PBS -l select=1:ncpus=8:ngpus=1
```

or, for two GPUs:

```bash
#PBS -l select=1:ncpus=16:ngpus=2
```

The hook allocates complete physical GPUs. MIG and MIC are intentionally unsupported.

### Environment set for launched processes

| Variable | Meaning |
|---|---|
| `CUDA_VISIBLE_DEVICES` | Comma-separated GPU UUIDs allocated to this local job. The implementation escapes commas for the PBS hook environment representation. For a zero-GPU job it is explicitly set to an empty string, preventing accidental CUDA use of unallocated GPUs. |
| `CUDA_DEVICE_ORDER` | Set to `PCI_BUS_ID` when at least one GPU is allocated. |

Because UUIDs are used, applications should not assume that the values in `CUDA_VISIBLE_DEVICES` are the node-global numeric GPU indices.

### Accounting resources

The supplied qmgr file creates two resources:

```qmgr
create resource gpupercent
set resource gpupercent type = long
set resource gpupercent flag = r

create resource gpumemmaxpercent
set resource gpumemmaxpercent type = long
set resource gpumemmaxpercent flag = r
```

Their meanings are:

| Resource | Meaning |
|---|---|
| `resources_used.gpupercent` | Running arithmetic mean of the **sum** of instantaneous GPU utilisation percentages across all GPUs allocated to the local job. Range is `0..100*N` for `N` GPUs. |
| `resources_used.gpumemmaxpercent` | Maximum observed aggregate allocated-GPU memory fraction: `100 * sum(memory.used) / sum(memory.total)`. Range `0..100`. |

For example, if a two-GPU job averages 70% utilisation on one GPU and 60% on the other at sampling times, `gpupercent` can be approximately `130`.

### Restrictions

- Physical NVIDIA GPUs only; no MIG support.
- GPU allocation is immutable for the lifetime of a job. `execjob_resize` is rejected.
- A requested GPU job is rejected if `nvidia-smi` cannot provide a usable physical-GPU inventory.
- The per-job cgroup created by `hook_job_cgroups_v2` must already exist at `execjob_begin`; otherwise the job is rejected.
- Device isolation failures are fatal. Telemetry failures are non-fatal and only affect accounting data.
- GPU accounting is **sampled** on `exechost_periodic`; it is not continuous hardware integration. Short jobs can finish before the first sample.

## 3. Technical documentation

### Events and ordering

The supplied qmgr setup enables:

```text
exechost_periodic
execjob_begin
execjob_launch
execjob_epilogue
execjob_end
execjob_abort
execjob_resize
```

with periodic frequency `30` seconds and hook order `20`. The source explicitly requires the CPU/memory cgroup hook to execute first on `execjob_begin`.

### GPU inventory and allocation

At begin, `local_ngpus()` sums assigned `ngpus` over local `exec_vnode` chunks. `nvidia-smi` is queried for:

```text
index, uuid, pci.bus_id, memory.total
```

The runtime inventory also resolves each GPU's NUMA node, `/dev/nvidiaN` device number, and associated `/dev/dri/card*`/`renderD*` DRM nodes.

Allocation state is persisted below `PBS_MOM_HOME/mom_priv/hooks/<state_subdir>`. Existing state files are used to identify GPU UUIDs already assigned to other jobs. The allocation strategy is:

- `index`: choose the lowest free NVIDIA indices;
- `numa`: sort free GPUs by `(NUMA node, index)` and choose the first entries.

The current `numa` mode does **not** correlate GPU NUMA placement with the CPU cores selected for the same job; it only changes the ordering of free GPUs.

### Device isolation

When `device_isolation=true`, the hook builds and attaches a minimal eBPF `BPF_PROG_TYPE_CGROUP_DEVICE` program to the existing per-job cgroup. It explicitly matches NVIDIA and associated DRM device major/minor numbers, allowing selected GPUs and rejecting those belonging to unallocated GPUs. Unrelated devices are allowed by default.

The implementation contains direct Linux `bpf()` syscall numbers for `x86_64/amd64` and `aarch64/arm64`; other architectures are rejected when device isolation is attempted.

### DRM ACL management

When `manage_drm_acl=true`, `setfacl` is used to add per-user ACL access to selected DRM nodes and to remove those ACL entries at epilogue/end or stale-state cleanup. `/dev/nvidiaN` access is primarily controlled through the cgroup-device policy.

### CUDA environment

At `execjob_launch`, the hook loads the persisted allocation and sets `CUDA_VISIBLE_DEVICES` to allocated GPU UUIDs. `CUDA_DEVICE_ORDER=PCI_BUS_ID` is added for GPU jobs.

### Telemetry

With `telemetry=true`, each periodic event obtains a single node-wide `nvidia-smi` sample containing:

```text
uuid, utilization.gpu, memory.used, memory.total
```

For each live GPU job, only rows matching its allocated UUIDs are used. A sample is discarded for that job if any allocated GPU is missing from the sample.

`gpupercent` is the arithmetic mean over periodic samples of the per-sample sum of utilisation values. `gpumemmaxpercent` is the maximum aggregate memory fraction observed over all valid samples. The hook intentionally does not take a final epilogue sample because the GPU workload has normally already exited and such a sample would bias utilisation downward.

### JSON configuration

| Item | Type | Default | Description |
|---|---:|---:|---|
| `cgroup_root` | path | `/sys/fs/cgroup/system.slice/pbs-mom.service` | Must match the delegated cgroup root used by `hook_job_cgroups_v2`. |
| `jobs_subdir` | string | `pbs_jobs` | Must match the job-cgroup subdirectory used by `hook_job_cgroups_v2`. |
| `state_subdir` | string | `gpu_v2` | State directory below `PBS_MOM_HOME/mom_priv/hooks/`. |
| `nvidia_smi` | path | `/usr/bin/nvidia-smi` | NVIDIA utility used for inventory and telemetry. |
| `device_isolation` | boolean | `true` | Attach a cgroup-device BPF policy restricting NVIDIA/DRM device access to the allocated GPUs. |
| `manage_drm_acl` | boolean | `true` | Add/remove user ACLs on associated `/dev/dri` nodes. |
| `telemetry` | boolean | `true` | Enable periodic `nvidia-smi` accounting. Allocation and isolation still operate when telemetry is disabled. |
| `allocation` | string | `index` | GPU selection ordering. `numa` uses `(NUMA,index)` ordering; other values follow index ordering. |

### Limitations and design notes

- This hook assumes GPU allocation is exclusively coordinated through its persistent state. External allocators changing GPU ownership independently can conflict with that state.
- Device isolation depends on Linux cgroup v2 and eBPF cgroup-device support and sufficient privileges for BPF program loading/attachment.
- BPF isolation covers NVIDIA and discovered associated DRM nodes; the program permits unrelated device nodes.
- The telemetry metric is a simple equally weighted sample mean, so its accuracy depends on `freq` and job duration.
- `gpumemmaxpercent` is aggregate allocated-GPU memory occupancy, not the maximum occupancy of any single GPU.
- State cleanup removes stale ACLs/allocation records. The BPF program itself disappears with the job cgroup, whose lifecycle is owned by `hook_job_cgroups_v2`.
