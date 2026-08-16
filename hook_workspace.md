# `hook_workspace`

## 1. Overview

`hook_workspace` provides per-job scratch/workspace directories backed by local HDD, SSD or NVMe storage, shared storage, or `/dev/shm`/tmpfs. It validates user scratch requests, discovers and publishes reservable scratch capacity, chooses an appropriate backend, creates the job directory, exports compatibility environment variables, tracks reservations, and removes or preserves the workspace according to backend policy when the job ends.

The requested scratch size is a **scheduler reservation/accounting value**. The hook does **not** create filesystem quotas and therefore does not enforce the requested byte count as a hard storage limit.

## 2. User documentation

### User-facing resources

| Resource | Type | Meaning |
|---|---|---|
| `scratch_local` | size | Logical local scratch. The hook chooses one configured local subtype able to satisfy the complete request. |
| `scratch_hdd` | size | Explicitly request the HDD local backend. |
| `scratch_ssd` | size | Explicitly request the SSD local backend. |
| `scratch_nvme` | size | Explicitly request the NVMe local backend. |
| `scratch_shared` | size | Shared scratch/workspace backend. |
| `scratch_shm` | boolean | tmpfs/shmem workspace. Capacity is controlled by the job's memory/cgroup allocation rather than by a scratch byte reservation. |

### Example requests

Request 100 GiB of the best configured local scratch backend:

```bash
#PBS -l select=1:ncpus=8:mem=16gb:scratch_local=100gb
```

Request specifically NVMe scratch:

```bash
#PBS -l select=1:ncpus=8:mem=16gb:scratch_nvme=100gb
```

Request shared scratch:

```bash
#PBS -l select=1:ncpus=8:mem=16gb:scratch_shared=200gb
```

Request a shared-memory workspace:

```bash
#PBS -l select=1:ncpus=8:mem=16gb:scratch_shm=true
```

### Request restrictions

- Scratch resources must be requested in the `select` syntax handled by the hook.
- **At most one `scratch_*` resource is allowed per select chunk.**
- Unsupported resource names beginning with `scratch_` are rejected early at queue/modify time.
- `scratch_shm` is boolean and must be requested as `scratch_shm=true`; it is not a size resource.
- All other scratch resources are positive sizes.
- Dynamic scratch resizing is unsupported; `execjob_resize` is rejected.
- A `scratch_local` request must fit entirely on **one** concrete local backend. Capacities from multiple local filesystems are never added together to satisfy one request.

When `scratch_shared` is present, the queue/modify hook also ensures the PBS place specification contains `group=cluster` if no `group=` component is already present.

### Environment variables

For normal local/shared scratch, the hook sets:

| Variable | Meaning |
|---|---|
| `SCRATCHDIR` | Job workspace path. |
| `SCRATCH` | Alias of `SCRATCHDIR`. |
| `SINGULARITY_TMPDIR` | Set to the workspace path. |
| `SINGULARITY_CACHEDIR` | Set to the workspace path. |
| `SCRATCH_VOLUME` | Locally requested scratch size in **bytes**. |
| `PBS_RESC_SCRATCH_VOLUME` | Local requested size in bytes. |
| `TORQUE_RESC_SCRATCH_VOLUME` | Torque-compatible alias. |
| `PBS_RESC_TOTAL_SCRATCH_VOLUME` | Total request for the same scratch resource across all execution chunks, in bytes. |
| `TORQUE_RESC_TOTAL_SCRATCH_VOLUME` | Torque-compatible alias. |
| `SCRATCH_RESOURCE` | Resource requested by the user, e.g. `scratch_local`, `scratch_nvme`, `scratch_shared`, or `none`. |
| `SCRATCH_SUBTYPE` | Actual chosen backend, e.g. `scratch_nvme`; for explicit/shared/shm requests it reflects that backend. |
| `SCRATCH_TYPE` | `local`, `shared`, `shm`, or `none`. |
| `PBS_RESC_<RESOURCE>` | Local requested byte count for the selected resource. |
| `TORQUE_RESC_<RESOURCE>` | Torque-compatible alias. |

For an explicit local subtype (`scratch_hdd`, `scratch_ssd`, `scratch_nvme`), the hook additionally sets `PBS_RESC_SCRATCH_LOCAL` and `TORQUE_RESC_SCRATCH_LOCAL` as compatibility aliases.

For `scratch_shm`, size variables are set to zero because no independent scratch capacity is reserved; usable capacity is governed by the job's memory allocation/cgroup.

If no scratch resource is requested, no workspace directory is created by the hook, but the scratch path variables are pointed at the configured `fallback_dir`, with `SCRATCH_TYPE=none` and zero sizes.

## 3. Technical documentation

### Events

The implementation handles:

- `queuejob`, `modifyjob`: validate scratch syntax and supported resources;
- `exechost_startup`: probe/publish backend capacities;
- `exechost_periodic`: clean stale reservation state and refresh publication;
- `execjob_begin`: validate the local assigned request, choose/probe a backend, create state/directory, set environment;
- `execjob_end`, `execjob_abort`: cleanup workspace according to preservation policy;
- `execjob_resize`: reject dynamic resizing.

### Local backend selection

Configured local subtypes are independently probed. For a logical `scratch_local` request, usable backends with sufficient `reservable` capacity are sorted by descending `priority`, then by backend name. The highest-priority candidate is selected.

An explicit `scratch_hdd`, `scratch_ssd`, or `scratch_nvme` request must be satisfied by that exact configured backend.

### Backend probing

A backend is usable only if its configured `mount_point` is an actual mount point and satisfies configured properties:

- mounted source or resolved leaf block device matches `source_patterns`;
- filesystem type matches `filesystem_patterns`;
- when `rotational` is specified, backing device rotational status matches it.

