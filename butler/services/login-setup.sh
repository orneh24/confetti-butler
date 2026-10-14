# /etc/profile.d/confetti-butler-setup.sh — invite an unconfigured confetti-butler VM
# to run its setup wizard at first interactive login. Guarded to interactive
# shells with a real tty so it never fires for scp/rsync/non-interactive SSH
# commands. Identical pattern to confetti-traffic's hub/services/confettictl-login-setup.sh.

case "$-" in
    *i*)
        if [ -t 0 ] && [ -f /opt/confetti-butler/BUILD ]; then
            echo "  confetti-butler $(cat /opt/confetti-butler/BUILD)"
            # Compare with the newest commit on GitHub; short timeouts so a VM without internet logs in at once.
            _have=$(cat /opt/confetti-butler/COMMIT 2>/dev/null || true)
            _latest=$(curl -fsL --connect-timeout 2 -m 3 -H 'Accept: application/vnd.github.sha'                 "https://api.github.com/repos/orneh24/confetti-butler/commits/main" 2>/dev/null | head -c 40 || true)
            case "$_latest" in
                [0-9a-f]*)
                    if [ -z "$_have" ]; then
                        echo "  Cannot tell if this is the newest build (installed from a local copy). To get the latest: butler-update.sh"
                    elif [ "$_have" = "$_latest" ]; then
                        echo "  This is the newest build."
                    else
                        echo "  A newer build is available. To update: butler-update.sh"
                    fi
                    ;;
                *) echo "  Could not check for a newer build (no internet?)." ;;
            esac
            unset _have _latest
        fi
        if [ -t 0 ] && [ ! -f /etc/confetti-butler/.setup-done ]; then
            /opt/confetti-butler/butler-setup.sh || true
        elif [ -t 0 ]; then
            _ip=$(ip -o -4 addr show scope global 2>/dev/null | awk '{print $4; exit}' | cut -d/ -f1)
            _port=$(sed -n 's/^BUTLER_PORT=//p' /opt/confetti-butler/butler.env 2>/dev/null | tr -d '"' | tail -n 1)
            case "$_port" in ""|80) _port="" ;; *) _port=":$_port" ;; esac
            [ -z "$_ip" ] || echo "  Dashboard:                    http://${_ip}${_port}/"
            unset _ip _port
            echo "  To re-run setup script:       butler-setup.sh"
            echo "  To re-configure static IP:    set-static-ip <ip/cidr> <gateway> [dns] [hostname]"
            echo
        fi
        ;;
esac
