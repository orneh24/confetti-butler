#!/usr/bin/env python3
"""dev/regress.py - the regression suite, as one script.

One check per numbered constraint in CLAUDE.md (R1..R13 are constraints 1..13),
plus checks for what was added later and cross-file consistency (R14 and up).
Prints one line per check; details only on failure.

    python dev/regress.py            # everything
    python dev/regress.py --static   # skip the live tier (no server started)
    python dev/regress.py -v         # details for passes too

Exit 0 when nothing failed (some checks may be NOT RUN), 1 on any failure.
The reasoning behind each check is the matching constraint in CLAUDE.md.

The script itself is stdlib only. The unit tier imports the app, so it needs
the app's own packages (flask, jinja2, netmiko ...); without them those checks
are NOT RUN, never PASS.
"""
import glob
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

VERBOSE = "-v" in sys.argv
STATIC_ONLY = "--static" in sys.argv

results = []   # (status, id, desc, [detail lines])
LIVE = [("R20", "live: real server answers, pages render, /metrics, syslog round trip")]
TMP = tempfile.mkdtemp(prefix="butler-regress-")


class Skip(Exception):
    pass


def check(cid, desc):
    """Register and run a check. It returns a list of problems (empty =
    pass) or raises Skip(reason). A crash is a FAIL, never a pass."""
    def wrap(fn):
        try:
            problems = fn() or []
            results.append(("FAIL" if problems else "PASS", cid, desc, problems))
        except Skip as e:
            results.append(("NOT RUN", cid, desc, [str(e)]))
        except Exception as e:
            results.append(("FAIL", cid, desc, ["check crashed: %r" % e]))
        return fn
    return wrap


# ---------------------------------------------------------------- helpers

_cache = {}


def read(path):
    if path not in _cache:
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            _cache[path] = f.read()
    return _cache[path]


def lines(path):
    return read(path).splitlines()


def is_comment(line):
    s = line.strip()
    return s.startswith(("#", "//", "/*", "*", "<!--"))


def grep(pattern, path, flags=0, comments=False):
    """[(lineno, line)] matching pattern, skipping comment lines by default."""
    rx = re.compile(pattern, flags)
    return [(n, l) for n, l in enumerate(lines(path), 1)
            if rx.search(l) and (comments or not is_comment(l))]


def at(path, hits):
    return ["%s:%d %s" % (path, n, l.strip()) for n, l in hits]


def need(cond, msg, problems):
    if not cond:
        problems.append(msg)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def py_files(sub="butler/app"):
    return sorted(glob.glob(sub + "/**/*.py", recursive=True))


# The unit tier imports the real app against scratch databases. The env var
# must be set before app.config is first imported.
_mods = {}


def app_modules():
    if _mods:
        return _mods
    os.environ["BUTLER_DB_PATH"] = os.path.join(TMP, "import.db")
    os.environ["BUTLER_SYSLOG_ENABLED"] = "false"
    sys.path.insert(0, os.path.join(ROOT, "butler"))
    try:
        from app import config, db, identity, rendering, poller, events, syslog_server
        from app import app as app_pkg_app            # the Flask module
        from app.collectors import ssh, sweep, confetti
        from app.parsers import ios
    except ImportError as e:
        raise Skip("app not importable here (%s)" % e)
    _mods.update(config=config, db=db, identity=identity, rendering=rendering,
                 poller=poller, events=events, syslog_server=syslog_server,
                 appmod=app_pkg_app, ssh=ssh, sweep=sweep, confetti=confetti, ios=ios)
    return _mods


_n = [0]


def fresh_db():
    """A new empty scratch database; returns (modules, open connection)."""
    m = app_modules()
    _n[0] += 1
    m["config"].DB_PATH = os.path.join(TMP, "t%d.db" % _n[0])
    m["db"].init_db()
    return m, m["db"].connect()


def add_device(conn, ip, hostname="rtr", role="router"):
    m = app_modules()
    did = m["identity"].ingest(conn, [("mgmt_ip", ip)], {"hostname": hostname, "role": role},
                               source="manual")
    conn.commit()
    return did


# ============================================================ static tier

@check("R1", "aliases are never deleted (static + a re-IP keeps the old IP)")
def _():
    p = []
    for path in py_files():
        p += at(path, grep(r"DELETE\s+FROM\s+device_aliases", path, re.I))
    m, conn = fresh_db()
    ident = m["identity"]
    did = ident.ingest(conn, [("serial", "S1"), ("mgmt_ip", "192.0.2.1")], {}, source="manual")
    ident.ingest(conn, [("serial", "S1"), ("mgmt_ip", "192.0.2.2")], {}, source="manual")
    conn.commit()
    row = conn.execute("SELECT device_id FROM device_aliases WHERE kind='mgmt_ip' AND value='192.0.2.1'").fetchone()
    need(row and row["device_id"] == did, "old mgmt_ip alias 192.0.2.1 is gone after a re-IP", p)
    conn.close()
    return p


@check("R2", "stronger identifier disagreeing = conflict; weaker drifting = update")
def _():
    p = []
    m, conn = fresh_db()
    ident = m["identity"]
    ident.ingest(conn, [("serial", "S1"), ("mgmt_ip", "192.0.2.1")], {}, source="manual")
    conn.commit()
    try:   # match via the weak IP while a DIFFERENT serial arrives
        ident.ingest(conn, [("mgmt_ip", "192.0.2.1"), ("serial", "S2")], {}, source="manual")
        p.append("different serial behind a known IP was merged silently (no Conflict)")
    except ident.Conflict:
        conn.rollback()
    serial = conn.execute("SELECT serial FROM devices").fetchone()["serial"]
    need(serial in (None, "S1"), "stored serial was overwritten: %r" % serial, p)
    try:   # match via the strong serial while the IP drifted
        ident.ingest(conn, [("serial", "S1"), ("mgmt_ip", "192.0.2.9")], {}, source="manual")
    except ident.Conflict:
        p.append("a legitimate re-IP (same serial) was flagged as a conflict")
        conn.rollback()
    conn.close()
    return p


