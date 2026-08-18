# OpenPBS Hooks for Small Clusters

This repository contains a set of OpenPBS hooks intended for small clusters built from commodity PCs and entry-level servers. The hooks provide automatic node resource discovery, cgroups v2 based CPU and memory isolation, NVIDIA GPU allocation and accounting, job environment setup, and per-job workspace management.

The hooks were tested on **Ubuntu 24.04** and are designed to work with the **cgroups v2** controller. They are primarily aimed at relatively simple OpenPBS installations where execution nodes are managed directly by PBS MoM without an additional cluster resource-management layer.

## Notes
The code was mostly created in ChatGPT chat (Plus Subscription) and is currently tested on a small cluster. 

## Hooks

- [hook_discovery_node](hook_discovery_node.md) — discovers basic node properties, operating-system information, cgroup version, and PBS server association.
- [hook_discovery_cpus](hook_discovery_cpus.md) — discovers physical CPU cores, logical threads, CPU properties, memory, and CPU performance metadata.
- [hook_discovery_gpus](hook_discovery_gpus.md) — discovers physical NVIDIA GPUs and publishes GPU model, compute capability, and CUDA driver information.
- [hook_job_enqueued](hook_job_enqueued.md) — normalises and validates CPU/MPI/OpenMP `select` chunks, applies CPU defaults, and enforces a consistent job-wide SMT request before scheduling.
- [hook_job_cgroups_v2](hook_job_cgroups_v2.md) — creates per-job cgroups v2, allocates physical CPU cores, optionally enables SMT PU siblings, enforces memory limits, and provides CPU/memory accounting.
- [hook_job_gpus](hook_job_gpus.md) — allocates whole NVIDIA GPUs, applies cgroups v2 device isolation, sets CUDA visibility, and collects lightweight GPU accounting data.
- [hook_job_env](hook_job_env.md) — exports PBS/Torque-compatible environment variables describing the resources assigned to a job.
- [hook_workspace](hook_workspace.md) — provides dynamically configured scratch resources, persistently selects storage subtypes during node discovery, publishes scheduler-selectable subtype properties, and supports managed or filesystem-total capacity accounting.
