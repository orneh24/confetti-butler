"""Pull syslog from the other Confetti servers on a timer, so a device only has to log to one of them.

THE SAME FILE in confetti-butler, confetti-traffic and confetti-topo (copy it by hand and keep the
three byte-identical; a regress check compares them). It imports no app code: each app passes in
connect(), get(conn, key) and put(conn, key, value) for its own settings table.

Settings keys, per other server <name> (butler, traffic or topo):
    url_<name>            its address: the same value the Settings page keeps for the header link
    syslog_pull_<name>    "1" = pull from it. Off unless an operator turns it on
    syslog_cursor_<name>  the last id of its that we stored (so each message is read once)
    syslog_token_<name>   its access token, if it has one (only confetti-topo can); write-only

Switching a server on starts from its newest message: its history is not copied. Each server is
pulled on its own: one that is down or answers badly never stops the others, and nothing is stored
from a failed request. Only that server's own messages (origin=local) are asked
for, so a message never travels from server to server to server.
"""

import sys
import threading
import time
from datetime import datetime, timezone

from . import syslog_core

MAX_BATCHES = 20        # per server per round; a longer backlog is caught up over several rounds

status = {}             # name -> {"last_ok": iso, "last_error": text, "stored": n since start}
_thread = None
_lock = threading.Lock()


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pull_server(conn, name, get, put, dedup_s=10, on_stored=None, timeout=10):
    """One server, up to MAX_BATCHES requests. Returns the number of messages stored, or None if
    this server is not switched on. Commits the rows and the cursor together."""
    url = (get(conn, "url_" + name) or "").strip().rstrip("/")
    if get(conn, "syslog_pull_" + name) != "1" or not url:
        return None
    token = get(conn, "syslog_token_" + name) or None
    saved = get(conn, "syslog_cursor_" + name)
    st = status.setdefault(name, {"last_ok": "", "last_error": "", "stored": 0})
    total = 0
    try:
        if saved is None:
            # First time: start from now. Ask for ids beyond any it has, which returns nothing but
            # its newest id, and begin there. Its history is not copied.
            _, newest = syslog_core.fetch(url, 10 ** 12, token, timeout)
            put(conn, "syslog_cursor_" + name, str(newest))
            conn.commit()
            st["last_ok"], st["last_error"] = _now_iso(), ""
            return 0
        try:
            cursor = int(saved)
        except ValueError:
            cursor = 0
        for _ in range(MAX_BATCHES):
            cursor, stored, more = syslog_core.pull_once(
                conn, name, url, cursor, token, timeout=timeout, dedup_s=dedup_s, on_stored=on_stored)
            put(conn, "syslog_cursor_" + name, str(cursor))
            conn.commit()
            total += stored
            if not more:
                break
        st["last_ok"], st["last_error"] = _now_iso(), ""
    except Exception as exc:
        conn.rollback()
        st["last_error"] = "{}: {}".format(type(exc).__name__, str(exc)[:200])
    st["stored"] += total
    return total


def pull_round(conn, self_name, get, put, dedup_s=10, on_stored=None):
    """Every other server that is switched on, once."""
    for name in syslog_core.PEERS:
        if name != self_name:
            pull_server(conn, name, get, put, dedup_s, on_stored)


def _loop(connect, self_name, get, put, interval_s, dedup_s, on_stored):
    while True:
        try:
            conn = connect()
            try:
                pull_round(conn, self_name, get, put, dedup_s, on_stored)
            finally:
                conn.close()
        except Exception as exc:           # never let one bad round stop the thread
            sys.stderr.write("[syslog-pull] round failed: {}\n".format(exc))
        time.sleep(max(5, interval_s))


def is_running():
    return _thread is not None and _thread.is_alive()


def start(connect, self_name, get, put, interval_s, dedup_s=10, on_stored=None):
    """Start the puller once (idempotent). interval_s <= 0 switches it off. It does nothing until
    an operator turns pulling on for a server on the Settings page."""
    global _thread
    if interval_s <= 0:
        return
    with _lock:
        if is_running():
            return
        _thread = threading.Thread(
            target=_loop, name="syslog-pull", daemon=True,
            args=(connect, self_name, get, put, interval_s, dedup_s, on_stored))
        _thread.start()
