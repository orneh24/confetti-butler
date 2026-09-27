---
name: lab-butler-dev-run
description: Run lab-butler locally on a Windows dev machine — start the server, add the lab CSR1000v, set its SSH credentials, and poll it. Use when asked to start, run, or test the app locally.
origin: lab-butler
---

# Running lab-butler locally

For the full route list see `lab-butler-hub-api`. This skill only covers the local dev loop.

## Start the server

Port 80 and UDP 514 need admin rights on Windows, so use 8080 and 5514. Run in the background:

```sh
cd butler && BUTLER_PORT=8080 BUTLER_SYSLOG_PORT=5514 python serve.py
```

- The database is `butler.db` in the working directory (`BUTLER_DB_PATH` overrides). It is created
  empty on first start. It holds plaintext device credentials, so keep it out of git
  (add `butler.db*` to `.gitignore` when the repo is created).
- Check it is up: `curl http://127.0.0.1:8080/api/health`. `"rc-service not installed"` for every
  service is normal on Windows — those checks look for Alpine's OpenRC.
- **Stopping on Windows:** stopping the background shell leaves `python serve.py` running, still
  holding 8080 and 5514. Find it with `Get-NetTCPConnection -LocalPort 8080 -State Listen` and kill
  that PID before restarting. If the startup log says `[syslog] not listening ... 10048`, an old
  server is still up, and it (not the new code) is answering requests.
- The poller and syslog receiver start with the server. The poller does nothing until a device exists.

## Add and poll a device

1. Add: `POST /api/devices` with `{"mgmt_ip": "...", "vendor": "cisco", "platform": "cisco_ios"}`.
   The new device's key is `mgmt:<ip>` until its first poll finds a serial.
2. Credentials: `PUT /api/devices/<id>/credentials` with `{"username": ..., "password": ...,
   "enable_secret": ...}`. Write the JSON to a temp file and send it with `--data @file`. Passwords
   often contain `!` or `$`, which the shell would otherwise mangle. Delete the file afterwards.
3. Poll now: `POST /api/devices/<id>/poll`. It is synchronous and can take up to about a minute. It
   returns ok/error for each of the five tasks (version, interfaces, lldp, bgp, ospf).
4. Check: `GET /api/devices/<id>` (the key should now be `serial:...`) and
   `GET /api/devices/<id>/interfaces`.

## The lab router

- CSR1000v at `192.0.2.10`, IOS-XE 17.3.8a, hostname `lab-rtr-a`, serial `9SIMLAB0001`.
- Credentials are **not** stored here. Ask the user for them each session.
- LLDP is disabled and there are no BGP/OSPF peers, so those three tasks succeed but return nothing.
  That is expected, not a parser bug.
