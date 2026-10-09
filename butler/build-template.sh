#!/bin/sh
# build-template.sh — Build the confetti-butler golden template on a fresh Alpine
# install. Run as root after booting the Alpine ISO and completing
# setup-alpine. Mirrors confetti-traffic/hub/confettictl-build-template.sh closely — same
# infra (chrony, lldpd, open-vm-tools, dropbear, OpenRC, guestinfo
# first-boot), same structure. See that file's comments for the reasoning
# behind each infra choice; this file only calls out what's different here.
#
# This script:
#   1. Enables the community repo
#   2. Installs all required packages (Python, Flask, Netmiko's deps, etc.)
#   3. Copies the app into place
#   4. Creates an OpenRC service
#   5. Cleans up for template conversion
#
# Usage:
#   1. SCP the entire butler/ directory to the Alpine VM
#   2. Run: sh /root/butler/build-template.sh
#   3. Shutdown and convert to template in vCenter
#
# Updating an already-deployed VM (code only, with backup and rollback):
#   butler-update.sh <butler-dir | butler.tar.gz>     (see scripts/butler-update.sh)
#
# After cloning:
#   1. Set a static IP (or a DHCP reservation)
#   2. Boot — the dashboard starts automatically on port 80

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="/opt/confetti-butler"
DB_DIR="/var/lib/confetti-butler"
LAB_ROOT_PASSWORD="${LAB_ROOT_PASSWORD:-lab123}"

# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
log() {
    printf '[build-template] %s\n' "$1"
}

die() {
    printf '[build-template] FATAL: %s\n' "$1" >&2
    exit 1
}

# -------------------------------------------------------------------
# Sanity checks
# -------------------------------------------------------------------
[ "$(id -u)" -eq 0 ] || die "Must run as root"

log "=== confetti-butler template builder ==="

# -------------------------------------------------------------------
# 1. Enable community repository
# -------------------------------------------------------------------
log "Enabling community repository"
ALPINE_VERSION=$(cat /etc/alpine-release | cut -d. -f1,2)
if ! grep -q "^[^#].*community" /etc/apk/repositories; then
    echo "http://dl-cdn.alpinelinux.org/alpine/v${ALPINE_VERSION}/community" >> /etc/apk/repositories
    log "Community repo added"
else
    log "Community repo already enabled"
fi

# -------------------------------------------------------------------
# 2. Install packages
#
# py3-paramiko, py3-yaml and py3-requests are real Alpine community
# packages Netmiko/the collectors need; py3-jinja2 too. netmiko itself is
# NOT in Alpine's index (checked against the same discipline as confetti-traffic
# CLAUDE.md constraint 14 — verify, don't assume), so it's pip-installed
# below alongside waitress, same fallback pattern as the hub's build script.
# net-snmp-tools provides snmpwalk/snmpget for SNMP collection — shelling
# out to it rather than a Python SNMP library, see the plan's SNMP decision.
# -------------------------------------------------------------------
log "Updating package index"
apk update

log "Installing packages"
apk add --no-cache \
    python3 \
    py3-pip \
    py3-flask \
    py3-paramiko \
    py3-yaml \
    py3-requests \
    py3-jinja2 \
    sqlite \
    curl \
    net-snmp-tools \
    open-vm-tools \
    chrony \
    lldpd

log "Packages installed"

rc-update add open-vm-tools default

# confetti-butler stamps every poll and syslog message with its own receipt
# time, and the syslog page pins correlation windows around it — same
# reasoning as the hub's clock being the mesh reference.
rc-update add chronyd default

# LLDP neighbor discovery — always-on infrastructure, not a poll target
# itself (devices are polled over SSH; lldpd here is for troubleshooting
# confetti-butler's own VM placement, same role it plays on confetti-traffic's VMs).
rc-update add lldpd default

# -------------------------------------------------------------------
# 2b. Set default lab credentials
# -------------------------------------------------------------------
log "Setting root password"
echo "root:${LAB_ROOT_PASSWORD}" | chpasswd

log "Credentials: root / ${LAB_ROOT_PASSWORD}"

# -------------------------------------------------------------------
# 3. Create directories
# -------------------------------------------------------------------
log "Creating directories"
mkdir -p "$INSTALL_DIR" "$DB_DIR"

