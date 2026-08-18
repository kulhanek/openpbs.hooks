# `hook_job_env`

## 1. Overview

`hook_job_env` runs at `execjob_begin` and adds PBS/Torque-compatible resource information to the job environment. It derives per-node and job-wide CPU, memory, GPU, node-count, and walltime values from `exec_vnode`/`Resource_List`, and exposes the logical-thread/SMT result produced by `hook_job_cgroups_v2`.

The supplied qmgr file enables only `execjob_begin` and configures hook order `20`, after `hook_job_cgroups_v2` (order `10`).

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
| `TORQUE_RESC_PROC` | Torque-compatible local CPU count. |
| `PBS_NGPUS` | Number of GPUs assigned to the current execution host. |
| `PBS_SMT` | `y` if `resources_used.smt` reports enabled, otherwise `n`. If unavailable, falls back to `n`. |

### Job-wide environment variables

| Variable | Meaning |
|---|---|
| `PBS_RESC_TOTAL_MEM` | Sum of memory allocations over all nodes, in **bytes**. |
| `TORQUE_RESC_TOTAL_MEM` | Torque-compatible alias. |
| `PBS_NCPUS` | Job-wide total number of physical CPU cores, summed over all allocated chunks/nodes. |
| `PBS_NTHREADS` | Job-wide total `nthreads`, normally taken from `resources_used.nthreads`. |
| `PBS_RESC_TOTAL_PROCS` | Sum of `ncpus` allocations over all nodes. |
| `TORQUE_RESC_TOTAL_PROCS` | Torque-compatible alias. |
| `PBS_NUM_NODES` | Number of distinct node names parsed from `exec_vnode`. |
| `PBS_RESC_TOTAL_WALLTIME` | Requested walltime converted by PBS/Python to an integer value, when `walltime` is present. |
| `TORQUE_RESC_TOTAL_WALLTIME` | Torque-compatible alias. |

### Example

For a two-node job with four physical cores per node, one local GPU, and a total normalized thread count of 16, the environment on each execution host will contain job-wide CPU totals equivalent to:

```text
PBS_NCPUS=8
PBS_NUM_PPN=4
PBS_NGPUS=1
PBS_NTHREADS=16
PBS_SMT=y
PBS_RESC_MEM=8589934592
```

### Restrictions

- Memory parsing assumes the `exec_vnode` string contains `mem=<integer>kb`; the hook converts that value to bytes.
- Node matching strips the DNS suffix from `exec_vnode` node names and compares it with `pbs.get_local_nodename()` as used by the implementation.
- `PBS_NCPUS` and `PBS_NTHREADS` are job-wide values; `PBS_NUM_PPN`, `PBS_RESC_MEM`, and `PBS_NGPUS` remain local-host values.
- `PBS_NTHREADS` prefers `resources_used.nthreads`. If it is unavailable, the hook sums `nthreads` parsed from `exec_vnode` and falls back to each chunk/node's `ncpus` for legacy allocations without `nthreads`.
- `PBS_SMT` is job-wide and prefers `resources_used.smt`; if unavailable, it falls back to `n`.

## 3. Technical documentation

### Operation

At `execjob_begin` the hook converts `job.exec_vnode` to a string and splits it by `+`. For every chunk it extracts:

- node name;
- `ncpus=<integer>`;
- `ngpus=<integer>`;
- `mem=<integer>kb`.

Values are accumulated per node. Local memory/GPU/PPN values are exported for the current MoM node. `PBS_NCPUS`, `PBS_NTHREADS`, total memory, and total processor counts are job-wide sums across all parsed nodes/chunks.

The implementation accesses the custom `resources_used` fields through attributes (`getattr`) rather than iterating a `pbs_resource` object, avoiding the `pbs_resource has no attribute items` failure mode seen with dictionary-style iteration.

For the SMT flag, the hook accepts common truth values (`1`, `true`, `t`, `yes`, `y`, `on`) case-insensitively and exports only `y` or `n`.

### qmgr configuration

The supplied setup defines:

```text
hook name : job_env
type      : site
event     : execjob_begin
user      : pbsadmin
alarm     : 60
order     : 20
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
- Hook ordering should ensure any producer of `resources_used.nthreads`/`smt` has run before these values are consumed. The supplied qmgr file uses order `20`, so `hook_job_cgroups_v2` at order `10` runs first and can populate these values before they are consumed.
