"""ICMP reachability checker.

The poller only learns a router is gone when its next SSH poll fails, minutes
later, and it never looks at role='node' devices at all. This thread pings
every enabled device's mgmt_ip each BUTLER_PING_INTERVAL_S and keeps
devices.reachable (1 up, 0 down, NULL not checked yet).

Flap guard: a device is only marked down after BUTLER_PING_FAILS_TO_DOWN
failures in a row. The first result for a device is a baseline and emits no
event (CLAUDE.md constraint 15); after that, up->down and down->up each emit
one event, in the same transaction as the column change.
"""

import ipaddress
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import config
from . import db
from . import events

_thread = None
_running = threading.Event()
_start_lock = threading.Lock()


def is_running():
    return _running.is_set()


def _ping(host):
    """True/False for one ICMP echo; None when host is not a plain IP (never
    pass an arbitrary string to a subprocess)."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return None
    if sys.platform == "win32":
        cmd = ["ping", "-n", "1", "-w", "1000", host]
    else:
        cmd = ["ping", "-c", "1", "-W", "1", host]   # BusyBox and iputils both take these
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def run_cycle(ping=_ping):
    """One pass over every enabled device with a mgmt_ip. `ping` is injectable
    so the state machine can be tested without a network."""
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id, hostname, mgmt_ip, reachable, ping_fail FROM devices "
            "WHERE enabled = 1 AND mgmt_ip IS NOT NULL AND mgmt_ip != ''"
        ).fetchall()
    finally:
        conn.close()

    with ThreadPoolExecutor(max_workers=config.PING_WORKERS) as pool:
        outcomes = list(pool.map(lambda r: ping(r["mgmt_ip"]), rows))

    conn = db.connect()
    try:
        now = db.sqlite_now()
        for r, ok in zip(rows, outcomes):
            if ok is None:
                continue
            _record(conn, r, ok, now)
        conn.commit()
    finally:
        conn.close()


def _record(conn, r, ok, now):
    """Apply one ping result to a device row (see the module docstring)."""
    was = r["reachable"]
    fails = 0 if ok else r["ping_fail"] + 1
    if ok:
        reachable = 1
    elif was is None:
        # Never seen up: a first failure is a baseline, mark it down at once.
        reachable = 0 if fails >= config.PING_FAILS_TO_DOWN else None
    else:
        reachable = 0 if fails >= config.PING_FAILS_TO_DOWN else was

    if was is not None and reachable is not None and reachable != was:
        if reachable == 0:
            events.emit(conn, r["id"], "ping_down", "warning",
                        "{} ({}): not answering ping".format(r["hostname"], r["mgmt_ip"]),
                        "{} failed pings in a row".format(fails))
        else:
            events.emit(conn, r["id"], "ping_up", "info",
                        "{} ({}): answering ping again".format(r["hostname"], r["mgmt_ip"]))
    conn.execute("UPDATE devices SET reachable = ?, ping_fail = ?, last_ping_at = ? WHERE id = ?",
                 (reachable, fails, now, r["id"]))


def _loop():
    _running.set()
    try:
        while True:
            try:
                run_cycle()
            except Exception:
                # A bug in one cycle must not end the checker.
                pass
            time.sleep(max(5, config.PING_INTERVAL_S))
    finally:
        _running.clear()


def start():
    """Start the checker thread. Idempotent; a no-op when disabled."""
    global _thread
    if not config.PING_ENABLED:
        return
    with _start_lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_loop, name="reach", daemon=True)
        _thread.start()
