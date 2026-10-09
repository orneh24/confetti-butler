"""SNMP collector for devices that cannot (or should not) be polled over SSH.

Shells out to net-snmp's snmpget / snmpwalk (apk: net-snmp-tools) rather than
a Python SNMP library — see CLAUDE.md Design Decisions. Select it by setting a
device's platform to "snmp". Read-only like every other collector.

Two tasks, the same names the SSH collector uses so the rest of the app does
not care where the data came from: `version` (sysName / sysDescr) and
`interfaces` (IF-MIB + the IPv4 address table). No LLDP, routing or config —
those need a CLI. SNMP v1 and v2c only; v3 needs per-user credentials this
app does not store, so it fails with a clear message.

The community string goes on the command line, so it is visible to other
users of the server VM (a single-purpose appliance) in the process list.
"""

import ipaddress
import re
import subprocess

from .. import config
from ..parsers import ios

SYS_DESCR = ".1.3.6.1.2.1.1.1.0"
SYS_NAME = ".1.3.6.1.2.1.1.5.0"

IF_DESCR = ".1.3.6.1.2.1.2.2.1.2"
IF_MTU = ".1.3.6.1.2.1.2.2.1.4"
IF_SPEED = ".1.3.6.1.2.1.2.2.1.5"
IF_ADMIN = ".1.3.6.1.2.1.2.2.1.7"
IF_OPER = ".1.3.6.1.2.1.2.2.1.8"
IF_IN_ERRORS = ".1.3.6.1.2.1.2.2.1.14"
IF_ALIAS = ".1.3.6.1.2.1.31.1.1.1.18"
IP_ADDR = ".1.3.6.1.2.1.4.20.1.1"
IP_IFINDEX = ".1.3.6.1.2.1.4.20.1.2"
IP_MASK = ".1.3.6.1.2.1.4.20.1.3"

TASKS = ("version", "interfaces")
TIMEOUT_S = 20

# "<oid> <value>" as printed by snmpget/snmpwalk -Oqn; the value may be quoted.
_LINE_RE = re.compile(r"^(\.[\d.]+)\s+(.*)$")


class SnmpError(Exception):
    pass


def _run(tool, community, version, host, *oids):
    cmd = [tool, "-v", version, "-c", community, "-Oqn", "-t", "3", "-r", "1", host] + list(oids)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S)
    except FileNotFoundError:
        raise SnmpError("{} not installed (apk add net-snmp-tools)".format(tool))
    except subprocess.TimeoutExpired:
        raise SnmpError("SNMP request timed out")
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip().splitlines()
        raise SnmpError(msg[0] if msg else "{} failed".format(tool))
    return r.stdout


def parse_pairs(text):
    """{oid: value} from -Oqn output. Values lose surrounding quotes."""
    out = {}
    for line in text.splitlines():
        m = _LINE_RE.match(line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"')
    return out


def table(pairs, base):
    """{index: value} for the rows of one column, keyed by the OID suffix."""
    prefix = base + "."
    return {oid[len(prefix):]: v for oid, v in pairs.items() if oid.startswith(prefix)}


def _status(value):
    # net-snmp prints "up", "up(1)" or "1" depending on options; accept all.
    v = value.lower()
    return "up" if v.startswith("up") or v == "1" else "down"


def _int(value):
    m = re.match(r"-?\d+", value or "")
    return int(m.group(0)) if m else None


def parse_version(pairs):
    descr = pairs.get(SYS_DESCR, "")
    ver = re.search(r"Version\s+([\w.()-]+)", descr)
    return {
        "model": None, "serial": None,
        "os_version": ver.group(1).rstrip(",") if ver else None,
        "hostname_hint": pairs.get(SYS_NAME) or None,
    }


def parse_interfaces(walks):
    """walks: {column OID: {oid: value}} from the interface and address tables."""
    names = table(walks[IF_DESCR], IF_DESCR)
    admin = table(walks[IF_ADMIN], IF_ADMIN)
    oper = table(walks[IF_OPER], IF_OPER)
    mtu = table(walks[IF_MTU], IF_MTU)
    speed = table(walks[IF_SPEED], IF_SPEED)
    errors = table(walks[IF_IN_ERRORS], IF_IN_ERRORS)
    alias = table(walks[IF_ALIAS], IF_ALIAS)

    addr_of = {}                       # ifIndex -> (ip, prefix)
    ifindex = table(walks[IP_IFINDEX], IP_IFINDEX)
    masks = table(walks[IP_MASK], IP_MASK)
    for ip, idx in ifindex.items():
        try:
            prefix = ipaddress.ip_network("0.0.0.0/" + masks.get(ip, "255.255.255.255")).prefixlen
        except ValueError:
            continue
        addr_of.setdefault(idx, (ip, prefix))

    out = []
    for idx, name in sorted(names.items(), key=lambda kv: _int(kv[0]) or 0):
        entry = {
            "name": name, "description": alias.get(idx, ""),
            "admin_status": _status(admin.get(idx, "2")), "oper_status": _status(oper.get(idx, "2")),
            "ip": None, "prefix_len": None, "network": None, "speed": None, "duplex": None,
            "mtu": _int(mtu.get(idx)), "input_errors": _int(errors.get(idx)), "crc_errors": None,
        }
        bps = _int(speed.get(idx))
        if bps:
            entry["speed"] = "{} Mbps".format(bps // 1000000) if bps >= 1000000 else "{} bps".format(bps)
        if idx in addr_of:
            ip, prefix = addr_of[idx]
            entry["ip"], entry["prefix_len"] = ip, prefix
            entry["network"] = ios._network(ip, prefix)
        out.append(entry)
    return out


def run_tasks(device, creds):
    """Same contract as ssh.run_tasks: yields (task, ok, raw_output, error, parsed)."""
    host = device.get("mgmt_ip")
    if not host:
        for task in TASKS:
            yield task, False, None, "device has no mgmt_ip", None
        return
    try:
        ipaddress.ip_address(host)
    except ValueError:
        for task in TASKS:
            yield task, False, None, "mgmt_ip is not an IP address", None
        return
    version = (creds.get("snmp_version") or config.DEFAULT_SNMP_VERSION or "2c").lower()
    community = creds.get("snmp_community") or config.DEFAULT_SNMP_COMMUNITY
    if version not in ("1", "2c"):
        for task in TASKS:
            yield task, False, None, "SNMP v{} is not supported (v1 and v2c only)".format(version), None
        return

    try:
        text = _run("snmpget", community, version, host, SYS_DESCR, SYS_NAME)
        yield "version", True, text, None, parse_version(parse_pairs(text))
    except SnmpError as exc:
        yield "version", False, None, str(exc), None

    try:
        raw = []
        walks = {}
        for col in (IF_DESCR, IF_MTU, IF_SPEED, IF_ADMIN, IF_OPER, IF_IN_ERRORS, IF_ALIAS,
                    IP_IFINDEX, IP_MASK):
            text = _run("snmpwalk", community, version, host, col)
            raw.append(text)
            walks[col] = parse_pairs(text)
        yield "interfaces", True, "".join(raw), None, parse_interfaces(walks)
    except SnmpError as exc:
        yield "interfaces", False, None, str(exc), None
