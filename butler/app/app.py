"""confetti-butler — device inventory, IPAM, config templates, syslog, topology.

Unlike confetti-traffic's hub/app/app.py, which keeps nearly everything in one
file, a section that outgrows a screenful moves to its own module under app/
(identity.py, ipam.py, rendering.py, poller.py, collectors/, parsers/) and
app.py keeps only the @app.route wiring for it.
"""

import os
import shutil
import subprocess

from flask import Flask, jsonify, render_template, request, Response

from . import config
from . import db
from . import identity
from . import ipam
from . import poller
from . import rendering
from . import syslog_server
from .collectors import confetti
from .collectors import seedfile
from .collectors import sweep as sweep_collector
from .collectors import vcenter as vcenter_collector

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "..", "templates"),
    static_folder=os.path.join(os.path.dirname(__file__), "..", "static"),
)


@app.teardown_appcontext
def _close_db(exc):
    db.close_db(exc)


# ---------------------------------------------------------------------------
# Clock
#
# Ported from confetti-traffic's /api/time. Several later features here (syslog
# correlation, poll-history windows) depend on the server's clock being
# disciplined, same reasoning as confetti-traffic's hub.
# ---------------------------------------------------------------------------

CHRONY_TIMEOUT_S = 3
LOCAL_REFID_PREFIX = "7F7F"


def parse_chrony_tracking(text):
    """Parse `chronyc tracking` output. Returns None if unrecognised."""
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip().lower()] = value.strip()

    if "stratum" not in fields and "reference id" not in fields:
        return None

    try:
        stratum = int(fields.get("stratum", ""))
    except ValueError:
        stratum = None

    offset = None
    system_time = fields.get("system time", "")
    parts = system_time.split()
    if parts:
        try:
            offset = float(parts[0])
            if "slow" in system_time:
                offset = -offset
        except ValueError:
            offset = None

    refid = fields.get("reference id", "")
    source = None
    if "(" in refid and ")" in refid:
        source = refid[refid.index("(") + 1:refid.rindex(")")].strip() or None
    if not source:
        source = refid.split()[0] if refid.split() else None

    return {
        "stratum": stratum,
        "system_offset_s": offset,
        "synced": fields.get("leap status", "").lower() == "normal",
        "local_only": refid.upper().startswith(LOCAL_REFID_PREFIX),
        "source": source,
    }


