"""Alerting: send a webhook or an e-mail when an event or a syslog message matches a rule.

Rules live in the alert_rules table (GET/POST /api/alert-rules). Two kinds:
  event   matches rows of the events table (what the poller, the ICMP checker,
          discovery and the drift check record), by kind, minimum severity,
          a regex over subject + detail, and part of the device name
  syslog  matches received syslog messages, by worst severity (0-7), a regex
          over the mnemonic + message, and part of the host

One daemon thread (same start() shape as poller.py) does all the sending, so
a slow webhook can never delay the poller or the UDP syslog receiver. Events are
picked up from the database (events.alerted = 0); syslog messages are handed
over through a small in-memory queue by syslog_server.offer_syslog() after the
row is stored, and that call only appends to a deque.

Flood protection: a rule fires at most once per cooldown per device (per host
for syslog); what was held back is counted and mentioned in the next message.
Events older than BUTLER_ALERT_MAX_AGE_S are marked done without sending, so
turning alerting on, or restarting after downtime, does not replay history.

A failed send is retried once, then recorded on the rule (last_error) and
dropped; it is never retried forever and never raises events of its own.
"""

import calendar
import collections
import re
import smtplib
import threading
import time
from email.message import EmailMessage

import requests

from . import config
from . import db

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}
SYSLOG_NAMES = ["emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"]

_thread = None
_running = threading.Event()
_start_lock = threading.Lock()
_syslog_queue = collections.deque(maxlen=1000)
_last_sent = {}      # (rule_id, key) -> time.time() of the last send
_held_back = {}      # (rule_id, key) -> messages suppressed since then


def is_running():
    return _running.is_set()


def offer_syslog(rec):
    """Called by the syslog receiver for every stored message. Never blocks,
    never raises."""
    try:
        _syslog_queue.append(dict(rec))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _regex_ok(pattern, text):
    if not pattern:
        return True
    try:
        return re.search(pattern, text, re.I) is not None
    except re.error:
        return False


def _kinds(rule):
    return [k.strip() for k in (rule["kinds"] or "").split(",") if k.strip()]


def matches_event(rule, ev):
    if rule["source"] != "event":
        return False
    kinds = _kinds(rule)
    if kinds and ev["kind"] not in kinds:
        return False
    if SEVERITY_ORDER.get(ev["severity"], 0) < SEVERITY_ORDER.get(rule["min_severity"] or "info", 0):
        return False
    if rule["device"] and rule["device"].lower() not in (ev["hostname"] or "").lower():
        return False
    return _regex_ok(rule["pattern"], "{} {}".format(ev["subject"], ev["detail"] or ""))


def matches_syslog(rule, msg):
    if rule["source"] != "syslog":
        return False
    sev = msg.get("severity")
    if rule["syslog_max_severity"] is not None and (sev is None or sev > rule["syslog_max_severity"]):
        return False
    host = "{} {}".format(msg.get("host") or "", msg.get("source_ip") or "")
    if rule["device"] and rule["device"].lower() not in host.lower():
        return False
    return _regex_ok(rule["pattern"], "{} {}".format(msg.get("mnemonic") or "", msg.get("message") or ""))


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def _post_webhook(url, payload):
    resp = requests.post(url, json=payload, timeout=10)
    if resp.status_code >= 300:
        raise RuntimeError("webhook answered HTTP {}".format(resp.status_code))


def _send_mail(address, title, body):
    if not config.SMTP_HOST:
        raise RuntimeError("SMTP is not configured (BUTLER_SMTP_HOST)")
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = config.SMTP_FROM
    msg["To"] = address
    msg.set_content(body)
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15) as smtp:
        if config.SMTP_TLS:
            smtp.starttls()
        if config.SMTP_USER:
            smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
        smtp.send_message(msg)


def deliver(target, payload):
    """Send one alert. target is 'webhook:<url>' or 'mail:<address>'. Raises on failure."""
    kind, _, dest = target.partition(":")
    if kind == "webhook":
        _post_webhook(dest, payload)
    elif kind == "mail":
        _send_mail(dest, payload["title"], payload["text"])
    else:
        raise RuntimeError("unknown target type {!r}".format(kind))


def deliver_with_retry(target, payload):
    try:
        deliver(target, payload)
    except Exception:
        time.sleep(1)
        deliver(target, payload)      # one retry, then the caller records the failure


def make_payload(severity, kind, device, subject, detail, held=0):
    text = "[confetti-butler] {}{}".format(subject, " ({})".format(detail) if detail else "")
    if held:
        text += " (+{} similar alert(s) held back)".format(held)
    title = "confetti-butler {}: {}".format(severity, kind)
    return {"title": title, "text": text, "content": text, "message": text,
            "severity": severity, "kind": kind, "device": device, "detail": detail or "",
            "at": db.iso(db.sqlite_now())}


