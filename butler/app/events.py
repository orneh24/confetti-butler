"""Change events — "BGP peer 10.0.0.2 went Established -> Active", "config changed".

The poller's apply steps already hold the old state just before they overwrite
it, so they call emit() with the difference. emit() never commits: the event
is written in the caller's transaction, so it exists exactly when the change
it describes does.
"""

from . import db


def emit(conn, device_id, kind, severity, subject, detail=""):
    conn.execute(
        "INSERT INTO events (at, device_id, kind, severity, subject, detail) VALUES (?, ?, ?, ?, ?, ?)",
        (db.sqlite_now(), device_id, kind, severity, subject, detail),
    )


def is_baseline(conn, device_id, task):
    """True until this task has succeeded once for the device.

    The first poll of a new device finds every interface, neighbor and peer
    "new" — that is the starting picture, not a change, and would otherwise
    flood the timeline with one event per row.
    """
    row = conn.execute(
        "SELECT 1 FROM poll_history WHERE device_id = ? AND task = ? AND ok = 1 LIMIT 1",
        (device_id, task),
    ).fetchone()
    return row is None