@check("R3", "IOS-XE commands: no invalid LLDP self-query, no textfsm, brief-table fallback present")
def _():
    p = []
    ssh_src = read("butler/app/collectors/ssh.py")
    cmds = " | ".join(app_modules()["ssh"].COMMANDS.values())
    for bad in ("lldp local-info", "lldp entry local"):
        need(bad not in cmds, "COMMANDS uses '%s' (invalid / always empty on IOS-XE 17.3)" % bad, p)
    import ast
    for path in py_files():
        for node in ast.walk(ast.parse(read(path))):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "use_textfsm" and getattr(kw.value, "value", None) is True:
                        p.append("%s:%d use_textfsm=True (needs ntc-templates on Alpine)" % (path, node.lineno))
    need("fill_lldp_local_if" in ssh_src, "ssh.py no longer fills Local Intf from the brief table", p)
    need("fill_lldp_local_if" in read("butler/app/parsers/ios.py"), "ios.fill_lldp_local_if is missing", p)
    return p


@check("R4", "timestamps: sqlite_now format, iso() conversion, no isoformat() stored")
def _():
    p = []
    m = app_modules()
    db = m["db"]
    now = db.sqlite_now()
    need(re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", now) is not None, "sqlite_now() = %r" % now, p)
    need(db.iso(now) == now.replace(" ", "T") + "Z", "iso() does not produce ...T...Z", p)
    need(db.sqlite_ts_arg(db.iso(now)) == now, "sqlite_ts_arg(iso(x)) != x", p)
    for path in py_files():
        if not path.endswith("db.py"):
            p += at(path, grep(r"\.isoformat\(", path))
    return p


@check("R5", "busy_timeout on every connection, set before journal_mode")
def _():
    p = []
    m = app_modules()
    import inspect
    src = inspect.getsource(m["db"].connect)
    # The PRAGMA statements, not the docstring that also mentions both words.
    need("PRAGMA busy_timeout" in src and "PRAGMA journal_mode" in src
         and src.index("PRAGMA busy_timeout") < src.index("PRAGMA journal_mode"),
         "db.connect(): busy_timeout must be set before journal_mode", p)
    for path in py_files():
        text = read(path)
        if "sqlite3.connect(" in text and "busy_timeout" not in text:
            p.append("%s opens a raw sqlite3 connection without busy_timeout" % path)
    m["config"].DB_PATH = os.path.join(TMP, "r5.db")
    conn = m["db"].connect()
    got = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    need(got == m["config"].BUSY_TIMEOUT_MS, "busy_timeout is %r, expected %r" % (got, m["config"].BUSY_TIMEOUT_MS), p)
    conn.close()
    return p


@check("R6", "syslog listener does not set allow_reuse_address / SO_REUSE*")
def _():
    p = []
    path = "butler/app/syslog_server.py"
    p += at(path, grep(r"allow_reuse_(address|port)\s*=\s*True|SO_REUSE(ADDR|PORT)", path))
    import socketserver
    sl = app_modules()["syslog_server"]
    srv = getattr(sl, "_Server", None)
    need(srv is not None and issubclass(srv, socketserver.UDPServer), "syslog_server._Server is missing", p)
    if srv:
        need(not getattr(srv, "allow_reuse_address", False), "_Server.allow_reuse_address is True", p)
        need(not getattr(srv, "allow_reuse_port", False), "_Server.allow_reuse_port is True", p)
    return p


@check("R7", "every page renders (Jinja2 {% raw %} trap) and the editor keeps its raw block")
def _():
    p = []
    m, conn = fresh_db()
    did = add_device(conn, "192.0.2.1")
    conn.close()
    client = m["appmod"].app.test_client()
    for url in ("/", "/devices", "/devices/%d" % did, "/conflicts", "/ipam", "/templates",
                "/topology", "/syslog"):
        code = client.get(url).status_code
        need(code == 200, "GET %s -> %s" % (url, code), p)
    ed = read("butler/templates/templates_editor.html")
    need("{% raw %}" in ed and "{% endraw %}" in ed, "templates_editor.html lost its {% raw %} block", p)
    return p


@check("R8", "template rendering: StrictUndefined and sandboxed")
def _():
    p = []
    import jinja2
    import jinja2.sandbox
    r = app_modules()["rendering"]
    need(isinstance(r._ENV, jinja2.sandbox.SandboxedEnvironment), "environment is not a SandboxedEnvironment", p)
    need(r._ENV.undefined is jinja2.StrictUndefined, "undefined is not StrictUndefined", p)
    for src, what in (("{{ vars.nope }}", "unset variable"),
                      ("{{ cycler.__init__.__globals__ }}", "sandbox escape")):
        try:
            out = r.render(src, {"vars": {}})
            p.append("%s rendered instead of raising: %r" % (what, out[:60]))
        except r.RenderError:
            pass
    return p


@check("R9", "poller path uses identity.observe, never identity.ingest")
def _():
    p = []
    for path in ("butler/app/collectors/ssh.py", "butler/app/poller.py"):
        p += at(path, grep(r"identity\.ingest\(", path))
    need(grep(r"identity\.observe\(", "butler/app/collectors/ssh.py"), "ssh.py no longer calls identity.observe", p)
    return p


@check("R10", "role='node' devices are never claimed by the poller")
def _():
    p = []
    m, conn = fresh_db()
    router = add_device(conn, "192.0.2.1", "r1", "router")
    add_device(conn, "192.0.2.2", "n1", "node")
    conn.close()

    class Rec:
        def __init__(self):
            self.ids = []

        def submit(self, fn, device_id):
            self.ids.append(device_id)
    rec = Rec()
    m["poller"]._tick(rec)
    need(rec.ids == [router], "poller claimed %r, expected only the router [%d]" % (rec.ids, router), p)
    return p


@check("R11", "a sweep never overwrites a known hostname with an IP")
def _():
    p = []
    m, conn = fresh_db()
    did = add_device(conn, "192.0.2.5", hostname="real-rtr")
    sweep = m["sweep"]
    real_probe = sweep._probe
    sweep._probe = lambda ip: str(ip)
    try:
        sweep.sweep(conn, "192.0.2.5/32")
        sweep.sweep(conn, "192.0.2.6/32")
    finally:
        sweep._probe = real_probe
    conn.commit()
    hn = conn.execute("SELECT hostname FROM devices WHERE id = ?", (did,)).fetchone()["hostname"]
    need(hn == "real-rtr", "re-sweep changed the hostname to %r" % hn, p)
    new = conn.execute("SELECT key, hostname FROM devices WHERE mgmt_ip = '192.0.2.6'").fetchone()
    need(new is not None and new["hostname"] == new["key"], "new swept device should fall back to its key as name", p)
    conn.close()
    return p


@check("R12", "confetti-traffic imports are vendor=alpine, platform=linux, role=node")
def _():
    p = []
    m, conn = fresh_db()
    conf = m["confetti"]

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"hostname": "alp1", "ip": "192.0.2.50", "group_name": "labgroup"}]
    real_get = conf.requests.get
    conf.requests.get = lambda *a, **k: Resp()
    try:
        conf.discover(conn, "http://hub.invalid")
    finally:
        conf.requests.get = real_get
    conn.commit()
    row = conn.execute("SELECT vendor, platform, role, site FROM devices").fetchone()
    need(row is not None and (row["vendor"], row["platform"], row["role"], row["site"])
         == ("alpine", "linux", "node", "labgroup"), "imported node row is %r" % (dict(row) if row else None), p)
    conn.close()
    return p


