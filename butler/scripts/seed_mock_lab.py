"""Fill a NEW scratch database with a fake lab, for README screenshots and demos.

Three routers (documentation-range IPs, fake serials 9SIMLAB000x), their
interfaces, LLDP/OSPF/BGP neighbours, one identity conflict, one IPAM overlap,
one duplicate IP, a base template and 14 recent syslog lines. No real lab data.

    python scripts/seed_mock_lab.py mock-lab.db

Refuses to touch a file that already exists. Devices are marked "polled just
now" and next_poll_at is far in the future, so a server started on this file
never dials the fake addresses. Used by capture_readme.py.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

if len(sys.argv) != 2:
    sys.exit("usage: seed_mock_lab.py <new-db-path>")
if os.path.exists(sys.argv[1]):
    sys.exit("{} already exists - pick a new path".format(sys.argv[1]))
os.environ["BUTLER_DB_PATH"] = sys.argv[1]

from app import db, identity, ipam  # noqa: E402  (env var must be set first)
from app.parsers import ios  # noqa: E402

ROUTERS = [
    # hostname, mgmt_ip, serial, os_version
    ("lab-rtr-a", "192.0.2.10", "9SIMLAB0001", "17.3.8a"),
    ("lab-rtr-b", "192.0.2.171", "9SIMLAB0002", "17.3.2"),
    ("lab-rtr-c", "192.0.2.167", "9SIMLAB0003", "3.11.4S"),
]

# device index -> (name, description, ip, prefix_len)
INTERFACES = {
    0: [("GigabitEthernet1", "mgmt", "192.0.2.10", 24),
        ("GigabitEthernet2", "to lab-rtr-b", "10.0.12.1", 24),
        ("GigabitEthernet3", "to lab-rtr-c", "10.0.13.1", 30),
        ("Loopback0", "router id", "10.255.0.1", 32)],
    1: [("GigabitEthernet1", "mgmt", "192.0.2.171", 24),
        ("GigabitEthernet2", "to lab-rtr-a", "10.0.12.2", 24),
        ("GigabitEthernet3", "to lab-rtr-c", "10.0.23.1", 24),
        ("Loopback0", "router id", "10.255.0.2", 32)],
    2: [("GigabitEthernet1", "mgmt", "192.0.2.167", 24),
        ("GigabitEthernet2", "to lab-rtr-b", "10.0.23.2", 24),
        ("GigabitEthernet3", "to lab-rtr-a", "10.0.13.2", 30),
        ("GigabitEthernet4", "lab segment", "10.0.13.254", 24),   # swallows the /30 above: overlap
        ("Loopback0", "router id", "10.255.0.3", 32),
        ("Loopback1", "test", "10.255.0.1", 32)],                  # same IP as lab-rtr-a Lo0: duplicate
}

# device index, local_if, remote sysname, remote port, remote device index (None = ghost), remote mgmt ip
LLDP = [
    (0, "GigabitEthernet2", "lab-rtr-b", "GigabitEthernet2", 1, "192.0.2.171"),
    (1, "GigabitEthernet2", "lab-rtr-a", "GigabitEthernet2", 0, "192.0.2.10"),
    (1, "GigabitEthernet3", "lab-rtr-c", "GigabitEthernet2", 2, "192.0.2.167"),
    (2, "GigabitEthernet2", "lab-rtr-b", "GigabitEthernet3", 1, "192.0.2.171"),
    (2, "GigabitEthernet4", "sw-access-1", "Fa0/24", None, None),
]

# device index, proto, peer_ip, remote_as, area, state, uptime, prefixes, local_if
ADJACENCIES = [
    (0, "ospf", "10.255.0.2", None, "0", "FULL", "2d03h", None, "GigabitEthernet2"),
    (1, "ospf", "10.255.0.1", None, "0", "FULL", "2d03h", None, "GigabitEthernet2"),
    (0, "bgp", "203.0.113.9", 64500, None, "Established", "1d02h", 14, None),
    (1, "bgp", "198.51.100.7", 64501, None, "Established", "05:12:44", 6, None),
    (2, "bgp", "198.51.100.8", 64501, None, "Active", "never", None, None),
]

# minutes ago, router index, severity, mnemonic, message
SYSLOG = [
    (1, 0, 5, "BGP-5-ADJCHANGE", "neighbor 203.0.113.9 Up"),
    (2, 1, 3, "CRYPTO-3-IKMP_NO_SA", "IKE message from 198.51.100.7 has no SA and is not an initialization offer"),
    (4, 0, 6, "SYS-6-LOGOUT", "User butler has exited tty session 2(192.0.2.5)"),
    (7, 2, 4, "BGP-4-NOTIFICATION", "received from neighbor 10.255.0.2 4/0 (hold time expired) 0 bytes"),
    (9, 1, 6, "SYS-6-CLOCKUPDATE", "System clock has been updated from 10:02:11 UTC to 10:02:12 UTC"),
    (12, 2, 5, "LINEPROTO-5-UPDOWN", "Line protocol on Interface GigabitEthernet3, changed state to up"),
    (15, 1, 3, "DUAL-3-SIA", "Route 10.0.12.0/24, stuck-in-active, GigabitEthernet4"),
    (18, 0, 6, "SEC_LOGIN-6-LOGIN_SUCCESS", "Login Success [user: butler] [Source: 192.0.2.5] [localport: 22]"),
    (22, 0, 5, "OSPF-5-ADJCHG", "Process 1, Nbr 10.255.0.3 on GigabitEthernet3 from LOADING to FULL, Loading Done"),
    (25, 2, 5, "BGP-5-ADJCHANGE", "neighbor 198.51.100.8 Down BGP Notification sent"),
    (29, 2, 4, "BGP-4-MSGDUMP", "unsupported or mal-formatted message received from 10.255.0.2"),
    (33, 1, 6, "SYS-6-LOGOUT", "User butler has exited tty session 3(192.0.2.5)"),
    (41, 0, 5, "LINK-5-CHANGED", "Interface GigabitEthernet4, changed state to administratively down"),
    (50, 2, 6, "SYS-6-CLOCKUPDATE", "System clock has been updated from 09:30:00 UTC to 09:30:01 UTC"),
]

TEMPLATE = """hostname {{ device.hostname }}
!
{% for i in interfaces if i.ip %}interface {{ i.name }}
 description {{ i.description }}
 ip address {{ i.ip }}/{{ i.prefix_len }}
 no shutdown
