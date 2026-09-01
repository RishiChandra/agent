#!/usr/bin/env bash
# Deploy the agent backend to the Oracle Cloud VM.
#
#   Mac:  deploy/deploy.sh             rsync the repo + deploy/.env to the VM,
#                                      then run the VM-side steps over ssh
#   VM:   deploy/deploy.sh --remote    the VM-side steps (what the Mac run
#                                      invokes; fine to run by hand on the VM)
#
# VM-side steps: host firewall, models, mosquitto passwd/acl, compose build +
# up, certsync cron, first cert sync, status. Every step is idempotent, so
# re-running only rebuilds/restarts what changed.
#
# Override with env vars: VM_HOST, VM_USER, REMOTE_DIR.
set -euo pipefail

VM_HOST="${VM_HOST:-146.235.229.232}"
VM_USER="${VM_USER:-ubuntu}"
REMOTE_DIR="${REMOTE_DIR:-/home/ubuntu/agent}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
ENV_FILE="deploy/.env"

log() { printf '\n==> %s\n' "$*"; }
die() { echo "deploy: $*" >&2; exit 1; }

# env_get KEY DEFAULT — read one value from deploy/.env without sourcing it.
env_get() {
    local v
    v="$(grep -E "^${1}=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2- || true)"
    v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
    printf '%s' "${v:-$2}"
}

# ============================================================== Mac side ====
local_deploy() {
    [ -f "$ENV_FILE" ] || die "$ENV_FILE missing — copy deploy/.env.example to deploy/.env and fill it in"
    local target="$VM_USER@$VM_HOST"

    log "Preparing $target:$REMOTE_DIR"
    ssh "$target" "mkdir -p '$REMOTE_DIR' && (command -v rsync >/dev/null || sudo apt-get install -y -qq rsync)"

    log "Syncing repo -> $target:$REMOTE_DIR"
    # Excluded paths are also protected from --delete: models (data/), the
    # generated mosquitto passwd/acl and synced certs stay on the VM.
    rsync -a --delete -e ssh \
        --exclude='.git' \
        --exclude='.claude' \
        --exclude='.env' \
        --exclude='data' \
        --exclude='deploy/mosquitto/passwd' \
        --exclude='deploy/mosquitto/acl' \
        --exclude='deploy/mosquitto/certs' \
        --exclude='mobile_app' \
        --exclude='vosk-model-*' \
        --exclude='piper_voices' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='.venv' \
        --exclude='venv' \
        --exclude='node_modules' \
        --exclude='*.zip' \
        --exclude='.DS_Store' \
        ./ "$target:$REMOTE_DIR/"

    log "Syncing $ENV_FILE (secrets; never committed)"
    rsync -a -e ssh "$ENV_FILE" "$target:$REMOTE_DIR/deploy/.env"

    log "Running VM-side deploy"
    ssh "$target" "bash '$REMOTE_DIR/deploy/deploy.sh' --remote"
}

# =============================================================== VM side ====
DOCKER="docker"
COMPOSE=""

open_firewall() {
    # Oracle's Ubuntu image ships iptables/ip6tables rules that REJECT all
    # inbound traffic except ssh. Allow the three public ports if no rule
    # already does (the OCI security list must allow them too — see README).
    local ipt port changed=0
    for ipt in iptables ip6tables; do
        command -v "$ipt" >/dev/null 2>&1 || continue
        if sudo "$ipt" -C INPUT -p tcp -m multiport --dports 80,443,8883 -j ACCEPT 2>/dev/null; then
            continue
        fi
        for port in 80 443 8883; do
            if ! sudo "$ipt" -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null; then
                sudo "$ipt" -I INPUT 1 -p tcp --dport "$port" -j ACCEPT
                changed=1
            fi
        done
    done
    if [ "$changed" -eq 1 ]; then
        echo "opened tcp 80/443/8883 in host firewall"
        if command -v netfilter-persistent >/dev/null 2>&1; then
            sudo netfilter-persistent save >/dev/null
        fi
    else
        echo "host firewall already allows tcp 80/443/8883"
    fi
}

