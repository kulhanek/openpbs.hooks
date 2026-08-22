# `hook_discovery_node`

## Overview

`hook_discovery_node` publishes basic execution-host classification information: operating-system identity, operating-system family, detected Linux cgroup version, and the PBS server associated with the execution host.

These resources provide a common foundation for scheduling constraints and for other execution hooks that depend on operating-system or cgroup capabilities.

## User documentation

Users can constrain jobs to a particular operating-system image or family when software compatibility requires it. For example:

```bash
#PBS -l select=1:ncpus=8:os=ubuntu24
```

or:

```bash
#PBS -l select=1:ncpus=8:osfamily=ubuntu
```

The `cgroups` resource can be inspected to determine which cgroup implementation a node advertises. In this hook set, `hook_job_cgroups_v2` requires `v2` on every execution vnode used by a job.

`pbs_server` identifies the PBS server to which the execution host belongs. It is mainly useful for administration and cluster classification rather than for ordinary job requests.

## Technical and administration documentation

### Hook events

The supplied `hook_discovery_node.qmgr` installs the hook for:

- `exechost_startup`
- `exechost_periodic`

The default periodic interval is 8000 seconds and the hook order is 10, making it the earliest of the supplied discovery hooks.

### Operating-system detection

The hook reads `PRETTY_NAME` from `/etc/os-release`, falling back to `/usr/lib/os-release`. It then evaluates the ordered `distros` mapping from the JSON configuration. The first matching wildcard rule supplies both `os` and `osfamily`.

The supplied mappings classify current Debian and Ubuntu releases, for example:

- Debian 14 -> `os=debian14`, `osfamily=debian`;
- Ubuntu 26.04 -> `os=ubuntu26`, `osfamily=ubuntu`;
- Debian 13 -> `debian13` / `debian`;
- Ubuntu 24.04 -> `ubuntu24` / `ubuntu`;
- Debian 12 -> `debian12` / `debian`;
- Ubuntu 22.04 -> `ubuntu22` / `ubuntu`.

An unrecognized distribution is treated as a configuration/detection error rather than being silently assigned an arbitrary name.

### Cgroup detection

The hook determines the active Linux cgroup generation. It first considers the kernel `systemd.unified_cgroup_hierarchy` setting where present, then falls back to filesystem/mount evidence such as `/sys/fs/cgroup/cgroup.controllers`.

The published `cgroups` value is `v1` or `v2`.

### PBS server detection

`pbs_server` is taken from the PBS server object when available and otherwise falls back to `PBS_SERVER` from `pbs.conf`.

### Configuration

The JSON configuration contains the ordered `distros` list. Each entry provides a wildcard pattern for the OS `PRETTY_NAME` and the values to publish as `os` and `osfamily`.

Administrators adding an operating-system release should add a sufficiently specific mapping before broader rules that could also match it.

### PBS resources

The supplied `.qmgr` file defines:

| Resource | Type | Flags | Meaning |
| --- | --- | --- | --- |
| `os` | `string` | `h` | Site-normalized operating-system release identifier. |
| `osfamily` | `string` | `h` | Operating-system family. |
| `cgroups` | `string_array` | `h` | Detected Linux cgroup generation. |
| `pbs_server` | `string` | `h` | PBS server associated with the execution host. |

### Interaction with other hooks

Most importantly, `hook_job_cgroups_v2` checks the `cgroups` vnode resource and refuses execution on a local vnode that does not advertise cgroup v2. The OS resources may also be consumed by normalization or site policy hooks.
