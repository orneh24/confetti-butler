# lab-butler — Build Handoff

Point-in-time record of the initial build, 2026-09-14/15, plus follow-up sessions on 2026-09-27
and 2026-09-28 (see the "Follow-up session" sections below). Read `CLAUDE.md` at the repo root first —
it's the architecture reference and carries the numbered "do not regress" constraints. This file is
the narrative of how the build went: what was done, what was verified against real hardware, what
wasn't, and what to check before trusting any of it further.

## What this session did

Designed and built lab-butler from nothing — a Flask/SQLite server for network device inventory,
IPAM, pull-only config template delivery, syslog, and LLDP/BGP/OSPF topology visualization,
companion to the sibling project `../confetti-traffic`. All ten phases of the approved build plan were
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
| 7 | LLDP/BGP/OSPF parsers and neighbor resolution | Built; chassis self-registration **replaced 2026-09-27** by advertised-mgmt-IP matching, then verified live across two routers (see follow-up session) |
| 8 | Topology (vis.js graph, ghost nodes) | Built, verified with synthetic topology data |
| 9 | Remaining seed sources (sweep, vCenter, confetti-traffic import) | Sweep and confetti-traffic import verified live; **vCenter built but not verified** — no vCenter instance was reachable |
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
  mirrors confetti-traffic's own build script closely, but has not been run against a real Alpine install —
  same caveat confetti-traffic's own build docs carried at this stage of that project.
- ~~**Cross-device LLDP/BGP/OSPF resolution** in a real multi-device lab.~~ Resolved 2026-09-27:
  lab-rtr-b and lab-rtr-c resolve to each other over LLDP in both directions, and live OSPF
  (FULL) and BGP (Established/Active) adjacencies parse correctly. BGP/OSPF peer resolution to a
  *known device* is still unexercised — no peer IP matched a device's mgmt IP in this lab.
- ~~**`show lldp entry local`'s success-path output.**~~ Resolved 2026-09-27: with LLDP enabled
  (lab-rtr-b, IOS-XE 17.3.2) the command returns 0 entries. It looks up a neighbor named "local".
  The command was dropped, and LLDP neighbors now resolve by advertised management IP. That was
  verified live: lab-rtr-b's neighbor LAB-RTR-C resolved to its device row via `192.0.2.167`.

## Follow-up session (2026-09-27)

Three routers in the dev inventory: `lab-rtr-a` (192.0.2.10, IOS-XE 17.3.8a),
`lab-rtr-b` (192.0.2.171, 17.3.2) and `lab-rtr-c` (192.0.2.167, IOS-XE 3.11 / 15.4). These are
**placeholders** — the repo is public, so the real addresses and hostnames were scrubbed from the
whole history and are kept outside the repo. Main changes, all verified live unless noted:

- **LLDP:** `show lldp entry local` dropped (it looks up a neighbor *named* "local"; IOS-XE has no
  command showing a device its own chassis ID). Neighbors now resolve by advertised mgmt IP. Older
  IOS omits `Local Intf:` from the detail output, so the brief table fills it in.
- **Security:** template rendering moved to Jinja2's `SandboxedEnvironment` — the unsandboxed one
  allowed shell commands on the server through the unauthenticated template API.
- **Poller:** one SSH session per poll (~1.2s, was 10–15s); vanished interfaces, neighbors and
  peers are deleted; a device can no longer get stuck in `'running'`.
- **Conflicts:** Dismiss route and button; an identical open conflict is not re-inserted.
- **UI:** device page Edit and Credentials forms (secrets never returned by the API); topology
  keeps its layout on refresh and draws each LLDP link once, with one ghost node per peer IP; the
  IPAM page's missing Topology nav link is back.
