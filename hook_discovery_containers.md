# hook_discovery_containers

## Overview

`hook_discovery_containers` discovers container runtimes that are intentionally enabled on an OpenPBS execution host and publishes them through a single vnode resource:

```text
resources_available.containers
```

The resource is a non-consumable `string_array`. Each value is the runtime name used as a key in the hook JSON configuration. The hook does not publish container versions and does not create per-runtime boolean resources such as `singularity=true` or `docker=true`.

The hook runs for `exechost_startup` and `exechost_periodic` events. This publishes the runtime capabilities when PBS MOM starts and refreshes them periodically if the host software changes.

## User documentation

### Resource

The resource is created as:

```qmgr
create resource containers type=string_array flags=h
```

Example vnode values are:

```text
resources_available.containers = apptainer
```

or:

```text
resources_available.containers = apptainer,singularity
```

When no enabled and usable runtime is detected, the hook clears `resources_available.containers` rather than publishing a synthetic runtime name.

### Requesting a container runtime

A job can require a particular runtime in its select statement. For example:

```bash
#PBS -l select=1:ncpus=8:containers=apptainer
```

For a two-node job where both chunks require Apptainer:

```bash
#PBS -l select=2:ncpus=32:containers=apptainer
```

The `containers` resource only expresses runtime availability. The discovery hook does not start containers, transform the job command, configure mounts, set up GPUs, or otherwise modify job execution.

### Runtime names

Runtime names are taken directly from the keys of the `runtimes` object in the JSON configuration. For example:

```json
{
    "runtimes": {
        "apptainer": {
            "enabled": true,
            "commands": ["/usr/bin/apptainer"],
            "probe": ["version"]
        }
    }
}
```

publishes:

```text
resources_available.containers = apptainer
```

No aliasing or automatic normalization is performed.

## Technical documentation

### Discovery procedure

For each configured runtime, the hook performs the following steps:

1. Read the runtime configuration.
2. Skip the runtime when `enabled` is `false`.
3. Examine the configured `commands` in their listed order.
4. Require each command path to be an absolute path.
5. Check whether a candidate path exists as an executable regular file.
6. Execute the candidate command with the configured `probe` arguments.
7. Consider the runtime detected when the probe exits with status 0.
8. Add the runtime configuration key to `resources_available.containers`.

Only the first successful command for a runtime is used. Probe output is discarded. The hook currently has no version discovery or version publication functionality.

A probe is limited to 5 seconds. A failed or timed-out probe affects only that runtime; discovery continues with the remaining candidates and runtimes.

### Absolute command paths

All entries in `commands` must be absolute paths. Examples:

```json
"commands": [
    "/usr/bin/apptainer",
    "/usr/local/bin/apptainer"
]
```

The following is invalid:

```json
"commands": ["apptainer"]
```

The hook intentionally does not use `PATH` or `shutil.which()`. This makes runtime discovery independent of PBS MOM's environment and prevents a changed hook `PATH` from altering the scheduler-visible capabilities of a node.

The absolute-path requirement is validated for every configured runtime, including disabled runtimes. A malformed configuration is therefore detected before discovery begins.

### Configuration

The top-level configuration contains one `runtimes` object. Each key is both the runtime identifier and the exact string published to the PBS resource.

| Item | Type | Required | Meaning |
|---|---|---:|---|
| `runtimes` | object | yes | Mapping of runtime names to their discovery configuration. |
| `runtimes.<name>.enabled` | boolean | yes | Whether this runtime may be advertised by the hook. Disabled runtimes are never probed or published. |
| `runtimes.<name>.commands` | array of strings | yes | Ordered list of candidate executable paths. Every path must be absolute. |
| `runtimes.<name>.probe` | array of strings | no | Arguments appended to the executable for the lightweight usability probe. The default is an empty argument list. |

Runtime names may contain letters, digits, `_`, `.`, `+`, and `-`. They must not contain commas or whitespace because they are published as elements of a PBS `string_array`.

### Default runtime policy

The supplied configuration deliberately distinguishes software discovery from site policy. A runtime is considered only when its `enabled` value is `true`.

The defaults are:

| Runtime | Default | Reason |
|---|---:|---|
| `apptainer` | enabled | Suitable for the intended HPC execution model and does not inherently require per-user subordinate UID/GID mappings. |
| `singularity` | enabled | Suitable for the intended HPC execution model and does not inherently require per-user subordinate UID/GID mappings. |
| `podman` | disabled | Rootless Podman uses user namespaces and requires user entries in `/etc/subuid` and `/etc/subgid`. |
| `docker` | disabled | Safe unprivileged/rootless Docker operation uses user namespaces and subordinate UID/GID mappings; daemon/socket access also requires explicit site policy. |
| `charliecloud` | disabled | Charliecloud's unprivileged execution model relies on unprivileged Linux user namespaces. |
| `enroot` | disabled | Enroot relies on Linux user and mount namespaces for its unprivileged execution model. |

A disabled entry is retained in the example configuration so that an administrator can explicitly enable it after the execution-host prerequisites and security policy have been established.

### Meaning of `enabled`

`enabled` is a site-policy switch, not a result of automatic prerequisite discovery.

For example:

```json
"podman": {
    "enabled": false,
    "commands": ["/usr/bin/podman"],
    "probe": ["--version"]
}
```

means that Podman must not be advertised even if `/usr/bin/podman` is installed.

Changing it to `true` tells the hook that the administrator considers Podman an allowed execution-host capability. The hook does not verify `/etc/subuid`, `/etc/subgid`, user-specific mappings, Docker socket permissions, or other per-user authorization details.

### Probe semantics and limitations

The probe verifies only that the configured executable exists, is executable, and can successfully execute the supplied lightweight probe command in the PBS MOM hook environment.

It does **not** guarantee that every job user can launch an arbitrary container. In particular, the hook does not test:

- per-user subordinate UID/GID mappings;
- user namespace policy for individual users;
- Docker daemon or socket authorization for individual users;
- registry authentication;
- image availability;
- filesystem bind permissions;
- GPU passthrough;
- network namespace configuration;
- runtime-specific site configuration required by a particular image.

These properties are either user-specific or job-specific and therefore cannot be represented accurately by one vnode-wide discovery test.

### Failure handling

A failure while probing one runtime is logged and does not prevent other runtimes from being discovered.

A configuration error is treated differently: the hook logs the error and leaves the current resource state unchanged for that invocation. This avoids replacing a previously valid periodic discovery result with an empty capability set solely because the JSON configuration is temporarily invalid.

The hook does not reject jobs because container discovery failed.

## Files

- `hook_discovery_containers.py` — hook implementation.
- `hook_discovery_containers.json` — runtime discovery configuration.
- `hook_discovery_containers.qmgr` — resource and hook setup.
- `hook_discovery_containers.md` — this documentation.