@check("R13", "GET /configs/<key>.cfg stays unauthenticated")
def _():
    p = []
    path = "butler/app/app.py"
    p += at(path, grep(r"before_request|login_required|WWW-Authenticate|abort\(40[13]", path))
    m, conn = fresh_db()
    did = add_device(conn, "192.0.2.1")
    key = conn.execute("SELECT key FROM devices WHERE id = ?", (did,)).fetchone()["key"]
    conn.execute("INSERT INTO templates (name, body, updated_at) VALUES ('t', 'hostname {{ device.hostname }}', ?)",
                 (m["db"].sqlite_now(),))
    conn.execute("UPDATE devices SET template_name = 't' WHERE id = ?", (did,))
    conn.commit()
    conn.close()
    client = m["appmod"].app.test_client()
    resp = client.get("/configs/%s.cfg" % key)
    need(resp.status_code == 200 and b"hostname rtr" in resp.data,
         "pull returned %s %r" % (resp.status_code, resp.data[:60]), p)
    need(client.get("/configs/nope.cfg").status_code == 404, "unknown key should be 404, not an auth error", p)
    return p


# ---- added after the 13 constraints ------------------------------------

RUNNING = """Building configuration...

Current configuration : 1234 bytes
!
! Last configuration change at 10:00:00 UTC Mon Oct 5 2026
version 17.3
hostname r1
enable secret 9 $9$abc$def
username bob password 0 hunter2
snmp-server community lab123 RO
ntp clock-period 123456
end
"""


@check("R14", "running-config backup: only complete configs, stored on change only, secrets masked")
def _():
    p = []
    m, conn = fresh_db()
    ios, ssh, rendering = m["ios"], m["ssh"], m["rendering"]
    need(ios.parse_running_config("% Invalid input detected at '^' marker.") == "", "error text accepted as a config", p)
    need(ios.parse_running_config(RUNNING.replace("end\n", "")) == "", "truncated config (no 'end') accepted", p)
    a = ios.parse_running_config(RUNNING)
    b = ios.parse_running_config(RUNNING.replace("10:00:00", "11:11:11").replace("123456", "999"))
    need(a and a == b, "volatile lines (last change / clock-period) make identical configs differ", p)
    did = add_device(conn, "192.0.2.1")
    ssh.apply_result(conn, did, "config", a)
    ssh.apply_result(conn, did, "config", b)
    conn.commit()
    n = conn.execute("SELECT count(*) FROM config_versions").fetchone()[0]
    need(n == 1, "same config stored %d times" % n, p)
    masked = rendering.redact_secrets(a)
    for leak in ("$9$abc", "hunter2", "lab123"):
        need(leak not in masked, "secret %r survives redact_secrets" % leak, p)
    conn.close()
    resp = m["appmod"].app.test_client().get("/api/devices/%d/configs/1" % did)
    need(resp.status_code == 200 and b"hunter2" not in resp.data, "API returns an unmasked secret by default", p)
    return p


