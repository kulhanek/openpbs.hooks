# `hook_workspace`

## 1. Overview

`hook_workspace` provides per-job workspace directories using three top-level scratch classes:

- `scratch_local` — node-local disk/SSD/NVMe workspace, expressed as a consumable size;
- `scratch_shared` — shared-filesystem workspace, expressed as a consumable size;
- `scratch_shm` — tmpfs/shared-memory workspace, expressed as a boolean.

Storage technologies are represented by **subtypes**, not by separate consumable scratch resources. During node discovery the hook chooses one configured subtype for each top-level scratch class, persists that choice, and publishes it through `resources_available.<resource>_subtype`. For example, a node may publish:

```text
resources_available.scratch_local = 850gb
resources_available.scratch_local_subtype = nvme
resources_available.scratch_shared = 5tb
resources_available.scratch_shared_subtype = nfs
resources_available.scratch_shm = True
resources_available.scratch_shm_subtype = tmpfs
```

The requested scratch size is a scheduler reservation/accounting value. The hook does **not** create a filesystem quota, so it does not enforce the requested byte count as a hard storage limit.

## 2. User documentation

### User-facing resources

| Resource | Type | Meaning |
|---|---|---|
| `scratch_local` | size, consumable | Node-local workspace using the node's persistently selected local subtype. |
| `scratch_shared` | size, consumable | Shared workspace using the node's persistently selected shared subtype. |
| `scratch_shm` | boolean | tmpfs/shared-memory workspace. Capacity is controlled by the job's memory/cgroup allocation. |
| `scratch_local_subtype` | string | Scheduler-selectable property describing the selected local backend, e.g. `nvme`, `ssd`, or `hdd`. |
| `scratch_shared_subtype` | string | Scheduler-selectable property describing the selected shared backend, e.g. `nfs`, `lustre`, `ceph`, or `gpfs`. |
| `scratch_shm_subtype` | string | Scheduler-selectable property describing the selected shared-memory backend, normally `tmpfs`. |

The subtype resources are **not consumable capacities**. They are node properties and may be used to constrain scheduling.

### Example requests

Request 100 GiB of local scratch regardless of its subtype:

```bash
#PBS -l select=1:ncpus=8:mem=16gb:scratch_local=100gb
```

Request 100 GiB of local scratch specifically on a node whose selected subtype is NVMe:

```bash
#PBS -l select=1:ncpus=8:mem=16gb:scratch_local=100gb:scratch_local_subtype=nvme
```

Request shared scratch on Lustre:

```bash
#PBS -l select=1:ncpus=8:mem=16gb:scratch_shared=200gb:scratch_shared_subtype=lustre
```

Request a shared-memory workspace:

```bash
#PBS -l select=1:ncpus=8:mem=16gb:scratch_shm=true
```

A subtype property can also be used by itself as a general node-selection constraint; it does not imply a scratch allocation.

### Request restrictions

- Scratch resources must be requested through `select` chunks handled by the hook.
- At most one **top-level** scratch resource (`scratch_local`, `scratch_shared`, or `scratch_shm`) is allowed per select chunk.
- `scratch_shm` is boolean and must be requested as a true value.
- `scratch_local` and `scratch_shared` are positive size resources.
- Former resources such as `scratch_nvme`, `scratch_ssd`, and `scratch_hdd` are no longer supported. Use `scratch_local=<size>:scratch_local_subtype=<subtype>` when a particular local storage technology is required.
- Dynamic scratch resizing is unsupported; `execjob_resize` is rejected.
- When `scratch_shared` is requested, the queue/modify hook adds `group=cluster` to `place` if no `group=` component is already present.

### Environment variables

For a requested workspace the hook sets:

| Variable | Meaning |
|---|---|
| `SCRATCHDIR` | Job workspace path. |
| `SCRATCH` | Alias of `SCRATCHDIR`. |
| `SINGULARITY_TMPDIR` | Set to the workspace path. |
| `SINGULARITY_CACHEDIR` | Set to the workspace path. |
| `SCRATCH_RESOURCE` | Top-level requested resource: `scratch_local`, `scratch_shared`, `scratch_shm`, or `none`. |
| `SCRATCH_TYPE` | `local`, `shared`, `shm`, or `none`. |
| `SCRATCH_SUBTYPE` | Persistently selected backend subtype, e.g. `nvme`, `nfs`, or `tmpfs`. |
| `SCRATCH_VOLUME` | Locally assigned scratch size in bytes; zero for `scratch_shm`. |
| `PBS_RESC_SCRATCH_VOLUME` | Local requested size in bytes. |
| `TORQUE_RESC_SCRATCH_VOLUME` | Torque-compatible alias. |
| `PBS_RESC_TOTAL_SCRATCH_VOLUME` | Sum of the same top-level size resource over all execution chunks, in bytes. |
| `TORQUE_RESC_TOTAL_SCRATCH_VOLUME` | Torque-compatible alias. |
| `PBS_RESC_SCRATCH_LOCAL` | Local `scratch_local` request in bytes, when applicable. |
| `PBS_RESC_SCRATCH_SHARED` | Local `scratch_shared` request in bytes, when applicable. |
| `PBS_RESC_SCRATCH_SHM` | Zero for `scratch_shm`; the resource itself is boolean. |
| `TORQUE_RESC_<RESOURCE>` | Torque-compatible counterpart of the top-level resource variable. |

If no scratch resource is requested, the hook does not create a workspace directory. Scratch path variables point to `fallback_dir`, `SCRATCH_RESOURCE=none`, `SCRATCH_TYPE=none`, `SCRATCH_SUBTYPE=none`, and size variables are zero.

## 3. Technical documentation

### Events

The hook handles:

- `queuejob`, `modifyjob` — validate scratch syntax/resources and add shared-scratch placement grouping;
- `exechost_startup` — select missing subtypes and publish scratch resources/properties;
- `exechost_periodic` — remove stale reservation state and refresh availability/capacity without changing existing subtype selections;
- `execjob_begin` — verify the selected backend, reserve capacity, create the workspace and set environment variables;
- `execjob_end`, `execjob_abort` — cleanup according to backend preservation policy;
- `execjob_resize` — reject dynamic scratch resizing.

### Persistent subtype selection

Each of `scratch_local`, `scratch_shared`, and `scratch_shm` contains an ordered-by-priority set of discovery candidates under `subtypes`.

If no subtype has yet been selected for a top-level resource, discovery probes all configured candidates that currently satisfy their mount/source/filesystem/rotational constraints. Usable candidates are sorted by descending `priority` and then by subtype name; the first candidate is selected.

The resulting mapping is stored in:

```text
$PBS_MOM_HOME/mom_priv/<state_subdir>/selected_subtypes.json
```

Once selected, a subtype remains selected while its name remains present in the configuration. Periodic discovery **does not fail over** to another subtype when the selected backend becomes temporarily unavailable. Instead:

- size resources publish no additional reservable capacity;
- `scratch_shm` publishes `false`;
- the corresponding `<resource>_subtype` property remains unchanged.

This prevents the semantic meaning of `scratch_local`, `scratch_shared`, or `scratch_shm` from changing underneath queued/running jobs.

A new automatic selection occurs when there is no persisted selection or when the persisted subtype name is no longer present in that resource's configuration. To force complete rediscovery manually, remove `selected_subtypes.json` while the MoM/hook is not running and then restart/reload discovery.

### Backend probing

A subtype is usable only if its configured `mount_point` is an actual mount point and satisfies configured properties:

- mounted source or resolved leaf block device matches `source_patterns`;
- filesystem type matches `filesystem_patterns`;
- when `rotational` is specified, backing-device rotational status matches it.

The hook resolves leaf block devices through device-mapper/LVM/MD stacks using sysfs. This allows a mount such as `/dev/mapper/...` to be classified by its underlying NVMe/SSD/HDD device.

Filesystem free bytes are obtained from `statvfs(...).f_bavail`.

### Reservation and unmanaged-data accounting

For size resources (`scratch_local` and `scratch_shared`), reservation state is associated with the top-level resource **and its selected subtype**. The hook tracks live PBS workspace reservations and optionally scans `accounting_root` for unmanaged allocated data.

Published capacity is constructed so PBS can subtract reservations made against the same top-level consumable resource without the hook double-counting its own reservation state.

Because one subtype is fixed per top-level resource, capacities from different subtypes are never summed or dynamically substituted.

`scratch_shm` performs no independent byte reservation. Its usable capacity is governed by the job's requested memory and the memory cgroup.

### Workspace creation and reruns

`job_dir` templates support:

```text
$PBS_JOBID / ${PBS_JOBID}
$PBS_JOBID_SHORT / ${PBS_JOBID_SHORT}
$USER / ${USER}
$HOST / ${HOST}
```

The expanded workspace path must remain under the subtype's `mount_point`.

