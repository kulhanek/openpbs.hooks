# `hook_aggregate_resources`

## Overview

`hook_aggregate_resources` is a periodic server-side hook that collects selected vnode resource values from the cluster and writes a compact JSON summary into the PBS private directory. It is intended to provide other hooks or administrative tooling with a cluster-wide view of values such as GPU compute capabilities and CPU ISA levels.

The hook does not create or modify PBS resources and does not change job scheduling directly.

## User documentation

Regular users do not interact with this hook directly. It does not add job submission options, modify jobs, or change the resources assigned to jobs.

Its effect is indirect: other hooks may use the generated cluster-wide resource summary when normalizing or validating user requests. For example, `hook_normalize_job_gpucap` can use the aggregated `gpu_cap` values to avoid adding compatibility alternatives that do not exist anywhere in the cluster.

No user action is required.

## Technical and administration documentation

### Hook events

The supplied `hook_aggregate_resources.qmgr` installs the hook as a `periodic` server hook. The default configuration runs it every 8000 seconds.

### Configuration

The hook reads its JSON configuration from the PBS hook configuration file. The supplied configuration is:

```json
{
    "state_file": "server_priv/hooks/hook_data/aggregate_resources.json",
    "sources": [
        "gpu_cap",
        "cpu_isa"
    ]
}
```

Configuration fields:

| Field | Description |
| --- | --- |
| `state_file` | Output JSON file. A relative path is resolved below `PBS_HOME`; an absolute path is also accepted. |
| `sources` | Non-empty list of vnode resource names to aggregate. Duplicate names are ignored after their first occurrence. |

Each source value is converted to a string and split on commas. Empty items are discarded, duplicate items are removed, and the resulting values are sorted.

### Output file

A typical output file has the following structure:

```json
{
    "generated": 1787333315,
    "resources": {
        "cpu_isa": [
            "x86-64-v2",
            "x86-64-v3"
        ],
        "gpu_cap": [
            "sm_61",
            "sm_86",
            "sm_89"
        ]
    },
    "version": 1
}
```

`generated` is a Unix timestamp. The file is rewritten on every successful periodic run. The hook creates the parent directory when necessary, writes through a temporary file, calls `fsync()`, sets mode `0644`, and atomically replaces the previous state file.

### PBS resources

This hook defines no PBS resources of its own. It reads vnode resources named in `sources`. With the supplied configuration these are:

| Resource | Produced by |
| --- | --- |
| `gpu_cap` | `hook_discovery_gpus` |
| `cpu_isa` | `hook_discovery_cpus` |

If a configured source is absent on a vnode, that vnode simply contributes no value for that source.

### Failure behavior

Configuration and file-writing errors are logged. Because this hook is used to maintain auxiliary state rather than to execute a job, administrators should monitor the PBS server log for failures and ensure that the configured output directory is writable by the PBS server process.
