# `hook_workspace`

## Overview

`hook_workspace` discovers configured scratch/workspace backends, publishes their availability as vnode resources, validates scratch requests during submission, creates a per-job workspace on the selected backend, exports workspace environment variables, and removes the workspace at job completion according to the backend policy.

The hook supports three resource classes in the supplied configuration:

- `scratch_local`: node-local disk scratch with size reservation;
- `scratch_shared`: shared-filesystem scratch with size reservation;
- `scratch_shm`: tmpfs/shared-memory workspace selected as a boolean capability.

Backends can have persistent subtypes such as NVMe, SSD, HDD, NFS, Lustre, Ceph, GPFS, or tmpfs.

## User documentation

### Requesting local scratch

Request local scratch as part of a select chunk:

```bash
#PBS -l select=1:ncpus=8:scratch_local=100gb
```

Inside the job, use the workspace path supplied by the hook rather than hard-coding `/scratch`:

```bash
cd "$SCRATCHDIR"
```

`$SCRATCH` is also set to the same path.

### Requesting a particular local subtype

When a particular backend class is required, constrain the corresponding subtype resource as well:

```bash
#PBS -l select=1:ncpus=8:scratch_local=100gb:scratch_local_subtype=nvme
```

The subtype describes the backend selected for that vnode; it is not chosen independently for every job.

### Shared scratch

Shared scratch can be requested with:

```bash
#PBS -l select=2:ncpus=16:scratch_shared=200gb
```

The configured shared resource can add an appropriate PBS placement group so that chunks requiring that workspace are scheduled consistently for the shared backend.

### Shared-memory scratch

The tmpfs workspace is requested as a boolean:

```bash
#PBS -l select=1:ncpus=8:scratch_shm=true
```

It does not reserve a size through `scratch_shm`; applications must still respect the memory limits of the job and the underlying tmpfs/filesystem.

### One workspace type per chunk

A select chunk may request at most one top-level scratch resource (`scratch_local`, `scratch_shared`, or `scratch_shm`). Requests for more than one are rejected because the hook exports one active workspace path per local job allocation.

### Environment variables

For a requested workspace the hook exports:

| Variable | Meaning |
| --- | --- |
| `SCRATCHDIR`, `SCRATCH` | Path of the job workspace on the local host/backend. |
| `SCRATCH_RESOURCE` | Selected top-level PBS scratch resource, or `none` if no scratch was requested. |
| `SCRATCH_SUBTYPE` | Selected backend subtype. |
| `SCRATCH_TYPE` | Backend kind: `local`, `shared`, `shm`, or `none`. |
| `SCRATCH_VOLUME` | Locally allocated scratch size in bytes for size resources. |
| `PBS_RESC_SCRATCH_VOLUME` | Local requested scratch size in bytes. |
| `PBS_RESC_TOTAL_SCRATCH_VOLUME` | Total requested scratch size across the job. |

Torque-compatible counterparts are also set. For a size resource, resource-specific variables such as `PBS_RESC_SCRATCH_LOCAL` are exported for the local allocation.

When no scratch is requested, the hook still provides a fallback job directory under the configured fallback path and reports `SCRATCH_RESOURCE=none`.

### Cleanup behavior

Local and shared disk backends in the supplied configuration use `preserve_nonempty=true`: the hook removes an empty job directory but deliberately leaves a non-empty directory in place. Users should therefore copy required results back to permanent storage and clean scratch data according to site policy.

The supplied tmpfs backend uses `preserve_nonempty=false` and is recursively removed at cleanup.

Scratch size reservation is a scheduling/allocation mechanism, not a filesystem quota. Applications can still fail if the underlying filesystem runs out of space or if external usage changes unexpectedly.

## Technical and administration documentation

### Hook events

The supplied `hook_workspace.qmgr` installs the hook for:

- `queuejob`
- `modifyjob`
- `exechost_startup`
- `exechost_periodic`
- `execjob_begin`
- `execjob_end`
- `execjob_abort`
- `execjob_resize`

The periodic interval is 600 seconds and the hook order is 30.

### Configuration model

Workspace resources are defined by top-level JSON keys beginning with `scratch_`. Each resource describes its PBS type, storage kind, discovery/capacity policy, optional placement behavior, backend subtypes, job-directory template, accounting root, and cleanup policy.

The supplied configuration also defines:

```json
{
    "state_subdir": "workspace",
    "scan_refresh": 7200,
    "fallback_dir": "/var/tmp/pbs.$PBS_JOBID"
}
```