If the job directory already exists, the hook creates a rerun backup directory using `rerun_prefix` (default `.run_count`) and the PBS job run count, then moves existing non-rerun entries into it.

At job end:

- `preserve_nonempty=true` removes the workspace only when empty; otherwise it is preserved;
- `preserve_nonempty=false` removes it recursively.

Shared scratch creation/state/cleanup is performed only by the mother-superior MoM so sister MoMs do not race over the same shared directory.

### JSON configuration: top level

| Item | Type | Default | Description |
|---|---:|---:|---|
| `state_subdir` | string | `workspace` | State/cache directory below `PBS_MOM_HOME/mom_priv/`. |
| `scan_refresh` | integer seconds | `7200` | Lifetime of cached unmanaged-space scans; can be overridden per subtype. |
| `fallback_dir` | path template | `/var/tmp/pbs.$PBS_JOBID` | Path exported when no scratch resource is requested. |
| `scratch_local` | object/null | enabled object | Local scratch class and discovery subtypes. |
| `scratch_shared` | object/null | enabled object | Shared scratch class and discovery subtypes. |
| `scratch_shm` | object/null | enabled object | Shared-memory scratch class and discovery subtypes. |

Setting a scratch-class section to `null` is equivalent to `discover=false` with no configured subtypes.

### JSON configuration: scratch-class object

The same structure is used for all three top-level resources:

| Item | Type | Description |
|---|---|---|
| `discover` | boolean | If true, the hook publishes `resources_available.<resource>` and `<resource>_subtype`. |
| `subtypes` | array | Candidate backend definitions. Discovery persistently selects one usable candidate. |

### JSON configuration: subtype object

| Item | Required | Description |
|---|---|---|
| `name` | Yes | Subtype value published to PBS, e.g. `nvme`, `ssd`, `hdd`, `nfs`, `lustre`, or `tmpfs`. It must not start with `scratch_`. |
| `priority` | Yes | Integer discovery priority; larger values are preferred. |
| `mount_point` | Yes | Absolute path that must be an actual mounted filesystem. |
| `job_dir` | Yes | Absolute per-job path template; expansion must remain below `mount_point`. |
| `source_patterns` | No | Non-empty shell-glob list matched against the mounted source and/or resolved leaf devices; default `[*]`. |
| `filesystem_patterns` | No | Non-empty shell-glob list matched against filesystem type; default `[*]`. |
| `rotational` | No | `true` requires rotational storage, `false` non-rotational storage, `null`/omitted disables the check. |
| `accounting_root` | No | Root scanned for unmanaged space; defaults to `mount_point`. Relevant to size resources. |
| `scan_refresh` | No | Per-subtype override of global unmanaged-scan cache lifetime. |
| `scan_enabled` | No | Enable/disable unmanaged-data scanning; default true. |
| `preserve_nonempty` | No | Cleanup policy. Defaults effectively to true for disk/shared and false for shm state. |
| `rerun_prefix` | No | Prefix for preserved rerun directories; default `.run_count`. |

The supplied configuration maps local storage to `nvme`, `ssd`, or `hdd`; shared storage to `nfs`, `lustre`, `ceph`, or `gpfs`; and shared memory to `tmpfs`.

### PBS resource configuration

The top-level size resources are consumable host resources:

```text
scratch_local          type=size     flag=hn
scratch_shared         type=size     flag=hn
```

Shared memory is a host boolean:

```text
scratch_shm            type=boolean  flag=h
```

Subtype properties are scheduler-selectable string host resources:

```text
scratch_local_subtype  type=string   flag=h
scratch_shared_subtype type=string   flag=h
scratch_shm_subtype    type=string   flag=h
```

If upgrading from the previous design, `scratch_nvme`, `scratch_ssd`, and `scratch_hdd` should be removed after no queued or running jobs reference them.

### Limitations and design notes

- Scratch sizes are reservations, not filesystem quotas.
- Persistent subtype selection deliberately prefers semantic stability over automatic failover. A failed selected backend makes the top-level resource unavailable until the backend returns or the selection is explicitly reset/reconfigured.
- Capacity accounting is based on filesystem free-space snapshots, persistent hook reservations, and periodic unmanaged-data scans; it is accounting-oriented rather than transactional quota enforcement.
- Source/rotational classification depends on Linux mount/sysfs information.
- `scratch_shared` capacity is published per vnode even though the filesystem itself may be common to multiple nodes; site scheduling policy and `group=cluster` placement must therefore be appropriate for the shared-storage topology.
- Dynamic resizing is deliberately unsupported.
