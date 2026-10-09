#!/bin/sh
# migrate-from-lab-butler.sh — move a VM deployed before the 2026-10-08 rename
# (lab-butler -> confetti-butler) onto the new paths and service names.
#
# Usage: migrate-from-lab-butler.sh <butler-dir>
#   <butler-dir> is a copy of this repo's butler/ folder; the new OpenRC and
#   login scripts are installed from it. Run butler-update.sh afterwards to
#   bring the application code itself up to date.
#
# Moves, never copies or deletes data: /opt, /var/lib and /etc/lab-butler
# become the confetti-butler paths, the database and its stamps come along.
# Safe to re-run; on a VM with nothing to migrate it does nothing. It stops
# without changing anything if a confetti-butler path already exists next to a
# lab-butler one (two installs — that needs a human).
#
# Not changed: /etc/motd still says lab-butler; edit it by hand if you care.

set -eu

OLD="lab-butler"
NEW="confetti-butler"

log() { printf '[migrate] %s\n' "$1"; }
die() { printf '[migrate] FATAL: %s\n' "$1" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Must run as root"
[ $# -eq 1 ] || die "Usage: migrate-from-lab-butler.sh <butler-dir>"
SRC="${1%/}"

if [ ! -d "/opt/${OLD}" ] && [ ! -d "/var/lib/${OLD}" ] && [ ! -f "/etc/init.d/${OLD}" ]; then
    log "No ${OLD} install found — nothing to migrate"
    exit 0
fi

for _f in services/confetti-butler.initd services/firstboot.initd services/login-setup.sh; do
    [ -f "${SRC}/${_f}" ] || die "${SRC} does not look like butler/: missing ${_f}"
done

# Refuse a half-and-half state rather than guess which side is real.
for _p in /opt /var/lib /etc; do
    if [ -e "${_p}/${OLD}" ] && [ -e "${_p}/${NEW}" ]; then
        die "${_p}/${OLD} and ${_p}/${NEW} both exist — resolve by hand"
    fi
done

# -------------------------------------------------------------------
# 1. Stop and unregister the old services
# -------------------------------------------------------------------
for _svc in "$OLD" "${OLD}-firstboot"; do
    if [ -f "/etc/init.d/${_svc}" ]; then
        rc-service "$_svc" stop >/dev/null 2>&1 || true
        rc-update del "$_svc" default >/dev/null 2>&1 || true
    fi
done

# -------------------------------------------------------------------
# 2. Move the data and install directories
# -------------------------------------------------------------------
for _p in /opt /var/lib /etc; do
    if [ -d "${_p}/${OLD}" ]; then
        log "Moving ${_p}/${OLD} -> ${_p}/${NEW}"
        mv "${_p}/${OLD}" "${_p}/${NEW}"
    fi
done
# The old install dir may have left a .prev next to it from butler-update.sh
[ ! -d "/opt/${OLD}.prev" ] || mv "/opt/${OLD}.prev" "/opt/${NEW}.prev"

# -------------------------------------------------------------------
# 3. Point butler.env at the new names
# -------------------------------------------------------------------
ENV_FILE="/opt/${NEW}/butler.env"
if [ -f "$ENV_FILE" ]; then
    log "Updating paths and service names in butler.env"
    sed -i "/^BUTLER_DB_PATH=/s/${OLD}/${NEW}/g; /^BUTLER_HEALTH_SERVICES=/s/${OLD}/${NEW}/g" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
fi

# -------------------------------------------------------------------
# 4. New OpenRC services and login hook
# -------------------------------------------------------------------
log "Installing the ${NEW} services"
rm -f "/etc/init.d/${OLD}" "/etc/init.d/${OLD}-firstboot" "/etc/profile.d/${OLD}-setup.sh"
cp -f "${SRC}/services/confetti-butler.initd" "/etc/init.d/${NEW}"
cp -f "${SRC}/services/firstboot.initd" "/etc/init.d/${NEW}-firstboot"
cp -f "${SRC}/services/login-setup.sh" "/etc/profile.d/${NEW}-setup.sh"
chmod +x "/etc/init.d/${NEW}" "/etc/init.d/${NEW}-firstboot"
rc-update add "$NEW" default >/dev/null
rc-update add "${NEW}-firstboot" default >/dev/null

# Helper symlinks in /usr/local/bin pointed into /opt/lab-butler.
for _s in butler-setup.sh butler-update.sh; do
    if [ -e "/opt/${NEW}/${_s}" ]; then
        ln -sf "/opt/${NEW}/${_s}" "/usr/local/bin/${_s}"
    fi
done

# -------------------------------------------------------------------
# 5. Start and check
# -------------------------------------------------------------------
log "Starting ${NEW}"
rc-service "$NEW" start >/dev/null 2>&1 || true
_port=$(sed -n 's/^BUTLER_PORT=//p' "$ENV_FILE" 2>/dev/null | tail -1)
_i=0
while [ "$_i" -lt 30 ]; do
    if curl -fs -m 3 -o /dev/null "http://127.0.0.1:${_port:-80}/api/health"; then
        log "Migrated and healthy. Now run: butler-update.sh <butler-dir> to update the code."
        exit 0
    fi
    sleep 1
    _i=$((_i + 1))
done
die "${NEW} did not come up — check /var/log/confetti-butler.log (old data is under /opt/${NEW} and /var/lib/${NEW})"
