---
name: confetti-butler-lab-validation
description: Procedure for validating a confetti-butler feature on the real lab routers via the lab VM, with cleanup and a HANDOFF record. Use when the user says validate, verify on real hardware, or test on the lab.
origin: confetti-butler
---

# Real-hardware validation

The user prefers real routers over mocks. Real addresses are in the auto-memory file `reference_lab_routers.md`; never commit them.

1. **Confirm** the VM and routers are on. Credentials come from the user in chat only.
2. **Update the VM:** copy `butler/` over and run `butler-update.sh -y <dir>` (without `-y` it asks first and a closed stdin cancels). Full path `/usr/local/bin/...` is safest (a non-login SSH session may not have it in PATH). The SSH server is OpenSSH (`sshd`) since 2026-10-14, so `scp`/`sftp` work; a VM still on Dropbear is switched by its first `--update`. After a fresh build the SSH host keys are gone until reboot. Snapshot the VM before the first update that switches the SSH server.
3. **Add the devices** through the API, set credentials per device, wait for a poll, and read results from the API.
4. **Compare with the router itself** (`show` output over SSH), not only with butler's own view.
5. **Change the router only with a revert ready** (a loopback or description line is enough). Always undo it and confirm butler sees the revert.
6. **Receivers for webhooks/syslog** are throwaway listeners on the VM; kill them after. Do not use `pkill -f` with a pattern that matches your own shell.
7. **Clean up:** delete rules, templates, devices, credentials; `VACUUM`; grep the DB and the VM for credential text.
8. **Fix real bugs found** in the repo, add the regress check (skill `constraint-regression`), and run `python dev/regress.py`.
9. **Record** a dated section in `docs/HANDOFF.md` with placeholders only, then run the `leak-scanner` agent.
10. Say plainly what was not verified.
