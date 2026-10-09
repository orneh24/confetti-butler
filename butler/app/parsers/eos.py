"""Arista EOS 'show' output parsers.

EOS prints interfaces much like IOS, so parse_interfaces is IOS's. The other
formats differ and are parsed here.

NOT VERIFIED AGAINST REAL HARDWARE. These were written from Arista's
documented output formats and tested only against hand-written samples in
dev/samples/ (see dev/regress.py R21). CLAUDE.md constraint 3 is the reason
for the warning: IOS-XE command names and output shapes were "known" too,
until a real CSR1000v disagreed. Check each task against a real EOS device
before relying on it.

Like ios.py, every parser fails soft: unrecognised input gives an empty result.
"""

import re

from . import ios

parse_interfaces = ios.parse_interfaces

# ---------------------------------------------------------------------------
# show version
# ---------------------------------------------------------------------------

_MODEL_RE = re.compile(r"^Arista[ \t]+(\S+)", re.MULTILINE)
_SERIAL_RE = re.compile(r"^Serial number:[ \t]*(\S+)", re.MULTILINE)
_VERSION_RE = re.compile(r"^Software image version:[ \t]*(\S+)", re.MULTILINE)


def parse_version(text):
    model = _MODEL_RE.search(text)
    serial = _SERIAL_RE.search(text)
    version = _VERSION_RE.search(text)
    return {
        "model": model.group(1) if model else None,
        "serial": serial.group(1) if serial else None,
        "os_version": version.group(1) if version else None,
        "hostname_hint": None,   # not in 'show version'
    }


# ---------------------------------------------------------------------------
# show lldp neighbors detail
# ---------------------------------------------------------------------------

_LLDP_IF_RE = re.compile(r"^Interface (\S+) detected \d+ LLDP neighbors?:", re.MULTILINE)
_LLDP_NEIGHBOR_SPLIT_RE = re.compile(r"^[ \t]*Neighbor ", re.MULTILINE)
_LLDP_CHASSIS_RE = re.compile(r"Chassis ID[ \t]*:[ \t]*\"?([^\"\n]+?)\"?[ \t]*$", re.MULTILINE)
_LLDP_PORT_RE = re.compile(r"Port ID[ \t]*:[ \t]*\"?([^\"\n]+?)\"?[ \t]*$", re.MULTILINE)
_LLDP_SYSNAME_RE = re.compile(r"System Name:[ \t]*\"?([^\"\n]+?)\"?[ \t]*$", re.MULTILINE)
_LLDP_MGMT_RE = re.compile(r"Management Address[ \t]*:[ \t]*(\d{1,3}(?:\.\d{1,3}){3})")


def parse_lldp_neighbors(text):
    out = []
    heads = list(_LLDP_IF_RE.finditer(text))
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        for block in _LLDP_NEIGHBOR_SPLIT_RE.split(text[h.end():end])[1:]:
            chassis = _LLDP_CHASSIS_RE.search(block)
            if not chassis:
                continue
            port = _LLDP_PORT_RE.search(block)
            sysname = _LLDP_SYSNAME_RE.search(block)
            mgmt = _LLDP_MGMT_RE.search(block)
            out.append({
                "local_if": h.group(1),
                "remote_chassis": chassis.group(1),
                "remote_port": port.group(1) if port else None,
                "remote_sysname": sysname.group(1) if sysname else None,
                "remote_mgmt_ip": mgmt.group(1) if mgmt else None,
            })
    return out


# ---------------------------------------------------------------------------
# show ip bgp summary
# ---------------------------------------------------------------------------

_BGP_RE = re.compile(
    r"^[ \t]+(?P<peer>\d{1,3}(?:\.\d{1,3}){3})[ \t]+\d[ \t]+(?P<asn>\d+)[ \t]+\d+[ \t]+\d+[ \t]+\d+[ \t]+\d+[ \t]+"
    r"(?P<up>\S+)[ \t]+(?P<state>\S+)(?:[ \t]+(?P<pfx>\d+))?",
    re.MULTILINE,
)


def parse_bgp_summary(text):
    out = []
    for m in _BGP_RE.finditer(text):
        state = "Established" if m.group("state").lower().startswith("estab") else m.group("state")
        out.append({
            "peer_ip": m.group("peer"),
            "remote_as": int(m.group("asn")),
            "state": state,
            "prefixes": int(m.group("pfx")) if m.group("pfx") and state == "Established" else None,
            "uptime": m.group("up"),
        })
    return out


# ---------------------------------------------------------------------------
# show ip ospf neighbor
# ---------------------------------------------------------------------------

_OSPF_RE = re.compile(
    r"^(?P<rid>\d{1,3}(?:\.\d{1,3}){3})[ \t]+\d+[ \t]+\S+[ \t]+\d+[ \t]+(?P<state>[A-Za-z0-9-]+)(?:/\S+)?[ \t]+"
    r"\S+[ \t]+(?P<addr>\d{1,3}(?:\.\d{1,3}){3})[ \t]+(?P<intf>\S+)",
    re.MULTILINE,
)


def parse_ospf_neighbors(text):
    return [{"peer_ip": m.group("addr"), "state": m.group("state").upper(), "local_if": m.group("intf")}
            for m in _OSPF_RE.finditer(text)]


# ---------------------------------------------------------------------------
# show running-config
# ---------------------------------------------------------------------------

_VOLATILE_RE = re.compile(r"^![ \t]*(Command:|device:|Time:)", re.I)


def parse_running_config(text):
    """Config without the header comments EOS regenerates on every read;
    "" unless it is complete (ends with `end`)."""
    kept = [ln.rstrip() for ln in text.replace("\r", "").split("\n") if not _VOLATILE_RE.match(ln)]
    while kept and not kept[0].strip():
        kept.pop(0)
    while kept and not kept[-1].strip():
        kept.pop()
    if len(kept) < 3 or kept[-1].strip() != "end":
        return ""
    return "\n".join(kept) + "\n"
