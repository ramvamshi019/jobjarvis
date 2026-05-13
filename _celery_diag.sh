#!/usr/bin/env bash
# Pinpoint why manually-queued Celery tasks stay PENDING.
# Run from project root:  bash _celery_diag.sh
set -u
cd "$(dirname "$0")"

bar() { printf '\n──── %s ────\n' "$1"; }

bar "1. Redis queue depths (db 1 = broker)"
for q in default scans ai reports celery; do
  depth=$(docker compose exec -T redis redis-cli -n 1 llen "$q" 2>/dev/null | tr -d '\r')
  printf "  %-10s = %s\n" "$q" "${depth:-0}"
done

bar "2. All keys in broker DB 1 (look for task messages waiting)"
docker compose exec -T redis redis-cli -n 1 keys '*' 2>&1 | head -30

bar "3. Celery inspect — active tasks (what's running right now)"
docker compose exec -T backend celery -A app.workers.celery_app inspect active --timeout 5 2>&1 | head -60

bar "4. Celery inspect — reserved tasks (prefetched, waiting for a slot)"
docker compose exec -T backend celery -A app.workers.celery_app inspect reserved --timeout 5 2>&1 | head -60

bar "5. Celery inspect — registered tasks (sanity check)"
docker compose exec -T backend celery -A app.workers.celery_app inspect registered --timeout 5 2>&1 | head -15

bar "6. Celery inspect — stats (concurrency, prefetch, pool)"
docker compose exec -T backend celery -A app.workers.celery_app inspect stats --timeout 5 2>&1 | head -40

bar "7. Queue a fast no-op task and see if it runs in 5s"
TASK_OUTPUT=$(docker compose exec -T backend python -c "
from app.workers.celery_app import celery_app
r = celery_app.send_task('app.workers.scan_tasks.scan_tier_companies', args=['tier3'], queue='scans')
print(r.id)
" 2>&1)
TASK_ID=$(echo "$TASK_OUTPUT" | tail -n1 | tr -d '\r')
echo "queued task id: $TASK_ID"
sleep 8
docker compose exec -T backend python -c "
from celery.result import AsyncResult
r = AsyncResult('$TASK_ID')
print('state:', r.state)
print('info :', r.info)
" 2>&1

bar "8. Worker stdout — most recent 60 lines, no grep filter"
docker compose logs --tail 60 celery_worker 2>&1 | tail -60

echo ""
echo "──── DONE ────"