def validate_target(target):
    """Return an error string, or None when target is usable."""
    kind, _, dest = (target or "").partition(":")
    if kind == "webhook":
        if not re.match(r"^https?://[^\s/]+", dest):
            return "webhook target must look like webhook:http(s)://host/path"
    elif kind == "mail":
        if not re.match(r"^[^@\s]+@[^@\s]+$", dest):
            return "mail target must look like mail:name@example.com"
    else:
        return "target must start with webhook: or mail:"
    return None


def display_target(target):
    """The target with any path or query removed: webhook URLs often carry a token."""
    kind, _, dest = (target or "").partition(":")
    if kind == "webhook":
        m = re.match(r"^(https?://[^/?#]+)", dest)
        return "webhook:{}/...".format(m.group(1)) if m else "webhook:..."
    return target


# ---------------------------------------------------------------------------
# One pass
# ---------------------------------------------------------------------------

def _fire(conn, rule, key, severity, kind, device, subject, detail, now, deliver_fn):
    ck = (rule["id"], key)
    if now - _last_sent.get(ck, 0) < rule["cooldown_s"]:
        _held_back[ck] = _held_back.get(ck, 0) + 1
        return
    payload = make_payload(severity, kind, device, subject, detail, _held_back.get(ck, 0))
    try:
        deliver_fn(rule["target"], payload)
        error = None
    except Exception as exc:
        error = "{}: {}".format(type(exc).__name__, exc)[:200]
    _last_sent[ck] = now
    _held_back[ck] = 0
    if error:
        conn.execute("UPDATE alert_rules SET last_error = ? WHERE id = ?", (error, rule["id"]))
    else:
        conn.execute("UPDATE alert_rules SET last_error = NULL, last_fired_at = ?, "
                     "fired_count = fired_count + 1 WHERE id = ?", (db.sqlite_now(), rule["id"]))
    conn.commit()


def process_once(deliver_fn=None, now=None):
    """Handle every pending event and queued syslog message. Returns how many
    alerts were attempted. deliver_fn is injectable for tests."""
    deliver_fn = deliver_fn or deliver_with_retry
    now = now if now is not None else time.time()
    sent = 0
    conn = db.connect()
    try:
        rules = [dict(r) for r in conn.execute("SELECT * FROM alert_rules WHERE enabled = 1")]
        event_rules = [r for r in rules if r["source"] == "event"]
        syslog_rules = [r for r in rules if r["source"] == "syslog"]

        pending = conn.execute(
            """SELECT e.id, e.at, e.kind, e.severity, e.subject, e.detail, e.device_id, d.hostname
               FROM events e LEFT JOIN devices d ON d.id = e.device_id
               WHERE e.alerted = 0 ORDER BY e.id LIMIT 200""").fetchall()
        # Mark them done and commit BEFORE any network call: a slow webhook must
        # never sit inside an open write transaction (the poller and the syslog
        # receiver share this database and would wait on its lock).
        conn.executemany("UPDATE events SET alerted = 1 WHERE id = ?", [(ev["id"],) for ev in pending])
        conn.commit()
        for ev in pending:
            age = now - calendar.timegm(time.strptime(ev["at"], "%Y-%m-%d %H:%M:%S"))
            if age > config.ALERT_MAX_AGE_S:
                continue
            for rule in event_rules:
                if matches_event(rule, ev):
                    _fire(conn, rule, ev["device_id"], ev["severity"], ev["kind"], ev["hostname"] or "",
                          ev["subject"], ev["detail"], now, deliver_fn)
                    sent += 1

        while _syslog_queue:
            msg = _syslog_queue.popleft()
            for rule in syslog_rules:
                if matches_syslog(rule, msg):
                    sev = msg.get("severity")
                    name = SYSLOG_NAMES[sev] if isinstance(sev, int) and 0 <= sev <= 7 else "unknown"
                    host = msg.get("host") or msg.get("source_ip") or ""
                    _fire(conn, rule, host, "critical" if (sev is not None and sev <= 2) else
                          ("warning" if sev is not None and sev <= 4 else "info"), "syslog", host,
                          "{} syslog {}: {}".format(host, name, msg.get("message") or ""),
                          msg.get("mnemonic") or "", now, deliver_fn)
                    sent += 1
    finally:
        conn.close()
    return sent


def _loop():
    _running.set()
    try:
        while True:
            try:
                process_once()
            except Exception:
                pass          # a bug in one pass must not end alerting
            time.sleep(max(2, config.ALERT_TICK_S))
    finally:
        _running.clear()


def start():
    """Idempotent; a no-op when BUTLER_ALERTS_ENABLED is false."""
    global _thread
    if not config.ALERTS_ENABLED:
        return
    with _start_lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_loop, name="alerts", daemon=True)
        _thread.start()
