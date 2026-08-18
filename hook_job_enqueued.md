# `hook_job_enqueued`

## 1. Overview

`hook_job_enqueued` is an OpenPBS `queuejob` hook that normalises CPU-related `select` chunks and validates MPI/OpenMP process layout before a job enters the queue.

The hook follows the CPU model used by the companion CPU discovery and cgroup-v2 hooks:

- `ncpus` is the number of **physical CPU cores** requested by the chunk;
- `smt=true` requests SMT-capable execution and may be used independently, including on hybrid CPUs where the number of logical PUs per physical core is not uniform;
- `npus_per_core` is an exact-match **string vnode property** for homogeneous CPU topology;
- `nthreads` is a numeric, scheduler-consumable resource used when the request explicitly selects homogeneous SMT topology with `npus_per_core`;
- `mpiprocs` and `ompthreads` describe application-level MPI and OpenMP parallelisation and are not themselves descriptions of hardware SMT topology.

The original hook used regular expressions to infer missing MPI/OpenMP values and checked only `mpiprocs * ompthreads <= ncpus`. The refactored hook parses every `select` chunk, validates malformed values explicitly, and permits application parallelism up to `nthreads` only when the homogeneous-SMT request makes that capacity deterministic. The original rejection of legacy `nodes=` syntax and the Debian `< 10` guard are retained.

## 2. User documentation

### Resources

| Resource | Type | Meaning in this hook |
|---|---|---|
| `ncpus` | integer | Number of physical CPU cores. Missing value is added as `ncpus=1`. Must be `>= 1`. |
| `smt` | boolean | Requests SMT-capable execution. It may be specified without `npus_per_core`; this is important for hybrid/irregular SMT CPUs. |
| `npus_per_core` | string vnode property, integer syntax in requests | Exact number of PUs per physical core on homogeneous hardware. If present, the hook enforces `smt=true` and full `nthreads=ncpus*npus_per_core`. |
| `nthreads` | long, consumable | Number of scheduler-consumed logical PUs. In the homogeneous-SMT mode it must equal `ncpus*npus_per_core`. |
| `mpiprocs` | integer | MPI ranks/processes per chunk. Defaults to `ncpus`. Must satisfy `1 <= mpiprocs <= ncpus`. |
| `ompthreads` | integer | OpenMP threads per MPI process. Defaults to `1`. |

### Normal CPU request

```bash
#PBS -l select=1:ncpus=8
```

is normalised to:

```text
select=1:ncpus=8:mpiprocs=8:ompthreads=1
```

The queue hook assumes only eight schedulable PUs for MPI/OpenMP validation. A later cgroup hook may expose additional SMT siblings according to the actual vnode topology, but their use is then under the control of the job script.

### Generic SMT request

```bash
#PBS -l select=1:ncpus=8:smt=true
```

is normalised to:

```text
select=1:ncpus=8:smt=true:nthreads=8:mpiprocs=8:ompthreads=1
```

The hook adds `nthreads=ncpus` as the deterministic scheduler-visible baseline. This mode is suitable for hybrid or otherwise irregular CPUs. The later cgroup-v2 hook may expose additional SMT siblings locally, but those topology-dependent extra PUs are not included in the queue-time `nthreads` value.

### Homogeneous SMT request

```bash
#PBS -l select=1:ncpus=8:npus_per_core=2
```

is normalised to:

```text
select=1:ncpus=8:npus_per_core=2:smt=true:nthreads=16:mpiprocs=8:ompthreads=1
```

`npus_per_core=2` is an exact string match against the vnode property. Because the topology is now deterministic, the hook makes the scheduler consume all logical PUs by adding:

```text
nthreads = ncpus * npus_per_core
```

If the user already supplies `smt`, it must not be false. If the user already supplies `nthreads`, it must exactly equal the calculated product.

### Job-wide SMT consistency

