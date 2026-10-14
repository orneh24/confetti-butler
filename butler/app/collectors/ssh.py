"""SSH collector — read-only 'show' commands via Netmiko.

Never writes configuration; the pull-only decision applies to SSH exactly
as it does to the config-delivery HTTP endpoint. Error handling follows the
netmiko-ssh-automation skill's safe_connect taxonomy: every failure mode
(auth, timeout, unreachable) is caught and reported per-task rather than
raised, so one dead device never blocks the poller from moving on to the
next one — see poller.py, which runs the device pool through a
ThreadPoolExecutor.
"""

import hashlib

from netmiko import ConnectHandler
from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException

from .. import config
from .. import db
from .. import drift
from .. import events
from .. import identity
from .. import platforms
from . import snmp
from ..parsers import ios

# The IOS entry, kept under these names: it is the default platform, and
# dev/regress.py (R3, R16) reads them. Other platforms live in platforms.py.
COMMANDS = platforms.IOS["commands"]
PARSERS = platforms.IOS["parsers"]
TASKS = platforms.tasks(platforms.IOS)


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


def _run_command(conn, task, plat):
    # A large config is slow to read over SSH.
    output = conn.send_command(plat["commands"][task], read_timeout=60 if task == "config" else 20)
    parsed = plat["parsers"][task](output)
    if task == "config" and not parsed:
        # Unlike the show-command parsers, an empty result is not a normal
        # device state: it is an error message or a truncated read.
        raise ValueError("output is not a complete running-config")
    return output, parsed


def run_tasks(device, creds, tasks=None):
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
    plat = platforms.get(device.get("platform"))
    if plat["transport"] == "snmp":
        yield from snmp.run_tasks(device, creds)
        return
    tasks = tasks or platforms.tasks(plat)

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
                output, parsed = _run_command(conn, task, plat)
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
    elif task == "config":
        _apply_config(conn, device_id, parsed, now)


def _apply_version(conn, device_id, parsed, now):
    candidates = []
    if parsed.get("serial"):
        candidates.append(("serial", parsed["serial"]))
    if parsed.get("hostname_hint"):
        candidates.append(("hostname", parsed["hostname_hint"]))
    fields = {"model": parsed.get("model"), "os_version": parsed.get("os_version")}
    old = conn.execute("SELECT hostname, os_version FROM devices WHERE id = ?", (device_id,)).fetchone()
    if (old and old["os_version"] and fields["os_version"] and old["os_version"] != fields["os_version"]
            and not events.is_baseline(conn, device_id, "version")):
        events.emit(conn, device_id, "os_version_changed", "warning",
                    "{}: OS version changed".format(old["hostname"]),
                    "{} -> {}".format(old["os_version"], fields["os_version"]))

    if candidates:
        # May raise identity.Conflict — poller.py catches it and marks the
        # version task failed without touching interfaces/config.
        identity.observe(conn, device_id, candidates, fields, source="poll", ref="ssh")
    elif fields.get("model") or fields.get("os_version"):
        conn.execute(
            "UPDATE devices SET model = COALESCE(?, model), os_version = COALESCE(?, os_version) "
            "WHERE id = ?",
            (fields.get("model"), fields.get("os_version"), device_id),
        )


