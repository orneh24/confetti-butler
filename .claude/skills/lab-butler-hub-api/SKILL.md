---
name: lab-butler-hub-api
description: lab-butler's own HTTP contract — device identity/merge rule, poller scheduling, config-template rendering and pull delivery, IPAM, topology, and syslog correlation. Read before changing app/app.py, identity.py, poller.py, rendering.py, or any schema in db.py.
origin: lab-butler
---

# lab-butler Hub API

The HTTP contract, schema, and data-flow rules for lab-butler's server. Mirrors
`confetti-hub-api`'s role for its own project — this is the reference for lab-butler's own
routes and internals, not for network devices in general (see the other five skills for that).

## When to Activate

- Adding or changing a route in `app/app.py`
- Changing anything in `identity.py`, `poller.py`, `rendering.py`, `ipam.py`, or the schema in `db.py`
- Debugging why a device didn't dedupe correctly, a poll didn't run, or a config render failed
- Adding a new seeding/discovery source beyond the four that exist

## Device identity — do not break without understanding this first

`devices.key` comes from a strength ladder: `serial` (4) > `vmuuid` (3) > `mgmt_ip` (2) >
`hostname` (1). Every identifier ever observed lives in `device_aliases` permanently — not current
state, a history. A key only ever promotes upward, never demotes.

Two entry points, not interchangeable:
- `identity.ingest(conn, candidates, fields, source, ref)` — discovery paths (manual, sweep,
  vCenter, seedfile, confetti-traffic import). Resolves the device by alias-matching from scratch.
