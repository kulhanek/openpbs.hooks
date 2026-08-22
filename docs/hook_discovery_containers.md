# `hook_discovery_containers`

## Overview

`hook_discovery_containers` detects container runtimes that are actually executable on each PBS execution host and publishes their names in the vnode resource `containers`.

The hook performs discovery only. It does not start containers, prepare images, grant container privileges, or alter how a submitted job is executed.

## User documentation

Users can inspect the `containers` resource to determine which container runtimes are available on a node and can request a runtime in a job's `select` specification when they need to be scheduled only on suitable nodes.

For example:

```bash
#PBS -l select=1:ncpus=8:containers=apptainer
```

A node matches only if the requested runtime is among the values published for that vnode according to the scheduler's string-array matching rules.

The resource reports runtime availability, not permission to use every runtime feature. Site policy, image access, namespace restrictions, subordinate UID/GID configuration, and runtime-specific security settings still apply.

## Technical and administration documentation

### Hook events

The supplied `hook_discovery_containers.qmgr` installs the hook for:

- `exechost_startup`
- `exechost_periodic`

The default periodic interval is 4000 seconds and the hook order is 50.

### Configuration

The JSON configuration contains a `runtimes` mapping. Each runtime has:

| Field | Description |
| --- | --- |
| `enabled` | Whether discovery for this runtime is enabled. |
| `commands` | Candidate executable paths. Every path must be absolute. Candidates are tried in order. |
| `probe` | Command-line arguments used to verify that the executable works, for example `version` or `--version`. |

A simplified example is:

```json
{
    "runtimes": {
        "apptainer": {
            "enabled": true,
            "commands": [
                "/usr/bin/apptainer",
                "/usr/local/bin/apptainer"
            ],
            "probe": ["version"]
        }
    }
}
```

The shipped configuration enables Apptainer and Singularity. Podman, Docker, Charliecloud, and Enroot entries are present but disabled by default because some of those runtimes require additional namespace, privilege, or `/etc/subuid` and `/etc/subgid` preparation.

Runtime names are validated and may contain letters, digits, `_`, `.`, `+`, and `-`; whitespace and commas are not allowed.

### Detection procedure

For each enabled runtime, the hook tests candidate command paths until one succeeds. A candidate must be an executable regular file, and the configured probe is run with a short timeout. A zero exit status marks that runtime as available.

Only the runtime name is published; runtime versions are intentionally not exposed.

### PBS resources

The supplied `.qmgr` file defines:

| Resource | Type | Flags | Meaning |
| --- | --- | --- | --- |
| `containers` | `string_array` | `h` | Names of detected container runtimes on the vnode. |

If no configured runtime is available, the hook clears the vnode value so stale data is not retained.

### Administration notes

Adding a runtime normally requires adding an entry to the JSON configuration and ensuring that its probe command can be executed safely and non-interactively by the PBS execution daemon. Enabling discovery does not itself configure the runtime for unprivileged jobs.