The cgroup-v2 execution hook implements `smt` as one job-wide cpuset policy. Therefore, after per-chunk normalisation, all chunks that explicitly contain `smt` must agree. Chunks that omit `smt` are allowed and inherit the job-wide result.

This is rejected at queue time:

```bash
#PBS -l select=1:ncpus=8:smt=true+1:ncpus=8:smt=false
```

Moving this check to `queuejob` prevents a schedulable job from being accepted into the queue only to fail later at `execjob_begin`.

### MPI/OpenMP examples

A non-SMT mixed MPI/OpenMP layout is valid:

```bash
#PBS -l select=1:ncpus=8:mpiprocs=4:ompthreads=2
```

because:

```text
4 * 2 = 8 <= ncpus
```

A homogeneous 2-way SMT layout may use all 16 explicitly scheduled PUs:

```bash
#PBS -l select=1:ncpus=8:npus_per_core=2:mpiprocs=4:ompthreads=4
```

The hook adds `smt=true:nthreads=16`, and the request is valid because:

```text
4 * 4 = 16 <= nthreads
```

The following request is rejected:

```bash
#PBS -l select=1:ncpus=8:mpiprocs=8:ompthreads=2
```

because `npus_per_core` is absent and therefore only `ncpus=8` is available to the queue-time MPI/OpenMP validation.

### Validation rules

For every chunk:

1. Missing `ncpus` is added as `ncpus=1`.
2. `ncpus`, `mpiprocs`, `ompthreads`, and any explicit `nthreads` must be positive integers.
3. `npus_per_core`, although defined as a PBS string resource, must contain a positive integer in a request. It is normalised to its canonical decimal form so that exact string matching is reliable.
4. `smt`, if supplied, must be a recognised boolean value.
5. If `npus_per_core` is present:
   - explicit `smt=false` is rejected;
   - missing `smt` is added as `smt=true`;
   - missing `nthreads` is added as `ncpus*npus_per_core`;
   - explicit `nthreads` must equal `ncpus*npus_per_core`.
6. If `npus_per_core` is absent:
   - `smt=true` remains valid;
   - missing `nthreads` is added as `nthreads=ncpus`;
   - an explicitly supplied `nthreads` is accepted only when it equals `ncpus`.
7. Across the whole `select`, explicitly supplied/normalised `smt` values must be consistent. Omitted `smt` values do not conflict.
8. Missing `mpiprocs` is set to `ncpus`.
9. Missing `ompthreads` is set to `1`.
10. `mpiprocs` must satisfy:

   ```text
   1 <= mpiprocs <= ncpus
   ```

11. Without `npus_per_core`:

    ```text
    mpiprocs * ompthreads <= ncpus
    ```

12. With `npus_per_core`:

    ```text
    mpiprocs * ompthreads <= nthreads
    ```

### Examples of rejected requests

```text
select=1:ncpus=0
```

`ncpus` must be at least one.

```text
select=1:ncpus=8:npus_per_core=2:smt=false
```

`npus_per_core` requires `smt=true`.

```text
select=1:ncpus=8:npus_per_core=2:nthreads=12
```

The required value is `8 * 2 = 16`.

```text
select=1:ncpus=8:smt=true:nthreads=16
```

This is rejected because expanded consumable `nthreads` requires an explicit homogeneous topology through `npus_per_core`. For topology-dependent SMT, request only `smt=true` and let the later cgroup hook determine the actual PU count.

```text
select=1:ncpus=8:mpiprocs=9
```

`mpiprocs` cannot exceed the number of physical cores.

## 3. Technical documentation

### Event

The hook runs on:

```text
queuejob
```

It rewrites `job.Resource_List["select"]` with a new `pbs.select` object after all chunks have passed validation.

If no explicit `select` resource is present, the hook accepts the event without constructing one. Legacy `Resource_List.nodes` syntax is rejected.

### Select parser