@app.route("/api/time", methods=["GET"])
def api_time():
    """Server clock plus chrony tracking state. Never 500s."""
    out = {"utc": db.iso(db.sqlite_now()), "chrony": None}

    if shutil.which("chronyc") is None:
        out["reason"] = "chronyc not installed"
        return jsonify(out)

    try:
        proc = subprocess.run(
            ["chronyc", "-n", "tracking"],
            capture_output=True, text=True, timeout=CHRONY_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        out["reason"] = "chronyc timed out"
        return jsonify(out)
    except OSError as exc:
        out["reason"] = "chronyc failed: {}".format(exc.__class__.__name__)
        return jsonify(out)

    if proc.returncode != 0:
        out["reason"] = (proc.stderr or proc.stdout or "chronyd not responding").strip()[:120]
        return jsonify(out)

    tracking = parse_chrony_tracking(proc.stdout)
    if tracking is None:
        out["reason"] = "unrecognised chronyc output"
        return jsonify(out)

    out["chrony"] = tracking
    return jsonify(out)


# ---------------------------------------------------------------------------
# Self-health
#
# Ported from confetti-traffic's /api/health. Same never-500 discipline: every
# metric and every service check is independently guarded, and this is
# routinely exercised on a non-Alpine dev box where rc-service doesn't exist.
# ---------------------------------------------------------------------------

def _service_status(name):
    """Query one OpenRC service via `rc-service <name> status`."""
    if shutil.which("rc-service") is None:
        return {"status": None, "detail": "rc-service not installed"}
    try:
        proc = subprocess.run(
            ["rc-service", name, "status"],
            capture_output=True, text=True, timeout=config.HEALTH_SERVICE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return {"status": None, "detail": "rc-service timed out"}
    except OSError as exc:
        return {"status": None, "detail": "rc-service failed: {}".format(exc.__class__.__name__)}

    out = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode == 0:
        return {"status": "up", "detail": out[:120] or "started"}
    return {"status": "down", "detail": out[:120] or "not running"}


def _load_avg():
    try:
        one, five, fifteen = os.getloadavg()
        return {"1m": one, "5m": five, "15m": fifteen}
    except (OSError, AttributeError):
        return None


def _memory_info():
    try:
        fields = {}
        with open("/proc/meminfo", "r") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                rest = rest.strip()
                if rest.endswith("kB"):
                    rest = rest[:-2].strip()
                try:
                    fields[key] = int(rest)
                except ValueError:
                    pass
        total = fields.get("MemTotal")
        available = fields.get("MemAvailable")
        if total is None or available is None:
            return None
        used = total - available
        pct = round(used / total * 100, 1) if total else None
        return {"total_kb": total, "available_kb": available, "used_kb": used, "used_pct": pct}
    except OSError:
        return None


def _disk_info():
    try:
        directory = os.path.dirname(os.path.abspath(config.DB_PATH)) or "."
        usage = shutil.disk_usage(directory)
        pct = round(usage.used / usage.total * 100, 1) if usage.total else None
        return {
            "path": directory,
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "used_pct": pct,
        }
    except OSError:
        return None


def _uptime_s():
    try:
        with open("/proc/uptime", "r") as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


@app.route("/api/health", methods=["GET"])
def api_health():
    """Server self-health: services, load, memory, disk, uptime. Never 500s."""
    services = {}
    for name in config.HEALTH_SERVICES:
        try:
            services[name] = _service_status(name)
        except Exception as exc:
            services[name] = {"status": None, "detail": "check failed: {}".format(exc.__class__.__name__)}

    return jsonify({
        "checked_at": db.iso(db.sqlite_now()),
        "services": services,
        "syslog_listening": syslog_server.is_listening(),
        "poller_running": poller.is_running(),
        "load_avg": _load_avg(),
        "memory": _memory_info(),
        "disk": _disk_info(),
        "uptime_s": _uptime_s(),
    })


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/favicon.ico")
def favicon():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
        '<rect width="16" height="16" rx="3" fill="#0d1117"/>'
        '<rect x="3" y="3" width="4" height="4" fill="#58a6ff"/>'
        '<rect x="9" y="3" width="4" height="4" fill="#58a6ff"/>'
        '<rect x="3" y="9" width="4" height="4" fill="#58a6ff"/>'
        '<rect x="9" y="9" width="4" height="4" fill="#3fb950"/>'
        "</svg>"
    )
    return Response(svg, mimetype="image/svg+xml")


@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/devices")
def devices_page():
    return render_template("devices.html")


@app.route("/devices/<int:device_id>")
def device_page(device_id):
    return render_template("device.html", device_id=device_id)


@app.route("/conflicts")
def conflicts_page():
    return render_template("conflicts.html")


# ---------------------------------------------------------------------------
# Devices
#
# The only writer of the devices table other than the poller (which only
# updates poll_state/next_poll_at/fail_count on rows that already exist) —
# every path that can create or enrich a device goes through identity.ingest
# so the four seeding sources named in the plan can never silently produce
# duplicate rows. See identity.py for the merge rule.
# ---------------------------------------------------------------------------

def device_row(r):
    d = dict(r)
    d["last_seen"] = db.iso(d["last_seen"])
    d["first_seen"] = db.iso(d["first_seen"])
    d["next_poll_at"] = db.iso(d["next_poll_at"]) if d["next_poll_at"] else ""
    d["last_poll_ok"] = db.iso(d["last_poll_ok"]) if d["last_poll_ok"] else None
    return d


@app.route("/api/devices", methods=["GET"])
def api_list_devices():
    conn = db.get_db()
    rows = conn.execute(
        """SELECT id, key, hostname, mgmt_ip, vendor, platform, model, serial,
                  os_version, role, site, enabled, poll_interval_s, next_poll_at,
                  poll_state, fail_count, first_seen, last_seen, last_poll_ok
           FROM devices ORDER BY role, hostname"""
    ).fetchall()
    return jsonify([device_row(r) for r in rows])


@app.route("/api/devices", methods=["POST"])
def api_add_device():
    """Manually add (or enrich, if it already exists) a device.

    A manual add still goes through identity.ingest, so adding a device by
    hand that a sweep already found under a different name updates that same
    row instead of creating a duplicate.
    """
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Body must be a JSON object"}), 400

    candidates = [
        (kind, data[kind])
        for kind in ("serial", "vmuuid", "mgmt_ip", "hostname")
        if data.get(kind)
    ]
    if not candidates:
        return jsonify({"error": "at least one of serial, vmuuid, mgmt_ip, hostname is required"}), 400

    fields = {k: data.get(k) for k in identity.DEVICE_FIELDS}
    conn = db.get_db()
    try:
        device_id = identity.ingest(conn, candidates, fields, "manual")
    except identity.Conflict as exc:
        conn.commit()
        return jsonify({
            "error": "ambiguous device match",
            "device_ids": exc.device_ids,
            "resolve": "POST /api/devices/<survivor_id>/merge with {\"loser_id\": <id>}",
        }), 409

    if "poll_interval_s" in data and data["poll_interval_s"]:
        conn.execute(
            "UPDATE devices SET poll_interval_s = ? WHERE id = ?",
            (int(data["poll_interval_s"]), device_id),
        )
    conn.commit()

    row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    return jsonify(device_row(row)), 201


@app.route("/api/devices/<int:device_id>", methods=["GET"])
def api_get_device(device_id):
    conn = db.get_db()
    row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    out = device_row(row)
    out["aliases"] = [dict(a) for a in conn.execute(
        "SELECT kind, value, first_seen FROM device_aliases WHERE device_id = ? ORDER BY kind", (device_id,)
    ).fetchall()]
    out["sources"] = [dict(s) for s in conn.execute(
        "SELECT source, ref, first_seen, last_seen FROM device_sources WHERE device_id = ? ORDER BY source",
        (device_id,),
    ).fetchall()]
    return jsonify(out)


@app.route("/api/devices/<int:device_id>/interfaces", methods=["GET"])
def api_device_interfaces(device_id):
    conn = db.get_db()
    rows = conn.execute(
        """SELECT name, description, ip, prefix_len, network, vrf, admin_status, oper_status,
                  speed, duplex, input_errors, crc_errors, mtu, last_seen
           FROM interfaces WHERE device_id = ? ORDER BY name""",
        (device_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["last_seen"] = db.iso(d["last_seen"])
        out.append(d)
    return jsonify(out)


CREDENTIAL_FIELDS = ("username", "password", "enable_secret", "snmp_community", "snmp_version")
CREDENTIAL_SECRETS = ("password", "enable_secret", "snmp_community")


@app.route("/api/devices/<int:device_id>/credentials", methods=["GET"])
def api_get_device_credentials(device_id):
    """What the device page's credentials form needs — never the secrets
    themselves, only whether each one is set. Unset fields fall back to the
    BUTLER_SSH_* / BUTLER_SNMP_* environment defaults at poll time."""
    conn = db.get_db()
    if not conn.execute("SELECT 1 FROM devices WHERE id = ?", (device_id,)).fetchone():
        return jsonify({"error": "not found"}), 404
    row = conn.execute("SELECT * FROM credentials WHERE device_id = ?", (device_id,)).fetchone()
    out = {"username": row["username"] if row else None,
           "snmp_version": row["snmp_version"] if row else None,
           "updated_at": db.iso(row["updated_at"]) if row else None}
    for k in CREDENTIAL_SECRETS:
        out[k + "_set"] = bool(row and row[k])
    return jsonify(out)


@app.route("/api/devices/<int:device_id>/credentials", methods=["PUT"])
def api_device_credentials(device_id):
    """Per-device credential override. Plaintext in v1 — see config.py."""
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Body must be a JSON object"}), 400

    conn = db.get_db()
    if not conn.execute("SELECT 1 FROM devices WHERE id = ?", (device_id,)).fetchone():
        return jsonify({"error": "not found"}), 404

    # Only fields present in the body change: saving a new password must not
    # wipe the stored enable secret or SNMP settings. Send "" to clear one.
    fields = [k for k in CREDENTIAL_FIELDS if k in data]
    if not fields:
        return jsonify({"error": "no credential fields in body", "fields": list(CREDENTIAL_FIELDS)}), 400
    now = db.sqlite_now()
    conn.execute(
        "INSERT OR IGNORE INTO credentials (device_id, snmp_version, updated_at) VALUES (?, '2c', ?)",
        (device_id, now),
    )
    conn.execute(
        "UPDATE credentials SET {}, updated_at = ? WHERE device_id = ?".format(
            ", ".join("{} = ?".format(k) for k in fields)),
        [data[k] for k in fields] + [now, device_id],
    )
    conn.commit()
    return jsonify({"status": "ok", "device_id": device_id})


@app.route("/api/devices/<int:device_id>/poll", methods=["POST"])
def api_poll_device_now(device_id):
    """Poll a device immediately, inline on this request — see poller.poll_now."""
    conn = db.get_db()
    if not conn.execute("SELECT 1 FROM devices WHERE id = ?", (device_id,)).fetchone():
        return jsonify({"error": "not found"}), 404

    conn.execute("UPDATE devices SET poll_state = 'running' WHERE id = ?", (device_id,))
    conn.commit()
    # poll_now opens its own connection (it can run from the poller thread
    # pool too), so this request's db.get_db() connection must not be held
    # across it — commit above releases the lock this thread was holding.
    results = poller.poll_now(device_id)
    if results is None:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": "ok", "results": results})


DEVICE_EDITABLE = identity.DEVICE_FIELDS + ("enabled", "poll_interval_s", "template_name")


@app.route("/api/devices/<int:device_id>", methods=["PUT"])
def api_update_device(device_id):
    """Authoritative operator edit — bypasses the identity ladder entirely.

    Unlike identity.ingest, an explicit PUT can blank a field (e.g. clearing
    a wrong site tag), so empty strings are accepted here rather than
    filtered out.
    """
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Body must be a JSON object"}), 400

    updates = {k: data[k] for k in DEVICE_EDITABLE if k in data}
    if not updates:
        return jsonify({"error": "no editable fields in body", "editable": list(DEVICE_EDITABLE)}), 400

    conn = db.get_db()
    if not conn.execute("SELECT 1 FROM devices WHERE id = ?", (device_id,)).fetchone():
        return jsonify({"error": "not found"}), 404

    set_clause = ", ".join("{} = :{}".format(c, c) for c in updates)
    updates["id"] = device_id
    conn.execute("UPDATE devices SET {} WHERE id = :id".format(set_clause), updates)
    taken = identity.add_operator_aliases(conn, device_id, updates)
    conn.commit()
    row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    out = device_row(row)
    if taken:
        out["alias_warnings"] = [
            "{} {} already belongs to device {} — merge them if they are the same box".format(
                t["kind"], t["value"], t["device_id"])
            for t in taken
        ]
    return jsonify(out)


@app.route("/api/devices/<int:device_id>", methods=["DELETE"])
def api_delete_device(device_id):
    conn = db.get_db()
    cur = conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
    # Child tables cascade, but these three don't: poll_history has no FK,
    # and other devices' neighbor/peer rows only reference this one. Same
    # three tables identity.merge re-points. A NULLed reference re-renders
    # as a ghost node.
    conn.execute("DELETE FROM poll_history WHERE device_id = ?", (device_id,))
    conn.execute("UPDATE lldp_neighbors SET remote_device_id = NULL WHERE remote_device_id = ?",
                 (device_id,))
    conn.execute("UPDATE adjacencies SET peer_device_id = NULL WHERE peer_device_id = ?",
                 (device_id,))
    conn.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": "deleted", "id": device_id})


@app.route("/api/devices/<int:survivor_id>/merge", methods=["POST"])
def api_merge_device(survivor_id):
    """Fold another device into this one. Operator-invoked only — see identity.merge."""
    data = request.get_json(force=True, silent=True) or {}
    loser_id = data.get("loser_id")
    if not loser_id:
        return jsonify({"error": "loser_id is required"}), 400

    conn = db.get_db()
    try:
        identity.merge(conn, survivor_id, int(loser_id))
    except ValueError as exc:
        conn.rollback()
        return jsonify({"error": str(exc)}), 400
    conn.commit()
    row = conn.execute("SELECT * FROM devices WHERE id = ?", (survivor_id,)).fetchone()
    return jsonify(device_row(row))


@app.route("/api/conflicts", methods=["GET"])
def api_conflicts():
    """Unresolved identity conflicts, newest first — see /conflicts page."""
    conn = db.get_db()
    resolved = request.args.get("resolved", "0")
    rows = conn.execute(
        "SELECT id, seen_at, source, candidate, device_ids, detail, resolved "
        "FROM merge_conflicts WHERE resolved = ? ORDER BY seen_at DESC",
        (1 if resolved in ("1", "true") else 0,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["seen_at"] = db.iso(d["seen_at"])
        out.append(d)
    return jsonify(out)


@app.route("/api/conflicts/<int:conflict_id>/dismiss", methods=["POST"])
def api_dismiss_conflict(conflict_id):
    """Mark a conflict resolved without merging. Needed for single-device
    conflicts (a serial changed under polling, an IP reused by a new box):
    there is no second device to merge, so merge can never clear them."""
    conn = db.get_db()
    cur = conn.execute("UPDATE merge_conflicts SET resolved = 1 WHERE id = ?", (conflict_id,))
    conn.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": "dismissed", "id": conflict_id})


@app.route("/api/discover/seedfile", methods=["POST"])
def api_discover_seedfile():
    """Ingest devices from a YAML seed file. Body: {"path": "seed/devices.yaml"}."""
    data = request.get_json(force=True, silent=True) or {}
    path = data.get("path")
    if not path:
        return jsonify({"error": "path is required"}), 400
    if not os.path.isfile(path):
        return jsonify({"error": "file not found", "path": path}), 400

    conn = db.get_db()
    try:
        device_ids, conflicts = seedfile.load(conn, path)
    except Exception as exc:
        conn.rollback()
        return jsonify({"error": "failed to load seed file: {}".format(exc)}), 400
    conn.commit()
    return jsonify({"status": "ok", "ingested": len(device_ids), "device_ids": device_ids,
                     "conflicts": conflicts})


@app.route("/api/discover/sweep", methods=["POST"])
def api_discover_sweep():
    """Scan a CIDR for hosts with SSH open. Body: {"cidr": "10.0.0.0/24"}.
    A live network scan — runs only when explicitly invoked, never on a
    schedule."""
    data = request.get_json(force=True, silent=True) or {}
    cidr = data.get("cidr")
    if not cidr:
        return jsonify({"error": "cidr is required"}), 400

    conn = db.get_db()
    try:
        found, device_ids, conflicts = sweep_collector.sweep(conn, cidr)
    except ValueError as exc:
        return jsonify({"error": "invalid cidr: {}".format(exc)}), 400
    conn.commit()
    return jsonify({"status": "ok", "hosts_found": len(found), "device_ids": device_ids,
                     "conflicts": conflicts})


@app.route("/api/discover/vcenter", methods=["POST"])
def api_discover_vcenter():
    """Body: {"url", "username", "password", "name_filter" (optional)}."""
    data = request.get_json(force=True, silent=True) or {}
    if not all(data.get(k) for k in ("url", "username", "password")):
        return jsonify({"error": "url, username and password are required"}), 400

    conn = db.get_db()
    try:
        device_ids, conflicts = vcenter_collector.discover(
            conn, data["url"].rstrip("/"), data["username"], data["password"],
            name_filter=data.get("name_filter"),
        )
    except Exception as exc:
        return jsonify({"error": "vCenter request failed: {}".format(exc)}), 502
    conn.commit()
    return jsonify({"status": "ok", "device_ids": device_ids, "conflicts": conflicts})


@app.route("/api/discover/confetti", methods=["POST"])
def api_discover_confetti():
    """Body: {"hub_url": "http://10.0.0.100"}. Imports confetti-traffic's node
    fleet as role='node' devices — see collectors/confetti.py."""
    data = request.get_json(force=True, silent=True) or {}
    hub_url = data.get("hub_url")
    if not hub_url:
        return jsonify({"error": "hub_url is required"}), 400

    conn = db.get_db()
    try:
        device_ids, conflicts = confetti.discover(conn, hub_url)
    except Exception as exc:
        return jsonify({"error": "confetti-traffic hub request failed: {}".format(exc)}), 502
    conn.commit()
    return jsonify({"status": "ok", "device_ids": device_ids, "conflicts": conflicts})


# ---------------------------------------------------------------------------
# Syslog
#
# The receiver is syslog_server.py, started by serve.py in a daemon thread;
# these routes only read what it stored. device_id is resolved here, at
# query time, against device_aliases — never stamped at insert (see
# syslog_server.py's module docstring) — so a device added or re-addressed
# after a message arrives still correlates retroactively. Rows are
# attacker-controlled in the sense that anything on the segment can send
# UDP/514 with no authentication, so they are rendered as text and never
# interpreted — same posture as confetti-traffic's syslog page.
# ---------------------------------------------------------------------------

SYSLOG_MAX_LIMIT = 2000


def syslog_row(r):
    d = dict(r)
    d["received_at"] = db.iso(d["received_at"])
    return d


@app.route("/api/syslog", methods=["GET"])
def api_syslog():
    """Stored syslog messages, newest first.

    ?minutes=N (default 60), or ?from=&to= for a pinned window — from/to win
    when either is present. Also ?host= (matches the parsed hostname or the
    source IP), ?severity=N, ?q= substring, ?limit=N, ?device_id=N.
    """
    args = request.args
    where = []
    params = []

    frm = db.sqlite_ts_arg(args.get("from"))
    to = db.sqlite_ts_arg(args.get("to"))
    if frm or to:
        if frm:
            where.append("s.received_at >= ?")
            params.append(frm)
        if to:
            where.append("s.received_at <= ?")
            params.append(to)
    else:
        try:
            minutes = int(args.get("minutes", "60"))
        except ValueError:
            minutes = 60
        where.append("s.received_at >= datetime('now', ? || ' minutes')")
        params.append("-{:d}".format(abs(minutes)))

    host = args.get("host")
    if host:
        where.append("(s.host = ? OR s.source_ip = ?)")
        params.extend([host, host])

    device_id = args.get("device_id")
    if device_id:
        where.append("COALESCE(dia.device_id, dih.device_id) = ?")
        params.append(device_id)

    severity = args.get("severity")
    if severity not in (None, "", "all"):
        try:
            level = int(severity)
        except ValueError:
            level = None
        if level is not None:
            where.append("(s.severity <= ? OR s.severity IS NULL)")
            params.append(level)

    q = args.get("q")
    if q:
        where.append("(s.message LIKE ? ESCAPE '\\' OR s.mnemonic LIKE ? ESCAPE '\\' "
                     "OR s.raw LIKE ? ESCAPE '\\')")
        params.extend([db.like_arg(q)] * 3)

    try:
        limit = int(args.get("limit", SYSLOG_MAX_LIMIT))
    except ValueError:
        limit = SYSLOG_MAX_LIMIT
    limit = max(1, min(limit, SYSLOG_MAX_LIMIT))

    sql = """SELECT s.id, s.received_at, s.source_ip, s.host, s.facility, s.severity,
                    s.mnemonic, s.device_time, s.message,
                    COALESCE(dia.device_id, dih.device_id) AS device_id
             FROM syslog s
             LEFT JOIN device_aliases dia ON dia.kind = 'mgmt_ip' AND dia.value = s.source_ip
             LEFT JOIN device_aliases dih ON dih.kind = 'hostname' AND dih.value = LOWER(s.host)"""
    if where:
        sql += " WHERE " + " AND ".join(where)
    # id breaks ties: received_at has one-second resolution and a device can
    # emit a whole interface flap inside one second, which must stay in order.
    sql += " ORDER BY s.received_at DESC, s.id DESC LIMIT ?"
    params.append(limit)

    conn = db.get_db()
    rows = conn.execute(sql, params).fetchall()
    return jsonify([syslog_row(r) for r in rows])


@app.route("/api/syslog/sources", methods=["GET"])
def api_syslog_sources():
    """Distinct senders with message counts, for the page's filter dropdown.

    Bounded to a window (?minutes=, default 1440) so it stays an indexed
    range rather than a full scan of the row cap on every page load.
    """
    try:
        minutes = abs(int(request.args.get("minutes", "1440")))
    except ValueError:
        minutes = 1440

    conn = db.get_db()
    rows = conn.execute(
        """SELECT COALESCE(NULLIF(host, ''), source_ip) AS name, COUNT(*) AS count
           FROM syslog
           WHERE received_at >= datetime('now', ? || ' minutes')
           GROUP BY name
           ORDER BY count DESC, name""",
        ("-{:d}".format(minutes),),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/syslog")
def syslog_page():
    return render_template("syslog.html")


# ---------------------------------------------------------------------------
# IPAM — discovery-first, see ipam.py. interfaces is the source of truth;
# these routes only read it and the findings ipam.analyze() computes.
# ---------------------------------------------------------------------------

@app.route("/ipam")
def ipam_page():
    return render_template("ipam.html")


@app.route("/api/ipam/addresses", methods=["GET"])
def api_ipam_addresses():
    conn = db.get_db()
    rows = conn.execute(
        """SELECT d.id AS device_id, d.hostname, i.name AS interface, i.ip, i.prefix_len,
                  i.network, i.vrf, i.admin_status, i.oper_status, i.last_seen
           FROM interfaces i JOIN devices d ON d.id = i.device_id
           WHERE i.ip IS NOT NULL
           ORDER BY i.network, i.ip"""
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["last_seen"] = db.iso(d["last_seen"])
        out.append(d)
    return jsonify(out)


@app.route("/api/ipam/prefixes", methods=["GET"])
def api_ipam_prefixes():
    """Distinct networks in use, with how many interfaces/devices sit on each."""
    conn = db.get_db()
    rows = conn.execute(
        """SELECT i.network, COUNT(*) AS interfaces, COUNT(DISTINCT i.device_id) AS devices
           FROM interfaces i
           WHERE i.network IS NOT NULL
           GROUP BY i.network
           ORDER BY i.network"""
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/ipam/findings", methods=["GET"])
def api_ipam_findings():
    conn = db.get_db()
    rows = conn.execute(
        "SELECT id, checked_at, kind, a, b, detail FROM ipam_findings ORDER BY checked_at DESC, id"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["checked_at"] = db.iso(d["checked_at"])
        out.append(d)
    return jsonify(out)


@app.route("/api/ipam/analyze", methods=["POST"])
def api_ipam_analyze():
    conn = db.get_db()
    count = ipam.analyze(conn)
    conn.commit()
    return jsonify({"status": "ok", "findings": count})


# ---------------------------------------------------------------------------
# Config templates
#
# Pull-only: nothing here ever writes to a device. A template renders
# against a device's own polled facts (see rendering.build_context) and
# GET /configs/<key>.cfg serves the result as plain text for a device to
# fetch on its own console — "copy http://butler/configs/<key>.cfg
# running-config". The endpoint is intentionally unauthenticated (see the
# plan's config-endpoint decision); check_hardcoded_secrets on save is the
# corresponding rule that templates must not carry real credentials.
# ---------------------------------------------------------------------------

@app.route("/templates")
def templates_page():
    return render_template("templates_editor.html")


def template_row(r):
    d = dict(r)
    d["updated_at"] = db.iso(d["updated_at"])
    return d


@app.route("/api/templates", methods=["GET"])
def api_list_templates():
    conn = db.get_db()
    rows = conn.execute(
        "SELECT name, note, updated_at, version FROM templates ORDER BY name"
    ).fetchall()
    return jsonify([template_row(r) for r in rows])


@app.route("/api/templates", methods=["POST"])
def api_create_template():
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict) or not data.get("name"):
        return jsonify({"error": "name is required"}), 400

    name = data["name"]
    body = data.get("body", "")
    conn = db.get_db()
    if conn.execute("SELECT 1 FROM templates WHERE name = ?", (name,)).fetchone():
        return jsonify({"error": "a template named '{}' already exists".format(name)}), 409

    now = db.sqlite_now()
    conn.execute(
        "INSERT INTO templates (name, body, note, updated_at, version) VALUES (?, ?, ?, ?, 1)",
        (name, body, data.get("note", ""), now),
    )
    conn.execute(
        "INSERT INTO template_versions (name, version, body, saved_at) VALUES (?, 1, ?, ?)",
        (name, body, now),
    )
    conn.commit()
    warnings = rendering.check_hardcoded_secrets(body)
    return jsonify({"status": "ok", "name": name, "warnings": warnings}), 201


@app.route("/api/templates/<name>", methods=["GET"])
def api_get_template(name):
    conn = db.get_db()
    row = conn.execute(
        "SELECT name, body, note, updated_at, version FROM templates WHERE name = ?", (name,)
    ).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(template_row(row))


@app.route("/api/templates/<name>", methods=["PUT"])
def api_update_template(name):
    """Every save appends a template_versions row — a bad edit must be
    recoverable, since this is a template a router will pull and apply."""
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Body must be a JSON object"}), 400

    conn = db.get_db()
    row = conn.execute("SELECT version FROM templates WHERE name = ?", (name,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404

    body = data.get("body")
    if body is None:
        return jsonify({"error": "body is required"}), 400
    next_version = row["version"] + 1
    now = db.sqlite_now()

    conn.execute(
        "UPDATE templates SET body = ?, note = COALESCE(?, note), updated_at = ?, version = ? "
        "WHERE name = ?",
        (body, data.get("note"), now, next_version, name),
    )
    conn.execute(
        "INSERT INTO template_versions (name, version, body, saved_at) VALUES (?, ?, ?, ?)",
        (name, next_version, body, now),
    )
    conn.commit()
    warnings = rendering.check_hardcoded_secrets(body)
    return jsonify({"status": "ok", "name": name, "version": next_version, "warnings": warnings})


@app.route("/api/templates/<name>", methods=["DELETE"])
def api_delete_template(name):
    conn = db.get_db()
    cur = conn.execute("DELETE FROM templates WHERE name = ?", (name,))
    conn.execute("DELETE FROM template_versions WHERE name = ?", (name,))
    conn.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": "deleted", "name": name})


@app.route("/api/templates/<name>/versions", methods=["GET"])
def api_template_versions(name):
    conn = db.get_db()
    rows = conn.execute(
        "SELECT id, version, saved_at FROM template_versions WHERE name = ? ORDER BY version DESC",
        (name,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["saved_at"] = db.iso(d["saved_at"])
        out.append(d)
    return jsonify(out)


@app.route("/api/templates/<name>/preview", methods=["POST"])
def api_template_preview(name):
    """Render a template against a device's real polled facts. Body:
    {"device_id": N} — or {"body": "..."} to preview unsaved edits."""
    data = request.get_json(force=True, silent=True) or {}
    device_id = data.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id is required"}), 400

    conn = db.get_db()
    if "body" in data:
        body = data["body"]
    else:
        row = conn.execute("SELECT body FROM templates WHERE name = ?", (name,)).fetchone()
        if not row:
            return jsonify({"error": "template not found"}), 404
        body = row["body"]

    try:
        result = rendering.preview(conn, int(device_id), body)
    except rendering.RenderError as exc:
        return jsonify({"error": "render failed: {}".format(exc)}), 400
    return jsonify(result)


@app.route("/api/devices/<int:device_id>/vars", methods=["GET"])
def api_get_device_vars(device_id):
    conn = db.get_db()
    rows = conn.execute(
        "SELECT key, value FROM template_vars WHERE device_id = ? ORDER BY key", (device_id,)
    ).fetchall()
    return jsonify({r["key"]: r["value"] for r in rows})


@app.route("/api/devices/<int:device_id>/vars", methods=["PUT"])
def api_set_device_vars(device_id):
    """Replace the full var set for a device with the given object — simpler
    than a partial-merge PATCH, and a template editor round-trips the whole
    set anyway."""
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Body must be an object of key: value"}), 400

    conn = db.get_db()
    if not conn.execute("SELECT 1 FROM devices WHERE id = ?", (device_id,)).fetchone():
        return jsonify({"error": "not found"}), 404

    conn.execute("DELETE FROM template_vars WHERE device_id = ?", (device_id,))
    for key, value in data.items():
        conn.execute(
            "INSERT INTO template_vars (device_id, key, value) VALUES (?, ?, ?)",
            (device_id, key, str(value)),
        )
    conn.commit()
    return jsonify({"status": "ok"})


@app.route("/configs/")
def configs_index():
    """Index of devices with an assigned template, for a human browsing."""
    conn = db.get_db()
    rows = conn.execute(
        "SELECT key, hostname, template_name FROM devices WHERE template_name IS NOT NULL ORDER BY hostname"
    ).fetchall()
    lines = ["{}  ->  /configs/{}.cfg  (template: {})".format(r["hostname"], r["key"], r["template_name"])
              for r in rows]
    return Response("\n".join(lines) + "\n", mimetype="text/plain")


@app.route("/configs/<device_key>.cfg")
def serve_config(device_key):
    """The pull endpoint — a device's console runs
    'copy http://butler/configs/<key>.cfg running-config' against this.
    Unauthenticated by design (see the plan's config-endpoint decision):
    templates must not carry real secrets, enforced by check_hardcoded_secrets
    at save time, not by locking this endpoint down.
    """
    conn = db.get_db()
    device = conn.execute("SELECT id, template_name FROM devices WHERE key = ?", (device_key,)).fetchone()
    if not device:
        return Response("no device with key '{}'\n".format(device_key), mimetype="text/plain", status=404)
    if not device["template_name"]:
        return Response("device '{}' has no template assigned\n".format(device_key),
                         mimetype="text/plain", status=404)

    row = conn.execute("SELECT body FROM templates WHERE name = ?", (device["template_name"],)).fetchone()
    if not row:
        return Response("assigned template '{}' no longer exists\n".format(device["template_name"]),
                         mimetype="text/plain", status=500)

    try:
        rendered = rendering.render_for_device(conn, device["id"], row["body"])
    except rendering.RenderError as exc:
        return Response("template render failed: {}\n".format(exc), mimetype="text/plain", status=500)
    return Response(rendered, mimetype="text/plain")


# ---------------------------------------------------------------------------
# Topology
#
# Graph shaped for vis.js's DataSet format. A neighbor/peer that hasn't
# resolved to a known device_id (see identity.py's merge rule and the LLDP/
# BGP/OSPF resolution in collectors/ssh.py) renders as a "ghost" node rather
# than being dropped — an unlabelled edge to nowhere would look like a bug,
# where a distinctly-styled ghost node reads as "something is out there we
# haven't identified yet".
# ---------------------------------------------------------------------------

@app.route("/topology")
def topology_page():
    return render_template("topology.html")


@app.route("/api/topology", methods=["GET"])
def api_topology():
    """?layer=lldp|bgp|ospf restricts edges to one source; default overlays
    all three on the same device nodes."""
    layer = request.args.get("layer")
    conn = db.get_db()

    devices = conn.execute(
        "SELECT id, hostname, role, vendor, platform, enabled FROM devices"
    ).fetchall()
    nodes = {}
    for d in devices:
        nid = "dev:{}".format(d["id"])
        nodes[nid] = {
            "id": nid, "label": d["hostname"], "group": d["role"],
            "device_id": d["id"], "ghost": False,
        }

    ghost_seen = set()

    def ghost_id(kind, key):
        gid = "ghost:{}:{}".format(kind, key)
        if gid not in ghost_seen:
            ghost_seen.add(gid)
            nodes[gid] = {"id": gid, "label": key, "group": "unknown", "device_id": None, "ghost": True}
        return gid

    edges = []

    if layer in (None, "", "lldp"):
        rows = conn.execute(
            """SELECT device_id, local_if, remote_chassis, remote_sysname, remote_port, remote_device_id
               FROM lldp_neighbors"""
        ).fetchall()
        # Two known devices each report the other, so the same cable shows up
        # twice. Key a device-to-device link by both (device, interface) ends
        # and draw it once. If the two sides name a port differently ("Gi1"
        # vs "GigabitEthernet1") the keys differ and both edges stay — a
        # duplicate line is safer than hiding a real parallel link.
        lldp_seen = set()
        for r in rows:
            if r["remote_device_id"]:
                link = frozenset([(r["device_id"], r["local_if"]),
                                  (r["remote_device_id"], r["remote_port"])])
                if link in lldp_seen:
                    continue
                lldp_seen.add(link)
            target = ("dev:{}".format(r["remote_device_id"]) if r["remote_device_id"]
                       else ghost_id("lldp", r["remote_sysname"] or r["remote_chassis"] or "unknown"))
            edges.append({
                "from": "dev:{}".format(r["device_id"]), "to": target, "layer": "lldp",
                "label": "{} - {}".format(r["local_if"], r["remote_port"] or "?"),
            })

    if layer in (None, "", "bgp", "ospf"):
        protos = [layer] if layer in ("bgp", "ospf") else ["bgp", "ospf"]
        qmarks = ",".join("?" * len(protos))
        rows = conn.execute(
            "SELECT device_id, proto, peer_ip, peer_device_id, state, local_if FROM adjacencies "
            "WHERE proto IN ({})".format(qmarks),
            protos,
        ).fetchall()
        for r in rows:
            # One ghost per peer IP across BGP and OSPF — the same router
            # peering both ways is one box, not two.
            target = ("dev:{}".format(r["peer_device_id"]) if r["peer_device_id"]
                       else ghost_id("peer", r["peer_ip"]))
            edges.append({
                "from": "dev:{}".format(r["device_id"]), "to": target, "layer": r["proto"],
                "label": "{} {}".format(r["proto"], r["state"]),
            })

    return jsonify({"nodes": list(nodes.values()), "edges": edges})


@app.route("/api/adjacencies", methods=["GET"])
def api_adjacencies():
    conn = db.get_db()
    rows = conn.execute(
        """SELECT a.device_id, d.hostname, a.proto, a.peer_ip, a.peer_device_id, a.remote_as,
                  a.area, a.state, a.uptime, a.prefixes, a.local_if, a.last_seen
           FROM adjacencies a JOIN devices d ON d.id = a.device_id
           ORDER BY d.hostname, a.proto, a.peer_ip"""
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["last_seen"] = db.iso(d["last_seen"])
        out.append(d)
    return jsonify(out)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

db.init_db()
