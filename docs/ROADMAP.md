# confetti-butler — architecture review and feature roadmap

## Context
You asked for an analysis of the current architecture against the project's aim: *inventory, IPAM,
pull-delivered config, syslog, LLDP/BGP/OSPF topology for an ESXi + CSR1000v lab*. It should compare
the project with similar tools and suggest missing features, simpler deployment and better monitoring.
This is a review. Nothing gets built until you choose items from the roadmap below.

Sources read: CLAUDE.md, docs/HANDOFF.md, `app/app.py` routes, `app/db.py` schema, `app/config.py`,
`build-template.sh`.

---

## 1. Where the project stands

**Strong points (keep them):**
- **Identity ladder + permanent aliases + conflicts that are never auto-merged.** Most open-source tools
  don't have this. NetBox has no auto-discovery dedup. LibreNMS dedups by hostname or IP only.
- **Pull-only config, `StrictUndefined`, sandboxed Jinja2, secret scan on save, danger scan on rendered
  output.** Each of these closes a real, specific failure mode.
- **Poller design:** one SSH session, a commit per task, a per-device lock, backoff, stale-row
  deletion. Simple and robust.
- **Verified against real IOS-XE 17.3 and 15.4.** Most of the parser gotchas were found on real routers.
- **Small stack** (Flask, SQLite, waitress, vanilla JS). It runs on a tiny Alpine VM.

**Gaps that matter for the stated aim:**

| Area | Gap |
|---|---|
| Configuration | Nothing reads the device's **actual** config (`show running-config`). No backup, no history, no "intended vs actual" check. |
| Monitoring | Each poll overwrites current state. There are **no events** ("BGP peer went down at 14:02"), no alerts, no trends (interface errors, uptime), and no reachability check apart from the 5-minute SSH poll. |
| Security | The UI and API have **no auth at all**. Anyone on the lab network can edit templates, delete devices or set credentials. Constraint 13 covers only `/configs/*.cfg`. |
| Testing | No `tests/` and no `dev/regress.py`, even though CLAUDE.md lists 13 "do not regress" constraints. The parsers have no captured-output fixtures. |
| Deployment | `build-template.sh` and the OpenRC scripts have **never run on a real Alpine VM**. There is no upgrade path and no DB backup. A VM built before the rename is not migrated. |
| Discovery | Sweep, vCenter and confetti import run only when triggered by hand. vCenter is unverified. |
| Vendors | Parsers and commands are IOS only. There is no per-platform dispatch yet. |

---

## 2. Comparison with similar projects

| Project | What it does | Overlap | What's worth borrowing |
|---|---|---|---|
| **NetBox / Nautobot** | Source-of-truth DCIM + IPAM | Inventory, IPAM, config context (≈ `template_vars`), config templates (NetBox 3.5+ renders Jinja2 per device) | Prefix/IP *allocation* (next free IP), VLAN model, custom fields, tags. Nautobot's **Golden Config** (backup + intended + compliance) maps directly onto the config gap. |
| **LibreNMS / Observium** | SNMP monitoring | Discovery, syslog, LLDP maps, alerting | Event log, alert rules, port graphs, ICMP availability checks |
| **Oxidized / RANCID** | Config backup with git history | (none yet) | Scheduled `show run` into versioned storage with diffs. This is the main missing feature. |
| **Netdisco** | L2 discovery, LLDP/CDP crawl | LLDP topology | **Neighbor crawl**: discover new devices from LLDP mgmt IPs automatically |
| **phpIPAM** | IPAM | IPAM | Subnet utilisation, free-IP finder, ping scan per subnet |
| **Batfish / SuzieQ** | Config/state analysis | IPAM checks, adjacency state | SuzieQ-style **state snapshots over time** ("what changed between polls") |
| **Cisco PnP / IOS-XE autoinstall (ZTP)** | Day-0 provisioning | Pull config delivery | Serve configs to *unconfigured* routers by DHCP option 67 or by serial |
| **Graylog / rsyslog** | Log management | Syslog | Severity/mnemonic **alert rules**, forwarding |

