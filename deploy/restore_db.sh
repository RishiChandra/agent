#!/usr/bin/env bash
# Restore a plain-format pg_dump (.sql) into the aipin postgres container.
# Run on the VM from the repo root (scp the dump there first):
#
#   deploy/restore_db.sh /home/ubuntu/ai_pin_db_dump_2026-09-01.sql [--yes]
#
# Destructive and idempotent: drops and recreates the `public` schema of
# $DB_NAME, then replays the dump in one transaction (nothing changes if the
# dump fails half way). Content that stock postgres:16 cannot replay is
# stripped on the fly: the azure / pgaadauth / pg_cron extensions, the cron.*
# tables and sequences, pg_dump 17's \restrict guard lines and its
# `SET transaction_timeout` (a PostgreSQL 17-only parameter). Finally
# agents.agent_url is repointed from the old Azure host to this deployment.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
ENV_FILE="deploy/.env"
PG_CONTAINER="aipin-postgres"

usage() {
    echo "usage: deploy/restore_db.sh <dump.sql> [--yes]" >&2
}

DUMP=""
YES=0
for arg in "$@"; do
    case "$arg" in
        --yes|-y) YES=1 ;;
        -h|--help) usage; exit 0 ;;
        *) DUMP="$arg" ;;
    esac
done
if [ -z "$DUMP" ] || [ ! -f "$DUMP" ]; then
    usage
    exit 1
fi
if [ ! -f "$ENV_FILE" ]; then
    echo "restore_db: $ENV_FILE missing (copy deploy/.env.example)" >&2
    exit 1
fi

# env_get KEY DEFAULT — read one value from deploy/.env without sourcing it.
env_get() {
    local v
    v="$(grep -E "^${1}=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2- || true)"
    v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
    printf '%s' "${v:-$2}"
}
DB_NAME="$(env_get DB_NAME aipin)"
DB_USER="$(env_get DB_USER aipin)"

DOCKER="docker"
docker info >/dev/null 2>&1 || DOCKER="sudo docker"
COMPOSE="$DOCKER compose --env-file $ENV_FILE"

echo "==> Ensuring postgres is up"
$COMPOSE up -d postgres >/dev/null
for _ in $(seq 1 30); do
    if [ "$($DOCKER inspect -f '{{.State.Health.Status}}' "$PG_CONTAINER" 2>/dev/null || true)" = "healthy" ]; then
        break
    fi
    sleep 2
done
if [ "$($DOCKER inspect -f '{{.State.Health.Status}}' "$PG_CONTAINER" 2>/dev/null || true)" != "healthy" ]; then
    echo "restore_db: $PG_CONTAINER did not become healthy" >&2
    exit 1
fi

if [ "$YES" -ne 1 ]; then
    read -r -p "This DROPS schema public in database '$DB_NAME' on this VM and restores '$DUMP'. Continue? [y/N] " ans
    case "$ans" in
        y|Y|yes|YES) ;;
        *) echo "aborted"; exit 1 ;;
    esac
fi

PSQL="$DOCKER exec -i $PG_CONTAINER psql -v ON_ERROR_STOP=1 -q -U $DB_USER -d $DB_NAME"

# Strip what stock postgres cannot replay. awk rather than psql-side tricks so
# the dump file itself is never modified.
filter_dump() {
    awk '
        /^\\restrict /   { next }
        /^\\unrestrict / { next }
        /^SET transaction_timeout / { next }
        /^CREATE EXTENSION IF NOT EXISTS (azure|pgaadauth|pg_cron) / { next }
        /^COMMENT ON EXTENSION (azure|pgaadauth|pg_cron) /           { next }
        /^SELECT pg_catalog\.setval\(.cron\./                        { next }
        /^COPY cron\./   { skipping = 1; next }
        skipping && /^\\\.$/ { skipping = 0; next }
        skipping         { next }
        { print }
    ' "$1"
}

echo "==> Resetting schema public in $DB_NAME"
$PSQL <<'SQL'
DROP SCHEMA IF EXISTS public CASCADE;
CREATE SCHEMA public;
SQL

echo "==> Restoring $DUMP"
filter_dump "$DUMP" | $PSQL --single-transaction

# Schema the Oracle setup adds on top of the dump (e.g. the `jobs` queue table
# that replaced Service Bus). All files are idempotent; deploy.sh applies them
# on every deploy too, so a fresh database without a dump gets them as well.
for f in deploy/sql/*.sql; do
    [ -f "$f" ] || continue
    echo "==> Applying $f"
    $PSQL < "$f"
done

# The dump's agents.agent_url still names the old Azure App Service. The
# orchestrator dials it from inside the app container to bridge to the Kairos
# agent (app/developer_ws/pipeline.py), so point it at the compose-internal
# endpoint: OCI's public IPv4 does not hairpin from the VM. Prints UPDATE 0
# when there is nothing left to repoint.
echo "==> Repointing agents.agent_url from Azure to ws://app:8000"
$DOCKER exec -i "$PG_CONTAINER" psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" -c \
    "UPDATE agents SET agent_url = regexp_replace(agent_url, '^wss?://[^/]+', 'ws://app:8000') WHERE agent_url LIKE '%azurewebsites.net/%';"

echo "==> Done. Tables in $DB_NAME:"
$DOCKER exec "$PG_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -c '\dt public.*'