| Field | Description |
| --- | --- |
| `state_subdir` | Mom-private state directory used by workspace discovery/allocation. |
| `scan_refresh` | Refresh interval for filesystem scans used by managed-capacity backends. |
| `fallback_dir` | Per-job directory template used when no scratch resource is requested. |

### Backend subtypes

Each scratch resource contains ordered/priority subtypes with matching rules. The supplied configuration includes:

| Resource | Subtypes | Mount point |
| --- | --- | --- |
| `scratch_local` | `nvme`, `ssd`, `hdd` | `/scratch` |
| `scratch_shared` | `nfs`, `lustre`, `ceph`, `gpfs` | `/scratch.shared` |
| `scratch_shm` | `tmpfs` | `/dev/shm` |

Subtype probes can match mount source names, filesystem types, and for local block devices rotational characteristics. For example, the local NVMe/SSD rules require non-rotational backing devices, while HDD requires rotational media.

Candidate subtypes are ordered primarily by configured priority and then deterministically by name. Once a subtype has been selected for a resource on a node, the selection is persisted. If that backend temporarily becomes unusable, the hook retains the selected subtype and publishes zero/false capacity rather than silently failing over to a different storage class.

### Capacity modes

The implementation supports two principal size-capacity policies used by the supplied configuration.

#### `managed`

Used for `scratch_local`. Available capacity is derived from filesystem state while accounting for PBS reservations and unmanaged filesystem usage. Periodic scans refresh the accounting information. This allows the hook to advertise a consumable size resource representing currently reservable scratch space.

#### `filesystem_total`

Used for `scratch_shared`. Capacity is based on the configured filesystem's total size rather than scanning current free/unmanaged usage. This is appropriate when the PBS resource is intended as a placement/reservation abstraction for a shared filesystem rather than a direct free-space measurement.

Boolean `scratch_shm` simply advertises whether the selected tmpfs backend is usable.

### Submission-time validation

On `queuejob`/`modifyjob`, the hook parses `Resource_List.select` and validates scratch requests:

- only configured `scratch_*` resources and their subtype resources are accepted;
- a boolean scratch resource must be requested as true;
- size requests must be positive;
- at most one top-level scratch resource is allowed per chunk;
- incompatible placement-group requirements are rejected.

For resources configured with `place_group`, the hook can add a missing PBS `place=group=...` constraint. The supplied `scratch_shared` resource uses the group name `cluster`.

### Execution-time workspace creation

At `execjob_begin`, the hook determines the scratch resource assigned to the local host from `exec_vnode`. For a size resource it verifies that the requested amount can still be reserved, constructs the configured job directory, and stores state for later cleanup/accounting.

The supplied directory templates are:

```text
/scratch/$USER/batch_system_jobs/job_$PBS_JOBID_SHORT
/scratch.shared/$USER/batch_system_jobs/job_$PBS_JOBID_SHORT
/dev/shm/$USER/batch_system_jobs/job_$PBS_JOBID_SHORT
```

Templates support the hook's documented substitutions including user, hostname, full PBS job ID, and short job ID. The implementation verifies that expanded job paths remain below the configured mount point.

For shared storage, directory creation and destructive cleanup are coordinated so that the mother superior performs the shared operation rather than every sister independently.

### PBS resources

The supplied `.qmgr` file defines:

| Resource | Type | Flags | Meaning |
| --- | --- | --- | --- |
| `scratch_local` | `size` | `hn` | Consumable node-local scratch reservation. |
| `scratch_shared` | `size` | `hn` | Consumable shared scratch reservation/placement resource. |
| `scratch_shm` | `boolean` | `h` | Availability/request for tmpfs workspace. |
| `scratch_local_subtype` | `string` | `h` | Persistent selected local backend subtype. |
| `scratch_shared_subtype` | `string` | `h` | Persistent selected shared backend subtype. |
| `scratch_shm_subtype` | `string` | `h` | Persistent selected shared-memory backend subtype. |

### Cleanup and resize

`execjob_end` and `execjob_abort` clean workspace state/directories according to each resource's `preserve_nonempty` policy. Shared cleanup is coordinated through the mother superior. Dynamic workspace resizing is not supported; `execjob_resize` is rejected.

### Administration notes

Before enabling a backend, administrators should verify that the configured mount point, source/filesystem matching rules, directory ownership/permissions, capacity mode, and cleanup policy match the actual storage semantics. In particular, `managed` reservation is not a quota mechanism, and `filesystem_total` intentionally does not represent instantaneous free space.
