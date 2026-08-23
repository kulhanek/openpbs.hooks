# OpenPBS Hooks

This file provides a brief overview of the OpenPBS hooks included in this hook set.

| Hook | Description |
| --- | --- |
| [`hook_aggregate_resources`](hook_aggregate_resources.md) | Periodically aggregates selected vnode resource values, such as GPU capabilities and CPU ISA levels, into a cluster-wide JSON state file for use by other hooks and administrative tooling. |
| [`hook_discovery_containers`](hook_discovery_containers.md) | Detects container runtimes that are actually executable on each execution host and publishes the available runtime names as vnode resources. |
| [`hook_discovery_cpus`](hook_discovery_cpus.md) | Discovers CPU topology, processor identity and capabilities, CPU architecture and ISA level, memory capacity, SMT properties, and site-specific CPU classification resources on x86-64 and AArch64 hosts. |
| [`hook_discovery_gpus`](hook_discovery_gpus.md) | Discovers physical NVIDIA and AMD GPUs and publishes GPU count, vendor, model, memory, native/portable capability or target, architecture family, and NVIDIA CUDA compatibility information as vnode resources. |
| [`hook_discovery_interconnect`](hook_discovery_interconnect.md) | Detects usable Ethernet and RDMA-capable interconnects, including InfiniBand and RoCE, and publishes interconnect types and link speeds. |
| [`hook_discovery_node`](hook_discovery_node.md) | Publishes basic execution-host classification information, including operating-system identity, OS family, cgroup version, and associated PBS server. |
| [`hook_job_cgroups_v2`](hook_job_cgroups_v2.md) | Creates and manages per-job cgroup v2 environments for physical-core CPU and memory isolation, process placement, and CPU/memory usage accounting. |
| [`hook_job_env`](hook_job_env.md) | Exposes final PBS allocation information through a consistent set of PBS/Torque-compatible environment variables for CPUs, threads, GPUs, memory, nodes, and walltime. |
| [`hook_job_gpus`](hook_job_gpus.md) | Assigns concrete NVIDIA or AMD physical GPUs to scheduled jobs, applies vendor-neutral cgroup device isolation, configures CUDA/HIP visibility, tracks device ownership, and records GPU utilization, memory, power, and energy accounting. |
| [`hook_normalize_job_cpuisa`](hook_normalize_job_cpuisa.md) | Normalizes per-chunk `cpu_isa` requests, including comma-separated tokens and `compat[...]` expansion from architecture-specific compatibility maps, with optional filtering against the aggregate cluster CPU-ISA inventory. |
| [`hook_normalize_job_gpucap`](hook_normalize_job_gpucap.md) | Normalizes per-chunk `gpu_cap` token lists; ordinary tokens are preserved while NVIDIA `compute_XX` is converted to `sm_XX` and expanded to later configured NVIDIA capabilities, optionally filtered by aggregate GPU inventory. |
| [`hook_normalize_job_mpiomp`](hook_normalize_job_mpiomp.md) | Validates and normalizes CPU, MPI, OpenMP, SMT, and logical-thread requests so that `ncpus`, `nthreads`, `mpiprocs`, and `ompthreads` remain consistent. |
| [`hook_workspace`](hook_workspace.md) | Discovers configured scratch/workspace backends, validates scratch requests, creates per-job workspaces, exports scratch environment variables, and performs cleanup. |
