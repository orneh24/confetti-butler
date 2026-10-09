# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A simplistic server for deploying, documenting and visualizing network devices — inventory, IPAM,
config templates served for devices to pull, syslog, LLDP neighbor discovery, and routing-adjacency
(BGP/OSPF) monitoring, visualized as a topology graph. Infrastructure is ESXi 7.0 with Cisco
CSR1000v routers initially; other vendors later.

This is a companion project to `../confetti-traffic` (a separate repo, network connectivity test
harness; formerly mesh-flux — the import route, `collectors/confetti.py` and the `confetti` `source`
value were renamed from `meshflux` on 2026-10-07; `db.init_db` migrates old rows). This project
was called lab-butler until 2026-10-08.
The split is deliberate: confetti-traffic treats the network between its nodes as *"an opaque path it
tests, not something it configures."* confetti-traffic owns *is the path healthy*; confetti-butler owns *what
are the devices, how are they addressed, how are they connected, and how do they get configured*.
confetti-butler's six vendor/protocol reference skills under `.claude/skills/` were evicted from
confetti-traffic (then lab-tester) on 2026-09-14, when it was re-scoped to hub + node only — they are
confetti-butler's domain knowledge now, not leftovers.

**Config delivery is pull-only.** confetti-butler never writes to a device. It renders a Jinja2 template
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
  - SQLite database (WAL mode) at `/var/lib/confetti-butler/butler.db`
  - Web UI on port 80
  - A background **poller** thread pool (device SSH polling — see below)
  - UDP syslog receiver on port 514, in a daemon thread — its own receiver, deliberately not shared
    with confetti-traffic's hub syslog listener