!
{% endfor %}enable secret {{ vars.enable_secret }}
"""


def ts(minutes_ago=0):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def main():
    db.init_db()
    conn = db.connect()
    now = ts()
    ids = []
    for host, ip, serial, ver in ROUTERS:
        fields = {"hostname": host, "mgmt_ip": ip, "serial": serial, "os_version": ver,
                  "model": "CSR1000V", "site": "lab"}
        ids.append(identity.ingest(
            conn, [("serial", serial), ("mgmt_ip", ip), ("hostname", host)], fields, "manual"))

    for idx, dev_id in enumerate(ids):
        failing = idx == 2
        conn.execute(
            "UPDATE devices SET poll_state='idle', next_poll_at='2099-01-01 00:00:00', "
            "fail_count=?, last_poll_ok=? WHERE id=?",
            (3 if failing else 0, ts(180) if failing else now, dev_id))
        for name, desc, ip, plen in INTERFACES[idx]:
            conn.execute(
                "INSERT INTO interfaces (device_id, name, description, ip, prefix_len, network, "
                "admin_status, oper_status, speed, duplex, input_errors, crc_errors, mtu, last_seen) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,0,0,1500,?)",
                (dev_id, name, desc, ip, plen, ios._network(ip, plen),
                 "up", "up", "1000", "full", now))

    for idx, local_if, sysname, port, remote_idx, rip in LLDP:
        conn.execute(
            "INSERT INTO lldp_neighbors (device_id, local_if, remote_sysname, remote_port, "
            "remote_mgmt_ip, remote_device_id, last_seen) VALUES (?,?,?,?,?,?,?)",
            (ids[idx], local_if, sysname, port, rip,
             ids[remote_idx] if remote_idx is not None else None, now))
    for idx, proto, peer, asn, area, state, up, pfx, lif in ADJACENCIES:
        conn.execute(
            "INSERT INTO adjacencies (device_id, proto, peer_ip, remote_as, area, state, uptime, "
            "prefixes, local_if, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ids[idx], proto, peer, asn, area, state, up, pfx, lif, now))

    for ago, idx, sev, mnem, msg in SYSLOG:
        host, ip = ROUTERS[idx][0], ROUTERS[idx][1]
        conn.execute(
            "INSERT INTO syslog (received_at, source_ip, host, facility, severity, mnemonic, "
            "device_time, message, raw) VALUES (?,?,?,23,?,?,?,?,?)",
            (ts(ago), ip, host, sev, mnem, ts(ago), msg, "%{}: {}".format(mnem, msg)))

    body = TEMPLATE
    conn.execute(
        "INSERT INTO templates (name, body, note, updated_at, version) VALUES (?,?,?,?,1)",
        ("base-router", body, "Mock lab template", now))
    conn.execute(
        "INSERT INTO template_versions (name, version, body, saved_at) VALUES (?,1,?,?)",
        ("base-router", body, now))
    conn.execute("UPDATE devices SET template_name='base-router'")
    for dev_id in ids:
        conn.execute("INSERT INTO template_vars (device_id, key, value) VALUES (?,?,?)",
                     (dev_id, "enable_secret", "CHANGE-ME"))

    # An identity conflict: lab-rtr-b's IP now reports a different serial.
    try:
        identity.ingest(conn, [("serial", "9SIMLAB0099"), ("mgmt_ip", "192.0.2.171")],
                        {"serial": "9SIMLAB0099", "mgmt_ip": "192.0.2.171"}, "sweep", "192.0.2.0/24")
    except identity.Conflict:
        pass

    ipam.analyze(conn)
    conn.commit()
    conn.close()
    print("seeded {} with {} devices".format(sys.argv[1], len(ids)))


main()
