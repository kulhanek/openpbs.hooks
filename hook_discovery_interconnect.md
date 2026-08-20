# `hook_discovery_interconnect`

## Overview

`hook_discovery_interconnect` is an OpenPBS execution-host discovery hook that
publishes scheduler-visible information about the node's high-speed network
interconnects.

The hook discovers:

- active Ethernet interfaces,
- active native InfiniBand ports,
- active RDMA-over-Ethernet (RoCE) ports,
- the maximum Ethernet link speed,
- the maximum native InfiniBand link speed,
- the maximum speed of any discovered scheduler-relevant interconnect, and
- whether an active RDMA-capable port is available.

The hook runs on `execjob` hosts for the `exechost_startup` and
`exechost_periodic` events and updates `resources_available` on the local vnode.

The design intentionally exposes scheduling capabilities rather than detailed
network inventory. Interface names, GUIDs, LIDs, MTUs, and similar diagnostic
attributes are not published as PBS resources.

## PBS resources

The following custom resources are used.

| Resource | Type | Example | Meaning |
|---|---|---:|---|
| `interconnect` | `string_array` | `ethernet,ib` | Interconnect technologies currently available on the vnode. Possible values are `ethernet`, `ib`, and `roce`. |
| `interconnect_speed` | `long` | `200000` | Maximum speed, in Mbit/s, across all discovered active interconnects. |
| `rdma` | `boolean` | `True` | `True` if at least one active RDMA port is available. |
| `eth_speed` | `long` | `25000` | Maximum speed, in Mbit/s, among active Ethernet interfaces. |
| `ib_speed` | `long` | `200000` | Maximum speed, in Mbit/s, among active native InfiniBand ports. |

`interconnect_speed`, `eth_speed`, and `ib_speed` use Mbit/s so that the values
can be represented directly as integer PBS resources. For example:

- 1 Gb/s -> `1000`
- 25 Gb/s -> `25000`
- 100 Gb/s -> `100000`
- 200 Gb/s -> `200000`

### Meaning of `interconnect`

`interconnect` is a `string_array`. A vnode may therefore advertise multiple
technologies simultaneously.

Examples:

```text
resources_available.interconnect = ethernet
resources_available.interconnect = ethernet,ib
resources_available.interconnect = ethernet,roce
```

The values have the following meanings:

- `ethernet` -- at least one usable Ethernet interface is present.
- `ib` -- at least one active native InfiniBand port is present.
- `roce` -- at least one active RDMA port whose RDMA `link_layer` is Ethernet is
  present.

RoCE therefore implies RDMA over Ethernet. A node with RoCE will normally have
both `ethernet` and `roce` in the `interconnect` resource.

### Meaning of `rdma`

`rdma=True` means that the hook found at least one active port below
`/sys/class/infiniband` with either:

```text
link_layer = InfiniBand
```

or:

```text
link_layer = Ethernet
```

Thus both native InfiniBand and RoCE make `rdma=True`.

### Meaning of the speed resources

`eth_speed` is derived from:

```text
/sys/class/net/<interface>/speed
```

for usable Ethernet interfaces.

`ib_speed` is derived from:

```text
/sys/class/infiniband/<device>/ports/<port>/rate
```

but only for ports whose `link_layer` is `InfiniBand`.

The RDMA sysfs `rate` value represents the aggregate port rate, for example:

```text
200 Gb/sec (4X HDR)
```

The hook converts this to Mbit/s.

`interconnect_speed` is the maximum of:

- `eth_speed`,
- the maximum active native InfiniBand port rate, and
- the maximum active RoCE port rate.

This makes `interconnect_speed` a summary of the fastest discovered
scheduler-relevant communication path.

## User examples

Request a node with native InfiniBand:

```bash
#PBS -l select=1:ncpus=32:interconnect=ib
```

Request two RDMA-capable nodes:

```bash
#PBS -l select=2:ncpus=32:rdma=True
```

Request a node with RoCE:

```bash
#PBS -l select=1:ncpus=32:interconnect=roce
```

A node can advertise both Ethernet and InfiniBand:

```text
resources_available.interconnect = ethernet,ib
resources_available.interconnect_speed = 200000
resources_available.rdma = True
resources_available.eth_speed = 25000
resources_available.ib_speed = 200000
```

### Speed selection

The speed resources are numeric PBS resources. They are primarily intended to
describe the discovered hardware and to permit numeric scheduler constraints
when desired.

For most jobs, selecting a capability such as:

```bash
interconnect=ib
```

or:

```bash
rdma=True
```

is expected to be more robust than selecting a specific nominal link speed.

## Detection logic

### Ethernet

The hook enumerates interfaces below:

```text
/sys/class/net
```

An interface is considered Ethernet when its Linux ARPHRD type is `1`.

By default, only interfaces whose:

```text
operstate = up
```

are considered.

Native IPoIB interfaces are not misclassified as Ethernet because they use a
different ARPHRD type.

The maximum positive integer reported by:

```text
/sys/class/net/<interface>/speed
```

