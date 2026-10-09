---
name: confetti-butler-lab-validation
description: Procedure for validating a confetti-butler feature on the real lab routers via the lab VM, with cleanup and a HANDOFF record. Use when the user says validate, verify on real hardware, or test on the lab.
origin: confetti-butler
---

# Real-hardware validation

The user prefers real routers over mocks. Real addresses are in the auto-memory file `reference_lab_routers.md`; never commit them.

1. **Confirm** the VM and routers are on. Credentials come from the user in chat only.
2. **Update the VM:** copy `butler/` over and run `butler-update.sh <dir>`. Full path `/usr/local/bin/...` (non-login SSH has no `/usr/local/bin` in PATH). After a fresh build the dropbear host keys are gone until reboot.
3. **Add the devices** through the API, set credentials per device, wait for a poll, and read results from the API.
4. **Compare with the router itself** (`show` output over SSH), not only with butler's own view.
5. **Change the router only with a revert ready** (a loopback or description line is enough). Always undo it and confirm butler sees the revert.
6. **Receivers for webhooks/syslog** are throwaway listeners on the VM; kill them after. Do not use `pkill -f` with a pattern that matches your own shell.
7. **Clean up:** delete rules, templates, devices, credentials; `VACUUM`; grep the DB and the VM for credential text.
8. **Fix real bugs found** in the repo, add the regress check (skill `constraint-regression`), and run `python dev/regress.py`.
9. **Record** a dated section in `docs/HANDOFF.md` with placeholders only, then run the `leak-scanner` agent.
10. Say plainly what was not verified.
