"""Device identity resolution and merge.

Four independent ingest paths (manual add, subnet sweep, vCenter, a YAML
seed file, and mesh-flux's endpoint list) can all observe the same
physical device under different identifiers. This module is the one place
that decides whether an observation is a new device, an update to a known
one, or an ambiguous collision that needs a human — see the merge rule
below. Nothing else in the app should INSERT INTO devices directly.
"""

import json

from . import db

# Strength ladder for devices.key — strongest identifier wins, and a key is
# only ever promoted upward, never demoted. A serial number survives a
# re-IP or a rename; a hostname survives neither.
KIND_RANK = {
    "serial": 4,
    "vmuuid": 3,
    "mgmt_ip": 2,
    "hostname": 1,
}
KEY_PREFIX = {
    "serial": "serial",
    "vmuuid": "vmuuid",
    "mgmt_ip": "mgmt",
    "hostname": "name",
}
DEVICE_FIELDS = (
    "hostname", "mgmt_ip", "vendor", "platform", "model",
    "serial", "os_version", "role", "site",
)


class Conflict(Exception):
    """Raised when an observation's identifiers match more than one device.

    A merge_conflicts row is written before this is raised, so the caller
    doesn't need to record anything — it only needs to stop and surface the
    failure (e.g. skip this row of a bulk import and keep going).
    """
    def __init__(self, device_ids):
        self.device_ids = device_ids
        super().__init__("ambiguous device match: {}".format(device_ids))


def make_key(kind, value):
    return "{}:{}".format(KEY_PREFIX.get(kind, kind), value)


def _normalise(kind, value):
    value = (value or "").strip()
    if kind == "hostname":
        value = value.lower()
    return value


def best_candidate(candidates):
    """Pick the strongest (kind, value) pair to derive a device key from."""
    ranked = sorted(
        ((KIND_RANK.get(k, 0), k, v) for k, v in candidates if v),
        reverse=True,
    )
    if not ranked:
        return None
    _, kind, value = ranked[0]
    return kind, value


def _record_conflict(conn, now, source, candidates, device_ids, detail):
    """Write a merge_conflicts row — unless the same conflict is already open.

    The poller re-observes a swapped device every cycle; without this it
    added an identical row every few minutes. A repeat only bumps seen_at.
    """
    candidate = json.dumps(candidates)
    row = conn.execute(
        """SELECT id FROM merge_conflicts
           WHERE resolved = 0 AND candidate = ? AND device_ids = ? AND detail = ?""",
        (candidate, device_ids, detail),
    ).fetchone()
    if row:
        conn.execute("UPDATE merge_conflicts SET seen_at = ? WHERE id = ?", (now, row["id"]))
        return
    conn.execute(
        """INSERT INTO merge_conflicts (seen_at, source, candidate, device_ids, detail)
           VALUES (?, ?, ?, ?, ?)""",
        (now, source, candidate, device_ids, detail),
    )


def ingest(conn, candidates, fields, source, ref=""):
    """Resolve an observation to a device row, creating or updating it.

    candidates: list of (kind, value) tuples observed this time, e.g.
        [("serial", "9ABC123"), ("mgmt_ip", "10.0.0.1"), ("hostname", "r1")]
    fields: a subset of DEVICE_FIELDS to set. Only truthy values are
        applied — a partial observation (e.g. a bare seed-file entry with
        just a hostname) never blanks a field a richer source already
        populated.
    source: manual | sweep | vcenter | meshflux | seedfile | lldp
    ref: a source-specific reference (filename, sweep CIDR, vCenter moref) —
        distinguishes repeat runs of the same source from each other.

    Returns the device_id on success. Raises Conflict if the candidates
    match more than one existing device; nothing is written to devices or
    device_aliases in that case; the operator resolves it via
    POST /api/devices/<id>/merge.

    Caller owns the transaction — this does not commit.
    """
    candidates = [(k, _normalise(k, v)) for k, v in candidates]
    candidates = [(k, v) for k, v in candidates if v]
    now = db.sqlite_now()

    hits = []  # (kind, value, device_id) for candidates that already exist as aliases
    for kind, value in candidates:
        row = conn.execute(
            "SELECT device_id FROM device_aliases WHERE kind = ? AND value = ?",
            (kind, value),
        ).fetchone()
        if row:
            hits.append((kind, value, row["device_id"]))

    matched_ids = {h[2] for h in hits}

    if len(matched_ids) > 1:
        _record_conflict(
            conn, now, source, candidates,
            ",".join(str(i) for i in sorted(matched_ids)),
            "observation matched {} existing devices".format(len(matched_ids)),
        )
        raise Conflict(sorted(matched_ids))

    if matched_ids:
        device_id = next(iter(matched_ids))
        match_rank = max((KIND_RANK.get(k, 0) for k, v, d in hits), default=0)
        clash = _detect_kind_clash(conn, device_id, candidates, match_rank)
        if clash:
            _record_conflict(
                conn, now, source, candidates, str(device_id),
                "{} disagrees: on file={!r} observed={!r} (matched via a weaker "
                "identifier, so this looks like a different device sharing it, "
                "not a routine update)".format(clash["kind"], clash["existing"], clash["new"]),
            )
            raise Conflict([device_id])
        _update_device(conn, device_id, candidates, fields, now)
    else:
        device_id = _create_device(conn, candidates, fields, now)

    conn.execute(
        """INSERT INTO device_sources (device_id, source, ref, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(device_id, source, ref) DO UPDATE SET last_seen = excluded.last_seen""",
        (device_id, source, ref, now, now),
    )
    return device_id


