# `hook_discovery_node`

## 1. Overview

`hook_discovery_node` publishes basic execution-node identity and platform properties: a site-defined operating-system token, OS family, cgroup hierarchy version, and PBS server name. It is intended for `exechost_startup` and `exechost_periodic`.

The OS values are not inferred from hard-coded distribution rules. Instead, the hook reads `PRETTY_NAME` from `os-release` and matches it against an ordered mapping in the hook JSON configuration.

## 2. User documentation

### Published resources

| Resource | Meaning | Example use |
|---|---|---|
| `os` | Site-defined operating-system token selected from the configured `PRETTY_NAME` map. | `select=1:ncpus=8:os=ubuntu24` |
| `osfamily` | Site-defined OS family from the same mapping. | `select=1:ncpus=8:osfamily=ubuntu` |
| `cgroups` | Cgroup hierarchy token, either `v1` or `v2`. | `select=1:ncpus=8:cgroups=v2` |
| `pbs_server` | PBS server name associated with the execution host. | Can be used as a node-selection property in multi-server/site-specific deployments. |

Example OS-specific request using the supplied configuration:

```bash
#PBS -l select=1:ncpus=8:os=ubuntu24
```

Request any Ubuntu-family node:

```bash
#PBS -l select=1:ncpus=8:osfamily=ubuntu
```

Request a node advertising cgroup v2:

```bash
#PBS -l select=1:ncpus=8:cgroups=v2
```

### Restrictions

- `PRETTY_NAME` must match at least one configured `distros` entry. If it does not, discovery fails rather than silently inventing or falling back to an OS token.
- Distribution mappings are evaluated in order and the **first matching entry wins**.
- Matching is case-sensitive because the implementation uses `fnmatch.fnmatchcase()`.
- A matching distro entry must define both `os` and `osfamily`.
- The cgroup resource reports the host hierarchy only; enforcement is performed by other hooks such as `hook_job_cgroups_v2`.

## 3. Technical documentation

### OS detection

The hook reads `PRETTY_NAME` first from `/etc/os-release`, then from `/usr/lib/os-release`. It compares the complete string with each configured `distros[].name` shell-style glob.

For the supplied configuration, a value such as:

```text
Ubuntu 24.04.4 LTS
```

matches:

```json
{
  "name": "*Ubuntu 24.04*",
  "os": "ubuntu24",
  "osfamily": "ubuntu"
}
```

The glob is matched against the entire `PRETTY_NAME` string; leading/trailing `*` wildcards are therefore useful when matching only a stable substring.

### Cgroup detection

Cgroup detection follows this precedence:

1. `systemd.unified_cgroup_hierarchy=0` in `/proc/cmdline` -> `v1`.
2. `systemd.unified_cgroup_hierarchy=1` in `/proc/cmdline` -> `v2`.
3. Presence of `/sys/fs/cgroup/cgroup.controllers` -> `v2`.
4. `/proc/self/mountinfo` containing a `cgroup2` filesystem -> `v2`.
5. `/proc/self/mountinfo` containing a `cgroup` filesystem -> `v1`.

If none of these methods succeeds, the hook raises an error.

### PBS server detection

The hook first tries `pbs.server().name`. If that is unavailable, it reads `PBS_SERVER` from `PBS_CONF_FILE` (default `/etc/pbs.conf`). Failure of both methods is fatal to the discovery event.

### JSON configuration

| Item | Type | Default | Description |
|---|---:|---:|---|
| `distros` | array | `[]` | Ordered list mapping Linux `PRETTY_NAME` strings to site OS tokens. |
| `distros[].name` | string | — | Shell-style, case-sensitive glob matched against `PRETTY_NAME`. |
| `distros[].os` | string | — | Value published as `resources_available.os`. Required on a matching entry. |
| `distros[].osfamily` | string | — | Value published as `resources_available.osfamily`. Required on a matching entry. |

The supplied configuration defines Debian 12/13/14 and Ubuntu 22.04/24.04/26.04 mappings.

### Limitations and failure behaviour

- The hook is Linux-specific because it relies on `os-release`, `/proc`, and Linux cgroup filesystem conventions.
- A new distribution/release must be added to the JSON map before the hook will accept it.
- Because first match wins, broad patterns placed before specific patterns can shadow them.
- The hook publishes values only to vnodes recognised as local to the current MoM; failure to find a local vnode rejects the event.
