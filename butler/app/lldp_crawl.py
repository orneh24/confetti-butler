"""Turn unknown LLDP neighbors into devices (Netdisco-style crawl).

A neighbor that advertises a management IP no known device owns shows up on
the topology as a ghost node. adopt() ingests it through identity.ingest like
every other discovery path. Adopted devices start with polling OFF
(enabled=0): they are inventory until an operator gives them credentials and
enables them, so the crawl can never fill the poller with devices it cannot
log in to.
"""

import ipaddress

from . import events
from . import identity


def _valid_ip(value):
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return ip.version == 4 and not (ip.is_loopback or ip.is_unspecified or ip.is_multicast)


def adopt(conn, ip, sysname, seen_from):
    """Ingest one neighbor. Returns its device_id, or None when the IP is not
    usable or the identity rules report a conflict (a merge_conflicts row is
    already written in that case). Caller commits."""
    if not _valid_ip(ip):
        return None
    existed = conn.execute(
        "SELECT 1 FROM device_aliases WHERE kind = 'mgmt_ip' AND value = ?", (ip,)
    ).fetchone()
    try:
        device_id = identity.ingest(conn, [("mgmt_ip", ip)],
                                    {"hostname": sysname or "", "role": "unknown"},
                                    source="lldp", ref=seen_from)
    except identity.Conflict:
        return None
    if not existed:
        conn.execute("UPDATE devices SET enabled = 0 WHERE id = ?", (device_id,))
        events.emit(conn, device_id, "device_discovered", "info",
                    "{} ({}): found via LLDP on {}".format(sysname or ip, ip, seen_from))
    conn.execute("UPDATE lldp_neighbors SET remote_device_id = ? WHERE remote_mgmt_ip = ?",
                 (device_id, ip))
    return device_id


def unknown_neighbors(conn):
    """LLDP neighbors whose advertised mgmt IP matches no known device."""
    rows = conn.execute(
        """SELECT l.remote_mgmt_ip AS ip, MAX(l.remote_sysname) AS sysname,
                  GROUP_CONCAT(d.hostname || ' ' || l.local_if, ', ') AS seen_on
           FROM lldp_neighbors l JOIN devices d ON d.id = l.device_id
           WHERE l.remote_mgmt_ip IS NOT NULL AND l.remote_mgmt_ip != ''
             AND NOT EXISTS (SELECT 1 FROM device_aliases a
                             WHERE a.kind = 'mgmt_ip' AND a.value = l.remote_mgmt_ip)
           GROUP BY l.remote_mgmt_ip ORDER BY l.remote_mgmt_ip"""
    ).fetchall()
    return [dict(r) for r in rows if _valid_ip(r["ip"])]