def _apply_interfaces(conn, device_id, parsed, now):
    if parsed and not events.is_baseline(conn, device_id, "interfaces"):
        old = {r["name"]: r for r in conn.execute(
            "SELECT name, oper_status, crc_errors FROM interfaces WHERE device_id = ?", (device_id,))}
        host = _hostname(conn, device_id)
        for row in parsed:
            before = old.get(row["name"])
            if before is None:
                continue
            if before["oper_status"] != row["oper_status"]:
                events.emit(conn, device_id, "interface_" + ("up" if row["oper_status"] == "up" else "down"),
                            "info" if row["oper_status"] == "up" else "warning",
                            "{} {}: {}".format(host, row["name"], row["oper_status"]),
                            "{} -> {}".format(before["oper_status"], row["oper_status"]))
            if (row["crc_errors"] is not None and before["crc_errors"] is not None
                    and row["crc_errors"] > before["crc_errors"]):
                events.emit(conn, device_id, "interface_errors", "warning",
                            "{} {}: CRC errors rising".format(host, row["name"]),
                            "{} -> {}".format(before["crc_errors"], row["crc_errors"]))
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
    _record_interface_stats(conn, device_id, parsed, now)
    # Drop interfaces this poll no longer reported (removed subinterface,
    # deleted loopback) — otherwise IPAM keeps flagging a dead address. Not
    # on an empty result: every router has interfaces, so empty means the
    # output didn't parse, and wiping the table then would be the bug.
    if parsed:
        conn.execute("DELETE FROM interfaces WHERE device_id = ? AND last_seen <> ?",
                     (device_id, now))


def _hostname(conn, device_id):
    row = conn.execute("SELECT hostname FROM devices WHERE id = ?", (device_id,)).fetchone()
    return row["hostname"] if row else str(device_id)


STATS_HEARTBEAT_S = 3600


def _record_interface_stats(conn, device_id, parsed, now):
    """Keep a sample of each interface's error counters when they change, plus
    one an hour so a flat line still has points. A counter that went DOWN
    (device reload) is stored like any other change; growth() ignores resets."""
    last = {r["name"]: r for r in conn.execute(
        """SELECT name, input_errors, crc_errors, at FROM interface_stats
           WHERE device_id = ? AND id IN
                 (SELECT MAX(id) FROM interface_stats WHERE device_id = ? GROUP BY name)""",
        (device_id, device_id))}
    for row in parsed:
        if row["input_errors"] is None and row["crc_errors"] is None:
            continue
        prev = last.get(row["name"])
        if prev is not None and (prev["input_errors"], prev["crc_errors"]) == (
                row["input_errors"], row["crc_errors"]):
            age = conn.execute("SELECT strftime('%s', ?) - strftime('%s', ?)", (now, prev["at"])).fetchone()[0]
            if age is None or age < STATS_HEARTBEAT_S:
                continue
        conn.execute(
            "INSERT INTO interface_stats (device_id, name, at, input_errors, crc_errors) VALUES (?, ?, ?, ?, ?)",
            (device_id, row["name"], now, row["input_errors"], row["crc_errors"]))


def growth(samples, field):
    """Total increase of a counter across consecutive samples (oldest first).
    A decrease means the counters were reset by a reload; it adds nothing."""
    total = 0
    prev = None
    for s in samples:
        v = s[field]
        if v is None:
            continue
        if prev is not None and v > prev:
            total += v - prev
        prev = v
    return total


def _apply_config(conn, device_id, text, now):
    """Store the config only when it differs from the device's latest version."""
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    latest = conn.execute(
        "SELECT sha256 FROM config_versions WHERE device_id = ? ORDER BY id DESC LIMIT 1",
        (device_id,),
    ).fetchone()
    if latest and latest["sha256"] == sha:
        drift.refresh(conn, device_id)    # the template may have changed since the last poll
        return
    if latest:
        events.emit(conn, device_id, "config_changed", "info",
                    "{}: running-config changed".format(_hostname(conn, device_id)))
    conn.execute(
        "INSERT INTO config_versions (device_id, sha256, body, captured_at) VALUES (?, ?, ?, ?)",
        (device_id, sha, text, now),
    )
    if config.CONFIG_VERSIONS_KEEP > 0:
        conn.execute(
            """DELETE FROM config_versions WHERE device_id = ? AND id NOT IN
                   (SELECT id FROM config_versions WHERE device_id = ? ORDER BY id DESC LIMIT ?)""",
            (device_id, device_id, config.CONFIG_VERSIONS_KEEP),
        )
    drift.refresh(conn, device_id)