def _create_device(conn, candidates, fields, now):
    alias_map = dict(candidates)
    best = best_candidate(candidates)
    key = make_key(*best) if best else "unkeyed:{}:{}".format(now, id(candidates))
    hostname = fields.get("hostname") or alias_map.get("hostname") or key

    cols = {
        "key": key,
        "hostname": hostname,
        "mgmt_ip": fields.get("mgmt_ip") or alias_map.get("mgmt_ip"),
        "vendor": fields.get("vendor") or "cisco",
        "platform": fields.get("platform") or "cisco_ios",
        "model": fields.get("model"),
        "serial": fields.get("serial") or alias_map.get("serial"),
        "os_version": fields.get("os_version"),
        "role": fields.get("role") or "router",
        "site": fields.get("site") or "",
        "first_seen": now,
        "last_seen": now,
    }
    cur = conn.execute(
        """INSERT INTO devices (key, hostname, mgmt_ip, vendor, platform, model,
               serial, os_version, role, site, first_seen, last_seen)
           VALUES (:key, :hostname, :mgmt_ip, :vendor, :platform, :model,
               :serial, :os_version, :role, :site, :first_seen, :last_seen)""",
        cols,
    )
    device_id = cur.lastrowid
    for kind, value in candidates:
        conn.execute(
            "INSERT OR IGNORE INTO device_aliases (device_id, kind, value, first_seen) "
            "VALUES (?, ?, ?, ?)",
            (device_id, kind, value, now),
        )
    return device_id


def _detect_kind_clash(conn, device_id, candidates, match_rank):
    """A stronger-or-equal identifier disagreeing with what's on file for
    this device is not a routine update — it's evidence this observation
    might be a *different* physical device that happens to share the
    weaker identifier that produced the match (e.g. IP reuse after a
    decommission). Only checks kinds ranked >= the strongest kind that
    actually matched, so a device matched by serial can still freely update
    its mgmt_ip (a real re-IP) or hostname (a real rename) without tripping
    this — those are ranked lower than serial, so they're exempt.
    """
    for kind, value in candidates:
        rank = KIND_RANK.get(kind, 0)
        if rank == 0 or rank < match_rank:
            continue
        existing = conn.execute(
            "SELECT value FROM device_aliases WHERE device_id = ? AND kind = ?",
            (device_id, kind),
        ).fetchall()
        for row in existing:
            if row["value"] != value:
                return {"kind": kind, "existing": row["value"], "new": value}
    return None


def _key_rank(key):
    if not key or ":" not in key:
        return 0
    prefix = key.split(":", 1)[0]
    for kind, p in KEY_PREFIX.items():
        if p == prefix:
            return KIND_RANK.get(kind, 0)
    return 0


def _update_device(conn, device_id, candidates, fields, now):
    for kind, value in candidates:
        conn.execute(
            "INSERT OR IGNORE INTO device_aliases (device_id, kind, value, first_seen) "
            "VALUES (?, ?, ?, ?)",
            (device_id, kind, value, now),
        )

    row = conn.execute("SELECT key FROM devices WHERE id = ?", (device_id,)).fetchone()
    current_rank = _key_rank(row["key"] if row else None)

    best = best_candidate(candidates)
    updates = {"id": device_id, "last_seen": now}
    if best and KIND_RANK.get(best[0], 0) > current_rank:
        updates["key"] = make_key(*best)

    alias_map = dict(candidates)
    for col in DEVICE_FIELDS:
        value = fields.get(col) or alias_map.get(col)
        if value:
            updates[col] = value

    set_clause = ", ".join("{} = :{}".format(c, c) for c in updates if c != "id")
    conn.execute("UPDATE devices SET {} WHERE id = :id".format(set_clause), updates)


def add_operator_aliases(conn, device_id, values):
    """Record identifiers an operator typed in via PUT /api/devices/<id>.

    PUT bypasses ingest (it is authoritative), but without this a new
    mgmt_ip set by hand never became an alias, so syslog correlation and
    LLDP/BGP/OSPF resolution kept using the old one. Aliases are unique per
    (kind, value): a value already owned by a DIFFERENT device is not moved
    (that is merge's job) — it is returned so the caller can warn.
    """
    now = db.sqlite_now()
    taken = []
    for kind in ("serial", "vmuuid", "mgmt_ip", "hostname"):
        value = _normalise(kind, values.get(kind))
        if not value:
            continue
        row = conn.execute(
            "SELECT device_id FROM device_aliases WHERE kind = ? AND value = ?", (kind, value)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO device_aliases (device_id, kind, value, first_seen) VALUES (?, ?, ?, ?)",
                (device_id, kind, value, now),
            )
        elif row["device_id"] != device_id:
            taken.append({"kind": kind, "value": value, "device_id": row["device_id"]})
    return taken


