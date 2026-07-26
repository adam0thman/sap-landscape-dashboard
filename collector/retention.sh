#!/bin/bash
# Daily time-series purge — keeps the 100G disk bounded. Cron: 02:30 daily.
# Usage: retention.sh [DAYS]   (default 90)
set -euo pipefail
DAYS="${1:-90}"
echo "=== retention $(date -u +%FT%TZ) keep ${DAYS}d ==="
docker exec sapmon-postgres psql -U grafana -d sapmon -v ON_ERROR_STOP=1 <<SQL
DELETE FROM jco_results WHERE ts < now() - interval '${DAYS} days';
DELETE FROM uptime      WHERE ts < now() - interval '${DAYS} days';
DELETE FROM jco_details WHERE ts < now() - interval '2 days';
SELECT 'jco_results' t, count(*) FROM jco_results
UNION ALL SELECT 'uptime', count(*) FROM uptime
UNION ALL SELECT 'jco_details', count(*) FROM jco_details;
SQL
echo "=== db size ==="
docker exec sapmon-postgres psql -U grafana -d sapmon -tAc "SELECT pg_size_pretty(pg_database_size('sapmon'));"