- `identity.observe(conn, device_id, candidates, fields, source, ref)` — the poller only. The
  device_id is already known (it dialed that device's own `mgmt_ip`), so this skips alias-matching
  and only guards against a hardware-identity field (`serial`/`vmuuid`) disagreeing with what's on
  file — a possible device swap. Calling `ingest` from the poller with just a `serial` candidate
  (no `mgmt_ip`) would find zero matches and create a duplicate device.

Both raise `identity.Conflict` on ambiguity — >1 existing device matched, or a stronger identifier
disagreeing with what's on file for the one device matched via a weaker identifier. A
`merge_conflicts` row is written before the raise. **Nothing auto-merges.** The operator resolves
via `POST /api/devices/<survivor_id>/merge` with `{"loser_id": N}`, which re-points every child
table (`device_aliases`, `device_sources`, `credentials`, `interfaces`, `template_vars`,
`lldp_neighbors`, `adjacencies`) at the survivor and deletes the loser.

## Routes

**Pages:** `/` `/devices` `/devices/<id>` `/conflicts` `/ipam` `/templates` `/topology` `/syslog`

**Devices:**
- `GET|POST /api/devices` — POST goes through `identity.ingest`, so a manual add of an
  already-known device updates it rather than duplicating
- `GET|PUT|DELETE /api/devices/<id>` — PUT is an **authoritative operator edit**, bypasses the
  identity ladder entirely (can blank a field `ingest` would never overwrite with empty). It does
  record new `serial`/`mgmt_ip`/`hostname` values as aliases (`identity.add_operator_aliases`); a
  value another device already owns is not moved, it comes back in `alias_warnings`. The device
  page's Edit card sends only the fields that changed
- `POST /api/devices/<id>/poll` — synchronous, inline on the request thread (not queued through the
  pool) — an operator clicking "poll now" wants the result, not a 202
- `GET /api/devices/<id>/interfaces`, `GET|PUT /api/devices/<id>/credentials` (GET returns
  `username`, `snmp_version` and `*_set` booleans, **never a secret**; PUT is partial: only fields in
  the body change, `""` clears one). Both are driven by the device page's Credentials card,
  `GET|PUT /api/devices/<id>/vars`
- `POST /api/devices/<id>/merge`, `GET /api/conflicts?resolved=0|1`,
  `POST /api/conflicts/<id>/dismiss` (resolve without merging — the only way to clear a
  single-device conflict). An identical conflict already open is not re-inserted, only its
  `seen_at` is bumped
- `POST /api/discover/{sweep,vcenter,seedfile,confetti}` — all four return
  `{device_ids: [...], conflicts: [...]}` in the same shape

**Templates:** `GET|POST /api/templates`, `GET|PUT|DELETE /api/templates/<name>`,
`GET /api/templates/<name>/versions`, `POST /api/templates/<name>/preview` (body:
`{device_id, body?}` — `body` present previews unsaved edits without touching the saved template).
`GET /configs/<device_key>.cfg` is the **pull endpoint** a device's console fetches — unauthenticated
by design, plain text, 404 if the device or its assigned template doesn't exist, 500 if
`StrictUndefined` catches an unset variable.
`GET /configs/` is a plain-text index of the devices that have an assigned template.

**IPAM:** `GET /api/ipam/{prefixes,addresses,findings}`, `POST /api/ipam/analyze` (rebuilds
`ipam_findings` wholesale — not incremental)

**Topology/adjacencies:** `GET /api/topology?layer=lldp|bgp|ospf` (vis.js `{nodes, edges}` shape,
default overlays all three), `GET /api/adjacencies`

**Syslog:** `GET /api/syslog` (`?minutes=`, `?from=&to=`, `?host=`, `?severity=`, `?q=`,
`?device_id=` — device_id resolved via a join at query time, not stored at insert),
`GET /api/syslog/sources`

**Health:** `GET /api/time` (chrony tracking), `GET /api/health` (services, poller_running,
syslog_listening, load/memory/disk/uptime) — both **never 500**, every check degrades independently
to `null`/`"unknown"` on a flaky or non-Alpine box.

## Poller scheduling

`devices.poll_state` (`idle`/`running`) is the per-device lock; `next_poll_at` is the schedule.
Each tick claims due devices, marks them `running` (so a slow poll is never picked up twice), and
runs five independent tasks per device — `version`, `interfaces`, `lldp`, `bgp`, `ospf` — over one
SSH session, each committing its own transaction. Rows a task no longer sees are deleted.
`running` is always released in a `finally`, and reset at poller startup. `role='node'` devices are excluded from the claim query entirely.

Success: `fail_count=0`, `next_poll_at = now + poll_interval_s`. Failure:
`fail_count += 1`, `next_poll_at = now + min(interval * 2**fail_count, POLL_MAX_BACKOFF_S)`.

## Config rendering

Context passed to every template: `device` (the full devices row as a dict), `interfaces` (list of
that device's interface rows), `vars` (that device's `template_vars`, operator-set). Rendering uses
`jinja2.StrictUndefined` — an unset `{{ vars.foo }}` raises, it does not render empty.

`check_hardcoded_secrets` runs on template **save** (source text) and flags a secret-bearing command
(`enable secret`, `username ... secret/password`, `snmp-server community`, `key string`,
`neighbor ... password`) with a literal value instead of `{{ vars.* }}`. `check_dangerous_commands`
/ `check_security` run on **rendered output** at preview time, not on the template source — a
Jinja2 `{% if %}` can make a dangerous line appear only for some devices.

## Anti-Patterns

```python
# BAD: writing to devices directly from a new discovery source
conn.execute("INSERT INTO devices (...) VALUES (...)")
# GOOD: always go through identity.ingest — see the identity section above

# BAD: poller code calling identity.ingest instead of identity.observe
identity.ingest(conn, [("serial", new_serial)], {}, source="poll")
# creates a DUPLICATE device — the poller already knows device_id; use observe()

# BAD: storing a timestamp as ISO-8601
conn.execute("... VALUES (?)", (datetime.now().isoformat(),))
# GOOD: db.sqlite_now() on the way in, db.iso() on the way out — see CLAUDE.md constraint 4

# BAD: an inline <script> in a Flask template with literal {{ }} used as JS/regex syntax
# 500s — render_template() parses the WHOLE file as Jinja2. Wrap in {% raw %}...{% endraw %}.

# BAD: rendering a template with a permissive Undefined class, or unsandboxed
jinja2.Environment(undefined=jinja2.Undefined)
# GOOD: jinja2.sandbox.SandboxedEnvironment(undefined=jinja2.StrictUndefined) — templates come
# over an unauthenticated API; an unsandboxed Environment is remote code execution
# GOOD: StrictUndefined — a blank line in a config about to be applied to a
# router is worse than a loud failure at preview time
```

## Related Skills

- netmiko-ssh-automation (the SSH collector's connection/error-handling pattern)
- network-config-validation (dangerous-command / security-check source for rendering.py)
- network-bgp-diagnostics, network-interface-health (parser logic source for parsers/ios.py)
- cisco-ios-patterns (command syntax reference for template authoring)