STRICT_KINDS = ("serial", "vmuuid")


def observe(conn, device_id, candidates, fields, source, ref=""):
    """Update an ALREADY-RESOLVED device with freshly polled facts.

    Unlike ingest(), the device is not looked up by alias-matching — the
    caller (the poller) already knows which device it just polled, because
    it dialled that device's own mgmt_ip. This only guards against one
    thing: a hardware-identity field (serial, vmuuid) disagreeing with what
    is already on file, which would mean the box behind this management
    address changed without the inventory being told — a swap, not routine
    drift. mgmt_ip isn't rechecked here at all (the poller reached the
    device at the address already on file, so it hasn't changed), and a
    hostname discovered by polling updates freely, same as a real rename
    would in identity.ingest.
    """
    candidates = [(k, _normalise(k, v)) for k, v in candidates]
    candidates = [(k, v) for k, v in candidates if v]
    now = db.sqlite_now()

    for kind, value in candidates:
        if kind not in STRICT_KINDS:
            continue
        existing = conn.execute(
            "SELECT value FROM device_aliases WHERE device_id = ? AND kind = ?",
            (device_id, kind),
        ).fetchall()
        for row in existing:
            if row["value"] != value:
                _record_conflict(
                    conn, now, source, candidates, str(device_id),
                    "{} changed under polling: on file={!r} observed={!r} (the "
                    "device behind this management address may have been "
                    "swapped)".format(kind, row["value"], value),
                )
                raise Conflict([device_id])

    _update_device(conn, device_id, candidates, fields, now)
    conn.execute(
        """INSERT INTO device_sources (device_id, source, ref, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(device_id, source, ref) DO UPDATE SET last_seen = excluded.last_seen""",
        (device_id, source, ref, now, now),
    )
    return device_id


def merge(conn, survivor_id, loser_id):
    """Fold loser_id into survivor_id. Operator-invoked only — never automatic.

    (kind, value) aliases already belong to exactly one device_id each (the
    primary key enforces it), so repointing them can never collide. Tables
    with their own per-device uniqueness (device_sources, credentials,
    interfaces, template_vars, lldp_neighbors, adjacencies) move whatever
    the survivor doesn't already have and drop the rest — the survivor's
    existing data always wins over the loser's.
    """
    if survivor_id == loser_id:
        raise ValueError("cannot merge a device into itself")
    if not conn.execute("SELECT 1 FROM devices WHERE id = ?", (survivor_id,)).fetchone():
        raise ValueError("survivor device not found")
    if not conn.execute("SELECT 1 FROM devices WHERE id = ?", (loser_id,)).fetchone():
        raise ValueError("loser device not found")

    conn.execute(
        "UPDATE device_aliases SET device_id = ? WHERE device_id = ?",
        (survivor_id, loser_id),
    )

    for table, cols in (
        ("device_sources", "source, ref, first_seen, last_seen"),
        ("credentials", "username, password, enable_secret, snmp_community, snmp_version, updated_at"),
        ("interfaces", "name, description, ip, prefix_len, network, vrf, admin_status, "
                       "oper_status, speed, duplex, input_errors, crc_errors, mtu, last_seen"),
        ("template_vars", "key, value"),
        ("lldp_neighbors", "local_if, remote_chassis, remote_sysname, remote_port, "
                           "remote_mgmt_ip, remote_device_id, last_seen"),
        ("adjacencies", "proto, peer_ip, peer_device_id, remote_as, area, state, "
                        "uptime, prefixes, local_if, last_seen"),
    ):
        conn.execute(
            "INSERT OR IGNORE INTO {t} (device_id, {c}) "
            "SELECT ?, {c} FROM {t} WHERE device_id = ?".format(t=table, c=cols),
            (survivor_id, loser_id),
        )
        conn.execute("DELETE FROM {} WHERE device_id = ?".format(table), (loser_id,))

    # Plain reference columns, no uniqueness to protect.
    conn.execute("UPDATE poll_history SET device_id = ? WHERE device_id = ?",
                 (survivor_id, loser_id))
    conn.execute("UPDATE lldp_neighbors SET remote_device_id = ? WHERE remote_device_id = ?",
                 (survivor_id, loser_id))
    conn.execute("UPDATE adjacencies SET peer_device_id = ? WHERE peer_device_id = ?",
                 (survivor_id, loser_id))

    conn.execute("DELETE FROM devices WHERE id = ?", (loser_id,))
    conn.execute(
        """UPDATE merge_conflicts SET resolved = 1
           WHERE resolved = 0
             AND (',' || device_ids || ',') LIKE '%,' || ? || ',%'
             AND (',' || device_ids || ',') LIKE '%,' || ? || ',%'""",
        (str(survivor_id), str(loser_id)),
    )
