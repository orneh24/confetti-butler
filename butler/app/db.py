"""Database helpers shared by app.py, syslog_server.py, poller.py and identity.py.

Split out of app.py (unlike confetti-traffic, which keeps these inline) because
confetti-butler's app.py is itself split into route modules, and several
non-Flask modules (the poller thread, the syslog listener, identity
resolution) need a connection without importing Flask machinery.
"""

import sqlite3
from datetime import datetime, timezone

from flask import g

from . import config


def get_db():
    """Get a database connection for the current Flask request."""
    if "db" not in g:
        g.db = connect()
    return g.db


def connect():
    """Open a new connection with the pragmas every writer must set.

    busy_timeout first: journal_mode itself can contend with another writer,
    so setting the timeout after it would leave that one statement
    unprotected. See config.BUSY_TIMEOUT_MS — this file has three writers
    (Flask, the syslog listener thread, the poller's thread pool), one more
    than confetti-traffic's hub ever had.
    """
    db = sqlite3.connect(config.DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout={:d}".format(config.BUSY_TIMEOUT_MS))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create tables if they don't exist. Safe to call on every startup."""
    db = connect()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS devices (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            key             TEXT NOT NULL UNIQUE,
            hostname        TEXT NOT NULL,
            mgmt_ip         TEXT,
            vendor          TEXT NOT NULL DEFAULT 'cisco',
            platform        TEXT NOT NULL DEFAULT 'cisco_ios',
            model           TEXT,
            serial          TEXT,
            os_version      TEXT,
            role            TEXT NOT NULL DEFAULT 'router',
            site            TEXT NOT NULL DEFAULT '',
            template_name   TEXT REFERENCES templates(name) ON DELETE SET NULL,
            enabled         INTEGER NOT NULL DEFAULT 1,
            poll_interval_s INTEGER NOT NULL DEFAULT 300,
            next_poll_at    TEXT NOT NULL DEFAULT '',
            poll_state      TEXT NOT NULL DEFAULT 'idle',
            fail_count      INTEGER NOT NULL DEFAULT 0,
            first_seen      TEXT NOT NULL,
            last_seen       TEXT NOT NULL,
            last_poll_ok    TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_devices_next_poll ON devices(enabled, poll_state, next_poll_at);

        -- Every identifier ever observed for a device, across every ingest
        -- path. This is what lets identity.py recognise "the same box" seen
        -- twice under two different names. See identity.py for the merge rule.
        CREATE TABLE IF NOT EXISTS device_aliases (
            device_id  INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            kind       TEXT NOT NULL,
            value      TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            PRIMARY KEY (kind, value)
        );
        CREATE INDEX IF NOT EXISTS idx_aliases_device ON device_aliases(device_id);

        -- Which ingest path(s) have seen this device. Many rows per device —
        -- a device found by both a subnet sweep and vCenter is one devices
        -- row with two device_sources rows.
        CREATE TABLE IF NOT EXISTS device_sources (
            device_id  INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            source     TEXT NOT NULL,
            ref        TEXT NOT NULL DEFAULT '',
            first_seen TEXT NOT NULL,
            last_seen  TEXT NOT NULL,
            PRIMARY KEY (device_id, source, ref)
        );

        -- Written when an ingest path's identifiers match more than one
        -- existing device. Ambiguity is never auto-resolved — see
        -- identity.py. An operator resolves these from /conflicts.
        CREATE TABLE IF NOT EXISTS merge_conflicts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            seen_at     TEXT NOT NULL,
            source      TEXT NOT NULL,
            candidate   TEXT NOT NULL,
            device_ids  TEXT NOT NULL,
            detail      TEXT,
            resolved    INTEGER NOT NULL DEFAULT 0
        );

        -- Per-device override of the environment's default credentials.
        -- Plaintext in v1 — see docs/BUILD_GUIDE.md for the file-permission
        -- mitigation (butler.db must be 0600).
        CREATE TABLE IF NOT EXISTS credentials (
            device_id      INTEGER PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
            username       TEXT,
            password       TEXT,
            enable_secret  TEXT,
            snmp_community TEXT,
            snmp_version   TEXT NOT NULL DEFAULT '2c',
            updated_at     TEXT NOT NULL
        );

        -- IPAM is discovery-first: this table IS the source of truth, built
        -- from what devices actually report. No separate prefix-allocation
        -- table in v1.
        CREATE TABLE IF NOT EXISTS interfaces (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id     INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            name          TEXT NOT NULL,
            description   TEXT NOT NULL DEFAULT '',
            ip            TEXT,
            prefix_len    INTEGER,
            network       TEXT,
            vrf           TEXT NOT NULL DEFAULT 'default',
            admin_status  TEXT,
            oper_status   TEXT,
            speed         TEXT,
            duplex        TEXT,
            input_errors  INTEGER,
            crc_errors    INTEGER,
            mtu           INTEGER,
            last_seen     TEXT NOT NULL,
            UNIQUE(device_id, name)
        );
        CREATE INDEX IF NOT EXISTS idx_if_network ON interfaces(network);
        CREATE INDEX IF NOT EXISTS idx_if_ip ON interfaces(ip);

        -- Rebuilt wholesale by each POST /api/ipam/analyze run, not
        -- incrementally maintained — simpler, and cheap at lab scale.
        CREATE TABLE IF NOT EXISTS ipam_findings (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at TEXT NOT NULL,
            kind       TEXT NOT NULL,
            a          TEXT NOT NULL,
            b          TEXT,
            detail     TEXT
        );

        CREATE TABLE IF NOT EXISTS templates (
            name       TEXT PRIMARY KEY,
            body       TEXT NOT NULL,
            note       TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            version    INTEGER NOT NULL DEFAULT 1
        );
        -- Append-only. A bad edit must be recoverable — this is what a
        -- template pushed live to a device's console needs more than
        -- anything else here.
        CREATE TABLE IF NOT EXISTS template_versions (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            name     TEXT NOT NULL,
            version  INTEGER NOT NULL,
            body     TEXT NOT NULL,
            saved_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_template_versions_name ON template_versions(name);

        -- Operator-set values a template needs that Jinja2 cannot derive
        -- from polled facts alone (e.g. a BGP password, a site note).
        CREATE TABLE IF NOT EXISTS template_vars (
            device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            key       TEXT NOT NULL,
            value     TEXT NOT NULL,
            PRIMARY KEY (device_id, key)
        );

        CREATE TABLE IF NOT EXISTS lldp_neighbors (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id        INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            local_if         TEXT NOT NULL,
            remote_chassis   TEXT,
            remote_sysname   TEXT,
            remote_port      TEXT,
            remote_mgmt_ip   TEXT,
            remote_device_id INTEGER,
            last_seen        TEXT NOT NULL,
            UNIQUE(device_id, local_if, remote_port)
        );

        CREATE TABLE IF NOT EXISTS adjacencies (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id      INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            proto          TEXT NOT NULL,
            peer_ip        TEXT NOT NULL,
            peer_device_id INTEGER,
            remote_as      INTEGER,
            area           TEXT,
            state          TEXT NOT NULL,
            uptime         TEXT,
            prefixes       INTEGER,
            local_if       TEXT,
            last_seen      TEXT NOT NULL,
            UNIQUE(device_id, proto, peer_ip)
        );
        CREATE INDEX IF NOT EXISTS idx_adj_state ON adjacencies(state);

        -- Append-only, age-pruned like confetti-traffic's results table. One row
        -- per poll task (version/interfaces/lldp/bgp/ospf/config), not per device —
        -- a device's LLDP task failing must not hide that its interfaces task
        -- just succeeded.
        CREATE TABLE IF NOT EXISTS poll_history (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id    INTEGER NOT NULL,
            transport    TEXT NOT NULL,
            task         TEXT NOT NULL,
            ok           INTEGER NOT NULL,
            duration_ms  INTEGER,
            error        TEXT,
            output       TEXT,
            started_at   TEXT NOT NULL,
            received_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_poll_received ON poll_history(received_at);
        CREATE INDEX IF NOT EXISTS idx_poll_device ON poll_history(device_id, task);

        -- Running-config backups. One row per distinct config, not per poll:
        -- a row is only added when sha256 differs from the device's latest.
        -- The body is stored raw (it is the backup); the API redacts secrets.
        CREATE TABLE IF NOT EXISTS config_versions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id   INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            sha256      TEXT NOT NULL,
            body        TEXT NOT NULL,
            captured_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_config_device ON config_versions(device_id, id);

        -- State changes seen by the poller (see events.py). Age-pruned.
        CREATE TABLE IF NOT EXISTS events (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            at        TEXT NOT NULL,
            device_id INTEGER REFERENCES devices(id) ON DELETE CASCADE,
            kind      TEXT NOT NULL,
            severity  TEXT NOT NULL,
            subject   TEXT NOT NULL,
            detail    TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_events_at ON events(at);
        CREATE INDEX IF NOT EXISTS idx_events_device ON events(device_id, at);

        -- Same shape as confetti-traffic's syslog table. device_id is resolved at
        -- query time against device_aliases, never stamped at insert — a
        -- device added or re-addressed after a message arrives should still
        -- retroactively correlate.
        CREATE TABLE IF NOT EXISTS syslog (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            source_ip   TEXT NOT NULL,
            host        TEXT,
            facility    INTEGER,
            severity    INTEGER,
            mnemonic    TEXT,
            device_time TEXT,
            message     TEXT,
            raw         TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_syslog_received ON syslog(received_at);
        CREATE INDEX IF NOT EXISTS idx_syslog_host ON syslog(host);
        CREATE INDEX IF NOT EXISTS idx_syslog_source_ip ON syslog(source_ip);
    """)

    # Migration: template_name landed after the initial schema (phase 6).
    cols = {r[1] for r in db.execute("PRAGMA table_info(devices)").fetchall()}
    if "template_name" not in cols:
        db.execute("ALTER TABLE devices ADD COLUMN template_name TEXT REFERENCES templates(name)")

    # Migration: the confetti-traffic import's source value was 'meshflux'
    # (the sibling's old name). OR IGNORE + DELETE handles a device that
    # already has a 'confetti' row for the same ref (primary key clash).
    db.execute("UPDATE OR IGNORE device_sources SET source = 'confetti' WHERE source = 'meshflux'")
    db.execute("DELETE FROM device_sources WHERE source = 'meshflux'")
    db.execute("UPDATE merge_conflicts SET source = 'confetti' WHERE source = 'meshflux'")

    db.commit()
    db.close()


def sqlite_now():
    """UTC timestamp in SQLite's own comparable format: 'YYYY-MM-DD HH:MM:SS'.

    Everything that leaves a device or another system arrives in ISO-8601 or
    some other shape. Comparing that against datetime('now', ...) is a string
    comparison in which 'T' (0x54) sorts above ' ' (0x20), so any same-day
    row would pass any window. Every table here stamps its own timestamps in
    this format instead, and filters on that. See confetti-traffic CLAUDE.md
    constraints 2 and 18 — this bug bit them twice.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def iso(sqlite_ts):
    """Render a stored 'YYYY-MM-DD HH:MM:SS' UTC value as ISO-8601 with Z.

    Browsers parse a space-separated timestamp as *local* time, which would
    skew every "how long ago" display by the viewer's UTC offset.
    """
    return sqlite_ts.replace(" ", "T") + "Z" if sqlite_ts else sqlite_ts


def sqlite_ts_arg(value):
    """Normalise an ISO-8601 or SQLite timestamp argument for comparison.

    Accepts either shape a caller might hand in (including our own iso()
    output coming back from a drill-down link) and normalises inward, rather
    than string-comparing mixed formats — the same trap sqlite_now() exists
    to avoid. Returns None for anything that isn't a timestamp, so the caller
    drops the bound rather than comparing against garbage.
    """
    if not value:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1]
    try:
        parsed = datetime.fromisoformat(v.replace(" ", "T"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def like_arg(value):
    r"""Wrap a substring search for LIKE, escaping its wildcards.

    Without this a search for "100%" or "GigabitEthernet0_1" silently
    matches far more than the caller typed.
    """
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%{}%".format(escaped)
