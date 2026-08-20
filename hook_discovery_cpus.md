# `hook_discovery_cpus`

## 1. Overview

`hook_discovery_cpus` is an execution-host discovery hook that publishes CPU topology, CPU identification/performance metadata, and usable memory capacity to the local PBS vnode. It is intended to run on `exechost_startup` and `exechost_periodic`.

The hook deliberately treats **physical CPU cores** as PBS CPU capacity. Therefore, `resources_available.ncpus` is the number of physical cores, while `resources_available.nthreads` reports the number of online logical processing units (PUs). CPU topology is read from Linux sysfs, CPU metadata from `/proc/cpuinfo`, and memory information from `/proc/meminfo`.

In addition to the CPU counts, the hook publishes three topology properties: `smt`, `hybrid_cpu`, and `npus_per_core`. These are derived directly from the number of logical PUs belonging to each physical core.

## 2. User documentation

### Published resources

| Resource | Meaning | Typical use |
|---|---|---|
| `ncpus` | Number of physical CPU cores. This is the consumable CPU capacity used by PBS. | `select=1:ncpus=8` |
| `nthreads` | Number of online logical CPUs/PUs. Includes SMT siblings where present. | Informational/selectable node property. |
| `smt` | `true` when `nthreads > ncpus`; otherwise `false`. This means that at least one physical core exposes more than one logical PU. | `select=1:ncpus=8:smt=true` |
| `hybrid_cpu` | `false` whenever `smt=false`. When `smt=true`, it is `true` if physical cores do not all expose the same number of logical PUs. | `select=1:hybrid_cpu=false` to require uniform SMT topology. |
| `npus_per_core` | String describing the uniform number of logical PUs per physical core. It is forced to `"1"` when `smt=false` or `hybrid_cpu=true`; otherwise it is the actual uniform PU count, e.g. `"2"`. | `select=1:smt=true:hybrid_cpu=false:npus_per_core=2` |
| `mem` | Usable physical RAM after subtracting `memory_reserve`. Published explicitly in `kb`. | `select=1:ncpus=8:mem=32gb` |
| `vmem` | Usable physical RAM plus system swap. Published explicitly in `kb`; publication can be disabled. | `select=1:vmem=64gb` if used by local policy. |
| `cpu_model` | Unique CPU model name(s) found among online CPUs. | Select nodes by a site-defined exact value. |
| `cpu_vendor` | Unique CPU vendor value(s). The native vendor may be translated through `cpu_vendor_map`. | `select=1:cpu_vendor=amd` |
| `cpu_flag` | CPU flags common to **all** online logical CPUs. | Select nodes advertising a required instruction-set flag. |
| `cpu_isa` | Cumulative x86-64 psABI microarchitecture feature levels supported by all online PUs, e.g. `x86-64-v1,x86-64-v2,x86-64-v3`. Empty on non-x86-64 systems. | `select=1:cpu_isa=x86-64-v3` |
| `spec` | Site-defined floating-point relative performance value for one CPU core, selected from `cpu_spec_map`. | Node property for scheduler/site policy. |

The supplied setup defines the custom resources as:

```text
nthreads      : long
smt           : boolean
hybrid_cpu    : boolean
npus_per_core : string
cpu_model     : string
cpu_vendor    : string
cpu_flag      : string_array
cpu_isa       : string_array
spec          : float
```

`ncpus`, `mem`, and `vmem` are standard PBS resources.

### CPU topology semantics

The resources are related as follows:

| Physical-core PU counts | `ncpus` | `nthreads` | `smt` | `hybrid_cpu` | `npus_per_core` |
|---|---:|---:|---|---|---|
| `1,1,1,1` | 4 | 4 | `false` | `false` | `"1"` |
| `2,2,2,2` | 4 | 8 | `true` | `false` | `"2"` |
| `4,4,4,4` | 4 | 16 | `true` | `false` | `"4"` |
| `2,2,1,1` | 4 | 6 | `true` | `true` | `"1"` |

`hybrid_cpu` in this hook specifically describes **mixed logical-PU counts per physical core**. It is therefore a topology property, not a general classification of heterogeneous CPU microarchitectures.

### Example requests

Request eight physical CPU cores and 32 GiB of memory:

```bash
#PBS -l select=1:ncpus=8:mem=32gb
```

Request an AMD node:

```bash
#PBS -l select=1:ncpus=8:cpu_vendor=amd
```

Request a node with SMT available:

```bash
#PBS -l select=1:ncpus=8:smt=true
```


Request a node capable of running software built for the x86-64-v3 psABI level:

```bash
#PBS -l select=1:ncpus=8:cpu_isa=x86-64-v3
```

`cpu_isa` is cumulative. A node whose highest detected level is `x86-64-v3`
publishes `x86-64-v1,x86-64-v2,x86-64-v3`, so requests for either v1, v2,
or v3 match that node.

Request a non-hybrid 2-way SMT topology:

```bash
#PBS -l select=1:ncpus=8:smt=true:hybrid_cpu=false:npus_per_core=2
```

The exact scheduling behaviour of custom resources depends on how they are included in the scheduler configuration and on any job hooks that interpret them.

### Restrictions and important semantics

