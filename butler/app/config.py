"""Configuration for the lab-butler server."""

import os

DB_PATH = os.environ.get("BUTLER_DB_PATH", "butler.db")
PORT = int(os.environ.get("BUTLER_PORT", "80"))

# The syslog listener (own thread) and the poller (its own thread pool) both
# write to this same SQLite file alongside Flask request handlers — three
# writers, one more than mesh-flux's hub ever had. WAL allows one writer at
# a time; without a busy timeout on every connection, a burst from any one of
# them makes another fail outright with "database is locked" instead of
# waiting its turn.
BUSY_TIMEOUT_MS = int(os.environ.get("BUTLER_BUSY_TIMEOUT_MS", "5000"))

# ---------------------------------------------------------------------------
# Syslog receiver
# ---------------------------------------------------------------------------
SYSLOG_ENABLED = os.environ.get("BUTLER_SYSLOG_ENABLED", "true").lower() in ("true", "1", "yes")
SYSLOG_BIND = os.environ.get("BUTLER_SYSLOG_BIND", "0.0.0.0")
# 514 is privileged, so the server must start as root to bind it. Overridable
# mainly so a non-root local run can use something above 1024.
SYSLOG_PORT = int(os.environ.get("BUTLER_SYSLOG_PORT", "514"))
# Row cap, not a time window — a device at debug level can outpace any
# retention period, and the cap is what actually bounds the file.
SYSLOG_MAX_ROWS = int(os.environ.get("BUTLER_SYSLOG_MAX_ROWS", "300000"))

# ---------------------------------------------------------------------------
# Poller
# ---------------------------------------------------------------------------
POLL_TICK_S = int(os.environ.get("BUTLER_POLL_TICK_S", "15"))
# Halved from the netmiko-ssh-automation skill's 10-20 guidance — this runs
# on a small Alpine VM, not a workstation.
POLL_WORKERS = int(os.environ.get("BUTLER_POLL_WORKERS", "8"))
POLL_DEFAULT_INTERVAL_S = int(os.environ.get("BUTLER_POLL_DEFAULT_INTERVAL_S", "300"))
POLL_MAX_BACKOFF_S = int(os.environ.get("BUTLER_POLL_MAX_BACKOFF_S", "3600"))
POLL_HISTORY_RETENTION_HOURS = int(os.environ.get("BUTLER_POLL_HISTORY_RETENTION_HOURS", "168"))

# ---------------------------------------------------------------------------
# Default device credentials — a per-device row in the credentials table
# overrides these. Never hardcode a real lab password here; this is a shared
# default for an isolated lab, same posture as mesh-flux's root/lab123.
# ---------------------------------------------------------------------------
DEFAULT_SSH_USERNAME = os.environ.get("BUTLER_SSH_USERNAME", "admin")
DEFAULT_SSH_PASSWORD = os.environ.get("BUTLER_SSH_PASSWORD", "")
DEFAULT_SSH_SECRET = os.environ.get("BUTLER_SSH_SECRET", "")
DEFAULT_SNMP_COMMUNITY = os.environ.get("BUTLER_SNMP_COMMUNITY", "public")
DEFAULT_SNMP_VERSION = os.environ.get("BUTLER_SNMP_VERSION", "2c")

# ---------------------------------------------------------------------------
# Self-health (/api/health)
# ---------------------------------------------------------------------------
HEALTH_SERVICES = [
    s.strip() for s in os.environ.get(
        "BUTLER_HEALTH_SERVICES", "lab-butler,chronyd,dropbear,open-vm-tools,lldpd"
    ).split(",") if s.strip()
]
HEALTH_SERVICE_TIMEOUT_S = int(os.environ.get("BUTLER_HEALTH_SERVICE_TIMEOUT_S", "3"))