**Summary:** confetti-butler covers about 60% of NetBox-lite + Netdisco-lite + a syslog viewer, and adds
a better identity model. What's missing is mostly what Oxidized (config backup) and LibreNMS (events and
alerting) provide.

---

## 3. Suggested features (ranked by value for the effort)

### Tier 1: fits the aim closely, small change
1. **Running-config backup + diff (Oxidized-style).** Add a sixth read-only poll task, `config`:
   `show running-config`. Store a new `config_versions` row only when the hash changes (after
   normalising volatile lines such as `! Last configuration change`, `ntp clock-period`). The device
   page gets a version list and a diff view. Still read-only, so it stays inside the pull-only rule.
   Note: the stored config holds type-7/5/9 secrets. Keep it in the same 0600 DB, and redact secrets in
   the API output.
2. **Compliance: intended vs actual.** Compare the rendered template with the latest running-config
   backup and show missing/extra lines per device. Add a "Compliant / Drifted" column on the Devices
   page. This closes the loop on "how do they get configured".
3. **Change events table.** In each poll's apply step, compare old and new rows: interface oper up↔down,
   BGP/OSPF state change, LLDP neighbor added or removed, serial or OS version change, config changed.
   Write one `events` row per change. Add a timeline on the dashboard and the device page. The apply
   steps already delete stale rows, so they already have the "before" state.
4. **Optional write auth.** One `BUTLER_ADMIN_TOKEN` (or HTTP basic). If set, it is required on
   POST/PUT/DELETE only. GET pages and `/configs/*.cfg` stay open (constraint 13 still holds). Off by
   default, so the lab workflow doesn't change.
5. **Regression script.** Create `dev/regress.py` with one check per CLAUDE.md constraint, plus parser
   fixtures from scrubbed real output (17.3.8a, 17.3.2, 15.4). The `constraint-regression` skill and the
   `regression-gate` agent are already set up for exactly this.

### Tier 2: monitoring
6. **ICMP reachability ticker.** A light thread pings each `mgmt_ip` every 30–60s (BusyBox `ping`, no
   new dependency). Store up/down plus an RTT trend. Show a dot on the device and topology nodes. Today
   a dead router looks healthy until its next SSH poll fails.
7. **Alerting.** Rules on events and syslog (severity ≤ N, mnemonic match such as
   `%BGP-5-ADJCHANGE`, `%LINK-3-UPDOWN`). Send to a generic **webhook** (Teams, Slack, ntfy, Gotify all
   accept JSON POST), plus an optional SMTP mail. Rate-limit to avoid floods.
8. **Interface counter trends.** The `interfaces` task already runs `show interfaces`. Keep CRC/input
   errors/drops and rates in a small time-series table (pruned like `poll_history`). Flag rising errors
   using the `network-interface-health` skill's thresholds.
9. **`/metrics` endpoint (Prometheus text format, hand-written, no library).** Device up, poll
   duration/failures, adjacency state, syslog rate, poller queue depth, DB size. This lets
   Grafana/Prometheus monitor both the lab and butler itself.
10. **Topology state colouring.** Colour edges and nodes by adjacency state and reachability, so a
    down BGP session shows red. Optionally save node positions in the DB.

### Tier 3: discovery and IPAM
11. **Scheduled discovery.** Run confetti import, sweep and seedfile on a timer (env-configured), like
    the poller tick.
12. **LLDP neighbor crawl (Netdisco-style).** If an LLDP neighbor's `remote_mgmt_ip` is not a known
    device, offer it on a "Discovered" list or add it automatically through `identity.ingest`. Ghost
    nodes then turn into real devices.
13. **IPAM extras.** Subnet utilisation, next-free-IP per prefix, and reserved/planned prefixes (a
    small `prefixes` table). This is the first step from discovery-only IPAM towards a source of truth.
14. **Day-0 / ZTP.** Add `/configs/by-serial/<serial>.cfg` and docs for IOS-XE autoinstall (DHCP option
    67 → HTTP) and the CSR1000v's `ovf-env` / guestinfo bootstrap day-0 config. Still pull-only.

