"""Load devices from a static YAML seed file.

Reproducible and reviewable, but hand-maintained — the fallback ingest path
for a device that a subnet sweep or vCenter can't (yet) reach. See
seed/devices.yaml.sample for the expected shape.
"""

import yaml

from .. import identity


def load(conn, path):
    """Ingest every device in a YAML seed file.

    Returns (device_ids, conflicts) — device_ids is every device created or
    updated; conflicts is a list of {"entry": ..., "device_ids": [...]} for
    entries identity.ingest could not resolve unambiguously. Caller
    (the /api/discover/seedfile route) commits; this does not, so a bad file
    can be inspected without side effects if the caller chooses not to.
    """
    with open(path, "r") as fh:
        data = yaml.safe_load(fh) or {}

    entries = data.get("devices") or []
    device_ids = []
    conflicts = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        candidates = [
            (kind, entry[kind])
            for kind in ("serial", "vmuuid", "mgmt_ip", "hostname")
            if entry.get(kind)
        ]
        if not candidates:
            continue
        fields = {k: entry.get(k) for k in identity.DEVICE_FIELDS}
        try:
            device_id = identity.ingest(conn, candidates, fields, "seedfile", ref=path)
        except identity.Conflict as exc:
            conflicts.append({"entry": entry, "device_ids": exc.device_ids})
            continue
        device_ids.append(device_id)

    return device_ids, conflicts
