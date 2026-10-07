# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A simplistic server for deploying, documenting and visualizing network devices — inventory, IPAM,
config templates served for devices to pull, syslog, LLDP neighbor discovery, and routing-adjacency
(BGP/OSPF) monitoring, visualized as a topology graph. Infrastructure is ESXi 7.0 with Cisco
CSR1000v routers initially; other vendors later.

This is a companion project to `../confetti-traffic` (a separate repo, network connectivity test
harness; formerly mesh-flux — the import route, `collectors/confetti.py` and the `confetti` `source`
value were renamed from `meshflux` on 2026-10-07; `db.init_db` migrates old rows).
The split is deliberate: confetti-traffic treats the network between its nodes as *"an opaque path it
tests, not something it configures."* confetti-traffic owns *is the path healthy*; lab-butler owns *what
are the devices, how are they addressed, how are they connected, and how do they get configured*.
lab-butler's six vendor/protocol reference skills under `.claude/skills/` were evicted from
confetti-traffic (then lab-tester) on 2026-09-14, when it was re-scoped to hub + node only — they are
lab-butler's domain knowledge now, not leftovers.

**Config delivery is pull-only.** lab-butler never writes to a device. It renders a Jinja2 template
against a device's own polled facts and serves it as plain text; the operator runs `copy
http://butler/configs/<key>.cfg running-config` from the device's own console. SSH is used only for
read-only `show` commands. This is a v1 scope decision, not a permanent architectural limit — do not
add a config-push path without it being asked for explicitly.

## Architecture

