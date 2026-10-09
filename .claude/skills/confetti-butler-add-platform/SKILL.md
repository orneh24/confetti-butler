---
name: confetti-butler-add-platform
description: Add a new device platform (for example Junos) to confetti-butler - registry entry, commands, parser, captured-output fixtures, regression check. Use when asked to support another vendor or OS.
origin: confetti-butler
---

# Adding a platform

Constraint 3 is the reason for this order: command names and output are never assumed, they are checked on a real device. EOS and Junos parsers written from documentation were removed on 2026-10-10 for that reason.

1. **Get real output first.** Ask the user for a lab device. Capture the raw output of each command (version, interfaces, lldp, bgp, ospf, config) into `dev/samples/<platform>_<task>.txt`. Replace real hostnames, IPs and secrets with placeholders (see `dev/samples/README.md`).
2. **Registry.** Add one entry to `butler/app/platforms.py` with the same six task names, `config` last. Unknown platforms fall back to IOS, so a typo is silent - spell the key as stored in `devices.platform`.
3. **Parser.** New `butler/app/parsers/<name>.py`, pure regex (no textfsm). It must return the same row shapes as `ios.py`. `parse_running_config` must return `""` unless the config is complete (constraint 14).
4. **Secrets.** Extend `rendering._REDACT_RE` for the platform's secret syntax, and say in CLAUDE.md that only listed syntax is masked.
5. **Drift.** `drift.parse` assumes IOS indentation. Check it on the new config style before claiming drift support.
6. **Regress.** Add a check to `dev/regress.py` that parses the fixtures, and prove it fails when the parser is broken. Update the task-count wording if it changes (R16).
7. **Docs.** CLAUDE.md "Platforms and SNMP", README, ROADMAP status, hub-api skill if routes change.
8. **Validate on the device** with the `lab-validator` agent. Do not mark the platform verified before that.
