# lab-butler — Build Handoff

Point-in-time record of the initial build, 2026-09-14/15. Read `CLAUDE.md` at the repo root first —
it's the architecture reference and carries the numbered "do not regress" constraints. This file is
the narrative of how the build went: what was done, what was verified against real hardware, what
wasn't, and what to check before trusting any of it further.

## What this session did

Designed and built lab-butler from nothing — a Flask/SQLite server for network device inventory,
IPAM, pull-only config template delivery, syslog, and LLDP/BGP/OSPF topology visualization,
companion to the sibling project `../mesh-flux`. All ten phases of the approved build plan were
completed and, with one exception, verified against real hardware: a lab CSR1000v running
IOS-XE 17.3.8a at `192.0.2.10`, credentials supplied directly by the user for this purpose.

## Phase-by-phase status

| # | Phase | Status |
|---|---|---|
| 1 | Skeleton (config, db, health/time endpoints) | Built, verified |
| 2 | Devices + identity ladder | Built, verified — **bug found and fixed**, see below |
| 3 | Poller + SSH collector | Built, verified live against the CSR1000v |
| 4 | Syslog receiver | Built, verified (UDP send, parsing, query-time device correlation) |
| 5 | IPAM (overlap/duplicate-IP detection) | Built, verified with synthetic overlap scenarios |
| 6 | Config templates + pull delivery + editor | Built, verified live — **bug found and fixed**, see below |
| 7 | LLDP chassis self-registration, BGP/OSPF parsers | Built; parsers verified live, resolution logic verified with synthetic multi-device data (this lab has only one router, so live cross-device LLDP resolution couldn't be exercised) |
| 8 | Topology (vis.js graph, ghost nodes) | Built, verified with synthetic topology data |
| 9 | Remaining seed sources (sweep, vCenter, mesh-flux import) | Sweep and mesh-flux import verified live; **vCenter built but not verified** — no vCenter instance was reachable |
| 10 | Packaging (build-template.sh, OpenRC, firstboot) | Built, shell-syntax-checked (`sh -n`); **never run on a real Alpine VM** |

## Two real bugs caught during verification

Both would have shipped silently if this had gone straight from design to "looks right" without
live testing against real hardware and a real Flask template render.

1. **Identity ladder data corruption** (`app/identity.py`). Matching a device via a weak identifier
   (`mgmt_ip`) while the observation also carried a *different* strong identifier (`serial`) used to
   silently overwrite the stored serial. Fixed by `_detect_kind_clash`: a stronger-or-equal identifier
   disagreeing with what's on file is now a conflict, not a silent update. Verified both directions —
   a legitimate rename+re-IP via a matching serial still updates freely; a stale IP colliding with a
   different serial now correctly conflicts instead of merging two different physical devices.

2. **Flask/Jinja2 templating bug** (`templates/templates_editor.html`). The CodeMirror editor page's
   own inline `<script>` defines a custom syntax-highlighting mode containing literal `{{`, `}}`,
   `{%`, `%}` in JS regex source. `render_template()` parses the *entire* HTML file through Jinja2
   server-side, so it tried to interpret those as its own template syntax and 500'd. Fixed by wrapping
   the whole script block in `{% raw %}...{% endraw %}`.

Full detail on both, plus eleven other non-obvious constraints, is in `CLAUDE.md`'s "Non-obvious
constraints" section.

## What is NOT verified — check before relying on it

- **vCenter discovery** (`app/collectors/vcenter.py`). Written against the documented vSphere REST API
  shape for 7.0. No live vCenter was reachable to confirm the actual endpoint responses, auth flow, or
  field names match.
- **Packaging** (`build-template.sh` and the OpenRC/firstboot scripts). Syntax-valid, structurally
  mirrors mesh-flux's own build script closely, but has not been run against a real Alpine install —
  same caveat mesh-flux's own build docs carried at this stage of that project.
- **Cross-device LLDP/BGP/OSPF resolution** in a real multi-device lab. The resolution logic
  (`remote_device_id` / `peer_device_id` lookups) is verified correct against synthetic data seeded
  directly into SQLite, but this session only had one real router available, which had LLDP disabled
  and no BGP/OSPF peers configured — so the live end-to-end path (two real devices, one polls the
  other's neighbor table, the resolution actually fires) has not been exercised.
- ~~**`show lldp entry local`'s success-path output.**~~ Resolved 2026-09-27: with LLDP enabled
  (lab-rtr-b, IOS-XE 17.3.2) the command returns 0 entries. It looks up a neighbor named "local".
  The command was dropped, and LLDP neighbors now resolve by advertised management IP. That was
  verified live: lab-rtr-b's neighbor LAB-RTR-C resolved to its device row via `192.0.2.167`.

## Housekeeping

- **Not a git repository yet.** Nothing built this session has been committed anywhere.
- Local Python dependencies for development were installed into the ambient environment on this
  machine (`pyyaml`, `netmiko`, `requests` — `flask`/`waitress`/`jinja2` were already present). The
  Alpine package list `build-template.sh` installs is the authoritative list for a real deployment;
  it has not been cross-checked against the live Alpine package index (see constraint 3 in
  `CLAUDE.md` and the `py3-paramiko`/`netmiko` note in `build-template.sh` itself).
- CodeMirror 5.65.16 and vis-network 9.1.9 are vendored under `butler/static/vendor/` (pinned
  versions, downloaded directly, no CDN dependency at runtime).
