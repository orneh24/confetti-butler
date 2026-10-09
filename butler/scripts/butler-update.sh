#!/bin/sh
# butler-update.sh — update a running confetti-butler VM in place, with rollback.
#
# Usage:
#   butler-update.sh <butler-dir | butler.tar.gz>   update from a copy of butler/
#   butler-update.sh --rollback                     go back to the previous version
#
# What it does: checks the new code imports cleanly (before touching anything),
# backs up the database, swaps /opt/confetti-butler, restarts the service and
# waits for /api/health. If the new version does not come up healthy it swaps
# the old one back by itself. The previous version stays in
# /opt/confetti-butler.prev for a manual --rollback.
#
# What it does NOT do: update apk or pip packages (re-run build-template.sh for
# that), change the OpenRC service or first-boot scripts, or touch butler.env
# (it is carried over). It warns when any of these look out of date.
#
# The database is not restored on rollback: new versions only add tables and
# columns, which the old code ignores. The pre-update backup path is printed
# in case it is ever needed.

set -eu

INSTALL_DIR="/opt/confetti-butler"
PREV_DIR="${INSTALL_DIR}.prev"
NEW_DIR="${INSTALL_DIR}.new"
DB_DIR="/var/lib/confetti-butler"
DB_FILE="${BUTLER_DB_PATH:-${DB_DIR}/butler.db}"
BACKUP_DIR="${DB_DIR}/backups"
BACKUP_KEEP=5
SERVICE="confetti-butler"
HEALTH_WAIT_S=30