# -------------------------------------------------------------------
# 4. Copy application
# -------------------------------------------------------------------
log "Installing application to $INSTALL_DIR"

cp -r "${SCRIPT_DIR}/app" "$INSTALL_DIR/"
cp -r "${SCRIPT_DIR}/templates" "$INSTALL_DIR/"

# static/ carries the vendored CodeMirror and vis-network bundles — real
# content, not empty like the hub's static/, so this is a real copy, not
# the hub's mkdir-then-maybe-copy.
mkdir -p "$INSTALL_DIR/static"
cp -r "${SCRIPT_DIR}/static/"* "$INSTALL_DIR/static/"

mkdir -p "$INSTALL_DIR/seed"
if [ -d "${SCRIPT_DIR}/seed" ]; then
    cp -r "${SCRIPT_DIR}/seed/"* "$INSTALL_DIR/seed/" 2>/dev/null || true
fi

cp -f "${SCRIPT_DIR}/requirements.txt" "$INSTALL_DIR/"
cp -f "${SCRIPT_DIR}/serve.py" "$INSTALL_DIR/"
cp -f "${SCRIPT_DIR}/run.sh" "$INSTALL_DIR/"
cp -f "${SCRIPT_DIR}/scripts/butler-setup.sh" "$INSTALL_DIR/"
cp -f "${SCRIPT_DIR}/scripts/butler-update.sh" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/run.sh" "$INSTALL_DIR/serve.py" "$INSTALL_DIR/butler-setup.sh"     "$INSTALL_DIR/butler-update.sh"
ln -sf "$INSTALL_DIR/butler-setup.sh" /usr/local/bin/butler-setup.sh
ln -sf "$INSTALL_DIR/butler-update.sh" /usr/local/bin/butler-update.sh

# -------------------------------------------------------------------
# 5. Install Python dependencies not covered by apk
# -------------------------------------------------------------------
log "Checking Python dependencies"

# Versions come from requirements.txt, so a rebuild installs what was tested.
# Only the packages apk lacks are installed here; the py3-* ones above stay
# at whatever version this Alpine release ships.
pin() {
    spec=$(grep -i "^$1==" "${SCRIPT_DIR}/requirements.txt" || true)
    [ -n "$spec" ] || die "no pinned $1 in requirements.txt"
    printf '%s' "$spec"
}

if ! python3 -c "import flask" 2>/dev/null; then
    log "Installing Flask via pip"
    pip3 install --break-system-packages "$(pin flask)"
fi

if ! python3 -c "import netmiko" 2>/dev/null; then
    log "Installing Netmiko via pip (not an Alpine package)"
    pip3 install --break-system-packages "$(pin netmiko)"
fi

# Waitress serves the app instead of Flask's development server, which is
# single-threaded: the poller's own requests, a device's config pull, and
# the dashboard would all queue behind each other otherwise.
log "Installing waitress WSGI server"
if ! python3 -c "import waitress" 2>/dev/null; then
    apk add --no-cache py3-waitress 2>/dev/null ||         pip3 install --break-system-packages "$(pin waitress)"
fi

# -------------------------------------------------------------------
# 6. Create configuration file
# -------------------------------------------------------------------
log "Creating configuration"
cat > "$INSTALL_DIR/butler.env" <<'ENVEOF'
# confetti-butler environment configuration.
# Edit these values after cloning if needed.

BUTLER_DB_PATH=/var/lib/confetti-butler/butler.db
BUTLER_PORT=80

# --- Syslog receiver -------------------------------------------------
BUTLER_SYSLOG_ENABLED=true
BUTLER_SYSLOG_BIND=0.0.0.0
BUTLER_SYSLOG_PORT=514
BUTLER_SYSLOG_MAX_ROWS=300000

# Three writers share this database (Flask, the syslog listener thread,
# the poller's thread pool) — one more than confetti-traffic's hub. Without this,
# a burst on any one of them can fail another with "database is locked".
BUTLER_BUSY_TIMEOUT_MS=5000

# --- Poller ------------------------------------------------------------
BUTLER_POLL_TICK_S=15
BUTLER_POLL_WORKERS=8
BUTLER_POLL_DEFAULT_INTERVAL_S=300
BUTLER_POLL_MAX_BACKOFF_S=3600
BUTLER_POLL_HISTORY_RETENTION_HOURS=168

