"""SSH collector — read-only 'show' commands via Netmiko.

Never writes configuration; the pull-only decision applies to SSH exactly
as it does to the config-delivery HTTP endpoint. Error handling follows the
netmiko-ssh-automation skill's safe_connect taxonomy: every failure mode
(auth, timeout, unreachable) is caught and reported per-task rather than
raised, so one dead device never blocks the poller from moving on to the
next one — see poller.py, which runs the device pool through a
ThreadPoolExecutor.
"""

from netmiko import ConnectHandler
from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException

from .. import config
from .. import db
from .. import identity
from ..parsers import ios

COMMANDS = {
    "version": "show version",
    "interfaces": "show interfaces",
    "lldp": "show lldp neighbors detail",
    "bgp": "show bgp summary",
    "ospf": "show ip ospf neighbor",
}
PARSERS = {
    "version": ios.parse_version,
    "interfaces": ios.parse_interfaces,
    "lldp": ios.parse_lldp_neighbors,
    "bgp": ios.parse_bgp_summary,
    "ospf": ios.parse_ospf_neighbors,
}
TASKS = tuple(COMMANDS.keys())


def resolve_credentials(conn, device_id):
    """Per-device credentials row overrides the environment's lab default —
    see config.py's DEFAULT_SSH_* / DEFAULT_SNMP_*."""
    row = conn.execute(
        "SELECT username, password, enable_secret, snmp_community, snmp_version "
        "FROM credentials WHERE device_id = ?", (device_id,)
    ).fetchone()
    return {
        "username": (row["username"] if row and row["username"] else config.DEFAULT_SSH_USERNAME),
        "password": (row["password"] if row and row["password"] else config.DEFAULT_SSH_PASSWORD),
        "secret": (row["enable_secret"] if row and row["enable_secret"] else config.DEFAULT_SSH_SECRET),
        "snmp_community": (row["snmp_community"] if row and row["snmp_community"] else config.DEFAULT_SNMP_COMMUNITY),
        "snmp_version": (row["snmp_version"] if row and row["snmp_version"] else config.DEFAULT_SNMP_VERSION),
    }


def _error_text(exc):
    if isinstance(exc, NetmikoAuthenticationException):
        return "authentication failed"
    if isinstance(exc, NetmikoTimeoutException):
        return "connection timed out"
    return "{}: {}".format(type(exc).__name__, exc)


def _run_command(conn, task):
    output = conn.send_command(COMMANDS[task], read_timeout=20)
    parsed = PARSERS[task](output)
    if task == "lldp" and any(not row["local_if"] for row in parsed):
        # Older IOS leaves Local Intf out of the detail output; the brief
        # table has it. Only fetched when needed.
        brief_out = conn.send_command("show lldp neighbors", read_timeout=20)
        output = output + "\n\n" + brief_out
        parsed = ios.fill_lldp_local_if(parsed, ios.parse_lldp_brief(brief_out))
    return output, parsed


def run_tasks(device, creds, tasks=TASKS):
    """SSH to a device once and run each task's show command in turn.

    A generator: yields (task, ok, raw_output, error, parsed) per task, so
    the caller can commit each task's result before the next command runs
    — partial failure stays the normal case. One login per poll, not one per
    task: fewer auth lines in the device's log and a much shorter poll.

    ok is True whenever a response came back — even "not configured" or
    "feature disabled" is ok=True, since that's a normal device state
    (confirmed against a real CSR1000v with LLDP off and no BGP/OSPF
    configured), not a poll failure. A failed login fails every task with
    the same error; a failure on one command fails only that task.
    """
    host = device.get("mgmt_ip")
    if not host:
        for task in tasks:
            yield task, False, None, "device has no mgmt_ip", None
        return

    connect_kwargs = {
        "device_type": device.get("platform") or "cisco_ios",
        "host": host,
        "username": creds["username"],
        "password": creds["password"],
        "secret": creds["secret"] or "",
        "timeout": 15,
    }

    try:
        conn = ConnectHandler(**connect_kwargs)
        if creds["secret"] and not conn.check_enable_mode():
            conn.enable()
    except Exception as exc:
        for task in tasks:
            yield task, False, None, _error_text(exc), None
        return

    try:
        for task in tasks:
            try:
                output, parsed = _run_command(conn, task)
            except Exception as exc:
                yield task, False, None, _error_text(exc), None
            else:
                yield task, True, output, None, parsed
    finally:
        try:
            conn.disconnect()
        except Exception:
            pass


def apply_result(conn, device_id, task, parsed):
    """Write one task's parsed result into its table.

    Caller commits (or rolls back on identity.Conflict) — see poller.py.
    Each task writes independently: a failure applying one task's result
    must never roll back another task's already-committed data, which is
    why poller.py commits after each task rather than once per device.
    """
    now = db.sqlite_now()

    if task == "version":
        _apply_version(conn, device_id, parsed, now)
    elif task == "interfaces":
        _apply_interfaces(conn, device_id, parsed, now)
    elif task == "lldp":
        _apply_lldp(conn, device_id, parsed, now)
    elif task == "bgp":
        _apply_adjacencies(conn, device_id, "bgp", parsed, now)
    elif task == "ospf":
        _apply_adjacencies(conn, device_id, "ospf", parsed, now)


