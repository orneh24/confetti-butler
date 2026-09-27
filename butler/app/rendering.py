"""Config template rendering and safety checks.

Templates are pull-only: this module never writes to a device — it turns a
Jinja2 template plus a device's polled facts into config text, and
GET /configs/<key>.cfg serves that for a device to fetch on its own
(`copy http://butler/configs/<key>.cfg running-config`).

Reuses the network-config-validation skill's dangerous-command and
security pattern lists, run against the RENDERED output — the text a
device would actually receive — not the template source, since a Jinja2
conditional could make a dangerous line appear only for some devices.
"""

import re

import jinja2
import jinja2.sandbox

# ---------------------------------------------------------------------------
# Dangerous / security checks — ported from the network-config-validation
# skill. These warn, never block: v1 is pull-only, so nothing here can
# auto-apply a bad command — the operator sees the warning at preview time
# and decides.
# ---------------------------------------------------------------------------

DANGEROUS_PATTERNS = [
    (re.compile(r"\breload\b", re.I), "device reload — causes downtime"),
    (re.compile(r"\berase\s+(startup|nvram|flash)", re.I), "erase persistent storage"),
    (re.compile(r"\bformat\b", re.I), "format filesystem"),
    (re.compile(r"crypto\s+key\s+(generate|zeroize)", re.I), "crypto key operation"),
    (re.compile(r"no\s+router\s+(bgp|ospf|eigrp)", re.I), "remove entire routing process"),
    (re.compile(r"no\s+interface\s+\S+", re.I), "remove interface config"),
    (re.compile(r"aaa\s+new-model", re.I), "AAA model change — can lock you out"),
]

SECURITY_PATTERNS = [
    (re.compile(r"snmp-server community public", re.I),
     "SNMP community 'public' — change to something non-default"),
    (re.compile(r"ip ssh version 1", re.I), "SSH version 1 — upgrade to version 2"),
    (re.compile(r"transport input telnet", re.I), "Telnet enabled — use SSH only"),
    (re.compile(r"enable password\b", re.I),
     "'enable password' uses weak reversible encryption — use 'enable secret'"),
]

# Commands that take a secret argument. A literal value here (not a
# {{ vars.* }} reference) means a real credential was typed straight into
# the template source — exactly what the "no secrets in templates" rule
# exists to catch, since GET /configs/<key>.cfg is unauthenticated by
# design (see the plan's config-endpoint decision).
SECRET_BEARING_RE = re.compile(
    r"^\s*(enable secret|enable password|username \S+ (secret|password)|"
    r"snmp-server community|key string|neighbor \S+ password)\s+(\S.*)$",
    re.I | re.M,
)


def check_dangerous_commands(text):
    warnings = []
    for i, line in enumerate(text.splitlines(), start=1):
        for pattern, reason in DANGEROUS_PATTERNS:
            if pattern.search(line):
                warnings.append({"line": i, "text": line.strip(), "reason": reason})
    return warnings


def check_security(text):
    warnings = []
    for i, line in enumerate(text.splitlines(), start=1):
        for pattern, reason in SECURITY_PATTERNS:
            if pattern.search(line):
                warnings.append({"line": i, "text": line.strip(), "reason": reason})
    return warnings


def check_hardcoded_secrets(template_source):
    """Scan TEMPLATE SOURCE (not rendered output) for a literal secret value
    where a {{ vars.* }} reference was expected. Run on save — see app.py's
    api_save_template — not on every render.
    """
    warnings = []
    for i, line in enumerate(template_source.splitlines(), start=1):
        m = SECRET_BEARING_RE.match(line)
        if m and "{{" not in m.group(0):
            warnings.append({
                "line": i, "text": line.strip(),
                "reason": "looks like a literal secret — use a {{ vars.* }} reference "
                          "instead, injected out-of-band, since this endpoint is "
                          "unauthenticated",
            })
    return warnings


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class RenderError(Exception):
    pass


def build_context(conn, device_id):
    """The namespace every template renders against: device.*, interfaces[],
    vars.*. vars are operator-set (template_vars) — things Jinja2 can't
    derive from polled facts alone, e.g. a BGP password.
    """
    device = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    if not device:
        raise RenderError("device not found")
    interfaces = conn.execute(
        "SELECT * FROM interfaces WHERE device_id = ? ORDER BY name", (device_id,)
    ).fetchall()
    var_rows = conn.execute(
        "SELECT key, value FROM template_vars WHERE device_id = ?", (device_id,)
    ).fetchall()
    return {
        "device": dict(device),
        "interfaces": [dict(r) for r in interfaces],
        "vars": {r["key"]: r["value"] for r in var_rows},
    }


# StrictUndefined: an unresolved {{ var }} raises rather than silently
# rendering empty. A silently-blanked line in a config a router is about to
# apply to itself is worse than a loud failure at preview/pull time.
# Sandboxed: template source comes in over an unauthenticated API, and a plain
# Environment lets {{ cycler.__init__.__globals__.os.popen(...) }} run shell
# commands on this server. The sandbox blocks attribute access to internals
# (SecurityError, a TemplateError, surfaces as a normal RenderError).
_ENV = jinja2.sandbox.SandboxedEnvironment(
    undefined=jinja2.StrictUndefined, trim_blocks=True, lstrip_blocks=True
)


def render(template_source, context):
    try:
        template = _ENV.from_string(template_source)
        return template.render(**context)
    except jinja2.exceptions.TemplateError as exc:
        raise RenderError(str(exc))


def render_for_device(conn, device_id, template_source):
    context = build_context(conn, device_id)
    return render(template_source, context)


def preview(conn, device_id, template_source):
    """Render for a device and run the dangerous/security checks against the
    rendered output — a Jinja2 conditional means a dangerous line might only
    appear for some devices, so checking the source alone isn't enough.
    """
    rendered = render_for_device(conn, device_id, template_source)
    return {
        "rendered": rendered,
        "dangerous": check_dangerous_commands(rendered),
        "security": check_security(rendered),
    }