@check("R15", "events: no events on a task's first poll, none on repeat, emit() never commits")
def _():
    p = []
    m, conn = fresh_db()
    ssh, poller = m["ssh"], m["poller"]
    did = add_device(conn, "192.0.2.1")

    def intf(oper):
        return [dict(name="Gi1", description="", ip=None, prefix_len=None, network=None,
                     admin_status="up", oper_status=oper, speed=None, duplex=None,
                     input_errors=0, crc_errors=0, mtu=1500)]

    def count():
        return conn.execute("SELECT count(*) FROM events").fetchone()[0]
    ssh.apply_result(conn, did, "interfaces", intf("up"))
    ssh.apply_result(conn, did, "interfaces", intf("down"))
    need(count() == 0, "events were emitted during the baseline poll (%d)" % count(), p)
    conn.execute("INSERT INTO poll_history (device_id, transport, task, ok, started_at, received_at) "
                 "VALUES (?, 'ssh', 'interfaces', 1, ?, ?)", (did, m["db"].sqlite_now(), m["db"].sqlite_now()))
    ssh.apply_result(conn, did, "interfaces", intf("down"))
    need(count() == 0, "an unchanged interface emitted an event", p)
    ssh.apply_result(conn, did, "interfaces", intf("up"))
    need(count() == 1, "an interface coming up emitted %d events, expected 1" % count(), p)
    conn.commit()
    poller._finish(conn, did, False)
    poller._finish(conn, did, False)
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM events ORDER BY id")]
    need(kinds.count("device_unreachable") == 1, "unreachable fired %d times, expected once: %r"
         % (kinds.count("device_unreachable"), kinds), p)
    conn.close()
    p += at("butler/app/events.py", grep(r"\.commit\(", "butler/app/events.py"))
    return p


