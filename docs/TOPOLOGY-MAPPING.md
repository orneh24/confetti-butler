# Topology mapping: design and lessons

Standalone notes from confetti-butler (Flask + SQLite + vis.js). Written so another project can
rebuild the same feature without reading this repo. Code references are to confetti-butler, for
orientation only.

## 1. What it does

It draws a graph of the network from what the devices say about themselves:

- **LLDP** gives physical links (cables): "my port Gi1 sees device X port Gi2".
- **BGP** and **OSPF** give logical links (sessions): "I have a peer at 10.0.0.2, state Established".

The server logs in to each device over SSH, runs read-only `show` commands, parses the text with
regex, stores rows, and serves one JSON graph that the browser draws. Nothing is configured on
the device. Devices need no agent.

Flow: `poll -> parse -> two tables -> /api/topology -> vis.js`.

## 2. Data collected

| Layer | IOS / IOS-XE command | What it tells you |
|---|---|---|
| LLDP | `show lldp neighbors detail` | local port, neighbor chassis id, system name, port id, advertised management IP |
| LLDP (old IOS only) | `show lldp neighbors` | the table that maps a neighbor to a local port (see 6.3) |
| BGP | `show bgp summary` | neighbor IP, remote AS, state, prefixes received, uptime |
| OSPF | `show ip ospf neighbor` | neighbor address, state, local interface |

Verified on real IOS-XE 17.3 and 15.4 routers. Do not trust a command name from documentation:
check it on a real device first (see 6.1).

## 3. Storage (two tables are enough)

```sql
lldp_neighbors (
  id, device_id -> devices ON DELETE CASCADE,
  local_if, remote_chassis, remote_sysname, remote_port, remote_mgmt_ip,
  remote_device_id,          -- NULL = neighbor not matched to a known device
  last_seen,
  UNIQUE(device_id, local_if, remote_port))

adjacencies (
  id, device_id -> devices ON DELETE CASCADE,
  proto,                     -- 'bgp' | 'ospf'
  peer_ip, peer_device_id,   -- NULL = peer not matched to a known device
  remote_as, area, state, uptime, prefixes, local_if, last_seen,
  UNIQUE(device_id, proto, peer_ip))
```

`remote_device_id` and `peer_device_id` are plain integers, not foreign keys, on purpose: they may
point at a device that is later merged or deleted. When two device records are merged, re-point
these columns at the survivor; when one is deleted, set them to NULL (the neighbor then becomes a
ghost again).

Upsert each row with `ON CONFLICT(...) DO UPDATE`, stamping `last_seen = now` (the poll's own
timestamp, one value for the whole poll).

## 4. Collection rules

1. **One SSH session per device per poll**, several tasks on it. Commit each task on its own, so an
   LLDP timeout never throws away interface data already collected.
2. **Remove what the poll did not see.** After upserting, run
   `DELETE ... WHERE device_id = ? AND last_seen <> now` (adjacencies also `AND proto = ?`).
   Without this a removed cable or peer stays on the map forever.
3. **Empty is a real answer.** `% LLDP is not enabled` or "no BGP" must parse to an empty list and
   still count as a successful task (delete the old rows). Only a transport failure (timeout, bad
   login) is a failed task, and it must not delete anything.
4. **A down peer is still listed** (`Active`, `Idle`) so it stays on the map. Only a peer removed
   from the config disappears. Keep `Idle (Admin)` as the state text: the "(Admin)" part says the
   peer was shut down on purpose (a regex that takes only the first word loses it).
5. **Do not map the same cable twice** (see 5.2) and do not drop unresolved neighbors (see 5.1).
6. Skip devices you cannot log in to (for example test hosts that are not routers). Run polls in a
   thread pool with a per-device lock so a hung device is never queued twice; back off on failure.
7. **First poll emits no change events.** If you also log events (peer down, neighbor lost), compare
   old and new rows in the same transaction, and stay silent until the task has succeeded once;
   otherwise adding a device creates one event per neighbor.

## 5. Building the graph (`GET /api/topology`)

Returns `{nodes: [...], edges: [...]}` in the shape vis.js wants. Optional `?layer=lldp|bgp|ospf`
limits the edges to one source; the default overlays all three on the same nodes.