log() { printf '[butler-update] %s\n' "$1"; }
die() { printf '[butler-update] FATAL: %s\n' "$1" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Must run as root"
[ $# -eq 1 ] || die "Usage: butler-update.sh <butler-dir | butler.tar.gz> | --rollback"

TMP_DIR=""
cleanup() {
    [ -z "$TMP_DIR" ] || rm -rf "$TMP_DIR"
}
trap cleanup EXIT

# Port as the service sees it: butler.env wins over the default.
health_url() {
    _port=$(sed -n 's/^BUTLER_PORT=//p' "${INSTALL_DIR}/butler.env" 2>/dev/null | tail -1)
    printf 'http://127.0.0.1:%s/api/health' "${_port:-80}"
}

wait_healthy() {
    _url=$(health_url)
    _i=0
    while [ "$_i" -lt "$HEALTH_WAIT_S" ]; do
        if curl -fs -m 3 -o /dev/null "$_url"; then
            return 0
        fi
        sleep 1
        _i=$((_i + 1))
    done
    return 1
}

# Put the previous version back. $1 = where the current (bad) one is parked.
swap_back() {
    rc-service "$SERVICE" stop >/dev/null 2>&1 || true
    rm -rf "$1"
    mv "$INSTALL_DIR" "$1"
    mv "$PREV_DIR" "$INSTALL_DIR"
    rc-service "$SERVICE" start >/dev/null 2>&1 || true
}

if [ "$1" = "--rollback" ]; then
    [ -d "$PREV_DIR" ] || die "No previous version at $PREV_DIR"
    log "Rolling back to the previous version"
    swap_back "${INSTALL_DIR}.rolled-back"
    wait_healthy || die "Previous version did not become healthy — check /var/log/confetti-butler.log"
    log "Rolled back. The version you left is in ${INSTALL_DIR}.rolled-back"
    exit 0
fi

SRC="$1"
[ -d "$INSTALL_DIR" ] || die "$INSTALL_DIR not found — run build-template.sh first"

# -------------------------------------------------------------------
# 1. Locate the new code
# -------------------------------------------------------------------
TMP_DIR=$(mktemp -d)
if [ -f "$SRC" ]; then
    log "Unpacking $SRC"
    tar -xzf "$SRC" -C "$TMP_DIR" || die "Could not unpack $SRC"
    SRC_DIR=""
    for _d in "$TMP_DIR" "$TMP_DIR"/*/ "$TMP_DIR"/*/butler; do
        if [ -f "${_d%/}/serve.py" ]; then SRC_DIR="${_d%/}"; break; fi
    done
    [ -n "$SRC_DIR" ] || die "No serve.py found in $SRC"
elif [ -d "$SRC" ]; then
    SRC_DIR="${SRC%/}"
else
    die "$SRC is neither a directory nor a file"
fi

for _f in serve.py app/app.py templates static requirements.txt; do
    [ -e "${SRC_DIR}/${_f}" ] || die "${SRC_DIR} does not look like butler/: missing ${_f}"
done

# -------------------------------------------------------------------
# 2. Stage the new tree and check it before stopping anything
# -------------------------------------------------------------------
log "Staging new version in $NEW_DIR"
rm -rf "$NEW_DIR"
mkdir -p "$NEW_DIR"
cp -r "${SRC_DIR}/app" "${SRC_DIR}/templates" "${SRC_DIR}/static" "$NEW_DIR/"
mkdir -p "$NEW_DIR/seed"
if [ -d "${SRC_DIR}/seed" ]; then
    cp -r "${SRC_DIR}/seed/." "$NEW_DIR/seed/"
fi
cp -f "${SRC_DIR}/requirements.txt" "${SRC_DIR}/serve.py" "${SRC_DIR}/run.sh" "$NEW_DIR/"
cp -f "${SRC_DIR}/scripts/butler-setup.sh" "$NEW_DIR/"
cp -f "${SRC_DIR}/scripts/butler-update.sh" "$NEW_DIR/"
chmod +x "$NEW_DIR/run.sh" "$NEW_DIR/serve.py" "$NEW_DIR/butler-setup.sh" "$NEW_DIR/butler-update.sh"
# Settings and credentials stay as they are.
if [ -f "${INSTALL_DIR}/butler.env" ]; then
    cp -p "${INSTALL_DIR}/butler.env" "$NEW_DIR/butler.env"
fi

# Import the app against a throwaway database: catches a syntax error or a
# missing Python package now, while the old version is still running.
log "Checking the new code imports"
if ! (cd "$NEW_DIR" && BUTLER_DB_PATH="${TMP_DIR}/check.db" python3 -c "import app.app" 2>"${TMP_DIR}/import.err"); then
    cat "${TMP_DIR}/import.err" >&2
    rm -rf "$NEW_DIR"
    die "New version does not import — nothing was changed"
fi
find "$NEW_DIR" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

# Things this script does not update — say so rather than leave it a surprise.
if ! cmp -s "${SRC_DIR}/requirements.txt" "${INSTALL_DIR}/requirements.txt" 2>/dev/null; then
    log "WARNING: requirements.txt changed. Python packages are not updated by this script; re-run build-template.sh if the new version needs them."
fi
if [ -f "${SRC_DIR}/services/firstboot.initd" ] &&
   ! cmp -s "${SRC_DIR}/services/firstboot.initd" /etc/init.d/confetti-butler-firstboot 2>/dev/null; then
    log "WARNING: services/firstboot.initd changed. The OpenRC scripts are not updated by this script."
fi

# -------------------------------------------------------------------
# 3. Back up the database (WAL-safe: sqlite3 .backup, not a file copy)
# -------------------------------------------------------------------
BACKUP=""
if [ -f "$DB_FILE" ]; then
    command -v sqlite3 >/dev/null 2>&1 || die "sqlite3 not found (apk add sqlite) — nothing was changed"
    mkdir -p "$BACKUP_DIR"
    chmod 700 "$BACKUP_DIR"
    BACKUP="${BACKUP_DIR}/butler-pre-update-$(date -u +%Y%m%d-%H%M%S).db"
    log "Backing up the database to $BACKUP"
    sqlite3 "$DB_FILE" ".backup '${BACKUP}'" || die "Database backup failed — nothing was changed"
    chmod 600 "$BACKUP"
    # Keep the newest few pre-update backups.
    ls -1t "${BACKUP_DIR}"/butler-pre-update-*.db 2>/dev/null | tail -n +"$((BACKUP_KEEP + 1))" |
        while IFS= read -r _old; do rm -f "$_old"; done
else
    log "No database at $DB_FILE — skipping backup"
fi

# -------------------------------------------------------------------
# 4. Swap and restart
# -------------------------------------------------------------------
log "Stopping $SERVICE"
rc-service "$SERVICE" stop >/dev/null 2>&1 || true
rm -rf "$PREV_DIR"
mv "$INSTALL_DIR" "$PREV_DIR"
mv "$NEW_DIR" "$INSTALL_DIR"
log "Starting $SERVICE"
rc-service "$SERVICE" start >/dev/null 2>&1 || true

# -------------------------------------------------------------------
# 5. Health check, with automatic rollback
# -------------------------------------------------------------------
if wait_healthy; then
    log "Updated and healthy. Previous version kept in $PREV_DIR (butler-update.sh --rollback)."
    [ -z "$BACKUP" ] || log "Database backup: $BACKUP"
    exit 0
fi

log "New version did not become healthy within ${HEALTH_WAIT_S}s — rolling back"
swap_back "${INSTALL_DIR}.failed"
if wait_healthy; then
    log "Rolled back to the previous version. The failed one is in ${INSTALL_DIR}.failed; see /var/log/confetti-butler.log"
else
    log "Previous version is not healthy either — check /var/log/confetti-butler.log"
fi
[ -z "$BACKUP" ] || log "Database backup: $BACKUP"
exit 1
