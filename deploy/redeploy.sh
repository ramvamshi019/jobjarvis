#!/usr/bin/env bash
#
# Safe, low-thrash production redeploy for the 2-vCPU VM.
#
# `docker compose up -d --build` builds every image in parallel. On the
# 2-vCPU box with a cold cache that thrashes CPU/disk and can wedge. This
# script instead: pulls, frees space only if disk is tight (images +
# stopped containers — never volumes/DB, never the build cache right
# before a build), builds the shared Python image ONCE, then the rest
# (cache-warm = fast), then swaps containers with `up -d` (no rebuild).
# The site keeps serving the old images until the final swap.
#
# Usage (from anywhere, NO sudo — docker calls self-elevate):
#   bash deploy/redeploy.sh
#
set -euo pipefail

SUDO=${SUDO:-sudo}
cd "$(dirname "$0")/.."
CF="deploy/docker-compose.prod.yml"
EF="deploy/.env"
COMPOSE="$SUDO docker compose -f $CF --env-file $EF"

echo "==> git pull (fast-forward only)"
git pull --ff-only

echo "==> disk before:"
df -h / | tail -1
USE=$(df --output=pcent / | tail -1 | tr -dc '0-9')
if [ "${USE:-0}" -ge 85 ]; then
  echo "==> disk ${USE}% >= 85% — pruning old images + stopped containers"
  echo "    (volumes/DB and the warm build cache are left untouched)"
  $SUDO docker image prune -f
  $SUDO docker container prune -f
  df -h / | tail -1
fi

echo "==> building shared backend image (slow ONLY when cache is cold)"
$COMPOSE build backend

echo "==> building remaining images (reuse backend cache — fast)"
$COMPOSE build celery_worker celery_worker_scans celery_beat frontend

echo "==> swapping containers (no rebuild)"
$COMPOSE up -d

echo "==> status"
$COMPOSE ps

cat <<'EOF'

==> Done. Verify scan coverage:
  sudo docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env \
    exec postgres psql -U jobjarvis -d jobjarvis -c \
    "SELECT count(*) FILTER (WHERE last_scanned_at >= now() - interval '24 hours') AS scanned_24h, \
            count(*) FILTER (WHERE last_success_at IS NULL) AS never_scanned, \
            count(*) AS scannable \
       FROM companies \
      WHERE active AND NOT is_blocklisted AND ats IS NOT NULL AND ats_identifier IS NOT NULL;"
EOF