@check("R16", "CLAUDE.md names the right number of poll tasks and lists them all")
def _():
    p = []
    tasks = app_modules()["ssh"].TASKS
    words = {"four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
    doc = read("CLAUDE.md")
    for pat in (r"Per device, (\w+) tasks", r"All (\w+) tasks share"):
        m = re.search(pat, doc)
        need(m is not None, "CLAUDE.md lost the sentence matching %r" % pat, p)
        if m:
            need(words.get(m.group(1)) == len(tasks), "CLAUDE.md says %r tasks, code has %d" % (m.group(1), len(tasks)), p)
    m = re.search(r"Per device, \w+ tasks run independently.*?(?=\n\n)", doc, re.S)
    for t in tasks:
        need(m is not None and "`%s`" % t in m.group(0), "task %r is not listed in CLAUDE.md's per-device sentence" % t, p)
    return p


@check("R17", "shell/Alpine hygiene: LF, shebang, no bashisms, sh -n, exact pins")
def _():
    p = []
    sh = shutil.which("sh")
    files = sorted(glob.glob("butler/**/*.sh", recursive=True) + glob.glob("butler/services/*.initd"))
    need(len(files) >= 5, "expected the shell scripts under butler/, found %d" % len(files), p)
    for f in files:
        text = read(f)
        need("\r" not in text, "%s has CR characters (CRLF breaks the shebang on Alpine)" % f, p)
        # login-setup.sh is sourced from /etc/profile.d, not executed.
        need(text.startswith("#!") or f.endswith("login-setup.sh"), "%s has no shebang" % f, p)
        for n, l in grep(r"\[\[|<\(|\$RANDOM|\$\{[A-Za-z_]+,,|function\s+\w+\s*\{", f):
            p.append("%s:%d bashism: %s" % (f, n, l.strip()))
        if sh:
            r = subprocess.run([sh, "-n", f], capture_output=True, text=True)
            need(r.returncode == 0, "%s fails sh -n: %s" % (f, r.stderr.strip()[:120]), p)
    for n, l in enumerate(lines("butler/requirements.txt"), 1):
        if l.strip() and not l.startswith("#"):
            need(re.fullmatch(r"[A-Za-z0-9_.-]+==\d[\w.]*", l.strip()) is not None,
                 "butler/requirements.txt:%d is not an exact pin: %r" % (n, l), p)
    ga = read(".gitattributes")
    need("*.sh" in ga and "eol=lf" in ga, ".gitattributes no longer pins *.sh to LF", p)
    if not sh and not p:
        raise Skip("no sh for the syntax check (the other parts passed)")
    return p


@check("R18", "poller always releases its per-device lock and resets stuck rows at start")
def _():
    p = []
    import inspect
    poller = app_modules()["poller"]
    src = inspect.getsource(poller._poll_device)
    fin = src.find("finally:")
    need(fin != -1 and "_finish(" in src[fin:], "_poll_device no longer calls _finish() in a finally block", p)
    need("_reset_stuck()" in inspect.getsource(poller._loop), "poller._loop no longer resets stuck 'running' rows", p)
    return p


@check("R19", "every repo file the build/update/migrate scripts copy exists")
def _():
    p = []
    for script in ("butler/build-template.sh", "butler/scripts/butler-update.sh",
                   "butler/scripts/migrate-from-lab-butler.sh"):
        for ref in sorted(set(re.findall(r'\$\{(?:SCRIPT_DIR|SRC_DIR|SRC)\}/([\w./-]+?)["\s]', read(script)))):
            if ref.endswith("/") or "*" in ref:
                continue
            need(os.path.exists(os.path.join("butler", ref)) or os.path.exists(ref),
                 "%s copies '%s' which does not exist" % (script, ref), p)
    bt = read("butler/build-template.sh")
    need("/etc/periodic/daily/butler-backup" in bt, "nightly backup must be installed without a dot in its name", p)
    return p


# ---- features added after R19 -----------------------------------------

SAMPLES = "dev/samples/"


def sample(name):
    return read(SAMPLES + name)


@check("R21", "Arista EOS / Junos parsers handle their (hand-written) samples")
def _():
    p = []
    app_modules()
    from app.parsers import eos, junos
    v = eos.parse_version(sample("eos_show_version.txt"))
    need((v["model"], v["serial"], v["os_version"]) == ("vEOS-lab", "SN-EOS-0001", "4.28.3M"), "eos version: %r" % v, p)
    l = eos.parse_lldp_neighbors(sample("eos_lldp_detail.txt"))
    need(len(l) == 2 and l[0]["local_if"] == "Ethernet1" and l[0]["remote_mgmt_ip"] == "192.0.2.22"
         and l[0]["remote_port"] == "Ethernet2" and l[1]["remote_sysname"] == "core-sw", "eos lldp: %r" % l, p)
    b = eos.parse_bgp_summary(sample("eos_bgp_summary.txt"))
    need([(x["peer_ip"], x["state"]) for x in b] == [("192.0.2.2", "Established"), ("192.0.2.3", "Active")], "eos bgp: %r" % b, p)
    o = eos.parse_ospf_neighbors(sample("eos_ospf_neighbor.txt"))
    need([x["state"] for x in o] == ["FULL", "2-WAY"], "eos ospf: %r" % o, p)
    c = eos.parse_running_config(sample("eos_running_config.txt"))
    need(c and "Command:" not in c and c.rstrip().endswith("end"), "eos config header not stripped / not accepted", p)
    need(eos.parse_running_config("% Invalid input") == "", "eos error text accepted as a config", p)
    jv = junos.parse_version(sample("junos_show_version.txt"))
    need((jv["hostname_hint"], jv["model"], jv["os_version"]) == ("vsrx1", "vsrx", "21.4R3-S2.3"), "junos version: %r" % jv, p)
    ji = {x["name"]: x for x in junos.parse_interfaces(sample("junos_interfaces_terse.txt"))}
    need(ji.get("ge-0/0/0.0", {}).get("ip") == "198.51.100.1" and ji["ge-0/0/0.0"]["prefix_len"] == 24, "junos ip: %r" % ji.get("ge-0/0/0.0"), p)
    need(ji.get("ge-0/0/1", {}).get("oper_status") == "down" and ji.get("lo0.0", {}).get("prefix_len") == 32, "junos status/prefix", p)
    need(len(ji) == 6, "junos interfaces: %d rows, expected 6 (continuation lines must not become rows)" % len(ji), p)
    jl = junos.parse_lldp_neighbors(sample("junos_lldp.txt"))
    need([x["local_if"] for x in jl] == ["ge-0/0/0", "ge-0/0/2"], "junos lldp: %r" % jl, p)
    jb = junos.parse_bgp_summary(sample("junos_bgp_summary.txt"))
    need([(x["peer_ip"], x["state"]) for x in jb] == [("192.0.2.2", "Established"), ("192.0.2.3", "Active")], "junos bgp: %r" % jb, p)
    jo = junos.parse_ospf_neighbors(sample("junos_ospf.txt"))
    need([x["state"] for x in jo] == ["FULL", "2WAY"], "junos ospf: %r" % jo, p)
    need(junos.parse_running_config(sample("junos_config_set.txt")).count("\n") == 3, "junos config set lines", p)
    need(junos.parse_running_config("syntax error, expecting <command>") == "", "junos error text accepted as a config", p)
    return p


@check("R22", "platform registry: same tasks everywhere, config last, unknown platform = IOS")
def _():
    p = []
    m = app_modules()
    from app import platforms
    ios_tasks = tuple(platforms.IOS["commands"])
    for name, plat in platforms.PLATFORMS.items():
        if plat["transport"] == "ssh":
            need(tuple(plat["commands"]) == ios_tasks, "%s tasks are %r, expected %r" % (name, tuple(plat["commands"]), ios_tasks), p)
            need(set(plat["parsers"]) == set(plat["commands"]), "%s: commands and parsers differ" % name, p)
        else:
            need(set(plat["tasks"]) <= set(ios_tasks), "%s offers tasks the apply step does not know" % name, p)
    need(ios_tasks[-1] == "config", "config must stay the last task (a slow read must not delay the others)", p)
    need(platforms.get(None) is platforms.IOS and platforms.get("no-such-os") is platforms.IOS, "unknown platform must fall back to IOS", p)
    need(m["ssh"].TASKS == ios_tasks, "ssh.TASKS drifted from platforms.IOS", p)
    seen = []
    real = m["ssh"].snmp.run_tasks
    def fake_snmp(device, creds):
        seen.append("snmp")
        return iter([])
    m["ssh"].snmp.run_tasks = fake_snmp
    try:
        list(m["ssh"].run_tasks({"platform": "snmp", "mgmt_ip": "192.0.2.1"}, {}))
    finally:
        m["ssh"].snmp.run_tasks = real
    need(seen == ["snmp"], "platform 'snmp' was not routed to the SNMP collector", p)
    return p


@check("R23", "SNMP collector: parses real snmpd output, refuses v3, bad hosts, missing tools")
def _():
    p = []
    from app.collectors import snmp
    walks = {c: {} for c in (snmp.IF_DESCR, snmp.IF_MTU, snmp.IF_SPEED, snmp.IF_ADMIN, snmp.IF_OPER,
                             snmp.IF_IN_ERRORS, snmp.IF_ALIAS, snmp.IP_IFINDEX, snmp.IP_MASK)}
    for col, f in ((snmp.IF_DESCR, "snmp_ifDescr.txt"), (snmp.IF_ADMIN, "snmp_ifAdminStatus.txt"),
                   (snmp.IF_OPER, "snmp_ifOperStatus.txt"), (snmp.IP_IFINDEX, "snmp_ipAdEntIfIndex.txt"),
                   (snmp.IP_MASK, "snmp_ipAdEntNetMask.txt")):
        walks[col] = snmp.parse_pairs(sample(f))
    rows = {r["name"]: r for r in snmp.parse_interfaces(walks)}
    need(set(rows) == {"lo", "eth0"}, "interfaces parsed: %r" % sorted(rows), p)
    if "lo" in rows:
        need((rows["lo"]["ip"], rows["lo"]["prefix_len"], rows["lo"]["oper_status"]) == ("127.0.0.1", 8, "up"), "lo row: %r" % rows["lo"], p)
    v = snmp.parse_version(snmp.parse_pairs(sample("snmp_sys.txt")))
    need((v["hostname_hint"], v["os_version"]) == ("lab-snmp-1", "15.4(1)S2"), "version: %r" % v, p)

    def errors(device, creds):
        return [e for (_t, ok, _o, e, _p) in snmp.run_tasks(device, creds) if not ok]
    need(all("not supported" in e for e in errors({"mgmt_ip": "192.0.2.1"}, {"snmp_version": "3"})), "v3 must fail with a clear message", p)
    need(all("not an IP" in e for e in errors({"mgmt_ip": "192.0.2.1; id"}, {"snmp_version": "2c"})), "a non-IP host reached the subprocess", p)
    real = snmp.subprocess.run

    def missing(*a, **k):
        raise FileNotFoundError()
    snmp.subprocess.run = missing
    try:
        errs = errors({"mgmt_ip": "192.0.2.1"}, {"snmp_version": "2c", "snmp_community": "x"})
    finally:
        snmp.subprocess.run = real
    need(errs and all("net-snmp-tools" in e for e in errs), "missing snmpwalk should name the apk package: %r" % errs, p)
    return p


@check("R24", "ICMP checker: baseline silent, down after N fails, up on recovery, never shells out a non-IP")
def _():
    p = []
    m, conn = fresh_db()
    from app import reach
    a = add_device(conn, "192.0.2.1", "a")
    b = add_device(conn, "192.0.2.2", "b", "node")      # nodes are pinged too
    conn.close()
    answers = {"192.0.2.1": [True, False, False, True], "192.0.2.2": [False, False, False, False]}
    step = [0]

    def fake_ping(host):
        return answers[host][step[0]]

    def kinds():
        c = m["db"].connect()
        try:
            return [(r["device_id"], r["kind"]) for r in c.execute("SELECT device_id, kind FROM events ORDER BY id")]
        finally:
            c.close()
    for i in range(4):
        step[0] = i
        reach.run_cycle(ping=fake_ping)
        if i == 0:
            need(kinds() == [], "first result emitted an event: %r" % kinds(), p)
        if i == 1:
            need(kinds() == [], "one lost ping already raised an event", p)
        if i == 2:
            need(kinds() == [(a, "ping_down")], "after 2 failures expected ping_down, got %r" % kinds(), p)
    need(kinds() == [(a, "ping_down"), (a, "ping_up")], "recovery expected ping_up, got %r" % kinds(), p)
    c = m["db"].connect()
    need(c.execute("SELECT reachable FROM devices WHERE id = ?", (b,)).fetchone()[0] == 0, "node device not marked down", p)
    c.close()
    need(reach._ping("-h") is None and reach._ping("192.0.2.1; id") is None and reach._ping("") is None,
         "_ping must refuse anything that is not an IP", p)
    return p


@check("R25", "/metrics is valid Prometheus text with escaped labels")
def _():
    p = []
    m, conn = fresh_db()
    did = add_device(conn, "192.0.2.1", 'a"b\\c')
    conn.execute("INSERT INTO adjacencies (device_id, proto, peer_ip, state, last_seen) VALUES (?, 'bgp', '192.0.2.9', 'Active', ?)",
                 (did, m["db"].sqlite_now()))
    conn.execute("UPDATE devices SET reachable = 0 WHERE id = ?", (did,))
    conn.commit()
    conn.close()
    resp = m["appmod"].app.test_client().get("/metrics")
    need(resp.status_code == 200 and resp.content_type.startswith("text/plain"), "status/content-type: %s %s" % (resp.status_code, resp.content_type), p)
    text = resp.get_data(as_text=True)
    sample_rx = re.compile(r'^[a-zA-Z_:][a-zA-Z0-9_:]*(\{([a-zA-Z_][a-zA-Z0-9_]*="([^"\\\n]|\\.)*",?)*\})? -?\d+(\.\d+)?$')
    declared = []
    for ln in text.splitlines():
        if ln.startswith("# HELP "):
            declared.append(ln.split()[2])
        elif ln.startswith("# TYPE "):
            continue
        elif not sample_rx.match(ln):
            p.append("not valid exposition format: %r" % ln)
    need(len(declared) == len(set(declared)), "a metric is declared (# HELP) more than once", p)
    for want in ('butler_up 1', 'butler_adjacency_up{device="a\\"b\\\\c",peer="192.0.2.9",proto="bgp"} 0',
                 'butler_device_reachable{device="a\\"b\\\\c",role="router"} 0'):
        need(want in text, "missing line: %s" % want, p)
    return p


@check("R26", "interface error history: sampled on change, resets ignored, 24h growth in the API")
def _():
    p = []
    m, conn = fresh_db()
    ssh = m["ssh"]
    need(ssh.growth([{"x": v} for v in (10, 15, 3, 8)], "x") == 10, "growth() should ignore a counter reset", p)
    did = add_device(conn, "192.0.2.1")

    def intf(crc):
        return [dict(name="Gi1", description="", ip=None, prefix_len=None, network=None, admin_status="up",
                     oper_status="up", speed=None, duplex=None, input_errors=crc, crc_errors=crc, mtu=1500)]

    def samples():
        return conn.execute("SELECT COUNT(*) FROM interface_stats").fetchone()[0]
    ssh.apply_result(conn, did, "interfaces", intf(5))
    ssh.apply_result(conn, did, "interfaces", intf(5))
    need(samples() == 1, "unchanged counters stored %d samples, expected 1" % samples(), p)
    ssh.apply_result(conn, did, "interfaces", intf(9))
    need(samples() == 2, "changed counters not sampled (%d)" % samples(), p)
    conn.commit()
    conn.close()
    c = m["appmod"].app.test_client()
    row = c.get("/api/devices/%d/interfaces" % did).get_json()[0]
    need(row["crc_errors_24h"] == 4, "crc_errors_24h = %r, expected 4" % row["crc_errors_24h"], p)
    hist = c.get("/api/devices/%d/interface-stats?name=Gi1" % did).get_json()
    need([h["crc_errors"] for h in hist] == [5, 9], "history: %r" % hist, p)
    return p


@check("R27", "LLDP crawl: adopted devices are inventory-only; off by default; bad IPs refused")
def _():
    p = []
    cfg_src = read("butler/app/config.py")
    need('"BUTLER_LLDP_AUTO_ADOPT", "false"' in cfg_src, "BUTLER_LLDP_AUTO_ADOPT must default to false", p)
    need('"BUTLER_DISCOVERY_INTERVAL_S", "0"' in cfg_src, "BUTLER_DISCOVERY_INTERVAL_S must default to 0 (off)", p)
    m, conn = fresh_db()
    from app import lldp_crawl as crawl
    ssh, config = m["ssh"], m["config"]
    did = add_device(conn, "192.0.2.1", "r1")

    def nb(ip, name, port):
        return [dict(local_if="Gi1", remote_chassis="aa", remote_sysname=name, remote_port=port, remote_mgmt_ip=ip)]
    ssh.apply_result(conn, did, "lldp", nb("192.0.2.50", "sw9", "Gi0/1"))
    need([u["ip"] for u in crawl.unknown_neighbors(conn)] == ["192.0.2.50"], "unknown neighbor not listed", p)
    need(conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 1, "a neighbor became a device with auto-adopt off", p)
    new = crawl.adopt(conn, "192.0.2.50", "sw9", "r1")
    conn.commit()
    row = conn.execute("SELECT enabled, hostname FROM devices WHERE id = ?", (new,)).fetchone()
    need(row and row["enabled"] == 0 and row["hostname"] == "sw9", "adopted device must start disabled: %r" % (dict(row) if row else None), p)
    need(conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'device_discovered'").fetchone()[0] == 1, "no device_discovered event", p)
    need(conn.execute("SELECT remote_device_id FROM lldp_neighbors").fetchone()[0] == new, "lldp row not linked to the adopted device", p)
    need(crawl.unknown_neighbors(conn) == [], "adopted neighbor still listed as unknown", p)
    for bad in ("127.0.0.1", "0.0.0.0", "224.0.0.1", "999.1.1.1", "not-an-ip", ""):
        need(crawl.adopt(conn, bad, "x", "r1") is None, "adopt accepted %r" % bad, p)
    config.LLDP_AUTO_ADOPT = True
    try:
        ssh.apply_result(conn, did, "lldp", nb("192.0.2.51", "sw10", "Gi0/2"))
    finally:
        config.LLDP_AUTO_ADOPT = False
    auto = conn.execute("SELECT enabled FROM devices WHERE mgmt_ip = '192.0.2.51'").fetchone()
    need(auto is not None and auto["enabled"] == 0, "auto-adopt did not create a disabled device", p)
    conn.close()
    return p


@check("R28", "scheduled discovery: one source failing keeps the others; new devices announced once")
def _():
    p = []
    m, conn = fresh_db()
    conn.close()
    from app import discovery as disc
    config, conf = m["config"], m["confetti"]
    need(disc._thread is None and config.DISCOVERY_INTERVAL_S == 0, "discovery thread should not run by default", p)
    disc.start()
    need(disc._thread is None, "discovery.start() started a thread with the interval at 0", p)

    def fake(c, url):
        return [m["identity"].ingest(c, [("mgmt_ip", "192.0.2.77")], {"hostname": "found1"}, source="confetti")], []
    real, old = conf.discover, (config.DISCOVERY_CONFETTI_URL, config.DISCOVERY_SEEDFILE)
    conf.discover = fake
    config.DISCOVERY_CONFETTI_URL, config.DISCOVERY_SEEDFILE = "http://hub.invalid", os.path.join(TMP, "missing.yaml")
    try:
        s1 = disc.run_once()
        s2 = disc.run_once()
    finally:
        conf.discover = real
        config.DISCOVERY_CONFETTI_URL, config.DISCOVERY_SEEDFILE = old
    need(s1.get("confetti", {}).get("new") == 1 and "error" in s1.get("seedfile", {}), "first run summary: %r" % s1, p)
    need(s2.get("confetti", {}).get("new") == 0, "second run reported the device as new again: %r" % s2, p)
    c = m["db"].connect()
    n = c.execute("SELECT COUNT(*) FROM events WHERE kind = 'device_discovered'").fetchone()[0]
    need(n == 1, "device_discovered events: %d, expected exactly 1" % n, p)
    need(c.execute("SELECT COUNT(*) FROM devices WHERE hostname = 'found1'").fetchone()[0] == 1,
         "the confetti device was lost when the seedfile source failed", p)
    c.close()
    return p


@check("R29", "container and Packer files agree with the repo (COPY sources, contexts, ignored secrets)")
def _():
    p = []
    df = read("container/Dockerfile")
    for src in re.findall(r"^COPY\s+(\S+)\s+\S+", df, re.M):
        need(os.path.exists(os.path.join("butler", src)), "Dockerfile copies '%s', not in butler/" % src, p)
    need(re.search(r"^FROM alpine:\d", df, re.M) is not None, "Dockerfile base image is not a pinned alpine:<version>", p)
    need("BUTLER_HEALTH_SERVICES=" in df, "Dockerfile must empty BUTLER_HEALTH_SERVICES (no OpenRC in a container)", p)
    for pin in ("netmiko", "waitress"):
        need("'^%s=='" % pin in df, "Dockerfile does not install %s from the requirements.txt pin" % pin, p)
    dc = read("container/docker-compose.yml")
    need("context: ../butler" in dc and "dockerfile: ../container/Dockerfile" in dc, "docker-compose.yml build paths are off", p)
    di = read("butler/.dockerignore")
    need("butler.env" in di and "butler.db" in di, "butler/.dockerignore must keep butler.env and butler.db* out of the image", p)
    need(os.path.isfile("packer/http/answers"), "packer/http/answers is missing", p)
    need("http_directory" in read("packer/confetti-butler.pkr.hcl"), "Packer template lost http_directory", p)
    need("packer/vars.pkrvars.hcl" in read(".gitignore"), "packer/vars.pkrvars.hcl (passwords) is not git-ignored", p)
    return p


# ============================================================ live tier

def run_live():
    """Start the real server (serve.py) from a temp copy on free ports and
    drive it over HTTP and UDP."""
    import json
    import urllib.request
    desc = LIVE[0][1]
    work = os.path.join(TMP, "live")
    shutil.copytree("butler", work, ignore=shutil.ignore_patterns(
        "butler.db*", "__pycache__", "*.pyc", "butler.env"))
    port, sysport = free_port(), None
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    sysport = s.getsockname()[1]
    s.close()
    env = dict(os.environ, BUTLER_PORT=str(port), BUTLER_SYSLOG_PORT=str(sysport),
               BUTLER_SYSLOG_BIND="127.0.0.1", BUTLER_SYSLOG_ENABLED="true",
               BUTLER_DB_PATH=os.path.join(work, "live.db"), PYTHONDONTWRITEBYTECODE="1",
               BUTLER_POLL_TICK_S="3600")
    log = open(os.path.join(TMP, "live.log"), "w")
    proc = subprocess.Popen([sys.executable, "serve.py"], cwd=work, env=env, stdout=log, stderr=log)
    problems = []
    base = "http://127.0.0.1:%d" % port

    def get(path):
        with urllib.request.urlopen(base + path, timeout=5) as r:
            return r.status, r.read()
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("server exited early: " + open(os.path.join(TMP, "live.log")).read()[-300:])
            try:
                get("/api/health")
                break
            except Exception:
                time.sleep(0.3)
        else:
            raise RuntimeError("server never answered /api/health")
        health = json.loads(get("/api/health")[1])
        need(health.get("poller_running") is True, "poller is not running", problems)
        need(health.get("syslog_listening") is True, "syslog listener is not up", problems)
        need(health.get("reach_running") is True, "ICMP checker is not running", problems)
        need(b"butler_up 1" in get("/metrics")[1], "/metrics does not answer", problems)
        for url in ("/", "/devices", "/topology", "/ipam", "/templates", "/syslog", "/conflicts"):
            code = get(url)[0]
            need(code == 200, "GET %s -> %s" % (url, code), problems)
        req = urllib.request.Request(base + "/api/devices", method="POST",
                                     data=json.dumps({"hostname": "regress1", "mgmt_ip": "192.0.2.77"}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            need(r.status == 201, "POST /api/devices -> %s" % r.status, problems)
        need(b"regress1" in get("/api/devices")[1], "new device missing from GET /api/devices", problems)
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.sendto(b"<189>Oct  9 10:00:00 regress1 %LINK-3-UPDOWN: Interface Gi1, changed state to down",
                 ("127.0.0.1", sysport))
        u.close()
        seen = False
        for _ in range(20):
            if b"changed state to down" in get("/api/syslog?minutes=5")[1]:
                seen = True
                break
            time.sleep(0.25)
        need(seen, "UDP syslog message never showed up in /api/syslog", problems)
        results.append(("FAIL" if problems else "PASS", "R20", desc, problems))
    except Exception as e:
        results.append(("FAIL", "R20", desc, ["live tier crashed: %r" % e]))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


# ============================================================ run

def tree_state():
    """What git sees changed/untracked, plus any bytecode: a run must not
    add to either."""
    out = sorted(glob.glob("**/__pycache__", recursive=True))
    if shutil.which("git"):
        r = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
        out += r.stdout.splitlines()
    return out


before = tree_state()
if STATIC_ONLY:
    for cid, desc in LIVE:
        results.append(("NOT RUN", cid, desc, ["--static"]))
else:
    try:
        run_live()
    except Exception as e:
        results.append(("FAIL", "R20", "live tier", ["crashed: %r" % e]))
after = tree_state()
if after != before:
    results.append(("FAIL", "clean", "run left files in the tree",
                    ["before %s, after %s" % (before, after)]))
shutil.rmtree(TMP, ignore_errors=True)

results.sort(key=lambda r: (len(r[1]), r[1]))
fails = [r for r in results if r[0] == "FAIL"]
gaps = [r for r in results if r[0] == "NOT RUN"]
for status, cid, desc, detail in results:
    print("%-8s %-6s %s" % (status, cid, desc))
    if status != "PASS" or VERBOSE:
        for d in detail:
            print("           " + d)
verdict = "BLOCKED" if fails else ("CLEAR WITH GAPS" if gaps else "CLEAR")
print("\n%s - %d checks, %d failed, %d not run" % (verdict, len(results), len(fails), len(gaps)))
sys.exit(1 if fails else 0)
