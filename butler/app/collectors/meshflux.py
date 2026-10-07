"""Import confetti-traffic's node fleet as topology context.

GET <hub_url>/endpoints returns confetti-traffic's Alpine test VMs, not managed
routers — imported with role='node' and vendor/platform set explicitly so
they don't inherit this app's cisco/cisco_ios defaults for a brand-new
device (see identity._create_device). role='node' is what makes
poller.py skip them (`WHERE ... role != 'node'`) — they are never
SSH-polled with IOS show commands.
"""

import requests

from .. import identity


def discover(conn, hub_url):
    resp = requests.get("{}/endpoints".format(hub_url.rstrip("/")), timeout=10)
    resp.raise_for_status()
    endpoints = resp.json()

    device_ids = []
    conflicts = []
    for ep in endpoints:
        candidates = []
        if ep.get("hostname"):
            candidates.append(("hostname", ep["hostname"]))
        if ep.get("ip"):
            candidates.append(("mgmt_ip", ep["ip"]))
        if not candidates:
            continue

        fields = {"role": "node", "vendor": "alpine", "platform": "linux"}
        if ep.get("group_name"):
            fields["site"] = ep["group_name"]

        try:
            device_id = identity.ingest(conn, candidates, fields, source="meshflux", ref=hub_url)
        except identity.Conflict as exc:
            conflicts.append({"endpoint": ep, "device_ids": exc.device_ids})
            continue
        device_ids.append(device_id)

    return device_ids, conflicts
