"""Per-platform commands and parsers.

A device's `platform` column picks its entry here. The poller itself does not
care: every platform produces the same parsed shapes for the same task names
(version, interfaces, lldp, bgp, ospf, config), so the apply steps, events and
topology work unchanged for all of them. Adding a vendor means adding an entry
here plus its parsers (Junos is planned, see docs/ROADMAP.md).

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
        "lldp": "show lldp neighbors detail",
        "bgp": "show bgp summary",
        "ospf": "show ip ospf neighbor",
        # Last, so a slow config read never delays the other tasks.
        "config": "show running-config",
    },
    "parsers": {
        "version": ios.parse_version,
        "interfaces": ios.parse_interfaces,
        "lldp": ios.parse_lldp_neighbors,
        "bgp": ios.parse_bgp_summary,
        "ospf": ios.parse_ospf_neighbors,
        "config": ios.parse_running_config,
    },
    # Older IOS leaves "Local Intf" out of the detail output; this table has it.
    "lldp_brief_command": "show lldp neighbors",
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
