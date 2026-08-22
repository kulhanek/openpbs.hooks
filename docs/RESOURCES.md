# OpenPBS Resources

This file summarizes the OpenPBS resources used by the hooks in this hook set.

For custom resources, **Type** and **Flags** are taken from the supplied `.qmgr` resource definitions. For standard OpenPBS resources, the table marks the flags as **built-in** because these hooks do not define their flags.

| Resource | Type | Flags | Description | Hooks |
| --- | --- | --- | --- | --- |
| `cgroups` | `string_array` | `h` | Detected Linux cgroup generation (`v1` or `v2`). | `hook_discovery_node` (sets); `hook_job_cgroups_v2` (uses) |
| `containers` | `string_array` | `h` | Container runtimes detected as executable on the vnode. | `hook_discovery_containers` (sets) |
| `cpu_flag` | `string_array` | `ha` | CPU capability flags common to the host. | `hook_discovery_cpus` (sets) |
| `cpu_isa` | `string_array` | `ho` | Highest supported x86-64 ISA level advertised by the vnode. | `hook_discovery_cpus` (sets); `hook_aggregate_resources` (reads/aggregates) |
| `cpu_model` | `string` | `h` | Processor model identifier. | `hook_discovery_cpus` (sets) |
| `cpu_spec` | `float` | `hl` | Site-defined CPU performance/classification value. | `hook_discovery_cpus` (sets) |
| `cpu_vendor` | `string` | `h` | Site-normalized CPU vendor identifier. | `hook_discovery_cpus` (sets) |
| `cuda_version` | `string_array` | `ho` | CUDA version reported for the execution host. | `hook_discovery_gpus` (sets) |
| `eth_speed` | `long` | `hl` | Maximum detected ordinary Ethernet link speed in Mb/s. | `hook_discovery_interconnect` (sets) |
| `gpu_arch` | `string_array` | `ho` | GPU architecture family, such as `ampere`, `ada`, or `hopper`. | `hook_discovery_gpus` (sets) |
| `gpu_cap` | `string_array` | `ho` | GPU compute capability in canonical `sm_XX` form. | `hook_discovery_gpus` (sets); `hook_normalize_job_gpucap` (modifies requests); `hook_aggregate_resources` (reads/aggregates) |
| `gpu_model` | `string` | `h` | GPU model identifier. | `hook_discovery_gpus` (sets) |
| `gpu_mem` | `size` | `hl` | Per-GPU framebuffer capacity advertised by the vnode; for multiple GPUs the minimum detected capacity is published. | `hook_discovery_gpus` (sets) |
| `gpu_vendor` | `string` | `h` | GPU vendor identifier; currently `nvidia`. | `hook_discovery_gpus` (sets) |
| `gpuenergyconsumed` | `long` | `r` | Reserved resource for GPU energy-consumption accounting; defined but not currently populated by the hook implementation. | `hook_job_gpus` (defines/reserves) |
| `gpumemmaxpercent` | `long` | `r` | Maximum observed aggregate percentage of allocated GPU framebuffer memory in use. | `hook_job_gpus` (sets accounting value) |
| `gpupercent` | `long` | `r` | Running mean of aggregate GPU utilization across GPUs allocated on the local host. | `hook_job_gpus` (sets accounting value) |
| `gpupowerusageavg` | `long` | `r` | Reserved resource for average GPU power accounting; defined but not currently populated by the hook implementation. | `hook_job_gpus` (defines/reserves) |
| `hybrid_cpu` | `boolean` | `h` | Indicates an asymmetric/hybrid SMT topology where physical cores expose different numbers of logical CPUs. | `hook_discovery_cpus` (sets) |
| `ib_speed` | `long` | `hl` | Maximum detected native InfiniBand link speed in Mb/s. | `hook_discovery_interconnect` (sets) |
| `interconnect` | `string_array` | `h` | Detected interconnect classes such as `ethernet`, `ib`, and `roce`. | `hook_discovery_interconnect` (sets) |
| `interconnect_speed` | `long` | `hl` | Maximum detected speed among supported interconnects, in Mb/s. | `hook_discovery_interconnect` (sets) |
| `mem` | `size` (built-in) | built-in | Physical memory capacity/request. Discovery publishes vnode capacity; cgroup execution enforces and accounts job memory; other hooks read allocated memory. | `hook_discovery_cpus` (sets vnode availability); `hook_job_cgroups_v2` (uses/enforces/accounts); `hook_job_env` (uses); `hook_workspace` (indirectly relevant to tmpfs jobs) |
| `mpiprocs` | `long` (built-in) | built-in | MPI processes requested per select chunk. | `hook_normalize_job_mpiomp` (sets/defaults/validates) |
| `ncpus` | `long` (built-in) | built-in | Physical CPU cores in this hook set; used for vnode capacity, scheduling, execution placement, and exported allocation totals. | `hook_discovery_cpus` (sets vnode availability); `hook_normalize_job_mpiomp` (sets/defaults/validates request); `hook_job_cgroups_v2` (uses); `hook_job_env` (uses) |
| `ngpus` | `long` | `hn` | Number of physical GPUs available/requested; consumable by jobs. | `hook_discovery_gpus` (sets vnode availability); `hook_job_gpus` (uses allocation); `hook_job_env` (uses) |
| `nodes` | built-in | built-in | Legacy job resource syntax; explicitly rejected by the MPI/OpenMP normalization hook. | `hook_normalize_job_mpiomp` (validates/rejects) |
| `npus_per_core` | `string` | `h` | Logical processing units per physical core when topology is uniform; also used to request predictable SMT expansion. | `hook_discovery_cpus` (sets); `hook_normalize_job_mpiomp` (uses/validates) |
| `nthreads` | `long` | `hn` | Logical CPU count. Discovery publishes host capacity; normalization may synthesize requests; cgroup execution records the actual exposed count. | `hook_discovery_cpus` (sets vnode availability); `hook_normalize_job_mpiomp` (sets/validates request); `hook_job_cgroups_v2` (sets accounting value/uses vocabulary); `hook_job_env` (uses) |
| `ompthreads` | `long` (built-in) | built-in | OpenMP/application threads per MPI process. | `hook_normalize_job_mpiomp` (sets/defaults/validates) |
| `os` | `string` | `h` | Site-normalized operating-system release identifier. | `hook_discovery_node` (sets); `hook_normalize_job_mpiomp` (uses for compatibility validation) |
| `osfamily` | `string` | `h` | Operating-system family such as `ubuntu` or `debian`. | `hook_discovery_node` (sets) |
| `pbs_server` | `string` | `h` | PBS server associated with the execution host. | `hook_discovery_node` (sets) |
| `place` | built-in | built-in | PBS placement expression; workspace validation may add a configured placement group for shared scratch. | `hook_workspace` (reads/modifies request) |
| `rdma` | `boolean` | `h` | Whether an active InfiniBand or RoCE RDMA path is detected. | `hook_discovery_interconnect` (sets) |
| `scratch_local` | `size` | `hn` | Consumable node-local scratch/workspace reservation. | `hook_workspace` (sets vnode availability; validates/uses job request) |
| `scratch_local_subtype` | `string` | `h` | Persistent selected node-local scratch backend subtype, such as `nvme`, `ssd`, or `hdd`. | `hook_workspace` (sets/uses) |
| `scratch_shared` | `size` | `hn` | Consumable shared-filesystem scratch reservation/placement resource. | `hook_workspace` (sets vnode availability; validates/uses job request) |
| `scratch_shared_subtype` | `string` | `h` | Persistent selected shared scratch backend subtype, such as `nfs`, `lustre`, `ceph`, or `gpfs`. | `hook_workspace` (sets/uses) |
| `scratch_shm` | `boolean` | `h` | Availability/request for a tmpfs shared-memory workspace. | `hook_workspace` (sets vnode availability; validates/uses job request) |
| `scratch_shm_subtype` | `string` | `h` | Persistent selected shared-memory backend subtype; currently `tmpfs`. | `hook_workspace` (sets/uses) |
| `select` | built-in select specification | built-in | Primary PBS chunked resource request. Normalization hooks rewrite it while preserving the user's original expression. | `hook_normalize_job_gpucap` (reads/modifies); `hook_normalize_job_mpiomp` (reads/modifies); `hook_workspace` (reads/validates/modifies) |
| `smt` | `boolean` | `h` | SMT capability/request. Controls whether all logical siblings of allocated physical cores are exposed. | `hook_discovery_cpus` (sets); `hook_normalize_job_mpiomp` (sets/validates request); `hook_job_cgroups_v2` (uses/sets accounting value); `hook_job_env` (uses) |
| `user_select` | `string` | none | Metadata copy of the original `Resource_List.select` before queuejob normalization. | `hook_normalize_job_gpucap` (sets if empty); `hook_normalize_job_mpiomp` (sets if empty) |
| `vmem` | `size` (built-in) | built-in | Virtual-memory capacity/request. CPU discovery may publish it; cgroup execution uses it to derive swap limits and may account it. | `hook_discovery_cpus` (optionally sets vnode availability); `hook_job_cgroups_v2` (uses/enforces/accounts) |
| `walltime` | `time` (built-in) | built-in | Requested job walltime. | `hook_job_env` (reads and exports as environment variables) |
