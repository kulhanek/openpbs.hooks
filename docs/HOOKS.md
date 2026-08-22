# OpenPBS Hooks

This file provides a brief overview of the OpenPBS hooks included in this hook set.

| Hook | Description |
| --- | --- |
| `hook_aggregate_resources` | Periodically aggregates selected vnode resource values, such as GPU capabilities and CPU ISA levels, into a cluster-wide JSON state file for use by other hooks and administrative tooling. |
| `hook_discovery_containers` | Detects container runtimes that are actually executable on each execution host and publishes the available runtime names as vnode resources. |
| `hook_discovery_cpus` | Discovers CPU topology, processor identity and capabilities, memory capacity, SMT properties, CPU ISA level, and site-specific CPU classification resources. |
| `hook_discovery_gpus` | Discovers physical GPUs and publishes GPU count, vendor, model, memory, compute capability, architecture, and CUDA compatibility information as vnode resources. |
| `hook_discovery_interconnect` | Detects usable Ethernet and RDMA-capable interconnects, including InfiniBand and RoCE, and publishes interconnect types and link speeds. |
| `hook_discovery_node` | Publishes basic execution-host classification information, including operating-system identity, OS family, cgroup version, and associated PBS server. |
| `hook_job_cgroups_v2` | Creates and manages per-job cgroup v2 environments for CPU and memory isolation, process placement, and CPU/memory usage accounting. |
| `hook_job_env` | Exposes final PBS allocation information through a consistent set of PBS/Torque-compatible environment variables for CPUs, threads, GPUs, memory, nodes, and walltime. |
| `hook_job_gpus` | Assigns concrete NVIDIA GPUs to scheduled jobs, applies GPU device isolation, sets CUDA visibility, tracks device ownership, and records GPU usage accounting. |
| `hook_normalize_job_gpucap` | Normalizes GPU capability expressions in job requests, including `sm_XX`, `compute_XX`, `exact[...]`, and forward-compatible `compat[...]` forms. |
| `hook_normalize_job_mpiomp` | Validates and normalizes CPU, MPI, OpenMP, SMT, and logical-thread requests so that `ncpus`, `nthreads`, `mpiprocs`, and `ompthreads` remain consistent. |
| `hook_workspace` | Discovers configured scratch/workspace backends, validates scratch requests, creates per-job workspaces, exports scratch environment variables, and performs cleanup. |
