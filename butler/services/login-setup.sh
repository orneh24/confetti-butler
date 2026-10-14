# /etc/profile.d/confetti-butler-setup.sh — invite an unconfigured confetti-butler VM
# to run its setup wizard at first interactive login. Guarded to interactive
# shells with a real tty so it never fires for scp/rsync/non-interactive SSH
# commands. Identical pattern to confetti-traffic's hub/services/confettictl-login-setup.sh.

case "$-" in
    *i*)
        if [ -t 0 ] && [ -f /opt/confetti-butler/BUILD ]; then
            echo "  confetti-butler $(cat /opt/confetti-butler/BUILD)"
        fi
        if [ -t 0 ] && [ ! -f /etc/confetti-butler/.setup-done ]; then
            /opt/confetti-butler/butler-setup.sh || true
        elif [ -t 0 ]; then
            echo "  To re-run setup script:       butler-setup.sh"
            echo "  To re-configure static IP:    set-static-ip <ip/cidr> <gateway> [dns] [hostname]"
            echo
        fi
        ;;
esac