### Tier 4: platform breadth
15. **Per-platform command/parser dispatch** (`parsers/<platform>.py` keyed by `devices.platform`).
    Then Juniper vMX/vSRX (planned; Arista EOS was dropped from the roadmap 2026-10-10), and Linux via `lldpd`. That
    last one would let confetti-traffic nodes show up properly on the topology.
16. **SNMP polling** (already planned in Design Decisions): `snmpwalk` for interface counters on
    devices without SSH.

---

## 4. Streamlining deployment

1. **Actually run `build-template.sh` on Alpine (top priority).** It is the only unverified part of the
   main install path. Check the `pip install` step against PEP 668 (Alpine 3.19+ blocks system pip
   without `--break-system-packages` or a venv).
2. **Pinned `requirements.txt` plus a venv** at `/opt/confetti-butler/venv`, so netmiko/waitress
   versions are reproducible.
3. **Packer template** (`vsphere-iso` builder + Alpine answer file) that runs `build-template.sh` and
   outputs the vCenter template or an OVA in one command. This replaces "boot ISO, setup-alpine, scp,
   run, convert" by hand.
4. **OVF/vApp properties via guestinfo at first boot:** static IP, `BUTLER_SSH_*`, confetti hub URL,
   seed YAML. A clone then needs no console session. `firstboot.initd` already reads guestinfo, so this
   extends it.
5. **`butler-update.sh`:** take a tarball or `git pull`, back up the DB, swap `/opt/confetti-butler`,
   restart. Rollback = the previous directory. Also a one-time `migrate-from-lab-butler.sh` for the VM
   left behind by the rename.
6. **DB backup:** nightly cron `sqlite3 butler.db ".backup …"` with rotation. This is WAL-safe, unlike
   copying the file.
7. **Optional container image** (Alpine base, same entrypoint) for dev/demo outside ESXi. Needs host
   networking for syslog/514. Low priority, because the VM is the target.

---

## 5. Suggested order of work (before your answers; section 7 replaces it)
1. Run the build on Alpine + venv/requirements + DB backup (deployment is only trustworthy once verified)
2. `dev/regress.py` + parser fixtures (protects the rest of this list)
3. Running-config backup + diff → compliance view
4. Events table → ICMP reachability → webhook alerting
5. Optional write token
6. `/metrics`, topology state colouring, scheduled discovery, LLDP crawl
7. ZTP, IPAM allocation, multi-vendor

Each item is a separate change, verified the usual way: against the real lab routers where it touches
the poller or parsers, and on the mock-lab scratch DB (`scripts/seed_mock_lab.py`) for UI. Re-run
`capture_readme.py` after UI changes.

