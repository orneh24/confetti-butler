"""Syslog parsing and the one insert path, shared by confetti-butler, confetti-traffic and confetti-topo.

THE SAME FILE in all three repos (copy it by hand and keep the three byte-identical; a regress
check compares them). It imports nothing from any app: no config, no db module. The caller
passes in a sqlite3 connection and does its own commit.

Parsing is best-effort and never discards: a line that cannot be parsed is stored with its raw
text, because a message you cannot parse is often the one you most want to see.
"""

import hashlib
import re

SEVERITY_NAMES = {
    0: "emerg", 1: "alert", 2: "crit", 3: "err",
    4: "warning", 5: "notice", 6: "info", 7: "debug",
}

# <190>123: SW1: *Sep  8 12:00:00.123 UTC: %LINK-3-UPDOWN: Interface Gi2, ...
PRI_RE       = re.compile(r"^<(\d{1,3})>")
SEQ_RE       = re.compile(r"^\d+:\s+")
HOSTCOLON_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]{0,62}):\s+")
# The timezone group requires a following colon (Cisco-style devices write
# "12:00:00.123 UTC: %LINK-..."). Without that lookahead it swallows the
# hostname in plain RFC3164 lines like "Sep  8 12:02:00 SW3 %SEC-...".
TIMESTAMP_RE = re.compile(
    r"^\*?[A-Z][a-z]{2}\s+\d{1,2}\s+\d{1,2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?:\s+[A-Z]{2,5}(?=:))?:?\s*"
)
HOSTWORD_RE  = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]{0,62})\s+(?=[%\w])")
MNEMONIC_RE  = re.compile(r"%([A-Z0-9_]+)-(\d)-([A-Z0-9_]+)\s*:\s*(.*)$", re.S)

# RFC 5424: <PRI>VERSION TIMESTAMP HOSTNAME APP-NAME PROCID MSGID STRUCTURED-DATA [MSG]
# The timestamp must be ISO-8601 or "-", which keeps a 3164 line that starts "<189>1: ..." out.
RFC5424_RE = re.compile(
    r"^(\d{1,2}) (\d{4}-\d{2}-\d{2}T\S+|-) (\S+) (\S+) (\S+) (\S+) "
    r"(-|(?:\[(?:[^\]\\]|\\.)*\])+)(?: (.*))?$", re.S)


def _empty(raw, source_ip):
    return {
        "source_ip": source_ip,
        "host": None,
        "facility": None,
        "severity": None,
        "mnemonic": None,
        "device_time": None,
        "message": raw,
        "raw": raw,
    }


def _set_pri(result, rest):
    """Read a leading <PRI>; returns the rest of the line."""
    m = PRI_RE.match(rest)
    if m:
        pri = int(m.group(1))
        if 0 <= pri <= 191:
            result["facility"] = pri // 8
            result["severity"] = pri % 8
        rest = rest[m.end():]
    return rest


def _set_mnemonic(result, text):
    """Cisco-style %FACILITY-severity-MNEMONIC: message. True if found."""
    m = MNEMONIC_RE.search(text)
    if not m:
        return False
    result["mnemonic"] = "%{}-{}-{}".format(m.group(1), m.group(2), m.group(3))
    result["message"] = m.group(4).strip()
    if result["severity"] is None:
        result["severity"] = int(m.group(2))
    return True


def _parse_5424(result, raw, rest):
    m = RFC5424_RE.match(rest)
    if not m:
        return False
    _version, timestamp, host, app, _procid, msgid, _sd, msg = m.groups()
    msg = (msg or "").strip()
    result["device_time"] = None if timestamp == "-" else timestamp
    result["host"] = None if host == "-" else host
    if not _set_mnemonic(result, msg):
        # Not a Cisco line: keep the text, and use the sender's own tag if it gave one.
        result["message"] = msg or raw
        if msgid != "-":
            result["mnemonic"] = msgid
        elif app != "-":
            result["mnemonic"] = app
    return True