The implementation uses a small chunk parser rather than regular expressions for individual resources. A select request is split into `+`-separated chunks, and each chunk is split into colon-separated fields.

The parser supports the standard optional leading chunk multiplicity, for example:

```text
2:ncpus=8:mem=16gb
```

Existing resource order is preserved. Resources added by the hook are appended. Duplicate resource names inside one chunk, malformed fields, zero/negative integer values, and malformed booleans are rejected with a chunk-specific message.

Numeric values handled by this hook are re-rendered in canonical decimal form. This is particularly important for `npus_per_core`, because PBS treats it as a string resource and scheduler matching is exact.

### Homogeneous-SMT mode

The presence of `npus_per_core` is the switch that tells this hook that uniform topology can be used for scheduler accounting:

```text
npus_per_core present
    => smt=true
    => nthreads=ncpus*npus_per_core
```

This invariant ensures that `nthreads` consumption and the later cgroup allocation represent the same number of logical PUs for well-behaved homogeneous hardware.

### Generic/hybrid SMT mode

`smt=true` without `npus_per_core` is deliberately valid. The hook does not infer a PU/core ratio; it sets `nthreads=ncpus` as the scheduler-visible baseline. The later cgroup hook is responsible for inspecting the selected physical cores and may expose additional SMT siblings locally.

This separation is necessary for hybrid CPUs where different physical cores may have different numbers of logical PUs.

### MPI/OpenMP semantics

`mpiprocs` and `ompthreads` are treated as application launch geometry. In particular, `ompthreads` is **not** interpreted as a count of SMT siblings.

Therefore this valid request:

```text
ncpus=8:mpiprocs=4:ompthreads=2
```

uses eight application threads on eight physical-core PUs even with SMT disabled.

Conversely, a job may request `smt=true` while keeping:

```text
mpiprocs=8:ompthreads=1
```

because additional logical PUs exposed by the cgroup may still be useful to MPI runtime/helper threads or may be used explicitly by the job script.

### Retained behaviour from the original hook

The refactoring preserves two unrelated checks present in the supplied hook:

- legacy `nodes=` requests are rejected in favour of `select` syntax;
- an `os=debianN` request is rejected when `N < 10`.

The old automatic calculation based on `ceil(ncpus/mpiprocs)` or `ceil(ncpus/ompthreads)` is intentionally removed. Missing values now have deterministic defaults: `mpiprocs=ncpus` and `ompthreads=1`.

### PBS resource configuration

The supplied qmgr setup defines:

```qmgr
create resource nthreads
set resource nthreads type = long
set resource nthreads flag = hn

create resource smt
set resource smt type = boolean
set resource smt flag = h

create resource npus_per_core
set resource npus_per_core type = string
set resource npus_per_core flag = h
```

The scheduler must also be configured to know about `nthreads`, `smt`, and `npus_per_core` in `$PBS_HOME/sched_priv/sched_config`; otherwise custom-resource matching/accounting may not be performed as intended.

### Hook configuration

The supplied qmgr file creates a site hook with:

| Setting | Value |
|---|---|
| name | `job_enqueued` |
| type | `site` |
| event | `queuejob` |
| enabled | `true` |
| user | `pbsadmin` |
| alarm | `30` s |
| order | `10` |
| debug | `false` |
| fail_action | `none` |

There is no JSON configuration file.

### Limitations

- Expanded `nthreads > ncpus` accounting is intentionally supported only for homogeneous topology explicitly selected with `npus_per_core`. Otherwise the hook materializes `nthreads=ncpus`.
- `smt=true` alone cannot predict queue-time logical-PU capacity; this is deferred to the cgroup hook.
- SMT is job-wide in the execution architecture; contradictory explicit SMT values across chunks are rejected here at queue time.
- The hook operates only on an explicit `Resource_List.select`; it does not synthesise a select expression from top-level resource requests.
- The parser assumes that select resource values used at this site do not contain literal `:` or `+` separators.
