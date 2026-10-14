"""Per-platform commands and parsers.

A device's `platform` column picks its entry here. The poller itself does not
care: every platform produces the same parsed shapes for the same task names
(version, interfaces, config), so the apply steps and events work unchanged for
all of them. (LLDP/BGP/OSPF polling moved to confetti-topo on 2026-10-14.) Adding a vendor
means adding an entry here plus its parsers (Junos is planned, see docs/ROADMAP.md).

Status of each platform:
  cisco_ios / cisco_xe  verified against real IOS-XE 17.3 and 15.4 hardware
  snmp                  SNMP v1/v2c via net-snmp, version + interfaces only; verified against
                        a real snmpd in an Alpine container

An unknown platform string falls back to the IOS entry, as before this module
existed (netmiko is still given the platform string as its device_type).
"""

from .parsers import ios

IOS = {
    "transport": "ssh",
    "commands": {
        "version": "show version",
        "interfaces": "show interfaces",
        # Last, so a slow config read never delays the other tasks.
        "config": "show running-config",
    },
    "parsers": {
        "version": ios.parse_version,
        "interfaces": ios.parse_interfaces,
        "config": ios.parse_running_config,
    },
}

SNMP = {"transport": "snmp", "tasks": ("version", "interfaces")}

PLATFORMS = {
    "cisco_ios": IOS,
    "cisco_xe": IOS,
    "snmp": SNMP,
}


def get(platform):
    return PLATFORMS.get(platform or "cisco_ios", IOS)


def tasks(plat):
    return tuple(plat["commands"]) if plat["transport"] == "ssh" else plat["tasks"]
