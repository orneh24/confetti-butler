"""vCenter/ESXi REST API discovery.

Uses the vSphere REST API via `requests` rather than pyvmomi — pyvmomi is
heavy and ESXi 7.0 / vCenter expose the REST API directly, so nothing extra
needs installing. NOT live-verified in this build (no vCenter instance was
reachable while this was written) — the endpoint shapes below match the
documented vSphere REST API for 7.0, but confirm against a real vCenter
before relying on it.
"""

import requests
import urllib3

from .. import identity

# Lab vCenter instances commonly run with a self-signed certificate;
# verify_ssl is left as a caller-supplied option rather than hardcoded off,
# but the warning is silenced either way so a lab run isn't noisy.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _session_token(base_url, username, password, verify_ssl):
    resp = requests.post(
        "{}/api/session".format(base_url), auth=(username, password),
        verify=verify_ssl, timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def discover(conn, base_url, username, password, name_filter=None, verify_ssl=False):
    """Ingest VMs from vCenter as device candidates.

    name_filter, if given, only imports VMs whose name contains it
    (case-insensitive) — e.g. "csr" to skip unrelated lab VMs sharing the
    same vCenter. vendor/platform default to cisco/cisco_ios here, matching
    this lab's initial CSR1000v-only scope (see the plan) — a v1
    simplification to revisit once other device types are onboarded.

    Returns (device_ids, conflicts). Caller commits.
    """
    token = _session_token(base_url, username, password, verify_ssl)
    headers = {"vmware-api-session-id": token}

    resp = requests.get("{}/api/vcenter/vm".format(base_url), headers=headers,
                         verify=verify_ssl, timeout=15)
    resp.raise_for_status()
    vms = resp.json()

    device_ids = []
    conflicts = []
    for vm in vms:
        name = vm.get("name", "")
        if name_filter and name_filter.lower() not in name.lower():
            continue
        vm_id = vm.get("vm")

        candidates = []
        if vm_id:
            candidates.append(("vmuuid", vm_id))
        if name:
            candidates.append(("hostname", name))

        ip = None
        try:
            id_resp = requests.get(
                "{}/api/vcenter/vm/{}/guest/identity".format(base_url, vm_id),
                headers=headers, verify=verify_ssl, timeout=10,
            )
            if id_resp.ok:
                ip = id_resp.json().get("ip_address")
        except requests.RequestException:
            # VMware Tools not running/installed yet — the VM is still worth
            # tracking by name/uuid alone; a later sweep or poll can fill in
            # the address once Tools comes up.
            pass
        if ip:
            candidates.append(("mgmt_ip", ip))

        if not candidates:
            continue
        try:
            device_id = identity.ingest(
                conn, candidates, {"vendor": "cisco", "platform": "cisco_ios"},
                source="vcenter", ref=base_url,
            )
        except identity.Conflict as exc:
            conflicts.append({"vm": name, "device_ids": exc.device_ids})
            continue
        device_ids.append(device_id)

    return device_ids, conflicts
