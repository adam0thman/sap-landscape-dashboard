#!/usr/bin/env bash
# Load schema + demo data into a running postgres (defaults match docker-compose).
set -euo pipefail
: "${PGHOST:=127.0.0.1}" "${PGPORT:=5432}" "${PGUSER:=dashboard}" "${PGDATABASE:=sapmon}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
psql "host=$PGHOST port=$PGPORT user=$PGUSER dbname=$PGDATABASE" -v ON_ERROR_STOP=1 -f "$DIR/db/schema.sql"
psql "host=$PGHOST port=$PGPORT user=$PGUSER dbname=$PGDATABASE" -v ON_ERROR_STOP=1 -f "$DIR/db/seed_demo.sql"
echo "seeded."