render_mosquitto_auth() {
    local u p du dp prefix dev
    u="$(env_get MQTT_USERNAME backend)"
    p="$(env_get MQTT_PASSWORD "")"
    du="$(env_get MQTT_DEVICE_USERNAME esp32s3)"
    dp="$(env_get MQTT_DEVICE_PASSWORD "")"
    prefix="$(env_get MQTT_COMMAND_TOPIC_PREFIX aipin)"
    dev="$(env_get DEVICE_ID esp32s3)"
    [ -n "$p" ]  || die "MQTT_PASSWORD is empty in $ENV_FILE"
    [ -n "$dp" ] || die "MQTT_DEVICE_PASSWORD is empty in $ENV_FILE"

    # Mosquitto 2.1 insists that passwd/acl are owned by its user (uid 1883)
    # and not world-readable; both files end up 640 root-of-container:1883.

    # Password file: hashed by mosquitto_passwd inside the broker image so the
    # hash format always matches the broker. Credentials travel via env vars
    # (not argv). `-U` hashes a plaintext user:password file in place.
    $DOCKER run --rm \
        -v "$REPO_ROOT/deploy/mosquitto:/work" \
        -e "U1=$u" -e "P1=$p" -e "U2=$du" -e "P2=$dp" \
        eclipse-mosquitto:2 sh -c '
            umask 077 &&
            printf "%s:%s\n%s:%s\n" "$U1" "$P1" "$U2" "$P2" > /work/passwd.tmp &&
            mosquitto_passwd -U /work/passwd.tmp &&
            chown 1883:1883 /work/passwd.tmp && chmod 640 /work/passwd.tmp &&
            mv -f /work/passwd.tmp /work/passwd'
    echo "wrote deploy/mosquitto/passwd (users: $u, $du)"

    # ACL: substitute the @TOKENS@ of the committed acl.template into acl
    # (gitignored and rsync-excluded, like passwd). Rendered to a temp file
    # first because the installed acl is owned by uid 1883 and not writable
    # by us; the template itself is never touched, so re-runs are safe.
    python3 - "$REPO_ROOT/deploy/mosquitto/acl.template" "$REPO_ROOT/deploy/mosquitto/acl.tmp" \
        "$u" "$du" "$prefix" "$dev" <<'PY'
import pathlib, sys
template, out, backend, device, prefix, device_id = sys.argv[1:]
text = pathlib.Path(template).read_text()
for token, value in {
    "MQTT_USERNAME": backend,
    "MQTT_DEVICE_USERNAME": device,
    "MQTT_COMMAND_TOPIC_PREFIX": prefix,
    "DEVICE_ID": device_id,
}.items():
    text = text.replace(f"@{token}@", value)
pathlib.Path(out).write_text(text)
PY
    sudo install -o 1883 -g 1883 -m 640 "$REPO_ROOT/deploy/mosquitto/acl.tmp" "$REPO_ROOT/deploy/mosquitto/acl"
    rm -f "$REPO_ROOT/deploy/mosquitto/acl.tmp"
    echo "rendered deploy/mosquitto/acl from acl.template (device topics: $prefix/$dev/#)"
}

apply_sql_migrations() {
    # Schema the Oracle setup adds on top of the restored dump (e.g. the `jobs`
    # queue table that replaced Service Bus). Every file must be idempotent
    # (CREATE ... IF NOT EXISTS); restore_db.sh runs the same files.
    local db_name db_user f
    db_name="$(env_get DB_NAME aipin)"
    db_user="$(env_get DB_USER aipin)"
    for f in deploy/sql/*.sql; do
        [ -f "$f" ] || continue
        echo "applying $f"
        $DOCKER exec -i aipin-postgres psql -v ON_ERROR_STOP=1 -q -U "$db_user" -d "$db_name" < "$f"
    done
}

install_certsync_cron() {
    local script="$REPO_ROOT/deploy/mosquitto/certsync.sh"
    local line="*/10 * * * * $script >> /var/log/aipin-certsync.log 2>&1"
    local current
    current="$(sudo crontab -l 2>/dev/null || true)"
    if printf '%s\n' "$current" | grep -Fq "$script"; then
        echo "root crontab already runs certsync.sh every 10 min"
    else
        printf '%s\n%s\n' "$current" "$line" | sed '/^$/d' | sudo crontab -
        echo "installed root cron: $line"
    fi
}

