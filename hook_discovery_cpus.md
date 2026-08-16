# `hook_discovery_cpus`

## 1. Overview

`hook_discovery_cpus` is an execution-host discovery hook that publishes CPU topology, CPU identification/performance metadata, and usable memory capacity to the local PBS vnode. It is intended to run on `exechost_startup` and `exechost_periodic`.

The hook deliberately treats **physical CPU cores** as PBS CPU capacity. Therefore, `resources_available.ncpus` is the number of physical cores, while `resources_available.nthreads` reports the number of online logical CPUs (hardware threads/PUs). CPU topology is read from Linux sysfs, CPU metadata from `/proc/cpuinfo`, and memory information from `/proc/meminfo`.

## 2. User documentation

### Published resources

| Resource | Meaning | Typical use |
|---|---|---|
| `ncpus` | Number of physical CPU cores. This is the consumable CPU capacity used by PBS. | `select=1:ncpus=8` |
| `nthreads` | Number of online logical CPUs/PUs. Informational node property; includes SMT siblings where present. | Node selection if the site defines it as a selectable resource. |
| `hyperthreading` | `true` when `nthreads > ncpus`, otherwise `false`. Describes whether at least some cores expose more than one logical CPU. | `select=1:ncpus=8:hyperthreading=true` when used together with `hook_job_cgroups_v2`. |
| `mem` | Usable physical RAM after subtracting `memory_reserve`. Published explicitly in `kb`. | `select=1:ncpus=8:mem=32gb` |
| `vmem` | Usable physical RAM plus system swap. Published explicitly in `kb`; publication can be disabled. | `select=1:vmem=64gb` if used by local policy. |
| `cpu_model` | Unique CPU model name(s) found among online CPUs. | For example, select nodes by a site-defined exact/string-array value. |
| `cpu_vendor` | Unique CPU vendor value(s). The native vendor may be translated through `cpu_vendor_map`. | For example, `select=1:cpu_vendor=amd`. |
| `cpu_flag` | CPU flags common to **all** online logical CPUs. | For example, selecting nodes that advertise a required instruction-set flag. |
| `spec` | Site-defined floating-point relative performance value for one CPU core, selected from `cpu_spec_map`. | May be used as a node property for scheduler/site policy. |

Suggested custom resource types from the hook source are:

```text
nthreads       : long
hyperthreading : boolean
cpu_model      : string_array (or string on homogeneous nodes)
cpu_vendor     : string_array (or string on homogeneous nodes)
cpu_flag       : string_array
spec           : float
```

`ncpus`, `mem`, and `vmem` are standard PBS resources.

### Example requests

Request eight physical CPU cores and 32 GiB of memory:

```bash
#PBS -l select=1:ncpus=8:mem=32gb
```

Request an AMD node, assuming the supplied vendor mapping is used:

```bash
#PBS -l select=1:ncpus=8:cpu_vendor=amd
```

Request a node that supports hyperthreading and ask the cgroup hook to expose SMT siblings for the selected physical cores:

```bash
#PBS -l select=1:ncpus=8:hyperthreading=true
```

The exact scheduling behaviour of custom resources depends on how those resources are defined in PBS and included in scheduler configuration.

### Restrictions and important semantics

- `ncpus` means **physical cores**, not Linux logical CPU IDs.
- `hyperthreading=true` describes/request hyperthreading support; actual per-job SMT handling is implemented by `hook_job_cgroups_v2`, not by this discovery hook.
- On hybrid or mixed-SMT CPUs, `nthreads` can differ from `2 * ncpus`; the hook derives sibling groups from sysfs rather than assuming two threads per core.
- `cpu_flag` contains only flags present on every online logical CPU. This is important on heterogeneous/hybrid CPUs where not all logical CPUs necessarily expose identical features.
- `cpu_model`, `cpu_vendor`, and `cpu_flag` are comma-separated stable sets suitable for `string_array` resources.
- If `publish_vmem` is false, this hook leaves `resources_available.vmem` untouched.
- If the configured memory reserve exceeds physical memory, published `mem` is clamped to zero.

## 3. Technical documentation

### Operation

On each supported event, the hook:

1. Identifies online logical CPUs from `/sys/devices/system/cpu/online`, with a fallback to enumerating `cpu[0-9]*` directories.
2. Groups logical CPUs into physical cores using `topology/core_cpus_list`, falling back to `thread_siblings_list`.
3. Counts physical core groups as `ncpus` and all members of those groups as `nthreads`.
4. Reads `MemTotal` and `SwapTotal` from `/proc/meminfo`, subtracts the configured `memory_reserve`, and publishes memory values as PBS sizes in `kb`.
5. Reads `/proc/cpuinfo` for online CPUs and gathers CPU models, mapped vendors, and the intersection of CPU flags.
6. Matches the resulting CPU model against `cpu_spec_map`; the first matching entry wins, otherwise `default_spec` is used.
7. Publishes the values only to vnodes recognised as local to the MoM.

The hook does not allocate CPUs and does not manage cgroups. It only publishes vnode properties.

### CPU vendor mapping

`cpu_vendor_map` is evaluated in list order. Each entry uses shell-style wildcard matching. If `cs` is omitted or false, matching is case-insensitive. The first matching entry replaces the native vendor string with its `alias`. If no entry matches, the native vendor is published unchanged.

Example:

```json
{
  "pattern": "*AMD*",
  "cs": false,
  "alias": "amd"
}
```

### CPU performance mapping

`cpu_spec_map` is also evaluated in list order and uses shell-style wildcard matching against the consolidated `cpu_model` string. The first matching entry provides the floating-point `spec` value. If no entry matches, `default_spec` is used.

### JSON configuration

| Item | Type | Default | Description |
|---|---:|---:|---|
| `memory_reserve` | size/string or integer bytes | `"0B"` | Amount of physical RAM withheld from `resources_available.mem`. Supports binary size suffixes such as `KB`, `MB`, `GB`, etc. The supplied configuration uses `4GB`. |
| `publish_vmem` | boolean | `true` | If true, publishes usable physical RAM plus configured system swap as `resources_available.vmem`. If false, `vmem` is not modified by this hook. |
| `cpu_vendor_map` | array | `[]` | Ordered list translating native CPU vendor strings to site aliases. |
| `cpu_vendor_map[].pattern` | string | — | Shell-style wildcard matched against the native CPU vendor string. |
| `cpu_vendor_map[].cs` | boolean | `false` | Whether vendor matching is case-sensitive. |
| `cpu_vendor_map[].alias` | string | — | Value published in `cpu_vendor` when the entry matches. Entries missing `pattern` or `alias` are ignored. |
| `default_spec` | float | `0.0` | Fallback `spec` value when no CPU model mapping matches. The supplied configuration uses `30.0`. |
| `cpu_spec_map` | array | `[]` | Ordered mapping of CPU model patterns to per-core `spec` values. |
| `cpu_spec_map[].pattern` | string | — | Shell-style wildcard matched against `cpu_model`. |
| `cpu_spec_map[].cs` | boolean | `false` | Whether CPU model matching is case-sensitive. |
| `cpu_spec_map[].value` | float | — | Value published as `resources_available.spec`. |

### Limitations and failure behaviour

- Linux sysfs and procfs interfaces are required.
- Physical-core detection depends on kernel topology files being correct. If no physical cores can be discovered, the event is rejected.
- `spec` mapping is string-pattern based and is therefore a site policy, not a hardware benchmark performed by the hook.
- The hook recognises local vnodes from the local node name; if no local vnode is present in `event.vnode_list`, the event is rejected.