def parse(raw, source_ip):
    """Best-effort parse of one syslog line: RFC 5424, or RFC 3164 including the Cisco-style
    origin-id and %FACILITY-severity-MNEMONIC framing many network vendors emit.

    Never raises and never returns None.
    """
    result = _empty(raw, source_ip)
    rest = _set_pri(result, raw.strip())

    if _parse_5424(result, raw, rest):
        return result

    rest = SEQ_RE.sub("", rest, count=1)

    # "SW1: *Sep  8 ..." - Cisco-style origin-id
    m = HOSTCOLON_RE.match(rest)
    host_taken = False
    if m and not TIMESTAMP_RE.match(rest):
        # Only an origin-id if what follows looks like the rest of a Cisco-style line: its own
        # timestamp, or a %FACILITY-severity-MNEMONIC. Without this, "kernel: out of memory" or
        # "ERROR: disk full" files itself under host "kernel"/"ERROR", and those then appear as
        # separate devices in the sources list.
        after = rest[m.end():]
        if TIMESTAMP_RE.match(after) or MNEMONIC_RE.match(after):
            result["host"] = m.group(1)
            rest = after
            host_taken = True

    if not host_taken:
        # "Sep  8 12:00:00 SW1 %LINK-3-..." - plain RFC 3164. A bare word is only treated as a
        # hostname when a timestamp preceded it; otherwise the first word of an unparseable line
        # would become the host.
        m = TIMESTAMP_RE.match(rest)
        if m:
            result["device_time"] = m.group(0).strip().rstrip(":")
            rest = rest[m.end():]
            m = HOSTWORD_RE.match(rest)
            if m and not rest.startswith("%"):
                result["host"] = m.group(1)
                rest = rest[m.end():]

    # A Cisco-style device puts its own timestamp after the origin-id
    if result["device_time"] is None:
        m = TIMESTAMP_RE.match(rest)
        if m:
            result["device_time"] = m.group(0).strip().rstrip(":")
            rest = rest[m.end():]

    if not _set_mnemonic(result, rest):
        result["message"] = rest.strip() or raw
    return result


# ---------------------------------------------------------------- storage

# Columns every server's syslog table has besides the original ten. origin is 'local' for a
# message this server received itself, else the name of the server it was pulled from.
EXTRA_COLUMNS = (
    ("origin",    "TEXT NOT NULL DEFAULT 'local'"),
    ("origin_id", "INTEGER"),
    ("msg_hash",  "TEXT"),
)
EXTRA_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_syslog_hash ON syslog(msg_hash, received_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_syslog_origin ON syslog(origin, origin_id) "
    "WHERE origin_id IS NOT NULL",
)


def migrate(db):
    """Add the extra columns and indexes to an existing syslog table. Idempotent."""
    have = [r[1] for r in db.execute("PRAGMA table_info(syslog)")]
    for name, decl in EXTRA_COLUMNS:
        if name not in have:
            db.execute("ALTER TABLE syslog ADD COLUMN {} {}".format(name, decl))
    for sql in EXTRA_INDEXES:
        db.execute(sql)


def msg_hash(source_ip, raw):
    """Identity of a message across servers: the same device line seen by two of them."""
    return hashlib.sha1("{}\n{}".format(source_ip, raw).encode("utf-8", "replace")).hexdigest()


def sqlite_ts(value):
    """'2026-11-18T10:00:00Z' (what an API sends) -> '2026-11-18 10:00:00' (what is stored)."""
    return str(value)[:19].replace("T", " ")


def is_duplicate(db, rec, origin, received_at, dedup_s):
    """True if this message is already stored.

    Two copies this server received itself are real repeats (a link that flaps twice) and are
    both kept. A copy is a duplicate only when one of the two was pulled from another server,
    which is what happens when a device sends to more than one of them.
    """
    row = db.execute(
        "SELECT 1 FROM syslog WHERE msg_hash = ? "
        "AND received_at BETWEEN datetime(?, ?) AND datetime(?, ?) "
        "AND (? != 'local' OR origin != 'local') LIMIT 1",
        (rec["msg_hash"], received_at, "-%d seconds" % dedup_s,
         received_at, "+%d seconds" % dedup_s, origin)).fetchone()
    return row is not None


def insert(db, rec, now, origin="local", origin_id=None, dedup_s=10):
    """The one way a syslog row gets stored. Never commits.

    rec is parse()'s dict. now is the caller's sqlite_now(); a pulled row passes the other
    server's received_at as rec["received_at"], so the duplicate window compares like with like.
    Returns the new row id, or None when it was a duplicate and nothing was stored.
    """
    rec = dict(rec)
    rec["msg_hash"] = msg_hash(rec["source_ip"], rec["raw"])
    received_at = sqlite_ts(rec.get("received_at") or now)
    if origin_id is not None and db.execute(
            "SELECT 1 FROM syslog WHERE origin = ? AND origin_id = ?", (origin, origin_id)).fetchone():
        return None
    if is_duplicate(db, rec, origin, received_at, dedup_s):
        return None
    cur = db.execute(
        """INSERT INTO syslog
           (received_at, source_ip, host, facility, severity, mnemonic, device_time,
            message, raw, origin, origin_id, msg_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (received_at, rec["source_ip"], rec["host"], rec["facility"], rec["severity"],
         rec["mnemonic"], rec["device_time"], rec["message"], rec["raw"],
         origin, origin_id, rec["msg_hash"]))
    return cur.lastrowid