### Server VM (standalone)
- Alpine Linux VM, ESXi 7.0 + vCenter, `open-vm-tools`
- Runs:
  - Flask API served by **waitress**, not the Flask dev server (single-threaded — the poller's own
    requests, a device's config pull, and the dashboard would queue behind each other)
  - SQLite database (WAL mode) at `/var/lib/lab-butler/butler.db`
  - Web UI on port 80
  - A background **poller** thread pool (device SSH polling — see below)
  - UDP syslog receiver on port 514, in a daemon thread — its own receiver, deliberately not shared
    with confetti-traffic's hub syslog listener
- Installed to `/opt/lab-butler/`, started by OpenRC service `lab-butler`
- Entrypoint is `serve.py` — reads `BUTLER_PORT` at runtime. Do not move the port into the init
  script's `command_args`: OpenRC expands that at parse time, before `start_pre` sources
  `butler.env`, so the setting would be ignored (same constraint as confetti-traffic's hub).

### The poller — server-initiated pull

The inverse of confetti-traffic's node-initiated push: lab-butler dials out to devices over SSH rather than
waiting for them to report in. One daemon thread (`app/poller.py`), started by `serve.py` beside
`syslog_server.start()`, same idempotent shape — `start()` returns quietly if already running.

Each tick (`BUTLER_POLL_TICK_S`, default 15s): claim devices with `poll_state='idle'` and
`next_poll_at <= now`, mark them `'running'` (this is the per-device lock — a device whose SSH hangs
is never queued twice), and hand each to a `ThreadPoolExecutor` (`BUTLER_POLL_WORKERS`, default 8).

Per device, five tasks run independently, **each committing its own transaction**: `version`,
`interfaces`, `lldp`, `bgp`, `ospf`. Partial failure is the normal case — an LLDP timeout must never
discard interface data collected 200ms earlier. `role='node'` devices (confetti-traffic's imported Alpine
fleet) are never polled — `poller.py`'s device-selection query filters `role != 'node'`.

All five tasks share **one SSH session** per poll (`ssh.run_tasks` is a generator, so each task still
commits before the next command runs). Each apply step deletes that device's rows not seen in this
poll — a removed peer or cable must not stay on the topology forever. `interfaces` skips the delete
on an empty result, since empty there means a parse failure, not a router with no interfaces. The
per-device `'running'` lock is always released in a `finally`, and `poller.start()` resets any
`'running'` rows left by a server that stopped mid-poll.

Backoff on failure: `next_poll_at = now + min(interval * 2**fail_count, BUTLER_POLL_MAX_BACKOFF_S)`.
A dead device backs off toward an hour instead of burning a worker every 5 minutes.

Commands and parsers (`app/collectors/ssh.py`, `app/parsers/ios.py`) use **pure regex, not
`use_textfsm=True`** — deliberately, to avoid a hard dependency on the `ntc-templates` package, whose
Alpine availability is unverified (same caution as confetti-traffic CLAUDE.md constraint 14). This was
confirmed against a real IOS-XE 17.3 CSR1000v; see constraint 3 below for command-name gotchas found
that way.

### Config templates — pull delivery

`app/rendering.py` renders a Jinja2 template against `{device, interfaces, vars}` built from a
device's own polled facts (`interfaces` table) plus operator-set `template_vars`. Uses
**`StrictUndefined`** — an unresolved `{{ var }}` raises rather than silently rendering empty, because
a silently-blanked line in a config a router is about to apply to itself is worse than a loud failure
at preview time.

`GET /configs/<device_key>.cfg` is the pull endpoint, unauthenticated by design — a device's console
running `copy http://...` cannot present credentials. The corresponding rule: **templates must not
carry real secrets.** `check_hardcoded_secrets` scans template source on every save and warns if a
secret-bearing command (`enable secret`, `username ... secret`, `snmp-server community`, ...) has a
literal value instead of a `{{ vars.* }}` reference. `check_dangerous_commands` / `check_security`
(ported from the `network-config-validation` skill) run against **rendered output**, not template
source, at preview time — a Jinja2 conditional can make a dangerous line appear for only some devices.

### Device identity and the merge rule

Five independent ingest paths can all observe the same physical device: manual add, a subnet sweep,
vCenter, a YAML seed file, and confetti-traffic's `GET /endpoints`. `app/identity.py` is the **single place**
that decides whether an observation is a new device, an update to a known one, or an ambiguous
collision — nothing else should `INSERT INTO devices` directly.

`devices.key` is derived from a strength ladder, strongest identifier wins: `serial` > `vmuuid` >
`mgmt_ip` > `hostname`. A key is only ever **promoted** to a stronger identifier, never demoted.
Every identifier ever observed is kept permanently in `device_aliases` (not just current state) —
this is what makes cross-source dedup robust, at the cost of a decommissioned device's IP getting
reassigned to a genuinely different device initially surfacing as a conflict rather than silently
merging. That is the intended, safe failure mode — see constraint 1.

Two entry points:
- **`identity.ingest(conn, candidates, fields, source, ref)`** — used by every *discovery* path
  (manual add, sweep, vCenter, seedfile, confetti-traffic import). Looks up the device by alias-matching;
  ambiguity (>1 matched device, or a stronger identifier disagreeing with what's on file for the one
  matched device) writes a `merge_conflicts` row and raises `identity.Conflict` rather than guessing.
- **`identity.observe(conn, device_id, candidates, fields, source, ref)`** — used by the *poller*,
  which already knows the device_id (it dialed that device's own `mgmt_ip`). Only guards against a
  hardware-identity field (`serial`, `vmuuid`) disagreeing with what's on file — a possible device
  swap behind the same management address. `mgmt_ip`/`hostname` drift discovered by polling (a real
  re-IP, a real rename) updates freely.

Ambiguity is **never** auto-resolved. The operator merges via `POST /api/devices/<id>/merge` (see
`/conflicts` page), which re-points every child table at the survivor and deletes the loser.

### IPAM — discovery-first

`app/ipam.py` treats the `interfaces` table (populated by the poller) as the source of truth; there
is no separate address-allocation table in v1. `POST /api/ipam/analyze` rebuilds `ipam_findings`
wholesale from current `interfaces` — cheap at lab scale, simpler than retracting stale findings
incrementally. Ported from the `network-config-validation` skill's `find_subnet_overlaps` /
`find_duplicate_ips`, with one adaptation: two different devices sharing an **identical** network
(a point-to-point link, a shared segment) is the normal case and is excluded; only a genuine partial
overlap (a `/24` that swallows an already-configured `/30`) is flagged.

### Topology

`GET /api/topology` returns a vis.js-shaped `{nodes, edges}` graph over LLDP neighbors and BGP/OSPF
adjacencies. A neighbor/peer that hasn't resolved to a known `device_id` renders as a **ghost node**
(`ghost:lldp:<sysname>`, or `ghost:peer:<ip>` shared by BGP and OSPF) rather than being dropped — an edge to nowhere reads as a bug, a distinctly
styled ghost node reads as "something is out there, unidentified." A link both ends report over LLDP is
drawn once. LLDP resolves via the management IP a
neighbor advertises (`remote_mgmt_ip`) matching a device's `mgmt_ip` alias — see constraint 3 for why
not chassis ID. BGP/OSPF resolve the same way via `peer_ip`, which is why those resolve far
less often — a peer IP is usually a loopback or point-to-point address, not the neighbor's mgmt IP.
NULL there is the expected common case, not a bug.

### Syslog

`app/syslog_server.py` is a near-verbatim port of confetti-traffic's hub syslog receiver — same RFC3164
parsing, same never-discard/fail-soft philosophy, same hazards (constraints 4, 5, 6 below). One
addition: `GET /api/syslog` resolves `device_id` **at query time** by joining `source_ip`/`host`
against `device_aliases`, never stamped at insert — lab-butler has the IP-to-device map confetti-traffic
deliberately lacks, and resolving at query time means a device added or re-addressed after a message
arrives still correlates retroactively.

## Seeding paths (device discovery)

All five are meant to run together and land on the same `devices` row when they observe the same
box — that is what the identity ladder is for.

| Path | Route | Notes |
|---|---|---|
| Manual | `POST /api/devices` | Operator-entered, from the Devices page |
| Subnet sweep | `POST /api/discover/sweep` | TCP connect to port 22 only — coarse on purpose; the poller's own `version` task does real identification next cycle |
| vCenter | `POST /api/discover/vcenter` | vSphere REST API (`requests`, not `pyvmomi`) — **not live-verified**, no vCenter instance was reachable while this was built; confirm endpoint shapes before relying on it |
| YAML seed file | `POST /api/discover/seedfile` | `seed/devices.yaml.sample` shows the shape |
| confetti-traffic import | `POST /api/discover/confetti` | `GET <hub_url>/endpoints`, imported as `role='node'`, `vendor='alpine'`, `platform='linux'` — never SSH-polled |

## Project Structure
```
butler/
  build-template.sh   — builds the golden template
  serve.py            — production entrypoint (reads BUTLER_PORT at runtime)
  run.sh              — foreground launcher for debugging
  app/
    app.py             — Flask routes (grows as a single file; splits into its own
                          module once a section outgrows a screenful — see identity.py,
                          ipam.py, rendering.py, poller.py for what already has)
    config.py          — env-var config
    db.py              — get_db / connect / init_db / sqlite_now / iso / sqlite_ts_arg / like_arg
    identity.py         — device identity ladder, ingest/observe/merge — see above
    poller.py           — background SSH polling scheduler
    syslog_server.py    — UDP/514 receiver (ported from confetti-traffic)
    ipam.py              — overlap/duplicate-IP analysis
    rendering.py         — Jinja2 template rendering + safety checks
    collectors/          — ssh.py, sweep.py, vcenter.py, confetti.py, seedfile.py
    parsers/ios.py        — Cisco IOS/IOS-XE show-command regex parsers
  templates/            — base.html (head, header/nav, theme + layout pickers, footer, Retro taskbar,
                          written once) + dashboard, devices, device, conflicts, ipam, templates_editor,
                          topology, syslog, each `{% extends "base.html" %}` and holding only its own
                          content/CSS/JS (Jinja2 HTML — see constraint 7 re: {% raw %}). The look is
                          copied from confetti-traffic's hub UI; keep the two in step by hand.
  static/theme.css       — the ONE place colours live: dark :root + one :root[data-theme=NAME] block
                          per extra theme (Dark, Light, Dracula, Monokai, High Contrast, Terminal green,
                          Confetti Night, Neon Streamers); also the header confetti strip and Neon's
                          per-card colours. Pages must not define their own :root colours
  static/layout.css      — Classic structure (.header, .section, tables, buttons) + the four layouts:
                          Classic, Modern (side menu, KPI tiles on the dashboard), Retro 95, Amber CRT.
                          Loaded AFTER theme.css on purpose: Retro 95 and Amber bring their own colours
                          and beat Neon's per-card rules only by coming later. Pages use
                          .section > .section-header + .section-body, not their own card CSS
  static/theme.js        — THEMES + LAYOUTS lists and the two header dropdowns (lab-butler-theme,
                          lab-butler-layout in localStorage), themeColor() for JS colours (a layout
                          change fires 'themechange' too), confettiBlast(), the header health dot
                          (pollHealth; a page may define window.onHealth). Add a theme = one block in
                          theme.css + one THEMES entry. "Shuffle" (a mode, not a palette) rotates them
                          every 5-10 min; its pick and next-change time live in lab-butler-theme-shuffle
                          so every page stays in step. Retro 95 / Amber disable the theme picker
  static/vendor/         — codemirror/, vis-network/ (vendored pinned versions, no CDN)
  seed/devices.yaml.sample
  services/ — firstboot.initd, login-setup.sh
  scripts/ — butler-setup.sh
.claude/skills/  — 6 vendor/protocol reference skills (this app's domain knowledge) +
           lab-butler-hub-api (this app's own HTTP contract, mirroring confetti-hub-api) +
           lab-butler-dev-run (running it locally on Windows)
```

## Design Decisions
- **Mirror confetti-traffic's stack exactly**: Flask + SQLite (WAL) + waitress, vanilla JS with no build
  step, Alpine golden image, OpenRC.
- **Pull-only config delivery.** No Netmiko config-push path in v1 — see Architecture above.
- **SNMP is not polled in v1.** If added, it shells out to `net-snmp-tools` (`snmpwalk`/`snmpget`),
  not a Python SNMP library — `pysnmp` is heavy with a history of packaging breakage; `puresnmp` was
  considered and rejected for the same "one more Alpine dependency to verify" reason that keeps
  `use_textfsm` out of the collector. Today only the `credentials` columns, the `BUTLER_SNMP_*`
  defaults and the `net-snmp-tools` install in `build-template.sh` exist.
- **Credentials**: env defaults (`BUTLER_SSH_*`, `BUTLER_SNMP_*`) + a per-device `credentials` table
  override. Plaintext in v1 — `butler.db` must be `0600` (`checkpath --directory --mode 0700` on
  `/var/lib/lab-butler` in the OpenRC service is the current mitigation; the file itself should be
  tightened too if this ever holds a real credential).
- **Keep code simplistic to avoid over-engineering.** Same principle as confetti-traffic, same reasoning:
  a network engineer reading this at 2am should be able to follow it without decoding an abstraction.

## Working style
- Keep output minimalistic and use simple English.
- Do not output large code snippets. Point at `file:line` and say what changed; the file itself is
  the record.
- Do not display code changes in output unless asked to.

## Non-obvious constraints — do not regress these

1. **Aliases are never deleted, and that's deliberate.** `device_aliases` accumulates every
   identifier ever observed for a device, not current state. The cost: a decommissioned device's IP
   reassigned to a genuinely different device surfaces as a `merge_conflicts` row (a stale `mgmt_ip`
   alias disagreeing with a new `serial`) rather than silently merging two different physical boxes.
   That is the safe failure mode the whole identity system is built around — do not "fix" it by
   pruning old aliases.
2. **A stronger identifier disagreeing with what's on file is a conflict; a weaker one drifting is
   not.** `identity.ingest`'s `_detect_kind_clash` only flags kinds ranked >= the strongest kind that
   actually produced the match. This was a real bug caught mid-build: matching a device via `mgmt_ip`
   while a *different* `serial` came in used to silently overwrite the stored serial. Get this
   backwards and either legitimate re-IPs start false-conflicting, or real device swaps go unnoticed.
3. **IOS-XE command names are not assumed, they're checked.** `show lldp local-info` is invalid
   syntax on a real IOS-XE 17.3 CSR1000v. `show lldp entry local` is *not* an equivalent: it looks up
   a neighbor named "local" and always returns 0 entries (confirmed live, LLDP enabled, 2026-09-27).
   No IOS-XE 17.3 command shows a device its own LLDP chassis ID (it is the chassis base MAC, e.g.
   `001e.e64c.3d00`, absent from `show version`/`inventory`/`diag` output), which is why LLDP
   neighbors resolve by advertised management IP, not chassis ID. Older IOS (XE 3.11 / 15.4) omits
   `Local Intf:` from `show lldp neighbors detail`; the collector then fetches the brief
   `show lldp neighbors` table for it (`ios.fill_lldp_local_if`). `show interfaces` is parsed with pure regex rather than
   `use_textfsm=True` for the same "don't assume an Alpine package is there" reason as constraint 14
   in confetti-traffic's CLAUDE.md — verified working against real hardware, not assumed.
4. **Timestamps: everything stores `db.sqlite_now()`'s `'YYYY-MM-DD HH:MM:SS'` form and converts with
   `db.iso()` on the way out.** Identical trap to confetti-traffic CLAUDE.md constraints 2/18 — string-
   comparing ISO-8601 against `datetime('now', ...)` lets `'T'` (0x54) sort above `' '` (0x20) and any
   same-day row passes any window. Applies to `devices.last_seen`/`next_poll_at`, `poll_history`,
   `syslog`, everything.
5. **`busy_timeout` on every connection, no exceptions.** This database has **three** writers (Flask
   request handlers, the syslog listener thread, the poller's thread pool) — one more than
   confetti-traffic's hub ever had. `db.connect()` sets it before `journal_mode=WAL`, since `journal_mode`
   itself can contend and setting the timeout after it would leave that one statement unprotected.
6. **The syslog listener must NOT set `allow_reuse_address`** — identical reasoning to confetti-traffic
   CLAUDE.md constraint 20. Two processes silently splitting incoming datagrams between two databases
   is worse than a clean `EADDRINUSE` failure to start a second one.
7. **Flask templates that embed literal JS containing `{{`/`}}`/`{%`/`%}` must be wrapped in
   `{% raw %}...{% endraw %}`.** `render_template()` runs the *whole* HTML file through Jinja2
   server-side; `templates_editor.html`'s CodeMirror mode definitions and comments contain those
   sequences as literal JS/regex, and without `{% raw %}` Flask 500s trying to parse them as its own
   template syntax. This was a real bug caught while verifying phase 6 — any future inline `<script>`
   with example Jinja2 syntax in it needs the same treatment. `device.html`'s
   `var deviceId = {{ device_id }};` is the one deliberate exception — a real server-side substitution,
   left unwrapped.
8. **Jinja2 rendering uses `StrictUndefined`.** A template referencing an unset `{{ vars.* }}` must
   fail loudly at preview/pull time, not render an empty line into a config a device is about to
   apply to itself. Do not switch to a permissive `Undefined` class. It is also a
   **`SandboxedEnvironment`**: template source arrives over an unauthenticated API, and a plain
   `Environment` lets `{{ cycler.__init__.__globals__.os.popen(...) }}` run shell commands on the
   server (confirmed, then fixed, 2026-09-27). Do not switch back to `jinja2.Environment`.
9. **`identity.observe` (poller path) and `identity.ingest` (discovery paths) are not
   interchangeable.** `observe` skips alias-matching entirely because the caller already knows the
   device_id; calling `ingest` from the poller with only a newly-discovered `serial` candidate (no
   `mgmt_ip`) would find zero existing aliases and create a duplicate device instead of updating the
   one just polled.
10. **`role='node'` devices are never SSH-polled.** `poller.py`'s device-selection query filters
    `WHERE ... role != 'node'` — confetti-traffic's imported Alpine test VMs would fail every IOS `show`
    command if polled, and burn a worker doing it every cycle.
11. **A subnet sweep's ingest passes no `hostname` field**, only the `mgmt_ip` candidate. A
    already-known device's real hostname must never be overwritten with a raw IP string on re-sweep —
    a brand-new device falls back to its key (`mgmt:<ip>`) as a display name instead, via
    `identity._create_device`.
12. **confetti-traffic's imported nodes get explicit `vendor='alpine'`, `platform='linux'`** in
    `collectors/confetti.py` — passing empty strings there would fall through to
    `identity._create_device`'s `cisco`/`cisco_ios` defaults for a brand-new device, mislabeling
    every imported Alpine VM as a Cisco router.
13. **`GET /configs/<key>.cfg` is deliberately unauthenticated.** Do not add auth to "fix" this —
    the mitigating control is `check_hardcoded_secrets` at template save time, not access control on
    the endpoint, because the client fetching it (a router's `copy http://` from its own console)
    cannot present credentials anyway.