The hook resolves leaf block devices for stacked device-mapper/LVM/MD paths, allowing a mount such as `/dev/mapper/...` to be classified by its physical backing storage.

Available filesystem bytes are taken from `statvfs(...).f_bavail`. The reservation logic also accounts for known live job reservations and for unmanaged data found under the configured accounting root. Published capacity is constructed so PBS can subtract reservations made against the same resource without double-counting the hook's own reservation state.

For logical `scratch_local`, published free capacity is the **maximum** reservable capacity of any local subtype, never their sum.

### Workspace creation and reruns

`job_dir` is a template expanded using:

```text
$PBS_JOBID / ${PBS_JOBID}
$PBS_JOBID_SHORT / ${PBS_JOBID_SHORT}
$USER / ${USER}
$HOST / ${HOST}
```

The expanded path must remain under the backend `mount_point`.

If the job directory already exists, the hook creates a rerun backup directory named from `rerun_prefix` (default `.run_count`) plus the job run count and moves existing non-rerun entries into it. Permissions/ownership are set for the job identity.

At job end:

- `preserve_nonempty=true`: remove the directory only if it is empty; otherwise preserve it;
- `preserve_nonempty=false`: recursively remove it.

Shared scratch cleanup is performed only by the mother-superior MoM, avoiding multiple sister MoMs deleting the same shared path.

### Reservation state and unmanaged-data scanning

State is stored under PBS MoM private storage using `state_subdir`. A cache is used to avoid repeatedly scanning potentially large accounting trees. `scan_refresh` controls how long cached unmanaged-space data remains valid; a backend may override the global value.

`accounting_root` defines the directory tree whose unmanaged content is subtracted from total workspace capacity. If omitted, it defaults to the backend `mount_point`.

### JSON configuration: top level

| Item | Type | Default | Description |
|---|---:|---:|---|
| `state_subdir` | string | `workspace` | State/cache directory below `PBS_MOM_HOME/mom_priv/`. |
| `scan_refresh` | integer seconds | `7200` | Lifetime of cached unmanaged-space scans. Can be overridden per backend. |
| `fallback_dir` | absolute/path template | `/var/tmp/pbs.$PBS_JOBID` | Path exported when no scratch resource is requested. The hook does not create it in that case. |
| `scratch_local` | object or `null` | object with discovery enabled and no subtypes | Configuration for logical local scratch and its concrete subtypes. `null` disables local discovery/configuration. |
| `scratch_shared` | backend object or `null` | `null` | Shared scratch backend. |
| `scratch_shm` | backend object or `null` | `null` | Shared-memory/tmpfs backend. |

### JSON configuration: `scratch_local`

| Item | Type | Default | Description |
|---|---:|---:|---|
| `scratch_local.discover` | boolean | `true` | Publish `resources_available.scratch_local`. If false, the hook leaves that vnode resource unchanged but still validates/uses configured backends at job begin. |
| `scratch_local.subtypes` | array | `[]` | Local backend objects. Each must be named `scratch_hdd`, `scratch_ssd`, or `scratch_nvme`; duplicate names are rejected. |

### JSON configuration: backend items

The following fields apply to local subtype backends and, where relevant, to `scratch_shared`/`scratch_shm`.

| Item | Required | Description |
|---|---|---|
| `name` | Local subtype only | Must be one of `scratch_hdd`, `scratch_ssd`, `scratch_nvme`. |
| `priority` | Local subtype only | Integer selection priority for logical `scratch_local`; larger values are preferred. |
| `discover` | No; default true when queried | Whether this hook publishes `resources_available.<resource>`. Disabling discovery does **not** disable begin-time backend validation/use. |
| `mount_point` | Yes | Absolute path that must correspond to an actual mounted filesystem. |
| `job_dir` | Yes | Absolute path template for per-job directories; the expanded path must remain under `mount_point`. |
| `source_patterns` | No; effectively `[*]` | Non-empty list of shell globs matched against the mounted source and/or resolved backing leaf devices. |
| `filesystem_patterns` | No; effectively `[*]` | Non-empty list of shell globs matched against the mounted filesystem type. |
| `rotational` | No; `null` | `true` requires rotational media, `false` requires non-rotational media, `null` skips this check. |
| `accounting_root` | No | Root used when scanning unmanaged workspace data; defaults to `mount_point`. |
| `scan_refresh` | No | Per-backend override of the global unmanaged-scan cache lifetime. |
| `preserve_nonempty` | No | Cleanup policy. The implementation defaults to true for local/shared state and false for shm state when storing job state. |
| `rerun_prefix` | No; `.run_count` | Prefix used for backup directories when a job workspace already exists at begin/rerun. |

The supplied JSON config uses `/scratch` for local storage, prioritises NVMe (`300`) over SSD (`200`) over HDD (`100`), uses `/scratch.shared` for shared storage, and `/dev/shm` for shared-memory workspaces.

### Limitations and design notes

- Scratch sizes are reservations, not enforced quotas. A job can fill more filesystem space than requested unless the underlying filesystem/site policy enforces quotas independently.
- Capacity accounting depends on filesystem free-space snapshots, persistent hook state, and periodic unmanaged-data scans; it is therefore conservative/accounting-oriented rather than a transactional filesystem quota system.
- Backend source/rotational classification depends on Linux mount/sysfs information and may reject storage whose backing-device properties cannot be determined when a rotational constraint is configured.
- Only the fixed local subtype names `scratch_hdd`, `scratch_ssd`, and `scratch_nvme` are accepted by this implementation.
- `scratch_shm` has no independent byte reservation; users should request sufficient `mem`, and the cgroup memory limit determines practical capacity.
- Dynamic resizing is deliberately unsupported.