# --- Retention ---------------------------------------------------------
BUTLER_EVENT_RETENTION_DAYS=30
# Running-config versions kept per device; 0 keeps all.
BUTLER_CONFIG_VERSIONS_KEEP=0
# Nightly database backups kept (/var/lib/confetti-butler/backups).
BUTLER_BACKUP_KEEP=7
# Interface error-counter samples kept, in days.
BUTLER_STATS_RETENTION_DAYS=7

# --- ICMP reachability ---------------------------------------------------
BUTLER_PING_ENABLED=true
BUTLER_PING_INTERVAL_S=30
# Failed pings in a row before a device is marked down.
BUTLER_PING_FAILS_TO_DOWN=2

# --- Discovery (all off until configured) --------------------------------
# Seconds between scheduled runs of the sources below; 0 = never.
BUTLER_DISCOVERY_INTERVAL_S=0
# BUTLER_DISCOVERY_CONFETTI_URL=http://10.0.0.100
# BUTLER_DISCOVERY_SWEEP_CIDRS=10.0.1.0/24,10.0.2.0/24
# BUTLER_DISCOVERY_SEEDFILE=/opt/confetti-butler/seed/devices.yaml
# Add LLDP neighbors with an unknown management IP as inventory-only devices.
BUTLER_LLDP_AUTO_ADOPT=false

# --- Alerting (rules are created on the Alerts page) ----------------------
BUTLER_ALERTS_ENABLED=true
# Events older than this are never alerted on (no replay after a restart).
BUTLER_ALERT_MAX_AGE_S=600
# Needed only for rules with a mail: target.
# BUTLER_SMTP_HOST=smtp.example.net
# BUTLER_SMTP_PORT=25
# BUTLER_SMTP_USER=
# BUTLER_SMTP_PASSWORD=
# BUTLER_SMTP_FROM=confetti-butler@example.net
# BUTLER_SMTP_TLS=false

# --- Default device credentials -----------------------------------------
# Shared lab defaults; override per device via PUT /api/devices/<id>/credentials.
# Never put a real password here outside an isolated lab.
BUTLER_SSH_USERNAME=admin
BUTLER_SSH_PASSWORD=
BUTLER_SSH_SECRET=
BUTLER_SNMP_COMMUNITY=public
BUTLER_SNMP_VERSION=2c

# --- Self-health (/api/health) ------------------------------------------
BUTLER_HEALTH_SERVICES=confetti-butler,chronyd,dropbear,open-vm-tools,lldpd
BUTLER_HEALTH_SERVICE_TIMEOUT_S=3
ENVEOF
# Holds the default SSH password (and anything guestinfo adds): root only.
chmod 600 "$INSTALL_DIR/butler.env"

# -------------------------------------------------------------------
# 7. Create OpenRC init script
# -------------------------------------------------------------------
log "Creating OpenRC init script"
cp -f "${SCRIPT_DIR}/services/confetti-butler.initd" /etc/init.d/confetti-butler

chmod +x /etc/init.d/confetti-butler

# Nightly database backup. run-parts (Alpine's crond runs /etc/periodic/daily
# at 02:00) skips file names containing a dot, hence no .sh.
mkdir -p /etc/periodic/daily
cp -f "${SCRIPT_DIR}/services/butler-backup.sh" /etc/periodic/daily/butler-backup
chmod +x /etc/periodic/daily/butler-backup

# First-boot autoconfiguration from guestinfo — same pattern as the hub's
# confettid-hub-firstboot: stands down without both required keys rather
# than blocking boot on a prompt nobody can answer from an OpenRC start().
cp -f "${SCRIPT_DIR}/services/firstboot.initd" /etc/init.d/confetti-butler-firstboot
chmod +x /etc/init.d/confetti-butler-firstboot

# Invite an unconfigured VM to run butler-setup.sh at first interactive
# login, where a real tty is guaranteed.
cp -f "${SCRIPT_DIR}/services/login-setup.sh" /etc/profile.d/confetti-butler-setup.sh

# -------------------------------------------------------------------
# 8. Enable services
# -------------------------------------------------------------------
log "Enabling services"

rc-update add confetti-butler default
rc-update add confetti-butler-firstboot default
rc-update add crond default

apk add --no-cache dropbear
rc-update add dropbear default

log "Services enabled"

