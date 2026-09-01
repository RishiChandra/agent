#!/usr/bin/env bash
# Copy the Let's Encrypt certificate Caddy obtained for MQTT_TLS_HOST (default:
# SITE_HOST_V4) out of the caddy container into deploy/mosquitto/certs/ and
# SIGHUP mosquitto so it reloads it. Runs on the VM from root's crontab every
# 10 minutes (installed by deploy/deploy.sh); safe to run by hand any time.
#
# Exit codes: 0 = certs up to date (installed or unchanged), 2 = Caddy has no
# certificate for the host yet, 1 = error. Quiet unless something changes.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Root is needed to give the private key to the container's mosquitto user only.
if [ "$(id -u)" -ne 0 ]; then
    exec sudo -n "$0" "$@"
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="$REPO_ROOT/deploy/.env"
CERT_DIR="$REPO_ROOT/deploy/mosquitto/certs"
CADDY_CONTAINER="${CADDY_CONTAINER:-aipin-caddy}"
MOSQUITTO_CONTAINER="${MOSQUITTO_CONTAINER:-aipin-mosquitto}"
# uid/gid of the `mosquitto` user inside eclipse-mosquitto:2. It drops root
# before loading TLS files, so the key must be readable by it.
MOSQUITTO_UID=1883
MOSQUITTO_GID=1883

# env_get KEY DEFAULT — read one value from deploy/.env without sourcing it.
env_get() {
    local v
    v="$(grep -E "^${1}=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2- || true)"
    v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
    printf '%s' "${v:-$2}"
}

SITE_HOST_V4="$(env_get SITE_HOST_V4 146-235-229-232.sslip.io)"
HOST="$(env_get MQTT_TLS_HOST "$SITE_HOST_V4")"

if [ "$(docker inspect -f '{{.State.Running}}' "$CADDY_CONTAINER" 2>/dev/null || true)" != "true" ]; then
    echo "certsync: container $CADDY_CONTAINER is not running" >&2
    exit 1
fi

# Caddy stores certificates at /data/caddy/certificates/<issuer>/<host>/<host>.{crt,key}.
# Prefer Let's Encrypt production; fall back to whatever issuer Caddy ended up using.
SRC_DIR="$(docker exec "$CADDY_CONTAINER" sh -c '
    host="$1"
    for d in /data/caddy/certificates/acme-v02.api.letsencrypt.org-directory/"$host" \
             /data/caddy/certificates/*/"$host"; do
        if [ -f "$d/$host.crt" ] && [ -f "$d/$host.key" ]; then
            echo "$d"; break
        fi
    done' sh "$HOST")"

if [ -z "$SRC_DIR" ]; then
    echo "certsync: no certificate for $HOST in $CADDY_CONTAINER yet" >&2
    exit 2
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
docker exec "$CADDY_CONTAINER" cat "$SRC_DIR/$HOST.crt" > "$TMP/server.crt"
docker exec "$CADDY_CONTAINER" cat "$SRC_DIR/$HOST.key" > "$TMP/server.key"
if [ ! -s "$TMP/server.crt" ] || [ ! -s "$TMP/server.key" ]; then
    echo "certsync: empty certificate or key read from $CADDY_CONTAINER:$SRC_DIR" >&2
    exit 1
fi

mkdir -p "$CERT_DIR"
chmod 755 "$CERT_DIR"

if cmp -s "$TMP/server.crt" "$CERT_DIR/server.crt" && cmp -s "$TMP/server.key" "$CERT_DIR/server.key"; then
    exit 0
fi

install -m 644 -o root -g root "$TMP/server.crt" "$CERT_DIR/server.crt"
install -m 640 -o "$MOSQUITTO_UID" -g "$MOSQUITTO_GID" "$TMP/server.key" "$CERT_DIR/server.key"

EXPIRES=""
if command -v openssl >/dev/null 2>&1; then
    EXPIRES=" ($(openssl x509 -noout -enddate -in "$CERT_DIR/server.crt" 2>/dev/null || true))"
fi

if [ "$(docker inspect -f '{{.State.Running}}' "$MOSQUITTO_CONTAINER" 2>/dev/null || true)" = "true" ]; then
    docker kill -s HUP "$MOSQUITTO_CONTAINER" >/dev/null
    echo "certsync: $(date -Is) installed certificate for $HOST$EXPIRES; reloaded $MOSQUITTO_CONTAINER"
else
    # Mosquitto refuses to start without the cert files; now that they exist,
    # kick it instead of waiting for docker's restart backoff.
    docker start "$MOSQUITTO_CONTAINER" >/dev/null 2>&1 || true
    echo "certsync: $(date -Is) installed certificate for $HOST$EXPIRES; started $MOSQUITTO_CONTAINER"
fi