## 6. Your decisions (2026-10-09)
- In scope: **Config backup + drift**, **Events + alerting**, **Deployment hardening**.
- Alerting lives **inside butler** (webhook/SMTP). `/metrics` is not part of this plan.
- Config backups are stored **in SQLite**.
- **No auth.** This is a single-operator, isolated lab. Write auth (Tier 1 #4) is dropped.
- Not in this plan: regress.py, IPAM extras, ZTP, multi-vendor, Packer/container.

---

## 7. Implementation plan

Order: **A → B → C**. Each phase is a separate commit and is verified before the next one starts.

### Phase A: Running-config backup + drift
**Schema** (`app/db.py` `init_db`, `CREATE TABLE IF NOT EXISTS`, so no migration is needed):
- `config_versions(id, device_id → devices ON DELETE CASCADE, sha256, body, captured_at)` with an index
  on `(device_id, captured_at)`.
- **Retention:** keep every change, because changes are rare and small. Add an optional
  `BUTLER_CONFIG_VERSIONS_KEEP` (default 0 = unlimited) to `config.py`.

**Collector** (`app/collectors/ssh.py`):
- Add `"config": "show running-config"` to `COMMANDS`. The `PARSERS` entry goes in `parsers/ios.py`:
  `parse_running_config(text)` strips the `Building configuration...` / `Current configuration : N
  bytes` header and volatile lines (`! Last configuration change`, `! NVRAM config last updated`,
  `ntp clock-period`). It returns the normalised text, or `None` when the output doesn't look like a
  config. That case counts as a parse failure, so nothing is stored, the same rule as `interfaces` on
  an empty result.
- `read_timeout` for this task is about 60s, because a large config is slow over SSH.
- `_apply_config`: hash the text. Insert a row **only when the hash differs** from that device's latest
  row. Return "changed" so Phase B can emit an event.
- **The task runs last in `TASKS`**, so a slow config read never delays the five existing tasks. Note:
  `poll_history.output` is cut to 20000 chars (`poller.py:148`). That's fine, because the full text lives
  in `config_versions`.

**Secret handling:** the raw body is stored (it is the backup). The API redacts by default. A new
`rendering.redact_secrets(text)` reuses the same secret-bearing command patterns as
`check_hardcoded_secrets` (`rendering.py:75`) and masks the value after `secret|password|community|key`.
`?raw=1` returns the unredacted text for restore.

**Drift:** new `rendering.drift(conn, device_id)`. It renders the device's assigned template with
`render_for_device` (`rendering.py:142`), then compares the rendered config with the latest backup:
- **Containment, not a full diff.** A template is usually a *partial* config. Report rendered lines
  **missing** from running-config, matched by IOS section (parent line + its indented children).
  Running-config lines the template doesn't mention are not drift.
- Normalise whitespace. Skip `!`, blank lines and `end`.
- Result: `{status: compliant|drifted|no_template|no_backup|render_error, missing: [...]}`. A render
  error (`StrictUndefined`) is a status, not a 500.

**Routes** (`app/app.py`):
- `GET /api/devices/<id>/configs`: list (id, captured_at, sha, size).
- `GET /api/devices/<id>/configs/<vid>`: body (redacted unless `?raw=1`).
- `GET /api/devices/<id>/configs/diff?a=<vid>&b=<vid>`: unified diff (`difflib`, stdlib). Redacted.
- `GET /api/devices/<id>/drift`.
- Add `drift_status` to `GET /api/devices` (computed per request; cheap at lab scale).

**UI:**
- `templates/device.html`: a new "Config history" section with a version list, a two-version diff
  view (the `.section` pattern, theme colours for +/- lines) and a "Drift" panel listing the missing lines.
- `templates/devices.html`: add a Drift column (compliant / drifted / —).

### Phase B: Events + alerting
**Schema:**
- `events(id, at, device_id NULL, kind, severity, subject, detail, alerted INTEGER DEFAULT 0)` with an
  index on `at`. Pruned by `BUTLER_EVENT_RETENTION_DAYS` (default 30) in the poller tick, next to the
  `poll_history` prune (`poller.py:93`).
- `alert_rules(id, name, enabled, match_kind, match_text, min_severity, target, cooldown_s, last_fired_at)`.
  `match_kind` is one of `event` (by event kind) or `syslog` (mnemonic/regex + severity).

**Event sources (compare state before and after the write):**
- `_apply_interfaces`: read the old `oper_status` for the device first. Emit `interface_down` /
  `interface_up` on change, and `interface_errors` when `crc_errors`/`input_errors` grow. Skip the very
  first poll of a device (no old rows), so adding a device doesn't trigger a storm.
- `_apply_adjacencies`: old vs new `state` per peer gives `bgp_state` / `ospf_state`. Also
  `peer_removed` for rows the delete step drops (SELECT them before the DELETE).
- `_apply_lldp`: `lldp_neighbor_added` / `lldp_neighbor_removed`.
- `_apply_version`: `os_version_changed` (reload/upgrade).
- `_apply_config` (Phase A): `config_changed`.
- `poller._finish`: `device_unreachable` when `fail_count` goes 0→1, `device_recovered` on the first OK
  after failures.
- **ICMP reachability (new, small):** `app/reach.py`, a daemon thread with the same idempotent `start()`
  shape as `poller.py`, started in `serve.py`. Every `BUTLER_PING_INTERVAL_S` (default 30) it pings each
  enabled device's `mgmt_ip` (including `role='node'`) with `ping -c 1 -W 1` through `subprocess`. That
  is BusyBox ping on Alpine; the Windows dev run uses `ping -n 1 -w 1000`. Store `devices.reachable`
  + `last_ping_at`, plus a `ping_down`/`ping_up` event on change only, after 2 failures in a row to
  avoid flaps. New columns are added via `ALTER TABLE ... ADD COLUMN` guarded in `init_db`, following
  the existing migration style in `db.init_db`.
- All writers call one helper, `app/events.py: emit(conn, device_id, kind, severity, subject, detail)`.
  It runs inside the caller's transaction, so an event commits with the data that caused it.

**Alerter:** `app/alerts.py`, one daemon thread that wakes every 10s:
- Select `events WHERE alerted = 0`. Match them against enabled `event` rules, send, set `alerted = 1`.
- Syslog: `syslog_server` calls `alerts.match_syslog(row)` after insert. That only appends to an
  in-memory queue, and the alerter thread drains it, so the UDP receive path never blocks on HTTP
  (same fail-soft rule as the receiver).
- Targets: `webhook:<url>` (JSON POST with `requests`, already a dependency; a generic
  `{text, event}` body works with ntfy/Gotify/Teams/Slack-compatible endpoints) and `mail:<addr>`
  (stdlib `smtplib`, `BUTLER_SMTP_*` env).
- Per-rule `cooldown_s` stops floods. Failed sends are logged to the event's `detail` and are **not
  retried forever** (one retry, then give up).

**Routes + UI:**
- `GET /api/events?device_id=&kind=&since=&limit=`. `GET/POST/PUT/DELETE /api/alert-rules`.
  `POST /api/alert-rules/<id>/test`.
- Dashboard: an "Events" box (latest 15, like the Syslog box). Device page: an events timeline.
- New `templates/alerts.html` page plus a nav link in `base.html`: rule list and editor, "Send test".
- Topology: colour nodes by `reachable`, and colour BGP/OSPF edges red when not Established/FULL
  (`/api/topology` already returns state).

### Phase C: Deployment hardening
1. **Real Alpine run of `build-template.sh`** on an ESXi VM (latest Alpine 3.x). Fix whatever breaks.
   Confirm OpenRC start, port 80, UDP 514, poller, firstboot guestinfo. Record the result in HANDOFF.md.
   *Needs you:* a fresh Alpine VM, or SSH access to one.
2. **Pinned dependencies:** add `butler/requirements.txt` with exact versions for the pip-installed
   packages only (netmiko and its pulled-in deps, flask, waitress fallback). `build-template.sh` lines
   153–168 install from it with `--break-system-packages` (kept, already PEP-668 safe). Alpine `py3-*`
   packages stay with apk.
3. **DB backup:** `services/butler-backup.sh`. It runs `sqlite3 butler.db ".backup …/backups/butler-
   YYYYmmdd.db"` (WAL-safe) and keeps the last `BUTLER_BACKUP_KEEP` (default 7). Installed into
   `/etc/periodic/daily/` (BusyBox crond is already in Alpine; `rc-update add crond`). Add the `sqlite`
   apk to the package list. Backups go in a 0700 dir, because they contain credentials.
4. **`scripts/butler-update.sh <tarball|dir>`:** run the backup first, copy the new app to
   `/opt/confetti-butler.new`, then swap the directories (keep `.prev` for rollback), restart the service,
   and check `/api/health`. On a failed health check, swap back automatically.
5. **`scripts/migrate-from-lab-butler.sh`:** stop `lab-butler`, move `/opt/lab-butler`,
   `/var/lib/lab-butler` and `/etc/lab-butler` to the new names, swap the OpenRC services, fix the
   `BUTLER_HEALTH_SERVICES` line in `butler.env`, start the service. Idempotent, and a no-op on a fresh VM.
6. **guestinfo extension:** `firstboot.initd` additionally reads `guestinfo.butler.ssh_username`,
   `.ssh_password`, `.confetti_hub_url` and `.ping_interval` into `butler.env`, so a clone needs no
   console. Extend only the keys; reuse the existing reading pattern.

### Docs
CLAUDE.md gets: the `config` task (6 tasks, not 5), `config_versions` + redaction, drift semantics
(containment), the events/alerts/reach threads (now **five** DB writers; extend constraint 5's
wording), and new constraints worth numbering:
- the syslog alert match never blocks the receiver;
- events are emitted inside the data transaction;
- the first poll emits no events.

Also update HANDOFF.md, the `confetti-butler-hub-api` skill, and the README feature list. Re-run
`scripts/capture_readme.py` after the UI changes. Run the `docs-drift-checker` agent at the end.

## 8. Verification
- **Phase A, live:** poll lab-rtr-a/b/c → one `config_versions` row each. Poll again → no new row
  (normalisation works). Make an unsaved harmless change by hand (an interface `description`) → new
  version, the diff shows the line, and redaction hides `enable secret` / `username … secret`. Assign a
  template → drift shows compliant or the expected missing lines. A template with an unset var →
  `render_error`, not a 500. Check that 15.4 (lab-rtr-c) output parses as well.
- **Phase B, live + mock:** shut an interface or a BGP neighbor on a lab router → an event within one
  poll. Block the mgmt IP or power off a VM → `ping_down` in about 60s and `device_unreachable` at the
  next poll. A webhook rule pointing at a local catch server (`python -m http.server`-style listener in
  the scratchpad, or ntfy.sh) receives exactly one POST per event within cooldown. A syslog burst
  (20 datagrams to dev UDP 5514) matches a mnemonic rule and the receiver doesn't stall. A freshly
  added device produces no event storm.
- **Phase C:** full build on a real Alpine VM. Reboot, the service comes up. Run the backup by hand and
  open the backup with `sqlite3`. Run the update script with a deliberately broken build → automatic
  rollback, healthy again. Run the migrate script on a VM with old paths (or a staged copy).
- **Every phase:** check all pages in Chrome on the mock-lab DB for console errors, in Classic plus one
  CRT layout.

---

## 9. Selected optional features (2026-10-09)

Chosen on top of Phases A, B and C above:

- **Monitoring:** ICMP reachability, interface counter trends, `/metrics` endpoint, topology state colours.
- **Discovery:** scheduled discovery, LLDP neighbor crawl.
- **Platform and quality:** regression script (`dev/regress.py`), multi-vendor parsers, SNMP polling.
- **Deployment:** Packer template, container image.

Not selected (kept for later): IPAM allocation, ZTP by serial, write auth token.

Note: section 6 says `/metrics` is out of scope and the regression script is not in this plan. Both are now in scope; this section overrides section 6.

## 10. Per-phase selection (2026-10-09)

Build order starts with Phase A.

- **Phase A:** backup + versions (with secret redaction), diff view, retention limit.
  Not selected: drift check.
- **Phase B:** events + timeline only.
  Not selected: webhook alerts, email alerts, syslog alert rules.
- **Phase C:** Alpine build + pinned deps, update + rollback script.
  Not selected: DB backup cron, migrate + guestinfo.

Section 10 narrows sections 7 and 9: the drift check, alerting and the unselected Phase C scripts are deferred.

---

## 11. Status (2026-10-09)

Built: Phases A, B, C (backup + diff + retention, events + timeline, pinned requirements, update script,
real-VM build, nightly backup, guestinfo settings, lab-butler migration), then the optional features from
section 9: ICMP reachability, interface counter trends, `/metrics`, topology state colours, scheduled
discovery, LLDP neighbor crawl, regression script, multi-vendor dispatch (IOS + SNMP; the EOS and Junos parsers were removed 2026-10-10), SNMP polling, Packer
template, container image. Still open: drift check, alerting (webhook / email / syslog rules), IPAM
allocation, ZTP by serial, write auth token. Details and what was verified: `docs/HANDOFF.md`.

---

## 12. Status (2026-10-10)

- **Dropped:** Arista EOS. **Planned, not started:** Juniper Junos (the unverified EOS and Junos parsers that had
  been written from documentation were removed rather than shipped; the platform registry in
  `app/platforms.py` is where a vendor is added).
- **Built since section 11:** the drift check and alerting (webhook, e-mail, syslog rules).
- **Still open:** IPAM allocation (next-free-IP, planned prefixes), ZTP by serial, write auth token, Junos.
