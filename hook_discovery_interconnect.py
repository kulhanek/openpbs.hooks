import fnmatch
import json
import os
import re

import pbs


class Discovery(object):
    hook_name = "discovery_interconnect"

    DEFAULT_CONFIG = {
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
        "require_interface_up": True,
        "require_rdma_active": True
    }

    def __init__(self, pbs_event):
        self.e = pbs_event
        self.vnl = pbs_event.vnode_list
        self.local_node = pbs.get_local_nodename()
        self.config = dict(self.DEFAULT_CONFIG)

        if self.vnl is None or self.local_node is None:
            pbs.logmsg(
                pbs.EVENT_DEBUG,
                "%s: failed to obtain local vnode information" % self.hook_name
            )
            self.e.accept()

        self._load_config()

    def _load_config(self):
        config_file = os.environ.get("PBS_HOOK_CONFIG_FILE")
        if not config_file:
            return

        try:
            with open(config_file, "r") as handle:
                user_config = json.load(handle)
        except Exception as err:
            pbs.logmsg(
                pbs.EVENT_WARNING,
                "%s: failed to load configuration '%s': %s"
                % (self.hook_name, config_file, str(err))
            )
            return

        for key in self.DEFAULT_CONFIG:
            if key in user_config:
                self.config[key] = user_config[key]

    @staticmethod
    def _read_text(path):
        try:
            with open(path, "r") as handle:
                return handle.read().strip()
        except (IOError, OSError):
            return None

    @staticmethod
    def _matches_any(name, patterns):
        for pattern in patterns:
            if fnmatch.fnmatch(name, pattern):
                return True
        return False

    @staticmethod
    def _parse_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_rate_mbps(value):
        """
        Parse Linux RDMA sysfs port rate, e.g.:
            100 Gb/sec (4X EDR)
            200 Gb/sec (4X HDR)
        Return aggregate link rate in Mbit/s.
        """
        if not value:
            return None

        match = re.search(
            r"([0-9]+(?:\.[0-9]+)?)\s*([MGT])b/sec",
            value,
            re.IGNORECASE
        )
        if not match:
            return None

        number = float(match.group(1))
        unit = match.group(2).upper()

        if unit == "M":
            multiplier = 1
        elif unit == "G":
            multiplier = 1000
        elif unit == "T":
            multiplier = 1000000
        else:
            return None

        return int(round(number * multiplier))

    def _interface_is_usable(self, iface):
        if self._matches_any(iface, self.config["exclude_interfaces"]):
            return False

        if not self.config.get("require_interface_up", True):
            return True

        operstate = self._read_text("/sys/class/net/%s/operstate" % iface)
        return operstate == "up"

    def _discover_ethernet(self):
        """
        Return:
            (ethernet_present, maximum_speed_mbps)

        Linux ARPHRD type 1 is Ethernet.  Native IPoIB interfaces use a
        different ARPHRD type and are therefore not counted as Ethernet.
        """
        net_root = "/sys/class/net"
        ethernet_present = False
        max_speed = 0

        try:
            interfaces = os.listdir(net_root)
        except (IOError, OSError):
            return False, None

        for iface in interfaces:
            if not self._interface_is_usable(iface):
                continue

            iftype = self._parse_int(
                self._read_text("%s/%s/type" % (net_root, iface))
            )
            if iftype != 1:
                continue

            ethernet_present = True

            speed = self._parse_int(
                self._read_text("%s/%s/speed" % (net_root, iface))
            )
            if speed is not None and speed > max_speed:
                max_speed = speed

        return ethernet_present, max_speed if max_speed > 0 else None

    def _rdma_port_is_active(self, rdma_dev, port):
        if not self.config.get("require_rdma_active", True):
            return True

        state = self._read_text(
            "/sys/class/infiniband/%s/ports/%s/state" % (rdma_dev, port)
        )
        if not state:
            return False

        # Linux reports ACTIVE as "4: ACTIVE".
        return state.startswith("4:") or state.upper() == "ACTIVE"

    def _discover_rdma(self):
        """
        Discover active RDMA ports through /sys/class/infiniband.

        Return:
            {
                "rdma": bool,
                "ib": bool,
                "roce": bool,
                "ib_speed": int|None,
                "roce_speed": int|None
            }

        link_layer == "InfiniBand" -> native InfiniBand
        link_layer == "Ethernet"   -> Ethernet RDMA (RoCE)
        """
        result = {
            "rdma": False,
            "ib": False,
            "roce": False,
            "ib_speed": None,
            "roce_speed": None
        }

        rdma_root = "/sys/class/infiniband"
        try:
            rdma_devices = os.listdir(rdma_root)
        except (IOError, OSError):
            return result

        for rdma_dev in rdma_devices:
            if self._matches_any(
                rdma_dev, self.config["exclude_rdma_devices"]
            ):
                continue

            ports_root = "%s/%s/ports" % (rdma_root, rdma_dev)
            try:
                ports = os.listdir(ports_root)
            except (IOError, OSError):
                continue

            for port in ports:
                if not self._rdma_port_is_active(rdma_dev, port):
                    continue

                link_layer = self._read_text(
                    "%s/%s/link_layer" % (ports_root, port)
                )
                rate = self._parse_rate_mbps(
                    self._read_text("%s/%s/rate" % (ports_root, port))
                )

                if link_layer == "InfiniBand":
                    result["rdma"] = True
                    result["ib"] = True
                    if rate is not None:
                        current = result["ib_speed"] or 0
                        result["ib_speed"] = max(current, rate)

                elif link_layer == "Ethernet":
                    result["rdma"] = True
                    result["roce"] = True
                    if rate is not None:
                        current = result["roce_speed"] or 0
                        result["roce_speed"] = max(current, rate)

        return result

    def _set_resource(self, name, value):
        try:
            self.vnl[self.local_node].resources_available[name] = value
            pbs.logmsg(
                pbs.EVENT_DEBUG,
                "%s: resources_available.%s = %s"
                % (self.hook_name, name, str(value))
            )
        except Exception as err:
            pbs.logmsg(
                pbs.EVENT_ERROR,
                "%s: failed to set resources_available.%s: %s"
                % (self.hook_name, name, str(err))
            )
            return False
        return True

    def discover(self):
        ethernet_present, eth_speed = self._discover_ethernet()
        rdma = self._discover_rdma()

        interconnect = []
        speeds = []

        if ethernet_present:
            interconnect.append("ethernet")
        if rdma["ib"]:
            interconnect.append("ib")
        if rdma["roce"]:
            interconnect.append("roce")

        if eth_speed is not None:
            speeds.append(eth_speed)
        if rdma["ib_speed"] is not None:
            speeds.append(rdma["ib_speed"])
        if rdma["roce_speed"] is not None:
            speeds.append(rdma["roce_speed"])

        interconnect_speed = max(speeds) if speeds else None

        # Explicitly clear resources when a capability disappears, so periodic
        # discovery does not leave stale values on the vnode.
        interconnect_value = ",".join(interconnect) if interconnect else None

        ok = True
        ok = self._set_resource("interconnect", interconnect_value) and ok
        ok = self._set_resource("interconnect_speed", interconnect_speed) and ok
        ok = self._set_resource("rdma", rdma["rdma"]) and ok
        ok = self._set_resource("eth_speed", eth_speed) and ok
        ok = self._set_resource("ib_speed", rdma["ib_speed"]) and ok

        return ok

    def run(self):
        if self.e.type not in (pbs.EXECHOST_STARTUP, pbs.EXECHOST_PERIODIC):
            pbs.logmsg(
                pbs.EVENT_DEBUG,
                "%s: unsupported hook event" % self.hook_name
            )
            return

        if not self.discover():
            pbs.logmsg(
                pbs.EVENT_WARNING,
                "%s: interconnect discovery completed with errors"
                % self.hook_name
            )


try:
    event = pbs.event()
    discovery = Discovery(event)
    discovery.run()
    event.accept()
except SystemExit:
    pass
except Exception as err:
    pbs.event().reject(
        "hook_discovery_interconnect failed: %s" % str(err)
    )