- **Themes:** one shared palette in `static/theme.css`, picked from a header dropdown
  (`static/theme.js`): Dark, Light, Catppuccin Mocha, Gruvbox and Terminal green (since replaced by
  eight confetti-traffic palettes, 2026-10-05, see below). Gruvbox uses its
  aqua-green for "ok" and Terminal green uses amber for warnings, so ok/warning/failure stay
  distinct. The template editor (CodeMirror) is recoloured from the same palette. The same themes
  were added to confetti-traffic's hub pages. Verified in headless Chrome (Playwright): each theme applies,
  survives a page change, no JS errors.
- **README:** new, with a demo GIF (`docs/img/lab-butler-demo.gif`) recorded against a **mock lab**
  (documentation-range IPs, fake serials) on a scratch database — not real lab data. The seed
  script for that mock lab is not in the repo.
- **Other:** device delete cleans up `poll_history` and neighbor references; PUT records new
  identifiers as aliases; credential saves are partial.
- **Renamed:** sibling project lab-tester → mesh-flux (route `/api/discover/meshflux`, module
  `collectors/meshflux.py`; the sibling has since become Pervium on 2026-09-27, then Confetti Traffic
  on 2026-10-02; these names were kept until 2026-10-07, see below). Project skills moved from `skills/` to `.claude/skills/` so Claude Code
  loads them; new `lab-butler-dev-run` skill covers running locally on Windows.

## Follow-up session (2026-09-28)

- **Dashboard** (`templates/dashboard.html`, commit `72f654e`): the Clock and System cards are now
  one System card. A new full-width Syslog box shows the last 15 messages with the same filters as
  `/syslog`, using the existing `/api/syslog?limit=15` — no server change. Verified by sending 20
  test messages to the dev server's UDP 5514: the limit, severity and search filters returned the
  expected counts. **Not checked in a browser.**
- **RESTCONF tried on lab-rtr-a** (read-only, not part of the app). The user enabled it by hand
  (`aaa new-model`, `aaa authentication login default local`, `aaa authorization exec default local`,
  `restconf`) and left it **unsaved**, so a reload removes it. Findings:
  - `ietf-interfaces:interfaces` gives config only (name, IP, `enabled`), not live up/down state.
  - `Cisco-IOS-XE-interfaces-oper:interfaces/interface` gives live admin/oper status and IP. Its
    status names differ from the CLI (`if-oper-state-ready` = up, `if-oper-state-no-pass` = down),
    and unassigned interfaces show `0.0.0.0`.
  - Same data as `show ip interface brief` over SSH and lab-butler's `/api/devices/<id>/interfaces`.
    RESTCONF adds no new data, only structured output, and lab-rtr-c (IOS-XE 3.11) has no RESTCONF.
    The poller stays SSH-only; no RESTCONF path was added.

## Follow-up session (2026-10-05)

- **UI restyled to match confetti-traffic's hub UI.** All eight pages now `{% extends "base.html" %}`
  (header, nav, pickers, footer and Retro taskbar written once). New `static/layout.css` holds the
  Classic structure and four layouts (Classic, Modern, Retro 95, Amber CRT); `theme.css` now has
  confetti's eight palettes (Catppuccin and Gruvbox dropped), the header confetti strip and Neon's
  per-card colours. `theme.js` gained the layout picker, `confettiBlast()` and a header health dot
  that polls `/api/health` on every page. Modern shows five KPI tiles on the dashboard only, built
  from existing APIs — no backend change. Header title is `NETWORK LAB BUTLER`.
- Verified in Chrome against a dev server on a temp DB with two fake devices: every page returns 200
  with no console errors; Dark and Neon in Classic; all four layouts on the dashboard; Modern on the
  template editor; Retro 95 and Amber on topology (graph recoloured on layout change). Not checked:
  every theme × layout pair, Modern below 1100px, Shuffle across tabs, the Start menu, and a real
  poll or conflict showing in the KPI tiles.
- **README images re-recorded** for the new UI: `docs/img/lab-butler-demo.gif` (tour, themes, layouts,
  the Confetti button) plus `themes.png` and `layouts.png` (2×2 collages of the dashboard). Recorded
  with Playwright against a **mock lab** on a scratch database (3 routers, `192.0.2.0/24` and
  `10.0.x.x` addresses, fake serials `9SIMLAB000x`, one identity conflict, an overlap and a duplicate
  IP in IPAM) — no real lab data. The seed and capture scripts are not in the repo.
