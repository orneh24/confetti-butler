# /etc/profile.d/lab-butler-setup.sh — invite an unconfigured lab-butler VM
# to run its setup wizard at first interactive login. Guarded to interactive
# shells with a real tty so it never fires for scp/rsync/non-interactive SSH
# commands. Identical pattern to confetti-traffic's hub/services/confettictl-login-setup.sh.

case "$-" in
    *i*)
        if [ -t 0 ] && [ ! -f /etc/lab-butler/.setup-done ]; then
            /opt/lab-butler/butler-setup.sh || true
        fi
        ;;
esac
