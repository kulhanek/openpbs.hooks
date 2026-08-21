# `hook_aggregate_resources`

## Overview

`hook_aggregate_resources` is an OpenPBS server-periodic hook that builds
cluster-wide lists from vnode resources and stores them in a generated JSON
state file below `PBS_HOME`.

This version does **not** modify `server.resources_available` and therefore
does not invoke `qmgr`.

The generated file is intended as shared state for other server-side hooks,
such as a `queuejob` hook that normalizes submitted resource requests.

## Configuration

The supplied configuration is:

```json
{
    "state_file": "server_priv/hooks/hook_data/aggregate_resources.json",
    "collections": [
        {
            "source": "gpu_cap"
        },
        {
            "source": "cpu_isa"
        }
    ]
}
```

### `state_file`

`state_file` is interpreted relative to `PBS_HOME`.

For example, with:

```text
PBS_HOME=/var/spool/pbs
```

the default setting:

```text
server_priv/hooks/hook_data/aggregate_resources.json
```

resolves to:

```text
/var/spool/pbs/server_priv/hooks/hook_data/aggregate_resources.json
```

The path must be relative. Absolute paths are rejected.

After canonicalization, the resulting path must remain inside `PBS_HOME`;
paths using `..` to escape the PBS tree are rejected.

### `collections`

Each collection contains one vnode `resources_available` resource name:

```json
{
    "source": "gpu_cap"
}
```

The source name is also used as the key in the generated JSON file.

## Generated state file

A typical generated file is:

```json
{
    "generated": 1787331180,
    "resources": {
        "cpu_isa": [
            "x86-64-v3",
            "x86-64-v4"
        ],
        "gpu_cap": [
            "sm_80",
            "sm_89",
            "sm_90"
        ]
    },
    "version": 1
}
```

The `resources` object is the stable interface intended for consuming hooks.

A `queuejob` hook can therefore read:

```python
data["resources"]["gpu_cap"]
data["resources"]["cpu_isa"]
```

## Collection semantics

All vnodes in `pbs.event().vnode_list` are considered regardless of vnode
state.

Only `string` and `string_array` source resources are collected.

Other types are silently skipped.

Unset values on individual vnodes are ignored.

Every string-array member is converted to a plain Python string before
deduplication:

```python
str(item).strip()
```

This avoids duplicate textual values caused by PBS-specific Python wrapper
objects.

The resulting values are unique and lexically sorted.

## Change-only writes

The hook reads the existing state file and compares only:

```json
"resources"
```

with the newly collected resource mapping.

If the resource data is unchanged, the hook does not rewrite the file.

The `generated` timestamp therefore changes only when the aggregate content
changes.

This avoids unnecessary filesystem activity and provides a meaningful
generation timestamp.

## Atomic replacement

The state file is never modified in place.

The hook:

1. creates a temporary file in the destination directory;
2. writes the complete JSON document;
3. flushes and `fsync()`s the temporary file;
4. changes its mode to `0640`;
5. atomically replaces the live file with `os.replace()`.

A consuming `queuejob` hook therefore observes either the previous complete
file or the new complete file, never a partially written JSON document.

The destination directory is created with mode `0750` if it does not exist.

## Reading from another hook

A server-side consumer can use equivalent PBS_HOME resolution and then:

```python
with open(path, "r") as f:
    data = json.load(f)

gpu_caps = data.get("resources", {}).get("gpu_cap", [])
cpu_isas = data.get("resources", {}).get("cpu_isa", [])
```

Because updates use atomic rename, explicit file locking is not necessary for
the normal single-writer/multiple-reader model.

## PBS_HOME discovery

The hook first checks the `PBS_HOME` environment variable.

If it is absent, it reads:

```text
PBS_CONF_FILE
```

or, by default:

```text
/etc/pbs.conf
```

and obtains `PBS_HOME` from there.

## Hook setup

The supplied qmgr file creates only the periodic hook:

```text
aggregate_resources
```

No custom `all_*` server resources are created.

The default period is 300 seconds.

## Installation

Copy the Python and JSON files to the paths referenced by the qmgr setup and
run:

```bash
qmgr < hook_aggregate_resources.qmgr
```

For an existing hook, re-import both the implementation and configuration:

```bash
qmgr -c "import hook aggregate_resources application/x-python default /root/openpbs.hooks/hook_aggregate_resources.py"

qmgr -c "import hook aggregate_resources application/x-config default /root/openpbs.hooks/hook_aggregate_resources.json"
```

After the next periodic run, inspect the generated state file, for example:

```bash
cat /var/spool/pbs/server_priv/hooks/hook_data/aggregate_resources.json
```

using the actual value of `PBS_HOME` on the server.
