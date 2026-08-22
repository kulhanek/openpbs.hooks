# `hook_normalize_job_mpiomp`

## Overview

`hook_normalize_job_mpiomp` validates and normalizes CPU, MPI, OpenMP, SMT, and logical-thread requests in `Resource_List.select` at submission time. Its purpose is to make the relationship between physical cores (`ncpus`), logical CPUs (`nthreads`), MPI ranks (`mpiprocs`), and OpenMP threads (`ompthreads`) explicit and internally consistent before the scheduler sees the job.

The hook also preserves the user's original select expression in `Resource_List.user_select` before normalization.

## User documentation

### Default MPI/OpenMP values

If a chunk contains `ncpus` but omits MPI/OpenMP parameters, the hook supplies defaults. For example:

```bash
#PBS -l select=1:ncpus=8
```

is normalized so that the chunk has eight MPI processes and one OpenMP thread per process (`mpiprocs=8`, `ompthreads=1`).

A mixed MPI/OpenMP request can be written explicitly:

```bash
#PBS -l select=1:ncpus=8:mpiprocs=4:ompthreads=2
```

The product `mpiprocs * ompthreads` must fit within the CPU/thread capacity represented by the chunk.

### Physical cores and logical threads

In this PBS installation, `ncpus` means physical cores. A plain request therefore has application capacity equal to `ncpus`.

To request a known number of logical processing units per physical core, use `npus_per_core`. For example:

```bash
#PBS -l select=1:ncpus=8:npus_per_core=2:mpiprocs=8:ompthreads=2
```

The hook normalizes this to SMT-enabled execution with `nthreads=16`. `npus_per_core=2` means that each of the eight allocated physical cores is expected to expose two usable logical CPUs.

If `npus_per_core` is present and `smt` is omitted, the hook adds `smt=true`. An explicit `smt=false` together with `npus_per_core` is rejected.

An explicit `smt=true` by itself does **not** increase the application thread capacity during normalization, because the hook cannot infer a uniform logical-CPU multiplier from that statement alone. Use `npus_per_core` when a predictable expanded capacity is required.

### Explicit `nthreads`

When `npus_per_core` is present:

```text
nthreads = ncpus * npus_per_core
```

If the user supplies `nthreads`, it must equal that value. Otherwise the hook adds the correct value automatically.

Without `npus_per_core`, an explicitly requested `nthreads` must equal `ncpus`; use `npus_per_core` to request additional SMT threads.

### MPI process count

`mpiprocs` defaults to `ncpus`. It must be positive and cannot exceed `ncpus`. This keeps MPI rank placement tied to allocated physical-core capacity even when SMT is enabled for OpenMP/application threads.

### Consistency across chunks

If several select chunks explicitly specify `smt`, their values must not contradict one another. The execution cgroup hook applies SMT as a job-wide execution mode on a host, so incompatible per-chunk SMT requests are rejected early.

### Legacy `nodes=` syntax

The legacy `Resource_List.nodes` syntax is rejected. Jobs using these normalization rules must use `Resource_List.select`.

## Technical and administration documentation

### Hook event

The supplied `hook_normalize_job_mpiomp.qmgr` installs the hook for `queuejob` with order 10.

### Normalization rules

For each select chunk the hook applies the following rules:

1. `ncpus` defaults to 1 and must be positive.
2. `smt`, when present, must parse as a valid boolean.
3. `npus_per_core`, when present, must be a positive integer. Although the vnode resource is typed as a string for scheduler matching, job requests are normalized to a decimal integer representation.
4. With `npus_per_core`:
   - `smt=false` is invalid;
   - missing `smt` becomes `smt=true`;
   - expected `nthreads = ncpus * npus_per_core`;
   - missing `nthreads` is synthesized;
   - an explicit different `nthreads` is rejected;
   - application thread capacity becomes `nthreads`.
5. Without `npus_per_core`:
   - missing `nthreads` is not expanded merely because `smt=true`;
   - an explicit `nthreads` must equal `ncpus`;
   - application thread capacity remains `ncpus`.
6. `mpiprocs` defaults to `ncpus`, must be positive, and must not exceed `ncpus`.
7. `ompthreads` defaults to 1 and must be positive.
8. `mpiprocs * ompthreads` must not exceed the calculated application capacity.

Select chunk multipliers and chunk ordering are preserved.

### Operating-system guard

The implementation contains a compatibility guard for old Debian releases: an explicitly requested `os=debianN` with a major release below 10 is rejected.

### Preserving the original request

Before the first modification, the hook stores the original `Resource_List.select` in `Resource_List.user_select` when `user_select` is unset or empty. If an earlier normalization hook has already stored it, the value is left unchanged.

This permits multiple queuejob normalization hooks to transform `select` while retaining the user's initial expression.

### PBS resources

The supplied `.qmgr` file defines:

| Resource | Type | Flags | Meaning |
| --- | --- | --- | --- |
| `user_select` | `string` | none | Metadata containing the original select expression before normalization. |

`user_select` is not a scheduling resource and should **not** be added to the scheduler's `resources` list.

The hook consumes or writes the following standard/custom values inside `Resource_List.select`:

| Resource | Role |
| --- | --- |
| `ncpus` | Physical cores allocated/requested. |
| `nthreads` | Logical CPU count when explicitly established. Defined by CPU discovery setup. |
| `smt` | Enables exposure of SMT siblings at execution. Defined by CPU discovery setup. |
| `npus_per_core` | Explicit logical processing units per physical core. Defined by CPU discovery setup. |
| `mpiprocs` | MPI processes per chunk. |
| `ompthreads` | OpenMP/application threads per MPI process. |
| `os` | Optionally checked by the Debian compatibility guard. |

### Interaction with `hook_job_cgroups_v2`

This queuejob hook validates the arithmetic and makes predictable SMT requests explicit; it does not allocate CPUs itself. At execution time `hook_job_cgroups_v2` selects physical cores and exposes either one logical CPU per core or, for SMT-enabled jobs, the actual online sibling CPUs. For hybrid/asymmetric topologies the final `resources_used.nthreads` can therefore reflect hardware reality rather than a simplistic fixed multiplier.