**Nodes:** one per known device: `id "dev:<id>"`, `label` = hostname, `group` = role,
`reachable` (ping state, optional), `ghost: false`.

**Edges:** `{from, to, layer, label, up?}`.
- LLDP label: `"<local port> - <remote port>"`.
- BGP/OSPF label: `"bgp Established"`; `up = (state == Established)` for BGP, `(state == FULL)` for OSPF.

### 5.1 Ghost nodes

A neighbor or peer that cannot be matched to a known device is **not dropped**. It becomes a ghost
node: `id "ghost:lldp:<sysname|chassis>"` or `"ghost:peer:<ip>"`, `ghost: true`, drawn as a dashed
circle. An edge to nowhere looks like a bug; a ghost reads as "something is out there". Use one
shared ghost id per peer IP across BGP and OSPF, since the same router peering both ways is one box.

### 5.2 Draw each cable once

When both ends are known devices, both report the same cable, so you get two LLDP rows. Key a
device-to-device link as `frozenset({(device_id, local_if), (remote_device_id, remote_port)})` and
skip the second one. If the two sides spell a port differently (`Gi1` vs `GigabitEthernet1`) the
keys differ and both edges are kept. That is deliberate: a duplicate line is safer than hiding a
real parallel link. Do not de-duplicate ghost links.

## 6. Resolving "who is that neighbor?" (the hard part)

The map only joins up when a neighbor row is matched to a device record. How:

- **LLDP: match the management IP the neighbor advertises** (the `IP:` line under "Management
  Addresses") against your table of known management IPs. Keep a table of *every identifier ever
  seen* for a device (an alias table: kind + value -> device), not only its current IP, so a device
  that was re-addressed still matches.
- **BGP/OSPF: match `peer_ip` the same way**, but expect misses. A peer IP is usually a loopback
  or a point-to-point link address, not the management IP, so most peers resolve only if you also
  record each device's interface IPs as aliases. NULL (ghost) is a normal result, not a bug.
- Do not use the LLDP chassis id for matching unless you can learn your own chassis id (see 6.1).

### 6.1 Verify every command on real hardware

Found the hard way on IOS-XE 17.3:
- `show lldp local-info` is invalid syntax.
- `show lldp entry local` is **not** "show my own LLDP data": it looks up a neighbor named "local"
  and returns nothing. No command shows a device its own LLDP chassis id (it is the base MAC and
  appears in no `show version` / `inventory` output). That is why matching is by management IP.

### 6.2 LLDP detail output quirks

Blocks are separated by a line of 10+ dashes. Use one small regex per field
(`^Chassis id:`, `^Port id:`, `^System Name:`, `^Local Intf:`, `IP:\s*a.b.c.d`) instead of one big
pattern; a missing field then gives `None` and the rest still parses. A neighbor with no system
name is normal: fall back to chassis id for the label.

### 6.3 Older IOS omits `Local Intf:` in the detail output

IOS-XE 3.x / 15.4 prints no local interface in `show lldp neighbors detail`. Fix: also run
`show lldp neighbors` (the table) and fill the gap. The table's first column is a fixed 20
characters and can run straight into the next one (`LAB-RTR-C.lab.locGi1`), so split by the
**header's column position**, not by whitespace. Match a detail row to a table row when the Port ID
is equal and the table's Device ID is a prefix of the system name (it may be truncated) or equals
the chassis id. Require exactly one match; drop rows that still have no local interface (it is part
of the unique key).

## 7. Parsing notes (regex only)

Regex was chosen over TextFSM/ntc-templates to avoid one more package to install on a small Linux
image. Fixtures: save real, sanitized output next to the tests (`dev/samples/`) and test parsers
against them.

- BGP summary row: `neighbor  version  AS  msgrcvd  msgsent  tblver  inq  outq  up/down  State/PfxRcd`.
  If the last column is a number the session is Established and the number is the prefix count;
  otherwise it is the state word (`Idle`, `Active`, `Idle (Admin)`).
- OSPF row: `neighbor-id  pri  state  dead-time  address  interface`. State looks like `FULL/BDR`;
  keep only the part before `/`. Use the **address** column as the peer IP, not the router id.
- Compute interface networks yourself (a few lines of integer maths) if you also want IP maths.

## 8. Optional extras that fit the same data

- **Crawl:** an LLDP neighbor that advertises a management IP nobody owns can be added as a new
  device (inventory only, polling **off** until an operator gives it credentials), then the ghost
  becomes a real node and its own neighbors appear on its first poll. Offer it as a list with an
  "Add" button first; auto-adding should be an opt-in setting. Add it through the same identity
  function as every other discovery path, so duplicates are caught.
- **State colouring:** red outline on a node that stops answering ping (an ICMP checker thread
  sets `devices.reachable`); BGP/OSPF edges that are not Established/FULL drawn red and dashed.
- **Events:** peer state change, peer added/removed, neighbor added/removed, from the same apply
  step that already holds the old rows.

## 9. The browser side (vis-network)

- Vendor the library (no CDN) and keep vanilla JS with no build step.
- vis.js draws on a canvas, so it needs literal colour strings, not CSS variables. Read the
  current theme's colours into JS each time the graph is built, and rebuild on a theme change.
- Fetch `/api/topology` every ~20 s. **Update the data sets in place** (`DataSet.update` plus
  `remove` of ids that disappeared) instead of creating a new `vis.Network`; rebuilding resets
  layout and zoom on every refresh. Give edges a stable id (`from|to|layer|label`) so updates match.
- Nodes: boxes for devices, dots for ghosts. Edge colour per layer (LLDP, BGP, OSPF) plus the red
  dashed "down" style. Filter buttons All / LLDP / BGP / OSPF pass `?layer=`.
- Physics: `barnesHut` with `gravitationalConstant -4000`, `springLength 140` worked for a lab-size
  graph. Double-click a device node to open its detail page (ghosts have no page).
- Empty state: show a plain message ("no devices yet") and destroy the network object.
- Include a legend: node colours, ghost meaning, one swatch per link layer, the red dashed style.

## 10. Pitfalls (each one happened or nearly did)

1. Command names assumed from docs but invalid on the real OS (6.1).
2. A silent merge of two devices because an IP was reused. Prefer a visible conflict to a guess.
3. Hiding unresolved neighbors, which makes the map look incomplete instead of "unknown".
4. Deleting rows on a *failed* poll (timeout) — only a successful poll may delete.
5. Treating "LLDP not enabled" as a failure, which backs the device off and hides its other data.
6. Drawing every cable twice, or hiding a real second cable by de-duplicating too eagerly.
7. Timestamps: store one format (`YYYY-MM-DD HH:MM:SS`) and compare strings only in that format;
   mixing it with ISO-8601 (`T`) makes every same-day row look newer.
8. Giving the poller devices that are not routers (test hosts), which fail every `show` command.
9. A rebuilt vis.js network on each refresh (layout jumps).

## 11. Test checklist for a new build

- Two devices that see each other: exactly one LLDP edge.
- A neighbor with an unknown management IP: one ghost node, dashed.
- A neighbor with no advertised IP: still a ghost, labelled by system name or chassis id.
- BGP peer shut down: stays on the map as `Idle (Admin)`, drawn red dashed. Peer removed from the
  config: disappears after the next successful poll.
- LLDP turned off on a device: its neighbors disappear, other tasks stay OK.
- Old IOS (15.4) output without `Local Intf:`: local interface filled from the table.
- Timeout during the LLDP task: nothing deleted, other tasks' data kept.
- First poll of a new device: no events.
- Merge two device records: edges re-point to the survivor; delete one: edges become ghosts.
- Refresh while zoomed in: view does not reset.

## 12. Not done / limits

- IOS and IOS-XE only (and SNMP for basic inventory). Other vendors need their own commands and
  parsers, each verified on a real device first.
- LLDP only, no CDP. No L2 (MAC table / VLAN / STP) view.
- BGP/OSPF edges point at the peer address, so many show as ghosts until interface IPs are
  recorded as aliases.
- Node positions are not saved; the layout is recomputed in the browser.
