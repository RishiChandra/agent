#!/usr/bin/env bash
# Apply the schema migrations in deploy/sql/ to one database, in file order.
# Every file is idempotent, so re-running is safe. Run on the VM as ubuntu
# (host PostgreSQL) BEFORE deploying an image that needs the new schema:
#
#   deploy/migrate.sh ai_pin_db
#
# Files are piped to psql on stdin, so the postgres role doesn't need read
# access to the (private) release directory. Override the client with PSQL,
# e.g. PSQL="psql -h localhost -U appuser".
#
# The app and worker never create or alter tables themselves (DATABASE.md
# "Migrations").
set -euo pipefail
db="${1:?usage: migrate.sh <database>}"
psql_cmd="${PSQL:-sudo -u postgres psql}"
dir="$(cd "$(dirname "$0")/sql" && pwd)"
for f in "$dir"/*.sql; do
    echo "== $(basename "$f")"
    $psql_cmd -X -v ON_ERROR_STOP=1 -d "$db" < "$f"
done