# -------------------------------------------------------------------
# 9. First-boot instructions
# -------------------------------------------------------------------
log "Creating first-boot instructions"
cat > /etc/motd <<'MOTDEOF'

  ┌───────────────────────────────────────────────┐
  │         confetti-butler VM                         │
  │                                               │
  │  Dashboard: http://<this-vm-ip>/              │
  │  Config:    /opt/confetti-butler/butler.env        │
  │  DB:        /var/lib/confetti-butler/butler.db     │
  │  Logs:      rc-service confetti-butler status      │
  │                                               │
  │  Not configured yet? Log in and run:          │
  │    butler-setup.sh                            │
  │  (runs automatically at first login if the    │
  │   static IP hasn't been set)                  │
  │                                               │
  │  If IP needs changing later:                  │
  │    set-static-ip <ip/cidr> <gateway>          │
  │    rc-service networking restart              │
  └───────────────────────────────────────────────┘

MOTDEOF

# -------------------------------------------------------------------
# 10. Static IP configuration helper (identical to the hub's)
# -------------------------------------------------------------------
log "Creating static IP helper script"
cat > /usr/local/bin/set-static-ip <<'SIPEOF'
#!/bin/sh
# Helper to configure a static IP on this VM.
# Usage: set-static-ip <ip/cidr> <gateway>
# Example: set-static-ip 10.0.0.100/24 10.0.0.1

set -eu

if [ $# -lt 2 ]; then
    echo "Usage: set-static-ip <ip/cidr> <gateway>"
    echo "Example: set-static-ip 10.0.0.100/24 10.0.0.1"
    exit 1
fi

IP_CIDR="$1"
GATEWAY="$2"

IFACE=$(ip -o link show | awk -F': ' '!/lo/{print $2; exit}')

cat > /etc/network/interfaces <<EOF
auto lo
iface lo inet loopback

auto ${IFACE}
iface ${IFACE} inet static
    address ${IP_CIDR}
    gateway ${GATEWAY}
EOF

echo "Static IP configured on ${IFACE}: ${IP_CIDR} via ${GATEWAY}"
echo "Restart networking: rc-service networking restart"
SIPEOF

chmod +x /usr/local/bin/set-static-ip

# -------------------------------------------------------------------
# 11. Clean up for template conversion
# -------------------------------------------------------------------
log "Cleaning up for template conversion"

rm -f /etc/dropbear/dropbear_*_host_key
: > /etc/machine-id 2>/dev/null || true
find /var/log -type f -exec truncate -s 0 {} \; 2>/dev/null || true
: > /root/.ash_history 2>/dev/null || true
apk cache clean 2>/dev/null || true
rm -rf /var/cache/apk/*

# Remove the DB and its backups if they were created during testing.
rm -f "$DB_DIR/butler.db"
rm -rf "$DB_DIR/backups"

# Remove any setup stamp left from build-time testing, or every clone would
# consider itself already configured and skip butler-setup.sh.
rm -f /etc/confetti-butler/.setup-done /etc/confetti-butler/.env-done

rm -f "${SCRIPT_DIR}/build-template.sh"

log "Zeroing free space for thin provisioning (this may take a minute)..."
dd if=/dev/zero of=/zero.fill bs=1M 2>/dev/null || true
rm -f /zero.fill
sync

log ""
log "=== Template build complete ==="
log ""
log "Next steps:"
log "  1. Shutdown:   poweroff"
log "  2. In vCenter: right-click VM -> Template -> Convert to Template"
log ""
log "To deploy a clone (guestinfo, zero-touch):"
log "  1. Clone from template"
log "  2. Set these guestinfo keys on the clone in vCenter, then boot:"
log "     guestinfo.butler.ip       10.0.0.101/24"
log "     guestinfo.butler.gateway  10.0.0.1"
log "     optional: guestinfo.butler.ssh_username / ssh_password / ssh_secret /"
log "               poll_interval_s  (written into butler.env on first boot)"
log "  3. The dashboard starts automatically on port 80"
log ""
log "To deploy a clone (manual):"
log "  1. Clone from template, boot, log in (root / ${LAB_ROOT_PASSWORD})"
log "  2. butler-setup.sh runs automatically at login and prompts for the"
log "     static IP -- or run it by hand any time"
log "  3. The dashboard starts automatically on port 80"