- Installed to `/opt/confetti-butler/`, started by OpenRC service `confetti-butler`
- Entrypoint is `serve.py` — reads `BUTLER_PORT` at runtime. Do not move the port into the init
  script's `command_args`: OpenRC expands that at parse time, before `start_pre` sources
  `butler.env`, so the setting would be ignored (same constraint as confetti-traffic's hub).

### The poller — server-initiated pull

The inverse of confetti-traffic's node-initiated push: confetti-butler dials out to devices over SSH rather than
waiting for them to report in. One daemon thread (`app/poller.py`), started by `serve.py` beside
`syslog_server.start()`, same idempotent shape — `start()` returns quietly if already running.

Each tick (`BUTLER_POLL_TICK_S`, default 15s): claim devices with `poll_state='idle'` and
`next_poll_at <= now`, mark them `'running'` (this is the per-device lock — a device whose SSH hangs
is never queued twice), and hand each to a `ThreadPoolExecutor` (`BUTLER_POLL_WORKERS`, default 8).

Per device, six tasks run independently, **each committing its own transaction**: `version`,
`interfaces`, `lldp`, `bgp`, `ospf`, `config`. Partial failure is the normal case — an LLDP timeout must never
discard interface data collected 200ms earlier. `role='node'` devices (confetti-traffic's imported Alpine
fleet) are never polled — `poller.py`'s device-selection query filters `role != 'node'`.

All six tasks share **one SSH session** per poll (`ssh.run_tasks` is a generator, so each task still
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

### Platforms and SNMP

`app/platforms.py` maps a device's `platform` to its commands and parsers: `cisco_ios` / `cisco_xe`
(verified on real hardware), `arista_eos` and `juniper_junos` (**not verified** — written from documented
output and tested only against the hand-written files in `dev/samples/`; the module and parser docstrings
say so, and constraint 3 is why), and `snmp`. Every ssh platform has the same six task names, `config`
last; an unknown platform falls back to the IOS entry. `ssh.COMMANDS` / `PARSERS` / `TASKS` still name the
IOS entry because the regression script reads them. `snmp` (`collectors/snmp.py`) shells out to
`snmpget` / `snmpwalk -Oqn` (net-snmp-tools), v1 and v2c only, tasks `version` and `interfaces`; the host
must be an IP address. Its parser was written against output captured from a real `snmpd`
(`dev/samples/snmp_*.txt`). `ifSpeed` saturates at 4294 Mbps on fast links; `ifHighSpeed` is not read.

### Reachability, interface history, metrics

- **ICMP checker** (`app/reach.py`, a daemon thread like the poller, `BUTLER_PING_*`): pings every enabled
  device with a `mgmt_ip`, including `role='node'`, and keeps `devices.reachable` (1 / 0 / NULL = not
  checked). A device is marked down after `BUTLER_PING_FAILS_TO_DOWN` (2) failures in a row; the first
  result is a baseline and emits no event (the same rule as `events.is_baseline`). `_ping` refuses any
  string that is not an IP address before it reaches a subprocess. Shown on the Devices and device pages
  and as a red outline on the topology; BGP/OSPF sessions that are not Established / FULL are drawn red and dashed.
- **Interface error history:** `ssh._record_interface_stats` stores a sample in `interface_stats` when an
  interface's error counters change, plus one an hour; pruned after `BUTLER_STATS_RETENTION_DAYS` (7).
  `ssh.growth()` sums increases and ignores a counter that went down (a reload). The device page shows
  the 24 h growth; `GET /api/devices/<id>/interface-stats` returns the samples.
- **`GET /metrics`** (`app/metrics.py`): Prometheus text built from the database at scrape time, hand-written
  (no client library). Label values are escaped; each metric is declared once.

### Discovery on a timer, and the LLDP crawl

- `app/discovery.py` runs the confetti-traffic import, subnet sweeps and the seed file every
  `BUTLER_DISCOVERY_INTERVAL_S` seconds (default 0 = never; nothing runs unless configured via
  `BUTLER_DISCOVERY_CONFETTI_URL` / `_SWEEP_CIDRS` / `_SEEDFILE`). Each source commits on its own, so one
  failing source never discards another's work. A device that did not exist before gets a
  `device_discovered` event. `POST /api/discover/run` runs it once by hand.
- `app/lldp_crawl.py`: an LLDP neighbor advertising a management IP no device owns is listed by
  `GET /api/lldp/unknown` and added by `POST /api/lldp/adopt` (or automatically with
  `BUTLER_LLDP_AUTO_ADOPT=true`, default false). It goes through `identity.ingest`, and the new device is
  created with `enabled=0`: inventory only, never polled until an operator sets credentials and enables it.

### Running-config backup

The `config` task (`show running-config`, run last, 60s read timeout) stores the config in
`config_versions` **only when its sha256 differs** from the device's latest version
(`ssh._apply_config`). `ios.parse_running_config` strips lines that change on their own (`Current
configuration`, `! Last configuration change`, `ntp clock-period`, ...) so an unchanged config never
makes a new version; it returns `""` for anything without a `version` line and a closing `end`, which
`ssh._run_command` turns into a failed task — an error message or truncated read is never stored as a
backup. `BUTLER_CONFIG_VERSIONS_KEEP` caps versions per device (0 = unlimited). The body is stored
**raw**; `GET /api/devices/<id>/configs[/<vid>|/diff]` masks secrets with `rendering.redact_secrets`
unless `?raw=1`. Because the diff is built from masked text, a changed secret shows no difference.
The device page's Config history section shows the list, a viewer and the diff.

### Change events

`app/events.py` `emit()` writes a row to `events` (`BUTLER_EVENT_RETENTION_DAYS`, default 30, pruned
in the poller tick). Callers are the poller's apply steps, which hold the old state just before they
overwrite it: interface oper up/down and rising CRC errors, BGP/OSPF peer state/added/removed, LLDP
neighbor added/removed, OS version change, `config_changed`, and `poller._finish` for
`device_unreachable` / `device_recovered` (first failure and first success after failures only, not
every poll). `emit()` never commits — the event shares the transaction of the change it describes.
`events.is_baseline()` is true until a task has an `ok=1` row in `poll_history`; while true, nothing is
emitted, so adding a device doesn't produce one event per interface/peer. Read via
`GET /api/events` (`device_id`, `kind`, `severity`, `hours`, `limit`); shown on the dashboard
(last 15) and each device page. There is no alerting yet — events are only recorded.

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
against `device_aliases`, never stamped at insert — confetti-butler has the IP-to-device map confetti-traffic
deliberately lacks, and resolving at query time means a device added or re-addressed after a message
arrives still correlates retroactively.

## Updating a deployed VM

`butler-update.sh <butler-dir|tarball>` (also `--rollback`) updates the code only: it import-checks the
new tree against a throwaway DB *before* stopping anything, backs up the DB with `sqlite3 .backup`
(`/var/lib/confetti-butler/backups`, 0700, newest 5 kept), swaps `/opt/confetti-butler` (old one kept as
`.prev`), restarts, waits for `/api/health` and swaps back by itself if it never comes up. `butler.env`
is carried over; the DB is not restored on rollback (new versions only add tables/columns). It does not
touch apk/pip packages or the OpenRC scripts — it warns when `requirements.txt` or `firstboot.initd`
changed; re-run `build-template.sh` for those. `requirements.txt` holds exact tested versions;
`build-template.sh` pip-installs only netmiko/waitress/flask-if-missing from those pins, the rest come
from apk. Tested in an `alpine:3.20` container with a fake `rc-service` (update from dir and tarball,
rollback, import failure, unhealthy start → auto rollback) and then on a real Alpine 3.24.2 VM under OpenRC
(update, unhealthy start → auto rollback; 2026-10-09). A successful auto-rollback consumes `.prev`, so a
manual `--rollback` right after one has nothing to go back to.

### Backups, first boot, migration

- **Nightly backup:** `services/butler-backup.sh` is installed as `/etc/periodic/daily/butler-backup` (no
  dot in the name — run-parts skips those; `crond` is enabled by the build). `sqlite3 .backup` into
  `/var/lib/confetti-butler/backups/butler-daily-YYYYMMDD.db`, newest `BUTLER_BACKUP_KEEP` (default 7)
  kept, folder 0700, files 0600. Pre-update backups share the folder and rotate separately.
- **Guestinfo first boot:** besides `butler.ip`/`butler.gateway`, `firstboot.initd` applies
  `butler.ssh_username`, `ssh_password`, `ssh_secret` and `poll_interval_s` to `butler.env` once per clone
  (stamp `/etc/confetti-butler/.env-done`, independent of the IP stamp). Guestinfo is visible to anyone who
  can see the VM's advanced settings in vCenter. `butler.env` is 0600. There is no hub-URL key: the
  confetti-traffic import takes its URL in the request, not from config.
- **Pre-rename VMs:** `scripts/migrate-from-lab-butler.sh <butler-dir>` moves `/opt`, `/var/lib` and `/etc`
  `lab-butler` to `confetti-butler`, rewrites `butler.env`, swaps the OpenRC services and login hook, and
  checks health. It refuses if both names exist. Run `butler-update.sh` afterwards for the code. The OpenRC
  main script now lives in `services/confetti-butler.initd` (the build copies it) so the migration can use it.

### Other ways to deploy

- **Container** (`container/Dockerfile`, `docker-compose.yml`; context `butler/`): same Alpine packages and
  pinned pip packages as the VM, `BUTLER_HEALTH_SERVICES` emptied (no OpenRC). Built and run: health,
  `/metrics`, ping, UDP syslog and a restart with its volume were checked. On the default bridge network
  Docker may NAT UDP, hiding the real syslog source address (see the compose file).
- **Packer** (`packer/`): `vsphere-iso` template that installs Alpine from an answer file and runs
  `build-template.sh`. **Never run** — no vCenter was available; the README lists what will likely need adjusting.

## Seeding paths (device discovery)

All five are meant to run together and land on the same `devices` row when they observe the same
box — that is what the identity ladder is for.

| Path | Route | Notes |
|---|---|---|
| Manual | `POST /api/devices` | Operator-entered, from the Devices page |
| Subnet sweep | `POST /api/discover/sweep` | TCP connect to port 22 only — coarse on purpose; the poller's own `version` task does real identification next cycle |
| vCenter | `POST /api/discover/vcenter` | vSphere REST API (`requests`, not `pyvmomi`) — **not live-verified**, no vCenter instance was reachable while this was built; confirm endpoint shapes before relying on it |
| YAML seed file | `POST /api/discover/seedfile` | `seed/devices.yaml.sample` shows the shape |
| confetti-traffic import | `POST /api/discover/confetti` | `GET <hub_url>/endpoints`, imported as `role='node'`, `vendor='alpine'`, `platform='linux'` — never SSH-polled. `site` is the endpoint's `group_name`, which is confetti-traffic's *effective* group: a group set on its dashboard overrides what the node registers, so an override changes `site` on the next import |

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
    events.py           — emit() change events, is_baseline() first-poll guard
    platforms.py        — per-platform commands/parsers (IOS, EOS, Junos, snmp)
    reach.py            — ICMP reachability thread
    discovery.py        — scheduled discovery thread (off by default)
    lldp_crawl.py       — adopt unknown LLDP neighbors as inventory-only devices
    metrics.py          — /metrics Prometheus text
    syslog_server.py    — UDP/514 receiver (ported from confetti-traffic)
    ipam.py              — overlap/duplicate-IP analysis
    rendering.py         — Jinja2 template rendering + safety checks + redact_secrets (stored configs)
    collectors/          — ssh.py, snmp.py, sweep.py, vcenter.py, confetti.py, seedfile.py
    parsers/             — ios.py (verified), eos.py and junos.py (NOT verified on hardware), regex only
  templates/            — base.html (head, header/nav, theme + layout pickers, footer, Retro taskbar,
                          written once) + dashboard, devices, device, conflicts, ipam, templates_editor,
                          topology, syslog, each `{% extends "base.html" %}` and holding only its own
                          content/CSS/JS (Jinja2 HTML — see constraint 7 re: {% raw %}). The look is
                          copied from confetti-traffic's hub UI; keep the two in step by hand.
  static/theme.css       — the ONE place colours live: dark :root + one :root[data-theme=NAME] block
                          per extra theme (Dark, Light, Monokai, High Contrast, Terminal green,
                          Confetti Night, Neon Streamers); also the header confetti strip and Neon's
                          per-card colours. Pages must not define their own :root colours
  static/layout.css      — Classic structure (.header, .section, tables, buttons) + the five layouts:
                          Classic, Modern (side menu, KPI tiles on the dashboard), Retro 95, Amber CRT, Neon Green CRT (the last two share one block driven by --crt-* palettes, as in confetti-traffic).
                          Loaded AFTER theme.css on purpose: Retro 95 and Amber bring their own colours
                          and beat Neon's per-card rules only by coming later. Pages use
                          .section > .section-header + .section-body, not their own card CSS
  static/theme.js        — THEMES + LAYOUTS lists and the two header dropdowns (confetti-butler-theme,
                          confetti-butler-layout in localStorage), themeColor() for JS colours (a layout
                          change fires 'themechange' too), confettiBlast(), the header health dot
                          (pollHealth; a page may define window.onHealth). Add a theme = one block in
                          theme.css + one THEMES entry. "Shuffle" (a mode, not a palette) rotates them
                          every 5-10 min (each timer-driven change also fires the confettiBlast() rain, as in
                          confetti-traffic's hub; not on page load or a selector pick); its pick and
                          next-change time live in confetti-butler-theme-shuffle so every page stays in step (the same state carries a `layout` pick, as in confetti-traffic: not saved as the viewer's layout, a hand pick holds until the next change). Retro 95 / the CRT layouts disable the theme picker
  static/vendor/         — codemirror/, vis-network/ (vendored pinned versions, no CDN)
  seed/devices.yaml.sample
  services/ — confetti-butler.initd, firstboot.initd, login-setup.sh, butler-backup.sh
  dev/regress.py — the regression gate (see "Regression gate" below); dev/samples/ = parser fixtures
  container/ — Dockerfile + compose; packer/ — vSphere template (untested)
  scripts/ — butler-setup.sh, butler-update.sh (in-place update with backup + rollback), migrate-from-lab-butler.sh, seed_mock_lab.py (fake lab in a new scratch DB),
           capture_readme.py (re-records docs/img/ from that mock lab; run after any UI change)
.claude/skills/  — 6 vendor/protocol reference skills (this app's domain knowledge) +
           confetti-butler-hub-api (this app's own HTTP contract, mirroring confetti-hub-api) +
           confetti-butler-dev-run (running it locally on Windows)
```

## Design Decisions
- **Mirror confetti-traffic's stack exactly**: Flask + SQLite (WAL) + waitress, vanilla JS with no build
  step, Alpine golden image, OpenRC.
- **Pull-only config delivery.** No Netmiko config-push path in v1 — see Architecture above.
- **SNMP shells out to `net-snmp-tools`** (`snmpwalk`/`snmpget`), not a Python SNMP library — `pysnmp` is
  heavy with a history of packaging breakage; `puresnmp` was rejected for the same "one more Alpine
  dependency to verify" reason that keeps `use_textfsm` out of the collector. It is used only for devices
  whose `platform` is `snmp` (see "Platforms and SNMP"); v3 is not supported.
- **Credentials**: env defaults (`BUTLER_SSH_*`, `BUTLER_SNMP_*`) + a per-device `credentials` table
  override. Plaintext in v1 — `butler.db` must be `0600` (`checkpath --directory --mode 0700` on
  `/var/lib/confetti-butler` in the OpenRC service is the current mitigation; the file itself should be
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
14. **A stored running-config must be a complete config, stored only when it changes, and masked on the
    way out.** `ios.parse_running_config` returns `""` for anything without a `version` line and a
    closing `end` (an error message or truncated read must never become the "latest backup"), and
    strips lines that change on their own (`! Last configuration change`, `ntp clock-period`, ...) —
    leave them in and every poll after a `write memory` stores a new version. The body is stored raw;
    `rendering.redact_secrets` masks it in every API response unless `?raw=1`. Extend the masking
    patterns when a new secret-bearing command appears; the check only covers the ones listed there.
    A real router's config leaked `crypto isakmp key` through the first version (found 2026-10-10); NTP,
    HSRP and `authentication-key` lines are masked now too. Other vendors' secret syntax (EOS, Junos) is not covered.
15. **Events are written inside the transaction of the change they describe, and not on a task's first
    poll.** `events.emit()` must never commit (an event for a change that then rolled back is a lie).
    `events.is_baseline()` suppresses events until the task has an `ok=1` row in `poll_history`; without
    it, adding a device emits one event per interface, neighbor and peer. `device_unreachable` /
    `device_recovered` fire only on the 0 → failing and failing → ok transitions of `fail_count`.

## Regression gate

`python dev/regress.py` (stdlib only; `--static` skips the live tier, `-v` shows detail for passes) is
the gate to run before handing back any code change. `R1`..`R15` are constraints 1..15 above; `R16`..`R19`
are cross-file checks (docs vs code task count, shell/Alpine hygiene, files the build scripts copy, poller
lock); `R20` starts the real server from a temp copy and drives it over HTTP and UDP; `R21`..`R28` cover the
features added since (EOS/Junos parsers against `dev/samples/`, platform registry, SNMP, ICMP state machine,
`/metrics` format, interface history, LLDP crawl, scheduled discovery); `R29` checks the container and Packer
files against the repo; `R30` pins the BGP `Idle (Admin)` parse found on a real 15.4 router. R21 passes on hand-written samples, so it proves the parsers do what they were written
to do, not that they match real devices. Output is one line
per check and a verdict: `CLEAR`, `CLEAR WITH GAPS` (something not run) or `BLOCKED`. The unit tier
imports the app, so it needs the app's own Python packages (else those checks are `NOT RUN`). Each check
was proven red by reverting its fix in a scratch copy. Adding a constraint means adding its check with the
same number; the `regression-gate` agent runs this script. Doc-only changes need `docs-drift-checker`
instead.