wait_for_cert() {
    # certsync.sh exits 2 while Caddy has not obtained the certificate yet.
    local host="$1" i rc
    for i in $(seq 1 36); do
        rc=0
        sudo "$REPO_ROOT/deploy/mosquitto/certsync.sh" || rc=$?
        if [ "$rc" -eq 0 ]; then
            echo "mosquitto certificate for $host is in place"
            return 0
        fi
        if [ "$rc" -ne 2 ]; then
            echo "certsync.sh failed (exit $rc)" >&2
            return 1
        fi
        if [ $((i % 6)) -eq 0 ]; then
            echo "still waiting for Let's Encrypt to issue $host ..."
        fi
        sleep 5
    done
    echo "WARN: no certificate for $host after 3 min. Check 'docker logs aipin-caddy'."
    echo "      mosquitto keeps restarting until cron runs certsync.sh successfully."
    return 0
}

remote_deploy() {
    [ -f "$ENV_FILE" ] || die "$ENV_FILE missing on the VM — run deploy/deploy.sh from the Mac first"
    docker info >/dev/null 2>&1 || DOCKER="sudo docker"
    COMPOSE="$DOCKER compose --env-file $ENV_FILE"
    local site_v4 site_v6 mqtt_host
    site_v4="$(env_get SITE_HOST_V4 146-235-229-232.sslip.io)"
    site_v6="$(env_get SITE_HOST_V6 2603-c024-c020-3700-0-537e-9221-8587.sslip.io)"
    mqtt_host="$(env_get MQTT_TLS_HOST "$site_v4")"

    chmod 600 "$ENV_FILE"
    chmod +x deploy/*.sh deploy/mosquitto/certsync.sh

    log "Host firewall"
    open_firewall

    log "Speech models"
    bash deploy/download_models.sh "$REPO_ROOT/data"

    log "Mosquitto auth files"
    mkdir -p deploy/mosquitto/certs
    render_mosquitto_auth

    log "Building image"
    $COMPOSE build

    log "Starting services"
    $COMPOSE up -d --remove-orphans
    # Password/ACL changes only take effect on reload if mosquitto was already running.
    if [ "$($DOCKER inspect -f '{{.State.Running}}' aipin-mosquitto 2>/dev/null || true)" = "true" ]; then
        $DOCKER kill -s HUP aipin-mosquitto >/dev/null
    fi

    log "Database schema additions (deploy/sql/*.sql, idempotent)"
    # `up -d` only returns once postgres is healthy (app/worker depend on it).
    apply_sql_migrations

    log "Certificate sync for MQTT"
    install_certsync_cron
    wait_for_cert "$mqtt_host"

    log "Status"
    $COMPOSE ps
    echo
    # Oracle NATs the public IPv4 at the gateway, so from the VM itself we pin
    # each name to 127.0.0.1 (keeps the SNI/Host header) instead of routing out.
    local h out
    for h in "$site_v4" "$site_v6"; do
        out="$(curl -fsS --max-time 15 --resolve "$h:443:127.0.0.1" "https://$h/healthz" 2>&1 || true)"
        printf '  https://%s/healthz -> %s\n' "$h" "${out:-(not reachable yet; see docker logs aipin-caddy)}"
    done
    echo
    echo "  WSS:  wss://$site_v4/ws/developer/{user_id}   (and wss://$site_v6/...)"
    echo "  MQTT: $mqtt_host:8883 (TLS), device topic $(env_get MQTT_COMMAND_TOPIC_PREFIX aipin)/$(env_get DEVICE_ID esp32s3)/cmd"
    echo "  Logs: docker compose --env-file deploy/.env logs -f app worker"
}

case "${1:-}" in
    --remote) remote_deploy ;;
    "")       local_deploy ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *)        die "unknown argument: $1 (use --remote on the VM, nothing on the Mac)" ;;
esac
