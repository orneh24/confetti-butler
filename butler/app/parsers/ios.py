"""Cisco IOS/IOS-XE 'show' output parsers.

Pure regex, deliberately not netmiko's use_textfsm=True. TextFSM needs the
ntc-templates package, and constraint 14 in confetti-traffic's CLAUDE.md is a
reminder that an Alpine package assumption can silently fail to hold — this
avoids that dependency entirely, same choice confetti-traffic itself made
everywhere (fping/traceroute/smbclient output, all regex).

Device output is untrusted input (see confetti-butler's own constraint on this):
every function here fails soft — an unrecognised or empty response returns
an empty result, never an exception, so one bad command in a task list never
takes down the rest of a device's poll cycle.
"""

import re

# ---------------------------------------------------------------------------
# show version
# ---------------------------------------------------------------------------

_MODEL_RE = re.compile(r"^[Cc]isco\s+(\S+)\s*\(", re.MULTILINE)
_SERIAL_RE = re.compile(r"Processor board ID\s+(\S+)")
_VERSION_RE = re.compile(r"Version\s+([\w.()]+),\s*RELEASE")
_HOSTNAME_HINT_RE = re.compile(r"^(\S+)\s+uptime is", re.MULTILINE)


def parse_version(text):
    """Extract model, serial and OS version from 'show version'.

    Returns a dict with possibly-None values rather than raising — a
    platform whose banner doesn't match one of these patterns should still
    let the rest of the version task succeed with whatever it did find.
    """
    model = _MODEL_RE.search(text)
    serial = _SERIAL_RE.search(text)
    version = _VERSION_RE.search(text)
    hostname_hint = _HOSTNAME_HINT_RE.search(text)
    return {
        "model": model.group(1) if model else None,
        "serial": serial.group(1) if serial else None,
        "os_version": version.group(1) if version else None,
        "hostname_hint": hostname_hint.group(1) if hostname_hint else None,
    }


# ---------------------------------------------------------------------------
# show interfaces (full detail — one command covers status, IP, speed/duplex,
# errors and MTU, so nothing here depends on 'show ip interface brief' too)
# ---------------------------------------------------------------------------

_INTF_HEADER_RE = re.compile(
    r"^(?P<name>\S+) is (?P<admin>(?:administratively )?down|up),"
    r" line protocol is (?P<oper>up|down)",
    re.MULTILINE,
)
_DESCRIPTION_RE = re.compile(r"Description:\s*(.+)")
_IP_RE = re.compile(r"Internet address is (?P<ip>\d{1,3}(?:\.\d{1,3}){3})/(?P<prefix>\d+)")
_MTU_RE = re.compile(r"MTU (\d+) bytes")
_DUPLEX_SPEED_RE = re.compile(
    r"(?P<duplex>Full|Half|Auto)[- ]?[Dd]uplex,\s*(?P<speed>\d+\s*[MGmg]b(?:ps|/s))"
)
_ERRORS_RE = re.compile(r"(?P<in_errors>\d+) input errors,\s*(?P<crc>\d+) CRC")


def parse_interfaces(text):
    """Parse 'show interfaces' into one dict per interface.

    Each entry: name, admin_status (up/down), oper_status (up/down),
    description, ip, prefix_len, network, speed, duplex, mtu, input_errors,
    crc_errors. A field this device's platform doesn't emit (no
    'Description:' line, no L3 address on a trunk parent) stays None/""
    rather than raising — see parse_version's reasoning.
    """
    headers = list(_INTF_HEADER_RE.finditer(text))
    out = []
    for i, m in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        chunk = text[m.start():end]

        admin = "down" if "down" in m.group("admin") else "up"
        entry = {
            "name": m.group("name"),
            "admin_status": admin,
            "oper_status": m.group("oper"),
            "description": "",
            "ip": None,
            "prefix_len": None,
            "network": None,
            "speed": None,
            "duplex": None,
            "mtu": None,
            "input_errors": None,
            "crc_errors": None,
        }

        desc = _DESCRIPTION_RE.search(chunk)
        if desc:
            entry["description"] = desc.group(1).strip()

        ip = _IP_RE.search(chunk)
        if ip:
            entry["ip"] = ip.group("ip")
            entry["prefix_len"] = int(ip.group("prefix"))
            entry["network"] = _network(ip.group("ip"), int(ip.group("prefix")))

        mtu = _MTU_RE.search(chunk)
        if mtu:
            entry["mtu"] = int(mtu.group(1))

        ds = _DUPLEX_SPEED_RE.search(chunk)
        if ds:
            entry["duplex"] = ds.group("duplex")
            entry["speed"] = ds.group("speed")

        err = _ERRORS_RE.search(chunk)
        if err:
            entry["input_errors"] = int(err.group("in_errors"))
            entry["crc_errors"] = int(err.group("crc"))

        out.append(entry)
    return out


def _network(ip, prefix_len):
    """CIDR network address for an interface IP — no ipaddress import needed
    at call sites; ipam.py's overlap detection re-derives from this."""
    try:
        octets = [int(o) for o in ip.split(".")]
        bits = (octets[0] << 24) | (octets[1] << 16) | (octets[2] << 8) | octets[3]
        mask = (0xFFFFFFFF << (32 - prefix_len)) & 0xFFFFFFFF if prefix_len else 0
        net = bits & mask
        return "{}.{}.{}.{}/{}".format(
            (net >> 24) & 0xFF, (net >> 16) & 0xFF, (net >> 8) & 0xFF, net & 0xFF, prefix_len
        )
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# show running-config
# ---------------------------------------------------------------------------

# Lines that change on their own between two identical configs. Left in, every
# poll after a `write memory` would store a "new" version.
_CONFIG_VOLATILE_RE = re.compile(
    r"^(Building configuration|Current configuration\s*:|Using \d+ out of \d+ bytes|"
    r"!\s*(Last configuration change|NVRAM config last updated|No configuration change)|"
    r"ntp clock-period\b)",
    re.I,
)


def parse_running_config(text):
    """Return the config with volatile lines and trailing blanks removed (CLAUDE.md constraint 14).

    Returns "" when the output isn't a config (no `version` line or no
    closing `end`) — an error message or truncated read must never be stored
    as a backup.
    """
    lines = [ln.rstrip() for ln in text.replace("\r", "").split("\n")]
    kept = [ln for ln in lines if not _CONFIG_VOLATILE_RE.match(ln)]
    while kept and not kept[0].strip():
        kept.pop(0)
    while kept and not kept[-1].strip():
        kept.pop()
    if not any(ln.startswith("version ") for ln in kept) or not kept or kept[-1].strip() != "end":
        return ""
    return "\n".join(kept) + "\n"
