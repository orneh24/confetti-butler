"""Scheduled discovery.

Runs the existing discovery paths (confetti-traffic import, subnet sweep, YAML
seed file) on a timer, so a new VM or a new node shows up without anyone
pressing a button. Off by default: BUTLER_DISCOVERY_INTERVAL_S=0, and a source
that is not configured is skipped. Everything still goes through
identity.ingest; this module only decides when.

A device that did not exist before a run gets a `device_discovered` event.
"""

import threading
import time

from . import config
from . import db
from . import events
from .collectors import confetti
from .collectors import seedfile
from .collectors import sweep

_thread = None
_start_lock = threading.Lock()
_status = {"last_run": None, "last_error": None, "last_summary": None}


def status():
    out = dict(_status)
    out["enabled"] = config.DISCOVERY_INTERVAL_S > 0
    return out


def _known_ids(conn):
    return {r["id"] for r in conn.execute("SELECT id FROM devices")}


def _announce_new(conn, before, device_ids, source):
    for did in set(device_ids) - before:
        row = conn.execute("SELECT hostname, mgmt_ip FROM devices WHERE id = ?", (did,)).fetchone()
        if row:
            events.emit(conn, did, "device_discovered", "info",
                        "{} ({}): found by {}".format(row["hostname"], row["mgmt_ip"] or "no ip", source))


def run_once():
    """Run every configured source once. Each source gets its own transaction,
    so one failing source cannot discard another's work. Returns a summary."""
    summary = {}
    sources = []
    if config.DISCOVERY_CONFETTI_URL:
        sources.append(("confetti", lambda c: confetti.discover(c, config.DISCOVERY_CONFETTI_URL)))
    for cidr in config.DISCOVERY_SWEEP_CIDRS:
        sources.append(("sweep " + cidr, lambda c, cidr=cidr: sweep.sweep(c, cidr)[1:]))
    if config.DISCOVERY_SEEDFILE:
        sources.append(("seedfile", lambda c: seedfile.load(c, config.DISCOVERY_SEEDFILE)))

    errors = []
    for name, fn in sources:
        conn = db.connect()
        try:
            before = _known_ids(conn)
            device_ids, conflicts = fn(conn)
            _announce_new(conn, before, device_ids, name)
            conn.commit()
            summary[name] = {"devices": len(device_ids), "new": len(set(device_ids) - before),
                             "conflicts": len(conflicts)}
        except Exception as exc:
            conn.rollback()
            errors.append("{}: {}".format(name, exc))
            summary[name] = {"error": str(exc)}
        finally:
            conn.close()
    _status["last_run"] = db.iso(db.sqlite_now())
    _status["last_error"] = "; ".join(errors) or None
    _status["last_summary"] = summary
    return summary


def _loop():
    time.sleep(20)   # let the server finish starting before the first run
    while True:
        try:
            run_once()
        except Exception as exc:
            _status["last_error"] = str(exc)
        time.sleep(max(60, config.DISCOVERY_INTERVAL_S))


def start():
    """Idempotent; does nothing unless an interval is configured."""
    global _thread
    if config.DISCOVERY_INTERVAL_S <= 0:
        return
    with _start_lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_loop, name="discovery", daemon=True)
        _thread.start()
