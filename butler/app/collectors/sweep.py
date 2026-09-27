"""Subnet sweep discovery — the fallback when nothing else has found a
device yet. Fingerprinting is deliberately coarse: a TCP connect to port 22
is the "worth investigating" signal, and the poller's own 'version' task
does the real identification (serial, model, os_version) on the next cycle
— see poller.py. This keeps the sweep itself fast and dependency-free.

A live network scan only ever runs when explicitly invoked via
POST /api/discover/sweep — nothing here runs on a schedule.
"""

import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor

from .. import identity

SWEEP_PORT = 22
SWEEP_TIMEOUT_S = 1.0
SWEEP_WORKERS = 32


def _probe(ip):
    try:
        with socket.create_connection((str(ip), SWEEP_PORT), timeout=SWEEP_TIMEOUT_S):
            return str(ip)
    except OSError:
        return None


def sweep(conn, cidr):
    """Scan a CIDR for hosts with SSH open, ingesting each by mgmt_ip alone.

    No hostname field is set — a brand-new device falls back to its key
    ("mgmt:<ip>") as a display name via identity._create_device, and an
    already-known device's real hostname is never overwritten with a raw
    IP on re-sweep, since fields stays empty here.

    Returns (found_ips, device_ids, conflicts). Caller commits.
    """
    network = ipaddress.ip_network(cidr, strict=False)
    hosts = list(network.hosts()) if network.num_addresses > 2 else [network.network_address]

    found = []
    with ThreadPoolExecutor(max_workers=SWEEP_WORKERS) as executor:
        for result in executor.map(_probe, hosts):
            if result:
                found.append(result)

    device_ids = []
    conflicts = []
    for ip in found:
        try:
            device_id = identity.ingest(conn, [("mgmt_ip", ip)], {}, source="sweep", ref=cidr)
        except identity.Conflict as exc:
            conflicts.append({"ip": ip, "device_ids": exc.device_ids})
            continue
        device_ids.append(device_id)

    return found, device_ids, conflicts
