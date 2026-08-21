# `hook_aggregate_resources`

## 1. Overview

`hook_aggregate_resources` is an OpenPBS **server-periodic** hook that builds
server-wide indexes from resources published on vnodes.

For every configured collection, the hook:

1. reads one `resources_available` resource from every vnode;
2. flattens all string/string-array values;
3. removes duplicates;
4. sorts the resulting strings;
5. publishes the result into a configured server
   `resources_available` string-array resource.

The hook does **not** filter vnodes by state. Offline, down, busy, free, and
other vnode states are treated identically. This is intentional: the aggregate
describes capabilities represented in the PBS complex, not only capabilities
that are currently schedulable.

The server resource is written **only when the collected value changes**. This
avoids unnecessary server resource updates and corresponding scheduler
disturbance on every periodic invocation.

## 2. Supplied configuration

The supplied JSON configuration is:

```json
{
    "collections": [
        {
            "source": "gpu_cap",
            "target": "all_gpu_caps"
        },
        {
            "source": "cpu_isa",
            "target": "all_cpu_isas"
        }
    ]
}
```

It produces:

```text
vnode resources_available.gpu_cap
    -> server resources_available.all_gpu_caps

vnode resources_available.cpu_isa
    -> server resources_available.all_cpu_isas
```

For example, if the vnodes publish:

```text
node01  gpu_cap = sm_80
node02  gpu_cap = sm_90
node03  gpu_cap = sm_80
node04  gpu_cap = sm_89
```

the server receives:

```text
resources_available.all_gpu_caps = sm_80,sm_89,sm_90
```

Likewise, vnode values:

```text
node01  cpu_isa = x86-64-v3
node02  cpu_isa = x86-64-v4
node03  cpu_isa = x86-64-v3
```

produce:

```text
resources_available.all_cpu_isas = x86-64-v3,x86-64-v4
```

## 3. JSON configuration

The top-level `collections` item is a list of mappings.

Each mapping has two fields:

| Field | Meaning |
|---|---|
| `source` | Vnode `resources_available` resource to collect. |
| `target` | Server `resources_available` string-array resource receiving the sorted unique union. |

Example:

```json
{
    "collections": [
        {
            "source": "gpu_arch",
            "target": "all_gpu_archs"
        }
    ]
}
```

The hook contains no hard-coded knowledge of GPU or CPU resources. Additional
collections can therefore be added only by changing the JSON configuration and
defining the corresponding target PBS resource.

## 4. Source resource types

Only source values represented by PBS as:

- `string`, or
- `string_array`

are collected.

Other resource types, such as:

```text
boolean
long
float
size
```

are silently skipped. A collection with an unsupported source type does not
modify its target resource.

Unset source values on individual vnodes are simply ignored.

If the source is a valid string/string-array resource but no vnode currently
publishes any value, the resulting aggregate is empty and a previously set
target value is cleared.

## 5. Collection semantics

### All vnodes are considered

No vnode-state filter is applied.

In particular, a capability on an offline vnode remains represented in the
server aggregate. This prevents transient vnode states from changing a
server-wide description of the hardware/software capabilities known to PBS.

### String arrays are flattened

If a vnode publishes more than one item, every item participates in the union.

For example:

```text
node01  source = a,b
node02  source = b,c
```

becomes:

```text
target = a,b,c
```

### Values are unique and sorted

A Python set is used to remove duplicates, followed by lexical sorting. This
makes the published representation deterministic and prevents vnode traversal
order from causing spurious updates.

### Updates are change-only

Before publishing, the hook normalizes the current server target and compares
it with the newly collected sorted list.

If both are equal, no assignment to `server.resources_available` is made.

If they differ, the target is updated once.

An empty new list clears the target with `None`.

## 6. PBS setup

The supplied qmgr file creates:

```text
all_gpu_caps : string_array
all_cpu_isas : string_array
```

No resource flags are assigned to these target resources. They are intended as
server-level descriptive/index resources rather than vnode selection
resources.

The qmgr setup then creates:

```text
aggregate_resources
```

as a site hook using the `periodic` event with a default frequency of 300
seconds.

The hook files are imported from:

```text
/root/openpbs.hooks/hook_aggregate_resources.py
/root/openpbs.hooks/hook_aggregate_resources.json
```

Adjust these paths to the deployment location if necessary.

## 7. Installation

Copy the files to the paths referenced by the qmgr setup, then run the qmgr
script, for example:

```bash
qmgr < hook_aggregate_resources.qmgr
```

The hook is enabled by the supplied setup.

The resulting server resources can be inspected with:

```bash
qmgr -c "print server"
```

or:

```bash
qstat -Bf
```

Depending on the OpenPBS display format/version, the relevant entries appear
as:

```text
resources_available.all_gpu_caps = ...
resources_available.all_cpu_isas = ...
```

## 8. Behaviour and limitations

- This hook creates an eventually consistent server-wide index. A vnode
  resource change becomes visible in the aggregate on the next periodic run.
- The hook does not modify vnode resources.
- The hook does not restart or explicitly trigger a scheduler cycle.
- The hook does not inspect vnode state.
- The hook does not preserve unsupported source types by converting them to
  strings.
- Source resource definitions should be consistent across the PBS complex, as
  PBS custom resource definitions normally are.
- Target resources must be defined as `string_array`.
- A target should not also be used as a source in the same configuration.
- A collection whose `source` and `target` names are identical is ignored.

## 9. Design rationale

The hook reconstructs every aggregate from the current vnode data on each run
rather than trying to maintain an incremental cache. This makes it
self-healing: vnode additions, removals, and discovery-resource changes are
automatically reflected without maintaining persistent hook state.

The discovery hooks remain responsible only for facts about individual
execution hosts, while `hook_aggregate_resources` provides cluster-wide
indexes derived from those facts.
