# `hook_discovery_cpus`

## Overview

`hook_discovery_cpus` discovers CPU topology, processor identity and capabilities, CPU architecture and ISA level, memory capacity, and site-specific CPU classification resources on each execution host. It publishes this information as vnode resources for scheduling and for use by other execution hooks.

The hook supports both x86-64 and AArch64 hosts. `cpu_arch` identifies the execution architecture, `cpu_isa` publishes one highest supported ISA baseline detected for that architecture, and `cpu_flag` publishes the individual CPU flags/features common to all online logical CPUs.

The hook distinguishes physical CPU cores from logical processing units. In this installation `ncpus` represents physical cores, while `nthreads` represents logical CPUs visible to the operating system.

## User documentation

The resources published by this hook allow users to request nodes with particular processor properties.

`cpu_arch` selects the processor architecture. Typical values are `x86_64` and `aarch64`:

```bash
# request an AArch64 node
#PBS -l select=1:ncpus=16:cpu_arch=aarch64
```

`cpu_isa` contains one ISA baseline representing the highest level detected on the node. Examples are `x86-64-v3` on x86-64 systems or `armv9-a` on AArch64 systems:

```bash
# request exactly the x86-64-v3 ISA value
#PBS -l select=1:ncpus=8:cpu_isa=x86-64-v3
```

The discovery hook publishes only the exact detected ISA token. Compatibility requests such as `compat[x86-64-v2]` or `compat[armv9-a]` are not processed by this hook. They are handled separately by `hook_normalize_job_cpuisa`, which shares the same configuration file and expands compatible ISA values before scheduling.

`cpu_flag` can be used when a particular CPU instruction-set extension or capability is required:

```bash
# x86-64 example
#PBS -l select=1:ncpus=8:cpu_flag=avx2
```

```bash
# AArch64 example
#PBS -l select=1:ncpus=8:cpu_flag=sve
```

On x86-64, `cpu_flag` is populated from the common `flags` reported in `/proc/cpuinfo`. On AArch64, it is populated from the common `Features` reported in `/proc/cpuinfo`. The Linux names are published unchanged, for example `avx2` on x86-64 or `asimd`, `sve`, `sve2`, `bf16`, and `i8mm` on AArch64 when available.

The hook also publishes `smt`, `hybrid_cpu`, and `npus_per_core`, which describe the CPU topology. These values are used by the cgroup and MPI/OpenMP normalization hooks. In particular, this PBS installation treats `ncpus` as physical-core capacity rather than logical-thread capacity.

`mem` and, when enabled, `vmem` are also updated from the host's current memory configuration after applying the configured memory reserve.

Users normally do not need to know how these values are detected; they can inspect vnode resources with standard PBS commands and include appropriate resources in `select` requests.

## Technical and administration documentation

### Hook events

The supplied `hook_discovery_cpus.qmgr` installs the hook for:

- `exechost_startup`
- `exechost_periodic`

The default periodic interval is 3600 seconds and the hook order is 20.

### CPU topology model

The hook reads Linux CPU topology from sysfs and processor information from `/proc/cpuinfo`.

It publishes:

- `ncpus` as the number of physical cores;
- `nthreads` as the number of online logical CPUs;
- `smt=true` when more than one logical CPU is available per physical-core allocation;
- `hybrid_cpu=true` when SMT is present but physical cores do not all expose the same number of logical processing units;
- `npus_per_core` as the uniform number of processing units per physical core. It is published as `"1"` when SMT is absent or when the topology is hybrid.

This topology model is consumed by `hook_job_cgroups_v2` and `hook_normalize_job_mpiomp`.

### CPU architecture

`cpu_arch` is a `string_array` PBS resource, but the hook publishes exactly one architecture token for a host.

The default architecture comes from the system machine architecture and is normalized to stable names. In particular:

- `x86_64` and `amd64` are normalized to `x86_64`;
- `aarch64` and `arm64` are normalized to `aarch64`.

The `arch` field in a matching `cpu_vendor_map` entry can override the system-derived value. This allows an administrator to make architecture naming explicit in the shared configuration. If no matching rule supplies `arch`, the normalized system architecture is used.

### Processor identity and vendor mapping

`cpu_model` is derived from processor model information in `/proc/cpuinfo`.

`cpu_vendor` is based on the native CPU vendor identifier and is optionally translated through `cpu_vendor_map`. On x86 this is normally the `vendor_id` field. On AArch64 it is normally the `CPU implementer` field. The first matching mapping rule wins.

A mapping rule supports:

- `pattern`: shell-style wildcard matched against the native vendor value;
- `cs`: whether matching is case-sensitive; default is `false`;
- `alias`: value published as `cpu_vendor`;
- `arch`: optional architecture value published as `cpu_arch`.

For example, the supplied x86 mappings explicitly associate AMD and Intel with `x86_64`.

### CPU flags and features

The hook calculates the intersection of CPU capability sets for the online logical CPUs and publishes the result in `cpu_flag`. Thus a flag is advertised only if it is available to all online CPUs represented by the host vnode.

The source field is architecture-specific:

| Architecture | `/proc/cpuinfo` field | Examples |
| --- | --- | --- |
| x86-64 | `flags` | `sse4_2`, `avx`, `avx2`, `fma`, `avx512f` |
| AArch64 | `Features` | `fp`, `asimd`, `crc32`, `atomics`, `sve`, `sve2`, `bf16`, `i8mm` |

The hook does not translate Linux flag names into compiler or marketing names.

### CPU ISA discovery

`cpu_isa` is a `string_array` PBS resource, but the discovery hook publishes exactly one token: the highest ISA baseline it can safely detect on the host.

On x86-64, the hook uses the x86-64 psABI microarchitecture levels:

- `x86-64-v1`
- `x86-64-v2`
- `x86-64-v3`
- `x86-64-v4`

The highest cumulative level whose required Linux CPU flags are present is published.

On AArch64, the hook derives ISA support conservatively from the Linux `Features` exposed in `/proc/cpuinfo`. It recognizes the Armv8-A baseline and higher levels when their required userspace-visible feature sets establish them. Armv9 is treated as a branch from the Armv8.5-A baseline rather than as a simple successor of Armv8.9-A: an Armv8.5-A feature baseline plus `sve` and `sve2` establishes `armv9-a`, while the corresponding Armv8.6-A additions `bf16` and `i8mm` establish `armv9.1-a`.

Not every Arm architectural revision can be distinguished solely from Linux HWCAP/`Features` information. For example, the compiler architecture definition for Armv8.2-A does not add a distinct default feature bundle over Armv8.1-A. The hook therefore does not infer an architectural revision just because it is numerically newer. This can make ARM ISA discovery conservative on CPUs whose exact architecture revision is not uniquely represented by Linux-exposed features, but it avoids advertising an ISA baseline that cannot be established safely.

The compatibility relationships between ISA tokens are deliberately not used by this hook. The `cpu_isa` configuration block belongs to `hook_normalize_job_cpuisa` and provides its compatibility map for expansion of user requests such as `compat[...]`.

### CPU specification

`cpu_spec` is a site-defined floating-point performance/classification value. The hook first checks `cpu_spec_map` entries by processor-model wildcard and otherwise uses `default_cpu_spec`.

### Memory discovery

The hook reads memory information from `/proc/meminfo`.

`memory_reserve` is subtracted from usable physical memory before publishing `mem`. If `publish_vmem` is true, `vmem` is also published from usable memory plus swap according to the implementation.

### Configuration fields

The configuration file is shared with `hook_normalize_job_cpuisa`. Not every field in it is consumed by `hook_discovery_cpus`.

| Field | Used by this hook | Description |
| --- | --- | --- |
| `memory_reserve` | yes | Memory kept unavailable to jobs for the operating system and services. |
| `publish_vmem` | yes | Whether the hook also updates the standard `vmem` resource. |
| `cpu_vendor_map` | yes | Ordered wildcard rules mapping native CPU vendor identifiers to site aliases and optionally to `cpu_arch`. First match wins. |
| `default_cpu_spec` | yes | Default numerical `cpu_spec` value. |
| `cpu_spec_map` | yes | Ordered wildcard rules overriding `cpu_spec` for selected CPU models. |
| `state_file` | no | Shared setting reserved for `hook_normalize_job_cpuisa`. |
| `cpu_isa` | no | ISA compatibility map used by `hook_normalize_job_cpuisa`; it is not used for ISA discovery. |

### PBS resources

The supplied `.qmgr` file defines the following custom resources:

| Resource | Type | Flags | Meaning |
| --- | --- | --- | --- |
| `cpu_model` | `string` | `h` | Processor model. |
| `cpu_vendor` | `string` | `h` | Site-normalized CPU vendor. |
| `cpu_arch` | `string_array` | `ho` | CPU execution architecture; one token is published, for example `x86_64` or `aarch64`. |
| `cpu_isa` | `string_array` | `ho` | Highest supported ISA baseline detected on the host; one token is published. |
| `cpu_flag` | `string_array` | `ha` | CPU capability flags/features common to the host. |
| `cpu_spec` | `float` | `hl` | Site-defined CPU performance/classification value. |
| `nthreads` | `long` | `hn` | Number of logical CPUs. |
| `smt` | `boolean` | `h` | Whether simultaneous multithreading is available. |
| `hybrid_cpu` | `boolean` | `h` | Whether SMT sibling counts differ between cores. |
| `npus_per_core` | `string` | `h` | Logical processing units per physical core when uniform. |

The hook also updates the standard vnode resources `ncpus`, `mem`, and optionally `vmem`.

### Administration notes

Because the hook establishes the cluster-wide meaning of `ncpus`, all execution hooks and scheduler configuration must use the same physical-core interpretation.

`cpu_arch`, `cpu_isa`, and `cpu_flag` serve different purposes and should remain independent:

- `cpu_arch` identifies the processor/ABI family;
- `cpu_isa` provides a coarse ISA compatibility baseline;
- `cpu_flag` represents individual capabilities and extensions.

Changes to architecture naming, CPU vendor aliases, ISA compatibility maps, CPU flags, or `cpu_spec` should be coordinated with scheduler resource configuration, `hook_normalize_job_cpuisa`, and user documentation.