- The header status reads `up` (not `up since unknown`) when the server reports no uptime, as on Windows.
- Old saved `catppuccin` / `gruvbox` choices fall back to Dark (unknown names are ignored).
- Flask caches templates outside debug mode, so restart the server after editing a template.

## Follow-up session (2026-10-07)

- **meshflux renamed to confetti** (commit `8485ce2`): route `POST /api/discover/meshflux` →
  `/api/discover/confetti`, `collectors/meshflux.py` → `collectors/confetti.py`, `source` value
  `meshflux` → `confetti`, and the Devices page form id. Anything outside the repo still calling
  the old route now gets a 404.
- `db.init_db` migrates old rows: `device_sources` and `merge_conflicts` with `source='meshflux'`
  become `confetti`. If a device already has a `confetti` row for the same hub URL, the old row is
  dropped (primary key clash). Tested on a scratch DB with old rows, including that clash case;
  the route is registered. **Not tested:** a live import against a real confetti-traffic hub.
- "meshflux" now appears only in that migration, the CLAUDE.md history note and this file.

## Follow-up session (2026-10-08)

- **lab-butler renamed to confetti-butler.** Chosen to show the link to confetti-traffic while keeping
  "butler"; checked free on GitHub, PyPI, npm, Docker Hub and Alpine. `BUTLER_*` env vars,
  `butler.db`, `butler.env` and the `butler/` folder keep their names.
- **Renamed:** install paths (`/opt`, `/var/lib`, `/etc` + `confetti-butler`), OpenRC services
  `confetti-butler` and `confetti-butler-firstboot`, the `/etc/profile.d` login script, the health
  check's default service name, the UI title and header (`CONFETTI BUTLER`), both project skills
  (`confetti-butler-hub-api`, `confetti-butler-dev-run`), the README GIF and the docs.
- **Saved theme and layout carry over:** `static/theme.js` copies the old `lab-butler-theme` and
  `lab-butler-layout` localStorage values to the new keys once.
- **An already deployed VM is not migrated.** It still has `/opt/lab-butler`, `/var/lib/lab-butler`,
  the `lab-butler` service and a `BUTLER_HEALTH_SERVICES` line naming it. Rebuild from the template,
  or move the paths and services by hand and update that line in `butler.env`.
- Earlier entries above still say lab-butler; that was the name then.
- **Not tested:** `build-template.sh` and the OpenRC scripts were only syntax-checked, not run on Alpine.

## Housekeeping

- **Git:** repository initialised 2026-09-27 on `main`, public at
  https://github.com/orneh24/lab-butler. The unpushed history was rewritten before the first push to
  replace real lab IPs, hostnames and a serial with placeholders — don't commit real lab details.
  `butler.db*` and `butler.env` are gitignored (plaintext credentials). `.gitattributes` pins `.sh`, `.initd`, `.py` and
  `requirements.txt` to LF — the machine's system-wide `core.autocrlf=true` would otherwise check
  shell scripts out with CRLF, which breaks them on Alpine (same rule as confetti-traffic).
- Local Python dependencies for development were installed into the ambient environment on this
  machine (`pyyaml`, `netmiko`, `requests`, `pyflakes` for linting, `pillow` for resizing the README GIF —
  `flask`/`waitress`/`jinja2`/`playwright` were already present). The
  Alpine package list `build-template.sh` installs is the authoritative list for a real deployment;
  it has not been cross-checked against the live Alpine package index (see constraint 3 in
  `CLAUDE.md` and the `py3-paramiko`/`netmiko` note in `build-template.sh` itself).
- CodeMirror 5.65.16 and vis-network 9.1.9 are vendored under `butler/static/vendor/` (pinned
  versions, downloaded directly, no CDN dependency at runtime).
