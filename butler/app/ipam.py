"""IPAM analysis — discovery-first.

interfaces (populated by the poller) IS the source of truth; there is no
separate address-allocation table in v1. This is where the
network-config-validation skill's find_subnet_overlaps / find_duplicate_ips
logic lands, adapted to read already-parsed rows instead of raw config
text — the poller parsed 'show interfaces' once already, so this works
off structured data rather than re-parsing anything.
"""

import ipaddress

from . import db


def analyze(conn):
    """Rebuild ipam_findings wholesale from the current interfaces table.

    Wholesale, not incremental — cheap at lab scale, and far simpler than
    tracking which findings a since-removed or since-fixed interface should
    retract. Caller commits.
    """
    now = db.sqlite_now()
    rows = conn.execute(
        """SELECT i.device_id, i.name, i.ip, i.prefix_len, i.network, d.hostname
           FROM interfaces i JOIN devices d ON d.id = i.device_id
           WHERE i.ip IS NOT NULL"""
    ).fetchall()

    findings = []
    findings.extend(_find_overlaps(rows, now))
    findings.extend(_find_duplicate_ips(rows, now))

    conn.execute("DELETE FROM ipam_findings")
    conn.executemany(
        "INSERT INTO ipam_findings (checked_at, kind, a, b, detail) VALUES (?, ?, ?, ?, ?)",
        findings,
    )
    return len(findings)


def _label(row):
    return "{}:{}".format(row["hostname"], row["name"])


def _find_overlaps(rows, now):
    """Two different devices sharing the *identical* network (a point-to-
    point /30, a shared segment) is the normal, expected case — only a
    partial overlap (a /24 that swallows an already-configured /30, say)
    indicates a real misconfiguration, so identical networks are excluded.
    A device's own interfaces overlapping each other is excluded too
    (secondary addressing, HSRP-style configs are legitimately layered).
    """
    out = []
    networks = []
    for row in rows:
        if not row["network"]:
            continue
        try:
            net = ipaddress.ip_network(row["network"], strict=False)
        except ValueError:
            continue
        networks.append((net, row))

    for i, (net_a, row_a) in enumerate(networks):
        for net_b, row_b in networks[i + 1:]:
            if row_a["device_id"] == row_b["device_id"]:
                continue
            if net_a == net_b:
                continue
            if net_a.overlaps(net_b):
                out.append((now, "overlap", _label(row_a), _label(row_b),
                            "{} and {} overlap".format(net_a, net_b)))
    return out


def _find_duplicate_ips(rows, now):
    """Any IP assigned more than once — same device or different — is worth
    a human's attention; a same-device duplicate is usually a copy-paste
    error, a cross-device one is a live address conflict.
    """
    by_ip = {}
    for row in rows:
        if row["ip"]:
            by_ip.setdefault(row["ip"], []).append(row)

    out = []
    for ip, group in by_ip.items():
        if len(group) > 1:
            labels = ", ".join(_label(g) for g in group)
            out.append((now, "duplicate_ip", ip, None, "{} assigned to: {}".format(ip, labels)))
    return out
