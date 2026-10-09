"""Server-initiated device poller.

The inverse of confetti-traffic's node-initiated push: confetti-butler dials out to
devices over SSH rather than waiting for them to report in. One daemon
thread, started by serve.py beside syslog_server.start() with the same
idempotent shape — start() returns quietly if already running, and a bug
inside one tick must never kill the poller permanently, matching
confetti-traffic's rule that a missing background service must never take down
the piece that matters (the dashboard/API staying responsive).
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import config
from . import db
from . import events
from . import identity
from .collectors import ssh as ssh_collector

_thread = None
_running = threading.Event()
_start_lock = threading.Lock()


def is_running():
    return _running.is_set()


def start():
    """Start the poller thread. Idempotent — a second call is a no-op."""
    global _thread
    with _start_lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_loop, name="poller", daemon=True)
        _thread.start()


def _reset_stuck():
    """A device left 'running' by a server that stopped mid-poll would never
    be claimed again — the claim query only takes 'idle'. Nothing can be
    polling at startup, so any 'running' row here is stale."""
    conn = db.connect()
    try:
        conn.execute("UPDATE devices SET poll_state = 'idle' WHERE poll_state = 'running'")
        conn.commit()
    finally:
        conn.close()


def _loop():
    _running.set()
    try:
        _reset_stuck()
    except Exception:
        pass
    executor = ThreadPoolExecutor(max_workers=config.POLL_WORKERS)
    try:
        while True:
            try:
                _tick(executor)
            except Exception:
                # A bug in one tick's scheduling must not end the poller —
                # the next tick gets another chance, same posture as
                # syslog_server's per-datagram exception handling.
                pass
            time.sleep(config.POLL_TICK_S)
    finally:
        _running.clear()


def _tick(executor):
    """Claim due devices (mark them 'running' so a slow poll can't be
    picked up twice — confetti-traffic constraint 6's overlapping-cycle lock,
    moved server-side) and hand each to the thread pool."""
    conn = db.connect()
    try:
        now = db.sqlite_now()
        rows = conn.execute(
            """SELECT id FROM devices
               WHERE enabled = 1 AND poll_state = 'idle' AND role != 'node'
                 AND (next_poll_at = '' OR next_poll_at <= ?)
               LIMIT ?""",
            (now, config.POLL_WORKERS * 2),
        ).fetchall()
        device_ids = [r["id"] for r in rows]
        if device_ids:
            conn.executemany(
                "UPDATE devices SET poll_state = 'running' WHERE id = ?",
                [(d,) for d in device_ids],
            )
        conn.execute(
            "DELETE FROM poll_history WHERE received_at < datetime('now', ? || ' hours')",
            ("-{:d}".format(config.POLL_HISTORY_RETENTION_HOURS),),
        )
        conn.execute(
            "DELETE FROM events WHERE at < datetime('now', ? || ' days')",
            ("-{:d}".format(config.EVENT_RETENTION_DAYS),),
        )
        conn.execute(
            "DELETE FROM interface_stats WHERE at < datetime('now', ? || ' days')",
            ("-{:d}".format(config.STATS_RETENTION_DAYS),),
        )
        conn.commit()
    finally:
        conn.close()

    for device_id in device_ids:
        executor.submit(_poll_device, device_id)


def poll_now(device_id):
    """Synchronous single-device poll for POST /api/devices/<id>/poll.

    Runs inline on the request thread rather than through the pool — an
    operator clicking "poll now" wants to see the result, not a 202. Does
    not touch poll_state/next_poll_at scheduling beyond what _finish always
    does, so it composes safely with the background loop picking the same
    device up later.
    """
    return _poll_device(device_id)


def _poll_device(device_id):
    conn = db.connect()
    any_ok = False
    try:
        device = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        if device is None:
            return None
        creds = ssh_collector.resolve_credentials(conn, device_id)

        results = {}
        started = db.sqlite_now()
        for task, ok, output, error, parsed in ssh_collector.run_tasks(dict(device), creds):
            if ok:
                try:
                    ssh_collector.apply_result(conn, device_id, task, parsed)
                    conn.commit()
                    any_ok = True
                except identity.Conflict as exc:
                    conn.rollback()
                    ok = False
                    error = "identity conflict: {}".format(exc)
                except Exception as exc:
                    conn.rollback()
                    ok = False
                    error = "apply failed: {}: {}".format(type(exc).__name__, exc)

            received = db.sqlite_now()
            conn.execute(
                """INSERT INTO poll_history
                       (device_id, transport, task, ok, error, output, started_at, received_at)
                   VALUES (?, 'ssh', ?, ?, ?, ?, ?, ?)""",
                (device_id, task, 1 if ok else 0, error, (output or "")[:20000], started, received),
            )
            conn.commit()
            results[task] = {"ok": ok, "error": error}
            started = received
        return results
    finally:
        # Always release the per-device lock, even if something above raised
        # — otherwise the device stays 'running' and is never claimed again.
        try:
            conn.rollback()
            _finish(conn, device_id, any_ok)
        finally:
            conn.close()


def _finish(conn, device_id, any_ok):
    now = db.sqlite_now()
    row = conn.execute(
        "SELECT poll_interval_s, fail_count FROM devices WHERE id = ?", (device_id,)
    ).fetchone()
    interval = row["poll_interval_s"] if row else config.POLL_DEFAULT_INTERVAL_S

    # Only the transitions: one event when a device stops answering, one when
    # it comes back — not one per failed poll.
    if row:
        name = conn.execute("SELECT hostname FROM devices WHERE id = ?", (device_id,)).fetchone()["hostname"]
        if not any_ok and row["fail_count"] == 0:
            events.emit(conn, device_id, "device_unreachable", "warning",
                        "{}: polling failed".format(name))
        elif any_ok and row["fail_count"] > 0:
            events.emit(conn, device_id, "device_recovered", "info",
                        "{}: polling recovered".format(name),
                        "after {} failed poll(s)".format(row["fail_count"]))

    if any_ok:
        conn.execute(
            """UPDATE devices SET poll_state = 'idle', fail_count = 0,
                   last_poll_ok = ?, next_poll_at = datetime(?, ? || ' seconds')
               WHERE id = ?""",
            (now, now, interval, device_id),
        )
    else:
        fail_count = (row["fail_count"] if row else 0) + 1
        backoff = min(interval * (2 ** fail_count), config.POLL_MAX_BACKOFF_S)
        conn.execute(
            """UPDATE devices SET poll_state = 'idle', fail_count = ?,
                   next_poll_at = datetime(?, ? || ' seconds')
               WHERE id = ?""",
            (fail_count, now, backoff, device_id),
        )
    conn.commit()
