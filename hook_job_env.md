# `hook_job_env`

## 1. Overview

`hook_job_env` runs at `execjob_begin` and adds PBS/Torque-compatible resource information to the job environment. It derives per-node and job-wide CPU, memory, GPU, node-count, and walltime values from `exec_vnode`/`Resource_List`, and exposes the logical-thread/hyperthreading result produced by `hook_job_cgroups_v2`.

The supplied qmgr file enables only `execjob_begin` and configures hook order `10`.

## 2. User documentation

This hook does not introduce new resource requests. Users request normal PBS resources, for example:

```bash
#PBS -l select=2:ncpus=8:mem=16gb:ngpus=1
#PBS -l walltime=04:00:00
```

The hook then exports environment variables describing the allocation.

### Local-host environment variables

| Variable | Meaning |
|---|---|
| `PBS_RESC_MEM` | Memory assigned to the current execution host, in **bytes**. |
| `TORQUE_RESC_MEM` | Torque-compatible alias of `PBS_RESC_MEM`. |
| `PBS_NUM_PPN` | Number of `ncpus` assigned to the current execution host. |
| `PBS_NCPUS` | Number of `ncpus` assigned to the current execution host. |
| `TORQUE_RESC_PROC` | Torque-compatible local CPU count. |
| `PBS_NGPUS` | Number of GPUs assigned to the current execution host. |
| `PBS_NTHREADS` | Actual logical CPU count made available locally by `hook_job_cgroups_v2` (`resources_used.nthreads`). If unavailable, falls back to local `ncpus`. |
| `PBS_HYPERTHREADING` | `y` if `resources_used.hyperthreading` reports enabled, otherwise `n`. If unavailable, falls back to `n`. |

### Job-wide environment variables

| Variable | Meaning |
|---|---|
| `PBS_RESC_TOTAL_MEM` | Sum of memory allocations over all nodes, in **bytes**. |
| `TORQUE_RESC_TOTAL_MEM` | Torque-compatible alias. |
| `PBS_RESC_TOTAL_PROCS` | Sum of `ncpus` allocations over all nodes. |
| `TORQUE_RESC_TOTAL_PROCS` | Torque-compatible alias. |
| `PBS_NUM_NODES` | Number of distinct node names parsed from `exec_vnode`. |
| `PBS_RESC_TOTAL_WALLTIME` | Requested walltime converted by PBS/Python to an integer value, when `walltime` is present. |
| `TORQUE_RESC_TOTAL_WALLTIME` | Torque-compatible alias. |

### Example

For a job whose local allocation is four physical cores, one GPU, and 8 GiB memory, and for which the cgroup hook exposes eight SMT threads, the environment will contain values equivalent to:

```text
PBS_NCPUS=4
PBS_NUM_PPN=4
PBS_NGPUS=1
PBS_NTHREADS=8
PBS_HYPERTHREADING=y
PBS_RESC_MEM=8589934592
```

### Restrictions

- Memory parsing assumes the `exec_vnode` string contains `mem=<integer>kb`; the hook converts that value to bytes.
- Node matching strips the DNS suffix from `exec_vnode` node names and compares it with `pbs.get_local_nodename()` as used by the implementation.
- `PBS_NTHREADS` and `PBS_HYPERTHREADING` are most accurate when `hook_job_cgroups_v2` has already populated `resources_used.nthreads` and `resources_used.hyperthreading`.
- If those cgroup-derived values are not available, safe fallbacks are used: `PBS_NTHREADS=PBS_NCPUS` and `PBS_HYPERTHREADING=n`.

## 3. Technical documentation

### Operation

At `execjob_begin` the hook converts `job.exec_vnode` to a string and splits it by `+`. For every chunk it extracts:

- node name;
- `ncpus=<integer>`;
- `ngpus=<integer>`;
- `mem=<integer>kb`.

Values are accumulated per node. Local values are exported for the current MoM node, while total memory and CPU counts are summed across all parsed nodes.

The implementation accesses the custom `resources_used` fields through attributes (`getattr`) rather than iterating a `pbs_resource` object, avoiding the `pbs_resource has no attribute items` failure mode seen with dictionary-style iteration.

For the hyperthreading flag, the hook accepts common truth values (`1`, `true`, `t`, `yes`, `y`, `on`) case-insensitively and exports only `y` or `n`.

### qmgr configuration

The supplied setup defines:

```text
hook name : job_env
type      : site
event     : execjob_begin
user      : pbsadmin
alarm     : 60
order     : 10
fail_action: none
```

Only the Python source is imported; this hook has no JSON configuration file.

### JSON configuration

**None.** The current implementation has no configurable JSON items and does not read `PBS_HOOK_CONFIG_FILE`.

### Limitations and design notes

- The hook parses the textual representation of `exec_vnode` with regular expressions instead of using `exec_vnode.chunks`. It therefore depends on the current string representation, including memory being rendered in `kb`.
- It does not export per-node values for remote sister MoMs into separate variables; each MoM computes its local variables independently.
- `PBS_NUM_NODES` counts distinct node keys produced by the parser, not vnode chunks.
- The hook catches any unhandled exception and rejects the event with the generic message `env hook failed`; detailed diagnostics rely on PBS logs.
- Hook ordering should ensure any producer of `resources_used.nthreads`/`hyperthreading` has run before these values are consumed. The supplied qmgr file itself also uses order `10`, so site-wide ordering should be reviewed if both hooks share the same event/order.
