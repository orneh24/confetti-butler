"""Per-platform commands and parsers.

A device's `platform` column picks its entry here. The poller itself does not
care: every platform produces the same parsed shapes for the same task names
(version, interfaces, lldp, bgp, ospf, config), so the apply steps, events and
topology work unchanged for all of them.

Status of each platform:
  cisco_ios / cisco_xe  verified against real IOS-XE 17.3 and 15.4 hardware
  arista_eos            NOT verified against real hardware (hand-written samples only)
  juniper_junos         NOT verified against real hardware (hand-written samples only)
  snmp                  SNMP v1/v2c via net-snmp, version + interfaces only; verified against
                        a real snmpd in an Alpine container

An unknown platform string falls back to the IOS entry, as before this module
existed (netmiko is still given the platform string as its device_type).
"""

from .parsers import eos
from .parsers import ios
from .parsers import junos

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

EOS = {
    "transport": "ssh",
    "commands": {
        "version": "show version",
        "interfaces": "show interfaces",
        "lldp": "show lldp neighbors detail",
        "bgp": "show ip bgp summary",
        "ospf": "show ip ospf neighbor",
        "config": "show running-config",
    },
    "parsers": {
        "version": eos.parse_version,
        "interfaces": eos.parse_interfaces,
        "lldp": eos.parse_lldp_neighbors,
        "bgp": eos.parse_bgp_summary,
        "ospf": eos.parse_ospf_neighbors,
        "config": eos.parse_running_config,
    },
}

JUNOS = {
    "transport": "ssh",
    "commands": {
        "version": "show version",
        "interfaces": "show interfaces terse",
        "lldp": "show lldp neighbors",
        "bgp": "show bgp summary",
        "ospf": "show ospf neighbor",
        "config": "show configuration | display set",
    },
    "parsers": {
        "version": junos.parse_version,
        "interfaces": junos.parse_interfaces,
        "lldp": junos.parse_lldp_neighbors,
        "bgp": junos.parse_bgp_summary,
        "ospf": junos.parse_ospf_neighbors,
        "config": junos.parse_running_config,
    },
}

SNMP = {"transport": "snmp", "tasks": ("version", "interfaces")}

PLATFORMS = {
    "cisco_ios": IOS,
    "cisco_xe": IOS,
    "arista_eos": EOS,
    "juniper_junos": JUNOS,
    "snmp": SNMP,
}


def get(platform):
    return PLATFORMS.get(platform or "cisco_ios", IOS)


def tasks(plat):
    return tuple(plat["commands"]) if plat["transport"] == "ssh" else plat["tasks"]
