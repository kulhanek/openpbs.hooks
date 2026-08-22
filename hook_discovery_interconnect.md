# `hook_discovery_interconnect`

## Overview

`hook_discovery_interconnect` discovers usable Ethernet and RDMA-capable interconnects on each execution host and publishes their type and link speed as vnode resources.

It distinguishes ordinary Ethernet, native InfiniBand, and RoCE (RDMA over Ethernet), and provides both a general interconnect classification and speed resources suitable for scheduling constraints.

## User documentation

Users can request nodes with a required interconnect type, for example:

```bash
#PBS -l select=2:ncpus=32:interconnect=ib
```

or require RDMA-capable hosts:

```bash
#PBS -l select=2:ncpus=32:rdma=true
```

Speed resources can also be used as scheduling constraints where appropriate for the site's scheduler configuration. Speeds are represented in megabits per second (Mb/s).

The hook reports node capabilities only. It does not configure MPI, choose an MPI fabric provider, establish network routes, or guarantee that an application will use the fastest detected interface. Users must still configure their communication library as required by the software stack.

## Technical and administration documentation

### Hook events

The supplied `hook_discovery_interconnect.qmgr` installs the hook for:

- `exechost_startup`
- `exechost_periodic`

The default periodic interval is 4000 seconds and the hook order is 40.

### Ethernet discovery

Ethernet interfaces are inspected under `/sys/class/net`. Interfaces can be excluded by wildcard patterns, and the supplied configuration ignores loopback and common virtual/container interfaces such as `docker*`, `veth*`, `virbr*`, `br-*`, `tun*`, and `tap*`.

When `require_interface_up` is true, only interfaces in an operationally up state are considered. The hook uses Linux interface type information to identify ordinary Ethernet devices and reads link speed from sysfs where available.

### RDMA discovery

RDMA devices are inspected under `/sys/class/infiniband`. Administrators may exclude devices by wildcard. When `require_rdma_active` is true, only active RDMA ports contribute.

The port link layer determines the published type:

- `InfiniBand` -> `ib`;
- `Ethernet` -> `roce`.

Reported RDMA rates are normalized to Mb/s.

### Published speed values

The hook publishes the maximum detected speed for each relevant category:

- `eth_speed`: maximum ordinary Ethernet speed;
- `ib_speed`: maximum native InfiniBand speed;
- `interconnect_speed`: maximum speed among detected Ethernet, InfiniBand, and RoCE links.

RoCE contributes to `interconnect_speed` and to `rdma=true`, but there is no separate `roce_speed` resource in the supplied resource specification.

### Configuration

The supplied configuration contains:

```json
{
    "exclude_interfaces": [
        "lo",
        "docker*",
        "veth*",
        "virbr*",
        "br-*",
        "tun*",
        "tap*"
    ],
    "exclude_rdma_devices": [],
    "require_interface_up": true,
    "require_rdma_active": true
}
```

| Field | Description |
| --- | --- |
| `exclude_interfaces` | Wildcard patterns for network interfaces that must not be advertised. |
| `exclude_rdma_devices` | Wildcard patterns for RDMA devices that must not be advertised. |
| `require_interface_up` | Ignore Ethernet interfaces that are not up. |
| `require_rdma_active` | Ignore RDMA ports that are not active. |

If the hook configuration cannot be loaded, the implementation logs a warning and uses its built-in defaults.

### PBS resources

The supplied `.qmgr` file defines:

| Resource | Type | Flags | Meaning |
| --- | --- | --- | --- |
| `interconnect` | `string_array` | `h` | Detected interconnect classes: `ethernet`, `ib`, and/or `roce`. |
| `interconnect_speed` | `long` | `hl` | Maximum detected interconnect speed in Mb/s. |
| `rdma` | `boolean` | `h` | True when an active native InfiniBand or RoCE RDMA path is detected. |
| `eth_speed` | `long` | `hl` | Maximum ordinary Ethernet speed in Mb/s. |
| `ib_speed` | `long` | `hl` | Maximum native InfiniBand speed in Mb/s. |

The hook explicitly clears values that are no longer detected so that stale network capabilities are not left on vnodes.
