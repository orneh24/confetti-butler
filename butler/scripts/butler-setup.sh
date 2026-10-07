#!/bin/sh
# butler-setup.sh — interactive first-time configuration for confetti-butler.
#
# Run automatically at first interactive login (see services/login-setup.sh)
# when the VM hasn't been configured yet, or by hand at any time. Safe to
# re-run; does nothing once configured unless you pass --force. Identical
# pattern to confetti-traffic's hub/scripts/confettictl-hub-setup.sh — only the static IP is handled here,
# butler.env defaults are sane enough not to need a prompt.

set -eu

CONF_DIR="/etc/confetti-butler"
STAMP="${CONF_DIR}/.setup-done"

mkdir -p "$CONF_DIR"

if [ -f "$STAMP" ] && [ "${1:-}" != "--force" ]; then
    echo "confetti-butler already configured ($(cat "$STAMP"))."
    echo "Re-run with --force to reconfigure."
    exit 0
fi

echo "=== confetti-butler setup ==="
echo

CURRENT_IP=$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | head -1)
echo "Current address: ${CURRENT_IP:-none}"
echo

printf 'Configure a static IP now? [Y/n] '
read -r ANSWER
case "$ANSWER" in
    [nN]*)
        printf "Skip and don't ask again at login? [y/N] "
        read -r SKIP
        case "$SKIP" in
            [yY]*)
                date -u '+%Y-%m-%dT%H:%M:%SZ skipped' > "$STAMP"
                echo "Won't ask again. Run 'butler-setup.sh --force' any time to configure."
                ;;
            *)
                echo "Skipped for now -- you'll be asked again at next login."
                ;;
        esac
        exit 0
        ;;
esac

printf 'Static IP/CIDR (e.g. 10.0.0.101/24): '
read -r IP_CIDR
printf 'Gateway (e.g. 10.0.0.1): '
read -r GATEWAY

if [ -z "$IP_CIDR" ] || [ -z "$GATEWAY" ]; then
    echo "Both values are required -- aborting, nothing changed."
    exit 1
fi

echo
echo "About to set: ${IP_CIDR} via ${GATEWAY}"
printf 'Apply now? [y/N] '
read -r CONFIRM
case "$CONFIRM" in
    [yY]*) ;;
    *)
        echo "Cancelled -- nothing changed."
        exit 0
        ;;
esac

set-static-ip "$IP_CIDR" "$GATEWAY"
rc-service networking restart

date -u '+%Y-%m-%dT%H:%M:%SZ configured' > "$STAMP"

echo
echo "Static IP set. Dashboard: http://${IP_CIDR%/*}/"
echo "Edit /opt/confetti-butler/butler.env if the defaults don't suit, then:"
echo "  rc-service confetti-butler restart"
