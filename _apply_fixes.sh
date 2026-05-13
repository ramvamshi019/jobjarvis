#!/usr/bin/env bash
# Apply the fixes and verify everything is green.
# Run from project root:  bash _apply_fixes.sh
set -u
cd "$(dirname "$0")"

bar() { printf '\n──── %s ────\n' "$1"; }

bar "1. Rebuild backend/worker/beat images (frontend is unchanged, will reuse cache)"
docker compose build backend celery_worker celery_beat

bar "2. Recreate the four containers that changed (frontend healthcheck is compose-only, no rebuild needed)"
docker compose up -d --force-recreate backend celery_worker celery_beat frontend

bar "3. Wait 30s for healthchecks to settle"
sleep 30

bar "4. Status — every service should be (healthy)"
docker compose ps

bar "5. Tail celery_worker logs — should now show structured lines (PYTHONUNBUFFERED is on)"
docker compose logs --tail 30 celery_worker

bar "6. Tail celery_beat logs — should show 'beat: Starting...' and 'Scheduler: Sending due task'"
docker compose logs --tail 30 celery_beat

bar "7. Trigger a manual scan and watch the lifecycle handlers fire"
TASK_OUTPUT=$(docker compose exec -T backend python -c "
from app.workers.scan_tasks import scan_tier_companies
r = scan_tier_companies.delay('tier1')
print(r.id)
" 2>&1)
TASK_ID=$(echo "$TASK_OUTPUT" | tail -n1 | tr -d '\r')
echo "queued task id: $TASK_ID"

bar "8. Wait 20 seconds for worker to log task_received → task_success"
sleep 20

bar "9. Task state — should be SUCCESS (or STARTED if still running)"
docker compose exec -T backend python -c "
from celery.result import AsyncResult
r = AsyncResult('$TASK_ID')
print('state:', r.state)
print('info :', r.info)
" 2>&1

bar "10. Worker activity since the task was queued — should show task_received / task_success"
docker compose logs --since 30s celery_worker 2>&1 | grep -iE "task_received|task_success|task_failure|received|succeed" | tail -20

bar "11. Job-row counts (should keep climbing)"
docker compose exec -T postgres psql -U jobjarvis -d jobjarvis -c "
  SELECT
    COUNT(*) AS total_jobs,
    COUNT(*) FILTER (WHERE first_seen_at > now() - interval '5 minutes')  AS new_last_5min,
    COUNT(*) FILTER (WHERE first_seen_at > now() - interval '1 hour')     AS new_last_hour
  FROM jobs;
" 2>&1

echo ""
echo "──── DONE ────"
echo "If every service shows (healthy) in step 4 and task state is SUCCESS or STARTED in step 9, all fixes worked."