- `ncpus` means **physical cores**, not Linux logical CPU IDs.
- `smt=true` means that at least one physical core has more than one online PU.
- `hybrid_cpu=true` is possible only when `smt=true`.
- `npus_per_core` is intentionally `"1"` on mixed-SMT/hybrid CPUs, because a single PU-per-core value cannot represent the topology accurately.
- The hook derives physical-core sibling groups from sysfs and does not assume that SMT siblings are adjacent or that SMT is always 2-way.
- `cpu_flag` contains only flags present on every online logical CPU.
- `cpu_isa` is derived from those common flags and is therefore safe for the complete vnode even when logical CPUs differ in their reported capabilities.
- On x86-64, `cpu_isa` always starts with `x86-64-v1`; higher levels are added only when every required feature of each successive level is available.
- If `publish_vmem` is false, the hook leaves `resources_available.vmem` untouched.
- If the configured memory reserve exceeds physical memory, published `mem` is clamped to zero.

## 3. Technical documentation

### Operation

On each supported event, the hook:

1. Identifies online logical CPUs from `/sys/devices/system/cpu/online`, with a fallback to enumerating `cpu[0-9]*` directories.
2. Groups logical CPUs into physical cores using `topology/core_cpus_list`, falling back to `thread_siblings_list`.
3. Counts physical core groups as `ncpus` and the total members of those groups as `nthreads`.
4. Sets `smt = (nthreads > ncpus)`.
5. If `smt=false`, sets `hybrid_cpu=false` and `npus_per_core="1"`.
6. If `smt=true`, compares `len(core)` for every discovered physical-core group. If all counts are equal, `hybrid_cpu=false` and `npus_per_core` is that common count. If the counts differ, `hybrid_cpu=true` and `npus_per_core="1"`.
7. Reads `MemTotal` and `SwapTotal` from `/proc/meminfo`, subtracts the configured `memory_reserve`, and publishes memory values as PBS sizes in `kb`.
8. Reads `/proc/cpuinfo` for online CPUs and gathers CPU models, mapped vendors, and the intersection of CPU flags.
9. On x86-64, derives cumulative `cpu_isa` levels from the common CPU flags.
10. Matches the resulting CPU model against `cpu_spec_map`; the first matching entry wins, otherwise `default_spec` is used.
11. Publishes the values only to vnodes recognised as local to the MoM.

The topology calculation operates on sibling sets, so it is independent of Linux CPU-number ordering. For example, interleaved SMT numbering and systems that place secondary PUs at the end of the logical CPU range are handled identically.


### x86-64 microarchitecture feature levels

`cpu_isa` follows the x86-64 psABI microarchitecture feature levels. The levels
are cumulative: v3 includes the requirements of v2, and v4 includes the
requirements of v3.

The hook maps the psABI requirements to Linux `/proc/cpuinfo` flag names as
follows:

| Level | Additional required Linux flags |
|---|---|
| `x86-64-v1` | Baseline x86-64 architecture; inferred from `uname().machine` |
| `x86-64-v2` | `cx16`, `lahf_lm`, `popcnt`, `pni`, `sse4_1`, `sse4_2`, `ssse3` |
| `x86-64-v3` | `avx`, `avx2`, `bmi1`, `bmi2`, `f16c`, `fma`, `abm`, `movbe`, `xsave` |
| `x86-64-v4` | `avx512f`, `avx512bw`, `avx512cd`, `avx512dq`, `avx512vl` |

Linux names SSE3 as `pni`, CMPXCHG16B as `cx16`, LAHF/SAHF in 64-bit mode as
`lahf_lm`, and LZCNT capability as `abm`. For the psABI v3 OSXSAVE requirement,
the hook uses Linux's usable `avx` reporting together with `xsave`: AVX is not
advertised to userspace by Linux unless the required extended-state support is
enabled.

The resource contains every supported level rather than only the highest one.
For example, a v4 node publishes:

```text
x86-64-v1,x86-64-v2,x86-64-v3,x86-64-v4
```

This representation is intentional because PBS `string_array` matching can
then select any minimum required ISA level naturally.

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

`cpu_spec_map` is evaluated in list order and uses shell-style wildcard matching against the consolidated `cpu_model` string. The first matching entry provides the floating-point `spec` value. If no entry matches, `default_spec` is used.

### JSON configuration

The topology resources (`smt`, `hybrid_cpu`, and `npus_per_core`) require no configuration; they are always derived from the discovered topology.

| Item | Type | Default | Description |
|---|---:|---:|---|
| `memory_reserve` | size/string or integer bytes | `"0B"` | Amount of physical RAM withheld from `resources_available.mem`. Supports binary size suffixes such as `KB`, `MB`, `GB`, etc. The supplied configuration uses `4GB`. |
| `publish_vmem` | boolean | `true` | If true, publishes usable physical RAM plus system swap as `resources_available.vmem`. If false, `vmem` is not modified by this hook. |
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
- `hybrid_cpu` detects heterogeneous **PU counts per physical core** only. A CPU with different core microarchitectures but identical PU counts on every core will have `hybrid_cpu=false`.
- `cpu_isa` is currently defined only for x86-64. On other architectures the hook publishes an empty string-array value.
- ISA detection is based on Linux `/proc/cpuinfo` flags rather than executing test instructions or querying compiler support.
- Offline logical CPUs affect the observed topology because only online PUs are considered. Thus, administratively offlining one SMT sibling can make an otherwise uniform CPU appear hybrid.
- `spec` mapping is string-pattern based and is a site policy, not a hardware benchmark performed by the hook.
- The hook recognises local vnodes from the local node name; if no local vnode is present in `event.vnode_list`, the event is rejected.
