---
name: confetti-butler-release
description: Checklist to run before handing back or committing confetti-butler changes - regression gate, docs drift, leak scan, commit and push. Use when finishing a change or when asked to commit and push.
origin: confetti-butler
---

# Before handing back or committing

1. `python dev/regress.py` (or the `regression-gate` agent). Must be CLEAR; say so if CLEAR WITH GAPS.
2. Code change: if it fixes a silent bug, add the constraint, site comment and check (skill `constraint-regression`).
3. Docs: update CLAUDE.md, README, the hub-api skill and HANDOFF for what changed, then run the `docs-drift-checker` agent. Touched the confetti-traffic contract, themes or syslog receiver? Also run `confetti-interop-checker`.
4. UI change: re-record `docs/img/` with `scripts/capture_readme.py`.
5. Run the `leak-scanner` agent.
6. Commit and push **only when the user asks**. Message ends with the attribution line from the session, and names new check ids.
7. Report in short, simple English. No large code in the reply.