is published as `eth_speed`.

Interfaces matching `exclude_interfaces` are ignored.

### InfiniBand and RoCE

RDMA hardware is detected from:

```text
/sys/class/infiniband
```

rather than from network-interface naming conventions such as `ib0`.

For each RDMA device and port, the hook reads:

```text
state
link_layer
rate
```

By default, only ports whose state is `ACTIVE` are considered.

A port with:

```text
link_layer = InfiniBand
```

contributes the `ib` capability and can contribute to `ib_speed`.

A port with:

```text
link_layer = Ethernet
```

contributes the `roce` capability. It also makes `rdma=True`, but it does not
contribute to `ib_speed`.

This avoids the fragile assumption used by older discovery code that an
InfiniBand interface can be identified merely because its interface name
contains `ib`.

## Configuration

The hook does not require a configuration file for normal operation. An
optional JSON configuration can be imported to exclude site-specific
interfaces or RDMA devices and to relax link-state requirements.

Example:

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

### Configuration items

| Item | Type | Default | Description |
|---|---|---|---|
| `exclude_interfaces` | array of strings | see example | Shell-style interface-name patterns ignored during Ethernet discovery. Matching uses `fnmatch`. |
| `exclude_rdma_devices` | array of strings | `[]` | Shell-style RDMA device-name patterns to ignore, for example `mlx5_2`. |
| `require_interface_up` | boolean | `true` | If `true`, only Ethernet interfaces with `operstate=up` are considered. |
| `require_rdma_active` | boolean | `true` | If `true`, only RDMA ports whose state is `ACTIVE` are considered. |

The default exclusions primarily prevent local virtual networking used by
containers, bridges, and tunnels from being advertised as a compute-node
interconnect.

Site-specific interfaces can be added, for example:

```json
{
    "exclude_interfaces": [
        "lo",
        "docker*",
        "veth*",
        "virbr*",
        "br-*",
        "tun*",
        "tap*",
        "mgmt*"
    ]
}
```

If the management interface is intentionally part of the scheduler-visible
interconnect, it should not be excluded.

## OpenPBS resource setup

Create the custom resources before enabling the hook:

```text
create resource interconnect type=string_array,flag=h
create resource interconnect_speed type=long,flag=h
create resource rdma type=boolean,flag=h
create resource eth_speed type=long,flag=h
create resource ib_speed type=long,flag=h
```

These commands are also provided in `hook_discovery_interconnect.qmgr`.

The resources should be added to the scheduler's `resources` list if they are
to participate in scheduling decisions.

For example, inspect the current scheduler configuration with:

```bash
qmgr -c "p s"
```

and add the desired resources according to the site's existing scheduler
resource configuration.

## Hook installation

A typical installation sequence is:

```bash
qmgr -c "create hook discovery_interconnect"
qmgr -c "set hook discovery_interconnect event = exechost_startup,exechost_periodic"
qmgr -c "set hook discovery_interconnect enabled = true"
qmgr -c "set hook discovery_interconnect freq = 300"
qmgr -c "import hook discovery_interconnect application/x-python default hook_discovery_interconnect.py"
qmgr -c "import hook discovery_interconnect application/json default hook_discovery_interconnect.json"
```

The exact hook name and periodic frequency can be adjusted to match local site
conventions.

If no configuration file is desired, the Python hook can be used without the
JSON import because it contains equivalent defaults.

## Behaviour on missing or disappearing resources

Periodic discovery explicitly clears speed and interconnect resources when the
corresponding capability is no longer detected. This prevents stale values from
remaining on a vnode after a link goes down or hardware is removed.

`rdma` is always explicitly set to either `True` or `False`.

For optional speed resources:

- a positive detected speed is published as an integer in Mbit/s;
- if no usable speed is available, the resource is set to `None`.

Likewise, `interconnect` is cleared when no supported active interconnect is
detected.

## Limitations

### Fabric membership is not detected

The hook describes capabilities of an individual vnode. It does not determine
whether two nodes belong to the same physical InfiniBand or Ethernet fabric.

If a PBS complex contains multiple isolated fabrics, a separate site-defined
resource such as:

```text
interconnect_fabric
```

may be useful to guarantee that selected nodes can communicate over the same
fabric.

### Link speed is not application throughput

The reported speeds are nominal link rates. They do not represent measured MPI
bandwidth, RDMA throughput, congestion, topology distance, or switch
oversubscription.

### Bonding and aggregated Ethernet

`eth_speed` is the largest speed reported by an individual usable Linux
Ethernet interface. The hook does not attempt to calculate effective bandwidth
from bonding, teaming, multipath routing, or switch configuration.

### RoCE speed

RoCE ports are identified from the RDMA subsystem by `link_layer=Ethernet`.
Their RDMA port rate contributes to `interconnect_speed`, while `ib_speed`
remains reserved for native InfiniBand.

### Scheduler topology

The hook does not model switch topology or locality. If topology-aware
scheduling is required, it should be implemented separately from these
per-vnode capability resources.
