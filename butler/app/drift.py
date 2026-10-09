"""Drift check: does a device's running config contain what its template says?

The template assigned to a device is rendered exactly as the pull endpoint
would render it, then compared with the latest stored running-config backup
(config_versions). A template is usually a PARTIAL config, so this is a
containment check: every line the template produces must be present in the
running config, in the same place. Lines the running config has that the
template never mentions are not drift.

"Same place" means the same parent chain: `ip address 10.0.0.1 255.255.255.0`
must sit under the same `interface GigabitEthernet1` (and nested blocks under
the same nested parents). Indentation width does not matter, only nesting.

Skipped on purpose:
  - blank lines, `!`, `end`, `exit` and `exit-address-family` (printing noise)
  - secret-bearing lines (enable secret, username ... secret, snmp communities,
    keys ...): the device stores them encrypted, so a template can never match them

A template line `no X` counts as present when `no X` is in the running config OR
`X` is not (IOS does not print defaults: `no shutdown` is never shown).
"""

from . import events
from . import rendering

NOISE = {"!", "end", "exit", "exit-address-family"}


def _skippable(line):
    return not line or line.startswith("!") or line in NOISE


def parse(text):
    """[(parent path, line)] in file order. A line with less indentation than
    the line above closes the blocks it is no longer inside."""
    entries = []
    stack = []   # (indent, line)
    for raw in text.replace("\r", "").split("\n"):
        stripped = raw.strip()
        line = " ".join(stripped.split())
        if _skippable(line):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        entries.append((tuple(l for _, l in stack), line))
        stack.append((indent, line))
    return entries


def _is_secret(line):
    return rendering._REDACT_RE.match(line) is not None


def compare(rendered, running):
    """(missing, skipped): entries of the rendered text absent from the running
    config. skipped counts secret lines that were not compared."""
    have = set(parse(running))
    missing = []
    skipped = 0
    for path, line in parse(rendered):
        if _is_secret(line):
            skipped += 1
            continue
        if (path, line) in have:
            continue
        # IOS does not print what is already the default, so a template's `no X` is
        # met by `no X` being there OR by `X` being absent from the same place
        # (`no shutdown` never shows in a running-config). `X` present is drift.
        if line.startswith("no ") and (path, line[3:]) not in have:
            continue
        missing.append({"path": list(path), "line": line})
    return missing, skipped


def evaluate(conn, device_id):
    """Live result for one device. status is one of compliant, drifted,
    no_template, no_backup, render_error."""
    dev = conn.execute("SELECT template_name FROM devices WHERE id = ?", (device_id,)).fetchone()
    if dev is None or not dev["template_name"]:
        return {"status": "no_template", "missing": [], "skipped": 0}
    tpl = conn.execute("SELECT body FROM templates WHERE name = ?", (dev["template_name"],)).fetchone()
    if tpl is None:
        return {"status": "no_template", "missing": [], "skipped": 0,
                "detail": "assigned template '{}' no longer exists".format(dev["template_name"])}
    latest = conn.execute(
        "SELECT body, captured_at FROM config_versions WHERE device_id = ? ORDER BY id DESC LIMIT 1",
        (device_id,)).fetchone()
    if latest is None:
        return {"status": "no_backup", "missing": [], "skipped": 0}
    try:
        rendered = rendering.render_for_device(conn, device_id, tpl["body"])
    except rendering.RenderError as exc:
        return {"status": "render_error", "missing": [], "skipped": 0, "detail": str(exc)}
    missing, skipped = compare(rendered, latest["body"])
    return {"status": "drifted" if missing else "compliant", "missing": missing, "skipped": skipped,
            "config_captured_at": latest["captured_at"]}


def refresh(conn, device_id):
    """Evaluate, store devices.drift_status, and emit an event on a change
    between compliant and drifted. The first evaluation is a baseline and emits
    nothing (CLAUDE.md constraint 15). Caller commits."""
    result = evaluate(conn, device_id)
    new = result["status"]
    row = conn.execute("SELECT hostname, drift_status FROM devices WHERE id = ?", (device_id,)).fetchone()
    if row is None:
        return new
    old = row["drift_status"]
    conn.execute("UPDATE devices SET drift_status = ? WHERE id = ?", (new, device_id))
    if old in ("compliant", "drifted") and new in ("compliant", "drifted") and old != new:
        if new == "drifted":
            first = "; ".join(m["line"] for m in result["missing"][:3])
            events.emit(conn, device_id, "config_drift", "warning",
                        "{}: running-config no longer matches its template".format(row["hostname"]),
                        "{} line(s) missing, e.g. {}".format(len(result["missing"]), first))
        else:
            events.emit(conn, device_id, "config_compliant", "info",
                        "{}: running-config matches its template again".format(row["hostname"]))
    return new


def refresh_template(conn, template_name):
    """After a template is saved: re-evaluate every device that uses it."""
    for r in conn.execute("SELECT id FROM devices WHERE template_name = ?", (template_name,)).fetchall():
        refresh(conn, r["id"])
