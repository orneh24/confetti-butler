#!/bin/sh
# butler-backup — nightly WAL-safe backup of the confetti-butler database.
#
# Installed as /etc/periodic/daily/butler-backup (no dot in the name: run-parts
# skips names that contain one). Alpine's crond runs that directory at 02:00.
# Uses sqlite3 .backup, not a file copy — a copy of butler.db taken while the
# server is writing can miss what is still in the -wal file.
#
# Keeps the newest BUTLER_BACKUP_KEEP (default 7) daily backups. Pre-update
# backups made by butler-update.sh live in the same folder and are rotated
# separately. Backups hold device credentials in plaintext, like the database
# itself, so the folder is 0700 and the files 0600.

set -eu

ENV_FILE="/opt/confetti-butler/butler.env"
DB_FILE="/var/lib/confetti-butler/butler.db"
KEEP=7

if [ -f "$ENV_FILE" ]; then
    _v=$(sed -n 's/^BUTLER_DB_PATH=//p' "$ENV_FILE" | tail -1)
    [ -z "$_v" ] || DB_FILE="$_v"
    _v=$(sed -n 's/^BUTLER_BACKUP_KEEP=//p' "$ENV_FILE" | tail -1)
    case "$_v" in ''|*[!0-9]*) ;; *) KEEP="$_v" ;; esac
fi

BACKUP_DIR="$(dirname "$DB_FILE")/backups"

fail() {
    logger -t butler-backup "FAILED: $1"
    printf 'butler-backup: %s\n' "$1" >&2
    exit 1
}

[ -f "$DB_FILE" ] || exit 0   # nothing to back up yet
command -v sqlite3 >/dev/null 2>&1 || fail "sqlite3 not found (apk add sqlite)"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

OUT="${BACKUP_DIR}/butler-daily-$(date -u +%Y%m%d).db"
TMP="${OUT}.tmp"
rm -f "$TMP"
# Write to a temp name and rename, so a half-written file never counts as a backup.
sqlite3 "$DB_FILE" ".backup '${TMP}'" || { rm -f "$TMP"; fail "sqlite3 .backup failed"; }
chmod 600 "$TMP"
mv -f "$TMP" "$OUT"

# Rotate: newest KEEP stay.
ls -1t "${BACKUP_DIR}"/butler-daily-*.db 2>/dev/null | tail -n +"$((KEEP + 1))" |
    while IFS= read -r _old; do rm -f "$_old"; done

logger -t butler-backup "ok: $OUT"
