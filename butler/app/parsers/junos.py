"""Juniper Junos 'show' output parsers.

NOT VERIFIED AGAINST REAL HARDWARE. Written from Juniper's documented output
formats and tested only against hand-written samples in dev/samples/ (see
dev/regress.py R21) — the same caveat, and the same reason, as parsers/eos.py.
Known gaps: no serial number (it lives in 'show chassis hardware'), no
management IP from LLDP, no error counters, one address per interface.

Like ios.py, every parser fails soft: unrecognised input gives an empty result.
"""

import re

from . import ios

# ---------------------------------------------------------------------------
# show version
# ---------------------------------------------------------------------------

_HOSTNAME_RE = re.compile(r"^Hostname:[ \t]*(\S+)", re.MULTILINE)
_MODEL_RE = re.compile(r"^Model:[ \t]*(\S+)", re.MULTILINE)
_VERSION_RE = re.compile(r"^Junos:[ \t]*(\S+)", re.MULTILINE)


def parse_version(text):
    host = _HOSTNAME_RE.search(text)
    model = _MODEL_RE.search(text)
    version = _VERSION_RE.search(text)
    return {
        "model": model.group(1) if model else None,
        "serial": None,
        "os_version": version.group(1) if version else None,
        "hostname_hint": host.group(1) if host else None,
    }


# ---------------------------------------------------------------------------
# show interfaces terse
# ---------------------------------------------------------------------------

_INTF_RE = re.compile(
    r"^(?P<name>\S+)[ \t]+(?P<admin>up|down)[ \t]+(?P<link>up|down)"
    r"(?:[ \t]+(?P<proto>\S+)(?:[ \t]+(?P<local>\S+))?)?[ \t]*",
    re.MULTILINE,
)


def parse_interfaces(text):
    out = []
    for m in _INTF_RE.finditer(text):
        entry = {
            "name": m.group("name"), "admin_status": m.group("admin"), "oper_status": m.group("link"),
            "description": "", "ip": None, "prefix_len": None, "network": None,
            "speed": None, "duplex": None, "mtu": None, "input_errors": None, "crc_errors": None,
        }
        if m.group("proto") == "inet" and m.group("local"):
            addr, _, prefix = m.group("local").partition("/")
            if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", addr):
                entry["ip"] = addr
                entry["prefix_len"] = int(prefix) if prefix.isdigit() else 32
                entry["network"] = ios._network(addr, entry["prefix_len"])
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# show lldp neighbors
# ---------------------------------------------------------------------------

_LLDP_RE = re.compile(
    r"^(?P<local>\S+)[ \t]+(?P<parent>\S+)[ \t]+(?P<chassis>\S+)[ \t]+(?P<port>\S+)[ \t]+(?P<sys>\S.*?)[ \t]*$",
    re.MULTILINE,
)


def parse_lldp_neighbors(text):
    out = []
    for m in _LLDP_RE.finditer(text):
        if m.group("local") == "Local":     # header line
            continue
        out.append({
            "local_if": m.group("local"),
            "remote_chassis": m.group("chassis"),
            "remote_port": m.group("port"),
            "remote_sysname": m.group("sys"),
            "remote_mgmt_ip": None,
        })
    return out


# ---------------------------------------------------------------------------
# show bgp summary
# ---------------------------------------------------------------------------

_BGP_RE = re.compile(
    r"^(?P<peer>\d{1,3}(?:\.\d{1,3}){3})[ \t]+(?P<asn>\d+)[ \t]+\d+[ \t]+\d+[ \t]+\d+[ \t]+\d+[ \t]+"
    r"(?P<up>\S+)[ \t]+(?P<state>\S+)[ \t]*$",
    re.MULTILINE,
)


def parse_bgp_summary(text):
    out = []
    for m in _BGP_RE.finditer(text):
        state = m.group("state")
        # Established peers show "Establ" or the per-table counts "1/1/1/0".
        if state.lower().startswith("establ") or re.fullmatch(r"\d+/\d+/\d+/\d+", state):
            state = "Established"
        out.append({
            "peer_ip": m.group("peer"), "remote_as": int(m.group("asn")), "state": state,
            "prefixes": None, "uptime": m.group("up"),
        })
    return out


# ---------------------------------------------------------------------------
# show ospf neighbor
# ---------------------------------------------------------------------------

_OSPF_RE = re.compile(
    r"^(?P<addr>\d{1,3}(?:\.\d{1,3}){3})[ \t]+(?P<intf>\S+)[ \t]+(?P<state>\S+)[ \t]+"
    r"\d{1,3}(?:\.\d{1,3}){3}[ \t]+\d+[ \t]+\d+",
    re.MULTILINE,
)


def parse_ospf_neighbors(text):
    return [{"peer_ip": m.group("addr"), "state": m.group("state").upper(), "local_if": m.group("intf")}
            for m in _OSPF_RE.finditer(text)]


# ---------------------------------------------------------------------------
# show configuration | display set
# ---------------------------------------------------------------------------

_SET_RE = re.compile(r"^(set|deactivate|protect|activate) ")


def parse_running_config(text):
    """The `display set` form, one command per line. "" when any line is not
    one (an error such as 'syntax error' must never be stored as a backup)."""
    kept = [ln.rstrip() for ln in text.replace("\r", "").split("\n") if ln.strip()]
    if not kept or not all(_SET_RE.match(ln) for ln in kept):
        return ""
    return "\n".join(kept) + "\n"
