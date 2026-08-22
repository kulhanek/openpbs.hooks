# `hook_discovery_cpus`

## Overview

`hook_discovery_cpus` discovers CPU topology, processor identity and capabilities, memory capacity, and several site-specific CPU classification resources on each execution host. It publishes this information as vnode resources for scheduling and for use by other execution hooks.

The hook distinguishes physical CPU cores from logical processing units. In this installation `ncpus` represents physical cores, while `nthreads` represents logical CPUs visible to the operating system.

## User documentation

The resources published by this hook allow users to request nodes with particular processor properties. Typical examples are:

```bash
# request an AMD node
#PBS -l select=1:ncpus=16:cpu_vendor=amd
```

```bash
# request a node supporting a particular CPU ISA level
#PBS -l select=1:ncpus=8:cpu_isa=x86-64-v3
```

```bash
# request a required CPU flag
#PBS -l select=1:ncpus=8:cpu_flag=avx2
```

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

### Processor identity and capabilities

`cpu_model` is derived from the processor model information. `cpu_vendor` is mapped through administrator-defined wildcard rules. The supplied configuration maps AMD and Intel model/vendor text to `amd` and `intel`.

`cpu_flag` contains flags common to the detected CPUs. `cpu_isa` contains only the highest supported x86-64 psABI ISA level detected for the host, for example `x86-64-v2`, `x86-64-v3`, or `x86-64-v4`. On non-x86-64 systems no x86-64 ISA value is published.

`cpu_spec` is a site-defined floating-point performance/classification value. The hook first checks `cpu_spec_map` entries by processor-model wildcard and otherwise uses `default_cpu_spec`.

### Memory discovery

The hook reads memory information from `/proc/meminfo`.

`memory_reserve` is subtracted from usable physical memory before publishing `mem`. If `publish_vmem` is true, `vmem` is also published from usable memory plus swap according to the implementation.

The supplied configuration contains:

```json
{
    "memory_reserve": "4GB",
    "publish_vmem": true,
    "default_cpu_spec": 30.0,
    "cpu_spec_map": [
        {
            "pattern": "*AMD EPYC 7402P*",
            "value": 40.1
        }
    ]
}
```

### Configuration fields

| Field | Description |
| --- | --- |
| `memory_reserve` | Memory kept unavailable to jobs for the operating system and services. |
| `publish_vmem` | Whether the hook also updates the standard `vmem` resource. |
| `cpu_vendor_map` | Ordered wildcard rules mapping detected vendor/model strings to site aliases. First match wins. |
| `default_cpu_spec` | Default numerical `cpu_spec` value. |
| `cpu_spec_map` | Ordered wildcard rules overriding `cpu_spec` for selected CPU models. |

Vendor mapping rules can also specify whether wildcard matching is case-sensitive.

### PBS resources

The supplied `.qmgr` file defines the following custom resources:

| Resource | Type | Flags | Meaning |
| --- | --- | --- | --- |
| `cpu_model` | `string` | `h` | Processor model. |
| `cpu_vendor` | `string` | `h` | Site-normalized CPU vendor. |
| `cpu_flag` | `string_array` | `ha` | CPU capability flags common to the host. |
| `cpu_spec` | `float` | `hl` | Site-defined CPU performance/classification value. |
| `cpu_isa` | `string_array` | `ho` | Highest supported x86-64 ISA level. |
| `nthreads` | `long` | `hn` | Number of logical CPUs. |
| `smt` | `boolean` | `h` | Whether simultaneous multithreading is available. |
| `hybrid_cpu` | `boolean` | `h` | Whether SMT sibling counts differ between cores. |
| `npus_per_core` | `string` | `h` | Logical processing units per physical core when uniform. |

The hook also updates the standard vnode resources `ncpus`, `mem`, and optionally `vmem`.

### Administration notes

Because the hook establishes the cluster-wide meaning of `ncpus`, all execution hooks and scheduler configuration must use the same physical-core interpretation. Changes to `cpu_spec`, CPU vendor aliases, or ISA/flag matching should therefore be coordinated with scheduler resource configuration and user documentation.
