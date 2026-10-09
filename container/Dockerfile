# confetti-butler as a container, for development, demos and trying it out
# without an ESXi VM. The supported deployment is still the Alpine VM template
# (butler/build-template.sh); this image uses the same Alpine packages and the
# same pinned pip packages.
#
# Build (from the repo root):
#   docker build -f container/Dockerfile -t confetti-butler butler
# Run: see container/docker-compose.yml.

FROM alpine:3.24

# Same package set as build-template.sh, minus what only makes sense on a VM
# (open-vm-tools, chrony, lldpd, dropbear, OpenRC services). curl is for the
# HEALTHCHECK, sqlite for the backup tool, net-snmp-tools for platform "snmp".
RUN apk add --no-cache \
        python3 py3-pip py3-flask py3-paramiko py3-yaml py3-requests py3-jinja2 \
        sqlite curl net-snmp-tools

COPY requirements.txt /opt/confetti-butler/requirements.txt
# netmiko and waitress are not Alpine packages; install them at the pinned versions.
RUN pip3 install --no-cache-dir --break-system-packages \
        "$(grep -i '^netmiko==' /opt/confetti-butler/requirements.txt)" \
        "$(grep -i '^waitress==' /opt/confetti-butler/requirements.txt)"

COPY app /opt/confetti-butler/app
COPY templates /opt/confetti-butler/templates
COPY static /opt/confetti-butler/static
COPY seed /opt/confetti-butler/seed
COPY serve.py /opt/confetti-butler/serve.py

# BUTLER_HEALTH_SERVICES is empty: there is no OpenRC in a container to ask.
ENV BUTLER_DB_PATH=/var/lib/confetti-butler/butler.db \
    BUTLER_PORT=80 \
    BUTLER_HEALTH_SERVICES=

WORKDIR /opt/confetti-butler
VOLUME /var/lib/confetti-butler
EXPOSE 80 514/udp

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD curl -fs "http://127.0.0.1:${BUTLER_PORT}/api/health" >/dev/null || exit 1

CMD ["python3", "/opt/confetti-butler/serve.py"]