def _apply_version(conn, device_id, parsed, now):
    candidates = []
    if parsed.get("serial"):
        candidates.append(("serial", parsed["serial"]))
    if parsed.get("hostname_hint"):
        candidates.append(("hostname", parsed["hostname_hint"]))
    fields = {"model": parsed.get("model"), "os_version": parsed.get("os_version")}

    if candidates:
        # May raise identity.Conflict — poller.py catches it and marks the
        # version task failed without touching interfaces/lldp/bgp/ospf.
        identity.observe(conn, device_id, candidates, fields, source="poll", ref="ssh")
    elif fields.get("model") or fields.get("os_version"):
        conn.execute(
            "UPDATE devices SET model = COALESCE(?, model), os_version = COALESCE(?, os_version) "
            "WHERE id = ?",
            (fields.get("model"), fields.get("os_version"), device_id),
        )


def _apply_interfaces(conn, device_id, parsed, now):
    for row in parsed:
        conn.execute(
            """INSERT INTO interfaces (device_id, name, description, ip, prefix_len, network,
                   admin_status, oper_status, speed, duplex, input_errors, crc_errors, mtu, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(device_id, name) DO UPDATE SET
                   description=excluded.description, ip=excluded.ip, prefix_len=excluded.prefix_len,
                   network=excluded.network, admin_status=excluded.admin_status,
                   oper_status=excluded.oper_status, speed=excluded.speed, duplex=excluded.duplex,
                   input_errors=excluded.input_errors, crc_errors=excluded.crc_errors,
                   mtu=excluded.mtu, last_seen=excluded.last_seen""",
            (device_id, row["name"], row["description"], row["ip"], row["prefix_len"], row["network"],
             row["admin_status"], row["oper_status"], row["speed"], row["duplex"],
             row["input_errors"], row["crc_errors"], row["mtu"], now),
        )
    # Drop interfaces this poll no longer reported (removed subinterface,
    # deleted loopback) — otherwise IPAM keeps flagging a dead address. Not
    # on an empty result: every router has interfaces, so empty means the
    # output didn't parse, and wiping the table then would be the bug.
    if parsed:
        conn.execute("DELETE FROM interfaces WHERE device_id = ? AND last_seen <> ?",
                     (device_id, now))


def _apply_lldp(conn, device_id, parsed, now):
    for row in parsed:
        # Resolve by the management IP the neighbor advertises, matched
        # against mgmt_ip aliases. Not by chassis ID: IOS-XE has no command
        # that shows a device its own chassis ID ("show lldp entry local"
        # looks up a neighbor NAMED "local"), so a chassis alias can never be
        # self-registered. No match (or no advertised IP) stays NULL, and the
        # neighbor renders as a ghost node.
        remote_device_id = None
        if row.get("remote_mgmt_ip"):
            hit = conn.execute(
                "SELECT device_id FROM device_aliases WHERE kind = 'mgmt_ip' AND value = ?",
                (row["remote_mgmt_ip"],),
            ).fetchone()
            remote_device_id = hit["device_id"] if hit else None
        conn.execute(
            """INSERT INTO lldp_neighbors (device_id, local_if, remote_chassis, remote_sysname,
                   remote_port, remote_mgmt_ip, remote_device_id, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(device_id, local_if, remote_port) DO UPDATE SET
                   remote_chassis=excluded.remote_chassis, remote_sysname=excluded.remote_sysname,
                   remote_mgmt_ip=excluded.remote_mgmt_ip, remote_device_id=excluded.remote_device_id,
                   last_seen=excluded.last_seen""",
            (device_id, row["local_if"], row.get("remote_chassis"), row.get("remote_sysname"),
             row.get("remote_port"), row.get("remote_mgmt_ip"), remote_device_id, now),
        )
    # A neighbor this poll didn't report is gone (cable moved, LLDP turned
    # off) — drop it so the topology doesn't keep a dead link. Empty is a
    # real answer here ("LLDP is not enabled"), so this runs regardless.
    conn.execute("DELETE FROM lldp_neighbors WHERE device_id = ? AND last_seen <> ?",
                 (device_id, now))


def _apply_adjacencies(conn, device_id, proto, parsed, now):
    for row in parsed:
        # A BGP/OSPF peer_ip is usually the neighbor's loopback or a
        # point-to-point interface address, not its management IP — so
        # this resolves far less often than LLDP's advertised-IP match, and NULL
        # is the expected common case, not a bug.
        peer = conn.execute(
            "SELECT device_id FROM device_aliases WHERE kind = 'mgmt_ip' AND value = ?",
            (row["peer_ip"],),
        ).fetchone()
        conn.execute(
            """INSERT INTO adjacencies (device_id, proto, peer_ip, peer_device_id, remote_as,
                   state, uptime, prefixes, local_if, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(device_id, proto, peer_ip) DO UPDATE SET
                   peer_device_id=excluded.peer_device_id, remote_as=excluded.remote_as,
                   state=excluded.state, uptime=excluded.uptime, prefixes=excluded.prefixes,
                   local_if=excluded.local_if, last_seen=excluded.last_seen""",
            (device_id, proto, row["peer_ip"], peer["device_id"] if peer else None,
             row.get("remote_as"), row["state"], row.get("uptime"), row.get("prefixes"),
             row.get("local_if"), now),
        )
    # Same as LLDP: a peer no longer listed was removed from the config.
    # A peer that is merely down is still listed (Active/Idle) and stays.
    conn.execute("DELETE FROM adjacencies WHERE device_id = ? AND proto = ? AND last_seen <> ?",
                 (device_id, proto, now))
