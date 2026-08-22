# `hook_job_env`

## Overview

`hook_job_env` adds a consistent set of PBS/Torque-compatible resource environment variables to a job at execution time. It derives local and job-wide CPU, thread, GPU, memory, node-count, and walltime values from the final PBS allocation.

The hook does not discover resources, modify scheduling, or enforce limits. It exposes already-established allocation information in a convenient form for job scripts and applications.

## User documentation

The variables are added automatically; users do not need to request the hook explicitly.

A job script can use variables such as:

```bash
echo "Local physical cores: $PBS_NCPUS"
echo "Job logical threads:   $PBS_NTHREADS"
echo "Local GPUs:            $PBS_NGPUS"
echo "Nodes:                 $PBS_NUM_NODES"
```

The hook also provides PBS and Torque compatibility names for memory and processor counts. Memory values exported by this hook are expressed in bytes, and exported walltime is expressed in seconds.

Important variables include:

| Variable | Meaning |
| --- | --- |
| `PBS_NUM_PPN` | `ncpus` allocated on the current execution host. |
| `PBS_NGPUS` | GPUs allocated on the current execution host. |
| `PBS_RESC_MEM` | Memory allocated on the current execution host, in bytes. |
| `PBS_NCPUS` | Job-wide total `ncpus` across all execution hosts. |
| `PBS_NTHREADS` | Job-wide logical-thread count, preferring cgroup-derived `resources_used.nthreads` when available. |
| `PBS_NUM_NODES` | Number of distinct execution hosts in the allocation. |
| `PBS_RESC_TOTAL_MEM` | Job-wide allocated memory, in bytes. |
| `PBS_RESC_TOTAL_PROCS` | Job-wide total `ncpus`. |
| `PBS_RESC_TOTAL_WALLTIME` | Requested walltime in seconds, when present. |
| `PBS_SMT` | `y` when the effective job SMT mode is true, otherwise `n`. |

Equivalent `TORQUE_RESC_*` compatibility variables are set where implemented.

## Technical and administration documentation

### Hook events and ordering

The supplied `hook_job_env.qmgr` installs the hook only for `execjob_begin`, with hook order 40.

It intentionally runs after:

- `hook_job_cgroups_v2` (order 10);
- `hook_job_gpus` (order 20);
- `hook_workspace` (order 30).

This allows it to expose values after the execution environment and resource-specific hooks have established their state.

### Allocation parsing

The hook parses the final `job.exec_vnode` specification and groups chunks by execution host. For each host it aggregates:

- `ncpus`;
- `nthreads` when present;
- `ngpus`;
- `mem`.

It then derives both local-host and job-wide totals.

### Variables set by the hook

Local values include:

| Variable | Value |
| --- | --- |
| `PBS_RESC_MEM`, `TORQUE_RESC_MEM` | Local allocated memory in bytes. |
| `PBS_NUM_PPN`, `TORQUE_RESC_PROC` | Local `ncpus`. |
| `PBS_NGPUS` | Local allocated GPU count. |
| `PBS_SMT` | `y`/`n` from effective `resources_used.smt`, falling back to `n`. |

Job-wide values include:

| Variable | Value |
| --- | --- |
| `PBS_RESC_TOTAL_MEM`, `TORQUE_RESC_TOTAL_MEM` | Sum of allocated memory in bytes. |
| `PBS_RESC_TOTAL_PROCS`, `TORQUE_RESC_TOTAL_PROCS` | Sum of `ncpus`. |
| `PBS_NCPUS` | Sum of `ncpus`. |
| `PBS_NTHREADS` | Logical CPU count. The hook prefers `resources_used.nthreads`; otherwise it sums `nthreads` parsed from `exec_vnode`, falling back to `ncpus` for chunks without it. |
| `PBS_NUM_NODES` | Number of distinct execution hosts. |
| `PBS_RESC_TOTAL_WALLTIME`, `TORQUE_RESC_TOTAL_WALLTIME` | Requested walltime converted to seconds when `Resource_List.walltime` is set. |

### Configuration and resources

This hook has no JSON configuration file and its `.qmgr` file creates no PBS resources. It consumes the final allocation and values populated by the other hooks.

### Administration notes

Because these variables describe the final allocation, this hook should remain late in the `execjob_begin` ordering. If resource naming or the site's `ncpus`/`nthreads` semantics change, this hook must be reviewed together with CPU discovery, cgroup placement, and MPI/OpenMP normalization.
