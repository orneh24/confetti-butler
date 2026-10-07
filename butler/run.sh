#!/bin/sh
# Foreground launcher for confetti-butler (manual runs and debugging).
# In production the OpenRC service confetti-butler runs serve.py directly.

cd "$(dirname "$0")" || exit 1

# Pick up butler.env if present so a manual run matches the service's config.
if [ -f ./butler.env ]; then
    while IFS= read -r line; do
        case "$line" in
            \#*|"") continue ;;
            *=*) export "$line" ;;
        esac
    done < ./butler.env
fi

exec python3 ./serve.py
